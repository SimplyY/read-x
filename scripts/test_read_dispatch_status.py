from datetime import datetime,timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from reading_intake import register_event
from reading_dispatch_guard import APP
from read_dispatch_status import read_states


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.state=Path(self.tmp.name).resolve()
        self.event={'appId':APP,'chatId':'oc_group','chatType':'group','senderId':'ou_self',
                    'senderType':'user','messageId':'om_multi','urls':['https://example.com/a','https://example.com/b'],
                    'sourceCreatedAt':'1700000000000'}
        register_event(self.state,self.event,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
        self.now=datetime(2026,1,1,12,tzinfo=timezone.utc)
    def tearDown(self): self.tmp.cleanup()

    def test_child_statuses_and_timeout_are_visible(self):
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("UPDATE reading_dispatch_children SET phase='running',started_at='2026-01-01 11:00:00' WHERE canonical_url=?",(self.event['urls'][0],))
        db.execute("UPDATE reading_dispatch_children SET phase='completed' WHERE canonical_url=?",(self.event['urls'][1],))
        db.commit(); db.close()
        self.assertEqual(read_states(self.state,now=self.now),{self.event['urls'][0]:'needs_review',self.event['urls'][1]:'completed'})

    def test_multilink_parent_does_not_override_per_article_children(self):
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("UPDATE reading_dispatch_children SET phase='completed'")
        db.execute("INSERT INTO reading_dispatch(app_id,message_id,phase) VALUES (?,?,?)",(APP,'om_multi','needs_review'))
        db.commit(); db.close()
        self.assertEqual(read_states(self.state,now=self.now),
            {self.event['urls'][0]:'completed',self.event['urls'][1]:'completed'})

    def test_legacy_without_source_time_is_ambiguous(self):
        newer={**self.event,'messageId':'om_new','urls':[self.event['urls'][0]]}
        register_event(self.state,newer,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self')
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("INSERT INTO reading_dispatch(app_id,message_id,phase) VALUES (?,?,?)",(APP,'om_new','completed'))
        db.commit(); db.close()
        self.assertEqual(read_states(self.state,now=self.now)[self.event['urls'][0]],'needs_review')

    def test_original_event_time_wins_over_later_backfill_registration(self):
        older={**self.event,'messageId':'om_old','urls':[self.event['urls'][0],'https://example.com/c'],
               'sourceCreatedAt':'1600000000000'}
        register_event(self.state,older,allowed_app_id=APP,allowed_chat_id='oc_group',allowed_sender_id='ou_self',live_dispatch=True)
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("UPDATE reading_dispatch_children SET phase='completed' WHERE message_id='om_multi' AND canonical_url=?",(self.event['urls'][0],))
        db.execute("UPDATE reading_dispatch_children SET phase='failed' WHERE message_id='om_old' AND canonical_url=?",(self.event['urls'][0],))
        db.commit(); db.close()
        self.assertEqual(read_states(self.state,now=self.now)[self.event['urls'][0]],'completed')

    def test_queued_without_timestamp_never_claims_fresh_state(self):
        db=sqlite3.connect(self.state/'intake.sqlite3')
        db.execute("UPDATE reading_dispatch_children SET phase='running',started_at='invalid' WHERE canonical_url=?",(self.event['urls'][0],))
        db.commit(); db.close()
        self.assertEqual(read_states(self.state,now=self.now)[self.event['urls'][0]],'needs_review')

if __name__ == '__main__': unittest.main()
