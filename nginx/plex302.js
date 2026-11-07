// Plex302 njs 入口：拦截 / 播放架构对齐 plex-demo（bpking/embyExternalUrl）。
//
// 抄 plex-demo 的部分：
//   - 拦截点拓扑：metadata/sections/playQueues/decision/parts/start/session/streams
//   - 302 直链缓存在 nginx 共享内存（worker 间共享、reload 不丢），命中直接 302
//     连后端都不问 —— 对应它的 routeL1/L2Dict
//   - 强制 directPlay，避免服务端转码绕开 302
//   - 任何失败一律回源 Plex（fallbackUseOriginal），绝不把 500 甩给播放器
// 按 CD2 场景调整（不照抄）的部分：
//   - metadata 响应原生透传，不注入假 Stream / container：那是给 STRM 补流信息用的，
//     CD2 是真实文件，Plex 已扫到真实码率/编码，注入反而污染
//   - 不需要 partInfoDict 预存 part.key -> file：STRM 的 part_file 拿不到真实路径才需要，
//     CD2 真实文件由 Python 侧 part_cache 覆盖
//   - 直链用 115 网页 Cookie（后端 Python 换取），不是 115 开放平台
import fs from 'fs';

let rt = {
    plex: 'http://127.0.0.1:32400',
    resolver: 'http://127.0.0.1:999',
    token: '',
    trust_proxy: true,
    force_direct_play: true,
    route_cache: true,
};
let rtMtime = 0;
let rtChecked = 0;

// runtime.json 由 Python 侧写入，按 mtime 热加载，避免 nginx 比 app 先起来时用默认值。
function reloadRuntime() {
    const now = Date.now();
    if (now - rtChecked < 2000) return;
    rtChecked = now;
    try {
        const st = fs.statSync('/config/runtime.json');
        const mtime = st.mtime ? st.mtime.getTime() : 0;
        if (mtime && mtime === rtMtime) return;
        const cfg = JSON.parse(fs.readFileSync('/config/runtime.json', 'utf8'));
        // 显式合并：不依赖 Object.assign（各 njs 版本支持度不一），缺字段保留默认值
        if (cfg.plex !== undefined) rt.plex = cfg.plex;
        if (cfg.resolver !== undefined) rt.resolver = cfg.resolver;
        if (cfg.token !== undefined) rt.token = cfg.token;
        if (cfg.trust_proxy !== undefined) rt.trust_proxy = cfg.trust_proxy;
        if (cfg.force_direct_play !== undefined) rt.force_direct_play = cfg.force_direct_play;
        if (cfg.route_cache !== undefined) rt.route_cache = cfg.route_cache;
        rtMtime = mtime;
    } catch (e) {
        // 文件还没生成：保持当前值
    }
}

reloadRuntime();

function getPlex(r) {
    reloadRuntime();
    return rt.plex;
}

function clientIp(r) {
    if (rt.trust_proxy) {
        const xff = r.headersIn['X-Forwarded-For'];
        if (xff) {
            const first = xff.split(',')[0].trim();
            if (first) return first;
        }
        const real = r.headersIn['X-Real-IP'];
        if (real) return real.trim();
    }
    return r.remoteAddress || '';
}

function plainArgs(r) {
    const out = {};
    try {
        const args = r.args || {};
        for (const key in args) {
            const value = args[key];
            out[key] = Array.isArray(value) ? value : [String(value)];
        }
    } catch (e) { /* ignore */ }
    return out;
}

// ------------------------------------------------------------ 共享内存缓存
// routeDict：请求 key -> "<过期时间戳ms>|<直链>"。njs 0.8.2 不支持 per-key
//            timeout（要 0.8.5+），把直链真实过期时间编进 value 自己判断，
//            保证不会把过期直链甩给播放器。
const ROUTE_ZONE = 'routeDict';

function zone(name) {
    try { return ngx.shared[name]; } catch (e) { return null; }
}

