"""Message usability through CLI entrypoints and MCP/core contracts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch
from agentpeers import native
from agentpeers.core import Bridge
from agentpeers.mcp import dispatch

C='codex:'+str(uuid.uuid4())
A='claude:'+str(uuid.uuid4())
SCOPE='Human authorized fixture messaging only'
ROOT=Path(__file__).resolve().parent.parent


# Legacy journeys stub the new read-only probe rather than contacting real agents.
def setUpModule():
    global _return_route_fixture
    _return_route_fixture = patch.object(native, "receive_route", return_value={
        "status": "available", "transport": "fixture", "checked_at": 1,
        "proof": "fixture candidate only"})
    _return_route_fixture.start()


def tearDownModule():
    _return_route_fixture.stop()


class OptionsJourney(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'AGENTPEERS_HOME':self.temp.name})
        self.env.start()
        self.bridge=Bridge()
        self.transport=patch.object(native,'send_claude',return_value={'transport':'fixture'})
        self.claude=patch.object(native,'claude_sessions',return_value=[{'id':A}])
        self.sent=self.transport.start();self.claude.start()
    def tearDown(self):
        self.bridge.db.close();self.transport.stop();self.claude.stop();self.env.stop();self.temp.cleanup()
    def test_mcp_options_survive_reply_and_decision_lookup_without_implying_consent(self):
        row=dispatch('send_message',dict(to=A,message='Check original human decision',scope=SCOPE,
                     expects='ack',summary='Decision already recorded',record_ref='/fixture/decision.md',decision_id='layout-1'),actor=C)
        envelope=self.sent.call_args.args[1]
        self.assertEqual(envelope['expects'],'ack')
        self.assertIn('do not send a courtesy reply',envelope['receipt'])
        self.assertIsNone(row['read_at'])
        with patch.object(native,'send_codex',return_value={'transport':'fixture'}):
            answer=dispatch('reply',dict(id=row['id'],message='Read original record',summary='Checked',record_ref='/fixture/decision.md'),actor=A)
        self.assertEqual(answer['decision_id'],'layout-1')
        self.assertEqual(answer['expects'],'none')
        self.assertEqual(answer['scope'],SCOPE)
        status=dispatch('decision_status',dict(decision_id='layout-1'))
        self.assertEqual(len(status['messages']),2)
        self.assertIn('not verified',status['authority'])
    def test_running_mcp_setup_needs_no_late_import_after_plugin_cache_removal(self):
        import builtins
        real_import=builtins.__import__
        def deny_setup_import(name,*args,**kwargs):
            if name=='setup' or name.endswith('.setup'):
                raise ModuleNotFoundError('old plugin cache removed')
            return real_import(name,*args,**kwargs)
        with patch('agentpeers.mcp.doctor',return_value={'version':'fixture'}), patch('builtins.__import__',side_effect=deny_setup_import):
            self.assertEqual(dispatch('setup',{}),{'version':'fixture'})

    def test_ack_is_silent_even_for_no_reply_message(self):
        row=self.bridge.send(C,A,'notice',SCOPE,expects='none')
        self.assertIsNone(row['read_at'])
        with patch.object(native,'send_codex') as reverse:
            self.bridge.ack(row['id'],A)
            reverse.assert_not_called()
        self.assertEqual(self.sent.call_count,1)
    def test_options_are_part_of_idempotency_and_rejected_before_effects(self):
        row=self.bridge.send(C,A,'notice',SCOPE,expects='none',summary='Short',decision_id='d1')
        same=self.bridge.send(C,A,'notice',SCOPE,ident=row['id'],expects='none',summary='Short',decision_id='d1')
        self.assertFalse(same['submission_attempted'])
        with self.assertRaises(ValueError):
            self.bridge.send(C,A,'notice',SCOPE,ident=row['id'],expects='reply',summary='Short',decision_id='d1')
        for kwargs in [dict(expects='yes'),dict(summary='two\nlines'),dict(summary='x'*301),dict(record_ref=''),dict(decision_id='x'*201)]:
            with self.assertRaises(ValueError):self.bridge.send(C,A,'notice',SCOPE,**kwargs)
        self.assertEqual(self.sent.call_count,1)
    def test_reference_is_never_opened_or_fetched(self):
        with patch('builtins.open',side_effect=AssertionError('references must be inert')):
            row=self.bridge.send(C,A,'notice',SCOPE,record_ref='https://example.invalid/private')
        self.assertEqual(row['record_ref'],'https://example.invalid/private')
    def test_legacy_insert_defaults_remain_readable(self):
        ident=str(uuid.uuid4())
        self.bridge.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',(ident,C,A,'old',SCOPE,None,1,'submitted',None,None))
        self.bridge.db.commit()
        self.assertEqual(self.bridge.get(ident)['expects'],'reply')
        self.assertEqual(self.bridge.db.execute('PRAGMA table_info(messages)').fetchall().__len__(),10)

class CliInputJourney(unittest.TestCase):
    def run_cli(self,home,*args,input=None,actor=C):
        env={**os.environ,'AGENTPEERS_HOME':str(home/'state'),'CLAUDE_CONFIG_DIR':str(home/'claude'),'AGENTPEERS_SESSION':actor,'CODEX_HOME':str(home/'codex')}
        return subprocess.run([str(ROOT/'bin/agentpeers'),*args],input=input,capture_output=True,env=env)
    def test_file_and_stdin_preserve_quotes_newlines_unicode_and_options(self):
        body="Maintainer's decision: `literal` $(not-a-command) $HOME\n日本語 😀\n"
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);p=home/'message.txt';p.write_text(body)
            for source,data in [(('--message-file',str(p)),None),(('--message','-'),body.encode()),(('--message-file','-'),body.encode())]:
                result=self.run_cli(home,'send','--to',A,'--scope',SCOPE,*source,'--expects','none','--summary','Short','--decision-id','test-1',input=data)
                self.assertEqual(result.returncode,2,result.stderr)
                row=json.loads(result.stdout)
                self.assertEqual(row['body'],body)
                self.assertEqual(row['expects'],'none')
                self.assertEqual(row['delivery'],'unavailable')
                status=self.run_cli(home,'status',row['id'])
                self.assertEqual(json.loads(status.stdout)['body'],body)
            correlated=self.run_cli(home,'decision-status','test-1')
            self.assertEqual(len(json.loads(correlated.stdout)['messages']),3)
    def test_reply_file_and_stdin_preserve_content_scope_decision_and_default_no_reply(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d);p=home/'answer.txt';body="It's literal.\n日本語\n";p.write_text(body)
            first=self.run_cli(home,'send','--to',A,'--scope',SCOPE,'--message','fixture','--decision-id','answer-1')
            ident=json.loads(first.stdout)['id']
            for source,data in [(('--message-file',str(p)),None),(('--message','-'),body.encode())]:
                result=self.run_cli(home,'reply',ident,*source,input=data,actor=A)
                self.assertEqual(result.returncode,2,result.stderr)
                row=json.loads(result.stdout)
                self.assertEqual(row['body'],body)
                self.assertEqual(row['scope'],SCOPE)
                self.assertEqual(row['decision_id'],'answer-1')
                self.assertEqual(row['expects'],'none')
                self.assertEqual(row['recipient'],C)

    def test_oversized_invalid_utf8_missing_and_conflicting_inputs_fail_without_messages(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d)
            for data in [b'x'*32001,b'\xff',b'']:
                result=self.run_cli(home,'send','--to',A,'--scope',SCOPE,'--message','-',input=data)
                self.assertEqual(result.returncode,1)
            result=self.run_cli(home,'send','--to',A,'--scope',SCOPE,'--message-file',str(home/'missing'))
            self.assertEqual(result.returncode,1)
            result=self.run_cli(home,'send','--to',A,'--scope',SCOPE,'--message','x','--message-file','y')
            self.assertEqual(result.returncode,2)
            with patch.dict(os.environ,{'AGENTPEERS_HOME':str(home/'state')}),Bridge() as b:
                self.assertEqual(b.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)

class WorkerQuestionJourney(unittest.TestCase):
    setUp=OptionsJourney.setUp
    tearDown=OptionsJourney.tearDown
    def test_question_returns_to_exact_dispatcher_inherits_scope_and_cannot_repeat(self):
        assignment=self.bridge.send(C,A,'Fixture assignment',SCOPE,allow_question=True,decision_id='d-worker')
        with patch.object(native,'send_codex',return_value={'transport':'fixture'}) as reverse:
            question=dispatch('ask_dispatcher',dict(assignment_id=assignment['id'],message='Which fixture value?'),actor=A)
            self.assertEqual(question['recipient'],C)
            self.assertEqual(question['reply_to'],assignment['id'])
            self.assertEqual(question['scope'],SCOPE)
            self.assertEqual(question['decision_id'],'d-worker')
            self.assertEqual(question['expects'],'reply')
            with self.assertRaises(ValueError):self.bridge.ask_dispatcher(assignment['id'],A,'Second?')
            reverse.assert_called_once()
        self.assertEqual(self.bridge.get(assignment['id'])['question_id'],question['id'])
    def test_no_implicit_optin_wrong_actor_or_named_worker(self):
        row=self.bridge.send(C,A,'ordinary',SCOPE)
        with self.assertRaises(ValueError):self.bridge.ask_dispatcher(row['id'],A,'question')
        row=self.bridge.send(C,A,'assigned',SCOPE,allow_question=True)
        with self.assertRaises(ValueError):self.bridge.ask_dispatcher(row['id'],C,'question')
        with self.assertRaises(ValueError):self.bridge.send(C,'worker-name','assigned',SCOPE,allow_question=True)
        with self.assertRaises(ValueError):self.bridge.send(C,A,'assigned',SCOPE,allow_question=True,expects='none')
    def test_uncertain_question_uses_limit_and_preserves_receipt_without_replay(self):
        assignment=self.bridge.send(C,A,'assignment',SCOPE,allow_question=True)
        with patch.object(native,'send_codex',side_effect=native.Unconfirmed('lost response')) as reverse:
            question=self.bridge.ask_dispatcher(assignment['id'],A,'question')
            self.assertEqual(question['delivery'],'unconfirmed')
            with self.assertRaises(ValueError):self.bridge.ask_dispatcher(assignment['id'],A,'retry')
            reverse.assert_called_once()
    def test_concurrent_questions_claim_one_slot_atomically(self):
        import threading
        assignment=self.bridge.send(C,A,'assignment',SCOPE,allow_question=True)
        barrier=threading.Barrier(2);results=[];errors=[]
        def ask():
            with Bridge() as b:
                barrier.wait(timeout=3)
                try:results.append(b.ask_dispatcher(assignment['id'],A,'question'))
                except ValueError as e:errors.append(str(e))
        with patch.object(native,'send_codex',return_value={'transport':'fixture'}) as reverse:
            threads=[threading.Thread(target=ask) for _ in range(2)]
            for t in threads:t.start()
            for t in threads:t.join(timeout=5)
            self.assertEqual(len(results),1)
            self.assertEqual(len(errors),1)
            reverse.assert_called_once()

if __name__=='__main__':unittest.main()
