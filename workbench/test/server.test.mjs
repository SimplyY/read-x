import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
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

test('reports processing states without claiming an unverified pack is usable', async () => {
 const pending = {...record, '校验清单':'', '抓取状态':'失败', '评分状态':'待处理', '双包状态':'待重试'};
 const server = buildApp({gatewayKey:key, adapter:{list:async()=>[pending]}});
 const result = await server.inject({method:'GET',url:'/api/articles',headers:{'x-readx-gateway-key':key}});
 assert.equal(result.statusCode, 200);
 assert.equal(result.json().articles[0].packsReady, false);
 assert.deepEqual([result.json().articles[0].fetchStatus,result.json().articles[0].scoreStatus,result.json().articles[0].packStatus], ['失败','待处理','待重试']);
 await server.close();
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

test('review intent survives retry and freezes selected material before any write', async () => {
 const directory = await mkdtemp(path.join(os.tmpdir(), 'review-intent-'));
 let reads = 0, writes = 0, firstReview;
 const bookQuery = async () => {
  reads++;
  return {books:[{bookId:'book',highlights:[{sourceId:reads === 1 ? 'first' : 'later'}],thoughts:[]}],gaps:[]};
 };
 const reviewWrite = async ({review}) => {
  writes++;
  if (writes === 1) { firstReview = review; throw new Error('unknown'); }
  assert.equal(review, firstReview);
  return {ok:true};
 };
 const server = buildApp({gatewayKey:key,adapter:{},bookQuery,reviewWrite,reviewDir:directory});
 const headers = {'x-readx-gateway-key':key};
 try {
  const session = await server.inject({method:'GET',url:'/api/session',headers});
  const csrf = session.json().csrf;
  const body = {from:'2026-11-16',to:'2026-11-18',mode:'daily',operationId:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',finishedAt:new Date().toISOString()};
  const request = () => server.inject({method:'POST',url:'/api/books/review',headers:{...headers,'x-csrf-token':csrf},payload:body});
  assert.equal(writes,0);
  assert.equal((await request()).statusCode,503);
  assert.equal((await request()).statusCode,200);
  assert.equal(reads,1);
  assert.equal(writes,2);
 } finally { await server.close(); await rm(directory,{recursive:true,force:true}); }
});
