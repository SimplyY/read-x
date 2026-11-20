import copy
from pathlib import Path
import tempfile
import unittest

from archive_reading_context import FIELDS, archive, load_journal, save_journal
from reading_article_identity import article_key


class FakeBase:
    def __init__(self):
        self.records = []
        self.files = {}
        self.creates = 0
        self.uploads = 0
        self.lose_create_reply = False
        self.discard_create = False
        self.lose_upload_reply = False
        self.discard_upload = False

    def find(self, key):
        return copy.deepcopy([r for r in self.records if r['fields']['文章键'] == key])

    def find_source(self, url):
        return copy.deepcopy([r for r in self.records if r['fields'].get('原文链接') == url])

    def create(self, key, title):
        self.creates += 1
        if not self.discard_create:
            self.records.append({'id': 'rec1', 'fields': {'文章键': key, '标题': title, '状态': '已读'}})
        if self.lose_create_reply:
            raise RuntimeError('unknown')

    def upload(self, record_id, field, content):
        self.uploads += 1
        if not self.discard_upload:
            token = f'file{self.uploads}'
            self.files[token] = content
            self.records[0]['fields'][field] = [{'file_token': token}]
        if self.lose_upload_reply:
            raise RuntimeError('unknown')

    def download(self, record_id, token):
        return self.files[token]

    def set_manifest(self, record_id, manifest, fields=None):
        self.records[0]['fields']['校验清单'] = manifest
        self.records[0]['fields'].update(fields or {})


def job():
    return {'job_id': 'job1', 'app_id': 'app1', 'message_id': 'om_1', 'snapshot': 'abc',
            'title': 'Title', 'files': {name: name.encode() for name in FIELDS}}


class ArchiveTests(unittest.TestCase):
    def test_two_articles_in_one_message_have_independent_archives(self):
        class MultiBase(FakeBase):
            def create(self,key,title):
                self.creates+=1
                self.records.append({'id':f'rec{self.creates}','fields':{'文章键':key,'标题':title}})
            def upload(self,record_id,field,content):
                self.uploads+=1; token=f'file{self.uploads}'; self.files[token]=content
                next(r for r in self.records if r['id']==record_id)['fields'][field]=[{'file_token':token}]
            def set_manifest(self,record_id,manifest,fields=None):
                target=next(r for r in self.records if r['id']==record_id)['fields']
                target['校验清单']=manifest; target.update(fields or {})
        base,journal=MultiBase(),{'jobs':{}}
        for n in (1,2):
            url=f'https://example.com/{n}'
            item={**job(),'job_id':f'job{n}','source_url':url,'article_key':article_key(url,multi=True)}
            self.assertEqual(archive(item,base,journal,lambda:None)['status'],'verified')
        self.assertEqual(base.creates,2)
        self.assertEqual(base.uploads,4)
        self.assertNotEqual(base.records[0]['fields']['文章键'],base.records[1]['fields']['文章键'])

    def test_readback_idempotent_and_preserves_user_state(self):
        base, journal = FakeBase(), {'jobs': {}}
        first = archive(job(), base, journal, lambda: None)
        second = archive(job(), base, journal, lambda: None)
        self.assertEqual(first['status'], 'verified')
        self.assertEqual(second['status'], 'verified')
        self.assertEqual((base.creates, base.uploads), (1, 2))
        self.assertEqual(base.records[0]['fields']['状态'], '已读')

    def test_unknown_create_and_upload_reconcile_existing_remote(self):
        base, journal = FakeBase(), {'jobs': {}}
        base.lose_create_reply = base.lose_upload_reply = True
        result = archive(job(), base, journal, lambda: None)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual((base.creates, base.uploads), (1, 2))

    def test_unknown_upload_without_remote_never_retries(self):
        base, journal = FakeBase(), {'jobs': {}}
        base.lose_upload_reply = base.discard_upload = True
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            archive(job(), base, journal, lambda: None)
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            archive(job(), base, journal, lambda: None)
        self.assertEqual(base.uploads, 1)

    def test_unknown_create_without_remote_never_retries(self):
        base, journal = FakeBase(), {'jobs': {}}
        base.lose_create_reply = base.discard_create = True
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                archive(job(), base, journal, lambda: None)
        self.assertEqual(base.creates, 1)

    def test_changed_manifest_is_not_overwritten(self):
        base, journal = FakeBase(), {'jobs': {}}
        archive(job(), base, journal, lambda: None)
        base.records[0]['fields']['校验清单'] = 'external change'
        with self.assertRaisesRegex(ValueError, 'manifest conflicts'):
            archive(job(), base, journal, lambda: None)
        self.assertEqual(base.records[0]['fields']['校验清单'], 'external change')

    def test_changed_remote_content_is_rejected(self):
        base, journal = FakeBase(), {'jobs': {}}
        archive(job(), base, journal, lambda: None)
        base.files['file1'] = b'wrong'
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            archive(job(), base, journal, lambda: None)
        self.assertEqual(journal['jobs']['job1']['status'], 'verifying')

    def test_duplicate_identity_is_rejected(self):
        base, journal = FakeBase(), {'jobs': {}}
        base.create('context:app1:om_1', 'a')
        base.create('context:app1:om_1', 'b')
        with self.assertRaisesRegex(ValueError, 'multiple'):
            archive(job(), base, journal, lambda: None)

    def test_reuses_existing_intake_record_for_same_source(self):
        base, journal = FakeBase(), {'jobs': {}}
        base.records.append({'id':'rec1','fields':{'文章键':'url:existing','标题':'Existing',
                                                  '原文链接':'https://example.com/a','状态':'已读'}})
        source = {**job(), 'source_url':'https://example.com/a'}
        result = archive(source, base, journal, lambda: None)
        self.assertEqual(result['record_id'], 'rec1')
        self.assertEqual(base.creates, 0)
        self.assertEqual(base.records[0]['fields']['状态'], '已读')
        self.assertEqual(__import__('json').loads(base.records[0]['fields']['校验清单'])['source_key'], 'url:existing')

    def test_pending_attempt_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            base, journal = FakeBase(), {'schema': 1, 'jobs': {}}
            base.lose_upload_reply = base.discard_upload = True
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                archive(job(), base, journal, lambda: save_journal(path, journal))
            recovered = load_journal(path)
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                archive(job(), base, recovered, lambda: save_journal(path, recovered))
            self.assertEqual(base.uploads, 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
