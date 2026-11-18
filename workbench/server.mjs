import Fastify from 'fastify';
import { execFile as execFileCallback } from 'node:child_process';
import { createHash, timingSafeEqual, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm, lstat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const execFile = promisify(execFileCallback);
const base = 'HZLvb3lnUaH3z3sn3plcSRg0nPc';
const table = 'tbl6IkGyidB5H75v';
const publicDir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'public');
const fields = ['标题', '文章键', '校验清单', '深层解码', '芒格洞察', '原文链接', '作者', '来源', '入库时间', '质量分', '决策分', '阅读时长', '自动主题', '人工主题', '阅读状态', '人工版本', '最近操作', '抓取状态', '评分状态', '双包状态'];
const packFields = { decode: '深层解码', munger: '芒格洞察' };
const recordPattern = /^rec[A-Za-z0-9]+$/;
const filePattern = /^[A-Za-z0-9_-]+$/;
const hashPattern = /^[a-f0-9]{64}$/;
const maxPackBytes = 50 * 1024 * 1024;

async function cli(args, options = {}) {
  const { stdout } = await execFile('lark-cli', args, { timeout: 30000, maxBuffer: 8 * 1024 * 1024, ...options });
  const result = JSON.parse(stdout);
  if (!result.ok || !result.data) throw new Error('Base 请求失败');
  return result.data;
}

function readMatrix(data) {
  if (!Array.isArray(data.fields) || !Array.isArray(data.data) || !Array.isArray(data.record_id_list)
      || data.record_id_list.length !== data.data.length) throw new Error('Base 记录格式无效');
  return data.data.map((values, index) => {
    if (!Array.isArray(values) || values.length !== data.fields.length) throw new Error('Base 字段格式无效');
    return { id: data.record_id_list[index], ...Object.fromEntries(data.fields.map((field, i) => [field, values[i]])) };
  });
}

