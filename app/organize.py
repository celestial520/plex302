"""整理引擎：把「待整理」里的真实文件，按 TMDB 识别结果改名 + 归档。

和 CMS 的区别：
  - 不做 STRM，不下载，不刮削；只动 115 上的文件（移动 + 重命名）；
  - 整理完直接告诉 Plex 扫哪个目录（本项目本来就负责 302 和扫库）。

安全策略：
  - 默认 dry_run=true，只出「将会怎么改」的清单，确认后才真的动文件；
  - 只处理视频，其余文件（字幕/图片/nfo）原样留在待整理目录；
  - 认不出来的不硬猜，留在原地下次再说；
  - 目标已存在时默认跳过，避免洗版误删。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from . import ai, config, logger, plex, resolver, scanner, store, tmdb, u115ops, u115web

QUALITY_TOKENS = {
    "2160p", "1080p", "720p", "480p", "4k", "8k", "uhd", "webdl", "web-dl", "webrip", "web",
    "bluray", "blu-ray", "bdrip", "brrip", "dvdrip", "hdtv", "hdrip", "remux", "hdr", "hdr10",
    "dovi", "dv", "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "ddp",
    "atmos", "truehd", "10bit", "8bit", "repack", "proper", "internal", "extended", "imax",
    "chs", "cht", "chinese", "mandarin", "cantonese", "dubbed", "subbed", "hfr", "3d",
}

CN_EPISODE = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")
SXXEXX = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})")
SXX_ONLY = re.compile(r"(?:^|[\s._\-\[])[Ss](\d{1,2})(?:[\s._\-\]]|$)")
EP_ONLY = re.compile(r"(?:^|[\s._\-\[])(?:EP|E)(\d{1,4})(?:[\s._\-\]]|$)", re.I)
YEAR_RE = re.compile(r"(?:^|[\s._\-\(\[（【])((?:19|20)\d{2})(?:[\s._\-\)\]）】]|$)")
BRACKET_RE = re.compile(r"[\[\(【（][^\]\)】）]{0,40}[\]\)】）]")

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class OrganizeError(RuntimeError):
    pass


def sanitize(name: str, limit: int = 120) -> str:
    text = _ILLEGAL.sub(" ", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].strip(" .")


def _stem(filename: str) -> str:
    base = os.path.basename(str(filename or ""))
    stem, _ = os.path.splitext(base)
    return stem


def parse_name(filename: str, parent: str = "") -> Dict[str, Any]:
    """从文件名里抠出 片名 / 年份 / 季 / 集。抠不出片名就退回目录名。"""
    raw = _stem(filename)
    text = re.sub(r"[._]+", " ", raw)
    text = text.replace("【", " [").replace("】", "] ")

    year = 0
    year_match = YEAR_RE.search(text)
    if year_match:
        year = int(year_match.group(1))
        text = text[: year_match.start()] + " " + text[year_match.end():]

    season = episode = 0
    match = SXXEXX.search(text)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        text = text[: match.start()] + " " + text[match.end():]
    else:
        cn = CN_EPISODE.search(text)
        if cn:
            episode = int(cn.group(1))
            text = text[: cn.start()] + " " + text[cn.end():]
        else:
            ep = EP_ONLY.search(text)
            if ep:
                episode = int(ep.group(1))
                text = text[: ep.start()] + " " + text[ep.end():]
        season_match = SXX_ONLY.search(text)
        if season_match:
            season = int(season_match.group(1))
            text = text[: season_match.start()] + " " + text[season_match.end():]

    # 年份和季集已经拿走，剩下的方括号多是压制组/字幕组标记
    text = BRACKET_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # 截断到第一个画质/编码标记之前，剩下的就是片名
    words = text.split()
    kept: List[str] = []
    for word in words:
        token = re.sub(r"[^0-9a-zA-Z\-]", "", word).lower()
        if (
            token in QUALITY_TOKENS
            or re.fullmatch(r"\d{3,4}p", token or "")
            or re.search(r"\d{3,4}p$", token or "")
            or token in ("bd", "hd", "web", "blu", "ray")
        ):
            break
        kept.append(word)
    title = " ".join(kept).strip(" -_.")
    title = re.sub(r"(?i)\b(?:complete|season|全集|合集)\b.*$", "", title).strip(" -_.")

    # 文件名里没有片名（比如 01.mkv / E02.mp4 / 纯数字），用目录名兜底
    if (len(title) < 2 or title.isdigit()) and parent:
        parent_parsed = parse_name(parent + ".mkv", "")
        title = parent_parsed["title"]
        year = year or parent_parsed["year"]
        season = season or parent_parsed["season"]
        episode = episode or parent_parsed["episode"]

    if season or episode:
        kind = "tv"
    else:
        kind = "movie"
    return {
        "title": title.strip(),
        "year": year,
        "kind": kind,
        "season": season,
        "episode": episode,
        "raw": raw,
    }


def _format(template: str, data: Dict[str, Any], fallback: str) -> str:
    try:
        out = template.format(**data)
    except (KeyError, IndexError, ValueError):
        out = fallback.format(**data)
    # 年份缺失时别留下 "沙丘2 ()" 这种空括号
    out = re.sub(r"[\(\[]\s*[\)\]]", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    return "/".join(sanitize(part, 110) for part in out.replace("\\", "/").split("/") if part.strip())


def _query_variants(title: str) -> List[str]:
    """中英混排的文件名，拆成「中文名」「英文名」「前几个词」分别去搜。"""
    cjk = " ".join(re.findall(r"[\u4e00-\u9fff][\u4e00-\u9fff0-9]*", title))
    latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", title))
    out: List[str] = []
    for raw in (title, cjk, latin, " ".join(title.split()[:3])):
        value = re.sub(r"\s+", " ", raw or "").strip(" -_.")
        if len(value) >= 2 and value not in out:
            out.append(value)
    return out


class Organizer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.last_run = 0.0
        self.last_summary: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 入口
    def run(self, dry_run: Optional[bool] = None, limit: int = 0) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise OrganizeError("整理任务正在运行，请稍后再试")
        try:
            self.running = True
            cfg = config.load()
            org = cfg["organize"]
            if not org.get("enabled"):
                raise OrganizeError("整理功能未启用（设置 → 识别与整理）")
            if not u115web.has_cookie(cfg):
                raise OrganizeError("未配置 115 Cookie")
            dry = bool(org.get("dry_run")) if dry_run is None else bool(dry_run)
            started = time.time()
            sources = [str(x) for x in org.get("source_dirs") or [] if str(x).strip()]
            if not sources:
                raise OrganizeError("没有设置待整理目录")
            targets = self._collect_sources(cfg, sources, int(org.get("depth") or 2))
            if limit:
                targets = targets[: int(limit)]
            plans = [self._plan(cfg, item) for item in targets]
            if not dry:
                self._execute(cfg, plans)
            summary = self._summary(plans, dry, started)
            self.last_run = time.time()
            self.last_summary = summary
            for row in plans:
                if row["action"] in ("move", "done", "failed"):
                    store.add_organize(
                        row.get("source_path", ""),
                        row.get("target_path", ""),
                        "dry" if dry else row["action"],
                        row.get("reason", ""),
                        row.get("match", {}).get("title", ""),
                        row.get("match", {}).get("year", 0),
                    )
            logger.info(
                f"[整理] {'预演' if dry else '执行'}完成：{summary['planned']} 个待整理 / "
                f"{summary['done']} 成功 / {summary['failed']} 失败 / {summary['skipped']} 跳过"
            )
            return summary
        finally:
            self.running = False
            self._lock.release()

    # ---------------------------------------------------------------- 收集
    def _collect_sources(self, cfg: Dict[str, Any], sources: List[str], depth: int) -> List[Dict[str, Any]]:
        exts = tuple(cfg["scan"]["video_exts"])
        min_size = int(cfg["organize"].get("min_size_mb") or 0) * 1024 * 1024
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def walk(path: str, level: int) -> None:
            cid = u115ops.find_dir(cfg, path)
            if not cid:
                logger.warn(f"[整理] 待整理目录不存在：{path}")
                return
            for item in u115web.list_dir(cfg, cid, force=level == 0):
                fid = str(u115ops._pick(item, "fid", "file_id", "id", default=""))  # noqa: SLF001
                name = str(u115ops._pick(item, "n", "file_name", "name", default=""))  # noqa: SLF001
                is_dir = bool(u115ops._pick(item, "is_dir", default=False)) or not str(  # noqa: SLF001
                    u115ops._pick(item, "pc", "pick_code", default="")  # noqa: SLF001
                )
                if is_dir:
                    if level < depth and name and not name.startswith("."):
                        walk(path.rstrip("/") + "/" + name, level + 1)
                    continue
                if not name.lower().endswith(exts) or fid in seen:
                    continue
                size = int(u115ops._pick(item, "s", "file_size", "size", default=0) or 0)  # noqa: SLF001
                if size and size < min_size:
                    continue
                seen.add(fid)
                out.append(
                    {
                        "file_id": fid,
                        "name": name,
                        "size": size,
                        "path": path.rstrip("/") + "/" + name,
                        "folder": path.rstrip("/"),
                    }
                )

        for source in sources:
            walk(source.rstrip("/") or "/", 0)
        return out

    # ---------------------------------------------------------------- 识别
    def _match(self, cfg: Dict[str, Any], parsed: Dict[str, Any], filename: str, folder: str) -> Dict[str, Any]:
        org = cfg["organize"]
        title, year, kind = parsed["title"], parsed["year"], parsed["kind"]
        source = "name"
        result: Optional[Dict[str, Any]] = None
        if title and tmdb.configured(cfg):
            for query in _query_variants(title):
                result = tmdb.best(cfg, query, kind, year)
                if result:
                    break
            if not result and kind == "movie":
                # 电影名里带季集，或者其实是剧：换个类型再试一次
                for query in _query_variants(title):
                    result = tmdb.best(cfg, query, "tv", year)
                    if result:
                        kind = "tv"
                        break
        if not result and title and tmdb.configured(cfg):
            # 用目录名再试（很多资源文件夹名比文件名规范）
            folder_name = os.path.basename(folder.rstrip("/"))
            if folder_name and folder_name != title:
                alt = parse_name(folder_name + ".mkv")
                if alt["title"]:
                    result = tmdb.best(cfg, alt["title"], kind, alt["year"] or year)
                    if result:
                        source = "folder"
                        year = year or alt["year"]
        if not result and ai.configured(cfg):
            got = ai.ask(cfg, filename, folder)
            if got:
                if got.get("season"):
                    parsed["season"] = parsed["season"] or got["season"]
                if got.get("episode"):
                    parsed["episode"] = parsed["episode"] or got["episode"]
                if tmdb.configured(cfg):
                    result = tmdb.best(cfg, got["title"], got["kind"], got["year"])
                if not result and got.get("confidence", 0) >= 0.6:
                    result = {
                        "id": 0,
                        "title": got["title"],
                        "year": got["year"],
                        "kind": got["kind"],
                        "score": got.get("confidence", 0.6),
                    }
                    source = "ai"
                if result:
                    kind = result.get("kind") or kind
                    year = year or got.get("year") or 0
        if not result:
            return {"ok": False, "reason": "没认出来（TMDB 无匹配）", "parsed": parsed}
        if result.get("kind") == "tv":
            kind = "tv"
        return {"ok": True, "match": result, "parsed": parsed, "kind": kind, "source": source, "year": year}

    # ---------------------------------------------------------------- 规划
    def _plan(self, cfg: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        org = cfg["organize"]
        parsed = parse_name(item["name"], item["folder"])
        row: Dict[str, Any] = {
            "source_path": item["path"],
            "source_name": item["name"],
            "file_id": item["file_id"],
            "size": item["size"],
            "parsed": parsed,
            "action": "skip",
            "reason": "",
        }
        try:
            matched = self._match(cfg, parsed, item["name"], item["folder"])
        except (tmdb.TmdbError, ai.AiError, u115web.Web115Error) as err:
            row["reason"] = str(err)
            row["action"] = "failed"
            return row
        if not matched["ok"]:
            row["reason"] = matched["reason"]
            return row
        match = matched["match"]
        kind = matched["kind"]
        year = int(match.get("year") or parsed.get("year") or 0)
        data = {
            "title": sanitize(match.get("title") or parsed["title"]),
            "year": year or "",
            "season": int(parsed.get("season") or 1),
            "episode": int(parsed.get("episode") or 1),
            "tmdb_id": match.get("id") or "",
        }
        if kind == "tv" and not parsed.get("episode"):
            row["reason"] = "像是剧集但文件名里没有集号，先不动（避免把整季塞进一集）"
            row["match"] = match
            return row
        if kind == "tv":
            rel = _format(
                str(org.get("tv_template") or "{title} ({year})/Season {season:02d}/"
                     "{title} ({year}) - S{season:02d}E{episode:02d}"),
                data,
                "{title} ({year})/Season {season:02d}/{title} ({year}) - S{season:02d}E{episode:02d}",
            )
            base_dir = str(org.get("tv_dir") or "").rstrip("/")
        else:
            rel = _format(
                str(org.get("movie_template") or "{title} ({year})/{title} ({year})"),
                data,
                "{title} ({year})/{title} ({year})",
            )
            base_dir = str(org.get("movie_dir") or "").rstrip("/")
        if not base_dir:
            row["reason"] = "没有设置电影 / 剧集归档目录（设置 → 识别与整理）"
            row["match"] = match
            return row
        ext = os.path.splitext(item["name"])[1].lower() or ".mkv"
        target_dir = (base_dir + "/" + os.path.dirname(rel)).rstrip("/")
        target_name = os.path.basename(rel) + ext
        row.update(
            {
                "match": match,
                "kind": kind,
                "source": matched["source"],
                "target_dir": target_dir,
                "target_name": target_name,
                "target_path": target_dir + "/" + target_name,
                "action": "move",
            }
        )
        if kind == "tv":
            row["season"], row["episode"] = data["season"], data["episode"]
        return row

    # ---------------------------------------------------------------- 执行
    def _execute(self, cfg: Dict[str, Any], plans: List[Dict[str, Any]]) -> None:
        org = cfg["organize"]
        overwrite = bool(org.get("overwrite"))
        delay = float(org.get("delay") or 0.8)
        existing: Dict[str, set] = {}
        scanned: set[str] = set()
        for row in plans:
            if row["action"] != "move":
                continue
            target_dir = row["target_dir"]
            target_name = row["target_name"]
            try:
                if target_dir not in existing:
                    existing[target_dir] = set(self._names(cfg, target_dir))
                if not overwrite and target_name in existing[target_dir]:
                    row["action"] = "skipped_target"
                    row["reason"] = "目标已存在，跳过（可在设置里开启覆盖）"
                    continue
                u115ops.place(cfg, row["file_id"], target_dir, target_name)
                existing[target_dir].add(target_name)
                row["action"] = "done"
                if org.get("scan_after", True) and target_dir not in scanned:
                    scanned.add(target_dir)
                    self._scan(cfg, target_dir)
            except u115web.Web115Error as err:
                row["action"] = "failed"
                row["reason"] = str(err)
                logger.warn(f"[整理] 失败 {row['source_path']}：{err}")
            time.sleep(delay)

    def _names(self, cfg: Dict[str, Any], folder: str) -> List[str]:
        cid = u115ops.find_dir(cfg, folder)
        if not cid:
            return []
        return [
            str(u115ops._pick(item, "n", "file_name", "name", default="")).strip()  # noqa: SLF001
            for item in u115web.list_dir(cfg, cid, force=True)
        ]

    def _scan(self, cfg: Dict[str, Any], cloud_dir: str) -> None:
        try:
            plex_path = resolver.resolver.map_plex_path(cfg, cloud_dir)
        except Exception:  # noqa: BLE001
            plex_path = ""
        if not plex_path:
            return
        try:
            section = plex.section_for_path(plex_path)
        except plex.PlexError:
            section = None
        try:
            scanner.scanner.submit(plex_path, (section or {}).get("key"), source="organize", force=True)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"[整理] 触发扫描失败 {plex_path}：{err}")

    # ---------------------------------------------------------------- 汇总
    def _summary(self, plans: List[Dict[str, Any]], dry: bool, started: float) -> Dict[str, Any]:
        done = sum(1 for x in plans if x["action"] == "done")
        failed = sum(1 for x in plans if x["action"] == "failed")
        skipped = sum(1 for x in plans if x["action"] in ("skip", "skipped_target"))
        planned = sum(1 for x in plans if x["action"] in ("move", "done"))
        unmatched = sum(1 for x in plans if x["action"] == "skip" and "没认出来" in x.get("reason", ""))
        return {
            "ok": True,
            "dry_run": dry,
            "at": time.time(),
            "planned": planned,
            "done": done,
            "failed": failed,
            "skipped": skipped,
            "unmatched": unmatched,
            "elapsed": round(time.time() - started, 1),
            "items": plans[:200],
        }

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "last_run": self.last_run, "summary": self.last_summary}


organizer = Organizer()
