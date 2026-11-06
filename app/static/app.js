/* global location, fetch, navigator, structuredClone */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    config: null,
    status: null,
    sections: [],
    scans: [],
    events: [],
    tab: 'overview',
    timers: {},
  };

  const THEME_KEY = 'p302-theme';
  const THEME_ORDER = ['auto', 'light', 'dark'];
  const THEME_LABEL = { auto: '自动', light: '浅色', dark: '深色' };
  const SOURCE_LABEL = {
    manual: '手动', cms: 'CMS', 'cms-emby': 'CMS', '115event': '115事件',
    '115recent': '115最近', watch: '目录监听', verify: '复核', schedule: '定时',
  };

  const TITLES = {
    overview: ['概览', '外网 302、115 事件增量入库、每天全量兜底。'],
    library: ['媒体库与整理', '链接入库、TMDB 识别改名归档，以及 Plex 媒体库路径与扫描。'],
    scans: ['扫描记录', '局部扫描与全量同步的入队、执行结果和失败原因。'],
    settings: ['设置', '连接、路径映射、直链方式、自动入库、通知与安全。'],
    logs: ['日志', '302 决策、115 事件、扫描和错误都会记录。'],
  };

  // ------------------------------------------------------------------ 基础
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function toast(message, type) {
    const root = $('#toast-root');
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(6px)'; }, 3200);
    setTimeout(() => el.remove(), 3600);
  }

  async function api(url, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, opts);
    if (res.status === 401) {
      location.href = '/login';
      throw new Error('未登录');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `HTTP ${res.status}`);
    }
    return data;
  }

  function setPill(id, ok, text) {
    const el = $(id);
    el.className = 'pill ' + (ok === 'idle' ? 'idle' : (ok ? 'ok' : 'bad'));
    el.innerHTML = '<i class="dot"></i>' + esc(text);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast('已复制', 'ok')).catch(() => fallbackCopy(text));
    } else {
      fallbackCopy(text);
    }
  }
  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast('已复制', 'ok'); } catch (e) { toast('复制失败', 'bad'); }
    ta.remove();
  }

  // ------------------------------------------------------------------ 主题
  function themeMode() {
    try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { return 'auto'; }
  }

  function applyTheme(mode, save) {
    if (THEME_ORDER.indexOf(mode) < 0) mode = 'auto';
    if (save !== false) {
      try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* ignore */ }
    }
    const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = mode === 'auto' ? (dark ? 'dark' : 'light') : mode;
    document.documentElement.dataset.themeMode = mode;
    const icon = mode === 'auto' ? 'i-auto' : (mode === 'light' ? 'i-sun' : 'i-moon');
    $$('[data-theme-toggle]').forEach((btn) => {
      const use = btn.querySelector('use');
      if (use) use.setAttribute('href', '#' + icon);
      btn.title = '主题：' + THEME_LABEL[mode] + '（点击切换）';
    });
    $$('[data-theme-seg] button').forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  }

  function cycleTheme() {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(themeMode()) + 1) % THEME_ORDER.length];
    applyTheme(next);
    toast('外观：' + THEME_LABEL[next], 'ok');
  }

  // ------------------------------------------------------------------ 路由
  function switchTab(tab) {
    if (!TITLES[tab]) tab = 'overview';
    state.tab = tab;
    $$('.nav a, .tabbar a').forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + tab));
    $('#page-title').textContent = TITLES[tab][0];
    $('#page-sub').textContent = TITLES[tab][1];
    if (location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
    if (tab === 'settings' && state.config) renderSettings();
    if (tab === 'logs') loadLogs();
    if (tab === 'library') renderLibraries();
    if (tab === 'scans') loadScans();
  }

  // ------------------------------------------------------------------ 数据
  async function loadAll(silent) {
    try {
      const [status, cfg, sections, scans, events] = await Promise.all([
        api('/api/status'),
        api('/api/config'),
        api('/api/plex/sections').catch(() => ({ data: [] })),
        api('/api/scan/history?limit=50').catch(() => ({ data: [] })),
        api('/api/115/events?limit=8').catch(() => ({ data: [] })),
      ]);
      state.status = status.data;
      state.config = cfg.data;
      state.sections = sections.data || [];
      state.scans = scans.data || [];
      state.events = events.data || [];
      renderStatus();
      renderOverviewScans();
      renderEvents();
      renderFullStatus();
      renderLibraries();
      renderScans();
      if (state.tab === 'settings' && !document.querySelector('#settings-body .form-section')) renderSettings();
      if (!silent && state.status.default_password) {
        toast('当前还是默认密码 admin，请到「设置 → 安全」修改', 'bad');
      }
    } catch (err) {
      toast(err.message || '加载失败', 'bad');
    }
  }

  async function refreshStatusOnly() {
    try {
      const [res, ev] = await Promise.all([
        api('/api/status'),
        api('/api/115/events?limit=8').catch(() => ({ data: [] })),
      ]);
      state.status = res.data;
      state.events = ev.data || [];
      renderStatus();
      renderEvents();
      renderFullStatus();
    } catch (e) { /* ignore */ }
  }

  async function loadScans() {
    try {
      const res = await api('/api/scan/history?limit=50');
      state.scans = res.data || [];
      renderScans();
      renderOverviewScans();
    } catch (err) { toast(err.message, 'bad'); }
  }

  async function loadLogs() {
    try {
      const keyword = $('#log-keyword').value.trim();
      const res = await api('/api/logs?lines=400&keyword=' + encodeURIComponent(keyword));
      const box = $('#log-box');
      const lines = res.data || [];
      box.innerHTML = lines.length ? lines.map((line) => {
        let cls = 'lv-INFO';
        if (line.indexOf('| WARNING') >= 0) cls = 'lv-WARNING';
        if (line.indexOf('| ERROR') >= 0) cls = 'lv-ERROR';
        return `<span class="${cls}">${esc(line)}</span>`;
      }).join('\n') : '<span class="lv-INFO">暂无日志</span>';
      box.scrollTop = box.scrollHeight;
    } catch (err) { /* 静默 */ }
  }

  // ------------------------------------------------------------------ 概览
  function renderStatus() {
    const s = state.status || {};
    const plex = s.plex || {};
    const u115 = s.u115 || {};
    const cd2 = s.cd2 || {};

    setPill('#pill-plex', !!plex.ok, plex.ok ? 'Plex 已连接' : 'Plex 未连接');
    setPill('#pill-115', !!u115.cookie_set, u115.cookie_set ? '115 Cookie 已配置' : '115 未配置');
    setPill('#pill-cd2', cd2.ok ? true : 'idle', cd2.ok ? 'CD2 已连接' : 'CD2 未连接');

    const stats = s.stats || {};
    const today = stats.today || {};
    $('#stat-today').textContent = today.redirects || 0;
    $('#stat-total').textContent = (stats.totals && stats.totals.redirects) || 0;
    $('#stat-scans').textContent = stats.scans_today || 0;

    const ev = s.events || {};
    const evSummary = ev.summary || {};
    const pending = Number(ev.pending || 0);
    $('#stat-pending').textContent = pending;
    if (ev.enabled === false) $('#stat-pending-sub').textContent = '115 事件监听已关闭';
    else if (!ev.cookie_set) $('#stat-pending-sub').textContent = '未配置 115 Cookie';
    else if (evSummary.at) $('#stat-pending-sub').textContent = `上轮入队 ${evSummary.queued || 0} · 复核 ${ev.verify || 0}`;
    else $('#stat-pending-sub').textContent = '等待首轮检查';

    $('#side-version').textContent = 'v' + (s.version || '1.2.0');

    const watch = s.watch || {};
    const full = s.full || {};
    const rows = [
      ['Plex 服务器', plex.ok ? `v${plex.version || ''} · ${plex.name || ''}` : (plex.error || '未连接'), plex.ok, '#i-play'],
      ['115 直链（Cookie）', u115.cookie_set ? 'Cookie 已配置 · 外网 / 内网 302 可用' : '未配置 Cookie，全部回源 Plex', !!u115.cookie_set, '#i-link'],
      ['115 操作事件', !ev.cookie_set ? '未配置 Cookie（设置 → 115 授权）'
        : (evSummary.first_run ? '已记录游标，等待新事件'
          : (evSummary.at ? `上轮 ${evSummary.checked || 0} 条 / 待扫 ${pending}` : '等待首轮检查')),
        !!ev.cookie_set && evSummary.ok !== false, '#i-events'],
      ['播放索引', (s.index && s.index.count)
        ? `${s.index.count} 个文件 · 已扫 ${s.index.dirs || 0} 个目录 · 今日 115 请求 ${s.u115_calls_today || 0}`
        : `未建立 · 今日 115 请求 ${s.u115_calls_today || 0}（设置 → 播放索引）`,
        (s.index && s.index.count) ? true : 'idle', '#i-link'],
      ['链接入库', (s.ingest && s.ingest.enabled) ? `已启用 · 转存到 ${s.ingest.target || '未设置'}` : '未启用（企业微信发链接）', (s.ingest && s.ingest.enabled) ? true : 'idle', '#i-link'],
      ['CloudDrive2', cd2.ok ? `${cd2.user_name || ''} · ${cd2.ready ? '就绪' : '未就绪'}` : '未连接（Token 可选，不影响 Plex 扫描）', cd2.ok ? true : 'idle', '#i-scan'],
      ['CD2 目录监听', watch.enabled === false ? '已关闭（默认，事件已覆盖）' : (watch.at ? `${watch.targets || 0} 个根目录 · 上轮入队 ${watch.queued || 0}` : '等待首轮索引'), watch.enabled === false ? 'idle' : (watch.at ? true : 'idle'), '#i-logs'],
      ['全量同步', full.running ? `正在跑 ${full.done || 0}/${full.total || 0}` : (full.finished_at ? `上次 ${fmtTime(full.finished_at)} · ${full.done || 0}/${full.total || 0} 成功` : '还没跑过'), full.running ? true : (full.finished_at ? !full.failed : 'idle'), '#i-full'],
      ['企业微信', (s.notify && s.notify.wecom) ? '已启用 · 增量入库 / 全量 / 错误都会通知' : '未启用（设置 → 通知）', (s.notify && s.notify.wecom) ? true : 'idle', '#i-bell'],
      ['局域网直连', s.lan_redirect ? '已开启 · 电视也直接从云盘拉流' : '未开启 · 内网走 NAS 中转（设置 → 网络与安全）', s.lan_redirect ? true : 'idle', '#i-home'],
    ];
    renderWatchStatus(watch, s);

    $('#status-list').innerHTML = rows.map((r) => {
      const state = r[2] === 'idle'
        ? '<span class="badge">未启用</span>'
        : (r[2] ? '<span class="badge ok">正常</span>' : '<span class="badge bad">异常</span>');
      return `
      <div class="status-row">
        <div class="status-ico"><svg><use href="${r[3]}"></use></svg></div>
        <div class="status-main"><b>${esc(r[0])}</b><span>${esc(r[1])}</span></div>
        <div class="status-val">${state}</div>
      </div>`;
    }).join('');
  }

  function renderWatchStatus(watch, s) {
    const box = $('#watch-status');
    if (!box) return;
    const on = s.scan_watch_on;
    if (!on) {
      box.innerHTML = '<div class="callout">未启用。日常入库靠 115 操作事件就够了；需要兜底时到「设置 → CD2 目录监听」打开。</div>';
      return;
    }
    if (!watch || !watch.at) {
      box.innerHTML = '<div class="callout info">已启用，等待首轮遍历…</div>';
      return;
    }
    box.innerHTML = `<div class="watch-facts">
      <div><span>上轮</span><b>${fmtTime(watch.at)}</b></div>
      <div><span>根目录</span><b>${watch.targets || 0}</b></div>
      <div><span>视频文件</span><b>${watch.files || 0}</b></div>
      <div><span>新增</span><b>${watch.new || 0}</b></div>
      <div><span>入队</span><b>${watch.queued || 0}</b></div>
      <div><span>耗时</span><b>${watch.elapsed != null ? watch.elapsed + 's' : '—'}</b></div>
    </div>${watch.first_run ? '<div class="desc" style="margin-top:8px;font-size:11px;color:var(--muted-2)">首次运行只建索引，不触发扫描。</div>' : ''}`;
  }

  function renderOverviewScans() {
    const body = $('#recent-scans');
    if (!body) return;
    const rows = (state.scans || []).slice(0, 6);
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="3"><div class="empty">暂无记录</div></td></tr>';
      return;
    }
    body.innerHTML = rows.map((r) => `
      <tr>
        <td class="muted" data-label="时间">${fmtTime(r.created_at)}</td>
        <td data-label="路径"><span class="path" title="${esc(r.path)}">${esc(shortPath(r.path, 42))}</span></td>
        <td data-label="状态">${statusBadge(r.status)}</td>
      </tr>`).join('');
  }

  // ------------------------------------------------------------------ 115 事件 / 全量
  function eventIcon(type) {
    if (type === 22) return '#i-trash';
    if (type === 17 || type === 18 || type === 20) return '#i-library';
    if (type === 5 || type === 6) return '#i-scan';
    if (type === 14) return '#i-link';
    return '#i-plus';
  }

  function eventLabel(type) {
    return {
      1: '上传图片', 2: '上传', 3: '收藏', 4: '收藏', 5: '移动', 6: '移动',
      14: '接收', 17: '新建目录', 18: '复制目录', 20: '目录改名',
      22: '删除', 23: '复制', 24: '改名',
    }[type] || '变更';
  }

  function renderEvents() {
    const list = $('#event-list');
    if (!list) return;
    const items = state.events || [];
    if (!items.length) {
      list.innerHTML = '<div class="empty">还没有记录。填好 115 Cookie 后，这里会显示上传 / 移动 / 改名 / 复制 / 删除。</div>';
      return;
    }
    list.innerHTML = items.slice(0, 8).map((e) => {
      const target = e.target || e.plex_path || e.cloud_path || '';
      return `<div class="event-item">
        <div class="event-ico"><svg><use href="${eventIcon(e.type)}"></use></svg></div>
        <div class="event-main">
          <b>${esc(e.name)}</b>
          <span title="${esc(target)}">${esc(eventLabel(e.type))}${target ? ' · ' + esc(shortPath(target, 46)) : ''}</span>
        </div>
        <div class="event-time">${fmtTime(e.created_at || e.seen_at).slice(6, 11)}</div>
      </div>`;
    }).join('');
  }

  function renderFullStatus() {
    const box = $('#full-status');
    const f = (state.status && state.status.full) || {};
    if (!box) return;
    if (f.running) {
      const percent = f.total ? Math.round((f.done / f.total) * 100) : 0;
      box.innerHTML = `<div class="callout info">正在整库扫描 ${f.done || 0}/${f.total || 0}${f.current ? ' · ' + esc(f.current) : ''}</div>
        <div style="height:6px;border-radius:999px;background:var(--hover);margin-top:10px;overflow:hidden">
          <i style="display:block;height:100%;width:${percent}%;background:linear-gradient(90deg,var(--accent),var(--accent-2))"></i>
        </div>`;
      return;
    }
    if (f.finished_at) {
      box.innerHTML = `<div class="callout ${f.failed ? '' : 'info'}">
        ${fmtTime(f.finished_at)} 完成 · ${f.done || 0}/${f.total || 0} 个媒体库${f.failed ? ' · 失败 ' + f.failed : ''}${f.elapsed ? ' · ' + Math.round(f.elapsed) + 's' : ''}
      </div><div class="desc" style="margin-top:8px;font-size:11px;color:var(--muted-2)">来源：${esc(SOURCE_LABEL[f.source] || f.source || '—')}</div>`;
      return;
    }
    box.innerHTML = '<div class="empty">还没有跑过全量同步</div>';
  }

  function shortPath(path, max) {
    path = String(path || '');
    if (path.length <= max) return path;
    const parts = path.split('/');
    if (parts.length > 3) return '…/' + parts.slice(-2).join('/');
    return path.slice(0, max) + '…';
  }

  function statusBadge(status) {
    const map = {
      done: ['ok', '完成'], running: ['run', '执行中'], queued: ['', '排队'],
      failed: ['bad', '失败'],
    };
    const item = map[status] || ['', status || '—'];
    return `<span class="badge ${item[0]}">${esc(item[1])}</span>`;
  }

  // ------------------------------------------------------------------ 媒体库
  async function submitIngest(btn) {
    const el = $('#ingest-text');
    const out = $('#ingest-result');
    const text = (el && el.value || '').trim();
    if (!text) { toast('先粘贴链接', 'bad'); return; }
    if (btn) btn.disabled = true;
    if (out) out.textContent = '提交中…';
    try {
      const res = await api('/api/ingest', { method: 'POST', body: { text } });
      const items = (res.data && res.data.results) || [];
      const ok = items.filter((x) => x.ok).length;
      const msg = `已提交 ${ok}/${items.length} 条到 ${res.data.target || ''}`;
      if (out) out.textContent = msg;
      toast(msg, ok ? 'ok' : 'bad');
      if (el) el.value = '';
      loadOrganize();
    } catch (err) {
      if (out) out.textContent = err.message || '入库失败';
      toast(err.message || '入库失败', 'bad');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderLibraries() {
    const body = $('#lib-body');
    if (!body) return;
    const secs = state.sections || [];
    const watchList = (state.config && state.config.scan && state.config.scan.watch_sections) || [];
    const watchNone = watchList.indexOf('__none__') >= 0;
    secs.forEach((s) => { s.watching = watchNone ? false : (!watchList.length || watchList.includes(String(s.key))); });
    if (!secs.length) {
      body.innerHTML = '<tr><td colspan="6"><div class="empty">没有读到媒体库，检查 Plex 地址和 Token</div></td></tr>';
      return;
    }
    body.innerHTML = secs.map((s) => `
      <tr>
        <td data-label="媒体库"><b>${esc(s.title)}</b></td>
        <td data-label="类型"><span class="badge ${s.type === 'movie' ? 'movie' : 'show'}">${s.type === 'movie' ? '电影' : s.type === 'show' ? '剧集' : esc(s.type)}</span></td>
        <td data-label="路径">${(s.locations || []).map((p) => `<div class="path mono">${esc(p)}</div>`).join('') || '<span class="muted">—</span>'}</td>
        <td data-label="影片数">${s.count || 0}</td>
        <td data-label="自动监听"><label class="switch"><input type="checkbox" data-lib="${esc(s.key)}" ${s.watching ? 'checked' : ''}><i></i></label></td>
        <td data-label="操作"><button class="btn sm" data-scan-lib="${esc(s.key)}" data-scan-path="${esc((s.locations || [])[0] || '')}">扫描</button></td>
      </tr>`).join('');

    $$('#lib-body input[data-lib]').forEach((el) => {
      el.addEventListener('change', async () => {
        const checked = $$('#lib-body input[data-lib]').filter((x) => x.checked).map((x) => x.dataset.lib);
        const all = $$('#lib-body input[data-lib]').length;
        const payload = checked.length === 0 ? ['__none__'] : (checked.length === all ? [] : checked);
        try {
          await api('/api/watch/sections', { method: 'POST', body: { sections: payload } });
          if (state.config && state.config.scan) state.config.scan.watch_sections = payload;
          state.sections.forEach((s) => { s.watching = checked.includes(String(s.key)); });
          toast(checked.length ? '已更新监听媒体库' : '已关闭全部自动监听', 'ok');
        } catch (err) { toast(err.message, 'bad'); }
      });
    });
    $$('#lib-body [data-scan-lib]').forEach((el) => {
      el.addEventListener('click', () => scanPath(el.dataset.scanPath, el.dataset.scanLib));
    });
  }

  // ------------------------------------------------------------------ 扫描
  function renderScans() {
    const body = $('#scan-body');
    if (!body) return;
    const rows = state.scans || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6"><div class="empty">还没有扫描记录</div></td></tr>';
      return;
    }
    body.innerHTML = rows.map((r) => `
      <tr>
        <td class="muted" style="white-space:nowrap" data-label="时间">${fmtTime(r.created_at)}</td>
        <td data-label="路径"><span class="path mono" title="${esc(r.path)}">${esc(r.path)}</span></td>
        <td data-label="媒体库">${esc(r.section_name || '—')}</td>
        <td data-label="来源"><span class="badge ${r.kind === 'full' ? 'full' : ''}">${esc(SOURCE_LABEL[r.source] || r.source || 'manual')}</span></td>
        <td data-label="状态">${statusBadge(r.status)}</td>
        <td class="muted" data-label="信息">${esc(r.message || '')}</td>
      </tr>`).join('');
  }

  async function scanPath(path, sectionId) {
    if (!path) { toast('这条媒体库没有配置路径', 'bad'); return; }
    try {
      const res = await api('/api/scan', { method: 'POST', body: { path, section_id: sectionId } });
      toast(res.data && res.data.duplicate ? '90 秒内已提交过，已忽略' : '已加入扫描队列', 'ok');
      setTimeout(loadScans, 900);
    } catch (err) { toast(err.message, 'bad'); }
  }

  function scanDialog() {
    const root = $('#modal-root');
    root.innerHTML = `
      <div class="modal-mask">
        <div class="modal">
          <div class="modal-head"><h3>扫描一个路径</h3><button class="icon-btn" data-close><svg><use href="#i-x"></use></svg></button></div>
          <div class="modal-body">
            <p>填 Plex 里看到的绝对路径。目录（电影文件夹 / 剧集文件夹）最稳；不会触发全库扫描。</p>
            <div class="field"><label>Plex 路径</label>
              <input class="input" id="scan-path-input" placeholder="/mnt/cd2/115/电影/沙丘2 (2024)">
            </div>
            <div class="field" style="margin-top:10px"><label>媒体库（可留空自动匹配）</label>
              <select id="scan-section-select"><option value="">自动匹配</option>${state.sections.map((s) => `<option value="${esc(s.key)}">${esc(s.title)}</option>`).join('')}</select>
            </div>
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
              <button class="btn ghost" data-close>取消</button>
              <button class="btn primary" id="scan-go">开始扫描</button>
            </div>
          </div>
        </div>
      </div>`;
    root.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeModal));
    root.querySelector('.modal-mask').addEventListener('click', (e) => { if (e.target.classList.contains('modal-mask')) closeModal(); });
    $('#scan-go').addEventListener('click', async () => {
      const path = $('#scan-path-input').value.trim();
      const sid = $('#scan-section-select').value;
      if (!path) { toast('请填写路径', 'bad'); return; }
      closeModal();
      await scanPath(path, sid);
    });
    setTimeout(() => $('#scan-path-input').focus(), 50);
  }

  // ------------------------------------------------------------------ 设置
  function input(id, value, placeholder, type) {
    return `<input class="input" id="${id}" type="${type || 'text'}" value="${esc(value == null ? '' : value)}" placeholder="${esc(placeholder || '')}">`;
  }
  function switchLine(title, desc, id, checked) {
    return `<div class="switch-line"><div class="txt"><b>${esc(title)}</b><span>${esc(desc)}</span></div>
      <label class="switch"><input type="checkbox" id="${id}" ${checked ? 'checked' : ''}><i></i></label></div>`;
  }
  function section(title, desc, body, single) {
    const head = desc ? `<p>${desc}</p>` : '';
    return `<div class="form-section"><div class="form-section-head"><h3>${esc(title)}</h3>${head}</div>
      <div class="form-body ${single ? 'single' : ''}">${body}</div></div>`;
  }

  // 数字 + 单位（分钟/小时），和 115 秒传助手里的时间设置一样：想填多少填多少
  function dur(id, seconds) {
    const total = Math.max(60, Number(seconds) || 60);
    const useHour = total >= 3600 && total % 3600 === 0;
    const value = useHour ? total / 3600 : Math.max(1, Math.round(total / 60));
    return `<div class="dur">
      <input class="input" id="${id}" type="number" min="1" step="1" value="${value}">
      <select id="${id}-unit">
        <option value="60" ${useHour ? '' : 'selected'}>分钟</option>
        <option value="3600" ${useHour ? 'selected' : ''}>小时</option>
      </select>
    </div>`;
  }

  function durValue(id, fallback) {
    const el = $('#' + id);
    const unit = $('#' + id + '-unit');
    const num = Number((el && el.value) || 0);
    const mul = Number((unit && unit.value) || 60);
    const seconds = Math.round(num * mul);
    return seconds > 0 ? seconds : fallback;
  }

  function renderSettings() {
    const c = state.config;
    if (!c) return;
    const mounts = (c.mounts && c.mounts.length ? c.mounts : [{ name: 'CD2', plex_prefix: '', cloud_prefix: '/', cd2_prefix: '' }]);
    const mountRows = mounts.map((m) => `
      <div class="mount-row">
        <input class="input mount-plex" placeholder="/mnt/cd2/115" value="${esc(m.plex_prefix)}">
        <input class="input mount-cloud" placeholder="/" value="${esc(m.cloud_prefix)}">
        <input class="input mount-cd2" placeholder="/115（可留空）" value="${esc(m.cd2_prefix)}">
        <button class="icon-btn mount-del" title="删除"><svg><use href="#i-trash"></use></svg></button>
      </div>`).join('');

    const u115 = (state.status && state.status.u115) || {};
    const html = `
      ${section('外观', '自动跟随系统，也可以固定浅色 / 深色。只保存在当前浏览器。', `
        <div class="field full">
          <div class="seg" data-theme-seg>
            <button type="button" data-mode="auto"><svg><use href="#i-auto"></use></svg>自动</button>
            <button type="button" data-mode="light"><svg><use href="#i-sun"></use></svg>浅色</button>
            <button type="button" data-mode="dark"><svg><use href="#i-moon"></use></svg>深色</button>
          </div>
          <span class="desc">手机上自动启用底部导航和卡片式列表。</span>
        </div>
      `, true)}

      ${section('Plex 连接', 'Plex 的地址必须能从本容器访问。host 网络下通常是 <code>http://127.0.0.1:32400</code>。', `
        <div class="field"><label>Plex 地址</label>${input('cfg-plex-url', c.plex.url, 'http://127.0.0.1:32400')}</div>
        <div class="field"><label>X-Plex-Token</label>${input('cfg-plex-token', '', c.plex.token_set ? '已保存，留空不修改' : '在 Plex Web 的 URL 里找 X-Plex-Token', 'password')}
          <span class="desc">获取方法：Plex Web 打开任意影片 → 查看页面源码 → 搜索 <code>X-Plex-Token</code>，复制等号后面<b>连续 19~20 位</b>字符，别带 <code>X-Plex-Token=</code> 前缀、空格或引号。<b>「测试 Plex」直接用当前填写值，不必先保存</b>；测完记得点「保存」。这个 token 是临时的，改过 Plex 密码或「登出所有设备」后会作废，届时重新复制即可。</span></div>
        <div class="field full"><div class="field-row"><button class="btn sm" id="btn-test-plex">测试 Plex</button><span class="desc" id="plex-test-result"></span></div></div>
      `)}

      ${section('115（Cookie）', '302 直链、操作事件、最近文件全部走同一个网页 Cookie，不再需要开放平台扫码。', `
        <div class="field full">
          <div class="callout ${u115.cookie_set ? 'info' : ''}">
            当前状态：<b>${u115.cookie_set ? 'Cookie 已保存（' + esc(u115.cookie_hint || '') + '）' : '未配置'}</b>
            ${u115.cookie_set ? ' · 直链和增量入库都可用' : ' · 不填也能用，只是不走 115 直链、也没有事件'}
          </div>
        </div>
        <div class="field full">
          <label>115 网页 Cookie</label>
          <textarea class="input" id="cfg-115-cookie" rows="3" placeholder="${c.u115.cookie_set ? '已保存（' + esc(c.u115.cookie_hint || '') + '），留空不修改' : 'UID=...; CID=...; SEID=...'}"></textarea>
          <span class="desc">浏览器登录 <code>115.com</code> → F12 → Network → 点任意一个 <code>webapi.115.com</code> 请求 → 复制请求头里的整行 Cookie。
          这一个 Cookie 同时用于：<b>取直链</b>（<code>/files/download</code>）、<b>读最近操作</b>（<code>/behavior/detail</code>）、<b>反查目录</b>（<code>/files</code>）。</span>
        </div>
        <div class="field full"><div class="field-row">
          <button class="btn sm" id="btn-115-cookie-test">测试 Cookie</button>
          <button class="btn sm ghost" id="btn-115-cookie-full">测完整链路</button>
          <span class="desc" id="cookie-test-result"></span>
        </div></div>
        <div class="field full"><span class="desc">「测试 Cookie」只发 1 个 115 请求验登录态；「测完整链路」会连续请求最近文件 / 目录 / 直链，115 风控时可能返回 405。<b>同一份 Cookie 别让 CMS 等多个工具同时高频使用</b>；被风控后暂停 5~10 分钟再试，不必重复制 Cookie。</span></div>
      `, true)}

      ${section('播放索引', '把「云盘路径 → pick_code」提前存进本地库。Plex 起播时只发 1 个 115 请求；不建索引的话，一个文件第一次播放要逐级列 3~4 层目录。', `
        <div class="field full">
          <div class="callout info" id="index-box">读取中…</div>
        </div>
        <div class="field full"><div class="field-row">
          <button class="btn sm" id="btn-index-build">建立索引</button>
          <button class="btn sm ghost" id="btn-index-rebuild">重新扫描</button>
          <button class="btn sm ghost" id="btn-index-stop">停止</button>
          <button class="btn sm ghost" id="btn-index-clear">清空</button>
          <span class="desc" id="index-result"></span>
        </div></div>
      `, true)}

      ${section('CloudDrive2', 'CD2 只做「给 Plex 挂载」和「刷目录缓存」。<b>API Token 可以留空</b>：不填就跳过缓存刷新，Plex 照样能扫到文件。', `
        <div class="field"><label>gRPC 地址</label>${input('cfg-cd2-address', c.cd2.address, '127.0.0.1:19798')}</div>
        <div class="field"><label>API Token（可选）</label>${input('cfg-cd2-token', '', c.cd2.token_set ? '已保存，留空不修改' : '不填也行，只是不刷新 CD2 缓存', 'password')}
          <span class="desc">CD2 后台 → 设置 → API Token。填了会在扫描前强制刷新目录，避免 CD2 缓存看不到刚整理的文件。</span></div>
        <div class="field full"><div class="field-row"><button class="btn sm" id="btn-test-cd2">测试 CD2</button><span class="desc" id="cd2-test-result"></span></div></div>
      `)}

      ${section('路径映射', '把 Plex 看到的本地挂载路径，映射成云盘路径（给 115 用）和 CD2 内部路径（给刷新缓存用）。三列都支持留空。', `
        <div class="field full">
          <div class="callout info">例：Plex 里是 <code>/mnt/cd2/115/电影/沙丘2</code>，115 网盘根目录对应 <code>/</code>，CD2 内部是 <code>/115/电影/沙丘2</code>：
          <br>Plex 前缀 <code>/mnt/cd2/115</code> · 云盘前缀 <code>/</code> · CD2 前缀 <code>/115</code>。</div>
        </div>
        <div class="field full" id="mount-list">${mountRows}</div>
        <div class="field full"><button class="btn sm" id="btn-add-mount"><svg><use href="#i-plus"></use></svg>添加一条映射</button></div>
      `, true)}

      ${section('直链方式', '决定 302 的目标从哪里来。115 走 Cookie，最省事；CD2 需要直链权限；AList 适合混合网盘。', `
        <div class="field"><label>直链来源</label>
          <select id="cfg-provider">
            ${[['115', '115（网页 Cookie，推荐）'], ['cd2', 'CloudDrive2 直链'], ['alist', 'AList / OpenList'], ['custom', '自定义 HTTP 接口'], ['none', '不 302，全部回源 Plex']]
              .map((x) => `<option value="${x[0]}" ${c.provider.type === x[0] ? 'selected' : ''}>${x[1]}</option>`).join('')}
          </select>
        </div>
        <div class="field"><label>请求直链时的 User-Agent</label>
          <select id="cfg-ua-mode">
            <option value="client" ${c.provider.ua_mode === 'client' ? 'selected' : ''}>跟随客户端（推荐，115 链接会绑定 UA）</option>
            <option value="fixed" ${c.provider.ua_mode === 'fixed' ? 'selected' : ''}>固定 UA</option>
          </select>
        </div>
        <div class="field"><label>固定 UA</label>${input('cfg-fixed-ua', c.provider.fixed_ua, 'Plex302/1.0')}</div>
        <div class="field full" id="alist-fields">
          <div class="form-body" style="padding:0;grid-template-columns:1fr 1fr">
            <div class="field"><label>AList 地址</label>${input('cfg-alist-base', c.provider.alist_base, 'http://127.0.0.1:5244')}</div>
            <div class="field"><label>AList Token</label>${input('cfg-alist-token', '', c.provider.alist_token_set ? '已保存，留空不修改' : 'AList 后台 → 设置 → 令牌', 'password')}</div>
            <div class="field"><label>AList 公网地址（可选）</label>${input('cfg-alist-public', c.provider.alist_public_base, 'https://alist.example.com')}</div>
          </div>
        </div>
        <div class="field full" id="custom-fields">
          <div class="form-body" style="padding:0;grid-template-columns:1fr 1fr">
            <div class="field"><label>自定义接口 URL</label>${input('cfg-custom-url', c.provider.custom_url, 'http://127.0.0.1:8080/link?path=')}</div>
            <div class="field"><label>Bearer Token（可选）</label>${input('cfg-custom-token', '', c.provider.custom_token_set ? '已保存，留空不修改' : '', 'password')}</div>
          </div>
        </div>
        <div class="field full">
          ${switchLine('强制 https', '把 115 返回的 http 直链改成 https，避免部分客户端拦截混合内容', 'cfg-force-https', c.provider.force_https)}
        </div>
        <div class="field full"><label>测试直链（填 Plex 里的文件路径）</label>
          <div class="field-row">${input('cfg-test-path', '', '/mnt/cd2/115/电影/沙丘2 (2024)/沙丘2.mkv')}<button class="btn sm" id="btn-test-link">测试</button></div>
          <div class="result-box" id="link-test-result" style="margin-top:8px">还没有测试</div>
        </div>
      `, true)}

      ${section('链接入库（企业微信）', '把 115 分享 / 磁力 / ed2k 发到企业微信，自动转存到指定目录。', `
        <div class="field full">
          ${switchLine('启用链接入库', '接收 115 分享链接、磁力、ed2k，自动转存', 'cfg-ingest-enabled', c.ingest.enabled)}
        </div>
        <div class="field"><label>入库目录</label>${input('cfg-ingest-target', c.ingest.target_dir, '')}</div>
        <div class="field"><label>允许的成员 UserID（可选）</label>${input('cfg-ingest-users', c.ingest.allow_users, 'zhangsan,lisi')}
          <span class="desc">留空 = 应用可见范围内所有人都能用。</span></div>
        <div class="field full"><label>企业微信回调地址</label>
          <div class="field-row"><input class="input" id="cfg-wecom-callback" readonly value="${esc((state.status && state.status.wecom_callback) || '')}"><button class="btn sm" id="btn-copy-wecom"><svg><use href="#i-copy"></use></svg>复制</button></div>
          <span class="desc">企业微信 → 应用 → 接收消息 → 设置 API 接收。URL 填这个，Token 和 EncodingAESKey 填下面两栏。</span>
        </div>
        <div class="field"><label>回调 Token</label>${input('cfg-ingest-token', c.ingest.wecom_token, '自己填一串，和企微后台一致')}</div>
        <div class="field"><label>EncodingAESKey</label>${input('cfg-ingest-aes', '', c.ingest.wecom_aes_key_set ? '已保存，留空不修改' : '企微后台随机生成的 43 位', 'password')}</div>
        <div class="field full">
          <div class="callout warn">
            企业微信要求回调地址<b>公网可达</b>（HTTPS 或 80 端口）。手机微信里给这个应用发消息也能用：
            企业微信绑定微信后，消息会进同一个会话。<br>
            指令：直接发链接 → 转存；发「状态」→ 看最近结果。
          </div>
        </div>
      `)}

      ${section('增量入库（115 操作事件）', '115 里上传 / 移动 / 改名 → 「最近操作」出现事件 → 静置合并 → 局部扫描。主通道，比轮询目录可靠。', `
        <div class="field full">
          ${switchLine('启用 115 操作事件监听', '定时拉取上传 / 移动 / 改名 / 复制 / 删除流水，有新视频就局部扫描', 'cfg-events-enabled', c.events.enabled)}
          ${switchLine('首次运行也补扫历史', '默认只记录游标，不补扫历史，避免一装好就扫全库', 'cfg-events-first', c.events.first_scan)}
        </div>
        <div class="field"><label>检查间隔</label>${dur('cfg-events-interval', c.events.interval)}
          <span class="desc">多久去 115 看一次有没有新操作。填 1 分钟就够用；想更省请求可以填 5 分钟。</span></div>
        <div class="field"><label>&nbsp;</label><button class="btn sm" id="btn-events-run"><svg><use href="#i-events"></use></svg>立即检查</button></div>
        <div class="field full"><span class="desc" id="events-run-result"></span></div>
        <div class="field full">
          <div class="callout info">
            以下参数已按 115 的风控节奏固定好，不用调：<br>
            · 同一目录静置 <b>90 秒</b> 再扫，期间每来一个新事件就重新计时（整理没停就一直等）<br>
            · 单目录最多等 <b>10 分钟</b>，避免一直有事件就永远不扫<br>
            · 扫完 <b>3 分钟</b> 后复核一次，接住晚落盘的字幕 / 花絮<br>
            · 每次拉 100 条、最多翻 5 页；任意两次 115 请求至少隔 <b>2.05 秒</b>（和 CMS 的 2 秒对齐）
          </div>
        </div>
      `, true)}

      ${section('CD2 目录监听（兜底）', '和 115 操作事件完全独立：这是“自己去翻 CD2 挂载目录”。默认关闭。', `
        <div class="field full">
          ${switchLine('启用 CD2 目录监听', '按间隔遍历挂载目录，发现新视频就局部扫描', 'cfg-watch-enabled', c.scan.watch_enabled)}
        </div>
        <div class="field"><label>遍历间隔</label>${dur('cfg-watch-interval', c.scan.interval)}
          <span class="desc">多久翻一次目录。库大就填长一点（比如 1 小时）。</span></div>
        <div class="field full"><button class="btn sm" id="btn-watch-run-2"><svg><use href="#i-scan"></use></svg>立即跑一轮</button></div>
        <div class="field full">
          <div class="callout warn">
            CD2 挂的是 115，每翻一层目录都会产生一次 115 请求，库大时容易触发风控（405 / 403）。
            有 115 操作事件时<b>不需要开这个</b>；建议只在 Cookie 失效、事件接口异常时临时打开。
          </div>
        </div>
      `)}

      ${section('全量同步', '把 Plex 所有媒体库整库扫一遍。增量再稳也可能漏，这是最后的保险。', `
        <div class="field full">
          ${switchLine('每天定时全量', '到点自动把所有媒体库扫一遍', 'cfg-full-enabled', c.scan.full_enabled)}
          ${switchLine('启动时也全量一次', '容器重启后如果今天还没跑过，补一次', 'cfg-full-start', c.scan.full_on_start)}
        </div>
        <div class="field"><label>每天几点跑</label><input class="input" id="cfg-full-time" type="time" value="${esc(c.scan.full_time || '04:30')}"></div>
        <div class="field"><label>&nbsp;</label><button class="btn sm" id="btn-full-run-settings"><svg><use href="#i-full"></use></svg>立即全量扫描</button></div>
        <div class="field full"><div class="result-box" id="full-run-result">全量同步会按媒体库逐个调用 Plex <code>/library/sections/&lt;id&gt;/refresh</code>（不带 path）。</div></div>
      `)}

      ${section('通知（企业微信）', '', `
        <div class="form-rows">
          <div class="form-row-line"><label>企业ID</label>${input('cfg-wecom-corpid', c.notify.wecom_corpid, '')}</div>
          <div class="form-row-line"><label>应用Secret</label>${input('cfg-wecom-secret', '', c.notify.wecom_secret_set ? '已保存，留空不修改' : '', 'password')}</div>
          <div class="form-row-line"><label>应用ID</label>${input('cfg-wecom-agentid', c.notify.wecom_agentid, '')}</div>
          <div class="form-row-line"><label>接收人</label>${input('cfg-wecom-touser', c.notify.wecom_touser, '')}</div>
          <div class="form-row-line"><label>API代理</label>${input('cfg-wecom-proxy', c.notify.wecom_proxy, '')}</div>
          <div class="form-row-line"><label>状态</label>
            <label class="switch"><input type="checkbox" id="cfg-wecom-enabled" ${c.notify.wecom_enabled ? 'checked' : ''}><i></i></label>
          </div>
        </div>
        <div class="form-rows" style="margin-top:18px">
          <div class="form-row-line"><span></span><button class="btn sm" id="btn-notify-test"><svg><use href="#i-bell"></use></svg>发送测试消息</button></div>
        </div>
      `, true)}

      ${section('网络与安全', '判断「这个请求是不是局域网」用，决定给他 302 还是回源 Plex。', `
        <div class="field"><label>对外访问地址（可选）</label>${input('cfg-public-url', c.server.public_url, 'https://plex.example.com:32500')}
          <span class="desc">只用于显示 Webhook 地址和文档，不影响代理。</span></div>
        <div class="field"><label>允许 302 的局域网</label>
          <textarea id="cfg-lan" placeholder="192.168.0.0/16&#10;10.0.0.0/8">${esc((c.server.lan_cidrs || []).join('\n'))}</textarea>
          <span class="desc">这些网段默认回源 Plex；想电视也直接连云盘，打开下面的「局域网也 302」。</span></div>
        <div class="field full">
          ${switchLine('局域网也 302', '电视 / 盒子在内网也直接从 115 拉流，NAS 完全不上行；代价是内网也要能连公网', 'cfg-redirect-lan', c.server.redirect_lan)}
          ${switchLine('信任 X-Forwarded-For', '前面有 Lucky / NPM / CDN 时打开；端口直接暴露公网就关掉，防止伪造内网 IP', 'cfg-trust-xff', c.server.trust_forwarded_for)}
          ${switchLine('强制 directPlay', '拦截 Plex decision 请求并改写 directPlay=1，尽量避免服务端转码', 'cfg-force-dp', c.advanced.force_direct_play)}
        </div>
        <div class="field full">
          <div class="callout">修改 WebUI 密码（当前账号 admin）。默认密码是 admin，第一次登录后务必改掉。</div>
          <div class="field-row" style="margin-top:10px">
            ${input('cfg-old-pass', '', '原密码', 'password')}
            ${input('cfg-new-pass', '', '新密码（至少 6 位）', 'password')}
            <button class="btn sm" id="btn-change-pass">修改</button>
          </div>
        </div>
      `, true)}
    `;
    $('#settings-body').innerHTML = html;
    bindSettings();
    loadIndexStatus();
  }

  async function runEvents(btn, out) {
    if (btn) btn.disabled = true;
    if (out) out.textContent = '检查中…';
    try {
      const res = await api('/api/115/events/run', { method: 'POST', body: {} });
      const d = res.data || {};
      const msg = d.first_run
        ? `首次运行：已记录游标（${d.checked || 0} 条），不补扫历史`
        : `检查 ${d.checked || 0} 条 / 新事件 ${d.new || 0} / 待扫 ${d.queued || 0}`;
      toast(msg, 'ok');
      if (out) out.textContent = msg;
      loadAll(true);
      loadScans();
    } catch (err) {
      toast(err.message, 'bad');
      if (out) out.textContent = err.message || '检查失败';
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function runWatch(btn) {
    if (btn) btn.disabled = true;
    try {
      const res = await api('/api/watch/run', { method: 'POST', body: {} });
      const d = res.data || {};
      toast(`目录监听：发现 ${d.new || 0} 个新文件 / 入队 ${d.queued || 0}`, 'ok');
      await loadAll(true);
      loadScans();
    } catch (err) {
      toast(err.message || '执行失败', 'bad');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function buildIndex(force) {
    const btn = force ? $('#btn-index-rebuild') : $('#btn-index-build');
    const out = $('#index-result');
    if (btn) btn.disabled = true;
    if (out) out.textContent = '已提交，后台按 2.05 秒节奏扫描…';
    try {
      await api('/api/index/build', { method: 'POST', body: { force: !!force } });
      toast(force ? '重新扫描已开始' : '索引构建已开始', 'ok');
      loadIndexStatus();
      let tries = 0;
      const timer = setInterval(async () => {
        tries += 1;
        const d = await loadIndexStatus();
        if (!d || !d.running) {
          clearInterval(timer);
          if (btn) btn.disabled = false;
        } else if (tries > 200) {
          clearInterval(timer);
        }
      }, 3000);
    } catch (err) {
      if (out) out.textContent = err.message || '启动失败';
      if (btn) btn.disabled = false;
    }
  }

  async function loadIndexStatus() {
    try {
      const res = await api('/api/index/status');
      renderIndexBox(res.data || {});
      return res.data || {};
    } catch (err) {
      return null;
    }
  }

  function renderIndexBox(d) {
    const box = $('#index-box');
    const out = $('#index-result');
    if (box) {
      const parts = [
        `<b>${d.indexed || 0}</b> 个文件已建索引`,
        `已扫 <b>${d.dirs || 0}</b> 个目录`,
      ];
      if (d.running) parts.push(`<span class="badge">正在扫描</span>`);
      else if (d.done) parts.push(`<span class="badge ok">已完成</span>`);
      box.innerHTML = parts.join(' · ')
        + (d.error ? `<br><span style="color:var(--bad)">${esc(d.error)}</span>` : '')
        + `<br><span class="desc">未建索引也能播，只是第一次播放一个文件会多打 3~4 次 115 列目录。</span>`;
    }
    if (out && d.running) out.textContent = `扫描中：${d.dirs || 0} 个目录 / ${d.files || 0} 个视频`;
  }

  async function runFull(out) {
    try {
      await api('/api/sync/full', { method: 'POST', body: {} });
      toast('全量同步已开始', 'ok');
      if (out) out.textContent = '全量同步已开始，正在逐个媒体库扫描…';
      let tries = 0;
      const timer = setInterval(async () => {
        tries += 1;
        await refreshStatusOnly();
        if (out && state.status && state.status.full) {
          const f = state.status.full;
          out.textContent = f.running
            ? `正在扫描 ${f.done || 0}/${f.total || 0}${f.current ? ' · ' + f.current : ''}`
            : `完成：${f.done || 0}/${f.total || 0} 个媒体库${f.failed ? '，失败 ' + f.failed : ''}`;
        }
        if (tries >= 24 || !(state.status && state.status.full && state.status.full.running)) clearInterval(timer);
      }, 3000);
    } catch (err) {
      toast(err.message, 'bad');
      if (out) out.textContent = err.message || '启动失败';
    }
  }

  function bindSettings() {
    $('#btn-test-plex').addEventListener('click', async () => {
      const el = $('#plex-test-result');
      el.textContent = '测试中…';
      const val = (id) => { const n = $('#' + id); return n ? n.value.trim() : ''; };
      try {
        // 用输入框「当前值」测，不必先点保存（后端只用于本次请求，不落盘）
        const res = await api('/api/plex/test', {
          method: 'POST',
          body: { url: val('cfg-plex-url'), token: val('cfg-plex-token') },
        });
        const d = res.data;
        const src = d.using_input ? '当前填写值' : '已保存配置';
        const tip = d.using_input ? ' · 测试通过，记得点右下角「保存」' : '';
        el.innerHTML = `<span style="color:var(--green)">连接成功（${src}）：${esc(d.friendly_name || '')} v${esc(d.version || '')}，${d.sections} 个媒体库${tip}</span>`;
      } catch (err) { el.innerHTML = `<span style="color:var(--red)">${esc(err.message)}</span>`; }
    });
    $('#btn-test-cd2').addEventListener('click', async () => {
      const el = $('#cd2-test-result');
      el.textContent = '测试中…';
      try {
        const res = await api('/api/cd2/test', { method: 'POST', body: {} });
        el.innerHTML = res.data.ok
          ? `<span style="color:var(--green)">已连接：${esc(res.data.user_name || '')}</span>`
          : `<span style="color:var(--red)">${esc(res.data.message || '连接失败')}</span>`;
      } catch (err) { el.innerHTML = `<span style="color:var(--red)">${esc(err.message)}</span>`; }
    });
    $('#btn-test-link').addEventListener('click', async () => {
      const path = $('#cfg-test-path').value.trim();
      const box = $('#link-test-result');
      if (!path) { toast('先填一个 Plex 文件路径', 'bad'); return; }
      box.className = 'result-box';
      box.textContent = '解析中…';
      try {
        const res = await api('/api/test/link', { method: 'POST', body: { path } });
        if (res.data.ok) {
          box.className = 'result-box ok';
          box.textContent = `云盘路径：${res.data.cloud_path}\n直链：${res.data.url}`;
          copyText(res.data.url);
        } else {
          box.className = 'result-box bad';
          box.textContent = `${res.data.error || '解析失败'}\n云盘路径：${res.data.cloud_path || '—'}`;
        }
      } catch (err) {
        box.className = 'result-box bad';
        box.textContent = err.message;
      }
    });
    $('#btn-add-mount').addEventListener('click', () => {
      const wrap = $('#mount-list');
      const div = document.createElement('div');
      div.className = 'mount-row';
      div.innerHTML = '<input class="input mount-plex" placeholder="/mnt/cd2/115"><input class="input mount-cloud" placeholder="/"><input class="input mount-cd2" placeholder="/115（可留空）"><button class="icon-btn mount-del" title="删除"><svg><use href="#i-trash"></use></svg></button>';
      wrap.appendChild(div);
      bindMountDelete(div);
    });
    bindMountDelete($('#mount-list'));
    const cookieTest = async (full) => {
      const out = $('#cookie-test-result');
      const cookie = ($('#cfg-115-cookie').value || '').trim();
      out.textContent = full ? '完整链路测试中（会连续请求 115）…' : '测试中…';
      try {
        const res = await api('/api/115/cookie/test', { method: 'POST', body: { cookie, full } });
        const d = res.data || {};
        out.textContent = 'Cookie 可用（登录态正常）'
          + (d.count != null ? ` · 根目录 ${d.count} 项` : '')
          + (d.recent_count != null ? ` · 最近文件 ${d.recent_count} 条` : '')
          + (d.events != null ? ` · 操作事件 ${d.events} 条` : '')
          + (d.link ? ' · 直链 OK' : '')
          + (d.recent_error ? `（最近文件：${d.recent_error}）` : '')
          + (d.events_error ? `（事件接口：${d.events_error}）` : '')
          + (d.link_error ? `（直链：${d.link_error}）` : '');
      } catch (err) {
        out.textContent = err.message || 'Cookie 测试失败';
      }
    };
    $('#btn-115-cookie-test').addEventListener('click', () => cookieTest(false));
    const cookieFullBtn = $('#btn-115-cookie-full');
    if (cookieFullBtn) cookieFullBtn.addEventListener('click', () => cookieTest(true));
    const watchRun = $('#btn-watch-run');
    if (watchRun) watchRun.addEventListener('click', () => runWatch(watchRun));
    const watchRun2 = $('#btn-watch-run-2');
    if (watchRun2) watchRun2.addEventListener('click', () => runWatch(watchRun2));
    const indexBuild = $('#btn-index-build');
    if (indexBuild) indexBuild.addEventListener('click', () => buildIndex(false));
    const indexRebuild = $('#btn-index-rebuild');
    if (indexRebuild) indexRebuild.addEventListener('click', () => buildIndex(true));
    const indexStop = $('#btn-index-stop');
    if (indexStop) indexStop.addEventListener('click', async () => {
      try {
        await api('/api/index/stop', { method: 'POST', body: {} });
        toast('索引扫描已停止', 'ok');
        loadIndexStatus();
      } catch (err) { toast(err.message, 'bad'); }
    });
    const indexClear = $('#btn-index-clear');
    if (indexClear) indexClear.addEventListener('click', async () => {
      if (!confirm('清空播放索引？下次播放会重新逐级解析目录。')) return;
      const out = $('#index-result');
      out.textContent = '清空中…';
      try {
        await api('/api/index/clear', { method: 'POST', body: {} });
        out.textContent = '已清空';
        loadIndexStatus();
      } catch (err) { out.textContent = err.message || '清空失败'; }
    });
    const eventsRun = $('#btn-events-run');
    if (eventsRun) eventsRun.addEventListener('click', () => runEvents(eventsRun, $('#events-run-result')));
    const fullBtn = $('#btn-full-run-settings');
    if (fullBtn) fullBtn.addEventListener('click', () => runFull($('#full-run-result')));
    const notifyBtn = $('#btn-notify-test');
    if (notifyBtn) notifyBtn.addEventListener('click', async () => {
      const out = $('#notify-test-result');
      out.textContent = '发送中…';
      try {
        await api('/api/notify/test', { method: 'POST', body: {} });
        out.textContent = '已发送，去企业微信看看';
        toast('测试消息已发送', 'ok');
      } catch (err) { out.textContent = err.message || '发送失败'; }
    });
    const copyWecom = $('#btn-copy-wecom');
    if (copyWecom) copyWecom.addEventListener('click', () => copyText($('#cfg-wecom-callback').value));
    $$('[data-theme-seg] button').forEach((b) => b.addEventListener('click', () => applyTheme(b.dataset.mode)));
    applyTheme(themeMode(), false);
    $('#btn-change-pass').addEventListener('click', async () => {
      const old = $('#cfg-old-pass').value;
      const nw = $('#cfg-new-pass').value;
      try {
        await api('/api/password', { method: 'POST', body: { old, new: nw } });
        toast('密码已修改', 'ok');
        $('#cfg-old-pass').value = ''; $('#cfg-new-pass').value = '';
      } catch (err) { toast(err.message, 'bad'); }
    });
    $('#cfg-provider').addEventListener('change', updateProviderFields);
    updateProviderFields();
  }

  function bindMountDelete(scope) {
    scope.querySelectorAll('.mount-del').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        const rows = $$('#mount-list .mount-row');
        if (rows.length <= 1) { toast('至少保留一条映射', 'bad'); return; }
        btn.closest('.mount-row').remove();
      });
    });
  }

  function updateProviderFields() {
    const type = $('#cfg-provider').value;
    $('#alist-fields').style.display = type === 'alist' ? '' : 'none';
    $('#custom-fields').style.display = type === 'custom' ? '' : 'none';
  }

  function collectConfig() {
    const c = structuredClone(state.config);
    const val = (id) => { const el = $('#' + id); return el ? el.value.trim() : ''; };
    const checked = (id) => { const el = $('#' + id); return el ? el.checked : false; };
    // 设置页没渲染时不要保存：否则所有字段都读到空值，会把配置重置成默认
    if (!$('#cfg-plex-url')) return null;

    c.plex.url = val('cfg-plex-url') || c.plex.url;
    if (val('cfg-plex-token')) c.plex.token = val('cfg-plex-token');
    c.cd2.address = val('cfg-cd2-address');
    if (val('cfg-cd2-token')) c.cd2.token = val('cfg-cd2-token');
    c.provider.type = val('cfg-provider') || c.provider.type;
    c.provider.ua_mode = val('cfg-ua-mode');
    c.provider.fixed_ua = val('cfg-fixed-ua');
    c.provider.alist_base = val('cfg-alist-base');
    if (val('cfg-alist-token')) c.provider.alist_token = val('cfg-alist-token');
    c.provider.alist_public_base = val('cfg-alist-public');
    c.provider.custom_url = val('cfg-custom-url');
    if (val('cfg-custom-token')) c.provider.custom_token = val('cfg-custom-token');
    c.provider.force_https = checked('cfg-force-https');

    c.mounts = $$('#mount-list .mount-row').map((row, i) => ({
      name: 'CD2-' + (i + 1),
      plex_prefix: row.querySelector('.mount-plex').value.trim(),
      cloud_prefix: row.querySelector('.mount-cloud').value.trim() || '/',
      cd2_prefix: row.querySelector('.mount-cd2').value.trim(),
    })).filter((m) => m.plex_prefix);

    c.scan.enabled = true;
    c.scan.first_run_scan = checked('cfg-first-scan');
    c.scan.watch_enabled = checked('cfg-watch-enabled');
    c.scan.interval = durValue('cfg-watch-interval', 1800);

    c.scan.full_enabled = checked('cfg-full-enabled');
    c.scan.full_on_start = checked('cfg-full-start');
    if (val('cfg-full-time')) c.scan.full_time = val('cfg-full-time');

    c.events.enabled = checked('cfg-events-enabled');
    c.events.first_scan = checked('cfg-events-first');
    c.events.interval = durValue('cfg-events-interval', 60);

    c.notify.wecom_enabled = checked('cfg-wecom-enabled');
    c.notify.wecom_corpid = val('cfg-wecom-corpid');
    c.notify.wecom_agentid = val('cfg-wecom-agentid');
    if (val('cfg-wecom-secret')) c.notify.wecom_secret = val('cfg-wecom-secret');
    c.notify.wecom_touser = val('cfg-wecom-touser') || '@all';
    c.notify.wecom_proxy = val('cfg-wecom-proxy');

    c.u115.recent_enabled = c.events.recent_fallback;
    if (val('cfg-115-cookie')) c.u115.cookie = val('cfg-115-cookie');

    c.server.public_url = val('cfg-public-url');
    c.server.lan_cidrs = val('cfg-lan').split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);
    c.server.trust_forwarded_for = checked('cfg-trust-xff');
    c.server.redirect_lan = checked('cfg-redirect-lan');
    c.advanced.force_direct_play = checked('cfg-force-dp');
    return c;
  }

  async function saveConfig() {
    const btn = $('#btn-save');
    btn.disabled = true;
    const old = btn.innerHTML;
    btn.innerHTML = '保存中…';
    try {
      const cfg = collectConfig();
      if (!cfg) { toast('请先打开设置页再保存', 'bad'); return; }
      const res = await api('/api/config', { method: 'POST', body: cfg });
      state.config = res.data;
      toast('配置已保存', 'ok');
      await loadAll(true);
      renderSettings();
    } catch (err) {
      toast(err.message || '保存失败', 'bad');
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  function closeModal() {
    $('#modal-root').innerHTML = '';
  }

  // ------------------------------------------------------------------ 事件
  function bind() {
    $$('.nav a, .tabbar a').forEach((a) => a.addEventListener('click', (e) => {
      e.preventDefault();
      switchTab(a.dataset.tab);
    }));
    window.addEventListener('hashchange', () => switchTab((location.hash || '#overview').slice(1)));

    $('#btn-refresh-all').addEventListener('click', () => { loadAll(); toast('正在刷新', 'ok'); });
    const logout = async () => {
      await api('/api/logout', { method: 'POST', body: {} }).catch(() => {});
      location.href = '/login';
    };
    $('#btn-logout').addEventListener('click', logout);
    const logoutMobile = $('#btn-logout-mobile');
    if (logoutMobile) logoutMobile.addEventListener('click', logout);

    $$('[data-theme-toggle]').forEach((b) => b.addEventListener('click', cycleTheme));
    document.addEventListener('click', (e) => {
    });
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const onChange = () => { if (themeMode() === 'auto') applyTheme('auto', false); };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }

    $('#btn-hero-scan').addEventListener('click', scanDialog);
    $('#btn-hero-full').addEventListener('click', () => runFull(null));
    $('#btn-events-run').addEventListener('click', (e) => runEvents(e.currentTarget, null));
    $('#btn-events-run-2').addEventListener('click', (e) => runEvents(e.currentTarget, null));
    $('#btn-full-run').addEventListener('click', () => runFull(null));
    $('#btn-full-run-2').addEventListener('click', () => runFull(null));
    $('#btn-hero-test').addEventListener('click', () => { switchTab('settings'); setTimeout(() => $('#cfg-test-path') && $('#cfg-test-path').focus(), 80); });
    $('#btn-reload-libs').addEventListener('click', async () => {
      try {
        const res = await api('/api/plex/sections');
        state.sections = res.data || [];
        renderLibraries();
        toast('已重新读取媒体库', 'ok');
      } catch (err) { toast(err.message, 'bad'); }
    });
    $('#btn-scan-refresh').addEventListener('click', loadScans);
    $('#btn-ingest').addEventListener('click', (e) => submitIngest(e.currentTarget));
    $('#btn-run-watch').addEventListener('click', async () => {
      const btn = $('#btn-run-watch'); btn.disabled = true;
      try {
        const res = await api('/api/watch/run', { method: 'POST', body: {} });
        toast(`监听完成：扫描 ${res.data.files || 0} 个，入队 ${res.data.queued || 0}`, 'ok');
        loadScans(); loadAll(true);
      } catch (err) { toast(err.message, 'bad'); }
      finally { btn.disabled = false; }
    });
    $('#btn-save').addEventListener('click', saveConfig);
    $('#btn-log-refresh').addEventListener('click', loadLogs);
    $('#log-keyword').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadLogs(); });

    state.timers.status = setInterval(() => { if (state.tab === 'overview') refreshStatusOnly(); }, 15000);
    state.timers.logs = setInterval(() => {
      if (state.tab === 'logs' && $('#log-auto').checked) loadLogs();
    }, 3000);
    state.timers.scans = setInterval(() => { if (state.tab === 'scans') loadScans(); }, 8000);
  }

  async function boot() {
    applyTheme(themeMode(), false);
    bind();
    const tab = (location.hash || '#overview').slice(1);
    switchTab(tab);
    await loadAll();
  }

  boot();
})();
