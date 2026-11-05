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
    qrTimer: null,
  };

  const THEME_KEY = 'p302-theme';
  const THEME_ORDER = ['auto', 'light', 'dark'];
  const THEME_LABEL = { auto: '自动', light: '浅色', dark: '深色' };
  const SOURCE_LABEL = {
    manual: '手动', cms: 'CMS', 'cms-emby': 'CMS', '115event': '115事件',
    '115recent': '115最近', watch: '目录监听', verify: '复核', schedule: '定时',
  };

  const TITLES = {
    overview: ['概览', '外网 302、115 操作事件增量入库、每天全量兜底。'],
    library: ['媒体库', 'Plex 媒体库路径、自动监听开关和单库扫描。'],
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
    el.className = 'pill ' + (ok ? 'ok' : 'bad');
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
    setPill('#pill-115', !!u115.authorized, u115.authorized ? '115 已授权' : '115 未授权');
    setPill('#pill-cd2', !!cd2.ok, cd2.ok ? 'CD2 已连接' : 'CD2 未连接');

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
      ['115 开放平台', u115.authorized ? `${u115.user_name || '已授权'} · token 有效` : '未授权，外网 302 不可用', !!u115.authorized, '#i-link'],
      ['115 操作事件', !ev.cookie_set ? '未配置 Cookie（设置 → 115 授权）'
        : (evSummary.first_run ? '已记录游标，等待新事件'
          : (evSummary.at ? `上轮 ${evSummary.checked || 0} 条 / 待扫 ${pending}` : '等待首轮检查')),
        !!ev.cookie_set && evSummary.ok !== false, '#i-events'],
      ['CloudDrive2', cd2.ok ? `${cd2.user_name || ''} · ${cd2.ready ? '就绪' : '未就绪'}` : (cd2.message || '未连接'), !!cd2.ok, '#i-scan'],
      ['目录监听', watch.enabled === false ? '已关闭' : (watch.at ? `${watch.targets || 0} 个根目录 · 上轮入队 ${watch.queued || 0}` : '等待首轮索引'), watch.enabled !== false, '#i-logs'],
      ['全量同步', full.running ? `正在跑 ${full.done || 0}/${full.total || 0}` : (full.finished_at ? `上次 ${fmtTime(full.finished_at)} · ${full.done || 0}/${full.total || 0} 成功` : '还没跑过'), full.running ? true : (full.finished_at ? !full.failed : 'idle'), '#i-full'],
      ['企业微信', (s.notify && s.notify.wecom) ? '已启用 · 入库 / 全量 / 错误都会通知' : '未启用（设置 → 通知）', (s.notify && s.notify.wecom) ? true : 'idle', '#i-bell'],
    ];
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
    return `<div class="form-section"><div class="form-section-head"><h3>${esc(title)}</h3><p>${desc}</p></div>
      <div class="form-body ${single ? 'single' : ''}">${body}</div></div>`;
  }

  // 点一下就能选的数字输入（带常用值下拉）
  function combo(id, value, presets, unit) {
    unit = unit || '秒';
    const options = (presets || []).map((v) => {
      let label = `${v} ${unit}`;
      if (unit === '秒') {
        if (v >= 3600) label = `${v / 3600} 小时`;
        else if (v >= 60) label = v % 60 === 0 ? `${v / 60} 分钟` : `${Math.floor(v / 60)} 分 ${v % 60} 秒`;
      }
      return `<button type="button" data-value="${v}"><span>${label}</span><small>${v} ${unit}</small></button>`;
    }).join('');
    return `<div class="combo" data-combo>
      <input class="input" id="${id}" type="number" inputmode="numeric" value="${esc(value == null ? '' : value)}">
      <button type="button" class="combo-arrow" tabindex="-1" aria-label="选择常用值"><svg><use href="#i-chevron"></use></svg></button>
      <div class="combo-menu">${options}</div>
    </div>`;
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

    const u115 = state.status && state.status.u115 || {};
    const html = `
      ${section('Plex 连接', 'Plex 的地址必须能从本容器访问。host 网络下通常是 <code>http://127.0.0.1:32400</code>。', `
        <div class="field"><label>Plex 地址</label>${input('cfg-plex-url', c.plex.url, 'http://127.0.0.1:32400')}</div>
        <div class="field"><label>X-Plex-Token</label>${input('cfg-plex-token', '', c.plex.token_set ? '已保存，留空不修改' : '在 Plex Web 的 URL 里找 X-Plex-Token', 'password')}
          <span class="desc">获取方法：Plex Web 打开任意影片 → 查看页面源码 → 搜索 <code>X-Plex-Token</code>。</span></div>
        <div class="field full"><div class="field-row"><button class="btn sm" id="btn-test-plex">测试 Plex</button><span class="desc" id="plex-test-result"></span></div></div>
      `)}

      ${section('115 授权', '走 115 开放平台设备码，扫码后 token 自动续期。只有外网 302 需要它。', `
        <div class="field full">
          <div class="callout ${u115.authorized ? 'info' : ''}">
            当前状态：<b>${u115.authorized ? esc(u115.user_name || '已授权') : '未授权'}</b>
            ${u115.authorized ? ' · token 会在过期前自动刷新' : ' · 点右侧「扫码登录 115」'}
          </div>
        </div>
        <div class="field full"><div class="field-row">
          <button class="btn primary sm" id="btn-115-qr"><svg><use href="#i-qr"></use></svg>扫码登录 115</button>
          ${u115.authorized ? '<button class="btn sm danger" id="btn-115-logout">退出登录</button>' : ''}
          <button class="btn sm" id="btn-115-check">刷新状态</button>
        </div></div>
        <div class="field full" style="margin-top:18px">
          <label>115 网页 Cookie <span class="muted">（增量入库必需）</span></label>
          <textarea class="input" id="cfg-115-cookie" rows="3" placeholder="${c.u115.cookie_set ? '已保存（' + esc(c.u115.cookie_hint || '') + '），留空不修改' : 'UID=...; CID=...; SEID=...'}"></textarea>
          <span class="desc">浏览器登录 <code>115.com</code> → F12 → Network → 随便点一个 <code>webapi.115.com</code> 请求 → 复制请求头里的 Cookie。
          它用来读 115 的「最近操作」：CMS 整理完是移动 / 改名 / 复制，plex302 立刻就能知道，比等目录刷新可靠。</span>
        </div>
        <div class="field full"><div class="field-row">
          <button class="btn sm" id="btn-115-cookie-test">测试 Cookie</button>
          <span class="desc" id="cookie-test-result"></span>
        </div></div>
      `, true)}

      ${section('CloudDrive2', 'CD2 只用于「挂载给 Plex 扫描」和「刷新目录缓存」；gRPC 端口默认 19798。', `
        <div class="field"><label>gRPC 地址</label>${input('cfg-cd2-address', c.cd2.address, '127.0.0.1:19798')}</div>
        <div class="field"><label>API Token</label>${input('cfg-cd2-token', '', c.cd2.token_set ? '已保存，留空不修改' : 'CD2 后台 → 设置 → API Token', 'password')}</div>
        <div class="field full"><div class="field-row"><button class="btn sm" id="btn-test-cd2">测试 CD2</button><span class="desc" id="cd2-test-result"></span></div></div>
      `)}

      ${section('路径映射', '把 Plex 看到的本地挂载路径，映射成云盘路径（给 115/AList 用）和 CD2 内部路径（给刷新缓存用）。三列都支持留空。', `
        <div class="field full">
          <div class="callout info">例：Plex 里是 <code>/mnt/cd2/115/电影/沙丘2</code>，115 网盘根目录对应 <code>/</code>，CD2 内部是 <code>/115/电影/沙丘2</code>：
          <br>Plex 前缀 <code>/mnt/cd2/115</code> · 云盘前缀 <code>/</code> · CD2 前缀 <code>/115</code>。</div>
        </div>
        <div class="field full" id="mount-list">${mountRows}</div>
        <div class="field full"><button class="btn sm" id="btn-add-mount"><svg><use href="#i-plus"></use></svg>添加一条映射</button></div>
      `, true)}

      ${section('直链方式', '决定 302 的目标从哪里来。115 最稳；CD2 需要直链权限；AList 适合混合网盘。', `
        <div class="field"><label>直链来源</label>
          <select id="cfg-provider">
            ${[['115', '115 开放平台（推荐）'], ['cd2', 'CloudDrive2 直链'], ['alist', 'AList / OpenList'], ['custom', '自定义 HTTP 接口'], ['none', '不 302，全部回源 Plex']]
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
        <div class="field"><label>115 Client ID</label>${input('cfg-115-client', c.provider.u115_client_id, '100197847')}
          <span class="desc">默认是 MoviePilot 的公开应用，扫码页会显示 MoviePilot；也可换成自己的开放平台应用。</span></div>
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

      ${section('扫描与自动入库', '三层配合：115 操作事件（主）→ 目录监听（可选）→ 每天全量（兜底）。', `
        <div class="field full">
          ${switchLine('启用扫描', '关闭后手动 / CMS / 事件都不会触发 Plex 扫描', 'cfg-scan-enabled', c.scan.enabled)}
          ${switchLine('CD2 目录监听', '轮询挂载目录做兜底；库很大时可以关掉，交给 115 事件 + 全量', 'cfg-watch-enabled', c.scan.watch_enabled)}
          ${switchLine('首次索引时也扫描', '默认只建索引，避免第一次启动就把全库扫一遍', 'cfg-first-scan', c.scan.first_run_scan)}
        </div>
        <div class="field"><label>目录轮询间隔</label>${combo('cfg-interval', c.scan.interval, [60, 120, 180, 300, 600, 1800])}<span class="desc">CD2 挂载目录的轮询周期。</span></div>
        <div class="field"><label>文件静置</label>${combo('cfg-settle', c.scan.settle_seconds, [30, 60, 90, 180])}<span class="desc">文件修改时间小于这个值先跳过。</span></div>
        <div class="field"><label>最大目录深度</label>${input('cfg-depth', c.scan.max_depth, '4', 'number')}</div>
        <div class="field"><label>最小文件大小（MB）</label>${input('cfg-minsize', c.scan.min_size_mb, '20', 'number')}</div>

        <div class="field full" style="margin-top:6px">
          <div class="callout info">下面这组是 <b>115 操作事件</b>（就是 115 App 里的「最近操作」）：拉取上传 / 移动 / 改名 / 复制 / 删除流水，
          游标增量、按事件 ID 去重。CMS 整理完的那一刻事件就产生了，不依赖 CD2 目录缓存。</div>
        </div>
        <div class="field full">
          ${switchLine('115 操作事件监听（增量入库）', '定时拉取 115 操作流水，发现新视频就局部扫描', 'cfg-events-enabled', c.events.enabled)}
          ${switchLine('首次运行也扫描', '默认只记录游标，不补扫历史，避免一上来扫全库', 'cfg-events-first', c.events.first_scan)}
          ${switchLine('扫完再复核一次', '几分钟后对同一目录再扫一次，接住“扫的时候还没写完”', 'cfg-events-verify', c.events.verify_enabled)}
          ${switchLine('最近文件兜底', '事件接口偶发空返回时，用最近文件列表补一刀', 'cfg-events-recent', c.events.recent_fallback)}
        </div>
        <div class="field"><label>事件检查间隔</label>${combo('cfg-events-interval', c.events.interval, [30, 60, 120, 300, 600])}</div>
        <div class="field"><label>目录静置多久再扫</label>${combo('cfg-events-settle', c.events.settle_seconds, [60, 120, 180, 300, 600])}
          <span class="desc">CMS 还在整理时，每来一个新事件就往后延。</span></div>
        <div class="field"><label>单目录最长等待</label>${combo('cfg-events-maxwait', c.events.max_wait_seconds, [300, 600, 900, 1800])}
          <span class="desc">一直有事件也不会无限等。</span></div>
        <div class="field"><label>复核延迟</label>${combo('cfg-events-verify-delay', c.events.verify_delay, [120, 300, 600, 1800])}</div>
        <div class="field"><label>每次拉取条数</label>${combo('cfg-events-limit', c.events.limit, [50, 100, 200, 300], '条')}</div>
        <div class="field full"><div class="field-row">
          <button class="btn sm" id="btn-events-run">立即检查 115</button>
          <span class="desc" id="events-run-result"></span>
        </div></div>

        <div class="field full"><label>CMS / 外部通知 Webhook</label>
          <div class="field-row"><input class="input" id="cfg-webhook" readonly value="${esc((state.status && state.status.webhook_url) || '')}"><button class="btn sm" id="btn-copy-hook"><svg><use href="#i-copy"></use></svg>复制</button></div>
          <span class="desc">任何能发 HTTP 的工具都能调：<code>GET ...&amp;path=/电影/xxx</code>、<code>POST {"path":"..."}</code>，或 <code>&amp;full=1</code> 直接全量。
          CMS 的 Emby 扫库通知也能接（见下面「CMS 对接」）。</span>
        </div>
      `, true)}

      ${section('全量同步', '把 Plex 所有媒体库整库扫一遍。增量再稳也可能漏，这是最后的保险。', `
        <div class="field full">
          ${switchLine('每天定时全量', '到点自动把所有媒体库扫一遍', 'cfg-full-enabled', c.scan.full_enabled)}
          ${switchLine('启动时也全量一次', '容器重启后如果今天还没跑过，补一次', 'cfg-full-start', c.scan.full_on_start)}
        </div>
        <div class="field"><label>每天几点跑</label><input class="input" id="cfg-full-time" type="time" value="${esc(c.scan.full_time || '04:30')}"></div>
        <div class="field"><label>&nbsp;</label><button class="btn sm" id="btn-full-run-settings"><svg><use href="#i-full"></use></svg>立即全量扫描</button></div>
        <div class="field full"><div class="result-box" id="full-run-result">全量同步会按媒体库逐个调用 Plex <code>/library/sections/&lt;id&gt;/refresh</code>（不带 path）。</div></div>
      `)}

      ${section('通知（企业微信）', '和 115 秒传助手用同一套应用消息：CorpID + Secret + AgentID + 接收人，可选代理。', `
        <div class="field full">${switchLine('启用企业微信通知', '入库完成、全量结果、Cookie 失效都会推送', 'cfg-wecom-enabled', c.notify.wecom_enabled)}</div>
        <div class="field"><label>CorpID</label>${input('cfg-wecom-corpid', c.notify.wecom_corpid, '企业微信 → 我的企业 → 企业ID')}</div>
        <div class="field"><label>AgentID</label>${input('cfg-wecom-agentid', c.notify.wecom_agentid, '应用详情里的 AgentId')}</div>
        <div class="field"><label>Secret</label>${input('cfg-wecom-secret', '', c.notify.wecom_secret_set ? '已保存，留空不修改' : '应用详情里的 Secret', 'password')}</div>
        <div class="field"><label>接收人</label>${input('cfg-wecom-touser', c.notify.wecom_touser, '@all')}<span class="desc">成员账号用 <code>|</code> 分隔，默认全体。</span></div>
        <div class="field full"><label>代理地址（可选）</label>${input('cfg-wecom-proxy', c.notify.wecom_proxy, 'http://proxy:8980')}<span class="desc">企业微信 API 的转发地址，只换 host、路径不变。</span></div>
        <div class="field full">
          ${switchLine('入库通知', '一轮扫描结束后汇总成一条，避免刷屏', 'cfg-notify-scan', c.notify.on_scan)}
          ${switchLine('全量同步通知', '全量结束发一条汇总', 'cfg-notify-full', c.notify.on_full)}
          ${switchLine('错误通知', 'Cookie 失效 / 拉取失败，同类 30 分钟只发一次', 'cfg-notify-error', c.notify.on_error)}
        </div>
        <div class="field full"><div class="field-row"><button class="btn sm" id="btn-notify-test"><svg><use href="#i-bell"></use></svg>发送测试消息</button><span class="desc" id="notify-test-result"></span></div></div>
      `)}

      ${section('CMS 对接', '两种方式，任选一种；都失效时还有 115 事件和全量兜底。', `
        <div class="field full">
          <div class="callout info">
            <b>方式一（推荐）：什么都不用配。</b> CMS 整理就是把文件移动 / 改名到 media 目录，这本身就是 115 操作事件，
            plex302 会感知到并延迟合并扫描。<br><br>
            <b>方式二：让 CMS 主动通知。</b> CMS 的「通知 Emby 扫库」可以把地址填成本容器：
            <code>EMBY_HOST_PORT=http://NAS:999</code>、<code>EMBY_API_KEY=${esc((state.config && state.config.security && state.config.security.emby_token) || '')}</code>，
            它会打 <code>/emby/Library/Refresh</code>，plex302 收到就触发全量（或单库）扫描。
          </div>
        </div>
        <div class="field full"><label>Emby 兼容地址</label>
          <div class="field-row"><input class="input" id="cfg-emby-url" readonly value="${esc((state.status && state.status.emby_url) || '')}"><button class="btn sm" id="btn-copy-emby"><svg><use href="#i-copy"></use></svg>复制</button></div>
          <span class="desc">给 CMS / 其他只支持 Emby 通知的工具用；会校验 <code>api_key</code>。</span>
        </div>
      `, true)}

      ${section('外观', '自动跟随系统，也可以固定浅色 / 深色。只保存在当前浏览器。', `
        <div class="field full">
          <div class="seg" data-theme-seg>
            <button type="button" data-mode="auto"><svg><use href="#i-auto"></use></svg>自动</button>
            <button type="button" data-mode="light"><svg><use href="#i-sun"></use></svg>浅色</button>
            <button type="button" data-mode="dark"><svg><use href="#i-moon"></use></svg>深色</button>
          </div>
          <span class="desc">手机上会自动启用底部导航和卡片式列表。</span>
        </div>
      `, true)}

      ${section('网络与安全', '局域网判断基于客户端真实 IP；如果前面还有一层反代，请确保它传了 X-Forwarded-For。', `
        <div class="field"><label>对外访问地址（可选）</label>${input('cfg-public-url', c.server.public_url, 'https://plex.example.com:32500')}
          <span class="desc">只用于显示 Webhook 地址和文档，不影响代理。</span></div>
        <div class="field"><label>允许 302 的局域网</label>
          <textarea id="cfg-lan" placeholder="192.168.0.0/16&#10;10.0.0.0/8">${esc((c.server.lan_cidrs || []).join('\n'))}</textarea>
          <span class="desc">这些网段默认回源 Plex；要连内网也 302，打开下面的开关。</span></div>
        <div class="field full">
          ${switchLine('信任 X-Forwarded-For', '前面有反代/CDN 时打开；直接暴露端口时保持打开也没问题', 'cfg-trust-xff', c.server.trust_forwarded_for)}
          ${switchLine('局域网也 302', '内网客户端也直接走云盘，NAS 完全不参与播放流量', 'cfg-redirect-lan', c.server.redirect_lan)}
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

  function bindCombos() {
    $$('.combo').forEach((box) => {
      if (box.dataset.bound) return;
      box.dataset.bound = '1';
      const input = box.querySelector('input');
      const mark = () => {
        const v = String(Number(input.value));
        box.querySelectorAll('.combo-menu button').forEach((b) => b.classList.toggle('on', b.dataset.value === v));
      };
      const open = () => {
        $$('.combo.open').forEach((b) => { if (b !== box) b.classList.remove('open'); });
        mark();
        box.classList.add('open');
      };
      const close = () => box.classList.remove('open');
      input.addEventListener('click', open);
      input.addEventListener('focus', open);
      box.querySelector('.combo-arrow').addEventListener('click', (e) => {
        e.stopPropagation();
        if (box.classList.contains('open')) close(); else open();
      });
      box.querySelectorAll('.combo-menu button').forEach((b) => {
        b.addEventListener('mousedown', (e) => e.preventDefault());
        b.addEventListener('click', () => {
          input.value = b.dataset.value;
          close();
          input.dispatchEvent(new Event('change'));
        });
      });
    });
  }

  function bindSettings() {
    $('#btn-test-plex').addEventListener('click', async () => {
      const el = $('#plex-test-result');
      el.textContent = '测试中…';
      try {
        const res = await api('/api/plex/test', { method: 'POST', body: {} });
        const d = res.data;
        el.innerHTML = `<span style="color:var(--green)">连接成功：${esc(d.friendly_name || '')} v${esc(d.version || '')}，${d.sections} 个媒体库</span>`;
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
    $('#btn-115-qr').addEventListener('click', openQrModal);
    const logout = $('#btn-115-logout');
    if (logout) logout.addEventListener('click', async () => {
      if (!confirm('确定退出 115 登录？')) return;
      await api('/api/115/logout', { method: 'POST', body: {} });
      toast('已退出 115', 'ok');
      loadAll(true);
    });
    $('#btn-115-check').addEventListener('click', () => loadAll(true).then(() => toast('状态已刷新', 'ok')));
    $('#btn-115-cookie-test').addEventListener('click', async () => {
      const out = $('#cookie-test-result');
      const cookie = ($('#cfg-115-cookie').value || '').trim();
      out.textContent = '测试中…';
      try {
        const res = await api('/api/115/cookie/test', { method: 'POST', body: { cookie } });
        const d = res.data || {};
        out.textContent = `Cookie 可用：最近文件 ${d.count || 0} 条`
          + (d.events != null ? ` · 操作事件 ${d.events} 条` : '')
          + (d.events_error ? `（事件接口：${d.events_error}）` : '')
          + (d.sample && d.sample.name ? ` · 最新：${d.sample.name}` : '')
          + (d.path ? ` → ${d.path}` : '');
      } catch (err) {
        out.textContent = err.message || 'Cookie 测试失败';
      }
    });
    $('#btn-copy-hook').addEventListener('click', () => copyText($('#cfg-webhook').value));
    const copyEmby = $('#btn-copy-emby');
    if (copyEmby) copyEmby.addEventListener('click', () => copyText($('#cfg-emby-url').value));
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
    bindCombos();
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
    c.provider.u115_client_id = val('cfg-115-client');
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

    c.scan.enabled = checked('cfg-scan-enabled');
    c.scan.watch_enabled = checked('cfg-watch-enabled');
    c.scan.first_run_scan = checked('cfg-first-scan');
    c.scan.interval = Number(val('cfg-interval')) || 180;
    c.scan.settle_seconds = Number(val('cfg-settle')) || 90;
    c.scan.max_depth = Number(val('cfg-depth')) || 4;
    c.scan.min_size_mb = Number(val('cfg-minsize')) || 20;

    c.scan.full_enabled = checked('cfg-full-enabled');
    c.scan.full_on_start = checked('cfg-full-start');
    if (val('cfg-full-time')) c.scan.full_time = val('cfg-full-time');

    c.events.enabled = checked('cfg-events-enabled');
    c.events.first_scan = checked('cfg-events-first');
    c.events.verify_enabled = checked('cfg-events-verify');
    c.events.recent_fallback = checked('cfg-events-recent');
    c.events.interval = Number(val('cfg-events-interval')) || 60;
    c.events.settle_seconds = Number(val('cfg-events-settle')) || 120;
    c.events.max_wait_seconds = Number(val('cfg-events-maxwait')) || 900;
    c.events.verify_delay = Number(val('cfg-events-verify-delay')) || 300;
    c.events.limit = Number(val('cfg-events-limit')) || 100;

    c.notify.wecom_enabled = checked('cfg-wecom-enabled');
    c.notify.wecom_corpid = val('cfg-wecom-corpid');
    c.notify.wecom_agentid = val('cfg-wecom-agentid');
    if (val('cfg-wecom-secret')) c.notify.wecom_secret = val('cfg-wecom-secret');
    c.notify.wecom_touser = val('cfg-wecom-touser') || '@all';
    c.notify.wecom_proxy = val('cfg-wecom-proxy');
    c.notify.on_scan = checked('cfg-notify-scan');
    c.notify.on_full = checked('cfg-notify-full');
    c.notify.on_error = checked('cfg-notify-error');

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

  // ------------------------------------------------------------------ 115 登录
  function openQrModal() {
    const root = $('#modal-root');
    root.innerHTML = `
      <div class="modal-mask">
        <div class="modal">
          <div class="modal-head"><h3>扫码登录 115</h3><button class="icon-btn" data-close><svg><use href="#i-x"></use></svg></button></div>
          <div class="modal-body">
            <div class="qr-wrap"><img id="qr-img" alt="115 登录二维码" src=""><div class="qr-status" id="qr-status">正在获取二维码…</div></div>
            <p style="margin-top:14px">打开手机 115 App → 右上角扫一扫。二维码约 2 分钟有效，过期点「刷新二维码」。</p>
            <div style="display:flex;justify-content:space-between;gap:8px;margin-top:6px">
              <button class="btn ghost sm" id="qr-refresh">刷新二维码</button>
              <button class="btn sm" data-close>关闭</button>
            </div>
          </div>
        </div>
      </div>`;
    root.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeModal));
    root.querySelector('.modal-mask').addEventListener('click', (e) => { if (e.target.classList.contains('modal-mask')) closeModal(); });
    $('#qr-refresh').addEventListener('click', loadQr);
    loadQr();
  }

  async function loadQr() {
    if (state.qrTimer) { clearInterval(state.qrTimer); state.qrTimer = null; }
    $('#qr-status').textContent = '正在获取二维码…';
    try {
      const res = await api('/api/115/qr', { method: 'POST', body: {} });
      $('#qr-img').src = res.data.qrcode;
      $('#qr-status').textContent = '请用手机 115 App 扫码';
      state.qrTimer = setInterval(pollQr, 2200);
    } catch (err) {
      $('#qr-status').textContent = err.message;
    }
  }

  async function pollQr() {
    try {
      const res = await api('/api/115/qr/status');
      const d = res.data;
      if (d.status === 2) {
        clearInterval(state.qrTimer); state.qrTimer = null;
        $('#qr-status').textContent = '授权成功';
        toast('115 登录成功', 'ok');
        setTimeout(() => { closeModal(); loadAll(true); }, 600);
      } else if (d.status === 1) {
        $('#qr-status').textContent = '已扫码，请在手机上确认';
      } else {
        $('#qr-status').textContent = d.msg || '等待扫码…';
      }
    } catch (err) {
      clearInterval(state.qrTimer); state.qrTimer = null;
      $('#qr-status').textContent = err.message;
    }
  }

  function closeModal() {
    if (state.qrTimer) { clearInterval(state.qrTimer); state.qrTimer = null; }
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
      if (!e.target.closest('.combo')) $$('.combo.open').forEach((b) => b.classList.remove('open'));
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
