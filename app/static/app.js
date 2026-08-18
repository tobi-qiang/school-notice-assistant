// Shared helpers ------------------------------------------------------------

const toast = (message) => {
  const el = document.querySelector('#toast');
  if (!el) return;
  el.textContent = String(message);
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 2600);
};

const jsonFetch = async (url, options = {}) => {
  const response = await fetch(url, {
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
};

let appExiting = false;
let heartbeatTimer = null;

const showExitScreen = (finished = false) => {
  const screen = document.querySelector('#exitScreen');
  const title = document.querySelector('#exitTitle');
  const message = document.querySelector('#exitMessage');
  const retry = document.querySelector('#closePageAgain');
  if (screen) screen.hidden = false;
  document.body.classList.add('is-exiting');
  if (!finished) return;
  if (title) title.textContent = '程序已退出';
  if (message) {
    message.textContent = '浏览器没有允许网页自动关闭这个标签页。现在可以直接点击标签页的 ×，不会留下后台进程。';
  }
  if (retry) retry.hidden = false;
};

const requestProgramExit = () => {
  if (appExiting) return;
  appExiting = true;
  if (syncPollTimer) clearTimeout(syncPollTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (typeof abortInflight === 'function') abortInflight();
  document.querySelector('[data-exit]')?.setAttribute('disabled', '');
  showExitScreen(false);

  const body = new Blob([], { type: 'application/json' });
  const queued = typeof navigator.sendBeacon === 'function'
    ? navigator.sendBeacon('/api/exit', body)
    : false;
  if (!queued) {
    fetch('/api/exit', { method: 'POST', keepalive: true }).catch(() => {});
  }

  // Browsers only allow window.close() for script-closeable tabs.  Try it
  // while the user gesture is still active, then leave a clear terminal page.
  window.close();
  setTimeout(() => showExitScreen(true), 450);
};

// Global delegated handlers ------------------------------------------------

document.addEventListener('click', async (event) => {
  const copy = event.target.closest('[data-copy]');
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copy);
    toast('已复制');
  }
  const toggle = event.target.closest('[data-toggle-secret]');
  if (toggle) {
    const input = document.querySelector(toggle.dataset.toggleSecret || '#chatApiKey');
    if (!input) return;
    input.type = input.type === 'password' ? 'text' : 'password';
    toggle.textContent = input.type === 'password' ? '显示' : '隐藏';
  }
  if (event.target.closest('[data-exit]')) {
    if (confirm('退出后正在进行的同步会被取消。确定退出吗？')) {
      requestProgramExit();
    }
  }
});

document.querySelector('#closePageAgain')?.addEventListener('click', () => window.close());

// Sync panel (status poll, sources, events) --------------------------------

const syncButton = document.querySelector('#syncButton');
const stopButton = document.querySelector('#stopButton');
const syncStatus = document.querySelector('#syncStatus');
const syncPanel = document.querySelector('#syncPanel');
const syncMeta = document.querySelector('#syncMeta');
const syncEvents = document.querySelector('#syncEvents');
const syncSources = document.querySelector('#syncSources');
let syncPollTimer = null;
let lastSyncRunning = null;
let reloadScheduled = false;
const syncEventRows = new Map();
const syncEventTimes = new Map();

const formatClock = (timestamp) => {
  const date = new Date(timestamp);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
};

const setSyncRunning = (running) => {
  if (syncSources) syncSources.hidden = !running;
  if (syncEvents) syncEvents.hidden = !running;
};

const showSync = (running, message, state) => {
  if (syncButton) syncButton.disabled = running;
  if (stopButton) stopButton.hidden = !running;
  if (syncStatus && message) syncStatus.textContent = message;
  if (syncPanel && state) {
    syncPanel.dataset.state = state;
    setSyncRunning(running);
  }
};

const renderProgress = (progress = {}) => {
  const hasPosition = Boolean(progress.source_name || progress.page);
  if (syncMeta) {
    syncMeta.hidden = !hasPosition;
    if (hasPosition) {
      const source = progress.source_name
        ? `${progress.source_name}${progress.source_total ? `（${progress.source_index}/${progress.source_total}）` : ''}`
        : '';
      const page = progress.page ? `第 ${progress.page} 页` : '';
      const counts = `已检查 ${progress.checked_count || 0}，已抓取 ${progress.fetched_count || 0}，已知 ${progress.known_count || 0}，失效跳过 ${progress.skipped_count || 0}`;
      syncMeta.textContent = [source, page, counts].filter(Boolean).join(' · ');
    }
  }
  if (syncSources) {
    const sources = Array.isArray(progress.sources) ? progress.sources : [];
    syncSources.hidden = sources.length === 0 || syncPanel?.dataset.state !== 'running';
    sources.forEach((source) => {
      const key = String(source.key || source.index || 'source');
      let row = syncSources.querySelector(`[data-source-key="${CSS.escape(key)}"]`);
      if (!row) {
        row = document.createElement('li');
        row.dataset.sourceKey = key;
        syncSources.appendChild(row);
      }
      const state = String(source.state || 'queued');
      const text = `${source.name || key} · ${state === 'completed' ? '已完成' : state === 'running' ? '处理中' : state === 'failed' ? '失败' : state === 'cancelled' ? '已取消' : '等待中'}`;
      if (row.dataset.state !== state) row.dataset.state = state;
      if (row.textContent !== text) row.textContent = text;
    });
  }
  if (!syncEvents) return;
  const events = Array.isArray(progress.events) ? progress.events.slice(0, 3) : [];
  const isRunning = syncPanel?.dataset.state === 'running';
  syncEvents.hidden = events.length === 0 || !isRunning;
  const wanted = new Set(events.map((item) => String(item.seq)));
  const initial = syncEventRows.size === 0;
  events.forEach((item, slot) => {
    const key = String(item.seq);
    let row = syncEventRows.get(key);
    const isNew = !row;
    if (!row) {
      row = document.createElement('li');
      row.dataset.seq = key;
      const icon = document.createElement('span');
      icon.className = 'event-icon';
      const copy = document.createElement('span');
      copy.className = 'event-copy';
      const title = document.createElement('span');
      title.className = 'event-title';
      const source = document.createElement('span');
      source.className = 'event-source';
      const sourceName = document.createElement('span');
      sourceName.className = 'event-source-name';
      const sourceTime = document.createElement('span');
      sourceTime.className = 'event-source-time';
      source.append(sourceName, sourceTime);
      copy.append(title, source);
      const detail = document.createElement('span');
      detail.className = 'event-detail';
      row.append(icon, copy, detail);
      syncEventTimes.set(key, Date.now());
      syncEventRows.set(key, row);
      syncEvents.appendChild(row);
    }
    const state = String(item.state || 'fetching');
    if (row.dataset.state !== state) {
      row.dataset.state = state;
      row.className = state;
    }
    const values = {
      '.event-icon': state === 'saved' ? '✓' : state === 'skipped' ? '!' : '→',
      '.event-title': String(item.title || '未命名公告'),
      '.event-source-name': String(item.source_name || ''),
      '.event-source-time': String(item.published_date || ''),
      '.event-detail': String(item.detail || ''),
    };
    Object.entries(values).forEach(([selector, value]) => {
      const element = row.querySelector(selector);
      if (element && element.textContent !== value) element.textContent = value;
    });
    if (isNew && !initial) {
      row.style.setProperty('--slot', '-1');
      requestAnimationFrame(() => row.style.setProperty('--slot', String(slot)));
    } else {
      if (initial) row.classList.add('no-transition');
      row.style.setProperty('--slot', String(slot));
      if (initial) requestAnimationFrame(() => row.classList.remove('no-transition'));
    }
  });
  [...syncEventRows].forEach(([key, row]) => {
    if (wanted.has(key)) return;
    row.classList.add('leaving');
    row.style.setProperty('--slot', '3');
    syncEventTimes.delete(key);
    const remove = () => {
      row.remove();
      syncEventRows.delete(key);
    };
    row.addEventListener('transitionend', remove, { once: true });
    setTimeout(remove, 450);
  });
};

// Summary job (batch share-summary generation) ----------------------------

const summaryButton = document.querySelector('#summaryButton');
const stopSummaryButton = document.querySelector('#stopSummaryButton');
const summaryPanel = document.querySelector('#summaryPanel');
const summaryStatus = document.querySelector('#summaryStatus');
const summaryMeta = document.querySelector('#summaryMeta');
let summaryEverRan = false;

const SUMMARY_STATE_LABELS = {
  idle: '尚未生成',
  running: '正在生成摘要…',
  completed: '摘要生成已完成',
  partial: '摘要生成完成，部分项目失败',
  failed: '摘要生成失败',
  cancelled: '已停止生成',
  disabled: '摘要模型未配置',
  stopped: '已停止生成',
};

const setSummaryRunning = (running) => {
  if (summaryButton) summaryButton.disabled = running;
  if (stopSummaryButton) stopSummaryButton.hidden = !running;
};

const renderSummaryJob = (job = {}) => {
  if (!summaryPanel) return;
  const running = Boolean(job.running);
  const total = Number(job.total || 0);
  const processed = Number(job.processed || 0);
  const generated = Number(job.generated || 0);
  const reused = Number(job.reused || 0);
  const failed = Number(job.failed || 0);
  const rawState = String(job.status || (running ? 'running' : 'idle'));
  const hasStats = total > 0 || processed > 0 || generated > 0 || reused > 0 || failed > 0;
  const active = running || rawState !== 'idle' || hasStats || Boolean(job.current_title);
  if (active) summaryEverRan = true;
  const shouldShow = summaryEverRan || active;
  const nextVisible = shouldShow;
  const prevVisible = !summaryPanel.hidden;
  if (nextVisible !== prevVisible) summaryPanel.hidden = !nextVisible;
  if (!shouldShow) return;
  if (summaryPanel.dataset.state !== rawState) summaryPanel.dataset.state = rawState;
  setSummaryRunning(running);
  const defaultMessage = SUMMARY_STATE_LABELS[rawState]
    || (running ? '正在生成摘要…' : '摘要任务已结束');
  const message = String(job.message || '').trim() || defaultMessage;
  if (summaryStatus && summaryStatus.textContent !== message) summaryStatus.textContent = message;
  if (summaryMeta) {
    const statsLine = (total || processed || generated || reused || failed)
      ? `共 ${total} 条 · 已处理 ${processed} · 新生成 ${generated} · 复用 ${reused} · 失败 ${failed}`
      : '';
    const currentLine = job.current_title ? `当前：${job.current_title}` : '';
    const reasonEntries = Object.entries(job.failure_reasons || {})
      .filter(([, count]) => Number(count) > 0)
      .map(([reason, count]) => `${reason} ${count}`);
    const reasonLine = reasonEntries.length ? `失败原因：${reasonEntries.join('、')}` : '';
    const metaText = [statsLine, reasonLine, currentLine].filter(Boolean).join(' · ');
    if (metaText) {
      summaryMeta.hidden = false;
      if (summaryMeta.textContent !== metaText) summaryMeta.textContent = metaText;
    } else {
      summaryMeta.hidden = true;
      if (summaryMeta.textContent !== '') summaryMeta.textContent = '';
    }
  }
};

const scheduleStatusPoll = (delay) => {
  if (appExiting) return;
  if (syncPollTimer) clearTimeout(syncPollTimer);
  syncPollTimer = setTimeout(pollStatus, delay);
};

const pollStatus = async () => {
  if (appExiting) return;
  try {
    const status = await jsonFetch('/api/status');
    const wasRunning = lastSyncRunning === true;
    showSync(Boolean(status.running), status.message || '尚未同步', status.status || 'idle');
    renderProgress(status.progress || {});
    renderSummaryJob(status.summary_job || {});
    lastSyncRunning = Boolean(status.running);
    if (wasRunning && !status.running && status.status === 'completed' && !reloadScheduled) {
      reloadScheduled = true;
      location.reload();
      return;
    }
    scheduleStatusPoll(status.running || status.summary_job?.running ? 1000 : 3000);
  } catch (_error) {
    if (appExiting) return;
    lastSyncRunning = false;
    showSync(false, '本地服务已断开', 'disconnected');
    setSummaryRunning(false);
    scheduleStatusPoll(3000);
  }
};

if (syncButton) syncButton.addEventListener('click', async () => {
  showSync(true, '正在启动同步……', 'running');
  setSyncRunning(true);
  try {
    await jsonFetch('/api/sync', { method: 'POST' });
    lastSyncRunning = true;
    pollStatus();
  } catch (error) {
    showSync(false, error.message, 'failed');
    scheduleStatusPoll(3000);
  }
});

if (stopButton) stopButton.addEventListener('click', async () => {
  try {
    await jsonFetch('/api/sync/stop', { method: 'POST' });
    showSync(true, '正在安全停止……', 'running');
  } catch (error) {
    showSync(false, error.message, 'failed');
  }
});

if (summaryButton) summaryButton.addEventListener('click', async () => {
  if (summaryButton.disabled) return;
  summaryButton.disabled = true;
  summaryEverRan = true;
  if (summaryPanel) summaryPanel.hidden = false;
  if (summaryPanel && summaryPanel.dataset.state !== 'running') summaryPanel.dataset.state = 'running';
  if (summaryStatus) summaryStatus.textContent = '正在启动摘要生成……';
  if (summaryMeta) { summaryMeta.hidden = true; summaryMeta.textContent = ''; }
  try {
    await jsonFetch('/api/share-summaries/start', { method: 'POST' });
    pollStatus();
  } catch (error) {
    setSummaryRunning(false);
    if (summaryPanel) summaryPanel.dataset.state = 'failed';
    if (summaryStatus) summaryStatus.textContent = error.message;
    toast(error.message);
  }
});

if (stopSummaryButton) stopSummaryButton.addEventListener('click', async () => {
  if (stopSummaryButton.disabled) return;
  stopSummaryButton.disabled = true;
  try {
    await jsonFetch('/api/share-summaries/stop', { method: 'POST' });
  } catch (error) {
    toast(error.message);
  } finally {
    stopSummaryButton.disabled = false;
  }
});

if (syncStatus) {
  setSyncRunning(syncPanel?.dataset.state === 'running');
  pollStatus();
}


// Notice list (incremental, abortable) -------------------------------------

const INITIAL_PAGE_SIZE = 15;
const NEXT_PAGE_SIZE = 20;
const PREFETCH_ROOT_MARGIN = '600px 0px';
const SEARCH_DEBOUNCE_MS = 280;

const listEl = document.querySelector('#noticeList');
const sentinelEl = document.querySelector('#noticeSentinel');
const loadMoreBtn = document.querySelector('#noticeLoadMore');
const emptyEl = document.querySelector('#noticeEmpty');
const endEl = document.querySelector('#noticeEnd');
const searchInput = document.querySelector('#searchInput');
const yearInput = document.querySelector('#yearInput');
const sourceSelect = document.querySelector('#sourceSelect');
const sourceButton = document.querySelector('#sourceSelectButton');
const sourceMenu = document.querySelector('#sourceSelectMenu');
const sourceLabel = document.querySelector('#sourceSelectedLabel');
const sourceInput = document.querySelector('#sourceInput');
const sourceOptions = sourceMenu ? Array.from(sourceMenu.querySelectorAll('.source-option')) : [];
const noticeForm = document.querySelector('#noticeSearch');

const listState = {
  q: '',
  source: '',
  year: '',
  nextPage: 1,
  hasMore: true,
  total: 0,
  loading: false,
  seenIds: new Set(),
  inflight: null,
  inflightSeq: 0,
  prefetched: new Map(),
};

const readInitialFilters = () => {
  if (!listEl) return { q: '', source: '', year: '' };
  return {
    q: listEl.dataset.q || '',
    source: listEl.dataset.source || '',
    year: listEl.dataset.year || '',
  };
};

const countInitialCards = () => {
  if (!listEl) return 0;
  return Array.from(listEl.querySelectorAll('.notice-card')).length;
};

const syncSourceUI = () => {
  let selectedLabel = '全部来源';
  sourceOptions.forEach((option) => {
    const active = (option.dataset.source || '') === listState.source;
    option.setAttribute('aria-selected', active ? 'true' : 'false');
    if (active) selectedLabel = option.dataset.label || option.textContent.trim();
  });
  if (sourceLabel) sourceLabel.textContent = selectedLabel;
  if (sourceInput) sourceInput.value = listState.source;
};

const setSourceMenuOpen = (open, focusIndex = null) => {
  if (!sourceButton || !sourceMenu) return;
  sourceMenu.hidden = !open;
  sourceButton.setAttribute('aria-expanded', open ? 'true' : 'false');
  sourceSelect?.classList.toggle('is-open', open);
  if (open && sourceOptions.length) {
    const selected = sourceOptions.findIndex((option) => option.getAttribute('aria-selected') === 'true');
    const index = focusIndex === null ? Math.max(0, selected) : focusIndex;
    sourceOptions[Math.max(0, Math.min(index, sourceOptions.length - 1))].focus();
  }
};

const selectSource = (source) => {
  setSourceMenuOpen(false);
  sourceButton?.focus();
  applyFilters({ q: listState.q, source: source || '', year: listState.year, push: true });
};

const syncFormUI = () => {
  if (searchInput && searchInput.value !== listState.q) searchInput.value = listState.q;
  if (yearInput && yearInput.value !== listState.year) yearInput.value = listState.year;
  syncSourceUI();
};

const syncListClass = () => {
  if (!listEl) return;
  listEl.classList.toggle('is-searching', Boolean(listState.q));
};

const updateEmptyState = () => {
  if (!listEl || !emptyEl) return;
  const totalCards = listEl.querySelectorAll('.notice-card').length;
  emptyEl.hidden = totalCards > 0;
};

const showEndMark = () => {
  if (endEl) endEl.hidden = false;
  if (loadMoreBtn) loadMoreBtn.hidden = true;
};

const hideEndMark = () => {
  if (endEl) endEl.hidden = true;
};

const showLoadMore = (message) => {
  if (!loadMoreBtn) return;
  loadMoreBtn.hidden = false;
  loadMoreBtn.textContent = message || '继续加载';
};

const hideLoadMore = () => {
  if (!loadMoreBtn) return;
  loadMoreBtn.hidden = true;
};

const appendItems = (items) => {
  if (!listEl) return;
  for (const item of items) {
    const id = Number(item && item.id);
    if (!Number.isFinite(id) || listState.seenIds.has(id)) continue;
    listState.seenIds.add(id);
    listEl.appendChild(buildCard(item, Boolean(listState.q)));
  }
  updateEmptyState();
};

const buildCard = (item, searchActive) => {
  const card = document.createElement('li');
  card.className = 'notice-card';
  card.dataset.id = String(item && item.id || '');

  const meta = document.createElement('div');
  meta.className = 'meta';
  const source = document.createElement('span');
  source.className = 'meta-source';
  source.textContent = String((item && item.source_name) || '');
  const date = document.createElement('span');
  date.className = 'meta-date';
  date.textContent = String((item && item.published_date) || '');
  meta.append(source, date);
  card.append(meta);

  const title = document.createElement('h2');
  title.className = 'notice-title';
  const link = document.createElement('a');
  const id = Number(item && item.id);
  link.href = `/notice/${Number.isFinite(id) ? id : ''}`;
  link.textContent = String((item && item.title) || '');
  title.append(link);
  card.append(title);

  const preview = document.createElement('p');
  preview.className = 'notice-preview' + (searchActive ? ' is-context' : '');
  const fragments = Array.isArray(item && item.preview_fragments) ? item.preview_fragments : null;
  if (fragments && fragments.length > 0) {
    fragments.forEach((fragment) => {
      const text = String((fragment && fragment.text) || '');
      if (!text) return;
      if (fragment.match) {
        const mark = document.createElement('mark');
        mark.textContent = text;
        preview.append(mark);
      } else {
        preview.append(document.createTextNode(text));
      }
    });
  } else {
    preview.textContent = String((item && item.preview) || '');
  }
  card.append(preview);

  return card;
};


const consumePageData = (page, data) => {
  const items = Array.isArray(data && data.items) ? data.items : [];
  appendItems(items);
  listState.hasMore = Boolean(data && data.has_more);
  if (data && Number.isFinite(Number(data.total))) listState.total = Number(data.total);
  listState.nextPage = page + 1;
  if (!listState.hasMore) showEndMark();
  else hideEndMark();
  if (listState.hasMore) scheduleIdlePrefetch();
};

const abortInflight = () => {
  if (listState.inflight) {
    listState.inflight.controller.abort();
    listState.inflight = null;
  }
};

const fetchPage = async (page, { prefetch = false } = {}) => {
  const pageSize = page === 1 ? INITIAL_PAGE_SIZE : NEXT_PAGE_SIZE;
  const params = new URLSearchParams({
    q: listState.q,
    source: listState.source,
    year: listState.year,
    page: String(page),
    page_size: String(pageSize),
  });
  const seq = ++listState.inflightSeq;
  const controller = new AbortController();
  abortInflight();
  listState.inflight = { controller, seq, prefetch };
  if (!prefetch) listState.loading = true;

  try {
    const response = await fetch(`/api/notices?${params.toString()}`, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (seq !== listState.inflightSeq) return;
    if (!response.ok) throw new Error(`请求失败 (${response.status})`);
    const data = await response.json().catch(() => ({}));
    if (seq !== listState.inflightSeq) return;
    if (prefetch) {
      listState.prefetched.set(page, data);
      return;
    }
    consumePageData(page, data);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    if (seq === listState.inflightSeq && !prefetch) {
      showLoadMore('继续加载');
      toast(`加载失败：${error.message || error}`);
    }
  } finally {
    if (listState.inflight && listState.inflight.seq === seq) {
      listState.inflight = null;
      if (!prefetch) listState.loading = false;
    }
  }
};

const loadNextPage = () => {
  if (listState.loading || !listState.hasMore) return;
  const targetPage = listState.nextPage;
  if (listState.prefetched.has(targetPage)) {
    const cached = listState.prefetched.get(targetPage);
    listState.prefetched.delete(targetPage);
    consumePageData(targetPage, cached);
    return;
  }
  fetchPage(targetPage);
};

const idleScheduler = (callback) => {
  if (typeof window.requestIdleCallback === 'function') {
    return window.requestIdleCallback(callback, { timeout: 1500 });
  }
  return window.setTimeout(callback, 400);
};

const scheduleIdlePrefetch = () => {
  if (!listState.hasMore) return;
  const target = listState.nextPage;
  if (listState.prefetched.has(target)) return;
  if (listState.inflight && !listState.inflight.prefetch) return;
  idleScheduler(() => {
    if (!listState.hasMore) return;
    if (listState.prefetched.has(target)) return;
    if (listState.loading) return;
    if (listState.inflight) return;
    fetchPage(target, { prefetch: true });
  });
};

const buildUrl = (q, source, year) => {
  const url = new URL(window.location.href);
  const path = url.pathname || '/';
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (source) params.set('source', source);
  if (year) params.set('year', year);
  const query = params.toString();
  return `${path}${query ? `?${query}` : ''}`;
};

const clearListDom = () => {
  if (!listEl) return;
  Array.from(listEl.querySelectorAll('.notice-card')).forEach((node) => node.remove());
};

const applyFilters = ({ q, source, year, push = true, replace = false }) => {
  abortInflight();
  listState.q = q || '';
  listState.source = source || '';
  listState.year = year || '';
  listState.nextPage = 1;
  listState.hasMore = true;
  listState.prefetched.clear();

  clearListDom();
  listState.seenIds.clear();
  updateEmptyState();
  hideEndMark();
  hideLoadMore();
  syncFormUI();
  syncListClass();

  if (push || replace) {
    const url = buildUrl(listState.q, listState.source, listState.year);
    const state = { q: listState.q, source: listState.source, year: listState.year };
    if (replace) history.replaceState(state, '', url);
    else history.pushState(state, '', url);
  }

  fetchPage(1);
};

const debounce = (fn, ms) => {
  let timer = null;
  return (...args) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
};

const handleSearchInput = debounce(() => {
  if (!searchInput) return;
  applyFilters({
    q: searchInput.value.trim(),
    source: listState.source,
    year: listState.year,
    push: false,
    replace: true,
  });
}, SEARCH_DEBOUNCE_MS);

const handleSearchSubmit = (event) => {
  event.preventDefault();
  if (searchInput) {
    applyFilters({
      q: searchInput.value.trim(),
      source: listState.source,
      year: listState.year,
      push: true,
    });
  }
};

const handleYearInput = debounce(() => {
  if (!yearInput) return;
  applyFilters({
    q: listState.q,
    source: listState.source,
    year: yearInput.value.trim(),
    push: true,
  });
}, 150);

const handlePopState = () => {
  const params = new URLSearchParams(window.location.search);
  applyFilters({
    q: params.get('q') || '',
    source: params.get('source') || '',
    year: params.get('year') || '',
    push: false,
  });
};

const initNoticeList = () => {
  if (!listEl) return;
  const initial = readInitialFilters();
  listState.q = initial.q;
  listState.source = initial.source;
  listState.year = initial.year;
  const initialCount = countInitialCards();
  Array.from(listEl.querySelectorAll('.notice-card')).forEach((node) => {
    const id = Number(node.dataset.id);
    if (Number.isFinite(id)) listState.seenIds.add(id);
  });
  const pageSize = Number(listEl.dataset.pageSize) || INITIAL_PAGE_SIZE;
  listState.hasMore = initialCount >= pageSize;
  listState.nextPage = 2;
  syncFormUI();
  syncListClass();
  updateEmptyState();
  if (listState.hasMore) scheduleIdlePrefetch();
};


if (noticeForm) noticeForm.addEventListener('submit', handleSearchSubmit);
if (searchInput) {
  searchInput.addEventListener('input', handleSearchInput);
  searchInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      if (searchInput._debounceTimer) clearTimeout(searchInput._debounceTimer);
      applyFilters({
        q: searchInput.value.trim(),
        source: listState.source,
        year: listState.year,
        push: true,
      });
    }
  });
}
if (yearInput) {
  yearInput.addEventListener('input', handleYearInput);
  yearInput.addEventListener('change', () => {
    applyFilters({
      q: listState.q,
      source: listState.source,
      year: yearInput.value.trim(),
      push: true,
    });
  });
  yearInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      applyFilters({
        q: listState.q,
        source: listState.source,
        year: yearInput.value.trim(),
        push: true,
      });
    }
  });
}
sourceButton?.addEventListener('click', () => {
  setSourceMenuOpen(sourceButton.getAttribute('aria-expanded') !== 'true');
});
sourceButton?.addEventListener('keydown', (event) => {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const last = Math.max(0, sourceOptions.length - 1);
  setSourceMenuOpen(true, event.key === 'ArrowUp' || event.key === 'End' ? last : 0);
});
sourceOptions.forEach((option, index) => {
  option.addEventListener('click', () => selectSource(option.dataset.source || ''));
  option.addEventListener('keydown', (event) => {
    let next = null;
    if (event.key === 'ArrowDown') next = (index + 1) % sourceOptions.length;
    else if (event.key === 'ArrowUp') next = (index - 1 + sourceOptions.length) % sourceOptions.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = sourceOptions.length - 1;
    else if (event.key === 'Escape') {
      event.preventDefault();
      setSourceMenuOpen(false);
      sourceButton?.focus();
      return;
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectSource(option.dataset.source || '');
      sourceButton?.focus();
      return;
    }
    if (next !== null) {
      event.preventDefault();
      sourceOptions[next].focus();
    }
  });
});
document.addEventListener('pointerdown', (event) => {
  if (sourceSelect && !sourceSelect.contains(event.target)) setSourceMenuOpen(false);
});
if (loadMoreBtn) {
  loadMoreBtn.addEventListener('click', () => {
    hideLoadMore();
    fetchPage(listState.nextPage);
  });
}
window.addEventListener('popstate', handlePopState);