// 缓存键包含：配置版本(rtMtime) + 客户端IP + UA + 播放参数 + directPlay。
//   - UA：115 直链和 UA 绑定，不同设备不能串用（否则 403）
//   - IP：内网/外网是否 302 由 Python 决策，隔开避免绕过 redirect_lan
//   - rtMtime：runtime.json 一改（保存配置）旧缓存整体失效
//   - directPlay：转码请求不能命中直连的缓存
function routeKey(r, ua) {
    const a = r.args || {};
    const g = function (n) {
        const v = a[n];
        if (Array.isArray(v)) return v.length ? String(v[0]) : '';
        return v === undefined ? '' : String(v);
    };
    return String(rtMtime) + '|' + clientIp(r) + '|' + ua + '|' + r.uri
        + '|' + g('path') + '|' + g('mediaIndex') + '|' + g('partIndex')
        + '|' + g('directPlay');
}

function routeGet(key) {
    if (!rt.route_cache) return '';
    const z = zone(ROUTE_ZONE);
    if (!z) return '';
    try {
        const raw = z.get(key);
        if (!raw) return '';
        const i = raw.indexOf('|');
        if (i < 0) return '';
        const exp = Number(raw.slice(0, i));
        // 直链过期就当未命中，重新解析
        if (!exp || Date.now() > exp) return '';
        return raw.slice(i + 1);
    } catch (e) { return ''; }
}

function routeSet(key, url, ttlSec) {
    if (!rt.route_cache || !url) return;
    const z = zone(ROUTE_ZONE);
    if (!z) return;
    try {
        const ttl = Math.max(30, Math.min(Number(ttlSec) || 900, 21600));
        z.set(key, String(Date.now() + ttl * 1000) + '|' + url);
    } catch (e) { /* 满了就跳过 */ }
}

// ------------------------------------------------------------ 工具
async function askResolver(r, body) {
    // 超时交给 nginx 的 js_fetch_timeout（模板里 10s），后端 /api/resolve 自己
    // 也有 8s 硬上限。这里以前用 Promise.race + setTimeout 兜底，但 njs 的
    // 定时器是绑在请求生命周期上的：即便 fetch 已经秒回，请求也要等定时器
    // 触发才 finalize —— 每次没命中缓存的起播都白等整整 10 秒，播放器因此
    // 大量 499 提前断开。去掉这个多余定时器后，首播从 10s 降到百毫秒级。
    try {
        const res = await ngx.fetch(rt.resolver + '/api/resolve', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Internal-Token': rt.token
            },
            body: JSON.stringify(body),
            max_response_body_size: 65536
        });
        if (!res.ok) {
            return { action: 'proxy', reason: 'resolver-http-' + res.status };
        }
        return await res.json();
    } catch (e) {
        // fetch 超时 / 后端没监听：一律回源 Plex，绝不把 500 甩给播放器
        return { action: 'proxy', reason: 'resolver-timeout(nginx)' };
    }
}

function proxyToPlex(r) {
    // 418 交给 nginx 的 error_page -> @plex_proxy，用 $request_uri 回源，编码不丢。
    r.return(418);
}

function copyHeaders(from, to) {
    for (const key in from) {
        const lower = key.toLowerCase();
        if (lower === 'content-length' || lower === 'transfer-encoding' ||
            lower === 'connection' || lower === 'content-encoding') continue;
        to[key] = from[key];
    }
}

// ------------------------------------------------------------ 决策日志
// 对齐 plex-demo：它的 redirect() 每次 302 都 r.warn("[重定向] : " + url)，
// 所以缓存命中也能在 docker logs 里看到。我们额外带上 action/reason/ip/uri，
// 并把直链脱敏（带签名的完整 URL 不写进日志）。
// 日志失败绝不影响播放，整段包在 try 里。
function safeLink(url) {
    try {
        const m = String(url || '').match(/^([a-z]+:\/\/[^/]+)(\/[^?]*)/i);
        if (m) return m[1] + (m[2].length > 48 ? m[2].slice(0, 48) + '…' : m[2]);
    } catch (e) { /* ignore */ }
    return String(url || '').slice(0, 60);
}

