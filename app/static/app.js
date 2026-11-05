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
    tab: 'overview',
    timers: {},
    qrTimer: null,
  };

  const TITLES = {
    overview: ['概览', '外网播放 302 到云盘，内网回源 Plex；CMS 整理完自动局部入库。'],
    library: ['媒体库', 'Plex 媒体库路径、自动监听开关和单库扫描。'],
    scans: ['扫描记录', '局部扫描的入队、执行结果与失败原因。'],
    settings: ['设置', '连接、路径映射、直链方式、扫描策略与安全。'],
    logs: ['日志', '302 决策、扫描、CMS 通知和错误都会记录。'],
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

  // ------------------------------------------------------------------ 路由
  function switchTab(tab) {
    if (!TITLES[tab]) tab = 'overview';
    state.tab = tab;
    $$('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
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
      const [status, cfg, sections, scans] = await Promise.all([
        api('/api/status'),
        api('/api/config'),
        api('/api/plex/sections').catch(() => ({ data: [] })),
        api('/api/scan/history?limit=50').catch(() => ({ data: [] })),
      ]);
      state.status = status.data;
      state.config = cfg.data;
      state.sections = sections.data || [];
      state.scans = scans.data || [];
      renderStatus();
      renderOverviewScans();
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
      const res = await api('/api/status');
      state.status = res.data;
      renderStatus();
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

    const watch = s.watch || {};
    if (watch.enabled === false) {
      $('#stat-watch').textContent = '关闭';
      $('#stat-watch-sub').textContent = '可在设置中开启';
    } else if (watch.at) {
      $('#stat-watch').textContent = watch.queued != null ? String(watch.queued) : '0';
      $('#stat-watch-sub').textContent = `上轮 ${watch.files || 0} 个视频 / 新增 ${watch.new || 0}`;
    } else {
      $('#stat-watch').textContent = '待运行';
      $('#stat-watch-sub').textContent = '启动后 20 秒开始首轮索引';
    }

    $('#side-version').textContent = 'v' + (s.version || '1.0.0');

    const recent = s.recent || {};
    const rows = [
      ['Plex 服务器', plex.ok ? `v${plex.version || ''} · ${plex.name || ''}` : (plex.error || '未连接'), plex.ok, '#i-play'],
      ['115 开放平台', u115.authorized ? `${u115.user_name || '已授权'} · token 有效` : '未授权，外网 302 不可用', !!u115.authorized, '#i-link'],
      ['CloudDrive2', cd2.ok ? `${cd2.user_name || ''} · ${cd2.ready ? '就绪' : '未就绪'}` : (cd2.message || '未连接'), !!cd2.ok, '#i-scan'],
      ['目录监听', watch.enabled === false ? '已关闭' : (watch.at ? `${watch.targets || 0} 个根目录 · 上轮入队 ${watch.queued || 0}` : '等待首轮索引'), watch.enabled !== false, '#i-logs'],
      ['115 最近文件', !recent.cookie_set ? '未配置 Cookie' : (recent.summary && recent.summary.at ? `已记录 ${recent.seen || 0} 条 · 上轮入队 ${recent.summary.queued || 0}` : '等待首轮检查'), !!recent.cookie_set && !(recent.summary && recent.summary.ok === false), '#i-link'],
    ];
    $('#status-list').innerHTML = rows.map((r) => `
      <div class="status-row">
        <div class="status-ico"><svg><use href="${r[3]}"></use></svg></div>
        <div class="status-main"><b>${esc(r[0])}</b><span>${esc(r[1])}</span></div>
        <div class="status-val">${r[2] ? '<span class="badge ok">正常</span>' : '<span class="badge bad">异常</span>'}</div>
      </div>`).join('');
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
        <td class="muted">${fmtTime(r.created_at)}</td>
        <td><span class="path" title="${esc(r.path)}">${esc(shortPath(r.path, 42))}</span></td>
        <td>${statusBadge(r.status)}</td>
      </tr>`).join('');
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
        <td><b>${esc(s.title)}</b></td>
        <td><span class="badge ${s.type === 'movie' ? 'movie' : 'show'}">${s.type === 'movie' ? '电影' : s.type === 'show' ? '剧集' : esc(s.type)}</span></td>
        <td>${(s.locations || []).map((p) => `<div class="path mono">${esc(p)}</div>`).join('') || '<span class="muted">—</span>'}</td>
        <td>${s.count || 0}</td>
        <td><label class="switch"><input type="checkbox" data-lib="${esc(s.key)}" ${s.watching ? 'checked' : ''}><i></i></label></td>
        <td><button class="btn sm" data-scan-lib="${esc(s.key)}" data-scan-path="${esc((s.locations || [])[0] || '')}">扫描</button></td>
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
        <td class="muted" style="white-space:nowrap">${fmtTime(r.created_at)}</td>
        <td><span class="path mono" title="${esc(r.path)}">${esc(r.path)}</span></td>
        <td>${esc(r.section_name || '—')}</td>
        <td><span class="badge">${esc(r.source || 'manual')}</span></td>
        <td>${statusBadge(r.status)}</td>
        <td class="muted">${esc(r.message || '')}</td>
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
          <label>115 网页 Cookie <span class="muted">（用于“最近文件”监听，可选）</span></label>
          <textarea class="input" id="cfg-115-cookie" rows="3" placeholder="${c.u115.cookie_set ? '已保存（' + esc(c.u115.cookie_hint || '') + '），留空不修改' : 'UID=...; CID=...; SEID=...'}"></textarea>
          <span class="desc">浏览器登录 <code>115.com</code> → F12 → Network → 随便点一个 <code>webapi.115.com</code> 请求 → 复制请求头里的 Cookie。
          开放平台没有“最近文件”接口，靠它发现 CMS 刚整理进 media 目录的文件，比目录轮询更不容易漏。</span>
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

      ${section('扫描与自动入库', '只调 Plex 官方局部扫描接口。CD2 目录监听负责发现 CMS 整理后的新文件。', `
        <div class="field full">
          ${switchLine('启用扫描', '关闭后手动/CMS/监听都不会触发 Plex 扫描', 'cfg-scan-enabled', c.scan.enabled)}
          ${switchLine('目录监听', '轮询 CD2 挂载目录，发现新视频后自动局部扫描', 'cfg-watch-enabled', c.scan.watch_enabled)}
          ${switchLine('首次索引时也扫描', '默认只建索引，避免第一次启动就把全库扫一遍', 'cfg-first-scan', c.scan.first_run_scan)}
        </div>
        <div class="field"><label>轮询间隔（秒）</label>${input('cfg-interval', c.scan.interval, '180', 'number')}</div>
        <div class="field"><label>文件静置（秒）</label>${input('cfg-settle', c.scan.settle_seconds, '90', 'number')}
          <span class="desc">文件修改时间小于这个值先跳过，避免整理中途被扫到。</span></div>
        <div class="field"><label>最大目录深度</label>${input('cfg-depth', c.scan.max_depth, '4', 'number')}</div>
        <div class="field"><label>最小文件大小（MB）</label>${input('cfg-minsize', c.scan.min_size_mb, '20', 'number')}</div>

        <div class="field full" style="margin-top:6px">
          <div class="callout info">下面这组是「115 最近文件监听」：直接问 115 服务端最近有没有新文件，不依赖 CD2 目录缓存，
          适合 CMS 整理完立刻入库、又怕目录轮询漏掉的场景。需要先在上面填 115 Cookie。</div>
        </div>
        <div class="field full">
          ${switchLine('115 最近文件监听', '定时拉取 115 最近变动，发现新视频就局部扫描', 'cfg-recent-enabled', c.u115.recent_enabled)}
          ${switchLine('首次运行也扫描', '第一次启动时把当前最近文件也入库；默认只记录，不批量扫', 'cfg-recent-first', c.u115.recent_first_scan)}
        </div>
        <div class="field"><label>115 检查间隔（秒）</label>${input('cfg-recent-interval', c.u115.recent_interval, '120', 'number')}</div>
        <div class="field"><label>每次检查条数</label>${input('cfg-recent-limit', c.u115.recent_limit, '50', 'number')}
          <span class="desc">建议 30~100，越大越不容易漏，但请求也越重。</span></div>
        <div class="field full"><div class="field-row">
          <button class="btn sm" id="btn-recent-run">立即检查一次</button>
          <span class="desc" id="recent-run-result"></span>
        </div></div>

        <div class="field full"><label>CMS / 外部通知 Webhook</label>
          <div class="field-row"><input class="input" id="cfg-webhook" readonly value="${esc((state.status && state.status.webhook_url) || '')}"><button class="btn sm" id="btn-copy-hook"><svg><use href="#i-copy"></use></svg>复制</button></div>
          <span class="desc">CMS 整理完可以 GET 这个地址并带 <code>&amp;path=/电影/xxx</code>；也支持 POST JSON <code>{"path":"/电影/xxx"}</code>。不带 path 时监听会兜底。</span>
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
        out.textContent = `Cookie 可用，最近文件 ${d.count || 0} 条` + (d.sample && d.sample.name ? `（最新：${d.sample.name}）` : '');
      } catch (err) {
        out.textContent = err.message || 'Cookie 测试失败';
      }
    });
    $('#btn-recent-run').addEventListener('click', async () => {
      const out = $('#recent-run-result');
      out.textContent = '检查中…';
      try {
        const res = await api('/api/115/recent/run', { method: 'POST', body: {} });
        const d = res.data || {};
        out.textContent = `检查 ${d.checked || 0} 条 / 新视频 ${d.new || 0} / 入队 ${d.queued || 0}` + (d.first_run ? '（首次只记录）' : '');
        loadAll(true);
      } catch (err) {
        out.textContent = err.message || '检查失败';
      }
    });
    $('#btn-copy-hook').addEventListener('click', () => copyText($('#cfg-webhook').value));
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

    c.u115.recent_enabled = checked('cfg-recent-enabled');
    c.u115.recent_first_scan = checked('cfg-recent-first');
    c.u115.recent_interval = Number(val('cfg-recent-interval')) || 120;
    c.u115.recent_limit = Number(val('cfg-recent-limit')) || 50;
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
    $$('.nav a').forEach((a) => a.addEventListener('click', (e) => {
      e.preventDefault();
      switchTab(a.dataset.tab);
    }));
    window.addEventListener('hashchange', () => switchTab((location.hash || '#overview').slice(1)));

    $('#btn-refresh-all').addEventListener('click', () => { loadAll(); toast('正在刷新', 'ok'); });
    $('#btn-logout').addEventListener('click', async () => {
      await api('/api/logout', { method: 'POST', body: {} }).catch(() => {});
      location.href = '/login';
    });
    $('#btn-hero-scan').addEventListener('click', scanDialog);
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
    bind();
    const tab = (location.hash || '#overview').slice(1);
    switchTab(tab);
    await loadAll();
  }

  boot();
})();
