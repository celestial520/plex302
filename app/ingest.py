"""链接入料：115 分享 / 磁力 / ed2k → 存进「待整理」目录。

微信里发一条链接过来，走的就是这里：
  分享链接  -> /share/receive 转存到待整理
  磁力/ed2k -> /offline/add_task_urls 离线到待整理
存完可选立刻跑一轮整理（默认开）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from . import config, logger, u115ops, u115web

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s\"'<>]+", re.I)
ED2K_RE = re.compile(r"ed2k://\|file\|[^\s\"'<>]+", re.I)


def parse_links(text: str) -> List[Dict[str, str]]:
    text = str(text or "")
    out: List[Dict[str, str]] = []
    share = u115ops.parse_share(text)
    if share:
        out.append({"type": "share", **share, "url": text.strip()[:500]})
    for match in MAGNET_RE.finditer(text):
        out.append({"type": "magnet", "url": match.group(0)})
    for match in ED2K_RE.finditer(text):
        out.append({"type": "ed2k", "url": match.group(0)})
    return out


def ingest(text: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or config.load()
    ing = cfg["ingest"]
    if not ing.get("enabled"):
        return {"ok": False, "error": "链接入库未启用（设置 → 链接入库）"}
    if not u115web.has_cookie(cfg):
        return {"ok": False, "error": "未配置 115 Cookie"}
    target = str(ing.get("target_dir") or "").strip()
    if not target:
        return {"ok": False, "error": "没有设置入库目录"}

    links = parse_links(text)
    if not links:
        return {"ok": False, "error": "没找到 115 分享链接 / 磁力 / ed2k"}

    # ensure_dir 会走 115 网页接口，Cookie 失效 / 风控 / 非 JSON 都会抛异常。
    # 以前这一步在 try 之外，接口直接 500，用户只看到「入库失败」没有原因。
    try:
        cid = u115ops.ensure_dir(cfg, target)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "error": f"115 创建入库目录失败：{err}"}
    results = []
    for link in links[:5]:
        if link["type"] == "share":
            results.append(_share(cfg, link, cid))
        else:
            results.append(_offline(cfg, link, cid))
        time.sleep(u115web.API_GAP)  # 115 风控：两条链接之间也按 2 秒节奏

    ok_count = sum(1 for x in results if x.get("ok"))
    summary: Dict[str, Any] = {"ok": ok_count > 0, "target": target, "results": results}
    logger.info(f"[入库] {ok_count}/{len(results)} 条链接已提交到 {target}")
    return summary


def _share(cfg: Dict[str, Any], link: Dict[str, str], cid: str) -> Dict[str, Any]:
    share_code = link.get("share_code") or ""
    code = link.get("receive_code") or ""
    try:
        snap = u115ops.share_snap(cfg, share_code, code)
        items = snap.get("items") or []
        if not items:
            return {"ok": False, "type": "share", "error": "分享为空或提取码不对"}
        ids = [x["id"] for x in items if x.get("id")]
        u115ops.share_receive(cfg, share_code, code, cid, ids)
        names = "、".join(x["name"] for x in items[:3])
        return {"ok": True, "type": "share", "count": len(ids), "names": names}
    except u115web.Web115Error as err:
        return {"ok": False, "type": "share", "error": str(err)}


def _offline(cfg: Dict[str, Any], link: Dict[str, str], cid: str) -> Dict[str, Any]:
    try:
        info = u115ops.offline_add(cfg, link["url"], cid)
        return {"ok": True, "type": link["type"], "info_hash": info.get("info_hash", ""),
                "note": "已加入 115 离线任务（会员额度内秒传/下载）"}
    except u115web.Web115Error as err:
        return {"ok": False, "type": link["type"], "error": str(err)}
