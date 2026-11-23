import Fastify from 'fastify';
import { execFile as execFileCallback, spawn } from 'node:child_process';
import { createHash, timingSafeEqual, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm, lstat, mkdir, open } from 'node:fs/promises';
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
const baseWriteLockScript = path.join(publicDir, '..', '..', 'scripts', 'reading_write_lock.py');
const baseWriteLockPath = path.join(publicDir, '..', '..', 'state', 'long-read', 'reading-archive.lock');
const dispatchStatusScript = path.join(publicDir, '..', '..', 'scripts', 'read_dispatch_status.py');

async function withBaseWriteLock(operation) {
  const child = spawn('/usr/bin/python3', [baseWriteLockScript, baseWriteLockPath], { stdio: ['pipe', 'pipe', 'ignore'] });
  let output = '';
  try {
    await new Promise((resolve, reject) => {
      let settled = false;
      child.stdout.on('data', chunk => {
        output += chunk.toString('utf8');
        if (output.includes('\n') && !settled) {
          settled = true;
          if (output.split('\n', 1)[0] === 'locked') resolve();
          else reject(new Error('Base 写入锁获取失败'));
        }
      });
      child.once('error', error => {
        if (!settled) { settled = true; reject(error); }
      });
      child.once('close', code => {
        if (!settled) { settled = true; reject(new Error(`Base 写入锁进程退出: ${code}`)); }
      });
    });
    return await operation();
  } finally {
    child.stdin.end();
  }
}

async function readDispatchStatuses() {
  const { stdout } = await execFile('/usr/bin/python3', [dispatchStatusScript], { timeout: 5000, maxBuffer: 2 * 1024 * 1024 });
  const result = JSON.parse(stdout);
  const allowed = new Set(['queued', 'running', 'needs_review', 'failed', 'retryable', 'completed', 'suppressed', 'unmanaged']);
  if (result.schemaVersion !== 1 || result.status !== 'ok' || !result.articles || typeof result.articles !== 'object' || Array.isArray(result.articles) ||
      Object.entries(result.articles).some(([url, phase]) => typeof url !== 'string' || !allowed.has(phase))) throw new Error('文章接单状态格式无效');
  return result.articles;
}

function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
  const time = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
}

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