let noticeObserver = null;
if (sentinelEl && 'IntersectionObserver' in window) {
  noticeObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) loadNextPage();
    });
  }, { rootMargin: PREFETCH_ROOT_MARGIN });
  noticeObserver.observe(sentinelEl);
} else if (sentinelEl) {
  // Fallback for browsers without IntersectionObserver: poll scroll
  window.addEventListener('scroll', () => {
    if (!sentinelEl) return;
    const rect = sentinelEl.getBoundingClientRect();
    if (rect.top < window.innerHeight + 600) loadNextPage();
  }, { passive: true });
}

initNoticeList();

// Soft mouse halo (respects prefers-reduced-motion) -----------------------

const halo = document.createElement('div');
halo.className = 'mouse-halo';
halo.setAttribute('aria-hidden', 'true');
document.body.appendChild(halo);

const motionQuery = typeof window.matchMedia === 'function'
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : null;

const updateHalo = (x, y) => {
  halo.style.setProperty('--halo-x', `${x}px`);
  halo.style.setProperty('--halo-y', `${y}px`);
};

const handleMouseMove = (event) => {
  if (motionQuery && motionQuery.matches) return;
  updateHalo(event.clientX, event.clientY);
};

window.addEventListener('mousemove', handleMouseMove, { passive: true });


