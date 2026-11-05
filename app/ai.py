"""AI 辅助识别（OpenAI 兼容接口，默认 DeepSeek）。

只在「规则解析 + TMDB 都没认出来」时兜底，尽量少花钱：
输入一个文件名，要求模型只回 JSON：片名 / 年份 / 电影还是剧 / 季 / 集。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import net

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM = (
    "你是影视文件名识别助手。用户会给一个视频文件名，你判断它是哪部影视作品。"
    "只输出 JSON，不要解释，不要 markdown 代码块。字段："
    '{"title": "中文或原文片名", "year": 2024, "kind": "movie" 或 "tv", '
    '"season": 1, "episode": 2, "confidence": 0.0-1.0}。'
    "不知道的字段用 null；title 必须给出；如果只有电影名没有集数，kind 用 movie。"
)


class AiError(RuntimeError):
    pass


def configured(cfg: Dict[str, Any]) -> bool:
    ai = cfg.get("ai") or {}
    return bool(ai.get("enabled") and ai.get("key"))


def _endpoint(cfg: Dict[str, Any]) -> str:
    ai = cfg.get("ai") or {}
    base = str(ai.get("base") or DEFAULT_BASE).rstrip("/")
    return base + "/chat/completions"


def ask(cfg: Dict[str, Any], filename: str, hint: str = "") -> Optional[Dict[str, Any]]:
    ai = cfg.get("ai") or {}
    if not configured(cfg):
        return None
    user = f"文件名：{filename}"
    if hint:
        user += f"\n所在目录：{hint}"
    body = {
        "model": str(ai.get("model") or DEFAULT_MODEL),
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": 300,
        "stream": False,
    }
    try:
        data = net.post_json(
            _endpoint(cfg),
            json_body=body,
            headers={"Authorization": f"Bearer {ai.get('key')}", "Content-Type": "application/json"},
            proxy=str((cfg.get("metadata") or {}).get("proxy") or ""),
            timeout=float(ai.get("timeout") or 30),
        )
    except Exception as err:  # noqa: BLE001
        raise AiError(f"AI 请求失败：{err}") from err
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AiError("AI 返回格式不正确")
    return _parse_json(text)


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict) or not raw.get("title"):
        return None
    try:
        year = int(raw["year"]) if raw.get("year") else 0
    except (TypeError, ValueError):
        year = 0
    try:
        season = int(raw["season"]) if raw.get("season") is not None else 0
    except (TypeError, ValueError):
        season = 0
    try:
        episode = int(raw["episode"]) if raw.get("episode") is not None else 0
    except (TypeError, ValueError):
        episode = 0
    kind = str(raw.get("kind") or ("tv" if season or episode else "movie")).lower()
    kind = "tv" if kind in ("tv", "series", "show", "电视剧") else "movie"
    try:
        confidence = float(raw.get("confidence") or 0.6)
    except (TypeError, ValueError):
        confidence = 0.6
    return {
        "title": str(raw["title"]).strip(),
        "year": year,
        "kind": kind,
        "season": season,
        "episode": episode,
        "confidence": max(0.0, min(1.0, confidence)),
    }


def test(cfg: Dict[str, Any]) -> Dict[str, Any]:
    got = ask(cfg, "Dune.Part.Two.2024.2160p.WEB-DL.mkv")
    if not got:
        return {"ok": False, "error": "AI 没有返回可解析的 JSON"}
    return {"ok": True, "result": got}
