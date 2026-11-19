const content = document.querySelector('#content');
const articlesTab = document.querySelector('#articles-tab');
const booksTab = document.querySelector('#books-tab');
const originalContent = content.innerHTML;
const dialog = document.querySelector('#pack-dialog');
const packText = document.querySelector('#pack-content');
const packStatus = document.querySelector('#pack-status');
const copyButton = document.querySelector('#copy-pack');
let articles = [];
let showingBooks = false;
let page = 1;
const queryState = new URLSearchParams(location.search);


function action(label, callback) {
  const button = document.createElement('button');
  button.textContent = label;
  button.addEventListener('click', callback);
  return button;
}

function renderArticles() {
  const list = document.querySelector('#articles');
  const query = document.querySelector('#search').value.trim().toLocaleLowerCase();
  const state = document.querySelector('#state-filter').value;
  const topic = document.querySelector('#topic-filter').value;
  const pack = document.querySelector('#pack-filter').value;
  const score = document.querySelector('#score-filter').value;
  const length = document.querySelector('#length-filter').value;
  const sort = document.querySelector('#sort-filter').value;
  const shown = articles.filter(article => `${article.title} ${article.author}`.toLocaleLowerCase().includes(query) &&
    (state === '全部状态' || article.status === state) && (topic === '全部主题' || article.topic === topic) &&
    (pack === '全部上下文包' || article.packsReady === (pack === '已就绪')) &&
    (score === '全部评分' || (score === '评分未知' ? article.quality == null : article.quality != null && (score === '6分及以上' ? article.quality >= 6 : article.quality < 6))) &&
    (length === '全部时长' || (length === '时长未知' ? article.minutes == null : article.minutes != null && (length === '10分钟内' ? article.minutes <= 10 : length === '11至20分钟' ? article.minutes > 10 && article.minutes <= 20 : article.minutes > 20))));
  shown.sort((a,b) => sort === 'score' ? (b.quality ?? -1) - (a.quality ?? -1) :
    sort === 'short' ? (a.minutes ?? Infinity) - (b.minutes ?? Infinity) :
    (sort === 'old' ? 1 : -1) * String(a.added || '').localeCompare(String(b.added || '')) || a.id.localeCompare(b.id));
  page = Math.max(1, Math.min(page, Math.ceil(shown.length / 20) || 1));
  list.replaceChildren();
  document.querySelector('#count').textContent = `${shown.length} 篇`;
  document.querySelector('#page-info').textContent = `第 ${page} / ${Math.ceil(shown.length / 20) || 1} 页`;
  document.querySelector('#previous').disabled = page === 1;
  document.querySelector('#next').disabled = page * 20 >= shown.length;
  for (const article of shown.slice((page - 1) * 20, page * 20)) {
    const item = document.createElement('article');
    item.className = 'item';
    const title = document.createElement('h3');
    title.textContent = article.title;
    const meta = document.createElement('div');
    meta.className = 'meta';
    const dispatchLabel = ({queued:'已接单',running:'处理中',needs_review:'待核实',failed:'处理失败，待核实',retryable:'处理失败，可安全重试',completed:'加工完成',unavailable:'流程状态暂不可读'})[article.dispatchStatus] || '';
    meta.textContent = [article.author, article.topic, article.quality == null ? '' : `质量 ${article.quality}`, article.minutes == null ? '' : `约 ${article.minutes} 分钟`, article.status, article.packsReady ? '上下文包可用' : '上下文包待验证', dispatchLabel ? `流程：${dispatchLabel}` : '', !article.packsReady && article.packStatus ? `双包处理：${article.packStatus}` : '', article.fetchStatus ? `抓取：${article.fetchStatus}` : '', article.scoreStatus ? `评分：${article.scoreStatus}` : ''].filter(Boolean).join(' · ');
    const actions = document.createElement('div');
    actions.className = 'actions';
    if (article.sourceUrl) {
      const link = document.createElement('a');
      link.href = article.sourceUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '打开原文';
      actions.append(link);
    }
    if (article.dispatchStatus === 'retryable' && article.sourceUrl) {
      actions.append(action('复制链接以重试', async () => {
        let copied = false;
        try {
          await navigator.clipboard.writeText(article.sourceUrl);
          copied = true;
        } catch {
          const fallback = document.createElement('textarea');
          fallback.value = article.sourceUrl;
          fallback.readOnly = true;
          fallback.style.position = 'fixed';
          fallback.style.opacity = '0';
          document.body.append(fallback);
          fallback.select();
          try { copied = document.execCommand('copy'); } catch { copied = false; }
          fallback.remove();
        }
        document.querySelector('#status').textContent = copied
          ? '链接已复制。请将它发回 Read-X 群，系统会重新处理。'
          : '请复制原文链接并发回 Read-X 群以重试。';
      }));
    }
    for (const [kind, label] of [['decode', '文章深层解码'], ['munger', '芒格洞察']]) {
      const button = action(label, () => openPack(article.id, kind, label));
      button.disabled = !article.packsReady;
      actions.append(button);
    }
    const stateButton = article.status === '待读' ? [['已读','标记已读'], ['跳过','跳过']] : [['待读','恢复待读']];
    for (const [state,label] of stateButton) actions.append(action(label, () => updateArticle(article, {status:state})));
    const topic = document.createElement('select');
    topic.setAttribute('aria-label', '修改主题');
    for (const name of ['选择主题','AI与技术','投资与商业','历史与文明','学习与教育','生活与人文']) {
      const option = document.createElement('option'); option.textContent = name; option.value = name === '选择主题' ? '' : name;
      option.selected = option.value === article.topic; topic.append(option);
    }
    topic.addEventListener('change', () => { if (topic.value) updateArticle(article, {topic:topic.value}); });
    actions.append(topic);
    item.append(title, meta, actions);
    list.append(item);
  }
  document.querySelector('#status').textContent = shown.length ? '' : '没有符合条件的文章。';
}

