"""TMDB 识别：把文件名变成「哪部片子、哪一季哪一集」。

只做最小必要的事：搜索 + 打分 + 取标题/年份。刮削（海报、nfo）交给 Plex 自己。
"""
from __future__ import annotations

import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from . import net

BASE_DEFAULT = "https://api.themoviedb.org/3"
_cache: Dict[str, Any] = {}
_lock = threading.RLock()
_TTL = 6 * 3600


class TmdbError(RuntimeError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def _year_of(item: Dict[str, Any]) -> int:
    raw = item.get("release_date") or item.get("first_air_date") or ""
    match = re.match(r"(\d{4})", str(raw))
    return int(match.group(1)) if match else 0


def _title_of(item: Dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or "").strip()


def _cache_get(key: str) -> Any:
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any) -> None:
    with _lock:
        if len(_cache) > 800:
            _cache.clear()
        _cache[key] = (time.time(), value)


def configured(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("metadata") or {}).get("tmdb_key"))


def _get(cfg: Dict[str, Any], path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    meta = cfg.get("metadata") or {}
    key = str(meta.get("tmdb_key") or "").strip()
    if not key:
        raise TmdbError("未配置 TMDB API Key（设置 → 识别与整理）")
    base = str(meta.get("tmdb_base") or BASE_DEFAULT).rstrip("/")
    query = dict(params)
    query["api_key"] = key
    query["language"] = str(meta.get("tmdb_lang") or "zh-CN")
    try:
        return net.get_json(f"{base}/{path.lstrip('/')}", params=query, proxy=str(meta.get("proxy") or ""), timeout=18)
    except Exception as err:  # noqa: BLE001
        raise TmdbError(f"TMDB 请求失败：{err}") from err


def search(cfg: Dict[str, Any], query: str, kind: str = "movie", year: int = 0) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    cache_key = f"{kind}:{query}:{year}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    params: Dict[str, Any] = {"query": query, "include_adult": "false"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = str(year)
    try:
        data = _get(cfg, f"search/{kind}", params)
    except TmdbError:
        raise
    rows = data.get("results") or []
    out = [
        {
            "id": int(row.get("id") or 0),
            "title": _title_of(row),
            "original_title": str(row.get("original_title") or row.get("original_name") or ""),
            "year": _year_of(row),
            "popularity": float(row.get("popularity") or 0),
            "votes": int(row.get("vote_count") or 0),
            "kind": kind,
        }
        for row in rows
        if row.get("id") and _title_of(row)
    ]
    _cache_set(cache_key, out)
    return out


def best(
    cfg: Dict[str, Any], query: str, kind: str = "movie", year: int = 0
) -> Optional[Dict[str, Any]]:
    """返回最可信的一条，置信度低于 0.52 视为没认出来。"""
    rows = search(cfg, query, kind, year)
    if not rows and year:
        # 年份在文件名里经常是上映年/压制年，查不到就放宽再搜一次
        rows = search(cfg, query, kind, 0)
    if not rows:
        return None
    best_row, best_score = None, 0.0
    for row in rows[:12]:
        score = _similar(query, row["title"]) * 0.7 + _similar(query, row["original_title"]) * 0.3
        if year and row["year"]:
            diff = abs(row["year"] - year)
            score *= 1.0 if diff == 0 else (0.86 if diff == 1 else 0.62)
        elif year and not row["year"]:
            score *= 0.9
        # 热门度只做微调，避免把一个冷门正确结果挤掉
        score += min(row["popularity"], 100) / 100 * 0.03
        if score > best_score:
            best_row, best_score = row, score
    if not best_row or best_score < 0.52:
        return None
    out = dict(best_row)
    out["score"] = round(min(best_score, 1.0), 3)
    return out


def test(cfg: Dict[str, Any]) -> Dict[str, Any]:
    rows = search(cfg, "沙丘", "movie")
    if not rows:
        return {"ok": False, "error": "搜索无结果，检查 Key 或网络"}
    return {"ok": True, "count": len(rows), "sample": rows[0]["title"], "year": rows[0]["year"]}
