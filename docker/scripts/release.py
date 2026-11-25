#!/usr/bin/env python3
"""Promote digest-pinned Compose releases through Komodo."""

import argparse
import contextlib
from dataclasses import dataclass, field
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
import uuid

NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CATALOG = Path("docker/komodo/releases.toml")
REVISION = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
PHASES = {"preflight", "deploy", "probe"}
RESULTS = {"unchanged", "restored", "recovery blocked"}
MANAGED_LABEL = "io.technis.release.managed"
STACK_ARGS = ["--wait", "--wait-timeout", "300", "--no-build", "--pull", "never", "--no-deps"]
DETAILS = {"baseline", "registry", "executor", "running-image", "container-health", "client-probe",
           "volume", "sqlite-compatibility", "interrupted"}


class ReleaseError(Exception):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


def failure_detail(error, phase):
    detail = getattr(error, "detail", None)
    if detail in DETAILS:
        return detail
    return {"preflight": "baseline", "deploy": "executor", "probe": "client-probe"}[phase]


@dataclass(frozen=True)
class Target:
    host: str
    service: str
    architecture: str
    repository: str
    tags: str
    environment: dict
    probe: dict
    empty_mount: str | None = None
    source_service: str | None = None
    image_user: str | None = None
    depends_on: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    read_only: bool = False
    sqlite_db: str | None = None

    def __post_init__(self):
        if not NAME.fullmatch(self.host) or not NAME.fullmatch(self.service):
            raise ReleaseError("Invalid Compose release target")
        if self.architecture not in {"amd64", "arm64"}:
            raise ReleaseError("Unsupported release architecture")
        if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9_.-]+)+", self.repository):
            raise ReleaseError("An explicit image repository is required")
        if self.empty_mount and not re.fullmatch(r"(?:/[a-zA-Z0-9_.-]+)+", self.empty_mount):
            raise ReleaseError("Invalid release volume target")
        if self.source_service and not NAME.fullmatch(self.source_service):
            raise ReleaseError("Invalid Compose source directory")
        if self.image_user and (self.empty_mount or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", self.image_user)):
            raise ReleaseError("An inherited image user must be a non-root UID and GID on a volume-free service")
        if self.sqlite_db and (self.empty_mount or self.image_user or not self.read_only
                               or not re.fullmatch(r"/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+\.db", self.sqlite_db)):
            raise ReleaseError("SQLite releases require one dedicated database bind and a read-only root filesystem")
        if (any(not NAME.fullmatch(dependency) or dependency == self.service for dependency in self.depends_on)
                or MANAGED_LABEL in self.labels):
            raise ReleaseError("Invalid Compose service policy")

    @property
    def source(self):
        return Path("docker/services") / (self.source_service or self.service) / "compose.yaml"

    @property
    def stack(self):
        return Path("docker/stacks") / self.host / "compose.yaml"

    @property
    def name(self):
        return f"{self.host}/{self.service}"

    @property
    def sqlite_directory(self):
        return str(Path(self.sqlite_db).parent)

    def require_image(self, image):
        tagged, sep, digest = image.partition("@")
        repo, colon, tag = tagged.rpartition(":")
        if (not sep or not colon or repo != self.repository or not DIGEST.fullmatch(digest)
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)
                or not re.fullmatch(self.tags, tag, re.ASCII)):
            raise ReleaseError("The release image requires an approved tag and SHA-256 digest")
        return image


def targets(repo):
    entries = tomllib.loads((repo / CATALOG).read_text())["target"]
    result = [Target(**entry) for entry in entries]
    if len({item.name for item in result}) != len(result):
        raise ReleaseError("Duplicate release target")
    return result


def load(path):
    return json.loads(path.read_text())