// Share dialog ------------------------------------------------------------

const dialog = document.querySelector('#shareDialog');
const shareButton = document.querySelector('#shareButton');
const candidates = document.querySelector('#shareCandidates');
const shareText = document.querySelector('#shareText');
const shareDateRange = document.querySelector('#shareDateRange');
const shareDateStart = document.querySelector('#shareDateStart');
const shareDateEnd = document.querySelector('#shareDateEnd');
let shareMode = 'new';
const rebuildShare = () => {
  if (!candidates || !shareText) return;
  shareText.value = [...candidates.querySelectorAll('.candidate')]
    .filter((item) => item.querySelector('[type=checkbox]').checked)
    .map((item) => {
      const edited = item.querySelector('[type=text]').value.trim();
      return [edited, item.dataset.url].filter(Boolean).join(' ');
    })
    .join('\n');
};
const appendShareCandidate = (item) => {
  const label = document.createElement('label');
  label.className = 'candidate';
  label.dataset.id = String(item.id);
  label.dataset.url = String(item.original_url || '');
  label.dataset.summary = String(item.share_summary || '');
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = true;
  const content = document.createElement('span');
  content.textContent = String(item.official_title || '');
  const title = document.createElement('input');
  title.type = 'text';
  title.value = String(item.share_summary || item.short_title || item.official_title || '');
  content.appendChild(title);
  label.append(checkbox, content);
  candidates.appendChild(label);
};
const loadShareCandidates = async () => {
  if (!dialog || !candidates) return;
  candidates.textContent = '正在读取候选……';
  try {
    const params = new URLSearchParams({ mode: shareMode });
    if (shareMode === 'range') {
      if (!shareDateStart.value || !shareDateEnd.value) return;
      params.set('start', shareDateStart.value);
      params.set('end', shareDateEnd.value);
    }
    const data = await jsonFetch(`/api/share/candidates?${params}`);
    dialog.dataset.throughRun = String(data.through_run_id || 0);
    dialog.dataset.selectionMode = String(data.selection_mode || shareMode);
    dialog.dataset.dateStart = String(data.date_start || '');
    dialog.dataset.dateEnd = String(data.date_end || '');
    candidates.replaceChildren();
    if (data.items.length) data.items.forEach(appendShareCandidate);
    else {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = '没有待处理的新公告。';
      candidates.appendChild(empty);
    }
    rebuildShare();
  } catch (error) { candidates.textContent = error.message; }
};
if (shareButton) shareButton.addEventListener('click', async () => {
  const today = new Date();
  const localToday = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  shareDateEnd.value ||= localToday;
  shareDateStart.value ||= `${localToday.slice(0, 8)}01`;
  dialog.showModal();
  await loadShareCandidates();
});
document.querySelectorAll('[data-share-mode]').forEach((button) => button.addEventListener('click', async () => {
  shareMode = button.dataset.shareMode;
  document.querySelectorAll('[data-share-mode]').forEach((item) => {
    const active = item === button;
    item.classList.toggle('is-active', active);
    item.setAttribute('aria-selected', String(active));
  });
  shareDateRange.hidden = shareMode !== 'range';
  await loadShareCandidates();
}));
[shareDateStart, shareDateEnd].forEach((input) => input?.addEventListener('change', loadShareCandidates));
if (candidates) candidates.addEventListener('input', rebuildShare);
document.querySelector('#copyShare')?.addEventListener('click', async () => {
  await navigator.clipboard.writeText(shareText.value);
  toast('已复制；确认处理完成后才会推进检查点');
});
document.querySelector('#completeShare')?.addEventListener('click', async () => {
  const items = [...candidates.querySelectorAll('.candidate')].map((item) => ({
    id: Number(item.dataset.id), selected: item.querySelector('[type=checkbox]').checked,
    title: item.querySelector('[type=text]').value.trim(), url: item.dataset.url,
  }));
  try {
    await jsonFetch('/api/share/complete', { method: 'POST', body: JSON.stringify({
      through_run_id: Number(dialog.dataset.throughRun), selection_mode: dialog.dataset.selectionMode || 'new',
      date_start: dialog.dataset.dateStart || '', date_end: dialog.dataset.dateEnd || '',
      items, copied_text: shareText.value,
    }) });
    dialog.close();
    location.reload();
  } catch (error) { toast(error.message); }
});

