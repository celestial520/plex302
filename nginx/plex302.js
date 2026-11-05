// Plex302 njs 入口：只做两件事——问本地 resolver 要 302，或者回源 Plex。
import fs from 'fs';

let rt = {
    plex: 'http://127.0.0.1:32400',
    resolver: 'http://127.0.0.1:999',
    token: '',
    trust_proxy: true,
    force_direct_play: true
};
let rtMtime = 0;
let rtChecked = 0;

// runtime.json 由 Python 侧写入（保存配置时），这里按 mtime 热加载，
// 免得 nginx 比 app 先起来时一直用默认值（默认值里没有 Plex 地址）。
function reloadRuntime() {
    const now = Date.now();
    if (now - rtChecked < 2000) {
        return;
    }
    rtChecked = now;
    try {
        const st = fs.statSync('/config/runtime.json');
        const mtime = st.mtime ? st.mtime.getTime() : 0;
        if (mtime && mtime === rtMtime) {
            return;
        }
        rt = JSON.parse(fs.readFileSync('/config/runtime.json', 'utf8'));
        rtMtime = mtime;
    } catch (e) {
        // 文件还没生成：保持当前值（首次启动就是默认值）
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
    } catch (e) {
        // ignore
    }
    return out;
}

async function askResolver(r, body) {
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
        return null;
    }
    return await res.json();
}

async function redirect2Pan(r) {
    reloadRuntime();
    const args = plainArgs(r);
    const token = (args['X-Plex-Token'] && args['X-Plex-Token'][0]) || r.headersIn['X-Plex-Token'] || '';
    let decision = null;
    try {
        decision = await askResolver(r, {
            uri: r.uri,
            args: args,
            ip: clientIp(r),
            ua: r.headersIn['User-Agent'] || '',
            token: token
        });
    } catch (e) {
        decision = null;
    }

    if (decision && decision.action === 'redirect' && decision.url) {
        r.headersOut['X-Plex302'] = 'redirect';
        r.headersOut['X-Plex302-Reason'] = decision.reason || 'ok';
        r.return(302, decision.url);
        return;
    }

    r.headersOut['X-Plex302'] = 'proxy';
    if (decision && decision.reason) {
        r.headersOut['X-Plex302-Reason'] = decision.reason;
    }
    proxyToPlex(r);
}

function proxyToPlex(r) {
    // 418 交给 nginx 的 error_page -> @plex_proxy，
    // 命名 location 里用 $request_uri 回源，百分号编码不会丢。
    r.return(418);
}

// Plex 在真正起播前会请求 decision；这里强制 directPlay，避免服务端转码把 302 链路绕开。
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
    const sub = await r.subrequest('/__plex' + r.uri + (args ? '?' + args : ''), { method: r.method });
    const headers = sub.headersOut;
    for (const key in headers) {
        const lower = key.toLowerCase();
        if (lower === 'content-length' || lower === 'transfer-encoding' || lower === 'connection') continue;
        r.headersOut[key] = headers[key];
    }
    r.return(sub.status, sub.responseText);
}

export default { getPlex, redirect2Pan, decision };
