import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import release

OLD = "ghcr.io/example/server:v1.2.3@sha256:" + "a" * 64
NEW = "ghcr.io/example/server:v1.2.4@sha256:" + "b" * 64
TARGET = release.Target(
    host="example", service="server", architecture="amd64", repository="ghcr.io/example/server",
    tags=r"v1\.\d+\.\d+", environment={},
    probe={"container": "client", "port": 8080, "health": "/healthz"},
)
REV = "c" * 40


class Host:
    def __init__(self):
        self.policy = TARGET
        self.running = OLD
        self.fail = set()
        self.deploys = []
        self.busy = False

    def state_path(self, target):
        return self.path

    def prepare(self, previous, candidate):
        assert self.running == previous

    def deploy(self, image):
        self.deploys.append(image)
        self.running = image

    def restore(self, image):
        self.deploy(image)
        self.verify(image)

    def verify(self, image):
        assert self.running == image
        if image in self.fail:
            raise RuntimeError("synthetic-credential http://private.invalid/internal")

    def idle(self):
        return not self.busy


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.host = Host()
        release.save(self.path, {"good": OLD, "pending": None, "failed": {}, "outbox": {}})

    def test_rejects_mutable_and_foreign_images(self):
        for bad in ("ghcr.io/example/server:latest", NEW.replace("example/", "other/"),
                    NEW.replace("v1.2.4", "v1.2.4-dev"), NEW[:-1]):
            with self.subTest(image=bad), self.assertRaises(release.ReleaseError):
                TARGET.require_image(bad)
        self.assertEqual(TARGET.require_image(NEW), NEW)

    def test_success_records_candidate_and_duplicate_is_noop(self):
        self.assertEqual(release.apply(self.path, self.host, REV, NEW), "promoted")
        self.assertEqual(release.apply(self.path, self.host, REV, NEW), "unchanged")
        self.assertEqual(release.load(self.path)["good"], NEW)
        self.assertEqual(self.host.deploys, [NEW])

    def test_probe_failure_restores_exact_image_and_sanitizes_issue(self):
        self.host.fail.add(NEW)
        self.assertEqual(release.apply(self.path, self.host, REV, NEW), "restored")
        self.assertEqual(release.apply(self.path, self.host, "d" * 40, NEW), "quarantined")
        state = release.load(self.path)
        self.assertEqual(state["good"], OLD)
        self.assertEqual(self.host.deploys, [NEW, OLD])
        fingerprint, failure = next(iter(state["outbox"].items()))
        issue = release.issue(TARGET, fingerprint, failure)
        self.assertIn(REV, issue["body"])
        self.assertIn(OLD, issue["body"])
        self.assertIn("probe", issue["body"])
        self.assertIn("example/server", issue["body"])
        self.assertNotIn("synthetic-credential", str(issue) + self.path.read_text())
        self.assertNotIn("private.invalid", str(issue) + self.path.read_text())

    def test_failed_recovery_keeps_pending_until_retry(self):
        self.host.fail.update((OLD, NEW))
        self.assertEqual(release.apply(self.path, self.host, REV, NEW), "recovery blocked")
        self.assertIsNotNone(release.load(self.path)["pending"])
        self.host.fail.remove(OLD)
        self.assertEqual(release.resume(self.path, self.host), "restored")
        self.assertIsNone(release.load(self.path)["pending"])
        self.assertEqual(len(release.load(self.path)["outbox"]), 1)

    def test_interrupted_attempt_waits_for_komodo(self):
        state = release.load(self.path)
        state["pending"] = {"revision": REV, "candidate": NEW, "phase": "deploy"}
        release.save(self.path, state)
        self.host.busy = True
        self.assertEqual(release.resume(self.path, self.host), "recovery blocked")
        self.assertEqual(self.host.deploys, [])
        self.host.busy = False
        self.assertEqual(release.resume(self.path, self.host), "restored")
        self.assertEqual(self.host.deploys, [OLD])

    def test_komodo_submission_does_not_count_as_success(self):
        class Pending(release.Runtime):
            def komodo(self, kind, name, **params):
                assert (kind, name, params) == ("read", "GetUpdate", {"id": "1" * 24})
                return {"status": "Complete", "success": False}

        runtime = object.__new__(Pending)
        with patch.object(release.time, "sleep"), self.assertRaises(release.ReleaseError):
            runtime.wait_update({"_id": {"$oid": "1" * 24}, "status": "InProgress", "success": True})

    def test_staging_keeps_other_enrolled_service_in_the_same_project(self):
        root = Path(self.tmp.name)
        (root / "stack").mkdir()
        second = release.Target(
            host="example", service="worker", architecture="amd64", repository="ghcr.io/example/worker",
            tags=r"v1\.\d+\.\d+", environment={}, probe=TARGET.probe,
        )
        worker_ref = OLD.replace("/server:", "/worker:")
        inventories = {TARGET.service: TARGET, second.service: second}
        runtime = release.Runtime(root, {}, {}, TARGET, inventories)
        for target, image in ((TARGET, OLD), (second, worker_ref)):
            state = runtime.state_path(target)
            state.parent.mkdir(parents=True)
            release.save(state, {"good": image})
            release.save(state.with_name("template.json"), {"container_name": target.service})
        runtime.stage(NEW)
        active = release.load(root / "stack" / "compose.json")
        self.assertEqual(active["name"], "example")
        self.assertEqual({k: v["image"] for k, v in active["services"].items()},
                         {"server": NEW, "worker": worker_ref})

    def test_multi_service_file_pins_the_requested_service(self):
        exporter = replace(TARGET, source_service="datastore")
        compose = f"services:\n  datastore:\n    image: redis:latest\n  server:\n    image: {OLD}\n"
        self.assertEqual(exporter.source, Path("docker/services/datastore/compose.yaml"))
        self.assertEqual(release.pin_line(exporter, compose), OLD)

    def test_shared_signature_ignores_sibling_only_compose_changes(self):
        worker = replace(TARGET, source_service="datastore")
        override = worker.stack.parent / "services/datastore/compose.yaml"
        stack = ("\nname: example\ninclude:\n  - path:\n"
                 "      - ../../services/datastore/compose.yaml\n"
                 "      - services/datastore/compose.yaml\n"
                 "    env_file: ${TECHNIS_ENV:?run compose through docker/scripts/stacks}/datastore.env\n")

        def git(_repo, operation, *args):
            if operation == "ls-tree":
                return "blob" if str(override) in args[-1] else ""
            revision, path = args[0].split(":", 1)
            return {str(worker.stack): stack,
                    str(worker.source): (f"services:\n  datastore:\n    image: redis:{revision}\n"
                                         f"  server:\n    image: {OLD}\n"),
                    str(override): f"services:\n  datastore:\n    hostname: {revision}\n"}[path]

        with patch.object(release, "git", side_effect=git):
            first = release.source_signature(self.path.parent, "old", worker)
            second = release.source_signature(self.path.parent, "new", worker)
        self.assertEqual(first, second)

    def test_rollback_recreates_worker_even_when_dependency_is_down(self):
        worker = replace(TARGET, depends_on=["datastore"])
        runtime = release.Runtime(self.path.parent, {}, {}, worker, {worker.service: worker})
        with (patch.object(runtime, "stage"),
              patch.object(runtime, "target", return_value=({"configured": NEW}, None)),
              patch.object(runtime, "approved_stack"),
              patch.object(runtime, "image_id"),
              patch.object(runtime, "komodo") as komodo,
              patch.object(runtime, "wait_update"),
              patch.object(runtime, "dependencies_ready", side_effect=release.ReleaseError("Backend unavailable")),
              patch.object(runtime, "verify", side_effect=release.ReleaseError("Backend unavailable"))):
            with self.assertRaisesRegex(release.ReleaseError, "Backend unavailable"):
                runtime.restore(OLD)
        komodo.assert_called_once_with("execute", "DeployStack", stack=worker.host, services=[worker.service])

    def test_staging_preserves_worker_labels_and_root_filesystem(self):
        worker = replace(TARGET, depends_on=["datastore"], read_only=True)
        root = self.path.parent
        (root / "stack").mkdir()
        runtime = release.Runtime(root, {}, {}, worker, {worker.service: worker})
        runtime.state_path(worker).parent.mkdir(parents=True)
        template = {"read_only": True, "labels": {"observability.metrics.address": "server:9121"},
                    "depends_on": {"datastore": {"condition": "service_healthy", "required": True}}}
        release.save(runtime.state_path(worker).with_name("template.json"), template)
        runtime.stage(NEW)
        service = release.load(root / "stack" / "compose.json")["services"][worker.service]
        self.assertTrue(service["read_only"])
        self.assertEqual(service["labels"], {**template["labels"], release.MANAGED_LABEL: "true"})
        self.assertNotIn("depends_on", service)

    def test_sqlite_rollback_proves_previous_pin_against_latest_data(self):
        target = replace(TARGET, read_only=True, sqlite_db="/config/atuin.db")
        runtime = release.Runtime(self.path.parent, {}, {}, target, {target.service: target})
        calls = []
        with (patch.object(runtime, "target", return_value=({"configured": NEW}, None)),
              patch.object(runtime, "check_sqlite", side_effect=lambda image: calls.append(("check", image))),
              patch.object(runtime, "deploy", side_effect=lambda image: calls.append(("deploy", image))),
              patch.object(runtime, "verify", side_effect=lambda image: calls.append(("verify", image)))):
            runtime.restore(OLD)
        self.assertEqual(calls, [("check", OLD), ("deploy", OLD), ("verify", OLD)])

    def test_incompatible_sqlite_rollback_leaves_live_image_and_database_alone(self):
        target = replace(TARGET, read_only=True, sqlite_db="/config/atuin.db")
        runtime = release.Runtime(self.path.parent, {}, {}, target, {target.service: target})
        with (patch.object(runtime, "target", return_value=({"configured": NEW}, None)),
              patch.object(runtime, "check_sqlite", side_effect=release.ReleaseError("Incompatible database")),
              patch.object(runtime, "stage") as stage,
              patch.object(runtime, "deploy") as deploy,
              self.assertRaisesRegex(release.ReleaseError, "Incompatible database")):
            runtime.restore(OLD)
        stage.assert_not_called()
        deploy.assert_not_called()

    def test_sqlite_copy_includes_committed_wal_writes_and_detects_mutation(self):
        source = self.path.parent / "live.db"
        copy = self.path.parent / "copy.db"
        with sqlite3.connect(source) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("CREATE TABLE history (entry TEXT)")
            live.execute("INSERT INTO history VALUES ('accepted by candidate')")
            live.commit()
            release.copy_sqlite(source, copy)
            before = release.sqlite_signature(copy)
            with sqlite3.connect(copy) as replica:
                self.assertEqual(replica.execute("SELECT entry FROM history").fetchall(),
                                 [("accepted by candidate",)])
                replica.execute("DELETE FROM history")
            self.assertNotEqual(before, release.sqlite_signature(copy))
            self.assertEqual(live.execute("SELECT entry FROM history").fetchall(),
                             [("accepted by candidate",)])

    def test_missing_backend_metric_blocks_promotion(self):
        worker = replace(TARGET, probe={**TARGET.probe, "contains": "redis_up 1"})
        runtime = release.Runtime(self.path.parent, {}, {}, worker, {worker.service: worker})
        with (patch.object(runtime, "docker_call", return_value="redis_up 0\n"),
              self.assertRaisesRegex(release.ReleaseError, "response did not contain")):
            runtime.probe(worker.probe["health"])

    def test_rejects_image_defaults_that_would_create_storage_or_run_as_root(self):
        worker = replace(TARGET, image_user="59000:59000")
        runtime = release.Runtime(self.path.parent, {}, {}, worker, {worker.service: worker})
        metadata = ({"id": "image-id", "user": "0:0", "volumes": None},
                    {"id": "image-id", "user": worker.image_user, "volumes": {"/state": {}}})
        for invalid in metadata:
            with (self.subTest(metadata=invalid),
                  patch.object(runtime, "docker_call", return_value=json.dumps(invalid)),
                  self.assertRaisesRegex(release.ReleaseError, "user or volume defaults changed")):
                runtime.image_id(NEW)

    def test_release_commit_requires_both_successful_checks_on_the_exact_commit(self):
        endpoint = f"/repos/owner/releases/commits/{REV}/check-runs?per_page=100"
        compose = {"name": "Compose Policy", "head_sha": REV, "conclusion": "success",
                   "app": {"slug": "github-actions"}}
        gitleaks = {**compose, "name": "Gitleaks"}

        def github(_method, path):
            return {"check_runs": [compose, gitleaks]} if path == endpoint else []

        with patch.object(release, "github", side_effect=github) as api:
            self.assertTrue(release.checked_commit("owner/releases", REV))
            api.assert_called_once_with("GET", endpoint)

        mismatches = (
            {"name": "Another Check"},
            {"head_sha": "d" * 40},
            {"conclusion": "failure"},
            {"app": {"slug": "other-app"}},
        )
        for required, other in ((compose, gitleaks), (gitleaks, compose)):
            for mismatch in mismatches:
                with self.subTest(name=required["name"], mismatch=mismatch):
                    checks = [other, {**required, **mismatch}]
                    with patch.object(release, "github", return_value={"check_runs": checks}):
                        self.assertFalse(release.checked_commit("owner/releases", REV))

    def test_production_promotion_defaults_on_after_required_checks(self):
        self.host.path = self.path
        state = release.load(self.path)
        release.save(self.path, {**state, "revision": "d" * 40, "signature": "reviewed"})

        def git(repo, operation, *_):
            return REV + "\n" if operation == "rev-parse" else ""

        config = {"repository": "owner/releases"}
        with (patch.object(release, "git", side_effect=git),
              patch.object(release, "source_signature", return_value=("reviewed", NEW)),
              patch.object(release, "checked_commit", return_value=False),
              patch.object(release, "incident_api") as api):
            with self.assertRaises(release.ReleaseError):
                release.poll(self.path.parent, config, [self.host])
            api.assert_not_called()
            self.assertEqual(self.host.running, OLD)
        with (patch.object(release, "git", side_effect=git),
              patch.object(release, "source_signature", return_value=("reviewed", NEW)),
              patch.object(release, "checked_commit", return_value=True),
              patch.object(release, "incident_api", return_value=(None, "owner/incidents"))):
            release.poll(self.path.parent, config, [self.host])
        self.assertEqual(self.host.running, NEW)
        self.assertEqual(release.load(self.path)["revision"], REV)

    def test_reporting_retries_and_respects_private_repository(self):
        self.host.fail.add(NEW)
        release.apply(self.path, self.host, REV, NEW)
        existing = []
        private = False
        drop_response = True

        def api(method, path, data=None):
            nonlocal drop_response
            if path == "/repos/owner/incidents":
                return {"private": private, "has_issues": True}
            if method == "GET":
                return existing
            existing.append(data)
            if drop_response:
                drop_response = False
                raise OSError("response lost")
            return data

        with self.assertRaises(release.ReleaseError):
            release.report(self.path, TARGET, api, "owner/incidents")
        self.assertEqual(existing, [])
        private = True
        with self.assertRaises(OSError):
            release.report(self.path, TARGET, api, "owner/incidents")
        self.assertEqual(release.report(self.path, TARGET, api, "owner/incidents"), 1)
        self.assertEqual(len(existing), 1)
        self.assertEqual(release.load(self.path)["outbox"], {})


if __name__ == "__main__":
    unittest.main()
