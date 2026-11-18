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
    def test_uncertain_create_does_not_replay(self):
        b,j=Base(),{'jobs':{'1':{'key':'url:'+__import__('hashlib').sha256(item['canonicalUrl'].encode()).hexdigest(),'url':item['canonicalUrl'],'create_attempted':True}}}
        with self.assertRaisesRegex(RuntimeError,'unresolved'):
            sync_one(item,b,j,lambda:None)
        self.assertEqual(b.creates,0)
if __name__ == '__main__': unittest.main()
