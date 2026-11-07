"""播放链路自检：在容器里把「Plex 文件 → 路径映射 → 115 定位 → 直链 → 真正下载」跑一遍。

为什么要有它：播放失败时，外面只能看到「302 没生效 / 回源 Plex」，看不出断在哪一环。
这个自检每一步都直接给结论，哪一步红就是哪一步的问题，不用猜。

用法：
    docker exec -it plex302 python -m app.selftest
    docker exec -it plex302 python -m app.selftest --path "/mnt/cd2/115/media/电影/xxx.mkv"
    docker exec -it plex302 python -m app.selftest --json

退出码：0 全通，1 有失败步骤。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import config, logger, plex, providers, resolver, u115open


class Report:
    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail, "fix": fix})
        return ok

    @property
    def ok(self) -> bool:
        return all(s["ok"] for s in self.steps)

    def failed(self) -> List[Dict[str, Any]]:
        return [s for s in self.steps if not s["ok"]]


def _pick_item_from_plex(token: str) -> Tuple[str, str]:
    """从 Plex 里挑一个真实视频，返回 (人类可读标题, 文件绝对路径)。"""
    secs = plex.sections(force=True, token=token or None)
    for sec in secs:
        if sec.get("type") not in ("movie", "show", "episode"):
            continue
        resp = plex.request(
            "GET",
            f"/library/sections/{sec['key']}/all",
            params={"X-Plex-Container-Size": 8},
            token=token or None,
            timeout=10,
        )
        data = resp.json()
        mc = (data or {}).get("MediaContainer") or {}
        for item in mc.get("Metadata") or []:
            title = str(item.get("title") or item.get("parentTitle") or "")
            for media in item.get("Media") or []:
                for part in media.get("Part") or []:
                    path = str(part.get("file") or "")
                    if path:
                        return (f"{sec.get('title')}/{title}", path)
    raise plex.PlexError("Plex 里没找到任何带文件路径的视频（媒体库是空的？）")


def _probe_url(url: str, ua: str, timeout: float = 20.0) -> Tuple[bool, str]:
    """真正去下载前 1KB：302 给你了不代表能播，得看对方认不认。"""
    import requests

    headers = {"User-Agent": ua or "Plex302/selftest", "Range": "bytes=0-1023"}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
            chunk = next(r.iter_content(1024), b"")
            code = r.status_code
            ctype = r.headers.get("Content-Type") or "-"
            total = r.headers.get("Content-Range") or r.headers.get("Content-Length") or "-"
    except Exception as err:  # noqa: BLE001
        return False, f"下载失败：{type(err).__name__}: {err}"
    if code not in (200, 206):
        return False, f"HTTP {code}（115 拒绝，多半是 UA 不匹配或直链过期）；Content-Type={ctype}"
    if not chunk:
        return False, f"HTTP {code} 但一个字节都没下到；Content-Type={ctype}"
    head = chunk[:16]
    is_video = head[4:8] == b"ftyp" or head[:4] in (b"\x1a\x45\xdf\xa3",) or head[:2] == b"\xff\xfb"
    note = "看起来是视频头" if is_video else f"头部字节 {head[:8].hex()}（可能被重定向到网页/鉴权页）"
    return True, f"HTTP {code} · {ctype} · {total} · 实际收到 {len(chunk)} 字节 · {note}"


def run(path: Optional[str] = None, ua: str = "", timeout: float = 20.0) -> Report:
    rep = Report()
    cfg = config.load(force=True)

    # 1) 配置齐不齐
    has_token = bool(cfg["plex"].get("token"))
    rep.add(
        "Plex Token 已配置",
        has_token,
        "已配置" if has_token else "没填 X-Plex-Token",
        "设置 → Plex 连接，填 X-Plex-Token 后保存",
    )
    token_state = u115open.token_state(cfg)
    rep.add(
        "115 官方接口已登录",
        bool(token_state.get("logged_in")),
        f"{token_state.get('user_name') or '-'}（剩余 {int(token_state.get('expires_in') or 0)} 秒）"
        if token_state.get("logged_in") else "未扫码登录",
        "设置 → 115 官方接口 → 扫码登录",
    )
    mounts = cfg["mounts"] or []
    rep.add(
        "路径映射已配置",
        bool(mounts),
        "；".join(f"①{m.get('plex_prefix')} → ②{m.get('cloud_prefix') or '/'}" for m in mounts) or "没有配置",
        "设置 → 路径映射，①填 Plex 里看到的路径前缀",
    )

    # 2) 拿到一个真实文件路径（用户指定就直接用，便于对着报错的文件测）
    if path:
        plex_path = path
        rep.add("指定的文件路径", True, plex_path)
    else:
        try:
            title, plex_path = _pick_item_from_plex(str(cfg["plex"].get("token") or ""))
            rep.add("从 Plex 取一个真实视频", True, f"{title} → {plex_path}")
        except Exception as err:  # noqa: BLE001
            rep.add("从 Plex 取一个真实视频", False, f"{err}", "检查 Plex 地址 / Token，以及媒体库是否有片")
            return rep

    # 3) 路径映射
    cloud = resolver.Resolver.map_path(cfg, plex_path)
    if not rep.add(
        "路径映射命中",
        bool(cloud),
        f"{plex_path} → {cloud}" if cloud
        else f"{plex_path} 不在任何①前缀下（当前：{', '.join(str(m.get('plex_prefix')) for m in mounts) or '无'}）",
        "设置 → 路径映射 → 对照 Plex 实际路径，按提示改①",
    ):
        return rep
    diag = resolver.Resolver.explain_mapping(cfg, plex_path)
    if diag["hints"]:
        # 映射「命中」不等于「对」：①填太短时云盘路径会多出一层挂载点名，
        # 这种错最隐蔽 —— 命中了但 115 找不到文件。
        rep.add(
            "映射没有可疑之处",
            False,
            " / ".join(diag["hints"]),
            "设置 → 路径映射 → 对照 Plex 实际路径，按提示改①/②",
        )

    # 4) 115 定位文件（按完整路径点查，不列目录）
    ctx = providers.ResolveContext(ip="", ua=ua or "Plex302/selftest")
    try:
        pick = u115open.pickcode_by_path(cfg, cloud)
        rep.add("115 按路径定位到文件", bool(pick), f"pick_code={pick}")
    except Exception as err:  # noqa: BLE001
        rep.add(
            "115 按路径定位到文件", False, f"{err}",
            "云盘路径不对（大小写/空格/中文括号），或 115 官方接口的 token 权限不足",
        )
        return rep

    # 5) 换直链
    try:
        link = providers.resolve(cfg, cloud, ctx)
        rep.add("115 返回直链", True, f"来源={link.provider} · {link.url.split('?')[0][:80]}")
    except Exception as err:  # noqa: BLE001
        rep.add("115 返回直链", False, f"{err}", "多半是 pickcode 拿到了但 downurl 被拒，检查 token 是否过期")
        return rep

    # 6) 真的下 1KB —— 这一步才代表「能不能播」
    ok, detail = _probe_url(link.url, ua, timeout=timeout)
    rep.add(
        "直链真的能下载", ok, detail,
        "UA 不匹配（115 直链绑定 UA）：把设置里的 UA 模式改回「跟随客户端」；"
        "或直链已过期，重新播放一次",
    )
    return rep


def _print(rep: Report) -> None:
    print()
    print("=" * 72)
    print("播放链路自检")
    print("=" * 72)
    for i, s in enumerate(rep.steps, 1):
        mark = "✓" if s["ok"] else "✗"
        print(f"{i}. {mark} {s['step']}")
        if s["detail"]:
            print(f"     {s['detail']}")
        if not s["ok"] and s.get("fix"):
            print(f"     ↳ 怎么修：{s['fix']}")
    print("-" * 72)
    if rep.ok:
        print("结论：这条链路是通的。如果播放器还是不行，问题在客户端或 Plex 那边，不在 302。")
    else:
        bad = rep.failed()[0]
        print(f"结论：断在「{bad['step']}」——{bad.get('detail') or ''}")
        if bad.get("fix"):
            print(f"      {bad['fix']}")
    print("=" * 72)
    print()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="播放链路自检")
    ap.add_argument("--path", default="", help="直接测这个 Plex 文件路径（默认从 Plex 里随便取一个）")
    ap.add_argument("--ua", default="", help="模拟的客户端 UA（默认用内置的 Plex 客户端 UA）")
    ap.add_argument("--json", action="store_true", help="输出 JSON，便于贴给他人")
    args = ap.parse_args(argv)

    ua = args.ua or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        rep = run(path=args.path or None, ua=ua)
    except Exception as err:  # noqa: BLE001
        logger.error(f"[自检] 运行异常：{err}")
        print(f"自检本身崩了：{err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": rep.ok, "steps": rep.steps}, ensure_ascii=False, indent=2))
    else:
        _print(rep)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