def save(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        try:
            stream.write(json.dumps(data, indent=2).encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(stream.name, path)
        finally:
            Path(stream.name).unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def locked(root):
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def record(state, target, outcome):
    attempt = state["pending"]
    candidate = target.require_image(attempt["candidate"])
    digest = candidate.split("@", 1)[1]
    fingerprint = hashlib.sha256(f"{target.name}:{digest}".encode()).hexdigest()
    entry = state["outbox"].get(fingerprint, {})
    state["outbox"][fingerprint] = {
        **attempt, "previous": state["good"], "outcome": outcome,
        "time": entry.get("time") or attempt.get("time")
        or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    state["failed"][digest] = True
    if outcome != "recovery blocked":
        state["pending"] = None
    return outcome


def recover(path, state, host):
    try:
        if not host.idle():
            raise ReleaseError("Komodo is still executing")
        host.restore(state["good"])
    except Exception:
        result = record(state, host.policy, "recovery blocked")
    else:
        result = record(state, host.policy, "restored")
    save(path, state)
    return result


def resume(path, host):
    state = load(path)
    attempt = state["pending"]
    if not attempt:
        return "unchanged"
    attempt.setdefault("detail", "interrupted")
    if attempt["phase"] == "preflight":
        # Preflight cannot recreate a container.
        result = record(state, host.policy, "unchanged")
        save(path, state)
        return result
    return recover(path, state, host)


def apply(path, host, revision, candidate):
    host.policy.require_image(candidate)
    if not REVISION.fullmatch(revision):
        raise ReleaseError("An exact Git commit is required")
    state = load(path)
    if state["pending"]:
        raise ReleaseError("A release is still pending recovery")
    if state["good"] == candidate:
        return "unchanged"
    if candidate.split("@", 1)[1] in state["failed"]:
        return "quarantined"

    attempt = {"revision": revision, "candidate": candidate, "phase": "preflight",
               "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    state["pending"] = attempt
    save(path, state)
    try:
        host.prepare(state["good"], candidate)
    except Exception as error:
        attempt["detail"] = failure_detail(error, attempt["phase"])
        result = record(state, host.policy, "unchanged")
        save(path, state)
        return result
    attempt["phase"] = "deploy"
    save(path, state)
    try:
        host.deploy(candidate)
        attempt["phase"] = "probe"
        save(path, state)
        host.verify(candidate)
    except Exception as error:
        attempt["detail"] = failure_detail(error, attempt["phase"])
        save(path, state)
        return recover(path, state, host)
    state["good"] = candidate
    state["pending"] = None
    save(path, state)
    return "promoted"


def issue(target, fingerprint, failure):
    revision = failure["revision"]
    phase, outcome = failure["phase"], failure["outcome"]
    if (not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not REVISION.fullmatch(revision)
            or phase not in PHASES or outcome not in RESULTS):
        raise ReleaseError("Invalid incident metadata")
    when = dt.datetime.fromisoformat(failure["time"]).isoformat()
    detail = failure.get("detail", "interrupted")
    if detail not in DETAILS:
        raise ReleaseError("Invalid incident detail")
    previous = target.require_image(failure["previous"])
    candidate = target.require_image(failure["candidate"])
    return {
        "title": f"Compose release {target.name} {phase} failed ({fingerprint[:12]})",
        "body": (
            f"<!-- technis-release:{fingerprint} -->\n\n"
            f"- Stack/service: {target.name}\n- Revision: `{revision}`\n"
            f"- Attempted image: `{candidate}`\n- Last healthy image: `{previous}`\n"
            f"- Failed phase: {phase}\n- Failed check: {detail}\n"
            f"- Recovery: {outcome}\n- First observed: {when}\n"
        ),
    }


def report(path, target, api, repository):
    state = load(path)
    if not state["outbox"]:
        return 0
    if not REPOSITORY.fullmatch(repository):
        raise ReleaseError("An issue repository is required")
    endpoint = f"/repos/{repository}"
    info = api("GET", endpoint)
    if info.get("private") is not True or info.get("has_issues") is not True:
        raise ReleaseError("Incident reporting requires a private repository with Issues enabled")
    count = 0
    for fingerprint, incident in list(state["outbox"].items()):
        payload = issue(target, fingerprint, incident)
        match = None
        page = 1
        while True:
            issues = api("GET", f"{endpoint}/issues?state=all&per_page=100&page={page}")
            marker = f"<!-- technis-release:{fingerprint} -->"
            match = next((item for item in issues if not item.get("pull_request")
                          and (item.get("body") or "").startswith(marker)), None)
            if match or len(issues) < 100:
                break
            page += 1
        if not match:
            api("POST", f"{endpoint}/issues", payload)
        elif match["body"] != payload["body"]:
            api("PATCH", f"{endpoint}/issues/{int(match['number'])}", payload)
        state["outbox"].pop(fingerprint)
        save(path, state)
        count += 1
    return count


def command(*args, cwd=None, env=None, timeout=480):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ReleaseError(f"{Path(args[0]).name} command failed")
    return result.stdout.decode()


def copy_sqlite(source, destination):
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot timed out")

    with (contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
          contextlib.closing(sqlite3.connect(destination)) as replica):
        live.backup(replica, pages=256, progress=progress)
        replica.execute("PRAGMA journal_mode=DELETE")


def copy_sqlite_as_service(source, destination, uid, gid):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
            os.umask(0o077)
            copy_sqlite(source, destination)
        except BaseException:
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(child, 0)
    if status != 0:
        raise ReleaseError("Could not copy the SQLite database", "sqlite-compatibility")


def sqlite_signature(path):
    if not regular_sqlite_files(path):
        raise ReleaseError("SQLite database files require owner review", "volume")
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.execute("PRAGMA trusted_schema=OFF")
        if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ReleaseError("SQLite integrity check failed", "sqlite-compatibility")
        metadata = [database.execute(f"PRAGMA {key}").fetchone() for key in ("application_id", "user_version", "encoding")]
        schema = database.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name").fetchall()
        signature = hashlib.sha256(repr((metadata, schema)).encode())
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            table = '"' + name.replace('"', '""') + '"'
            signature.update(repr((name,)).encode())
            for row in database.execute(f"SELECT * FROM {table}"):
                signature.update(b"\0")
                signature.update(repr(row).encode())
        return signature.digest()


def regular_sqlite_files(path):
    files = [path.with_name(path.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    return path.is_file() and all(not item.is_symlink() and (not item.exists() or item.is_file()) for item in files)


def git(repo, *args):
    return command("git", "-C", str(repo), *args)


def service_stanza(target, contents):
    if not target.source_service:
        return contents
    stanza = re.search(rf"(?ms)^  {re.escape(target.service)}:[ \t]*\n(.*?)(?=^ {{0,2}}\S|\Z)", contents)
    return stanza[1] if stanza else ""


def pin_line(target, contents):
    contents = service_stanza(target, contents)
    matches = re.findall(r"^    image: ([^\s]+)$", contents, re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseError("Expected one literal release image")
    return target.require_image(matches[0])


def manifest(target, image):
    image = target.require_image(image)
    name = image.split("@", 1)[0]
    index = json.loads(command("docker", "buildx", "imagetools", "inspect", "--raw", image))
    platform = next((item for item in index.get("manifests", [])
                     if item.get("platform", {}).get("os") == "linux"
                     and item["platform"].get("architecture") == target.architecture), None)
    if platform is None:
        raise ReleaseError(f"Cannot find linux/{target.architecture} in the pinned image index")
    if not DIGEST.fullmatch(platform["digest"]):
        raise ReleaseError("Invalid registry manifest")
    platform_image = f"{name}@{platform['digest']}"
    config = json.loads(command("docker", "buildx", "imagetools", "inspect", "--raw",
                                platform_image))["config"]["digest"]
    if not DIGEST.fullmatch(config):
        raise ReleaseError("Invalid registry image configuration digest")
    if target.image_user or target.sqlite_db:
        metadata = json.loads(command("docker", "buildx", "imagetools", "inspect",
                                      "--format", "{{json .Image}}", platform_image))["config"]
        if metadata.get("Volumes") or (target.image_user and metadata.get("User") != target.image_user):
            raise ReleaseError("Image user or volume defaults changed")
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def request(url, method="GET", data=None, headers=None, timeout=30):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, payload, {"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    with client.open(req, timeout=timeout) as response:
        return json.load(response)


class Runtime:
    def __init__(self, root, config, headers, policy, inventory):
        self.root = root
        self.config = config
        self.headers = headers
        self.policy = policy
        self.inventory = inventory
        self.docker = ["docker", "--host", "unix:///var/run/docker.sock"]

    def state_path(self, target):
        return self.root / "services" / target.service / "state.json"

    def docker_call(self, *args):
        return command(*self.docker, *args)

    def template(self, target):
        return load(self.state_path(target).with_name("template.json"))

    def komodo(self, kind, name, **params):
        return request(f"{self.config['komodo_url'].rstrip('/')}/{kind}", "POST",
                       {"type": name, "params": params}, self.headers)

    def approved_stack(self):
        stack = self.komodo("read", "GetStack", stack=self.policy.host)
        config = stack["config"]
        if any(config.get(key) != value for key, value in {
                "server_id": self.config["server_id"], "files_on_host": True, "project_name": self.policy.host,
                "run_directory": str(self.root / "stack"), "file_paths": ["compose.json"],
                "extra_args": STACK_ARGS, "auto_pull": False, "webhook_enabled": False,
                "auto_update": False, "auto_update_all_services": False, "run_build": False,
                "destroy_before_deploy": False,
        }.items()) or any(config.get(key) for key in (
            "environment", "additional_env_files", "compose_cmd_wrapper", "file_contents", "swarm_id",
        )) or any(config.get(hook, {}).get("command") for hook in ("pre_deploy", "post_deploy")):
            raise ReleaseError("Komodo Stack settings differ from the approved policy", "executor")
        return stack["_id"]["$oid"]

    def idle(self):
        stack_id = self.approved_stack()
        state = self.komodo("read", "GetStackActionState", stack=self.policy.host)
        updates = self.komodo("read", "ListUpdates", query={"target": {"type": "Stack", "id": stack_id}})
        return all(state.get(key) is False for key in ("pulling", "deploying", "starting",
                   "restarting", "pausing", "unpausing", "stopping", "destroying")) and not any(
            item["status"] == "InProgress" for item in updates["updates"]
        )

    def wait_update(self, update):
        deadline = time.monotonic() + 420
        while update["status"] == "InProgress":
            if time.monotonic() >= deadline:
                raise ReleaseError("Komodo deployment did not finish", "executor")
            time.sleep(2)
            update = self.komodo("read", "GetUpdate", id=update["_id"]["$oid"])
        if update["status"] != "Complete" or update["success"] is not True:
            raise ReleaseError("Komodo deployment failed", "executor")

    def image_id(self, image):
        image = self.policy.require_image(image)
        if not (self.policy.image_user or self.policy.sqlite_db):
            return self.docker_call("image", "inspect", "-f", "{{.Id}}", image).strip()
        output = self.docker_call("image", "inspect", "-f",
                                  '{"id":{{json .Id}},"user":{{json .Config.User}},'
                                  '"volumes":{{json .Config.Volumes}}}', image)
        metadata = json.loads(output)
        if metadata["volumes"] or (self.policy.image_user and metadata["user"] != self.policy.image_user):
            raise ReleaseError("Image user or volume defaults changed", "registry")
        return metadata["id"]

    def inspect(self, allow_missing=False):
        # Constrain Docker's response to the properties needed for the safety check.
        template = ('{"image":{{json .Image}},"health":{{json .State.Health.Status}},'
                    '"configured":{{json .Config.Image}},"restarts":{{json .RestartCount}},"mounts":{{json .Mounts}},'
                    '"read_only":{{json .HostConfig.ReadonlyRootfs}},"user":{{json .Config.User}},'
                    '"networks":{{json .NetworkSettings.Networks}},'
                    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
        ids = self.docker_call("ps", "-aq",
                               "--filter", f"label=com.docker.compose.project={self.policy.host}",
                               "--filter", f"label=com.docker.compose.service={self.policy.service}",
                               "--filter", "label=com.docker.compose.oneoff=False").splitlines()
        if not ids and allow_missing:
            return None
        if len(ids) != 1:
            raise ReleaseError("Expected one Compose service container")
        return json.loads(self.docker_call("inspect", "-f", template, ids[0]))

    def target(self, allow_missing=False):
        status = self.inspect(allow_missing)
        if status is None:
            return None, self.volume(self.policy)
        if (status["project"], status["service"]) != (self.policy.host, self.policy.service):
            raise ReleaseError("Unexpected Compose ownership")
        if set(status["networks"]) != {f"{self.policy.host}_default"}:
            raise ReleaseError("Unexpected Compose network")
        mounts = status["mounts"]
        if self.policy.sqlite_db:
            if (len(mounts) != 1 or mounts[0]["Type"] != "bind" or not mounts[0]["RW"]
                    or mounts[0]["Destination"] != self.policy.sqlite_directory):
                raise ReleaseError("Unexpected SQLite bind mount", "volume")
            return status, mounts[0]["Source"]
        if self.policy.empty_mount:
            if len(mounts) != 1 or mounts[0]["Type"] != "volume" or mounts[0]["Destination"] != self.policy.empty_mount:
                raise ReleaseError("Unexpected release volume", "volume")
        elif mounts:
            raise ReleaseError("Unexpected persistent storage", "volume")
        return status, mounts[0]["Name"] if mounts else None

    def volume(self, target, service=None):
        if target.sqlite_db:
            service = self.template(target) if service is None else service
            return service["volumes"][0]["source"]
        return self.config.get("volumes", {}).get(target.service)

    def empty_volume(self):
        if not self.policy.empty_mount:
            return
        volume = self.volume(self.policy)
        if not volume or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", volume):
            raise ReleaseError("Invalid release volume")
        mount = Path(self.docker_call("volume", "inspect", "-f", "{{.Mountpoint}}", volume).strip())
        if not mount.is_absolute() or not mount.is_dir() or any(mount.iterdir()):
            raise ReleaseError("A nonempty release volume requires owner review", "volume")

    def dependencies_ready(self):
        for name in self.policy.depends_on:
            ids = self.docker_call(
                "ps", "-q", "--filter", f"label=com.docker.compose.project={self.policy.host}",
                "--filter", f"label=com.docker.compose.service={name}",
                "--filter", "label=com.docker.compose.oneoff=False",
            ).splitlines()
            if len(ids) != 1 or self.docker_call(
                    "inspect", "-f", "{{.State.Health.Status}}", ids[0]).strip() != "healthy":
                raise ReleaseError("A release dependency is unhealthy", "client-probe")

    def check_sqlite(self, image):
        try:
            _, mount = self.target(allow_missing=True)
            if mount != self.volume(self.policy):
                raise ReleaseError("Unexpected SQLite bind source", "volume")
            directory = Path(mount)
            live = directory / Path(self.policy.sqlite_db).name
            if directory.resolve(strict=True) != directory or not regular_sqlite_files(live):
                raise ReleaseError("SQLite database files require owner review", "volume")
            config = directory / "server.toml"
            if config.is_symlink() or (config.exists() and not config.is_file()):
                raise ReleaseError("The SQLite server configuration requires owner review", "volume")
            config_text = config.read_text() if config.exists() else None
            if config_text is not None and tomllib.loads(config_text):
                raise ReleaseError("The SQLite server configuration overrides Compose settings", "volume")
            service = self.template(self.policy)
            uid, gid = map(int, service["user"].split(":"))
            with tempfile.TemporaryDirectory(prefix="technis-sqlite-", dir="/run") as work:
                scratch = Path(work)
                os.chown(scratch, 0, gid)
                scratch.chmod(0o710)
                data = scratch / "data"
                data.mkdir(mode=0o700)
                os.chown(data, uid, gid)
                replica = data / live.name
                # A WAL reader can create live sidecars. Use Atuin's UID so they stay usable.
                copy_sqlite_as_service(live, replica, uid, gid)
                expected = sqlite_signature(replica)
                if config_text is not None:
                    replica_config = data / config.name
                    replica_config.write_text(config_text)
                    os.chown(replica_config, uid, gid)
                for key in ("container_name", "hostname", "labels", "networks", "expose"):
                    service.pop(key, None)
                service.update(image=self.policy.require_image(image), network_mode="none", restart="no",
                               logging={"driver": "none"})
                service["volumes"][0]["source"] = str(data)
                spec = scratch / "compose.json"
                save(spec, {"name": f"technis-sqlite-{uuid.uuid4().hex}", "services": {"check": service}})
                try:
                    self.docker_call("compose", "-f", str(spec), "up", "-d", "--wait", "--wait-timeout", "90",
                                     "--pull", "never", "--no-build")
                    self.docker_call("compose", "-f", str(spec), "stop", "-t", "10")
                finally:
                    self.docker_call("compose", "-f", str(spec), "down", "-t", "10")
                if sqlite_signature(replica) != expected:
                    raise ReleaseError("Image startup changed the SQLite database", "sqlite-compatibility")
        except (ReleaseError, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
            detail = error.detail if isinstance(error, ReleaseError) and error.detail else "sqlite-compatibility"
            raise ReleaseError("SQLite compatibility check failed", detail) from None

    def stage(self, image):
        services = {}
        volumes = {}
        for target in self.inventory.values():
            path = self.state_path(target)
            if target == self.policy:
                pin = image
            elif path.exists():
                pin = load(path)["good"]
            else:
                continue
            service = {**self.template(target), "image": target.require_image(pin)}
            service["labels"] = {**service.get("labels", {}), MANAGED_LABEL: "true"}
            # The executor uses --no-deps. Check the live dependency instead of recreating it.
            service.pop("depends_on", None)
            if target.empty_mount:
                source = f"release_{target.service}"
                service["volumes"] = [{"type": "volume", "source": source, "target": target.empty_mount,
                                      "read_only": True, "volume": {"nocopy": True}}]
                volumes[source] = {"name": self.volume(target), "external": True}
            services[target.service] = service
        save(self.root / "stack" / "compose.json", {"name": self.policy.host, "services": services,
             "networks": {"default": {"name": f"{self.policy.host}_default", "external": True}},
             "volumes": volumes})

    def prepare(self, previous, candidate):
        if not self.idle():
            raise ReleaseError("Komodo is busy")
        self.verify(previous, observe=False)
        self.empty_volume()
        try:
            self.docker_call("pull", "--platform", "linux/" + self.policy.architecture, candidate)
            self.image_id(candidate)
        except (ReleaseError, OSError, subprocess.TimeoutExpired):
            raise ReleaseError("Could not cache the candidate image", "registry") from None
        self.dependencies_ready()
        if self.policy.sqlite_db:
            self.check_sqlite(candidate)

    def deploy(self, image):
        self.approved_stack()
        self.empty_volume()
        if self.policy.sqlite_db:
            _, mount = self.target(allow_missing=True)
            if mount != self.volume(self.policy):
                raise ReleaseError("Unexpected SQLite bind source", "volume")
        self.image_id(image)
        self.stage(image)
        update = self.komodo("execute", "DeployStack", stack=self.policy.host, services=[self.policy.service])
        self.wait_update(update)

    def restore(self, image):
        try:
            status, _ = self.target()
            if status["configured"] == image:
                self.verify(image)
                self.stage(image)
                return
        except (ReleaseError, OSError, ValueError):
            pass
        if self.policy.sqlite_db:
            # Rehearse the previous image on a copy of the latest database. Keep the live bind and its writes.
            self.check_sqlite(image)
        self.deploy(image)
        self.verify(image)

    def verify(self, image, observe=True, service=None):
        expected = self.image_id(image)
        deadline = time.monotonic() + (self.config.get("observe_seconds", 300) if observe else 0)
        first = True
        while True:
            status, volume = self.target()
            if status["image"] != expected:
                raise ReleaseError("The service is running a different image", "running-image")
            if (volume != self.volume(self.policy, service)
                    or (observe and self.policy.empty_mount and status["mounts"][0]["RW"])):
                raise ReleaseError("Unexpected release volume state", "volume")
            if status["health"] != "healthy" or status["restarts"]:
                raise ReleaseError("Container failed release verification", "container-health")
            if self.policy.read_only and not status["read_only"]:
                raise ReleaseError("The release requires a read-only container filesystem", "container-health")
            if self.policy.image_user and status["user"] != self.policy.image_user:
                raise ReleaseError("The container user differs from its approved image user", "container-health")
            try:
                self.dependencies_ready()
                self.probe(self.policy.probe["health"])
                if first:
                    self.smoke()
                    first = False
            except (ReleaseError, OSError, ValueError, subprocess.TimeoutExpired):
                raise ReleaseError("Client route failed", "client-probe") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(15, remaining))


    def smoke(self):
        identifier = uuid.uuid4().hex
        for step in self.policy.probe.get("startup", []):
            data = {key: (identifier if value == "{id}" else value) for key, value in step["data"].items()}
            response = self.probe(step["path"], data)
            if any(response.get(key) != (identifier if value == "{id}" else value)
                   for key, value in step["expect"].items()):
                raise ReleaseError("Service smoke check failed")

    def probe(self, route, data=None):
        probe = self.policy.probe
        if not route.startswith("/") or not NAME.fullmatch(probe["container"]):
            raise ReleaseError("Invalid service probe")
        args = ["exec", probe["container"], "curl", "-fsS", "--max-time", "30"]
        if data is not None:
            args += ["-H", "Content-Type: application/json", "--data", json.dumps(data)]
        output = self.docker_call(*args, f"http://{self.policy.service}:{int(probe['port'])}{route}")
        if "contains" in probe and probe["contains"] not in output.splitlines():
            raise ReleaseError("The probe response did not contain the expected line", "client-probe")
        return json.loads(output) if data is not None else None


def source_signature(repo, revision, target):
    source = git(repo, "show", f"{revision}:{target.source}")
    image = pin_line(target, source)
    source = service_stanza(target, source)
    input_name = target.source.parent.name
    override_dir = target.stack.parent / "services" / input_name
    env = []
    keys = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", source))
    env_paths = [target.source.with_name(".env"), Path("docker/.env"), target.stack.with_name(".env")]
    if target.source_service:
        env_paths.append(override_dir / ".env")
    for path in env_paths:
        if git(repo, "ls-tree", revision, str(path)):
            contents = git(repo, "show", f"{revision}:{path}")
            env.extend(line for line in contents.splitlines()
                       if line.split("=", 1)[0].strip().removeprefix("export ") in keys)
    if target.source_service:
        override_file = override_dir / "compose.yaml"
        override = service_stanza(target, git(repo, "show", f"{revision}:{override_file}"))
    else:
        override = git(repo, "ls-tree", "-r", revision, str(override_dir))
    stack = git(repo, "show", f"{revision}:{target.stack}")
    relative = target.source.relative_to("docker")
    path = f"  - path: ../../{relative}\n"
    if target.source_service:
        path = f"  - path:\n      - ../../{relative}\n      - services/{input_name}/compose.yaml\n"
    required = path + f"    env_file: ${{TECHNIS_ENV:?run compose through docker/scripts/stacks}}/{input_name}.env\n"
    if required not in stack or f"\nname: {target.host}\n" not in stack:
        raise ReleaseError("Managed service wiring changed")
    contents = [source.replace(image, "RELEASE_IMAGE", 1), *env, override]
    return hashlib.sha256("\0".join(contents).encode()).hexdigest(), image


def effective_service(repo, target):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    model = json.loads(command("bash", "docker/scripts/stacks", "-s", target.host, "render", cwd=repo, env=env))
    service = model["services"].get(target.service)
    configured_user = (service or {}).get("user")
    user = str(configured_user or target.image_user or "").split(":", 1)[0]
    dependencies = {name: {"condition": "service_healthy", "required": True} for name in target.depends_on}
    extra = {"expose", "init", "volumes"} if target.sqlite_db else set()
    expected_command = ["start"] if target.sqlite_db else None
    if (model["name"] != target.host or model["networks"]["default"]["name"] != f"{target.host}_default"
            or service is None or set(service["networks"]) != {"default"}
            or set(service) - {"cap_drop", "command", "container_name", "deploy", "entrypoint",
                               "environment", "healthcheck", "hostname", "image", "networks",
                               "restart", "security_opt", "tmpfs", "user", "labels", "read_only", "depends_on"} - extra
            or service.get("cap_drop") != ["ALL"]
            or (not re.fullmatch(r"[1-9][0-9]*|[a-z_][a-z0-9_-]*", user) or user == "root")
            or (target.image_user and configured_user is not None)
            or service.get("command") != expected_command or service.get("entrypoint") is not None
            or service.get("security_opt") not in (["no-new-privileges:true"], ["no-new-privileges=true"])
            or not service.get("healthcheck") or service.get("environment", {}) != target.environment
            or service.get("labels", {}) != target.labels or service.get("read_only", False) != target.read_only
            or service.get("depends_on", {}) != dependencies
            or service["image"] != pin_line(target, (repo / target.source).read_text())):
        raise ReleaseError("The effective service requires owner review")
    if target.sqlite_db:
        mounts = service.get("volumes", [])
        if (not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", configured_user or "")
                or service["environment"].get("ATUIN_DB_URI") != "sqlite://" + target.sqlite_db
                or service["environment"].get("ATUIN_CONFIG_DIR") != target.sqlite_directory
                or service["environment"].get("ATUIN_OPEN_REGISTRATION") != "false"
                or len(mounts) != 1 or mounts[0].get("type") != "bind" or mounts[0].get("read_only")
                or mounts[0].get("target") != target.sqlite_directory
                or not Path(mounts[0].get("source", "")).is_absolute()
                or mounts[0].get("bind", {}) not in ({}, {"create_host_path": False})):
            raise ReleaseError("The effective SQLite bind requires owner review")
        # Compose 2.40 omits false on render. Make the staging behavior explicit.
        mounts[0]["bind"] = {"create_host_path": False}
    return service


def check(repo):
    base = os.environ.get("RELEASE_BASE", "")
    for target in targets(repo):
        service = effective_service(repo, target)
        if REVISION.fullmatch(base) and git(repo, "ls-tree", base, str(CATALOG)):
            catalog = tomllib.loads(git(repo, "show", f"{base}:{CATALOG}"))
            original = [Target(**entry) for entry in catalog["target"]]
            if target in original:
                old_signature, _ = source_signature(repo, base, target)
                signature, _ = source_signature(repo, "HEAD", target)
                if signature != old_signature:
                    raise ReleaseError("Automatic releases accept only image changes")
        manifest(target, service["image"])
        print(f"{target.name} Compose policy and linux/{target.architecture} digest verified")


def github(method, path, data=None, token=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "technis-release"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return request("https://api.github.com" + path, method, data, headers)


def checked_commit(repository, revision):
    if not REPOSITORY.fullmatch(repository) or not REVISION.fullmatch(revision):
        raise ReleaseError("Invalid GitHub release source")
    runs = github("GET", f"/repos/{repository}/commits/{revision}/check-runs?per_page=100")
    passed = {run["name"] for run in runs["check_runs"]
              if run["app"]["slug"] == "github-actions" and run["head_sha"] == revision
              and run["conclusion"] == "success"}
    return {"Compose Policy", "Gitleaks"} <= passed


def vault(repo, ref):
    if not re.fullmatch(r"vault://technis/[A-Za-z0-9_./-]+", ref):
        raise ReleaseError("Expected an Infisical vault reference")
    env = os.environ.copy()
    if directory := env.get("CREDENTIALS_DIRECTORY"):
        env["INFISICAL_CLIENT_ID"] = (Path(directory) / "infisical_id").read_text().strip()
        env["INFISICAL_CLIENT_SECRET"] = (Path(directory) / "infisical_secret").read_text().strip()
    return command("python3", str(repo / "bin/vault"), "get", ref.removeprefix("vault://technis/"), env=env).strip()


def incident_token(repo, config):
    # Sign on a pipe so the GitHub App key is never written to disk or passed as an argument.
    import base64
    import threading

    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    app_id = int(config["incident_app_id"])
    installation = int(config["incident_installation"])
    now = int(time.time())
    message = b64(b'{"alg":"RS256","typ":"JWT"}') + b"." + b64(json.dumps({
        "iat": now - 60, "exp": now + 540, "iss": str(app_id),
    }).encode())
    key = vault(repo, config["incident_key"]).encode() + b"\n"
    read_fd, write_fd = os.pipe()

    def write_key():
        try:
            with os.fdopen(write_fd, "wb") as stream:
                stream.write(key)
        except BrokenPipeError:
            pass

    writer = threading.Thread(target=write_key)
    writer.start()
    try:
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"], input=message,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(read_fd,), timeout=10,
        )
    finally:
        os.close(read_fd)
        writer.join()
    if signature.returncode:
        raise ReleaseError("Could not sign GitHub App authentication")
    jwt = (message + b"." + b64(signature.stdout)).decode()
    repo_name = config["incident_repository"].split("/", 1)[1]
    token = github("POST", f"/app/installations/{installation}/access_tokens",
                   {"repositories": [repo_name], "permissions": {"issues": "write"}}, jwt)
    return token["token"]


def read_headers(repo, config):
    return {"X-Api-Key": vault(repo, config["komodo_key"]),
            "X-Api-Secret": vault(repo, config["komodo_secret"])}


def validate_settings(config):
    if not NAME.fullmatch(config["host"]) or not re.fullmatch(r"[a-f0-9]{24}", config["server_id"]):
        raise ReleaseError("The local Komodo Server identity is required")
    if config.get("observe_seconds", 300) < 300:
        raise ReleaseError("A five-minute production observation window is required")
    url = urllib.parse.urlsplit(config["komodo_url"])
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/")):
        raise ReleaseError("Invalid private Komodo endpoint")
    if url.scheme != "https" and (url.scheme, url.hostname) != ("http", "127.0.0.1"):
        raise ReleaseError("Use TLS for Komodo outside loopback")


def enroll(repo, host):
    target = host.policy
    state_path = host.state_path(target)
    if any(host.state_path(item).exists() and load(host.state_path(item))["pending"]
           for item in host.inventory.values()):
        raise ReleaseError("Recovery must finish before enrolling another service")
    revision = git(repo, "rev-parse", "HEAD").strip()
    signature, image = source_signature(repo, revision, target)
    service = effective_service(repo, target)
    if service["image"] != image or git(repo, "status", "--porcelain", "--", "docker"):
        raise ReleaseError("Enroll from a clean, reviewed Compose checkout")
    host.approved_stack()
    status, volume = host.target()
    if host.volume(target, service) != volume:
        raise ReleaseError("Configure the exact existing release volume")
    host.docker_call("pull", "--platform", "linux/" + target.architecture, image)
    if status["image"] != host.image_id(image):
        raise ReleaseError("The pinned baseline differs from the live image")
    host.verify(image, observe=False, service=service)
    host.empty_volume()
    state = load(state_path) if state_path.exists() else {"failed": {}, "outbox": {}}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save(state_path.with_name("template.json"), service)
    host.stage(image)
    save(state_path, {**state, "good": image, "pending": None,
                      "signature": signature, "revision": revision})


def resolve_settings(repo, config, *keys):
    values = {}
    for key in keys:
        value = config[key]
        values[key] = vault(repo, value) if isinstance(value, str) and value.startswith("vault://") else value
    return {**config, **values}


def incident_api(repo, config):
    config = resolve_settings(repo, config, "incident_repository", "incident_app_id", "incident_installation")
    repository = config["incident_repository"]
    if not REPOSITORY.fullmatch(repository):
        raise ReleaseError("An incident repository is required")
    token = incident_token(repo, config)

    def api(method, url, data=None):
        return github(method, url, data, token)

    incident_repo = api("GET", f"/repos/{repository}")
    if incident_repo.get("private") is not True or incident_repo.get("has_issues") is not True:
        raise ReleaseError("A private incident repository with Issues is required")
    return api, repository


def poll(repo, config, hosts):
    reporter = None
    blocked = False
    active = []
    for host in hosts:
        target = host.policy
        path = host.state_path(target)
        if not path.exists():
            continue
        active.append((host, path))
        if load(path)["pending"]:
            outcome = resume(path, host)
            print(f"{target.name} {outcome}")
            blocked |= outcome == "recovery blocked"
    for host, path in active:
        if load(path)["outbox"]:
            reporter = reporter or incident_api(repo, config)
            report(path, host.policy, *reporter)
    if blocked:
        raise ReleaseError("Waiting for recovery before promoting another release on this host")
    if not active or not config.get("promote", True):
        return
    upstream = config["repository"]
    if not REPOSITORY.fullmatch(upstream):
        raise ReleaseError("A release repository is required")
    git(repo, "fetch", "--no-tags", f"https://github.com/{upstream}.git", "refs/heads/main")
    revision = git(repo, "rev-parse", "FETCH_HEAD").strip()
    if all(load(path)["revision"] == revision for _, path in active):
        return
    if not REVISION.fullmatch(revision) or not checked_commit(upstream, revision):
        raise ReleaseError("Waiting for Compose Policy and Gitleaks to pass on the exact main commit")
    for host, path in active:
        target = host.policy
        state = load(path)
        if state["revision"] == revision:
            continue
        if not git_is_ancestor(repo, state["revision"], revision):
            raise ReleaseError("Main has diverged from the enrolled revision")
        signature, image = source_signature(repo, revision, target)
        if signature != state["signature"]:
            raise ReleaseError(f"{target.name} configuration changed; repeat owner enrollment")
        if state["good"] != image and image.split("@", 1)[1] not in state["failed"]:
            reporter = reporter or incident_api(repo, config)
        outcome = apply(path, host, revision, image)
        print(f"{target.name} {outcome}")
        if outcome in {"promoted", "unchanged", "quarantined"}:
            state = load(path)
            state["revision"] = revision
            save(path, state)
        if reporter:
            report(path, target, *reporter)
        if outcome == "recovery blocked":
            raise ReleaseError("Waiting for recovery before promoting another release on this host")


def git_is_ancestor(repo, old, new):
    try:
        git(repo, "merge-base", "--is-ancestor", old, new)
        return True
    except ReleaseError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "enroll", "poll"))
    parser.add_argument("service", nargs="?")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--state", type=Path, default=Path("/var/lib/technis/releases"))
    args = parser.parse_args()
    os.umask(0o077)
    repo = args.repository.resolve()
    if args.action == "check":
        check(repo)
        return
    root = args.state.resolve()
    config = resolve_settings(repo, load(root / "settings.json"), "server_id", "komodo_url")
    validate_settings(config)
    inventory = {target.service: target for target in targets(repo) if target.host == config["host"]}
    if not inventory:
        raise ReleaseError("The definition has no release targets for this host")
    headers = read_headers(repo, config)
    with locked(root):
        if args.action == "enroll":
            if args.service not in inventory:
                raise ReleaseError("Specify an approved service to enroll")
            enroll(repo, Runtime(root, config, headers, inventory[args.service], inventory))
            print(f"{inventory[args.service].name} enrolled")
        else:
            poll(repo, config, [Runtime(root, config, headers, target, inventory)
                                for target in inventory.values()])


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        raise SystemExit(str(error)) from None
    except (OSError, KeyError, ValueError, subprocess.TimeoutExpired):
        raise SystemExit("Release operation unavailable. Check host state and credential provisioning.") from None
