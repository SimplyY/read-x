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
    meta.textContent = [article.author, article.topic, article.quality == null ? '' : `质量 ${article.quality}`, article.minutes == null ? '' : `约 ${article.minutes} 分钟`, article.status, article.packsReady ? '上下文包可用' : '上下文包待验证'].filter(Boolean).join(' · ');
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
booksTab.addEventListener('click', () => {
  showingBooks = true;
  articlesTab.removeAttribute('aria-current');
  booksTab.setAttribute('aria-current', 'page');
  const heading = document.createElement('h1');
  heading.textContent = '书籍复习';
  const message = document.createElement('p');
  message.textContent = '阅读资料尚未接入。';
  content.replaceChildren(heading, message);
});
document.querySelector('#close-pack').addEventListener('click', () => dialog.close());
copyButton.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(packText.value); packStatus.textContent = '已复制全文。'; }
  catch { packText.focus(); packText.select(); packStatus.textContent = '请选择文本手动复制。'; }
});
loadArticles();
