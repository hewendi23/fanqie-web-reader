// ====== Router ======
const routes = [];
function route(path, handler) { routes.push({ path, handler }); }

// Track where detail/reader should return. Discover is the bookstore, not an empty search.
let lastTopTab = 'discover';
let discoverTab = '';

function navigate(hash) { location.hash = hash; }

function goBack() {
  const hash = location.hash.slice(1) || 'discover';
  const path = hash.split('?')[0];
  const qs = hash.split('?')[1];
  const q = {};
  if (qs) new URLSearchParams(qs).forEach((v,k) => q[k] = v);

  if (path === 'search' && q.q) {
    $('searchInput').value = '';
    updateClearBtn();
    S.key = '';
    location.hash = discoverTab ? `discover?tab=${encodeURIComponent(discoverTab)}` : 'discover';
  } else if (path === 'category' || path === 'categories') {
    location.hash = discoverTab ? `discover?tab=${encodeURIComponent(discoverTab)}` : 'discover';
  } else if (path === 'reader' && q.book_id) {
    location.hash = `detail?book_id=${q.book_id}`;
  } else if (path === 'comments' && q.book_id) {
    location.hash = `detail?book_id=${q.book_id}`;
  } else if (path === 'author') {
    const fromBookId = q.from || '';
    location.hash = fromBookId ? `detail?book_id=${fromBookId}` : lastTopTab;
  } else if (path === 'detail') {
    location.hash = lastTopTab;
  } else {
    location.hash = 'discover';
  }
}

function router() {
  const hash = location.hash.slice(1) || 'discover';
  const [path, queryStr] = hash.split('?');
  const query = {};
  if (queryStr) new URLSearchParams(queryStr).forEach((v,k) => query[k] = v);

  if (currentChapterKey && !path.startsWith('reader')) {
    sessionStorage.setItem('scroll_' + currentChapterKey, window.scrollY);
    currentChapterKey = null;
  }
  if (!path.startsWith('reader')) { appendedChapters = 0; }

  const app = $('app');
  const isReader = path === 'reader';
  const isPageMode = isReader && getReadMode() === 'page';
  const hasSearchQuery = path === 'search' && query.q;
  const browsing = path === 'discover' || path === 'category' || path === 'categories' || path === 'search';
  const showSearch = browsing || path === 'shelf';
  const showBottomNav = (browsing || path === 'shelf' || path === 'profile') && !isPageMode;

  if (path === 'discover' || path === 'shelf' || path === 'profile' || path === 'category' || path === 'categories' || hasSearchQuery) {
    lastTopTab = hash;
  }
  const header = document.querySelector('.header');
  if (header) header.style.display = isReader ? 'none' : '';
  document.body.style.overflow = isPageMode ? 'hidden' : '';
  const showBackBtn = hasSearchQuery || path === 'category' || path === 'categories' || (!showSearch && !isReader);
  $('backBtn').classList.toggle('hidden', !showBackBtn);
  $('searchBar').classList.toggle('hidden', !showSearch);
  $('searchTabs').classList.toggle('hidden', path !== 'search');
  $('backTop').classList.remove('visible');
  const bottomNav = $('bottomNav');
  if (bottomNav) {
    bottomNav.classList.toggle('hidden', !showBottomNav);
    const navRoute = (path === 'search' || path === 'category' || path === 'categories') ? 'discover' : path;
    bottomNav.querySelectorAll('.bottom-nav-item').forEach(item => {
      item.classList.toggle('active', item.dataset.route === navRoute);
    });
  }

  for (const r of routes) { if (r.path === path) { r.handler(query, app); break; } }
  // Refresh Lucide icons after route renders (delay to wait for async + DOM updates)
  setTimeout(() => refreshIcons(), 300);
}
window.addEventListener('hashchange', router);

