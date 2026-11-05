"""通知：企业微信应用消息。

和 115 秒传助手用的是同一套（corpid + secret + agentid + touser，可走代理），
这样你只需要在一个地方维护企业微信配置。

扫描是批量的：一轮事件可能触发好几个目录，通知会等 15 秒汇总成一条，
避免一次整理发十几条消息。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import requests

from . import config, logger

DEFAULT_WECOM_BASE = "https://qyapi.weixin.qq.com"
_token_cache: Dict[str, Any] = {"token": "", "expire_at": 0, "key": ""}
_lock = threading.Lock()

# 待发送的扫描结果（15 秒窗口内合并）
_pending: List[Dict[str, Any]] = []
_pending_lock = threading.Lock()
FLUSH_SECONDS = 15
_last_error_at: Dict[str, float] = {}


def _truncate(text: str, limit: int = 1800) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _wecom_base(cfg: Dict[str, Any]) -> str:
    proxy = str(cfg["notify"].get("wecom_proxy") or "").strip().rstrip("/")
    return proxy or DEFAULT_WECOM_BASE


def _access_token(cfg: Dict[str, Any]) -> Optional[str]:
    notify = cfg["notify"]
    corpid = str(notify.get("wecom_corpid") or "").strip()
    secret = str(notify.get("wecom_secret") or "").strip()
    if not (corpid and secret):
        return None
    now = time.time()
    cache_key = f"{corpid}|{_wecom_base(cfg)}"
    with _lock:
        if _token_cache["token"] and _token_cache["key"] == cache_key and _token_cache["expire_at"] > now:
            return _token_cache["token"]
    try:
        resp = requests.get(
            f"{_wecom_base(cfg)}/cgi-bin/gettoken",
            params={"corpid": corpid, "corpsecret": secret},
            timeout=15,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as err:
        logger.warn(f"[通知] 获取企业微信 token 失败：{err}")
        return None
    token = data.get("access_token")
    if not token:
        logger.warn(f"[通知] 企业微信 token 返回异常：{data.get('errcode')} {data.get('errmsg')}")
        return None
    with _lock:
        _token_cache.update(
            {
                "token": token,
                "key": cache_key,
                "expire_at": now + int(data.get("expires_in") or 7200) - 300,
            }
        )
    return token


def send_wecom(title: str, content: str, force: bool = False) -> Optional[str]:
    """发一条应用消息。返回 None 表示成功，否则是错误说明。"""
    cfg = config.load()
    notify = cfg["notify"]
    if not force and not notify.get("wecom_enabled"):
        return "企业微信通知未启用"
    agentid = str(notify.get("wecom_agentid") or "").strip()
    if not agentid:
        return "企业微信配置不完整（CorpID / Secret / AgentID 必填）"
    token = _access_token(cfg)
    if not token:
        return "获取企业微信 access_token 失败"
    payload = {
        "touser": str(notify.get("wecom_touser") or "@all"),
        "msgtype": "text",
        "agentid": int(agentid) if agentid.isdigit() else agentid,
        "safe": 0,
        "text": {"content": _truncate(f"{title}\n{content}")},
    }
    try:
        resp = requests.post(
            f"{_wecom_base(cfg)}/cgi-bin/message/send",
            params={"access_token": token},
            json=payload,
            timeout=20,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as err:
        return f"企业微信发送异常：{err}"
    if data.get("errcode") not in (0, None):
        return f"企业微信返回错误：{data.get('errcode')} {data.get('errmsg')}"
    logger.info(f"[通知] 企业微信已发送：{title}")
    return None


# ------------------------------------------------------------------ 扫描汇总
def queue_scan(target: str, section: str = "", ok: bool = True, message: str = "", source: str = "") -> None:
    """把一次扫描结果放进待发送队列；由 flush_loop 定时汇总。"""
    if not config.load()["notify"].get("on_scan", True):
        return
    with _pending_lock:
        _pending.append(
            {
                "target": target,
                "section": section,
                "ok": ok,
                "message": message,
                "source": source,
                "at": time.time(),
            }
        )


def flush(force: bool = False) -> None:
    cfg = config.load()
    if not cfg["notify"].get("wecom_enabled"):
        with _pending_lock:
            _pending.clear()
        return
    with _pending_lock:
        if not _pending:
            return
        items = _pending[:]
        _pending.clear()
    ok_items = [x for x in items if x["ok"]]
    bad_items = [x for x in items if not x["ok"]]
    lines: List[str] = []
    if ok_items:
        lines.append(f"已刷新 {len(ok_items)} 个目录")
        for x in ok_items[:12]:
            name = x["target"].rstrip("/").split("/")[-1] or x["target"]
            lines.append(f"· {name}")
            lines[-1] += f"（{x['section']}）" if x["section"] else ""
        if len(ok_items) > 12:
            lines.append(f"…… 还有 {len(ok_items) - 12} 个")
    if bad_items:
        if lines:
            lines.append("")
        lines.append(f"失败 {len(bad_items)} 个")
        for x in bad_items[:6]:
            lines.append(f"· {x['target']}：{x['message'] or '未知错误'}")
    title = "Plex302 · 入库完成" if not bad_items else "Plex302 · 部分扫描失败"
    err = send_wecom(title, "\n".join(lines), force=force)
    if err:
        logger.warn(f"[通知] {err}")


async def flush_loop() -> None:
    import asyncio

    while True:
        try:
            await asyncio.sleep(FLUSH_SECONDS)
            flush()
        except asyncio.CancelledError:
            return
        except Exception as err:  # noqa: BLE001
            logger.warn(f"[通知] 汇总发送失败：{err}")


# ------------------------------------------------------------------ 全量 / 错误
def notify_full(total: int, ok: int, failed: int, elapsed: float, details: str = "") -> None:
    cfg = config.load()
    if not cfg["notify"].get("wecom_enabled") or not cfg["notify"].get("on_full", True):
        return
    lines = [f"{total} 个媒体库 · 成功 {ok} · 失败 {failed}", f"耗时 {elapsed:.0f}s"]
    if details:
        lines.append("")
        lines.append(details)
    title = "Plex302 · 全量同步完成" if not failed else "Plex302 · 全量同步有失败"
    err = send_wecom(title, "\n".join(lines))
    if err:
        logger.warn(f"[通知] {err}")


def notify_error(message: str, key: str = "error", cooldown: int = 1800) -> None:
    """同类错误 30 分钟内只发一次，避免 Cookie 失效刷屏。"""
    cfg = config.load()
    if not cfg["notify"].get("wecom_enabled") or not cfg["notify"].get("on_error", True):
        return
    now = time.time()
    if _last_error_at.get(key, 0) > now - cooldown:
        return
    _last_error_at[key] = now
    err = send_wecom("Plex302 · 需要处理", message)
    if err:
        logger.warn(f"[通知] {err}")


def test() -> Dict[str, Any]:
    err = send_wecom(
        "Plex302 · 测试通知",
        "如果你看到这条消息，说明企业微信应用消息配置正确。\n"
        "入库完成、全量同步和 Cookie 失效都会发到这里。",
        force=True,
    )
    return {"ok": err is None, "error": err or ""}
