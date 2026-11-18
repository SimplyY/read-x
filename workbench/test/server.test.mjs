import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { buildApp } from '../server.mjs';

const key = 'x'.repeat(40);
const content = Buffer.from('完整内容');
const sha256 = createHash('sha256').update(content).digest('hex');
const record = {id: 'rec123', '标题': '文章', '文章键': 'context:one', '深层解码': [{file_token:'file1',size:content.length}], '芒格洞察': [{file_token:'file2',size:content.length}]};
record['校验清单'] = JSON.stringify({schema:'readx.context-archive/v1',source_key:'context:one',snapshot_sha256:'a'.repeat(64),source_url:'https://example.com/article',packs:{decode:{file_token:'file1',bytes:content.length,sha256},munger:{file_token:'file2',bytes:content.length,sha256}}});
function app(download = async () => content) { return buildApp({gatewayKey:key,adapter:{list:async()=>[record],get:async()=>record,download}}); }

test('requires gateway credential on both pages and APIs', async () => {
 const server = app();
 for (const url of ['/', '/api/articles', '/api/articles/rec123/packs/decode']) {
  const result = await server.inject({method:'GET',url});
  assert.equal(result.statusCode,403);
 }
 await server.close();
});
test('returns only validated content and refuses changed bytes', async () => {
 const server = app();
 const headers = {'x-readx-gateway-key':key};
 const list = await server.inject({method:'GET',url:'/api/articles',headers});
 assert.equal(list.statusCode,200);
 assert.equal(list.json().articles[0].packsReady,true);
 const pack = await server.inject({method:'GET',url:'/api/articles/rec123/packs/decode',headers});
 assert.equal(pack.statusCode,200);
 assert.equal(pack.body,content.toString());
 assert.equal(pack.headers['cache-control'],'private, no-store');
 await server.close();
 const broken = app(async()=>Buffer.from('changed'));
 assert.equal((await broken.inject({method:'GET',url:'/api/articles/rec123/packs/decode',headers})).statusCode,409);
 await broken.close();
});

test('state changes require CSRF, reject stale versions, and replay safely', async () => {
 const data = {...record, '阅读状态':'待读', '人工版本':0};
 let writes = 0;
 const adapter = {list:async()=>[data],get:async()=>({...data}),download:async()=>content,
   updateUser:async(_id, values)=>{writes++;Object.assign(data,values);}};
 const server = buildApp({gatewayKey:key,adapter});
 const headers = {'x-readx-gateway-key':key};
 const session = await server.inject({method:'GET',url:'/api/session',headers});
 const csrf = session.json().csrf;
 const body = {status:'已读',version:0,operationId:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'};
 const request = extra => server.inject({method:'POST',url:'/api/articles/rec123/state',headers:{...headers,...extra},payload:body});
 assert.equal((await request({})).statusCode,403);
 assert.equal((await request({'x-csrf-token':csrf})).statusCode,200);
 assert.equal((await request({'x-csrf-token':csrf})).statusCode,200);
 assert.equal(writes,1);
 const stale = await server.inject({method:'POST',url:'/api/articles/rec123/state',headers:{...headers,'x-csrf-token':csrf},payload:{...body,status:'跳过',operationId:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}});
 assert.equal(stale.statusCode,409);
 assert.equal(data['阅读状态'],'已读');
 await server.close();
});
