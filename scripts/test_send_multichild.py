import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import reading_article_identity as identity
import reading_score_receipt as receipt
import send_score_card as sender
from reading_intake import register_event
from reading_dispatch_guard import claim_child


class SendTests(unittest.TestCase):
    def test_each_article_has_durable_send_intent_and_unknown_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); state=root/'private'; urls=['https://example.com/a','https://example.com/b']
            event={'appId':receipt.APP,'chatId':'oc_group','chatType':'group','senderId':'ou_self',
                   'senderType':'user','messageId':'om_multi','urls':urls}
            register_event(state,event,allowed_app_id=receipt.APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
            workflow=root/'workflow.sqlite3'
            db=sqlite3.connect(workflow)
            db.execute('CREATE TABLE jobs(job_id TEXT, app_id TEXT, source_message_id TEXT,article_key TEXT,payload_json TEXT)')
            db.close()

            for url in urls:
                self.assertEqual(claim_child(state,workflow,'om_multi',identity.article_key(url,multi=True),'test'),'claimed')
            scoring=root/'score.json'; scoring.write_text(json.dumps({'score_status':'scored','quality_score':7,'decision_score':8}))
            actual_prepare=receipt.prepare_recovery; actual_begin=receipt.begin_score_send
            actual_confirm=receipt.confirm_score_send; actual_record=receipt.record_receipt
            sent=[]
            def send(args,card):
                sent.append(args.url)
                return {'schema_version':'1','message_id':'om_card','scoring_result_sha256':hashlib.sha256(args.scoring_result.read_bytes()).hexdigest(),
                    'chat_id':None,'user_id':args.user_id,'idempotency_key':args.idempotency_key,
                    'card_sha256':hashlib.sha256(json.dumps(card,ensure_ascii=False).encode()).hexdigest(),'sent_at_command':True}
            with patch.object(receipt,'STATE',state), patch.object(sender,'render_card',return_value={'schema':'2.0'}), \
                 patch.object(sender,'_send',side_effect=send), \
                 patch.object(receipt,'prepare_recovery',side_effect=lambda **kw:actual_prepare(state=state,**kw)), \
                 patch.object(receipt,'begin_score_send',side_effect=lambda **kw:actual_begin(state=state,**kw)), \
                 patch.object(receipt,'confirm_score_send',side_effect=lambda **kw:actual_confirm(state=state,**kw)), \
                 patch.object(receipt,'record_receipt',side_effect=lambda **kw:actual_record(state=state,**kw)):
                for i,url in enumerate(urls):
                    key=identity.article_key(url,multi=True)
                    argv=['send_score_card.py',str(scoring),'--title','x','--url',url,'--source-message-id','om_multi',
                          '--idempotency-key',identity.score_request_id('om_multi',key),'--user-id','ou_self',
                          '--evidence-output',str(root/f'gate{i}.json')]
                    with patch.object(sys,'argv',argv): self.assertEqual(sender.main(),None)
                with patch.object(sys,'argv',argv): self.assertEqual(sender.main(),1)
            self.assertEqual(sent,urls)
            db=sqlite3.connect(state/'intake.sqlite3')
            self.assertEqual(db.execute("SELECT count(*) FROM score_send_effects_v2 WHERE status='confirmed'").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM score_receipts_v2').fetchone()[0],2)
            db.close()

    def test_unknown_send_is_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); state=root/'private'; urls=['https://example.com/a','https://example.com/b']
            event={'appId':receipt.APP,'chatId':'oc_group','chatType':'group','senderId':'ou_self',
                   'senderType':'user','messageId':'om_multi','urls':urls}
            register_event(state,event,allowed_app_id=receipt.APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
            workflow=root/'workflow.sqlite3'; db=sqlite3.connect(workflow)
            db.execute('CREATE TABLE jobs(job_id TEXT, app_id TEXT, source_message_id TEXT,article_key TEXT,payload_json TEXT)'); db.close()
            key=identity.article_key(urls[0],multi=True)
            self.assertEqual(claim_child(state,workflow,'om_multi',key,'test'),'claimed')
            scoring=root/'score.json'; scoring.write_text(json.dumps({'score_status':'scored','quality_score':7,'decision_score':8}))
            argv=['send_score_card.py',str(scoring),'--title','x','--url',urls[0],'--source-message-id','om_multi',
                  '--idempotency-key',identity.score_request_id('om_multi',key),'--user-id','ou_self','--evidence-output',str(root/'gate.json')]
            actual_prepare=receipt.prepare_recovery; actual_begin=receipt.begin_score_send
            with patch.object(receipt,'STATE',state), patch.object(sender,'render_card',return_value={'schema':'2.0'}), \
                 patch.object(sender,'_send',side_effect=RuntimeError('unknown')) as send, \
                 patch.object(receipt,'prepare_recovery',side_effect=lambda **kw:actual_prepare(state=state,**kw)), \
                 patch.object(receipt,'begin_score_send',side_effect=lambda **kw:actual_begin(state=state,**kw)), \
                 patch.object(sys,'argv',argv):
                with self.assertRaisesRegex(RuntimeError,'unknown'): sender.main()
                self.assertEqual(sender.main(),1)
                self.assertEqual(send.call_count,1)
            db=sqlite3.connect(state/'intake.sqlite3')
            self.assertEqual(db.execute('SELECT status FROM score_send_effects_v2').fetchone()[0],'attempting')
            db.close()
            self.assertFalse((root/'gate.json').exists())

if __name__ == '__main__': unittest.main()