export function createBaseAdapter() {
  const common = ['--base-token', base, '--table-id', table, '--as', 'bot', '--format', 'json'];
  return {
    async list() {
      const records = [];
      let offset = 0;
      while (true) {
        const page = await cli(['base', '+record-list', ...common, '--limit', '100', '--offset', String(offset), ...fields.flatMap(f => ['--field-id', f])]);
        const rows = readMatrix(page);
        records.push(...rows);
        if (!page.has_more) return records;
        if (!rows.length || offset + rows.length <= offset) throw new Error('Base 分页不完整');
        offset += rows.length;
      }
    },
    async get(id) {
      const data = await cli(['base', '+record-get', ...common, '--record-id', id, ...fields.flatMap(f => ['--field-id', f])]);
      const rows = readMatrix(data);
      if (rows.length !== 1 || rows[0].id !== id) throw new Error('文章记录不可用');
      return rows[0];
    },
    async updateUser(id, values) {
      await cli(['base', '+record-upsert', ...common, '--record-id', id, '--json', JSON.stringify(values)]);
    },
    async download(id, token) {
      const dir = await mkdtemp(path.join(os.tmpdir(), 'readx-pack-'));
      try {
        const target = path.join(dir, 'pack');
        await cli(['base', '+record-download-attachment', ...common, '--record-id', id, '--file-token', token, '--output', './pack'], { cwd: dir });
        const stats = await lstat(target);
        if (!stats.isFile() || stats.isSymbolicLink() || stats.size > maxPackBytes) throw new Error('附件无效');
        return await readFile(target);
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    },
  };
}

function safeSource(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function manifestFor(record) {
  if (typeof record['校验清单'] !== 'string') return null;
  try {
    const m = JSON.parse(record['校验清单']);
    if (m.schema !== 'readx.context-archive/v1' || m.source_key !== record['文章键'] || !hashPattern.test(m.snapshot_sha256 || '')) return null;
    for (const [key, field] of Object.entries(packFields)) {
      const pack = m.packs?.[key];
      const attachments = record[field];
      if (!pack || !filePattern.test(pack.file_token || '') || !hashPattern.test(pack.sha256 || '')
          || !Number.isSafeInteger(pack.bytes) || pack.bytes < 1 || pack.bytes > maxPackBytes
          || !Array.isArray(attachments) || attachments.length !== 1
          || attachments[0].file_token !== pack.file_token || attachments[0].size !== pack.bytes) return null;
    }
    if (m.source_url != null) {
      const url = new URL(m.source_url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    }
    return m;
  } catch { return null; }
}

export function buildApp({ gatewayKey, adapter = createBaseAdapter() }) {
  if (typeof gatewayKey !== 'string' || Buffer.byteLength(gatewayKey) < 32) throw new Error('网关服务凭据未配置');
  const expected = Buffer.from(gatewayKey);
  const app = Fastify({ logger: false, bodyLimit: 4096 });
  const csrf = randomBytes(32).toString('base64url');
  let writeQueue = Promise.resolve();
  app.addHook('onRequest', async (request, reply) => {
    reply.header('Cache-Control', 'private, no-store').header('X-Content-Type-Options', 'nosniff')
      .header('Referrer-Policy', 'no-referrer').header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    const candidate = Buffer.from(typeof request.headers['x-readx-gateway-key'] === 'string' ? request.headers['x-readx-gateway-key'] : '');
    if (candidate.length !== expected.length || !timingSafeEqual(candidate, expected)) return reply.code(403).send({ error: '访问被拒绝' });
  });
  for (const [route, file, type] of [['/', 'index.html', 'text/html; charset=utf-8'], ['/app.js', 'app.js', 'application/javascript; charset=utf-8'], ['/style.css', 'style.css', 'text/css; charset=utf-8']]) {
    app.get(route, async (_request, reply) => reply.type(type).send(await readFile(path.join(publicDir, file))));
  }
  app.get('/api/articles', async () => {
    const records = await adapter.list();
    return { articles: records.filter(record => recordPattern.test(record.id) && /^(context|url):/.test(String(record['文章键'] || ''))).map(record => {
      const manifest = manifestFor(record);
      return { id: record.id, title: String(record['标题'] || '未命名文章'), sourceUrl: safeSource(record['原文链接']) || safeSource(manifest?.source_url), packsReady: Boolean(manifest), status: record['阅读状态'] || '待读', version: record['人工版本'] || 0, topic: record['人工主题'] || record['自动主题'] || '', author: record['作者'] || '', quality: record['质量分'] ?? null, minutes: record['阅读时长'] ?? null, added: record['入库时间'] || null };
    }) };
  });
  app.get('/api/session', async () => ({ csrf }));
  app.post('/api/articles/:id/state', async (request, reply) => {
    if (request.headers['x-csrf-token'] !== csrf) return reply.code(403).send({ error: '请求校验失败' });
    const { id } = request.params;
    const { status, topic, version, operationId } = request.body || {};
    const topics = ['AI与技术', '投资与商业', '历史与文明', '学习与教育', '生活与人文'];
    if (!recordPattern.test(id) || !Number.isSafeInteger(version) || version < 0 ||
        typeof operationId !== 'string' || !/^[0-9a-f-]{36}$/i.test(operationId) ||
        (status === undefined && topic === undefined) ||
        (status !== undefined && !['待读', '已读', '跳过'].includes(status)) ||
        (topic !== undefined && !topics.includes(topic))) return reply.code(400).send({ error: '请求无效' });
    const fingerprint = createHash('sha256').update(JSON.stringify({ id, status, topic, version })).digest('hex');
    const operation = `${operationId}:${fingerprint}`;
    const run = async () => {
      const record = await adapter.get(id);
      if (record['最近操作'] === operation) return { ok: true, version: version + 1 };
      if ((record['人工版本'] || 0) !== version) return { conflict: true };
      const values = { '人工版本': version + 1, '最近操作': operation };
      if (status !== undefined) values['阅读状态'] = status;
      if (topic !== undefined) values['人工主题'] = topic;
      try { await adapter.updateUser(id, values); } catch { /* Read back before deciding outcome. */ }
      for (let attempt = 0; attempt < 6; attempt++) {
        if (attempt) await new Promise(resolve => setTimeout(resolve, 500));
        const check = await adapter.get(id);
        if (check['最近操作'] === operation && check['人工版本'] === version + 1 &&
            (status === undefined || check['阅读状态'] === status) &&
            (topic === undefined || check['人工主题'] === topic)) return { ok: true, version: version + 1 };
      }
      throw new Error('状态写入待核实');
    };
    const pending = writeQueue.then(run, run);
    writeQueue = pending.catch(() => {});
    const result = await pending;
    if (result.conflict) return reply.code(409).send({ error: '状态已更新，请刷新后重试' });
    return result;
  });
  async function queryBooks(from, to, bookId) {
    const { stdout } = await execFile('/opt/homebrew/bin/node', ['/Users/yuwei/code/learn-x/scripts/reading-workbench-sync.mjs', 'query', from, to, ...(bookId ? [bookId] : [])], { timeout: 30000, maxBuffer: 32 * 1024 * 1024 });
    const result = JSON.parse(stdout);
    if (result.schemaVersion !== 1 || result.from !== from || result.to !== to || !Array.isArray(result.books) || !Array.isArray(result.gaps)) throw new Error('阅读资料格式无效');
    return result;
  }
  app.get('/api/books', async (request, reply) => {
    const { from, to, bookId } = request.query;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from || '') || !/^\d{4}-\d{2}-\d{2}$/.test(to || '') || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId))) return reply.code(400).send({ error: '日期或书籍无效' });
    return queryBooks(from, to, bookId);
  });
  app.post('/api/books/review', async (request, reply) => {
    if (request.headers['x-csrf-token'] !== csrf) return reply.code(403).send({ error: '请求校验失败' });
    const { from, to, bookId, mode, operationId, finishedAt } = request.body || {};
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from || '') || !/^\d{4}-\d{2}-\d{2}$/.test(to || '') || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId)) || !['daily','weekly','biweekly','monthly'].includes(mode) ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(operationId || '') ||
        !Number.isFinite(Date.parse(finishedAt)) || Math.abs(Date.now()-Date.parse(finishedAt)) > 600000) return reply.code(400).send({ error: '复习信息无效' });
    const data = await queryBooks(from, to, bookId);
    if (!data.books.length) return reply.code(409).send({ error: '所选范围没有阅读资料' });
    const sources = data.books.flatMap(book => [...book.highlights, ...book.thoughts].map(item => item.sourceId)).sort();
    const review = JSON.stringify({operationId, finishedAt, from, to, mode, books:data.books.map(book => book.bookId).sort(),
      datasetSha256:createHash('sha256').update(JSON.stringify(sources)).digest('hex'), gaps:data.gaps});
    const run = async () => {
      const path = `/open-apis/bitable/v1/apps/${base}/tables/tblDwjpNOH6hWcUo/records`;
      try { await cli(['api','POST',path,'--params',JSON.stringify({client_token:operationId}),'--data',JSON.stringify({fields:{'书名':'复习记录','来源书籍ID':`review:${operationId}`,'最近复习':review}}),'--as','bot','--format','json']); } catch { /* Confirm by readback. */ }
      for (let attempt=0; attempt<6; attempt++) {
        if (attempt) await new Promise(resolve => setTimeout(resolve,500));
        const records=[]; let offset=0;
        while (true) {
          const page=await cli(['base','+record-list','--base-token',base,'--table-id','tblDwjpNOH6hWcUo','--as','bot','--format','json','--limit','200','--offset',String(offset)]);
          const rows=readMatrix(page); records.push(...rows);
          if (!page.has_more) break;
          if (!rows.length) throw new Error('复习记录分页失败'); offset+=rows.length;
        }
        const matches=records.filter(row=>row['来源书籍ID']===`review:${operationId}`);
        if (matches.length>1) throw new Error('复习记录重复');
        if (matches.length===1 && matches[0]['最近复习']===review) return {ok:true};
        if (matches.length===1) throw new Error('复习记录冲突');
      }
      throw new Error('复习记录待核实');
    };
    const pending=writeQueue.then(run,run);writeQueue=pending.catch(()=>{});return pending;
  });
  app.get('/api/books/context', async (request, reply) => {
    const { from, to, bookId, mode } = request.query;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from || '') || !/^\d{4}-\d{2}-\d{2}$/.test(to || '') || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId)) || !['daily','weekly','biweekly','monthly'].includes(mode)) return reply.code(400).send({ error: '范围或模式无效' });
    const data = await queryBooks(from, to, bookId);
    if (!data.books.length) return reply.code(409).send({ error: '所选范围没有可用阅读资料' });
    let prompt;
    if (mode === 'daily') {
      const result = await execFile('/opt/homebrew/bin/node', ['/Users/yuwei/code/skills/prompt-governance/scripts/fetch-prompt.mjs','learn-x.reading-daily'], {timeout:30000,maxBuffer:1024*1024});
      const asset = JSON.parse(result.stdout);
      if (asset.prompt_id !== 'learn-x.reading-daily' || createHash('sha256').update(asset.content).digest('hex') !== asset.prompt_sha256) throw new Error('伴读 Prompt 校验失败');
      const part = asset.content.split('## ChatGPT 伴读 Prompt\n\n');
      if (part.length !== 2 || !part[1].includes('\n```')) throw new Error('伴读 Prompt 格式变化');
      prompt = part[1].split('\n```')[0];
    } else {
      prompt = await readFile('/Users/yuwei/code/learn-x/02_prompts/meta/chatgpt-reading-weekly.md', 'utf8');
    }
    const text = [`# 阅读资料`, `范围：${from} 至 ${to}（Asia/Shanghai）`, `未完整日期：${data.gaps.join('、') || '无'}`];
    for (const book of data.books) {
      text.push(`\n## 《${book.title}》`, `作者：${book.author || '未知'}`);
      for (const item of book.highlights) text.push(`\n${item.chapterTitle} · ${item.date} · 位置 ${item.range || '未知'}`, `\n> ${item.text.replace(/\n/g,'\n> ')}`);
      for (const item of book.thoughts) text.push(`\n${item.chapterTitle} · ${item.date} · 个人想法`, item.content || item.abstract);
    }
    text.push('\n## 阅读 Prompt\n',prompt);
    return reply.type('text/plain; charset=utf-8').send(text.join('\n'));
  });
  app.get('/api/articles/:id/packs/:kind', async (request, reply) => {
    const { id, kind } = request.params;
    if (!recordPattern.test(id) || !Object.hasOwn(packFields, kind)) return reply.code(404).send({ error: '资源不存在' });
    const record = await adapter.get(id);
    const manifest = manifestFor(record);
    if (!manifest) return reply.code(409).send({ error: '上下文包尚未完成验证' });
    const pack = manifest.packs[kind];
    const bytes = await adapter.download(id, pack.file_token);
    if (!Buffer.isBuffer(bytes) || bytes.length !== pack.bytes || createHash('sha256').update(bytes).digest('hex') !== pack.sha256) return reply.code(409).send({ error: '上下文包完整性校验失败' });
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { return reply.code(409).send({ error: '上下文包编码无效' }); }
    return reply.type('text/plain; charset=utf-8').send(text);
  });
  app.setErrorHandler((_error, _request, reply) => reply.code(503).send({ error: '文章数据暂时不可用，请稍后重试' }));
  return app;
}

if ([process.argv[1], process.env.pm_exec_path].some(entry => entry && path.resolve(entry) === fileURLToPath(import.meta.url))) {
  const keyFile = path.join(path.dirname(publicDir), '..', 'state', 'workbench', 'gateway-key');
  const stats = await lstat(keyFile);
  if (!stats.isFile() || stats.isSymbolicLink() || (stats.mode & 0o077) !== 0) throw new Error('网关服务凭据文件权限无效');
  const app = buildApp({ gatewayKey: (await readFile(keyFile, 'utf8')).trim() });
  const port = Number(process.env.READX_WORKBENCH_PORT || '4185');
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('工作台端口无效');
  await app.listen({ host: '127.0.0.1', port });
}