function canonicalSource(value) {
  const safe = safeSource(value);
  if (!safe) return null;
  const url = new URL(safe);
  url.hash = '';
  return url.href;
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

export function buildApp({ gatewayKey, adapter = createBaseAdapter(), bookQuery, reviewWrite, dispatchQuery = readDispatchStatuses, baseWriteLock = operation => operation(), reviewDir = path.join(publicDir, '..', '..', 'state', 'workbench', 'reviews') }) {
  if (typeof gatewayKey !== 'string' || Buffer.byteLength(gatewayKey) < 32) throw new Error('网关服务凭据未配置');
  const expected = Buffer.from(gatewayKey);
  const app = Fastify({ logger: false, bodyLimit: 4096 });
  const csrf = randomBytes(32).toString('base64url');
  let writeQueue = Promise.resolve();
  app.addHook('onRequest', async (request, reply) => {
    reply.header('Cache-Control', 'private, no-store').header('X-Content-Type-Options', 'nosniff')
      .header('Referrer-Policy', 'no-referrer').header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    const suppliedKey = request.headers['x-readx-gateway-key'];
    if (suppliedKey !== undefined) {
      const candidate = Buffer.from(typeof suppliedKey === 'string' ? suppliedKey : '');
      if (candidate.length !== expected.length || !timingSafeEqual(candidate, expected)) return reply.code(403).send({ error: '访问被拒绝' });
      return;
    }

    // Match Invest-X: this service binds only to loopback, and an unkeyed
    // browser request is accepted only for an exact local Host. The Feishu
    // Gateway continues to authenticate with the service key above.
    const remoteAddress = request.raw.socket?.remoteAddress;
    const address = request.server.server.address();
    const port = address && typeof address !== 'string' ? address.port : null;
    const host = request.headers.host;
    const localPeer = ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(remoteAddress);
    const localHost = Number.isInteger(port) && [`127.0.0.1:${port}`, `localhost:${port}`].includes(host);
    if (!localPeer || !localHost) return reply.code(403).send({ error: '访问被拒绝' });
  });
  for (const [route, file, type] of [['/', 'index.html', 'text/html; charset=utf-8'], ['/app.js', 'app.js', 'application/javascript; charset=utf-8'], ['/style.css', 'style.css', 'text/css; charset=utf-8']]) {
    app.get(route, async (_request, reply) => reply.type(type).send(await readFile(path.join(publicDir, file))));
  }
  app.get('/api/articles', async () => {
    const records = await adapter.list();
    let dispatch = null;
    try { dispatch = await dispatchQuery(); } catch { /* Preserve article access; show status as unavailable. */ }
    return { articles: records.filter(record => recordPattern.test(record.id) && /^(context|url):/.test(String(record['文章键'] || ''))).map(record => {
      const manifest = manifestFor(record);
      const sourceUrl = safeSource(record['原文链接']) || safeSource(manifest?.source_url);
      const dispatchKey = sourceUrl ? canonicalSource(sourceUrl) : null;
      let dispatchStatus = dispatch === null ? 'unavailable' : (dispatchKey ? dispatch[dispatchKey] || '' : '');
      if (dispatchStatus && dispatchStatus !== 'completed') dispatchStatus = ({queued:'queued', running:'running', needs_review:'needs_review', failed:'failed', retryable:'retryable', suppressed:'suppressed', unmanaged:'unmanaged'})[dispatchStatus] || 'unavailable';
      return { id: record.id, title: String(record['标题'] || '未命名文章'), sourceUrl, packsReady: Boolean(manifest), status: record['阅读状态'] || '待读', version: record['人工版本'] || 0, topic: record['人工主题'] || record['自动主题'] || '', author: record['作者'] || '', quality: record['质量分'] ?? null, minutes: record['阅读时长'] ?? null, added: record['入库时间'] || null, fetchStatus: record['抓取状态'] || '', scoreStatus: record['评分状态'] || '', packStatus: record['双包状态'] || '', dispatchStatus };
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
    const pending = writeQueue.then(() => baseWriteLock(run), () => baseWriteLock(run));
    writeQueue = pending.catch(() => {});
    const result = await pending;
    if (result.conflict) return reply.code(409).send({ error: '状态已更新，请刷新后重试' });
    return result;
  });
  async function queryBooks(from, to, bookId) {
    if (bookQuery) return bookQuery(from, to, bookId);
    const { stdout } = await execFile('/opt/homebrew/bin/node', ['/Users/yuwei/code/learn-x/scripts/reading-workbench-sync.mjs', 'query', from, to, ...(bookId ? [bookId] : [])], { timeout: 30000, maxBuffer: 32 * 1024 * 1024 });
    const result = JSON.parse(stdout);
    if (result.schemaVersion !== 1 || result.from !== from || result.to !== to || !Array.isArray(result.books) || !Array.isArray(result.gaps)) throw new Error('阅读资料格式无效');
    return result;
  }
  app.get('/api/books', async (request, reply) => {
    const { from, to, bookId } = request.query;
    if (!validDate(from) || !validDate(to) || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId))) return reply.code(400).send({ error: '日期或书籍无效' });
    return queryBooks(from, to, bookId);
  });
  app.post('/api/books/review', async (request, reply) => {
    if (request.headers['x-csrf-token'] !== csrf) return reply.code(403).send({ error: '请求校验失败' });
    const { from, to, bookId, mode, operationId, finishedAt } = request.body || {};
    if (!validDate(from) || !validDate(to) || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId)) || !['daily','weekly','biweekly','monthly'].includes(mode) ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(operationId || '') ||
        !Number.isFinite(Date.parse(finishedAt))) return reply.code(400).send({ error: '复习信息无效' });
    const run = async () => {
      await mkdir(reviewDir, { recursive:true, mode:0o700 });
      const dirStat = await lstat(reviewDir);
      if (!dirStat.isDirectory() || dirStat.isSymbolicLink() || (dirStat.mode & 0o077)) throw new Error('复习记录目录无效');
      const intentPath = path.join(reviewDir, `${operationId}.json`);
      const requestKey = JSON.stringify({from,to,bookId:bookId || null,mode,operationId,finishedAt});
      let intent;
      try { intent = JSON.parse(await readFile(intentPath, 'utf8')); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        if (Math.abs(Date.now()-Date.parse(finishedAt)) > 600000) throw new Error('复习请求已过期');
        const data = await queryBooks(from, to, bookId);
        if (!data.books.length) throw new Error('所选范围没有阅读资料');
        const review = JSON.stringify({operationId, finishedAt, from, to, mode, books:data.books.map(book => book.bookId).sort(),
          datasetSha256:createHash('sha256').update(JSON.stringify({books:data.books,days:data.days,gaps:data.gaps})).digest('hex'), gaps:data.gaps});
        intent = {requestKey, review};
        const file = await open(intentPath, 'wx', 0o600);
        try { await file.writeFile(JSON.stringify(intent)); await file.sync(); } finally { await file.close(); }
        const directory = await open(reviewDir, 'r');
        try { await directory.sync(); } finally { await directory.close(); }
      }
      if (intent.requestKey !== requestKey || typeof intent.review !== 'string') throw new Error('复习请求冲突');
      const review = intent.review;
      if (reviewWrite) return reviewWrite({operationId, review});
      const reviewData = JSON.parse(review);
      if (!hashPattern.test(reviewData.datasetSha256 || '') || !Array.isArray(reviewData.books) || !Array.isArray(reviewData.gaps)) throw new Error('复习资料校验失败');
      const dateAt = (date, hour, minute, second, millisecond = 0) => Date.parse(`${date}T${hour}:${minute}:${second}.${String(millisecond).padStart(3,'0')}+08:00`);
      const reviewFields = {
        '记录类型':'复习记录', '书名':'阅读复习', '来源书籍ID':`review:${operationId}`,
        '最近复习':review,
        '复习完成时间':Date.parse(reviewData.finishedAt),
        '范围开始':dateAt(reviewData.from,'00','00','00'),
        '范围结束':dateAt(reviewData.to,'23','59','59',999),
        '复习模式':({daily:'日常伴读',weekly:'周度深读',biweekly:'双周深读',monthly:'月度深读'})[reviewData.mode],
        '本次书籍ID集合':JSON.stringify(reviewData.books),
        '阅读资料SHA256':reviewData.datasetSha256,
        '缺口日期':JSON.stringify(reviewData.gaps),
      };
      if (!Object.values(reviewFields).every(value => value !== undefined && value !== null) ||
          !['复习完成时间','范围开始','范围结束'].every(name => Number.isFinite(reviewFields[name]))) throw new Error('复习记录字段不完整');
      const apiPath = `/open-apis/bitable/v1/apps/${base}/tables/tblDwjpNOH6hWcUo/records`;
      try { await cli(['api','POST',apiPath,'--params',JSON.stringify({client_token:operationId}),'--data',JSON.stringify({fields:reviewFields}),'--as','bot','--format','json']); } catch { /* Confirm by readback. */ }
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
        const matchesEpoch = value => typeof value === 'number' ? value : typeof value === 'string' ? Date.parse(value) : NaN;
        if (matches.length===1 && matches[0]['最近复习']===review &&
            Object.entries(reviewFields).every(([name,value]) => {
              const actual=matches[0][name];
              return ['复习完成时间','范围开始','范围结束'].includes(name)
                ? Math.abs(matchesEpoch(actual)-value)<1000
                : actual===value;
            })) return {ok:true};
        if (matches.length===1) throw new Error('复习记录冲突');
      }
      throw new Error('复习记录待核实');
    };
    const pending=writeQueue.then(()=>baseWriteLock(run),()=>baseWriteLock(run));writeQueue=pending.catch(()=>{});return pending;
  });
  app.get('/api/books/context', async (request, reply) => {
    const { from, to, bookId, mode } = request.query;
    if (!validDate(from) || !validDate(to) || from > to ||
        (bookId && !/^[A-Za-z0-9_-]{1,200}$/.test(bookId)) || !['daily','weekly','biweekly','monthly'].includes(mode)) return reply.code(400).send({ error: '范围或模式无效' });
    const data = await queryBooks(from, to, bookId);
    if (!data.books.length) return reply.code(409).send({ error: '所选范围没有可用阅读资料' });
    const promptId = mode === 'daily' ? 'learn-x.reading-daily' : 'learn-x.reading-weekly';
    const result = await execFile('/opt/homebrew/bin/node', ['/Users/yuwei/code/skills/prompt-governance/scripts/fetch-prompt.mjs', promptId], {timeout:30000,maxBuffer:1024*1024});
    const asset = JSON.parse(result.stdout);
    if (asset.contract_version !== 'prompt-asset/v1' || asset.prompt_id !== promptId ||
        !Number.isInteger(asset.prompt_revision) || typeof asset.content !== 'string' ||
        !/^[a-f0-9]{64}$/.test(asset.prompt_sha256 || '') ||
        createHash('sha256').update(asset.content).digest('hex') !== asset.prompt_sha256) {
      throw new Error('阅读 Prompt 校验失败');
    }
    let prompt = asset.content;
    if (mode === 'daily') {
      const part = asset.content.split('## ChatGPT 伴读 Prompt\n\n');
      if (part.length !== 2 || !part[1].includes('\n```')) throw new Error('伴读 Prompt 格式变化');
      prompt = part[1].split('\n```')[0];
    }
    const modeLabel = {daily:'日常伴读',weekly:'周度深读',biweekly:'双周深读',monthly:'月度深读'}[mode];
    const text = [`# 阅读资料`, `可用资料：以下是微信读书已离线保存的划线与个人想法，不含全书正文或独立书评。`, `模式：${modeLabel}`, `范围：${from} 至 ${to}（Asia/Shanghai）`, `未完整日期：${data.gaps.join('、') || '无'}`];
    for (const book of data.books) {
      text.push(`\n## 《${book.title}》`, `作者：${book.author || '未知'}`);
      for (const item of book.highlights) text.push(`\n${item.chapterTitle} · ${item.date} · 位置 ${item.range || '未知'}`, `\n> ${item.text.replace(/\n/g,'\n> ')}`);
      for (const item of book.thoughts) {
        text.push(`\n${item.chapterTitle} · ${item.date} · 个人想法`);
        if (item.abstract) text.push(`\n> ${item.abstract.replace(/\n/g,'\n> ')}`);
        if (item.content) text.push(`\n${item.content}`);
      }
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
  const app = buildApp({ gatewayKey: (await readFile(keyFile, 'utf8')).trim(), baseWriteLock: withBaseWriteLock });
  const port = Number(process.env.READX_WORKBENCH_PORT || '4185');
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('工作台端口无效');
  await app.listen({ host: '127.0.0.1', port });
}
