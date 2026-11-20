from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from reading_intake import register_event
from reading_article_identity import article_key
from reading_dispatch_guard import APP, admit_children, claim_child, finish_child, reconcile, reconcile_child, queued_children


class ChildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        self.state=self.root/'reading-workbench'; self.wf=self.root/'workflow.sqlite3'
        db=sqlite3.connect(self.wf)
        db.execute('CREATE TABLE jobs(job_id TEXT, app_id TEXT, source_message_id TEXT,article_key TEXT,payload_json TEXT)')
        db.close()
        self.urls=['https://example.com/a','https://example.com/b']
        self.event={'appId':APP,'chatId':'oc_group','chatType':'group','senderId':'ou_self',
                    'senderType':'user','messageId':'om_message','urls':self.urls}
        register_event(self.state,self.event,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
        self.keys=[article_key(url,multi=True) for url in self.urls]
    def tearDown(self): self.tmp.cleanup()

    def test_concurrent_claims_are_single_attempt_per_child(self):
        admit_children(self.state,'om_message',self.urls,chat_id='oc_group',sender_id='ou_self')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda owner:claim_child(self.state,self.wf,'om_message',self.keys[0],owner),['a','b']))
        self.assertCountEqual(results,['claimed','needs_review'])
        self.assertEqual(claim_child(self.state,self.wf,'om_message',self.keys[1],'b'),'claimed')
        self.assertEqual(finish_child(self.state,'om_message',self.keys[0],'needs_review'),'needs_review')
        self.assertEqual(claim_child(self.state,self.wf,'om_message',self.keys[0],'c'),'needs_review')

    def test_legacy_job_for_message_blocks_new_child(self):
        admit_children(self.state,'om_message',self.urls,chat_id='oc_group',sender_id='ou_self')
        db=sqlite3.connect(self.wf)
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',('legacy',APP,'om_message','','{}')); db.commit(); db.close()
        self.assertEqual(claim_child(self.state,self.wf,'om_message',self.keys[0],'a'),'needs_review')

    def test_no_completion_without_both_score_and_archive(self):
        admit_children(self.state,'om_message',self.urls,chat_id='oc_group',sender_id='ou_self')
        self.assertEqual(claim_child(self.state,self.wf,'om_message',self.keys[0],'a'),'claimed')
        journal=self.root/'journal.json'; journal.write_text(json.dumps({'schema':1,'jobs':{}}))
        self.assertEqual(reconcile_child(self.state,self.wf,journal,'om_message',self.keys[0]),'needs_review')

    def test_worker_restart_recovers_only_verified_child(self):
        admit_children(self.state,'om_message',self.urls,chat_id='oc_group',sender_id='ou_self')
        for key in self.keys: self.assertEqual(claim_child(self.state,self.wf,'om_message',key,'worker'),'claimed')
        payload={'article_key':self.keys[0],'context_packs':[1,2],'snapshot_sha256':'a'*64}
        db=sqlite3.connect(self.wf)
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',('job1',APP,'om_message',self.keys[0],json.dumps(payload)))
        db.commit(); db.close()
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute('''CREATE TABLE score_receipts_v2(app_id TEXT,message_id TEXT,article_key TEXT,
            url TEXT,metadata TEXT,state TEXT,PRIMARY KEY(app_id,message_id,article_key))''')
        db.execute('INSERT INTO score_receipts_v2 VALUES (?,?,?,?,?,?)',
                   (APP,'om_message',self.keys[0],self.urls[0],json.dumps({'评分状态':'已评分'}),'synced'))
        db.commit(); db.close()
        pack={'file_token':'f','sha256':'b'*64,'bytes':3}
        journal=self.root/'journal.json'
        journal.write_text(json.dumps({'schema':1,'jobs':{'job1':{'status':'verified','snapshot':'a'*64,
                           'verified':{'深层解码':pack,'芒格洞察':pack}}}}))
        self.assertEqual(reconcile_child(self.state,self.wf,journal,'om_message',self.keys[0]),'completed')
        self.assertEqual(reconcile_child(self.state,self.wf,journal,'om_message',self.keys[1]),'needs_review')

    def test_parent_reconciles_only_after_every_child_is_verified(self):
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("INSERT INTO reading_dispatch(app_id,message_id,phase) VALUES (?,?,?)",(APP,'om_message','needs_review'))
        db.execute("UPDATE reading_dispatch_children SET phase='completed'")
        db.commit(); db.close()
        journal=self.root/'journal.json'; journal.write_text(json.dumps({'schema':1,'jobs':{}}))
        self.assertEqual(reconcile(self.state,self.wf,journal,'om_message'),'completed')
        db=sqlite3.connect(self.state/'intake.sqlite3')
        self.assertEqual(db.execute("SELECT phase FROM reading_dispatch WHERE message_id='om_message'").fetchone()[0],'completed')
        db.execute("UPDATE reading_dispatch_children SET phase='needs_review' WHERE article_key=?",(self.keys[1],))
        db.execute("UPDATE reading_dispatch SET phase='needs_review' WHERE message_id='om_message'")
        db.commit(); db.close()
        self.assertEqual(reconcile(self.state,self.wf,journal,'om_message'),'needs_review')

    def test_live_cli_claims_two_articles_and_never_claims_them_twice(self):
        long_read=self.root/'long-read'; long_read.mkdir()
        db=sqlite3.connect(long_read/'workflow.sqlite3')
        db.execute('CREATE TABLE jobs(job_id TEXT, app_id TEXT, source_message_id TEXT,article_key TEXT,payload_json TEXT)')
        db.close()
        import reading_intake
        cmd=[sys.executable,reading_intake.__file__,'--state-dir',str(self.state),'--live',
             '--allowed-app-id',APP,'--allowed-chat-id','oc_group','--allowed-sender-id','ou_self']
        kwargs={'input':json.dumps(self.event),'text':True,'capture_output':True,'check':True}
        first=json.loads(subprocess.run(cmd,**kwargs).stdout)
        self.assertEqual(first['dispatch'],'multi')
        self.assertEqual([x['dispatch'] for x in first['children']],['queued','queued'])
        second=json.loads(subprocess.run(cmd,**kwargs).stdout)
        self.assertEqual([x['dispatch'] for x in second['children']],['queued','queued'])
        queue=queued_children(self.state,chat_id='oc_group',sender_id='ou_self')
        self.assertEqual([x['url'] for x in queue],self.urls)
        claim=[sys.executable,reading_intake.__file__,'--state-dir',str(self.state),'--claim-child',
               '--message-id','om_message','--article-key',self.keys[0],
               '--allowed-app-id',APP,'--allowed-chat-id','oc_group','--allowed-sender-id','ou_self']
        self.assertEqual(json.loads(subprocess.run(claim,text=True,capture_output=True,check=True).stdout)['status'],'claimed')
        self.assertEqual([x['url'] for x in queued_children(self.state,chat_id='oc_group',sender_id='ou_self')],[self.urls[1]])
        self.assertEqual(json.loads(subprocess.run(claim,text=True,capture_output=True,check=True).stdout)['status'],'needs_review')

    def test_intake_and_queued_children_are_one_transaction(self):
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute('DROP TABLE reading_dispatch_children')
        db.execute('CREATE TABLE reading_dispatch_children(incompatible TEXT)')
        db.commit(); db.close()
        new={**self.event,'messageId':'om_new','urls':['https://example.com/c','https://example.com/d']}
        with self.assertRaises(sqlite3.Error):
            register_event(self.state,new,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
        db=sqlite3.connect(self.state/'intake.sqlite3')
        self.assertEqual(db.execute("SELECT count(*) FROM intake_messages WHERE message_id='om_new'").fetchone()[0],0)
        self.assertEqual(db.execute("SELECT count(*) FROM intake_articles WHERE canonical_url='https://example.com/c'").fetchone()[0],0)
        db.close()

    def test_backfill_before_cutover_never_auto_dispatches(self):
        old={**self.event,'messageId':'om_old','urls':['https://example.com/c','https://example.com/d'],
             'sourceCreatedAt':'1000000000000'}
        register_event(self.state,old,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self')
        self.assertEqual(claim_child(self.state,self.wf,'om_old',article_key(old['urls'][0],multi=True),'test'),'needs_review')
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute('INSERT INTO reading_dispatch_cutover VALUES (1,?)',(1500000000000,)); db.commit(); db.close()
        new={**old,'messageId':'om_new','urls':['https://example.com/e','https://example.com/f'],'sourceCreatedAt':'1600000000000'}
        register_event(self.state,new,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self')
        self.assertEqual(claim_child(self.state,self.wf,'om_new',article_key(new['urls'][0],multi=True),'test'),'claimed')

if __name__ == '__main__': unittest.main()
