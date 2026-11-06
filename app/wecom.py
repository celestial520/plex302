"""企业微信「接收消息」回调：在微信里发链接 / 发指令，交给 Plex302 干活。

企业微信自建应用 → 接收消息 → 设置 API 接收：
  URL：https://你的域名/api/wecom/callback
  Token：随便填一串（这里填一样的）
  EncodingAESKey：随机生成 43 位（这里填一样的）

企业微信要求 5 秒内响应，所以重活（转存 / 离线）都丢到后台线程，
先回一句「收到」，干完再用应用消息把结果发回来。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import config, ingest, logger, notify

BLOCK = 32


class WecomError(RuntimeError):
    pass


def _sha1(*parts: str) -> str:
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def _key(aes_key: str) -> bytes:
    raw = (aes_key or "").strip()
    if len(raw) != 43:
        raise WecomError("EncodingAESKey 必须是 43 位")
    return base64.b64decode(raw + "=")


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > BLOCK:
        return data
    return data[:-pad]


def _pad(data: bytes) -> bytes:
    amount = BLOCK - (len(data) % BLOCK)
    return data + bytes([amount]) * amount


def decrypt(aes_key: str, encrypted: str) -> str:
    key = _key(aes_key)
    try:
        raw = base64.b64decode(encrypted)
    except Exception as err:  # noqa: BLE001
        raise WecomError("密文不是合法 base64") from err
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = _unpad(decryptor.update(raw) + decryptor.finalize())
    if len(plain) < 20:
        raise WecomError("解密结果过短")
    length = int.from_bytes(plain[16:20], "big")
    return plain[20: 20 + length].decode("utf-8", "ignore")


def encrypt(aes_key: str, text: str, corpid: str = "") -> str:
    key = _key(aes_key)
    body = text.encode("utf-8")
    payload = os.urandom(16) + len(body).to_bytes(4, "big") + body + (corpid or "").encode("utf-8")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(_pad(payload)) + encryptor.finalize()).decode()


def _xml_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def verify_url(cfg: Dict[str, Any], params: Dict[str, str]) -> str:
    ing = cfg["ingest"]
    token = str(ing.get("wecom_token") or "")
    aes_key = str(ing.get("wecom_aes_key") or "")
    signature = params.get("msg_signature") or ""
    timestamp = params.get("timestamp") or ""
    nonce = params.get("nonce") or ""
    echostr = params.get("echostr") or ""
    if not (token and aes_key):
        raise WecomError("未配置企业微信回调 Token / EncodingAESKey")
    if _sha1(token, timestamp, nonce, echostr) != signature:
        raise WecomError("签名校验失败")
    return decrypt(aes_key, echostr)


def handle_message(cfg: Dict[str, Any], body: bytes, params: Dict[str, str]) -> str:
    """返回要回给企业微信的 XML（已加密）。"""
    ing = cfg["ingest"]
    token = str(ing.get("wecom_token") or "")
    aes_key = str(ing.get("wecom_aes_key") or "")
    signature = params.get("msg_signature") or ""
    timestamp = params.get("timestamp") or ""
    nonce = params.get("nonce") or ""
    try:
        outer = ET.fromstring(body.decode("utf-8", "ignore"))
    except ET.ParseError as err:
        raise WecomError("回调报文不是 XML") from err
    encrypted = _xml_text(outer, "Encrypt")
    if _sha1(token, timestamp, nonce, encrypted) != signature:
        raise WecomError("签名校验失败")
    inner_xml = decrypt(aes_key, encrypted)
    try:
        inner = ET.fromstring(inner_xml)
    except ET.ParseError as err:
        raise WecomError("解密后的消息不是 XML") from err

    from_user = _xml_text(inner, "FromUserName")
    content = _xml_text(inner, "Content")
    msg_type = _xml_text(inner, "MsgType") or "text"
    allow = str(ing.get("allow_users") or "").strip()
    if allow and from_user not in [x.strip() for x in allow.split(",") if x.strip()]:
        reply = "你没有使用权限（把 UserID 加到「允许的用户」里）"
    elif msg_type != "text":
        reply = "只支持文字：发 115 分享链接、磁力或 ed2k"
    else:
        reply = dispatch(cfg, content, from_user)

    reply_xml = (
        "<xml><ToUserName><![CDATA[{to}]]></ToUserName>"
        "<FromUserName><![CDATA[{frm}]]></FromUserName>"
        "<CreateTime>{ts}</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[{content}]]></Content></xml>"
    ).format(
        to=from_user,
        frm=str(cfg["notify"].get("wecom_corpid") or ""),
        ts=int(time.time()),
        content=(reply or "")[:1200],
    )
    cipher = encrypt(aes_key, reply_xml, str(cfg["notify"].get("wecom_corpid") or ""))
    new_ts = str(int(time.time()))
    new_nonce = str(int(time.time() * 1000))
    sign = _sha1(token, new_ts, new_nonce, cipher)
    return (
        "<xml><Encrypt><![CDATA[{c}]]></Encrypt><MsgSignature><![CDATA[{s}]]></MsgSignature>"
        "<TimeStamp>{t}</TimeStamp><Nonce><![CDATA[{n}]]></Nonce></xml>"
    ).format(c=cipher, s=sign, t=new_ts, n=new_nonce)


def dispatch(cfg: Dict[str, Any], content: str, user: str = "") -> str:
    text = (content or "").strip()
    if not text:
        return help_text()
    links = ingest.parse_links(text)
    if links:
        _background(lambda: _run_ingest(text, user), "链接入库")
        kinds = "、".join({"share": "115 分享", "magnet": "磁力", "ed2k": "ed2k"}[x["type"]] for x in links)
        return f"收到 {kinds}，正在转存到入库目录，完成后再通知你。"
    if re.fullmatch(r"(状态|status)", text, re.I):
        return status_text(cfg)
    if re.search(r"(帮助|help|怎么用|\?)", text, re.I) and len(text) < 20:
        return help_text()
    return "没看懂。发 115 分享链接 / 磁力 / ed2k，或者发「状态」。\n\n" + help_text()


def help_text() -> str:
    return (
        "Plex302 指令：\n"
        "· 直接发 115 分享链接（带提取码也行）→ 转存到入库目录\n"
        "· 直接发 magnet / ed2k → 115 离线下载到待整理\n"
        "· 状态 → 看最近的入库和扫描结果"
    )


def status_text(cfg: Dict[str, Any]) -> str:
    from . import events, watcher

    lines = ["Plex302 状态"]
    ev = events.event_watcher.last_summary or {}
    if ev:
        lines.append(f"· 115 事件：检查 {ev.get('checked', 0)} 条 / 新 {ev.get('new', 0)} / 待扫 {ev.get('queued', 0)}")
    watch = watcher.watcher.last_summary or {}
    if watch:
        lines.append(f"· 目录监听：{watch.get('scanned', 0)} 个目录有更新")
    return "\n".join(lines)


def _background(func, label: str) -> None:
    def runner() -> None:
        try:
            func()
        except Exception as err:  # noqa: BLE001
            logger.error(f"[微信] {label}失败：{err}")
            notify.send_wecom(f"Plex302 · {label}失败", str(err), force=True)

    threading.Thread(target=runner, name=f"wecom-{label}", daemon=True).start()


def _run_ingest(text: str, user: str) -> None:
    cfg = config.load()
    result = ingest.ingest(text, cfg)
    if not result.get("ok"):
        notify.send_wecom("Plex302 · 入库失败", str(result.get("error")), force=True)
        return
    lines = []
    for item in result.get("results", []):
        if item.get("ok"):
            if item["type"] == "share":
                lines.append(f"· 已转存：{item.get('names') or ''}（{item.get('count')} 项）")
            else:
                lines.append(f"· 已离线：{item['type']}")
        else:
            lines.append(f"· 失败：{item.get('error')}")
    notify.send_wecom("Plex302 · 链接已入库", "\n".join(lines), force=True)