function logDecision(r, action, reason, url) {
    try {
        r.warn('[Plex302] action=' + action + ' reason=' + (reason || '-')
            + ' ip=' + clientIp(r) + ' uri=' + r.uri
            + (url ? ' -> ' + safeLink(url) : ''));
    } catch (e) { /* 日志失败不能影响播放 */ }
}

// ------------------------------------------------------------ 播放：302
async function redirect2Pan(r) {
    reloadRuntime();
    const args = plainArgs(r);
    const ua = r.headersIn['User-Agent'] || '';
    const token = (args['X-Plex-Token'] && args['X-Plex-Token'][0]) || r.headersIn['X-Plex-Token'] || '';
    const isStart = r.uri.toLowerCase().indexOf('/transcode/universal/start') >= 0;

    // 客户端明确要转码：回源，不要 302（对齐 plex-demo 的 transcode 判断）
    if (isStart && String(args['directPlay'] ? args['directPlay'][0] : '') === '0') {
        r.headersOut['X-Plex302'] = 'proxy';
        r.headersOut['X-Plex302-Reason'] = 'client requested transcode';
        logDecision(r, 'proxy', 'client requested transcode');
        proxyToPlex(r);
        return;
    }

    const key = routeKey(r, ua);

    // 1) 命中 nginx 共享内存：直接 302，连 Python 后端都不问（plex-demo 的 L1/L2）
    const hit = routeGet(key);
    if (hit) {
        r.headersOut['X-Plex302'] = 'redirect';
        r.headersOut['X-Plex302-Reason'] = 'nginx-cache';
        logDecision(r, 'redirect', 'nginx-cache', hit);
        r.return(302, hit);
        return;
    }

    // 2) 问 Python 后端要 115 Cookie 直链（路径映射 / 播放索引 / 节流都在后端）
    let decision = null;
    try {
        decision = await askResolver(r, {
            uri: r.uri,
            args: args,
            ip: clientIp(r),
            ua: ua,
            token: token
        });
    } catch (e) {
        decision = null;
    }

    if (decision && decision.action === 'redirect' && decision.url) {
        const ttl = Number(decision.cache_ttl) || 900;
        routeSet(key, decision.url, ttl);
        r.headersOut['X-Plex302'] = 'redirect';
        r.headersOut['X-Plex302-Reason'] = decision.reason || 'ok';
        logDecision(r, 'redirect', decision.reason || 'ok', decision.url);
        // 客户端缓存时间不超过直链剩余寿命，避免拿着过期直链拖动进度条
        const clientMax = Math.min(3600, Math.max(0, ttl - 300));
        if (clientMax > 0) r.headersOut['Cache-Control'] = 'max-age=' + clientMax;
        r.return(302, decision.url);
        return;
    }

    // 3) fallbackUseOriginal：任何失败一律回源 Plex，绝不把 500 甩给播放器
    r.headersOut['X-Plex302'] = 'proxy';
    if (decision && decision.reason) r.headersOut['X-Plex302-Reason'] = decision.reason;
    logDecision(r, 'proxy', (decision && decision.reason) || 'fallback');
    proxyToPlex(r);
}

// ------------------------------------------------ 起播决策：强制 directPlay
// 对齐 plex-demo：decision 阶段改写参数强制 directPlay，避免服务端转码把 302 链路绕开。
// decision 响应很小，用子请求回源并原样返回，不改动响应体（CD2 不需要补流信息）。
async function decision(r) {
    reloadRuntime();
    let args = r.variables.args || '';
    if (rt.force_direct_play) {
        args = args
            .replace(/(^|&)directPlay=[^&]*/g, '$1')
            .replace(/(^|&)directStream=[^&]*/g, '$1')
            .replace(/^&/, '')
            .replace(/&$/, '')
            .replace(/&&+/g, '&');
        args += (args ? '&' : '') + 'directPlay=1&directStream=1';
    }
    try {
        const sub = await r.subrequest('/__plex' + r.uri + (args ? '?' + args : ''), { method: r.method });
        copyHeaders(sub.headersOut, r.headersOut);
        r.return(sub.status, sub.responseText);
    } catch (e) {
        proxyToPlex(r);
    }
}

export default { getPlex, redirect2Pan, decision };
