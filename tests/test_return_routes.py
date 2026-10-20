"""Return-route warnings and mailbox recovery, with no live agent sends."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agentpeers import native
from agentpeers.core import Bridge
from agentpeers.mcp import dispatch

C = 'codex:' + str(uuid.uuid4())
A = 'claude:' + str(uuid.uuid4())


class ReturnJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'AGENTPEERS_HOME': self.temp.name})
        self.env.start(); self.addCleanup(self.env.stop)
        self.bridge = Bridge(); self.addCleanup(self.bridge.db.close)
        self.route = {'session_id': C, 'status': 'unavailable', 'transport': None,
                      'checked_at': 10, 'proof': 'candidate only'}

    def send(self, expects='reply'):
        def deliver(target, envelope):
            saved = self.bridge.get(envelope['id'])
            self.assertEqual(saved['return_route'], self.route)
            self.assertEqual(envelope['return_route'], self.route)
            self.assertTrue(envelope['warnings'])
            return {'transport': 'fixture'}
        with patch.object(native, 'receive_route', return_value=self.route) as check, \
             patch.object(native, 'claude_sessions', return_value=[{'id': A}]), \
             patch.object(native, 'send_claude', side_effect=deliver) as outbound:
            row = self.bridge.send(C, A, 'Please report progress', 'approved fixture only', expects=expects)
        self.assertEqual(check.call_count, 1); self.assertEqual(outbound.call_count, 1)
        self.assertEqual(row['delivery'], 'submitted')
        self.assertIsNone(row['read_at'])
        return row

    def test_missing_return_route_is_visible_to_both_agents_before_delivery(self):
        row = self.send()
        self.assertEqual(next(iter(row)), 'warnings')
        self.assertIn('must collect', row['warnings'][0])
        with Bridge() as reopened, patch.object(native, 'receive_route') as check:
            old = reopened.send(C, A, 'Please report progress', 'approved fixture only', ident=row['id'])
            self.assertTrue(old['reused_id'])
            self.assertEqual(old['return_route'], self.route)
            check.assert_not_called()

    def test_even_messages_marked_no_response_show_the_return_route(self):
        # A conversational reply may ask a question while retaining expects:none.
        self.assertTrue(self.send(expects='none')['warnings'])

    def test_failed_reply_is_collectable_and_original_status_exposes_it(self):
        request = self.send()
        with patch.object(native, 'receive_route', return_value={'status':'available'}), \
             patch.object(native, 'send_codex', side_effect=native.Unavailable('no desktop route')), \
             patch.object(self.bridge, 'unavailable_hint', return_value={}):
            reply = self.bridge.reply(request['id'], A, 'Built; review pending')
        self.assertEqual(reply['transport_status'], 'unavailable')
        self.assertTrue(reply['mailbox_available'])
        self.assertIn('not yet read', reply['delivery_note'])
        self.assertNotIn('Session ID ended', reply['detail']['reason'])
        pending = self.bridge.get(request['id'])['replies'][0]
        self.assertEqual(pending['id'], reply['id'])
        self.assertIsNone(pending['read_at'])
        self.assertEqual(self.bridge.inbox(C)[0]['body'], 'Built; review pending')
        with self.assertRaises(ValueError): self.bridge.ack(reply['id'], A)
        read = self.bridge.ack(reply['id'], C)
        self.assertEqual(read['delivery'], 'read')
        self.assertEqual(read['transport_status'], 'unavailable')
        self.assertIn('Read from the mailbox', read['delivery_note'])

    def test_old_receipts_gain_explanation_without_rewriting_history(self):
        request = self.send()
        with self.bridge.db:
            self.bridge.db.execute('DELETE FROM message_return_routes WHERE message_id=?',(request['id'],))
            self.bridge.db.execute("UPDATE messages SET status='unavailable', detail=? WHERE id=?",
                                   (json.dumps({'reason':'Session ID ended or is unavailable'}),request['id']))
        row = self.bridge.get(request['id'])
        self.assertIsNone(row['return_route'])
        self.assertTrue(row['mailbox_available'])
        self.assertIn('Saved in the recipient',row['delivery_note'])
        self.assertEqual(row['detail']['reason'],'Session ID ended or is unavailable')
        self.assertIsNone(row['read_at'])

    def test_unresolved_name_is_not_claimed_as_recipient_mailbox(self):
        with patch('agentpeers.core.inventory', return_value=[]), patch.object(native,'receive_route') as check:
            row = self.bridge.send(C,'missing-name','hello','fixture')
        self.assertFalse(row['mailbox_available'])
        self.assertNotIn('delivery_note',row)
        check.assert_not_called()

    def test_failed_preflight_never_claims_a_candidate_or_blocks_a_saved_send(self):
        with patch.object(native,'receive_route',side_effect=RuntimeError('fixture')), \
             patch.object(native,'claude_sessions',return_value=[{'id':A}]), \
             patch.object(native,'send_claude',return_value={'transport':'fixture'}):
            row = self.bridge.send(C,A,'notice','fixture')
        self.assertEqual(row['return_route']['status'],'unknown')
        self.assertTrue(row['warnings'])
        self.assertEqual(row['delivery'],'submitted')

    def test_actual_cli_status_exposes_correlated_reply_and_does_not_ack(self):
        request = self.send()
        with patch.object(native,'receive_route',return_value={'status':'available'}), \
             patch.object(native,'send_codex',side_effect=native.Unavailable('fixture')), \
             patch.object(self.bridge,'unavailable_hint',return_value={}):
            reply = self.bridge.reply(request['id'],A,'status fixture')
        result = subprocess.run([sys.executable,'-m','agentpeers','status',request['id']],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['replies'][0]['id'],reply['id'])
        self.assertIsNone(self.bridge.get(reply['id'])['read_at'])
        status = dispatch('message_status',{'id':reply['id']})
        self.assertTrue(status['mailbox_available'])
        self.assertIn('not yet read',status['delivery_note'])


class CandidateChecks(unittest.TestCase):
    def test_shared_route_requires_exact_sender_and_uses_only_read_methods(self):
        with patch.object(native,'SharedServer') as shared, patch.object(native,'Desktop') as desktop:
            client=shared.return_value.__enter__.return_value
            client.call.return_value={'data':[C.split(':')[1]]}
            route=native.receive_route(C)
            desktop.assert_not_called()
            self.assertEqual(route['transport'],'codex-shared-server')
            self.assertEqual(client.call.call_args.args[0],'thread/loaded/list')
            shared.assert_called_once_with(timeout=2)

    def test_another_loaded_thread_does_not_prove_sender_can_receive(self):
        with patch.object(native,'SharedServer') as shared, patch.object(native,'Desktop') as desktop:
            shared.return_value.__enter__.return_value.call.return_value={'data':[str(uuid.uuid4())]}
            desktop.return_value.__enter__.return_value.owner.side_effect=native.Unavailable('no owner', code='desktop_owner_missing')
            self.assertEqual(native.receive_route(C)['status'],'unavailable')
            desktop.return_value.__enter__.return_value.owner.assert_called_once_with(C.split(':')[1])

    def test_desktop_candidate_does_not_send_or_resume(self):
        with patch.object(native,'SharedServer',side_effect=native.Unavailable('no server')), \
             patch.object(native,'Desktop') as desktop:
            self.assertEqual(native.receive_route(C)['transport'],'desktop')
            owner=desktop.return_value.__enter__.return_value
            owner.send.assert_not_called()
            owner.owner.assert_called_once_with(C.split(':')[1])

    def test_claude_candidate_uses_its_own_advertised_inbox(self):
        with patch.object(native,'claude_sessions',return_value=[{'id':A}]):
            self.assertEqual(native.receive_route(A)['transport'],'claude-native-inbox')
        with patch.object(native,'claude_sessions',return_value=[]):
            self.assertEqual(native.receive_route(A)['status'],'unavailable')
        with patch.object(native,'claude_sessions',side_effect=native.Unavailable('sandbox')):
            self.assertEqual(native.receive_route(A)['status'],'unknown')