// Settings ----------------------------------------------------------------

const CAPABILITY_LABEL = { chat: 'Chat', embedding: 'Embedding', rerank: 'Rerank', summary: '摘要模型' };

const capabilityPayload = (form) => {
  const payload = Object.fromEntries(new FormData(form));
  const capability = form.dataset.capability;
  if (capability === 'embedding') payload.embedding_enabled = Boolean(form.embedding_enabled?.checked);
  if (capability === 'rerank') payload.rerank_enabled = Boolean(form.rerank_enabled?.checked);
  if (capability === 'summary') payload.summary_enabled = Boolean(form.summary_enabled?.checked);
  return payload;
};
document.querySelectorAll('form[data-capability]').forEach((form) => form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const capability = form.dataset.capability;
  try {
    await jsonFetch(`/api/settings/${capability}`, {
      method: 'POST', body: JSON.stringify(capabilityPayload(form)),
    });
    toast(`${CAPABILITY_LABEL[capability] || capability} 设置已保存`);
  } catch (error) { toast(error.message); }
}));
document.querySelectorAll('[data-test-ai]').forEach((button) => button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    const form = button.closest('form[data-capability]');
    const data = await jsonFetch(`/api/settings/test/${button.dataset.testAi}`, {
      method: 'POST', body: JSON.stringify(form ? capabilityPayload(form) : {}),
    });
    toast(data.message || '连接成功');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}));


