import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reading_score_receipt as score
from reading_score_schema_migration import migrate_empty_legacy_v2
from reading_article_identity import article_key, score_request_id
from reading_dispatch_guard import claim_child
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

    def claim_children(self,event):
        db=sqlite3.connect(self.root/'state/long-read/workflow.sqlite3')
        db.execute('CREATE TABLE jobs(job_id TEXT, app_id TEXT, source_message_id TEXT,article_key TEXT,payload_json TEXT)')
        db.close()
        for url in event['urls']:
            self.assertEqual(claim_child(self.state,self.root/'state/long-read/workflow.sqlite3',event['messageId'],
                                         article_key(url,multi=True),'test'),'claimed')

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

    def test_blank_author_is_omitted_and_does_not_block_readback(self):
        FakeBase.record = {'id':'rec1','fields':{'标题':'https://example.com/a','阅读状态':'待读'}}
        score.record_receipt(state=self.state,message_id='om_message',url='https://example.com/a',
            title='标题',author='',result={'score_status':'scored','quality_score':6.5,'decision_score':5.8,
                'reading_category':'AI/技术','reading_category_confidence':'high'})
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))
        self.assertNotIn('作者',FakeBase.record['fields'])
        self.assertEqual(FakeBase.record['fields']['标题'],'标题')
        self.assertEqual(FakeBase.record['fields']['质量分'],6.5)

    def test_url_title_is_replaced_by_pending_marker_until_a_real_title_arrives(self):
        url='https://example.com/a'
        FakeBase.record={'id':'rec1','fields':{'标题':url,'阅读状态':'待读'}}
        score.record_receipt(state=self.state,message_id='om_message',url=url,title=url,author='',
            result={'score_status':'needs_review'})
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))
        self.assertEqual(FakeBase.record['fields']['标题'],'标题待补采')

    def test_score_sync_does_not_overwrite_an_established_manual_title(self):
        FakeBase.record={'id':'rec1','fields':{'标题':'我手动整理的标题','阅读状态':'待读'}}
        score.record_receipt(state=self.state,message_id='om_message',url='https://example.com/a',
            title='自动抓取的新标题',author='',result={'score_status':'needs_review'})
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',FakeBase):
            self.assertEqual(score.sync_receipts(self.state),(1,0))
        self.assertEqual(FakeBase.record['fields']['标题'],'我手动整理的标题')

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

    def test_multi_article_intents_do_not_replay_unknown_sends(self):
        event={**self.event,'messageId':'om_multi','urls':['https://example.com/c','https://example.com/d']}
        register_event(self.state,event,allowed_app_id=score.APP,allowed_chat_id='oc_room',allowed_sender_id='ou_owner',live_dispatch=True)
        self.claim_children(event)
        keys=[article_key(url,multi=True) for url in event['urls']]
        for url,key in zip(event['urls'],keys):
            request=score_request_id('om_multi',key)
            kwargs={'state':self.state,'message_id':'om_multi','url':url,'article_key':key,
                    'scoring_sha':'a'*64,'request_id':request,'card_sha':'b'*64,
                    'target_type':'user_id','target_id':'ou_owner'}
            with self.assertRaisesRegex(ValueError,'trusted source'):
                score.begin_score_send(**{**kwargs,'target_id':'ou_other'})
            score.begin_score_send(**kwargs)
            with self.assertRaisesRegex(ValueError,'already attempted'):
                score.begin_score_send(**kwargs)
        proof={'sent_at_command':True,'message_id':'om_card','idempotency_key':score_request_id('om_multi',keys[0]),
               'scoring_result_sha256':'a'*64,'card_sha256':'b'*64,'user_id':'ou_owner'}
        score.confirm_score_send(state=self.state,message_id='om_multi',article_key=keys[0],evidence=proof)
        with self.assertRaisesRegex(ValueError,'already attempted'):
            score.begin_score_send(**{**kwargs,'url':event['urls'][0],'article_key':keys[0],
                                      'request_id':score_request_id('om_multi',keys[0])})

    def test_legacy_empty_effect_schema_fails_closed_without_changing_rows(self):
        event={**self.event,'messageId':'om_multi','urls':['https://example.com/c','https://example.com/d']}
        register_event(self.state,event,allowed_app_id=score.APP,allowed_chat_id='oc_room',allowed_sender_id='ou_owner',live_dispatch=True)
        self.claim_children(event)
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute('''CREATE TABLE score_send_effects_v2(app_id TEXT,message_id TEXT,article_key TEXT,
                   request_id TEXT,scoring_sha TEXT,status TEXT,PRIMARY KEY(app_id,message_id,article_key))''')
        db.commit(); db.close()
        key=article_key(event['urls'][0],multi=True)
        with self.assertRaisesRegex(ValueError,'offline score effect schema migration required'):
            score.begin_score_send(state=self.state,message_id='om_multi',url=event['urls'][0],article_key=key,
                scoring_sha='a'*64,request_id=score_request_id('om_multi',key),card_sha='b'*64,
                target_type='user_id',target_id='ou_owner')
        db=sqlite3.connect(self.state/'intake.sqlite3')
        migrate_empty_legacy_v2(db); db.close()
        score.begin_score_send(state=self.state,message_id='om_multi',url=event['urls'][0],article_key=key,
            scoring_sha='a'*64,request_id=score_request_id('om_multi',key),card_sha='b'*64,
            target_type='user_id',target_id='ou_owner')

    def test_nonempty_effect_schema_is_never_migrated_automatically(self):
        db=sqlite3.connect(':memory:')
        db.execute('''CREATE TABLE score_send_effects_v2(app_id TEXT,message_id TEXT,article_key TEXT,
                   request_id TEXT,scoring_sha TEXT,status TEXT,PRIMARY KEY(app_id,message_id,article_key))''')
        db.execute("INSERT INTO score_send_effects_v2 VALUES ('a','m','k','r','s','attempting')")
        db.commit()
        with self.assertRaisesRegex(ValueError,'nonempty'): migrate_empty_legacy_v2(db)
        self.assertNotIn('card_sha',{r[1] for r in db.execute('PRAGMA table_info(score_send_effects_v2)')})
        self.assertEqual(db.execute('SELECT count(*) FROM score_send_effects_v2').fetchone()[0],1)
        db.close()

    def test_multi_scores_update_only_their_own_records(self):
        event={**self.event,'messageId':'om_multi','urls':['https://example.com/a','https://example.com/b']}
        register_event(self.state,event,allowed_app_id=score.APP,allowed_chat_id='oc_room',allowed_sender_id='ou_owner',live_dispatch=True)
        class MultiBase:
            records={url:{'id':f'rec{i}','fields':{'标题':url,'阅读状态':'已读'}} for i,url in enumerate(event['urls'])}
            def __init__(self,*args): pass
            def verify_identity(self,app): pass
            def find_source(self,url): return [self.records[url]]
            def call(self,command,*args):
                record_id=args[1]; fields=json.loads(args[3])
                next(r for r in self.records.values() if r['id']==record_id)['fields'].update(fields)
        for index,url in enumerate(event['urls']):
            score.record_receipt(state=self.state,message_id='om_multi',url=url,title=f'title{index}',author='',
                result={'score_status':'scored','quality_score':7+index,'decision_score':8},article_key=article_key(url,multi=True))
        with patch.object(score,'ROOT',self.root), patch.object(score,'LarkBase',MultiBase):
            self.assertEqual(score.sync_receipts(self.state),(2,0))
        self.assertEqual([MultiBase.records[u]['fields']['质量分'] for u in event['urls']],[7,8])
        self.assertTrue(all(MultiBase.records[u]['fields']['阅读状态']=='已读' for u in event['urls']))

if __name__ == '__main__': unittest.main()
