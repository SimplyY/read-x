from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from reading_intake import register_event
from reading_dispatch_guard import APP, admit, claim, reconcile
from reading_score_receipt import record_receipt


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root/'state'
        self.workflow = self.root/'workflow.sqlite3'
        db = sqlite3.connect(self.workflow)
        db.execute('CREATE TABLE jobs(app_id TEXT,source_message_id TEXT,job_id TEXT,payload_json TEXT)')
        db.close()
        event={'appId':APP,'chatId':'oc_room','chatType':'group','senderId':'ou_owner',
               'senderType':'user','messageId':'om_test','urls':['https://example.com/a']}
        register_event(self.state,event,allowed_app_id=APP,allowed_chat_id='oc_room',allowed_sender_id='ou_owner')

    def tearDown(self): self.tmp.cleanup()

    def test_legacy_intake_is_unmanaged(self):
        self.assertEqual(claim(self.state,self.workflow,'om_test','a'),'unmanaged')

    def test_only_one_claim_and_running_is_never_replayed(self):
        self.assertEqual(admit(self.state,'om_test',exclusive_intake=True),'queued')
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda owner: claim(self.state,self.workflow,'om_test',owner), ['a','b']))
        self.assertCountEqual(outcomes,['claimed','needs_review'])
        self.assertEqual(claim(self.state,self.workflow,'om_test','c'),'needs_review')

    def test_prior_workflow_refuses_replay(self):
        admit(self.state,'om_test',exclusive_intake=True)
        db=sqlite3.connect(self.workflow)
        db.execute('INSERT INTO jobs VALUES (?,?,?,?)',(APP,'om_test','job1','{}'))
        db.commit()
        db.close()
        self.assertEqual(claim(self.state,self.workflow,'om_test','a'),'needs_review')

    def test_missing_workflow_is_uncertain(self):
        admit(self.state,'om_test',exclusive_intake=True)
        self.assertEqual(claim(self.state,self.root/'missing.sqlite3','om_test','a'),'needs_review')

    def test_requires_exclusive_cutover_evidence(self):
        with self.assertRaisesRegex(ValueError,'exclusive'):
            admit(self.state,'om_test')

    def test_completion_requires_score_and_matching_verified_archive(self):
        admit(self.state,'om_test',exclusive_intake=True)
        self.assertEqual(claim(self.state,self.workflow,'om_test','owner'),'claimed')
        archive=self.root/'journal.json'
        pack={'file_token':'file1','sha256':'a'*64,'bytes':1}
        entry={'status':'verified','snapshot':'hash','verified':{'深层解码':pack,'芒格洞察':pack}}
        archive.write_text(json.dumps({'schema':1,'jobs':{'job1':entry}}))
        db=sqlite3.connect(self.workflow)
        db.execute('INSERT INTO jobs VALUES (?,?,?,?)',(APP,'om_test','job1',json.dumps({'context_packs':[1,2], 'snapshot_sha256':'hash'})))
        db.commit()
        db.close()
        self.assertEqual(reconcile(self.state,self.workflow,archive,'om_test'),'needs_review')
        record_receipt(state=self.state,message_id='om_test',url='https://example.com/a',
                       title='x',author='',result={'score_status':'scored','quality_score':7,'decision_score':8})
        db = sqlite3.connect(self.state/'intake.sqlite3')
        with db:
            db.execute("UPDATE score_receipts SET state='synced' WHERE app_id=? AND message_id=?",
                       (APP,'om_test'))
        self.assertEqual(reconcile(self.state,self.workflow,archive,'om_test'),'completed')

if __name__ == '__main__': unittest.main()
