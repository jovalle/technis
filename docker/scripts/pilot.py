#!/usr/bin/env python3
"""Digest-only Komodo releases for the Nexus FlareSolverr pilot."""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

SERVICE = "flaresolverr"
IMAGE = re.compile(r"ghcr\.io/flaresolverr/flaresolverr:v3\.\d+\.\d+@sha256:[0-9a-f]{64}", re.ASCII)
REVISION = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
PHASES = {"preflight", "deploy", "probe"}
RESULTS = {"unchanged", "restored", "recovery blocked"}
SOURCE = Path("docker/services/flaresolverr/compose.yaml")
ENV = SOURCE.with_name(".env")
STACK = Path("docker/stacks/nexus/compose.yaml")
MANAGED_LABEL = "io.technis.pilot.managed"
STACK_ARGS = ["--wait", "--wait-timeout", "300", "--no-build", "--pull", "never", "--no-deps"]
DETAILS = {"baseline", "registry", "executor", "running-image", "container-health", "client-probe",
           "volume", "interrupted"}


class PilotError(Exception):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


def failure_detail(error, phase):
    detail = getattr(error, "detail", None)
    if detail in DETAILS:
        return detail
    return {"preflight": "baseline", "deploy": "executor", "probe": "client-probe"}[phase]


def require_image(image):
    if not IMAGE.fullmatch(image):
        raise PilotError("FlareSolverr requires an approved v3 tag and SHA-256 digest")
    return image


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


def record(state, outcome):
    attempt = state["pending"]
    candidate = require_image(attempt["candidate"])
    digest = candidate.split("@", 1)[1]
    fingerprint = hashlib.sha256(digest.encode()).hexdigest()
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
            raise PilotError("Komodo is still executing")
        if hasattr(host, "restore"):
            host.restore(state["good"])
        else:
            host.deploy(state["good"])
            host.verify(state["good"])
    except Exception:
        result = record(state, "recovery blocked")
    else:
        result = record(state, "restored")
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
        result = record(state, "unchanged")
        save(path, state)
        return result
    return recover(path, state, host)


def apply(path, host, revision, candidate):
    require_image(candidate)
    if not REVISION.fullmatch(revision):
        raise PilotError("An exact Git commit is required")
    state = load(path)
    if state["pending"]:
        raise PilotError("A release is still pending recovery")
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
        result = record(state, "unchanged")
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


def issue(fingerprint, failure):
    revision = failure["revision"]
    phase, outcome = failure["phase"], failure["outcome"]
    if (not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not REVISION.fullmatch(revision)
            or phase not in PHASES or outcome not in RESULTS):
        raise PilotError("Invalid incident metadata")
    when = dt.datetime.fromisoformat(failure["time"]).isoformat()
    detail = failure.get("detail", "interrupted")
    if detail not in DETAILS:
        raise PilotError("Invalid incident detail")
    previous = require_image(failure["previous"])
    candidate = require_image(failure["candidate"])
    return {
        "title": f"FlareSolverr pilot {phase} failed ({fingerprint[:12]})",
        "body": (
            f"<!-- technis-pilot:{fingerprint} -->\n\n"
            f"- Stack/service: Nexus / FlareSolverr\n- Revision: `{revision}`\n"
            f"- Attempted image: `{candidate}`\n- Last healthy image: `{previous}`\n"
            f"- Failed phase: {phase}\n- Failed check: {detail}\n"
            f"- Recovery: {outcome}\n- First observed: {when}\n"
        ),
    }


def report(path, api, repository):
    state = load(path)
    if not state["outbox"]:
        return 0
    if not REPOSITORY.fullmatch(repository):
        raise PilotError("An issue repository is required")
    endpoint = f"/repos/{repository}"
    info = api("GET", endpoint)
    if info.get("private") is not True or info.get("has_issues") is not True:
        raise PilotError("Incident reporting requires a private repository with Issues enabled")
    count = 0
    for fingerprint, incident in list(state["outbox"].items()):
        payload = issue(fingerprint, incident)
        match = None
        page = 1
        while True:
            issues = api("GET", f"{endpoint}/issues?state=all&per_page=100&page={page}")
            marker = f"<!-- technis-pilot:{fingerprint} -->"
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
        raise PilotError(f"{Path(args[0]).name} command failed")
    return result.stdout.decode()


