import unittest
import uuid
import tempfile
from pathlib import Path
from reading_article_identity import article_key, context_task_id, score_request_id, identity_from_trusted_intake
from reading_intake import register_event


class IdentityTests(unittest.TestCase):
    def test_legacy_identity_is_unchanged(self):
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, 'read-x:context:cli_app:om_message'))
        self.assertEqual(article_key('https://example.com/a', multi=False), '')
        self.assertEqual(context_task_id('cli_app', 'om_message'), expected)
        self.assertEqual(score_request_id('om_message'), 'readx-score-om_message')

    def test_children_are_stable_and_distinct(self):
        a = article_key('https://EXAMPLE.com/a#part', multi=True)
        self.assertEqual(a, article_key('https://example.com/a', multi=True))
        b = article_key('https://example.com/b', multi=True)
        self.assertNotEqual(context_task_id('cli_app', 'om_message', a), context_task_id('cli_app', 'om_message', b))
        self.assertNotEqual(score_request_id('om_message', a), score_request_id('om_message', b))
        self.assertLessEqual(len(score_request_id('om_message', a)), 50)

    def test_rejects_bad_identities(self):
        with self.assertRaises(ValueError): context_task_id('cli_app', 'om_bad', 'not-a-hash')
        with self.assertRaises(ValueError): score_request_id('om_bad', 'fake')

    def test_child_must_belong_to_trusted_original_message(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory).resolve()
            event={'appId':'cli_app','chatId':'oc_chat','senderId':'ou_owner','senderType':'user',
                   'chatType':'group','messageId':'om_original','urls':['https://example.com/a','https://example.com/b']}
            register_event(state,event,allowed_app_id='cli_app',allowed_chat_id='oc_chat',allowed_sender_id='ou_owner')
            self.assertEqual(identity_from_trusted_intake(state,'cli_app','om_original','https://example.com/a'),
                             article_key('https://example.com/a',multi=True))
            with self.assertRaisesRegex(ValueError,'does not belong'):
                identity_from_trusted_intake(state,'cli_app','om_original','https://attacker.example/')

if __name__ == '__main__': unittest.main()