// Single-notice share draft ----------------------------------------------

const singleShareDialog = document.querySelector('#singleShareDialog');
const singleShareText = document.querySelector('#singleShareText');
const singleShareWarning = document.querySelector('#singleShareWarning');
const singleShareStatus = document.querySelector('#singleShareStatus');
const copySingleShare = document.querySelector('#copySingleShare');

const AI_STATUS_LABEL = {
  generated: 'AI 摘要已生成',
  cached: '已从本地缓存读取',
  unavailable: '摘要模型不可用，已使用标题',
  fallback: '已使用标题兜底',
};

document.querySelector('#generateShare')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = '正在生成…';
  singleShareText.value = '';
  singleShareText.disabled = true;
  copySingleShare.disabled = true;
  singleShareWarning.hidden = true;
  singleShareStatus.textContent = '正在生成简短事项摘要……';
  singleShareStatus.hidden = false;
  if (!singleShareDialog.open) singleShareDialog.showModal();
  try {
    const data = await jsonFetch(`/api/notices/${button.dataset.noticeId}/share-draft`, { method: 'POST' });
    singleShareText.value = String(data.text || '');
    singleShareWarning.textContent = String(data.warning || '');
    singleShareWarning.hidden = !data.warning;
    const aiStatus = String(data.ai_status || (data.cached ? 'cached' : 'generated'));
    singleShareStatus.textContent = AI_STATUS_LABEL[aiStatus]
      || (data.cached ? '已从本地缓存读取' : 'AI 摘要已生成');
    singleShareText.disabled = false;
    copySingleShare.disabled = !singleShareText.value;
  } catch (error) {
    singleShareStatus.textContent = '生成失败';
    singleShareWarning.textContent = error.message;
    singleShareWarning.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
});
copySingleShare?.addEventListener('click', async () => {
  await navigator.clipboard.writeText(singleShareText.value);
  toast('单篇分享内容已复制');
});