// ====== Search state ======
let S = { key: '', tab: 3, loading: false, tabCache: {} };
let _searchReq = 0;
function routePath() { return (location.hash.slice(1) || 'discover').split('?')[0]; }
function currentSearchQuery() {
  if (routePath() !== 'search') return '';
  const qs = (location.hash.slice(1) || '').split('?')[1] || '';
  try { return new URLSearchParams(qs).get('q') || ''; }
  catch (e) { return ''; }
}
function searchStillCurrent(key) { return currentSearchQuery() === key; }
function searchHasMore(data, batch) {
  if (data && typeof data.has_more === 'boolean') return data.has_more;
  return (batch || []).length >= 20;
}
function paintSearch() {
  try { renderResults(); }
  catch (e) {
    console.error(e);
    S.loading = false;
    S.error = '搜索结果无法显示，请重试';
    renderSearchEmpty($('app'));
  }
}
function getTabData() { return S.tabCache[S.tab] || { books: [], filtered: [], offset: 0, hasMore: false, sortBy: 'default', tagFilter: '' }; }
function saveTabData(d) { S.tabCache[S.tab] = d; }

// ====== Routes ======
route('discover', (q, app) => {
  $('pageTitle').textContent = '书城';
  $('searchInput').placeholder = '搜索书名、作者...';
  if (S.key) {
    $('searchInput').value = '';
    S.key = '';
    updateClearBtn();
  }
  renderDiscover(app, q);
});

route('categories', (q, app) => {
  $('pageTitle').textContent = '全部分类';
  $('searchInput').placeholder = '搜索书名、作者...';
  renderAllCategories(app);
});

route('category', (q, app) => {
  $('searchInput').placeholder = '搜索书名、作者...';
  renderCategory(app, q);
});

route('search', (q, app) => {
  if (!q.q) { navigate(discoverTab ? `discover?tab=${encodeURIComponent(discoverTab)}` : 'discover'); return; }
  $('pageTitle').textContent = '搜索';
  $('searchInput').placeholder = '搜索书名、作者...';
  $('searchInput').value = q.q;
  doSearch();
});

route('shelf', (q, app) => { const count = loadData().shelf.length; $('pageTitle').textContent = `我的书架 (${count})`; $('searchInput').value = ''; $('searchInput').placeholder = '搜索书架...'; updateClearBtn(); _shelfFilter = ''; renderShelf(app); });

route('profile', (q, app) => { $('pageTitle').textContent = '我的'; renderProfile(app); });

route('author', async (q, app) => {
  const authorId = q.author_id || '';
  const name = q.name || '';
  if (!authorId && !name) { navigate('discover'); return; }
  $('pageTitle').textContent = name || '作者';
  app.innerHTML = skeletonAuthor();
  renderAuthorPage(app, authorId, name);
});

route('detail', async (q, app) => {
  const bid = q.book_id;
  $('pageTitle').textContent = '加载中...';
  if (cache.detail[bid]) renderDetail(app, bid);
  else { app.innerHTML = skeletonDetail(); await fetchDetail(bid); renderDetail(app, bid); }
});

route('reader', async (q, app) => {
  const bid = q.book_id;
  let idx = parseInt(q.chapter_idx || '0');
  $('pageTitle').textContent = '加载中...';
  const header = document.querySelector('.header');
  if (header) header.style.display = 'none';
  if (!cache.detail[bid]) { app.innerHTML = skeletonReader(); await fetchDetail(bid); }
  const d = cache.detail[bid];
  if (!d || !d.chapters.length) { app.innerHTML = errorHtml('加载失败', `reader?book_id=${bid}&chapter_idx=0`); if (header) header.style.display = ''; return; }
  if (idx < 0 || idx >= d.chapters.length) idx = 0;
  currentBookId = bid;
  currentChapterIdx = idx;
  appendedChapters = 0;
  const chapterId = d.chapters[idx].ChapterID;
  if (getContentCache(chapterId)) renderReader(app, bid, idx);
  else { app.innerHTML = skeletonReader(); await fetchContent(chapterId, bid); renderReader(app, bid, idx); }
});

route('comments', async (q, app) => {
  const bid = q.book_id;
  $('pageTitle').textContent = '评论';
  if (cache.detail[bid] && cache.detail[bid].comments) renderComments(app, bid);
  else {
    app.innerHTML = skeletonComments();
    if (!cache.detail[bid]) await fetchDetail(bid);
    await fetchComments(bid);
    renderComments(app, bid);
  }
});

// ====== Search logic ======
function renderTabs() {
  $('searchTabs').innerHTML = TABS.map(t => `<button class="search-tab${S.tab===t.id?' active':''}${t.id!==3?' disabled':''}" ${t.id!==3?'disabled':''} onclick="selectTab(${t.id})">${t.name}</button>`).join('');
}
function selectTab(id) {
  S.tab = id; renderTabs();
  const td = getTabData();
  if (td.books.length > 0) renderResults($('app')); else renderSearchEmpty($('app'));
}

