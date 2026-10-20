"""Consumer setup flows, including failure and a relocated plugin entrypoint."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentpeers import native, __version__
from agentpeers.core import Bridge
from agentpeers.mcp import dispatch
from agentpeers.setup import doctor, registrations, start_probe, verify_probe, CHALLENGE_PREFIX
from test_bridge import C, A, SCOPE

ROOT = Path(__file__).resolve().parent.parent



# Legacy journeys stub the new read-only probe rather than contacting real agents.
def setUpModule():
    global _return_route_fixture
    _return_route_fixture = patch.object(native, "receive_route", return_value={
        "status": "available", "transport": "fixture", "checked_at": 1,
        "proof": "fixture candidate only"})
    _return_route_fixture.start()


def tearDownModule():
    _return_route_fixture.stop()


class SetupJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pb-setup-')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'AGENTPEERS_HOME': str(self.root / 'state')})
        self.env.start()
        self.identity = patch('agentpeers.setup.resolve_actor', return_value=C)
        self.identity.start()

    def tearDown(self):
        self.identity.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_doctor_is_read_only_and_missing_clients_are_actionable(self):
        with patch('agentpeers.setup.platform.system', return_value='Darwin'), \
             patch('agentpeers.setup.shutil.which', return_value=None), \
             patch.object(native, 'Desktop', side_effect=native.Unavailable('closed')), \
             patch.object(native, 'shared_threads', side_effect=native.Unavailable('stopped')), \
             patch.object(native, 'claude_sessions', return_value=[]), \
             patch('agentpeers.setup.registrations', return_value={'codex': {}, 'claude': {}}):
            result = doctor()
        self.assertFalse(result['ready_for_test'])
        self.assertEqual(result['message_test'], 'not_run')
        self.assertFalse((self.root / 'state').exists())
        failures = [c for c in result['checks'] if c['status'] == 'fail']
        self.assertGreaterEqual(len(failures), 4)
        self.assertTrue(all(c.get('fix') for c in failures))

    def test_submission_and_one_way_read_cannot_pass_setup(self):
        with patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'send_claude', return_value={'transport': 'fixture'}):
            probe = start_probe(A, SCOPE, C)
        ident = probe['probe_id']
        self.assertEqual(verify_probe(ident, C)['message_test'], 'awaiting_peer')
        with Bridge() as bridge:
            bridge.ack(ident, A)
        self.assertEqual(verify_probe(ident, C)['message_test'], 'awaiting_peer')
        with self.assertRaises(ValueError):
            verify_probe(ident, A)
        with Bridge() as bridge, patch.object(native, 'send_codex') as desktop:
            desktop.return_value = {'transport': 'fixture'}
            wrong = bridge.reply(ident, A, 'An unrelated acknowledgment')
            bridge.ack(wrong['id'], C)
            self.assertEqual(verify_probe(ident, C)['message_test'], 'awaiting_peer')
            reply = bridge.reply(ident, A, CHALLENGE_PREFIX + ident)
            self.assertEqual(verify_probe(ident, C)['message_test'], 'awaiting_local_read')
            bridge.ack(reply['id'], C)
        result = verify_probe(ident, C)
        self.assertEqual(result['message_test'], 'round_trip_verified')
        self.assertTrue(result['request_read'] and result['reply_read'])

    def test_existing_setup_challenge_survives_the_package_rename(self):
        with patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'send_claude', return_value={'transport': 'fixture'}):
            probe = start_probe(A, SCOPE, C)
        ident = probe['probe_id']
        challenge = 'PEERBRIDGE-SETUP:' + ident
        with Bridge() as bridge, patch.object(native, 'send_codex', return_value={'transport': 'fixture'}):
            bridge.db.execute('UPDATE messages SET body=? WHERE id=?',
                              ('Peerbridge setup test.\nChallenge: ' + challenge + '\nRead and reply.', ident))
            bridge.db.commit()
            bridge.ack(ident, A)
            reply = bridge.reply(ident, A, challenge)
            bridge.ack(reply['id'], C)
        self.assertEqual(verify_probe(ident, C)['message_test'], 'round_trip_verified')

    def test_setup_failure_does_not_retry_or_claim_success(self):
        with patch.object(native, 'claude_sessions', return_value=[]):
            probe = start_probe(A, SCOPE, C)
        result = verify_probe(probe['probe_id'], C)
        self.assertEqual(result['message_test'], 'unavailable')
        self.assertFalse(result['automatic_retry'])
        with self.assertRaises(ValueError):
            start_probe(C, SCOPE, C)
        with patch('agentpeers.core.current_actor', return_value=C):
            with self.assertRaises(ValueError):
                dispatch('setup', {'to': A})
            with self.assertRaises(ValueError):
                dispatch('setup', {'to': A, 'scope': SCOPE, 'sender': A})
            with self.assertRaises(ValueError):
                dispatch('setup', {'to': A, 'probe_id': probe['probe_id'], 'scope': SCOPE})

    def test_plugin_registration_reports_only_own_state_and_disabled_is_not_ready(self):
        responses = [{'installed': [{'name': 'agentpeers', 'enabled': False}]},
                     [{'name': 'other', 'enabled': True, 'secret': 'must-not-leak'}]]
        home = self.root / 'claude'
        (home / 'plugins').mkdir(parents=True)
        (home / 'plugins/installed_plugins.json').write_text(json.dumps({'plugins': {'agentpeers@agentpeers': [{}]}}))
        (home / 'settings.json').write_text(json.dumps({'enabledPlugins': {'agentpeers@agentpeers': False}, 'secret': 'must-not-leak'}))
        with patch('agentpeers.setup.command_json', side_effect=responses), \
             patch.object(native, 'claude_home', return_value=home), \
             patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(home)}):
            result = registrations()
        self.assertFalse(result['codex']['plugin'])
        self.assertFalse(result['claude']['plugin'])
        self.assertNotIn('must-not-leak', json.dumps(result))

    def test_plugin_mcp_inventory_is_not_a_duplicate_manual_registration(self):
        responses = [{'installed': [{'name': 'agentpeers', 'enabled': True}]},
                     [{'name': 'agentpeers', 'enabled': True,
                       'transport': {'env': {'PLUGIN_ROOT': '/fixture/plugin'}}}]]
        with patch('agentpeers.setup.command_json', side_effect=responses), \
             patch('agentpeers.setup.read_json', return_value={}):
            result = registrations()
        self.assertTrue(result['codex']['plugin'])
        self.assertFalse(result['codex']['manual_mcp'])

    def test_manual_registration_with_null_environment_is_detected(self):
        responses = [{'installed': []}, [{'name': 'agentpeers', 'enabled': True,
                                         'transport': {'env': None}}]]
        with patch('agentpeers.setup.command_json', side_effect=responses), \
             patch('agentpeers.setup.read_json', return_value={}):
            result = registrations()
        self.assertTrue(result['codex']['manual_mcp'])
        self.assertFalse(result['codex']['plugin'])

    def test_native_manifest_launches_relocated_bundle_without_pip(self):
        installed = self.root / 'plugin cache with spaces'
        shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('.git', '__pycache__', 'proof'))
        config = json.loads((installed / 'mcp.json').read_text())['mcpServers']['agentpeers']
        requests = '\n'.join(json.dumps(r) for r in [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'id': 2, 'method': 'tools/list'},
            {'id': 3, 'method': 'tools/call', 'params': {'name': 'setup', 'arguments': {'to': A}}},
        ]) + '\n'
        env = {**os.environ, 'AGENTPEERS_SESSION': C}
        result = subprocess.run([config['command'], *config['args']], cwd=installed,
                                input=requests, text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]['result']['serverInfo']['version'], __version__)
        names = {t['name'] for t in replies[1]['result']['tools']}
        self.assertTrue({'setup', 'send_message', 'acknowledge', 'reply'}.issubset(names))
        self.assertTrue(replies[2]['result']['isError'])
        self.assertIn('scope authorized', replies[2]['result']['content'][0]['text'])
        # Claude has a separate absolute plugin-root expansion, still the same executable.
        claude = json.loads((installed / '.claude-plugin/plugin.json').read_text())['mcpServers']['agentpeers']
        command = claude['command'].replace('${CLAUDE_PLUGIN_ROOT}', str(installed))
        self.assertFalse((installed / '.mcp.json').exists(),
                         'A root .mcp.json also registers a broken project server when Claude opens this checkout')
        result = subprocess.run([command, *claude['args']], cwd=self.root, input=requests,
                                capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]['result']['serverInfo']['version'], __version__)
        self.assertIn('reply', {t['name'] for t in replies[1]['result']['tools']})

    def test_missing_python_fails_before_mcp_with_clear_stderr(self):
        result = subprocess.run([str(ROOT / 'bin/agentpeers'), 'mcp'],
                                env={'PATH': str(self.root)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, '')
        self.assertIn('Python 3.10+', result.stderr)


if __name__ == '__main__':
    unittest.main()
