import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reading_score_receipt as score
from reading_intake import register_event


class FakeBase:
    record = None
    def __init__(self,*args): pass
    def verify_identity(self,app): pass
    def find_source(self,url): return [self.record]
    def call(self,command,*args):
        values = json.loads(args[3])
        self.record['fields'].update(values)


class ScoreReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root/'state/long-read').mkdir(parents=True)
        self.state = self.root/'state/reading-workbench'
        self.event = {'appId':score.APP,'chatId':'oc_room','chatType':'group',
            'senderId':'ou_owner','senderType':'user','messageId':'om_message',
            'urls':['https://example.com/a']}
        register_event(self.state,self.event,allowed_app_id=score.APP,allowed_chat_id='oc_room',allowed_sender_id='ou_owner')
        FakeBase.record = {'id':'rec1','fields':{'标题':'https://example.com/a','阅读状态':'已读','人工主题':'历史与文明'}}

    def tearDown(self): self.tmp.cleanup()

    def test_scoring_is_idempotent_and_preserves_user_fields(self):
        result={'score_status':'scored','quality_score':7.2,'decision_score':8.2,
                'reading_category':'AI/技术','reading_category_confidence':'high'}
        kw={'state':self.state,'message_id':'om_message','url':'https://example.com/a',
            'title':'标题','author':'作者','result':result}
        score.record_receipt(**kw)
        score.record_receipt(**kw)
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))
            self.assertEqual(score.sync_receipts(self.state),(0,0))
        self.assertEqual(FakeBase.record['fields']['阅读状态'],'已读')
        self.assertEqual(FakeBase.record['fields']['人工主题'],'历史与文明')
        self.assertEqual(FakeBase.record['fields']['质量分'],7.2)
        self.assertEqual(FakeBase.record['fields']['自动主题'],'AI与技术')

    def test_invalid_result_does_not_create_receipt(self):
        with self.assertRaises(ValueError):
            score.record_receipt(state=self.state,message_id='om_message',url='https://example.com/a',
                title='x',author='',result={'score_status':'needs_relevance'})

    def test_recovery_requires_matching_delivery_gate(self):
        gate = self.root/'score-gate.json'
        result={'score_status':'scored','quality_score':7.0,'decision_score':8.0}
        score.prepare_recovery(state=self.state,message_id='om_message',url='https://example.com/a',
            title='标题',author='作者',result=result,gate_path=gate,scoring_sha='a'*64)
        self.assertEqual(score.recover_receipts(self.state),0)
        self.assertEqual(len(list((self.state/'score-recovery').glob('*.json'))),1)
        gate.write_text(json.dumps({'sent_at_command':True,'idempotency_key':'readx-score-om_message',
            'scoring_result_sha256':'a'*64,'message_id':'om_card'}))
        self.assertEqual(score.recover_receipts(self.state),0)
        self.assertEqual(len(list((self.state/'score-recovery').glob('*.json'))),0)
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))

    def test_unmatched_receipts_do_not_block_matched_article(self):
        result={'score_status':'needs_review'}
        for n in range(105):
            score.record_receipt(state=self.state,message_id=f'om_other{n}',url=f'https://example.com/{n}',
                                 title='x',author='',result=result)
        score.record_receipt(state=self.state,message_id='om_message',url='https://example.com/a',
                             title='标题',author='',result=result)
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))

if __name__ == '__main__': unittest.main()