function applyFilters() {
  const td = getTabData();
  let list = td.books.slice();
  if (td.tagFilter) list = list.filter(b => bookTags(b).join(',').toLowerCase().includes(td.tagFilter.toLowerCase()));
  if (td.sortBy==='read') list.sort((a,b)=>(b.ReadCount||0)-(a.ReadCount||0));
  else if (td.sortBy==='words') list.sort((a,b)=>(b.WordCount||0)-(a.WordCount||0));
  else if (td.sortBy==='chapters') list.sort((a,b)=>(b.ChapterCount||0)-(a.ChapterCount||0));
  td.filtered = list;
}

async function doSearch() {
  const hash = location.hash.slice(1)||'';
  if (hash.startsWith('shelf')) { _shelfFilter = $('searchInput').value.trim(); _applyShelfFilter(); return; }
  const key = $('searchInput').value.trim();
  if (!key) {
    const path = (location.hash.slice(1) || 'discover').split('?')[0];
    if (path !== 'shelf' && path !== 'discover') navigate('discover');
    return;
  }
  // Compare the decoded query. Raw hash text can differ by percent-encoding
  // and then assigning location.hash does not fire hashchange, so the fetch
  // never starts and the page stays blank under the title 「搜索」.
  if (currentSearchQuery() !== key) {
    const before = location.hash;
    location.hash = `search?q=${encodeURIComponent(key)}`;
    if (location.hash !== before) return;
  }
  if (typeof abortDiscoverLoads === 'function') abortDiscoverLoads();
  S.key = key; S.loading = true; S.error = '';
  const req = ++_searchReq;
  saveTabData({ books: [], filtered: [], offset: 0, hasMore: false, sortBy: 'default', tagFilter: '' });
  hideSuggest(); addHistory(key); updateClearBtn(); paintSearch();
  try {
    const r = await fetchWithTimeout(`${API}/api/search?key=${encodeURIComponent(key)}&tab_type=${S.tab}`, {}, 30000);
    const data = await r.json();
    if (req !== _searchReq || !searchStillCurrent(key)) { S.loading = false; return; }
    const td = getTabData();
    const batch = data.code===200 && Array.isArray(data.data) ? data.data : [];
    td.books = batch;
    td.hasMore = data.code===200 ? searchHasMore(data, batch) : false;
    S.error = data.code===200 ? '' : '搜索失败，请稍后重试';
    applyFilters();
  } catch(e) {
    if (req !== _searchReq || !searchStillCurrent(key)) { S.loading = false; return; }
    getTabData().books = [];
    S.error = '搜索失败，请稍后重试';
  }
  S.loading = false;
  if (req === _searchReq && searchStillCurrent(key)) paintSearch();
}

function addHistory(key) {
  const data = loadData();
  data.searchHistory = data.searchHistory.filter(s => s !== key);
  data.searchHistory.unshift(key);
  if (data.searchHistory.length > 12) data.searchHistory.pop();
  saveSearchHistory(data.searchHistory);
}

function setSort(s) { const td = getTabData(); td.sortBy = td.sortBy===s?'default':s; applyFilters(); paintSearch(); }
function setTag(t) { const td = getTabData(); td.tagFilter = td.tagFilter===t?'':t; applyFilters(); paintSearch(); }

async function loadMore() {
  if (S.loading) return;
  const td = getTabData();
  if (!td.hasMore || !searchStillCurrent(S.key)) return;
  const req = ++_searchReq;
  const key = S.key;
  S.loading = true; S.error = ''; td.offset += 20; paintSearch();
  try {
    const r = await fetchWithTimeout(`${API}/api/search?key=${encodeURIComponent(key)}&tab_type=${S.tab}&offset=${td.offset}`, {}, 30000);
    const data = await r.json();
    if (req !== _searchReq || !searchStillCurrent(key)) { S.loading = false; return; }
    const more = data.code===200 && Array.isArray(data.data) ? data.data : [];
    td.books = td.books.concat(more);
    td.hasMore = data.code===200 ? searchHasMore(data, more) : false;
    if (data.code!==200) S.error = '搜索失败，请稍后重试';
    applyFilters();
  } catch(e) {
    if (req !== _searchReq || !searchStillCurrent(key)) { S.loading = false; return; }
    td.hasMore = false;
    S.error = '搜索失败，请稍后重试';
  }
  S.loading = false;
  if (req === _searchReq && searchStillCurrent(key)) paintSearch();
}

