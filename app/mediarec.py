"""媒体识别：文件名解析 → TMDB → AI 兜底。

输出是一个「模板变量字典」，字段对齐 CMS wiki 的变量说明：
  title / year / tmdb_id / first_letter / resource_* / season_* ...
另带 _type (movie|tv) 与 _tmdb（详情，供二级分类用）。

AI 只在本地 + TMDB 都识别不出来时调用（CMS 的「辅助使用」），
也可以设成「强制使用」先问 AI。任何 AI 结果仍要回 TMDB 校验，
避免模型编造一个不存在的 tmdb_id。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import requests

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SE = re.compile(r"(?i)\bS(\d{1,2})[ ._-]*E(\d{1,3})\b")
_SEASON = re.compile(r"(?i)(?:season|第)\s*(\d{1,2})\s*(?:季)?")
_EPISODE = re.compile(r"(?i)\bE(\d{1,3})\b|第\s*(\d{1,3})\s*集")
_TMDB_TAG = re.compile(r"(?i)(?:tmdb(?:id)?|tmdbid)\s*[=:_-]\s*(\d{2,12})")

_PIX = ["2160P", "1080P", "720P", "4K", "8K", "480P"]
_TYPE = ["BLURAY", "BLU-RAY", "BDRIP", "BRRIP", "REMUX", "WEB-DL", "WEBRIP", "WEB", "HDTV", "DVDRIP", "UHD"]
_ENCODE = ["X265", "H265", "H.265", "HEVC", "X264", "H264", "H.264", "AV1", "10BIT", "8BIT"]
_AUDIO = ["TRUEHD", "ATMOS", "DDP", "DD+", "DTS-HD", "DTS", "AC3", "EAC3", "AAC", "FLAC", "7.1", "5.1"]
_EFFECT = ["DV.HDR", "DOLBY.VISION", "DOVI", "HDR10+", "HDR10", "HDR", "DV", "SDR"]
_SOURCE = ["ITUNES", "DSNP", "HMAX", "NF", "AMZN", "BILI", "IQIYI", "TENCENT", "YOUKU", "USA.UHD"]
_VERSION = ["IMAX", "EXTENDED", "DIRECTOR", "UNCUT", "REPACK", "PROPER", "3D", "HQ", "CC", "DC"]
_TEAM_HINT = re.compile(r"-([A-Za-z0-9][A-Za-z0-9]{1,20})$")
_TEAM_STOP = {
    "WEB", "DL", "WEBDL", "WEBRIP", "BLURAY", "BDRIP", "BRRIP", "REMUX", "HDTV",
    "X264", "X265", "H264", "H265", "HEVC", "AV1", "10BIT", "8BIT", "HDR", "DV",
    "DOVI", "2160P", "1080P", "720P", "4K", "8K", "UHD", "ISO", "AAC", "DDP",
    "TRUEHD", "ATMOS", "DTS", "NF", "DSNP", "HMAX", "AMZN", "ITUNES", "SDR",
    "CN", "US", "HK", "TW", "JP", "KR", "CC", "DC", "HQ",
}

# GB2312 拼音首字母分区（常用汉字，无需第三方依赖）
_GB_TABLE = [
    (-20319, -20284, "A"), (-20283, -19776, "B"), (-19775, -19219, "C"),
    (-19218, -18711, "D"), (-18710, -18527, "E"), (-18526, -18240, "F"),
    (-18239, -17923, "G"), (-17922, -17418, "H"), (-17417, -16475, "J"),
    (-16474, -16213, "K"), (-16212, -15641, "L"), (-15640, -15166, "M"),
    (-15165, -14923, "N"), (-14922, -14915, "O"), (-14914, -14631, "P"),
    (-14630, -14150, "Q"), (-14149, -14091, "R"), (-14090, -13319, "S"),
    (-13318, -12839, "T"), (-12838, -12557, "W"), (-12556, -11848, "X"),
    (-11847, -11056, "Y"), (-11055, -10247, "Z"),
]


def first_letter(text: str) -> str:
    """标题首字母：中文取拼音首字母，英文/数字直接取首字符，统一大写。"""
    for ch in str(text or "").strip():
        if ch.isspace() or ch in "·:：-.,!?【】[]()（）":
            continue
        if ch.isascii():
            return ch.upper() if ch.isalnum() else ""
        try:
            raw = ch.encode("gb2312")
        except UnicodeEncodeError:
            return ""
        if len(raw) != 2:
            return ""
        code = raw[0] * 256 + raw[1] - 65536
        for low, high, letter in _GB_TABLE:
            if low <= code <= high:
                return letter
        return ""
    return ""


def _find(upper: str, words: list[str]) -> str:
    for word in words:
        if word in upper:
            return word.replace(".", "").replace("-", "") if word in ("DDP",) else word
    return ""


def parse_name(filename: str) -> Dict[str, Any]:
    """从文件名里抽出标题、年份、季集和 CMS 的资源变量（不联网）。"""
    original = str(filename or "")
    stem, _, ext = original.rpartition(".")
    if not stem:
        stem, ext = original, ""
    upper = original.upper()

    tag = _TMDB_TAG.search(original)
    tmdb_id = tag.group(1) if tag else ""
    years = _YEAR.findall(stem)
    year = years[0] if years else ""
    se = _SE.search(original)
    season = int(se.group(1)) if se else 0
    episode = int(se.group(2)) if se else 0
    if not season:
        m = _SEASON.search(original)
        season = int(m.group(1)) if m else 0
    if not episode:
        m = _EPISODE.search(original)
        episode = int(m.group(1) or m.group(2)) if m else 0

    # 标题 = 年份 / 季集 / 方括号标签之前那段
    cut = len(stem)
    for pattern in (r"(?i)\bS\d{1,2}[ ._-]*E\d{1,3}\b", r"(?i)\b(?:19|20)\d{2}\b", r"\[", r"\("):
        m = re.search(pattern, stem)
        if m:
            cut = min(cut, m.start())
    title = stem[:cut]
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"(?i)\b(2160P|1080P|720P|4K|BLURAY|WEB-?DL|REMUX|X26[45]|H26[45])\b.*$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -_.")

    fps_m = re.search(r"(?i)(\d{2,3})\s*FPS", original)
    pix = _find(upper, _PIX)
    if pix.endswith("P") and pix[:-1].isdigit():
        pix = pix.lower()  # CMS 变量示例是 2160p 而不是 2160P
    rtype = _find(upper, _TYPE)
    encode = _find(upper, _ENCODE)
    audio = _find(upper, _AUDIO)
    effect = _find(upper, _EFFECT)
    source = _find(upper, _SOURCE)
    version = _find(upper, _VERSION)
    team = ""
    team_m = _TEAM_HINT.search(stem)
    if team_m and team_m.group(1).upper() not in _TEAM_STOP:
        team = team_m.group(1)

    return {
        "original_name": original,
        "ext": ext.lower(),
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "resource_pix": pix,
        "resource_type": rtype,
        "video_encode": encode,
        "audio_encode": audio,
        "resource_effect": effect,
        "resource_source": source,
        "resource_version": version,
        "resource_team": team,
        "fps": f"{fps_m.group(1)}FPS" if fps_m else "",
        "season_num": season,
        "episode_num": episode,
        "season_episode": f"S{season:02d}E{episode:02d}" if season and episode else "",
        "disc_num": "",
        "season_name": "",
        "season_year": "",
        "episode_name": "",
        "_type": "tv" if (season or episode) else "",
    }


class TMDB:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        c = (cfg.get("organize") or {}).get("tmdb") or {}
        self.base = str(c.get("api_base") or "https://api.tmdb.org").rstrip("/")
        self.key = str(c.get("api_key") or "")
        self.lang = str(c.get("language") or "zh-CN")

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        params.update({"api_key": self.key, "language": self.lang})
        resp = requests.get(self.base + path, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def test(self) -> Dict[str, Any]:
        if not self.key:
            return {"ok": False, "error": "没填 TMDB API 密钥"}
        try:
            data = self._get("/3/configuration")
            return {"ok": bool(data), "error": "" if data else "返回为空"}
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": str(err)[:160]}

    def search(self, title: str, year: str = "", media_type: str = "movie") -> Optional[Dict[str, Any]]:
        if not (self.key and title):
            return None
        kind = "movie" if media_type == "movie" else "tv"
        params: Dict[str, Any] = {"query": title, "page": 1}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        try:
            data = self._get(f"/3/search/{kind}", **params)
        except Exception:  # noqa: BLE001
            return None
        results = data.get("results") or []
        if not results and year:
            try:
                results = (self._get(f"/3/search/{kind}", query=title, page=1) or {}).get("results") or []
            except Exception:  # noqa: BLE001
                return None
        return results[0] if results else None

    def detail(self, tmdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
        if not (self.key and tmdb_id):
            return None
        kind = "movie" if media_type == "movie" else "tv"
        try:
            data = self._get(f"/3/{kind}/{tmdb_id}")
        except Exception:  # noqa: BLE001
            return None
        return data or None


class AIRec:
    """OpenAI 兼容协议；只在识别不出来时兜底。"""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        c = (cfg.get("organize") or {}).get("ai") or {}
        self.mode = str(c.get("mode") or "disabled")
        base = str(c.get("api_base") or "").rstrip("/")
        self.url = base if base.endswith("/chat/completions") else (
            base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
        )
        self.model = str(c.get("model") or "")
        self.key = str(c.get("api_key") or "")

    def test(self) -> Dict[str, Any]:
        if not (self.key and self.url.startswith("http") and self.model):
            return {"ok": False, "error": "没填 AI 地址、模型或密钥"}
        data = self.ask("测试文件 Iron.Man.2008.1080p.mkv")
        if not data:
            return {"ok": False, "error": "模型没有返回有效 JSON（检查地址 / 模型 / 密钥）"}
        return {"ok": True, "detail": str(data)[:200]}

    def ask(self, filename: str) -> Optional[Dict[str, Any]]:
        if not (self.key and self.url.startswith("http") and self.model):
            return None
        prompt = (
            "从影视文件名里识别媒体信息，只返回 JSON，不要解释。"
            '格式：{"title":"中文或原文标题","year":"4位年份","type":"movie或tv",'
            '"season":季号数字或0,"episode":集号数字或0,"tmdb_id":"已知则填否则空"}。'
            "标题要去掉分辨率、编码、发布组等噪音；不确定就留空。"
        )
        try:
            resp = requests.post(
                self.url,
                headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
                json={"model": self.model, "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": filename},
                ], "temperature": 0},
                timeout=25,
            )
            resp.raise_for_status()
            text = (((resp.json() or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r"\{[^{}]*\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except ValueError:
            return None


def recognize(filename: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """返回模板变量字典；识别不出来返回 None（由调用方分流到冗余目录）。"""
    info = parse_name(filename)
    tmdb = TMDB(cfg)
    ai = AIRec(cfg)
    title, year = info["title"], info["year"]
    media_type = info["_type"] or "movie"

    if ai.mode == "force" and ai.key:
        hint = ai.ask(filename)
        if hint:
            title = str(hint.get("title") or title)
            year = str(hint.get("year") or year)
            media_type = str(hint.get("type") or media_type)
            if hint.get("tmdb_id"):
                info["tmdb_id"] = str(hint["tmdb_id"])
            if not info["season_num"] and hint.get("season"):
                info["season_num"] = int(hint["season"] or 0)
            if not info["episode_num"] and hint.get("episode"):
                info["episode_num"] = int(hint["episode"] or 0)

    found = None
    if info["tmdb_id"]:
        found = {"id": info["tmdb_id"]}
    elif title:
        found = tmdb.search(title, year, media_type)
        if not found and media_type == "movie":
            found = tmdb.search(title, year, "tv")
            if found:
                media_type = "tv"
        if not found and ai.mode in ("assist", "force") and ai.key and ai.mode != "force":
            hint = ai.ask(filename)
            if hint:
                gtitle = str(hint.get("title") or "")
                gyear = str(hint.get("year") or "")
                gtype = str(hint.get("type") or "movie")
                if gtitle:
                    found = tmdb.search(gtitle, gyear, gtype)
                    if found:
                        media_type = gtype
                        title, year = gtitle, gyear

    if not found:
        return None
    tid = str(found.get("id") or info["tmdb_id"] or "")
    detail = tmdb.detail(tid, media_type) or {}
    name = detail.get("title") or detail.get("name") or found.get("title") or found.get("name") or title
    date = detail.get("release_date") or detail.get("first_air_date") or found.get("release_date") or found.get("first_air_date") or ""
    info.update({
        "title": str(name),
        "year": str(year or (date[:4] if date else "")),
        "tmdb_id": tid,
        "_type": media_type,
        "_tmdb": detail,
    })
    info["first_letter"] = first_letter(info["title"])
    if media_type == "tv" and info["season_num"] and info["episode_num"]:
        info["season_episode"] = f"S{info['season_num']:02d}E{info['episode_num']:02d}"
    if not info["tmdb_id"]:
        return None
    return info