async function openPack(id, kind, label) {
  document.querySelector('#pack-title').textContent = label;
  packText.value = '';
  packStatus.textContent = '正在读取并校验…';
  copyButton.disabled = true;
  dialog.showModal();
  try {
    const response = await fetch(`./api/articles/${encodeURIComponent(id)}/packs/${kind}`, { cache:'no-store' });
    if (!response.ok) throw new Error('读取失败，请稍后重试。');
    packText.value = await response.text();
    packStatus.textContent = '全文已校验。可以复制，或选中文本手动复制。';
    copyButton.disabled = false;
  } catch (error) { packStatus.textContent = error.message; }
}

async function updateArticle(article, change) {
  const status = document.querySelector('#status');
  try {
    const session = await fetch('./api/session', {cache:'no-store'});
    if (!session.ok) throw new Error('会话已失效，请重新进入。');
    const {csrf} = await session.json();
    const response = await fetch(`./api/articles/${encodeURIComponent(article.id)}/state`, {
      method:'POST', headers:{'content-type':'application/json','x-csrf-token':csrf},
      body:JSON.stringify({...change,version:article.version,operationId:crypto.randomUUID()})
    });
    if (response.status === 409) { await loadArticles(); throw new Error('状态已在其他设备更新，请重试。'); }
    if (!response.ok) throw new Error('保存结果待核实，请刷新后查看。');
    await loadArticles();
  } catch (error) { status.textContent = error.message; }
}

async function loadArticles() {
  for (const id of ['search','state-filter','topic-filter','score-filter','length-filter','pack-filter','sort-filter']) {
    const element = document.querySelector(`#${id}`);
    if (queryState.has(id)) element.value = queryState.get(id);
    const onChange = () => { page = 1; queryState.set(id, element.value); history.replaceState(null, '', `${location.pathname}?${queryState}`); renderArticles(); };
    if (id === 'search') element.oninput = onChange; else element.onchange = onChange;
  }
  document.querySelector('#previous').onclick = () => { page--; renderArticles(); };
  document.querySelector('#next').onclick = () => { page++; renderArticles(); };
  try {
    const response = await fetch('./api/articles', { cache:'no-store' });
    if (!response.ok) throw new Error('文章暂时无法读取，请稍后重试。');
    const result = await response.json();
    if (!Array.isArray(result.articles)) throw new Error('文章数据格式有误。');
    articles = result.articles;
    if (!showingBooks) renderArticles();
  } catch (error) { if (!showingBooks) document.querySelector('#status').textContent = error.message; }
}

