import unittest
from backfill_reading_messages import scan, urls_for

class RecoveryTests(unittest.TestCase):
    def test_excludes_bots_and_other_users_and_extracts_message_urls(self):
        rows = [
            {'sender':{'sender_type':'user','id_type':'open_id','id':'ou_owner'},'msg_type':'text','message_id':'om_one','content':'文章 https://example.com/a?x=1。'},
            {'sender':{'sender_type':'app','id_type':'open_id','id':'ou_owner'},'msg_type':'text','message_id':'om_bot','content':'https://example.com/b'},
            {'sender':{'sender_type':'user','id_type':'open_id','id':'ou_other'},'msg_type':'text','message_id':'om_other','content':'https://example.com/c'},
        ]
        seen=[]
        self.assertEqual(scan(rows,'ou_owner',lambda event:(seen.append(event) or {'articles':1})),1)
        self.assertEqual(seen[0]['urls'],['https://example.com/a?x=1'])
        self.assertEqual(seen[0]['messageId'],'om_one')
    def test_malformed_content_fails_closed(self):
        rows=[{'sender':{'sender_type':'user','id_type':'open_id','id':'ou_owner'},'msg_type':'text','content':None}]
        with self.assertRaises(ValueError): scan(rows,'ou_owner',lambda event:None)

if __name__ == '__main__': unittest.main()