// ====== Search suggest + clear ======
function showSuggest() {
  const hash = location.hash.slice(1)||'';
  if (hash.startsWith('shelf')) return;
  const data = loadData();
  const el = $('searchSuggest');
  el.innerHTML = data.searchHistory.length === 0
    ? '<div class="search-suggest-empty">暂无搜索历史</div>'
    : data.searchHistory.map(s => `<div class="search-suggest-item" data-key="${escapeHtml(s)}"><span>${escapeHtml(s)}</span><span class="del" data-del="${escapeHtml(s)}"><i data-lucide="x" width="12" height="12"></i></span></div>`).join('');
  el.classList.add('open');
  refreshIcons(el);
}
function hideSuggest() { $('searchSuggest').classList.remove('open'); }
function filterSuggest(q) {
  q = q.trim();
  if (!q) { showSuggest(); return; }
  const data = loadData();
  const matched = data.searchHistory.filter(s => s.toLowerCase().includes(q.toLowerCase()));
  const el = $('searchSuggest');
  el.innerHTML = matched.length === 0
    ? '<div class="search-suggest-empty">按回车搜索</div>'
    : matched.map(s => `<div class="search-suggest-item" data-key="${escapeHtml(s)}"><span>${escapeHtml(s)}</span><span class="del" data-del="${escapeHtml(s)}"><i data-lucide="x" width="12" height="12"></i></span></div>`).join('');
  el.classList.add('open');
  refreshIcons(el);
}
function quickSearch(key) { $('searchInput').value = key; doSearch(); }
function delHistory(key) { const data = loadData(); data.searchHistory = data.searchHistory.filter(s => s !== key); saveSearchHistory(data.searchHistory); showSuggest(); }

function onSearchInput() { updateClearBtn(); const hash = location.hash.slice(1)||''; if (hash.startsWith('shelf')) { _shelfFilter = $('searchInput').value.trim(); _applyShelfFilter(); } else { filterSuggest($('searchInput').value); } }
function clearSearch() {
  $('searchInput').value = '';
  updateClearBtn();
  const hash = location.hash.slice(1) || '';
  if (hash.startsWith('shelf')) { _shelfFilter = ''; _applyShelfFilter(); $('searchInput').focus(); return; }
  if (hash.startsWith('search')) { S.key = ''; navigate('discover'); return; }
  $('searchInput').focus();
  showSuggest();
}
function updateClearBtn() { $('searchClear').classList.toggle('visible', $('searchInput').value.length > 0); }

// ====== Global event listeners ======

document.addEventListener('click', function(e) {
  const tab = e.target.closest('[data-discover-tab]');
  if (tab) { selectDiscoverTab(tab.dataset.discoverTab); return; }
  const cat = e.target.closest('[data-cat-id]');
  if (cat && cat.dataset.catId) { openCategory(cat.dataset.catId, cat.dataset.catName || ''); return; }
  const filter = e.target.closest('[data-cat-filter]');
  if (filter) { setCategoryFilter(filter.dataset.catFilter, filter.dataset.catValue || ''); }
});

// Suggest clicks
document.addEventListener('mousedown', function(e) {
  const del = e.target.closest('[data-del]');
  if (del) { e.stopPropagation(); delHistory(del.dataset.del); return; }
  const item = e.target.closest('[data-key]');
  if (item && $('searchSuggest').classList.contains('open')) quickSearch(item.dataset.key);
});

// Unified scroll handler (non-reader pages)
window.addEventListener('scroll', function() {
  const hash = location.hash.slice(1) || 'discover';
  if (hash.startsWith('reader')) return;
  updateProgress();
  const btn = $('backTop');
  if (window.scrollY > 600) btn.classList.add('visible');
  else btn.classList.remove('visible');
});

// System dark mode auto-follow
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (getTheme() === 'auto') applyTheme('auto');
});

// ====== Init ======
initTheme();
renderTabs();
_profileUser = getCachedUser();
_onlineShelf = getCachedShelf();
router();
_tryAutoLogin();

// Service Worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
}