def compose_service(repo):
    # Only the public pilot env is loaded. Compose gets no workstation environment overrides.
    path = repo / SOURCE
    env = {"PATH": os.environ["PATH"], "COMPOSE_DISABLE_ENV_FILE": "1"}
    rendered = command("docker", "compose", "--env-file", str(repo / ENV), "-f", str(path),
                       "config", "--format", "json", cwd=path.parent, env=env)
    model = json.loads(rendered)
    if set(model["services"]) != {SERVICE}:
        raise PilotError("Expected exactly one pilot service")
    service = model["services"][SERVICE]
    require_image(service["image"])
    if (set(service["networks"]) != {"default"} or
            set(service) - {"cap_drop", "command", "container_name", "deploy", "entrypoint", "environment",
                            "healthcheck", "hostname",
                            "image", "networks", "restart", "security_opt", "tmpfs", "user"} or
            service.get("cap_drop") != ["ALL"] or service.get("user") != SERVICE or
            service.get("command") is not None or service.get("entrypoint") is not None or
            service.get("security_opt") not in (["no-new-privileges:true"], ["no-new-privileges=true"]) or
            set(service["environment"]) != {"CAPTCHA_SOLVER", "LOG_HTML", "LOG_LEVEL", "TZ"} or
            service["environment"]["CAPTCHA_SOLVER"] != "none" or
            service["environment"]["LOG_HTML"] != "false" or
            service["environment"]["LOG_LEVEL"] not in {"info", "warning", "error"} or
            service["environment"]["TZ"] != "America/New_York"):
        raise PilotError("FlareSolverr configuration requires owner review")
    return service


def git(repo, *args):
    return command("git", "-C", str(repo), *args)


def pin_line(contents):
    matches = re.findall(r"^    image: ([^\s]+)$", contents, re.MULTILINE)
    if len(matches) != 1:
        raise PilotError("Expected one literal pilot image")
    return require_image(matches[0])


