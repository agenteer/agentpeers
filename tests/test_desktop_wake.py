"""Desktop wake must preserve provenance, destination and no-replay guarantees."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import native
from agentpeers.addressing import resolve_name
from agentpeers.core import Bridge
from agentpeers.desktop_wake import policy

C = "codex:" + str(uuid.uuid4())
A = "claude:" + str(uuid.uuid4())
IDENT = C.split(":")[1]


def missing():
    return native.Unavailable("no owner", code="desktop_owner_missing",
                              desktop_connected=True, desktop_owner_found=False)


class DesktopWake(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"AGENTPEERS_HOME": self.folder.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_opt_in_persists_and_disable_preserves_mailbox(self):
        self.assertFalse(policy()["enabled"])
        with Bridge() as bridge:
            before = bridge.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertTrue(policy(True)["enabled"])
            self.assertEqual(Path(policy()["path"]).stat().st_mode & 0o777, 0o600)
            self.assertFalse(policy(False)["enabled"])
            self.assertEqual(bridge.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before)

    def test_policy_rejects_public_or_symlink_files(self):
        p = Path(self.folder.name) / "desktop-wake.json"
        p.write_text('{"enabled":true}')
        p.chmod(0o644)
        with self.assertRaises(ValueError):
            policy()
        p.unlink()
        other = Path(self.folder.name) / "other"
        other.write_text('{"enabled":true}')
        p.symlink_to(other)
        with self.assertRaises(ValueError):
            policy(True)

    def test_disabled_missing_owner_does_not_open_anything(self):
        d = native.Desktop()
        with patch.object(d, "owner", side_effect=missing()), patch.object(d, "wake") as wake:
            with self.assertRaises(native.Unavailable):
                d.send({"id": C}, {})
            wake.assert_not_called()

    def test_enabled_wake_preserves_tool_output_and_sends_once(self):
        policy(True)
        d = native.Desktop()
        envelope = {"id": str(uuid.uuid4()), "kind": "peer-message", "body": "$(literal) `literal`", "from": A}
        with patch.object(d, "owner", side_effect=missing()), patch.object(d, "wake", return_value="receiver") as wake, \
             patch.object(d, "call", side_effect=[{"resultType": "error", "error": "not currently running"},
                                                {"resultType": "success", "result": {}}]) as call:
            result = d.send({"id": C}, envelope)
        self.assertTrue(result["desktop_wake"])
        wake.assert_called_once_with(IDENT)
        self.assertEqual(call.call_count, 2)
        for invocation in call.call_args_list:
            params = invocation.args[1]
            request = params.get("turnStart", {}).get("request", params)
            self.assertEqual(request["input"], [])
            self.assertEqual(json.loads(request["toolOutput"]["output"]), envelope)
            self.assertEqual(invocation.args[3], "receiver")

    def test_uncertain_or_incompatible_receiver_never_wakes(self):
        policy(True)
        for code in ["desktop_owner_discovery_failed", "desktop_owner_incompatible"]:
            d = native.Desktop()
            with patch.object(d, "owner", side_effect=native.Unavailable("fixture", code=code)), \
                 patch.object(d, "wake") as wake:
                with self.assertRaises(native.Unavailable):
                    d.send({"id": C}, {})
                wake.assert_not_called()

    def test_post_wake_submission_timeout_is_not_retried(self):
        policy(True)
        d = native.Desktop()
        with patch.object(d, "owner", side_effect=missing()), patch.object(d, "wake", return_value="receiver") as wake, \
             patch.object(d, "call", side_effect=TimeoutError("after write")) as call:
            with self.assertRaises(native.Unconfirmed):
                d.send({"id": C}, {"id": str(uuid.uuid4())})
        wake.assert_called_once()
        call.assert_called_once()

    def test_open_uses_fixed_uuid_url_without_message_and_verifies_owner(self):
        d = native.Desktop()
        with patch.object(native.platform, "system", return_value="Darwin"), \
             patch.object(native, "AppServer") as app, patch.object(native.subprocess, "run") as run, \
             patch.object(d, "owner", return_value="receiver"):
            app.return_value.__enter__.return_value.call.return_value = {"thread": {"id": IDENT}}
            run.return_value.returncode = 0
            self.assertEqual(d.wake(IDENT), "receiver")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-g", "codex://threads/" + IDENT])

    def test_unknown_chat_is_not_opened_and_open_success_is_not_delivery(self):
        d = native.Desktop()
        with patch.object(native.platform, "system", return_value="Darwin"), \
             patch.object(native, "AppServer") as app, patch.object(native.subprocess, "run") as run:
            app.return_value.__enter__.return_value.call.side_effect = native.Unavailable("not found")
            with self.assertRaises(native.Unavailable):
                d.wake(IDENT)
            run.assert_not_called()
        with patch.object(native.platform, "system", return_value="Darwin"), \
             patch.object(native, "AppServer") as app, patch.object(native.subprocess, "run") as run, \
             patch.object(d, "owner", side_effect=missing()), patch.object(native.time, "monotonic", side_effect=[0, 31]):
            app.return_value.__enter__.return_value.call.return_value = {"thread": {"id": IDENT}}
            run.return_value.returncode = 0
            with self.assertRaises(native.Unavailable) as failed:
                d.wake(IDENT)
            self.assertEqual(failed.exception.diagnostics["code"], "desktop_wake_failed")

    def test_archived_chat_and_invalid_uuid_are_not_opened(self):
        d = native.Desktop()
        with patch.object(native.platform, "system", return_value="Darwin"), \
             patch.object(native, "AppServer") as app, patch.object(native.subprocess, "run") as run:
            app.return_value.__enter__.return_value.call.return_value = {
                "thread": {"id": IDENT, "path": "/fixture/archived_sessions/chat.jsonl"}}
            with self.assertRaises(native.Unavailable):
                d.wake(IDENT)
            with self.assertRaises(ValueError):
                d.wake("not-a-uuid?prompt=anything")
            run.assert_not_called()

    def test_unique_unloaded_name_can_resolve_without_opening(self):
        policy(True)
        row = {"id": C, "runtime": "codex", "name_address": "receiver"}
        with patch.object(native, "Desktop") as desktop:
            desktop.return_value.__enter__.return_value.owner.side_effect = missing()
            self.assertEqual(resolve_name("receiver", [row])[0], row)
            desktop.return_value.__enter__.return_value.wake.assert_not_called()
            duplicate = {**row, "id": "codex:" + str(uuid.uuid4())}
            with self.assertRaisesRegex(native.Unavailable, "Ambiguous"):
                resolve_name("receiver", [row, duplicate])

    def test_return_probe_is_read_only_and_reports_wake_requirement(self):
        policy(True)
        with patch.object(native, "SharedServer") as shared, patch.object(native, "Desktop") as desktop:
            shared.return_value.__enter__.side_effect = native.Unavailable("no server")
            desktop.return_value.__enter__.return_value.owner.side_effect = missing()
            route = native.receive_route(C)
            self.assertEqual(route["status"], "wake_required")
            desktop.return_value.__enter__.return_value.wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