// JWC auth ----------------------------------------------------------------

document.querySelector('#jwcLogin')?.addEventListener('click', async (event) => {
  event.currentTarget.disabled = true;
  const status = document.querySelector('#jwcAuthStatus');
  if (status) status.textContent = '请在弹出的苏大官方页面完成登录……';
  try {
    await jsonFetch('/api/jwc-auth/login', { method: 'POST' });
    if (status) status.textContent = '已保存登录态';
    toast('教务处登录成功');
  } catch (error) {
    if (status) status.textContent = error.message;
    toast(error.message);
  } finally { event.currentTarget.disabled = false; }
});

document.querySelector('#jwcClear')?.addEventListener('click', async () => {
  await jsonFetch('/api/jwc-auth/clear', { method: 'POST' });
  const status = document.querySelector('#jwcAuthStatus');
  if (status) status.textContent = '尚未登录';
  toast('已清除教务处登录态');
});

// Heartbeat & disconnect --------------------------------------------------

heartbeatTimer = setInterval(() => {
  if (!appExiting) fetch('/api/heartbeat', { method: 'POST', keepalive: true }).catch(() => {});
}, 5000);
fetch('/api/heartbeat', { method: 'POST', keepalive: true }).catch(() => {});
window.addEventListener('pagehide', () => {
  if (!appExiting) navigator.sendBeacon('/api/disconnect');
});

