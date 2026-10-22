"""Run the production Python Worker in workerd with fixture-only native transports."""
import base64
import hashlib
import html
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

WRANGLER = os.environ.get('AGENTPEERS_WRANGLER')
SCRATCH = os.environ.get('AGENTPEERS_TEST_SCRATCH')


@unittest.skipUnless(WRANGLER and SCRATCH, 'Cloud runtime checks need Wrangler and an admitted scratch directory')
class CloudRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.temp = tempfile.TemporaryDirectory(prefix='agentpeers-cloud-', dir=SCRATCH)
        cls.path = Path(cls.temp.name)
        runtime = root / '.cloud-build/runtime'
        assert (runtime / 'worker.py').exists(), 'Run scripts/prepare_cloud.py first'
        shutil.copytree(runtime, cls.path / 'runtime')
        shutil.copytree(root / 'python_modules', cls.path / 'python_modules')
        shutil.copy2(root / 'tests/cloud_fixture.py', cls.path / 'runtime/fixture.py')
        (cls.path / '.dev.vars').write_text('AGENTPEERS_OWNER_KEY="' + 'o' * 40 + '"\n')
        config = {'name':'agentpeers-cloud-fixture','main':'runtime/fixture.py','base_dir':'runtime','compatibility_date':'2026-10-22','compatibility_flags':['python_workers'],'durable_objects':{'bindings':[{'name':'MAILBOX','class_name':'Mailbox'}]},'migrations':[{'tag':'v1','new_sqlite_classes':['Mailbox']}],'observability':{'enabled':False}}
        (cls.path / 'wrangler.json').write_text(json.dumps(config))
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1',0)); cls.port = reserve.getsockname()[1]
        cls.log = (cls.path / 'runtime.log').open('w+')
        env = {k:v for k,v in os.environ.items() if not k.startswith('CLOUDFLARE_')}
        env.update(WRANGLER_SEND_METRICS='false', XDG_CONFIG_HOME=str(cls.path / 'tool-config'), CI='true')
        cls.process = subprocess.Popen([WRANGLER,'dev','--local','--ip','127.0.0.1','--port',str(cls.port),'--inspector-port','0','--log-level','error','--persist-to',str(cls.path / 'state')],cwd=cls.path,env=env,stdout=cls.log,stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.close_runtime)
        deadline=time.monotonic()+25
        while True:
            try:
                connection=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=2)
                connection.request('GET','/setup');r=connection.getresponse();raw=r.read();connection.close()
                if r.status==200:break
                raise OSError('Worker returned error')
            except OSError:
                if cls.process.poll() is not None or time.monotonic()>deadline:
                    cls.log.seek(0);raise AssertionError('Cloud fixture failed to start: '+cls.log.read()[-2000:])
                time.sleep(.1)

    @classmethod
    def close_runtime(cls):
        if cls.process.poll() is None:cls.process.terminate()
        cls.process.wait(timeout=10); cls.log.close(); cls.temp.cleanup()

    def request(self,method,path,body=None,token=None,headers=None):
        h=dict(headers or {})
        if token:h['Authorization']='Bearer '+token
        if body is not None and not isinstance(body,str):body=json.dumps(body);h['Content-Type']='application/json'
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=8)
        connection.request(method,path,body=body,headers=h)
        r=connection.getresponse();status,headers,raw=r.status,r.headers,r.read();connection.close()
        return status,headers,json.loads(raw) if raw and 'application/json' in headers.get('Content-Type','') else raw.decode()

    def setUp(self):
        self.request('POST','/test/reset',{})
        self.config={'owner_key':'o'*40,'public_url':'https://bridge.example'}
        self.setup_args={'owner_key':'o'*40,'public_url':'https://bridge.example','webhook_url':'https://vendor.example/routine','webhook_key':'fixture-only','callback_hosts':'callbacks.example'}
        status,_,page=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)})
        self.assertEqual(status,200,page)
        self.grok=re.search('<pre>([^<]+)</pre>',page)[1]

    def connect_dot(self):
        status,_,client=self.request('POST','/oauth/register',{'redirect_uris':['https://chatgpt.com/connector/oauth/test']});self.assertEqual(status,201)
        verifier='v'*43
        args={'client_id':client['client_id'],'redirect_uri':client['redirect_uris'][0],'resource':'https://bridge.example/mcp/dot','response_type':'code','code_challenge_method':'S256','code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),'state':'return-state'}
        status,_,page=self.request('GET','/oauth/authorize?'+urlencode(args));self.assertEqual(status,200)
        fields={html.unescape(k):html.unescape(v) for k,v in re.findall('type="hidden" name="([^"]+)" value="([^"]*)"',page)}
        status,headers,_=self.request('POST','/oauth/authorize',urlencode({**fields,'owner_key':'o'*40}),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'https://bridge.example'});self.assertEqual(status,303)
        code=parse_qs(urlsplit(headers['Location']).query)['code'][0]
        status,_,token=self.request('POST','/oauth/token',urlencode({'grant_type':'authorization_code','code':code,'code_verifier':verifier,'redirect_uri':args['redirect_uri'],'client_id':args['client_id'],'resource':args['resource']}),headers={'Content-Type':'application/x-www-form-urlencoded'});self.assertEqual(status,200,token)
        return token['access_token']

    def rpc(self,token,method,params=None):
        status,_,value=self.request('POST','/mcp/dot',{'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},token)
        self.assertEqual(status,200,value);self.assertNotIn('error',value);return value['result']

    def tool(self,token,name,**args):
        result=self.rpc(token,'tools/call',{'name':name,'arguments':args});self.assertNotIn('isError',result);return result['structuredContent']

    def subscribe(self,token):
        return self.rpc(token,'events/subscribe',{'name':'message.created','arguments':{'recipient':'dot'},'delivery':{'mode':'webhook','url':'https://callbacks.example/event','secret':'whsec_'+base64.b64encode(b'k'*32).decode()}})

    def delivery(self,mid):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            for row in self.request('GET','/test/deliveries')[2]:
                p=row['payload']
                if p.get('message_id')==mid or p.get('eventId')==mid:return row
            time.sleep(.05)
        self.fail('Native fixture notification did not arrive for '+mid)

    def test_dot_to_grok_and_followup_are_actually_consumed(self):
        token=self.connect_dot();self.subscribe(token)
        self.tool(token,'send_message',to='Grokbot',message='Which interface wakes you?',scope='Fixture conversation',message_id='cloud-question')
        callback=urlsplit(self.delivery('cloud-question')['payload']['callback_url']).path
        self.assertTrue(self.request('POST',callback,{'action':'claim'})[2]['claimed'])
        self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.request('POST',callback,{'action':'reply','message':'A native routine webhook. Should I ask a follow-up?','reply_id':'cloud-answer','expects':'reply'})[0],200)
        self.delivery('cloud-answer')
        incoming=self.tool(token,'claim_message',message_id='cloud-answer');self.assertIn('native routine',incoming['body'])
        self.tool(token,'acknowledge',message_id='cloud-answer')
        self.tool(token,'reply',message_id='cloud-answer',message='Yes; this is the final answer.',reply_id='cloud-final')
        callback=urlsplit(self.delivery('cloud-final')['payload']['callback_url']).path
        self.request('POST',callback,{'action':'claim'});self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.tool(token,'message_status',message_id='cloud-final')['state'],'read')

    def test_grok_initiates_and_dot_replies(self):
        token=self.connect_dot();self.subscribe(token)
        status,_,sent=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'How do events wake you?','scope':'Fixture conversation','message_id':'grok-first'}},self.grok);self.assertEqual(status,200,sent)
        self.delivery('grok-first')
        incoming=self.tool(token,'claim_message',message_id='grok-first');self.assertTrue(incoming['claimed'])
        self.tool(token,'acknowledge',message_id='grok-first')
        self.tool(token,'reply',message_id='grok-first',message='An MCP event wakes this existing agent.',reply_id='dot-answer')
        callback=urlsplit(self.delivery('dot-answer')['payload']['callback_url']).path
        self.request('POST',callback,{'action':'claim'});self.request('POST',callback,{'action':'acknowledge'})
        self.assertEqual(self.tool(token,'message_status',message_id='dot-answer')['state'],'read')

    def test_missing_subscription_waits_for_a_change_instead_of_polling(self):
        token=self.connect_dot()
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Wait for a receiver','scope':'Fixture','message_id':'wait-sub'}},self.grok);self.assertEqual(status,200)
        alarm=self.request('GET','/test/alarm')[2]['alarm']
        self.assertGreater(alarm,time.time()*1000+60000)
        self.subscribe(token);self.delivery('wait-sub')
        self.assertTrue(self.tool(token,'claim_message',message_id='wait-sub')['claimed'])
        self.assertFalse(self.tool(token,'claim_message',message_id='wait-sub')['claimed'])

    def test_saved_message_and_oauth_grant_survive_runtime_restart(self):
        token=self.connect_dot()
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Survive a cold restart','scope':'Fixture','message_id':'after-restart'}},self.grok)
        self.assertEqual(status,200)
        self.restart_runtime()
        self.assertEqual(self.tool(token,'message_status',message_id='after-restart')['body'],'Survive a cold restart')
        self.subscribe(token);self.delivery('after-restart')
        self.assertTrue(self.tool(token,'claim_message',message_id='after-restart')['claimed'])

    def restart_runtime(self):
        cls=type(self)
        cls.process.terminate();cls.process.wait(timeout=10)
        env={k:v for k,v in os.environ.items() if not k.startswith('CLOUDFLARE_')}
        env.update(WRANGLER_SEND_METRICS='false',XDG_CONFIG_HOME=str(cls.path/'tool-config'),CI='true')
        cls.process=subprocess.Popen([WRANGLER,'dev','--local','--ip','127.0.0.1','--port',str(cls.port),'--inspector-port','0','--log-level','error','--persist-to',str(cls.path/'state')],cwd=cls.path,env=env,stdout=cls.log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+20
        while True:
            try:
                status,_,_=self.request('GET','/setup')
                if status==200:break
            except OSError:
                pass
            self.assertLess(time.monotonic(),deadline)
            time.sleep(.1)

    def test_interrupted_grok_submission_is_unknown_and_is_not_replayed(self):
        token=self.connect_dot()
        # The native fixture leaves dispatching rows, then a full runtime restart
        # exercises the production recovery logic against persistent DO storage.
        self.tool(token,'send_message',to='Grokbot',message='Check uncertain native acceptance',scope='Fixture',message_id='uncertain-grok')
        self.delivery('uncertain-grok')
        # Mark the already-submitted request at the exact lost-response boundary.
        # This avoids a real external request or a timing-sensitive process kill.
        self.request('POST','/test/interrupted',{})
        self.restart_runtime()
        saved=self.tool(token,'message_status',message_id='uncertain-grok')
        self.assertEqual(saved['state'],'unknown')
        before=len(self.request('GET','/test/deliveries')[2])
        self.assertIsNone(self.request('GET','/test/alarm')[2]['alarm'])
        self.assertEqual(len(self.request('GET','/test/deliveries')[2]),before)

    def test_oversized_body_is_rejected_before_tool_execution(self):
        status,_,_=self.request('POST','/v1/call','x'*262145,self.grok,{'Content-Type':'application/json'})
        self.assertEqual(status,413)

    def test_native_transaction_rolls_back_an_interrupted_write(self):
        self.assertTrue(self.request('POST','/test/rollback',{})[2]['rolled_back'])

    def test_owner_setup_cannot_be_changed_by_peer_or_foreign_origin(self):
        args={**self.setup_args,'owner_key':self.grok}
        status,_,_=self.request('POST','/setup',urlencode(args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)});self.assertEqual(status,400)
        status,_,_=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'https://outside.example'});self.assertEqual(status,400)
        self.assertEqual(self.request('POST','/mcp/dot',{'id':1,'method':'tools/list'},self.grok)[0],401)

    def test_owner_update_preserves_the_peers_key_and_saved_mailbox(self):
        status,_,_=self.request('POST','/v1/call',{'tool':'send_message','arguments':{'to':'Dot','message':'Keep this message','scope':'Fixture','message_id':'preserved'}},self.grok);self.assertEqual(status,200)
        status,_,page=self.request('POST','/setup',urlencode(self.setup_args),headers={'Content-Type':'application/x-www-form-urlencoded','Origin':'http://127.0.0.1:'+str(self.port)});self.assertEqual(status,200)
        self.assertEqual(re.search('<pre>([^<]+)</pre>',page)[1],self.grok)
        status,_,saved=self.request('POST','/v1/call',{'tool':'message_status','arguments':{'message_id':'preserved'}},self.grok)
        self.assertEqual(status,200);self.assertEqual(saved['body'],'Keep this message')