def manifest(image, arch):
    image = require_image(image)
    name, digest = image.split("@", 1)
    index = json.loads(command("docker", "buildx", "imagetools", "inspect", "--raw", image))
    platform = next((item for item in index.get("manifests", [])
                     if item.get("platform", {}).get("os") == "linux"
                     and item["platform"].get("architecture") == arch), None)
    if platform is None:
        raise PilotError(f"Cannot find linux/{arch} in the pinned image index")
    config = json.loads(command("docker", "buildx", "imagetools", "inspect", "--raw",
                                f"{name}@{platform['digest']}"))["config"]["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", config) or not digest.startswith("sha256:"):
        raise PilotError("Invalid registry manifest")
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
    def __init__(self, root, config, headers):
        self.root = root
        self.config = config
        self.headers = headers
        self.service = load(root / "template.json")
        self.docker = ["docker"]
        if config.get("docker_context"):
            self.docker += ["--context", config["docker_context"]]

    def docker_call(self, *args):
        return command(*self.docker, *args)

    def komodo(self, kind, name, **params):
        return request(f"{self.config['komodo_url'].rstrip('/')}/{kind}", "POST",
                       {"type": name, "params": params}, self.headers)

    def approved_stack(self):
        stack = self.komodo("read", "GetStack", stack=self.config["stack"])
        config = stack["config"]
        if any(config.get(key) != value for key, value in {
                "files_on_host": True, "project_name": self.config["project"],
                "run_directory": str(self.root / "stack"), "file_paths": ["compose.json"],
                "extra_args": STACK_ARGS, "auto_pull": False, "webhook_enabled": False,
                "auto_update": False, "auto_update_all_services": False, "run_build": False,
                "destroy_before_deploy": False,
        }.items()) or any(config.get(key) for key in (
            "environment", "additional_env_files", "compose_cmd_wrapper", "file_contents", "swarm_id",
        )) or any(config.get(hook, {}).get("command") for hook in ("pre_deploy", "post_deploy")):
            raise PilotError("Komodo pilot Stack settings differ from the approved policy", "executor")
        return stack["_id"]["$oid"]

    def idle(self):
        stack_id = self.approved_stack()
        state = self.komodo("read", "GetStackActionState", stack=self.config["stack"])
        updates = self.komodo("read", "ListUpdates", query={"target": {"type": "Stack", "id": stack_id}})
        return all(state.get(key) is False for key in ("pulling", "deploying", "starting",
                   "restarting", "pausing", "unpausing", "stopping", "destroying")) and not any(
            item["status"] == "InProgress" for item in updates["updates"]
        )

    def wait_update(self, update):
        deadline = time.monotonic() + 420
        while update["status"] == "InProgress":
            if time.monotonic() >= deadline:
                raise PilotError("Komodo deployment did not finish", "executor")
            time.sleep(2)
            update = self.komodo("read", "GetUpdate", id=update["_id"]["$oid"])
        if update["status"] != "Complete" or update["success"] is not True:
            raise PilotError("Komodo deployment failed", "executor")

    def image_id(self, image):
        return self.docker_call("image", "inspect", "-f", "{{.Id}}", require_image(image)).strip()

    def inspect(self):
        # Constrain Docker's response to the properties needed for the safety check.
        template = ('{"image":{{json .Image}},"health":{{json .State.Health.Status}},'
                    '"configured":{{json .Config.Image}},"restarts":{{json .RestartCount}},"mounts":{{json .Mounts}},'
                    '"networks":{{json .NetworkSettings.Networks}},'
                    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
        return json.loads(self.docker_call("inspect", "-f", template, self.config["container"]))

    def target(self):
        status = self.inspect()
        if (status["project"], status["service"]) != (self.config["project"], SERVICE):
            raise PilotError("Unexpected Compose ownership")
        mounts = status["mounts"]
        if len(mounts) != 1 or mounts[0]["Type"] != "volume" or mounts[0]["Destination"] != "/config":
            raise PilotError("Unexpected FlareSolverr storage")
        if set(status["networks"]) != {self.config["network"]}:
            raise PilotError("Unexpected FlareSolverr network")
        return status, mounts[0]["Name"]

    def empty_volume(self):
        volume = self.config["volume"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", volume):
            raise PilotError("Invalid FlareSolverr volume")
        mount = Path(self.docker_call("volume", "inspect", "-f", "{{.Mountpoint}}", volume).strip())
        if not mount.is_absolute() or not mount.is_dir() or any(mount.iterdir()):
            raise PilotError("FlareSolverr config volume requires owner recovery review", "volume")

    def stage(self, image):
        service = {**self.service, "image": require_image(image), "container_name": self.config["container"]}
        service["labels"] = {MANAGED_LABEL: "true"}
        service["volumes"] = [{"type": "volume", "source": "pilotconfig", "target": "/config",
                              "read_only": True, "volume": {"nocopy": True}}]
        save(self.root / "stack" / "compose.json", {"name": self.config["project"], "services": {SERVICE: service},
             "networks": {"default": {"name": self.config["network"], "external": True}},
             "volumes": {"pilotconfig": {"name": self.config["volume"], "external": True}}})

    def prepare(self, previous, candidate):
        if not self.idle():
            raise PilotError("Komodo is busy")
        self.verify(previous, observe=False)
        self.empty_volume()
        try:
            self.docker_call("pull", "--platform", "linux/" + self.config["architecture"], candidate)
            self.image_id(candidate)
        except (PilotError, OSError, subprocess.TimeoutExpired):
            raise PilotError("Could not cache the candidate image", "registry") from None

    def deploy(self, image):
        self.approved_stack()
        self.empty_volume()
        self.image_id(image)
        self.stage(image)
        update = self.komodo("execute", "DeployStack", stack=self.config["stack"], services=[SERVICE])
        self.wait_update(update)

    def restore(self, image):
        self.stage(image)
        try:
            status, _ = self.target()
            if status["configured"] == image:
                self.verify(image)
                return
        except (PilotError, OSError, ValueError):
            pass
        self.deploy(image)
        self.verify(image)

    def verify(self, image, observe=True):
        expected = self.image_id(image)
        deadline = time.monotonic() + (self.config["observe_seconds"] if observe else 0)
        first = True
        while True:
            status, volume = self.target()
            if status["image"] != expected:
                raise PilotError("FlareSolverr is running a different image", "running-image")
            if volume != self.config["volume"] or (observe and status["mounts"][0]["RW"]):
                raise PilotError("Unexpected FlareSolverr volume state", "volume")
            if status["health"] != "healthy" or status["restarts"]:
                raise PilotError("FlareSolverr container failed verification", "container-health")
            try:
                self.probe("/health")
                if first:
                    session = uuid.uuid4().hex
                    created = self.probe("/v1", {"cmd": "sessions.create", "session": session})
                    if created.get("status") != "ok" or created.get("session") != session:
                        raise PilotError("FlareSolverr browser session did not start")
                    deleted = self.probe("/v1", {"cmd": "sessions.destroy", "session": session})
                    if deleted.get("status") != "ok":
                        raise PilotError("FlareSolverr browser session did not stop")
                    first = False
            except (PilotError, OSError, ValueError, subprocess.TimeoutExpired):
                raise PilotError("FlareSolverr client route failed", "client-probe") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(15, remaining))


    def probe(self, route, data=None):
        args = ["exec", self.config.get("probe_container", "prowlarr"), "curl", "-fsS", "--max-time", "30"]
        if data is not None:
            args += ["-H", "Content-Type: application/json", "--data", json.dumps(data)]
        output = self.docker_call(*args, "http://flaresolverr:8191" + route)
        return json.loads(output) if data is not None else None


def source_signature(repo, revision):
    source = git(repo, "show", f"{revision}:{SOURCE}")
    image = pin_line(source)
    env = [git(repo, "show", f"{revision}:{path}") for path in
           (ENV, Path("docker/.env"), STACK.with_name(".env"))]
    override = git(repo, "ls-tree", "-r", revision, "docker/stacks/nexus/services/flaresolverr")
    stack = git(repo, "show", f"{revision}:{STACK}")
    required = ("  - path: ../../services/flaresolverr/compose.yaml\n"
                "    env_file: ${TECHNIS_ENV:?run compose through docker/scripts/stacks}/flaresolverr.env\n")
    if required not in stack or "\nname: nexus\n" not in stack or "\n    name: nexus_default\n" not in stack:
        raise PilotError("Nexus pilot wiring changed")
    contents = [source.replace(image, "PILOT_IMAGE", 1), *env, override]
    return hashlib.sha256("\0".join(contents).encode()).hexdigest(), image


def effective_service(repo):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    rendered = command("bash", "docker/scripts/stacks", "-s", "nexus", "render", cwd=repo, env=env)
    model = json.loads(rendered)
    service = compose_service(repo)
    if (model["name"] != "nexus" or model["networks"]["default"]["name"] != "nexus_default"
            or model["services"].get(SERVICE) != service):
        raise PilotError("Nexus effective FlareSolverr differs from the approved service model")
    return service


def check(repo):
    service = effective_service(repo)
    image = pin_line((repo / SOURCE).read_text())
    if service["image"] != image:
        raise PilotError("Nexus does not use the pilot digest")
    base = os.environ.get("PILOT_BASE", "")
    if REVISION.fullmatch(base):
        previous = git(repo, "show", f"{base}:{SOURCE}")
        # The initial adoption is the one transition from an unpinned image.
        if IMAGE.search(previous):
            old_signature, _ = source_signature(repo, base)
            signature, _ = source_signature(repo, "HEAD")
            if signature != old_signature:
                raise PilotError("The pilot accepts only image changes")
    manifest(image, "amd64")
    print("Nexus Compose policy and linux/amd64 digest verified")


def github(method, path, data=None, token=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "technis-flare-pilot"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return request("https://api.github.com" + path, method, data, headers)


def checked_commit(repository, revision):
    if not REPOSITORY.fullmatch(repository) or not REVISION.fullmatch(revision):
        raise PilotError("Invalid GitHub release source")
    branch = f"/repos/{repository}/rules/branches/main"
    checks = {"Compose Policy", "Gitleaks"}
    rules = github("GET", branch)
    types = {rule["type"] for rule in rules}
    if not {"pull_request", "non_fast_forward", "deletion"} <= types or not any(
            rule["type"] == "pull_request" and rule["parameters"]["require_code_owner_review"] for rule in rules):
        return False
    enforced = {(check["context"], check.get("integration_id")) for rule in rules if rule["type"] == "required_status_checks"
                and rule["parameters"]["strict_required_status_checks_policy"]
                for check in rule["parameters"]["required_status_checks"]}
    runs = github("GET", f"/repos/{repository}/commits/{revision}/check-runs?per_page=100")
    passed = {run["name"] for run in runs["check_runs"]
              if run["app"]["slug"] == "github-actions" and run["head_sha"] == revision
              and run["conclusion"] == "success" and (run["name"], run["app"]["id"]) in enforced}
    return checks <= passed


def vault(repo, ref):
    if not re.fullmatch(r"vault://technis/[A-Za-z0-9_./-]+", ref):
        raise PilotError("Expected an Infisical vault reference")
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
        raise PilotError("Could not sign GitHub App authentication")
    jwt = (message + b"." + b64(signature.stdout)).decode()
    repo_name = config["incident_repository"].split("/", 1)[1]
    token = github("POST", f"/app/installations/{installation}/access_tokens",
                   {"repositories": [repo_name], "permissions": {"issues": "write"}}, jwt)
    return token["token"]


def read_headers(repo, config):
    return {"X-Api-Key": vault(repo, config["komodo_key"]),
            "X-Api-Secret": vault(repo, config["komodo_secret"])}


def observe_settings(config):
    if (config["project"], config["stack"], config["container"], config["network"],
            config["architecture"]) != ("nexus", "nexus", SERVICE, "nexus_default", "amd64"):
        raise PilotError("Expected the Nexus FlareSolverr pilot target")
    if (config.get("docker_context") or config.get("probe_container", "prowlarr") != "prowlarr"
            or config["observe_seconds"] < 300):
        raise PilotError("Expected the local Nexus engine and a five minute observation window")
    url = urllib.parse.urlsplit(config["komodo_url"])
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise PilotError("Invalid private Komodo endpoint")
    if url.scheme != "https" and (url.scheme, url.hostname) != ("http", "127.0.0.1"):
        raise PilotError("Use TLS for Komodo outside loopback")


def enroll(root, repo, config):
    state_path = root / "state.json"
    if state_path.exists():
        raise PilotError("Pilot is already enrolled")
    revision = git(repo, "rev-parse", "HEAD").strip()
    signature, image = source_signature(repo, revision)
    service = effective_service(repo)
    if service["image"] != image or git(repo, "status", "--porcelain", "--", str(SOURCE), str(ENV), str(STACK)):
        raise PilotError("Enroll from a clean checked-out pilot configuration")
    save(root / "template.json", service)
    host = Runtime(root, config, read_headers(repo, config))
    status, volume = host.target()
    if host.config["volume"] != volume:
        raise PilotError("Configure the exact existing FlareSolverr volume")
    host.docker_call("pull", "--platform", "linux/amd64", image)
    if status["image"] != host.image_id(image):
        raise PilotError("The pinned baseline differs from the live image")
    host.verify(image, observe=False)
    host.empty_volume()
    host.stage(image)
    save(state_path, {"good": image, "pending": None, "failed": {}, "outbox": {},
                      "signature": signature, "revision": revision})


def poll(root, repo, config):
    path = root / "state.json"
    state = load(path)
    host = Runtime(root, config, read_headers(repo, config))
    outcome = resume(path, host)
    state = load(path)
    # Fail closed before a new mutation if private issue delivery is unavailable.
    token = incident_token(repo, config)
    api = lambda method, url, data=None: github(method, url, data, token)
    repository = config["incident_repository"]
    if not REPOSITORY.fullmatch(repository):
        raise PilotError("An incident repository is required")
    incident_repo = api("GET", f"/repos/{repository}")
    if incident_repo.get("private") is not True or incident_repo.get("has_issues") is not True:
        raise PilotError("A private incident repository with Issues is required")
    report(path, api, repository)
    if state["pending"] or not config.get("promote", False):
        return outcome
    upstream = config["repository"]
    if not REPOSITORY.fullmatch(upstream):
        raise PilotError("A release repository is required")
    url = f"https://github.com/{upstream}.git"
    git(repo, "fetch", "--no-tags", url, "refs/heads/main")
    revision = git(repo, "rev-parse", "FETCH_HEAD").strip()
    if not REVISION.fullmatch(revision) or not checked_commit(upstream, revision):
        raise PilotError("Waiting for protected main and successful checks on the exact commit")
    if state["revision"] and not git_is_ancestor(repo, state["revision"], revision):
        raise PilotError("Main has diverged from the enrolled revision")
    signature, image = source_signature(repo, revision)
    if signature != state["signature"]:
        raise PilotError("The pilot only promotes image changes")
    outcome = apply(path, host, revision, image)
    if outcome in {"promoted", "unchanged"}:
        state = load(path)
        state["revision"] = revision
        save(path, state)
    report(path, api, repository)
    return outcome


def git_is_ancestor(repo, old, new):
    try:
        git(repo, "merge-base", "--is-ancestor", old, new)
        return True
    except PilotError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "enroll", "poll"))
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--state", type=Path, default=Path("/var/lib/technis/pilot"))
    args = parser.parse_args()
    os.umask(0o077)
    repo = args.repository.resolve()
    if args.action == "check":
        check(repo)
    else:
        root = args.state.resolve()
        config = load(root / "settings.json")
        observe_settings(config)
        with locked(root):
            if args.action == "enroll":
                enroll(root, repo, config)
                print("Enrolled the existing FlareSolverr digest")
            else:
                print(poll(root, repo, config))


if __name__ == "__main__":
    try:
        main()
    except PilotError as error:
        raise SystemExit(str(error)) from None
    except (OSError, KeyError, ValueError, subprocess.TimeoutExpired):
        raise SystemExit("Pilot operation unavailable. Check host state and credential provisioning.") from None
