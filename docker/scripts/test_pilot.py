import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pilot

OLD = "ghcr.io/flaresolverr/flaresolverr:v3.5.2@sha256:" + "a" * 64
NEW = "ghcr.io/flaresolverr/flaresolverr:v3.5.3@sha256:" + "b" * 64
REV = "c" * 40


class Host:
    def __init__(self):
        self.running = OLD
        self.fail = set()
        self.deploys = []
        self.busy = False

    def prepare(self, previous, candidate):
        assert self.running == previous

    def deploy(self, image):
        self.deploys.append(image)
        self.running = image

    def verify(self, image):
        assert self.running == image
        if image in self.fail:
            raise RuntimeError("synthetic-credential http://private.invalid/internal")

    def idle(self):
        return not self.busy


class PilotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.host = Host()
        pilot.save(self.path, {"good": OLD, "pending": None, "failed": {}, "outbox": {}})

    def test_rejects_mutable_and_foreign_images(self):
        for bad in ("ghcr.io/flaresolverr/flaresolverr:latest", NEW.replace("flaresolverr/", "other/"),
                    NEW.replace("v3.5.3", "v3.5.3-dev"), NEW[:-1]):
            with self.subTest(image=bad), self.assertRaises(pilot.PilotError):
                pilot.require_image(bad)
        self.assertEqual(pilot.require_image(NEW), NEW)

    def test_success_records_candidate_and_duplicate_is_noop(self):
        self.assertEqual(pilot.apply(self.path, self.host, REV, NEW), "promoted")
        self.assertEqual(pilot.apply(self.path, self.host, REV, NEW), "unchanged")
        self.assertEqual(pilot.load(self.path)["good"], NEW)
        self.assertEqual(self.host.deploys, [NEW])

    def test_probe_failure_restores_exact_image_and_sanitizes_issue(self):
        self.host.fail.add(NEW)
        self.assertEqual(pilot.apply(self.path, self.host, REV, NEW), "restored")
        self.assertEqual(pilot.apply(self.path, self.host, "d" * 40, NEW), "quarantined")
        state = pilot.load(self.path)
        self.assertEqual(state["good"], OLD)
        self.assertEqual(self.host.deploys, [NEW, OLD])
        fingerprint, failure = next(iter(state["outbox"].items()))
        issue = pilot.issue(fingerprint, failure)
        self.assertIn(REV, issue["body"])
        self.assertIn(OLD, issue["body"])
        self.assertIn("probe", issue["body"])
        self.assertNotIn("synthetic-credential", str(issue) + self.path.read_text())
        self.assertNotIn("private.invalid", str(issue) + self.path.read_text())

    def test_failed_recovery_keeps_pending_until_retry(self):
        self.host.fail.update((OLD, NEW))
        self.assertEqual(pilot.apply(self.path, self.host, REV, NEW), "recovery blocked")
        self.assertIsNotNone(pilot.load(self.path)["pending"])
        self.host.fail.remove(OLD)
        self.assertEqual(pilot.resume(self.path, self.host), "restored")
        self.assertIsNone(pilot.load(self.path)["pending"])
        self.assertEqual(len(pilot.load(self.path)["outbox"]), 1)

    def test_interrupted_attempt_waits_for_komodo(self):
        state = pilot.load(self.path)
        state["pending"] = {"revision": REV, "candidate": NEW, "phase": "deploy"}
        pilot.save(self.path, state)
        self.host.busy = True
        self.assertEqual(pilot.resume(self.path, self.host), "recovery blocked")
        self.assertEqual(self.host.deploys, [])
        self.host.busy = False
        self.assertEqual(pilot.resume(self.path, self.host), "restored")
        self.assertEqual(self.host.deploys, [OLD])

    def test_komodo_submission_does_not_count_as_success(self):
        class Pending(pilot.Runtime):
            def komodo(self, kind, name, **params):
                assert (kind, name, params) == ("read", "GetUpdate", {"id": "1" * 24})
                return {"status": "Complete", "success": False}

        runtime = object.__new__(Pending)
        with patch.object(pilot.time, "sleep"), self.assertRaises(pilot.PilotError):
            runtime.wait_update({"_id": {"$oid": "1" * 24}, "status": "InProgress", "success": True})

    def test_reporting_retries_and_respects_private_repository(self):
        self.host.fail.add(NEW)
        pilot.apply(self.path, self.host, REV, NEW)
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

        with self.assertRaises(pilot.PilotError):
            pilot.report(self.path, api, "owner/incidents")
        self.assertEqual(existing, [])
        private = True
        with self.assertRaises(OSError):
            pilot.report(self.path, api, "owner/incidents")
        self.assertEqual(pilot.report(self.path, api, "owner/incidents"), 1)
        self.assertEqual(len(existing), 1)
        self.assertEqual(pilot.load(self.path)["outbox"], {})


if __name__ == "__main__":
    unittest.main()
