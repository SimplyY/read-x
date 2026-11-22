import json
import unittest
from sync_reading_intake import sync_one

class Base:
    def __init__(self, records=None):
        self.records = records or []
        self.creates = 0
    def call(self, operation, *args):
        if operation == '+record-list':
            fields = ['文章键', '原文链接', '校验清单']
            return {'fields': fields, 'data': [[r['fields'].get(f) for f in fields] for r in self.records],
                    'record_id_list': [r['id'] for r in self.records], 'has_more': False}
        if operation == '+record-upsert':
            self.creates += 1
            if args[0] == '--record-id':
                record_id, value = args[1], json.loads(args[3])
                next(r for r in self.records if r['id'] == record_id)['fields'].update(value)
            else:
                value = json.loads(args[1])
                self.records.append({'id':'recnew','fields':value})
            return {}
        raise AssertionError(operation)
    def find(self, key):
        return [r for r in self.records if r['fields'].get('文章键') == key]

item = {'id':1, 'canonicalUrl':'https://example.com/a', 'originalUrl':'https://example.com/a'}
class SyncTests(unittest.TestCase):
    def test_reuses_historical_manifest_url(self):
        b = Base([{'id':'recold','fields':{'文章键':'context:old','校验清单':json.dumps({'source_url':'https://example.com/a'})}}])
        self.assertEqual(sync_one(item,b,{'jobs':{}},lambda:None),'recold')
        self.assertEqual(b.creates,0)
    def test_creates_once_then_reuses(self):
        b,j=Base(),{'jobs':{}}
        self.assertEqual(sync_one(item,b,j,lambda:None),'recnew')
        self.assertEqual(sync_one(item,b,j,lambda:None),'recnew')
        self.assertEqual(b.creates,1)
    def test_new_article_keeps_intake_time_and_source(self):
        b = Base()
        sync_one({**item, 'createdAt':'2026-11-19 01:30:00'},b,{'jobs':{}},lambda:None)
        self.assertEqual(b.records[0]['fields']['入库时间'],'2026-11-19 09:30')
        self.assertEqual(b.records[0]['fields']['来源'],'example.com')
        self.assertEqual(b.records[0]['fields']['标题'],'标题待补采')
    def test_repairs_only_url_placeholder_on_intake_owned_record(self):
        b = Base([{'id':'recold','fields':{'文章键':'url:'+__import__('hashlib').sha256(item['canonicalUrl'].encode()).hexdigest(),
                                           '原文链接':item['originalUrl'],'标题':item['originalUrl']}}])
        sync_one(item,b,{'jobs':{}},lambda:None)
        self.assertEqual(b.records[0]['fields']['标题'],'标题待补采')
    def test_preserves_established_human_title(self):
        b = Base([{'id':'recold','fields':{'文章键':'url:'+__import__('hashlib').sha256(item['canonicalUrl'].encode()).hexdigest(),
                                           '原文链接':item['originalUrl'],'标题':'我手动整理的标题'}}])
        sync_one(item,b,{'jobs':{}},lambda:None)
        self.assertEqual(b.records[0]['fields']['标题'],'我手动整理的标题')
    def test_preserves_a_different_user_url_in_title_field(self):
        b = Base([{'id':'recold','fields':{'文章键':'url:'+__import__('hashlib').sha256(item['canonicalUrl'].encode()).hexdigest(),
                                           '原文链接':item['originalUrl'],'标题':'https://example.com/other'}}])
        sync_one(item,b,{'jobs':{}},lambda:None)
        self.assertEqual(b.records[0]['fields']['标题'],'https://example.com/other')
    def test_uncertain_create_does_not_replay(self):
        b,j=Base(),{'jobs':{'1':{'key':'url:'+__import__('hashlib').sha256(item['canonicalUrl'].encode()).hexdigest(),'url':item['canonicalUrl'],'create_attempted':True}}}
        with self.assertRaisesRegex(RuntimeError,'unresolved'):
            sync_one(item,b,j,lambda:None)
        self.assertEqual(b.creates,0)
if __name__ == '__main__': unittest.main()
