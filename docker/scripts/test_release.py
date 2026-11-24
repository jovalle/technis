import tempfile
import unittest
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