articlesTab.addEventListener('click', () => {
  if (!showingBooks) return;
  showingBooks = false;
  booksTab.removeAttribute('aria-current');
  articlesTab.setAttribute('aria-current', 'page');
  content.innerHTML = originalContent;
  loadArticles();
});
const shanghaiDate = () => new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
}).format(new Date());
function earlierDate(date, days) {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - days);
  return value.toISOString().slice(0, 10);
}
const bookState = { from: earlierDate(shanghaiDate(), 2), to: shanghaiDate(), mode: 'daily', bookId: '', days: 3 };
let bookRequest = 0;
let bookData = null;
const pendingReviewsKey = 'readx.pendingReviews.v1';
function loadPendingReviews() {
  const raw = sessionStorage.getItem(pendingReviewsKey);
  if (!raw) return {};
  const value = JSON.parse(raw);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('复习记录恢复信息不可用。');
  return value;
}
function writePendingReviews(value) {
  sessionStorage.setItem(pendingReviewsKey, JSON.stringify(value));
}

function bookParameters() {
  return { from: bookState.from, to: bookState.to, mode: bookState.mode,
    ...(bookState.bookId ? { bookId: bookState.bookId } : {}) };
}
function setBookPeriod(days) {
  bookState.days = days;
  bookState.to = shanghaiDate();
  bookState.from = earlierDate(bookState.to, days - 1);
  bookState.bookId = '';
  renderBooks();
}
function buildBookView() {
  content.innerHTML = `<h1>书籍复习</h1>
    <section class="filters" aria-label="复习设置">
      <span class="book-label">阅读范围</span><div class="book-period" id="book-period"></div>
      <div class="book-dates"><label>开始日期<input id="book-from" type="date"></label><label>结束日期<input id="book-to" type="date"></label></div>
      <div class="book-settings"><label>阅读模式<select id="book-mode"><option value="daily">日常伴读</option><option value="weekly">周度深读</option><option value="biweekly">双周深读</option><option value="monthly">月度深读</option></select></label><label>选择书籍<select id="book-select"><option value="">全部书籍</option></select></label></div>
    </section>
    <p id="book-status" class="book-status" role="status">正在读取阅读资料…</p>
    <section class="book-summary" id="book-summary" hidden><div><h2 id="book-summary-title"></h2><p id="book-summary-meta" class="meta"></p></div><div class="actions"><button id="book-context" class="primary">查看并复制上下文</button><button id="book-reviewed">已复习</button></div></section>
    <div class="list-head"><h2>本时段书籍</h2></div><div id="book-items" class="items"></div>`;
  const periods = content.querySelector('#book-period');
  for (const days of [1, 2, 3, 5]) {
    const button = action(`${days} 天`, () => setBookPeriod(days));
    button.setAttribute('aria-pressed', String(bookState.days === days));
    periods.append(button);
  }
  const custom = action('自定义', () => {
    bookState.days = null;
    for (const button of periods.querySelectorAll('button')) button.setAttribute('aria-pressed', 'false');
    custom.setAttribute('aria-pressed', 'true');
    content.querySelector('#book-from').focus();
  });
  custom.setAttribute('aria-pressed', String(bookState.days === null));
  periods.append(custom);
  const from = content.querySelector('#book-from');
  const to = content.querySelector('#book-to');
  from.value = bookState.from; to.value = bookState.to;
  from.max = to.max = shanghaiDate();
  for (const input of [from, to]) input.addEventListener('change', () => {
    if (!from.value || !to.value || from.value > to.value || to.value > shanghaiDate()) {
      content.querySelector('#book-status').textContent = '请选择有效的日期范围。';
      content.querySelector('#book-summary').hidden = true;
      return;
    }
    bookState.from = from.value; bookState.to = to.value; bookState.days = null; bookState.bookId = '';
    renderBooks();
  });
  const mode = content.querySelector('#book-mode');
  mode.value = bookState.mode;
  mode.addEventListener('change', () => { bookState.mode = mode.value; });
  const select = content.querySelector('#book-select');
  select.addEventListener('change', () => { bookState.bookId = select.value; showBooks(); });
  content.querySelector('#book-context').onclick = () => openBookContext();
  content.querySelector('#book-reviewed').onclick = () => saveBookReview();
}
async function renderBooks() {
  const requestId = ++bookRequest;
  buildBookView();
  const params = new URLSearchParams({ from: bookState.from, to: bookState.to });
  try {
    const response = await fetch(`./api/books?${params}`, {cache:'no-store'});
    if (!response.ok) throw new Error('阅读资料暂时无法读取，请稍后重试。');
    const result = await response.json();
    if (!Array.isArray(result.books) || !Array.isArray(result.gaps)) throw new Error('阅读资料格式有误。');
    if (!showingBooks || requestId !== bookRequest) return;
    bookData = result;
    const select = content.querySelector('#book-select');
    for (const book of result.books) {
      const option = document.createElement('option');
      option.value = book.bookId;
      option.textContent = book.title || '未命名书籍';
      select.append(option);
    }
    if (!result.books.some(book => book.bookId === bookState.bookId)) bookState.bookId = '';
    select.value = bookState.bookId;
    showBooks();
  } catch (error) {
    if (showingBooks && requestId === bookRequest) content.querySelector('#book-status').textContent = error.message;
  }
}
function showBooks() {
  if (!bookData) return;
  const books = bookState.bookId ? bookData.books.filter(book => book.bookId === bookState.bookId) : bookData.books;
  const highlights = books.reduce((count, book) => count + book.highlights.length, 0);
  const thoughts = books.reduce((count, book) => count + book.thoughts.length, 0);
  const status = content.querySelector('#book-status');
  status.textContent = bookData.gaps.length ? `资料未完整覆盖：${bookData.gaps.join('、')}` : '';
  const summary = content.querySelector('#book-summary');
  summary.hidden = !books.length;
  content.querySelector('#book-summary-title').textContent = `${bookState.from} 至 ${bookState.to} · ${books.length} 本书`;
  content.querySelector('#book-summary-meta').textContent = `${highlights} 条划线 · ${thoughts} 条个人想法`;
  const list = content.querySelector('#book-items');
  list.replaceChildren();
  if (!books.length) {
    const empty = document.createElement('p'); empty.className = 'book-empty';
    empty.textContent = '这个时间范围没有可用的阅读记录。'; list.append(empty); return;
  }
  for (const book of books) {
    const item = document.createElement('article'); item.className = 'item';
    const title = document.createElement('h3'); title.textContent = `《${book.title || '未命名书籍'}》`;
    const meta = document.createElement('div'); meta.className = 'meta';
    meta.textContent = [book.author, `${book.highlights.length} 条划线`, `${book.thoughts.length} 条想法`].filter(Boolean).join(' · ');
    const actions = document.createElement('div'); actions.className = 'actions';
    actions.append(action('复习这本书', () => {
      bookState.bookId = book.bookId; content.querySelector('#book-select').value = book.bookId; showBooks();
      openBookContext();
    }));
    item.append(title, meta, actions); list.append(item);
  }
}
async function openBookContext() {
  document.querySelector('#pack-title').textContent = '阅读上下文';
  packText.value = ''; packStatus.textContent = '正在准备阅读资料…'; copyButton.disabled = true;
  dialog.showModal();
  try {
    const response = await fetch(`./api/books/context?${new URLSearchParams(bookParameters())}`, {cache:'no-store'});
    if (!response.ok) throw new Error('上下文暂时无法准备，请稍后重试。');
    packText.value = await response.text();
    packStatus.textContent = '可以复制，或选中文本手动复制。'; copyButton.disabled = false;
  } catch (error) { packStatus.textContent = error.message; }
}
async function saveBookReview() {
  const status = content.querySelector('#book-status');
  const button = content.querySelector('#book-reviewed');
  const parameters = bookParameters();
  const selection = JSON.stringify(parameters);
  button.disabled = true;
  status.textContent = '正在保存复习记录…';
  try {
    const pending = loadPendingReviews();
    let operation = pending[selection];
    if (!operation) {
      operation = { ...parameters, operationId: crypto.randomUUID(), finishedAt: new Date().toISOString() };
      pending[selection] = operation;
      writePendingReviews(pending);
    }
    const session = await fetch('./api/session', {cache:'no-store'});
    if (!session.ok) throw new Error('会话已失效，请重新进入。');
    const {csrf} = await session.json();
    const response = await fetch('./api/books/review', {method:'POST',
      headers:{'content-type':'application/json','x-csrf-token':csrf}, body:JSON.stringify(operation)});
    if (!response.ok) throw new Error('保存结果待核实，请稍后点击“已复习”重试。');
    const remaining = loadPendingReviews();
    delete remaining[selection];
    writePendingReviews(remaining);
    status.textContent = '已记录本次复习。';
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}
booksTab.addEventListener('click', () => {
  if (showingBooks) return;
  showingBooks = true;
  articlesTab.removeAttribute('aria-current');
  booksTab.setAttribute('aria-current', 'page');
  renderBooks();
});
document.querySelector('#close-pack').addEventListener('click', () => dialog.close());
copyButton.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(packText.value); packStatus.textContent = '已复制全文。'; }
  catch { packText.focus(); packText.select(); packStatus.textContent = '请选择文本手动复制。'; }
});
loadArticles();
