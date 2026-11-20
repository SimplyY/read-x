import json
from pathlib import Path
import unittest
from unittest.mock import patch
from test_long_read_context_packs import ContextTaskTests
from reading_intake import register_event
from reading_dispatch_guard import APP, claim_child
from reading_article_identity import article_key
from long_read_workflow_store import WorkflowStore


class MultiTests(ContextTaskTests):
    def test_distinct_jobs_with_real_message_identity(self):
        self.state = self.state.resolve()
        intake=self.state.parent/'reading-workbench'
        urls=['https://example.test/source','https://example.test/other']
        event={'appId':APP,'chatId':'oc_group','chatType':'group','senderId':'ou_self',
               'senderType':'user','messageId':'om_source','urls':urls}
        register_event(intake,event,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
        with WorkflowStore(self.state): pass
        for url in urls:
            self.assertEqual(claim_child(intake,self.state/'workflow.sqlite3','om_source',article_key(url,multi=True),'test'),'claimed')
        self.context.write_text(json.dumps({**self.trigger,'appId':APP,'chatType':'group','chatId':'oc_group'}))
        with patch('long_read_workflow_notifications.resolve_delivery_target',return_value=({'user_id':'ou_self'},'oc_private')):
            first=self.submit(article_url=urls[0],app_id=APP)
            evidence=json.loads(self.evidence.read_text())
            evidence['metadata']['source_url']=urls[1]
            self.evidence.write_text(json.dumps(evidence,ensure_ascii=False))
            second=self.submit(article_url=urls[1],app_id=APP)
            self.assertNotEqual(first['job_id'],second['job_id'])
            third=self.submit(article_url=urls[1],app_id=APP)
            self.assertFalse(third['created'])
            self.assertEqual(third['job_id'],second['job_id'])
            with self.assertRaises(ValueError): self.submit(article_url='https://not-sent.test/',app_id=APP)
            self.context.write_text(json.dumps({**self.trigger,'appId':APP,'chatType':'group',
                                                'chatId':'oc_group','senderId':'ou_attacker'}))
            with self.assertRaises(ValueError): self.submit(article_url=urls[1],app_id=APP)

if __name__ == '__main__': unittest.main()
