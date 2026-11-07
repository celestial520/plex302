"""二级分类策略（对齐 CMS / MoviePilot 的 YAML 配置）。

不引入 PyYAML：用户给的策略格式固定（两级缩进 + key: value），
用轻量解析即可，少一个多架构依赖。

规则：
  - movie / tv 两个大类，下面按**书写顺序**排列分类
  - 一个分类里可写多个条件，需要**同时满足**才算命中
  - 条件支持 genre_ids / original_language / origin_country / production_countries / ext
  - 都不命中时返回该大类的**最后一个**分类（CMS 的「未匹配返回最后一个」）
  - 分类名同时也是目标目录名；cid 是 CMS 的目录 ID，本项目按路径走，可留空
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DEFAULT_POLICY = """# 电影分类策略（按顺序匹配，都不中就用最后一个）
movie:
  动画电影:
    genre_ids: '16'
  华语电影:
    original_language: 'zh,cn,bo,za'
  外语电影:

# 电视剧分类策略
tv:
  儿童:
    genre_ids: '10762'
  国漫:
    genre_ids: '16'
    origin_country: 'CN,TW,HK'
  日番:
    genre_ids: '16'
    origin_country: 'JP'
  纪录片:
    genre_ids: '99'
  综艺:
    genre_ids: '10764,10767'
  国产剧:
    origin_country: 'CN,TW,HK'
  欧美剧:
    origin_country: 'US,FR,GB,DE,ES,IT,NL,PT,RU,UK'
  日韩剧:
    origin_country: 'JP,KP,KR,TH,IN,SG'
  其它:
"""

_COND_KEYS = {"genre_ids", "original_language", "origin_country", "production_countries", "ext"}


def parse_policy(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """把策略文本解析成 {movie/tv: [{name, cond{key:set(值)}}]}。"""
    out: Dict[str, List[Dict[str, Any]]] = {"movie": [], "tv": []}
    group = ""
    current: Optional[Dict[str, Any]] = None
    # 按缩进层级判断，不能靠「有没有冒号」：分类名本身也是 "动画电影:"。
    group_indent = -1
    cat_indent = -1
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if group_indent < 0 or indent <= group_indent:
            group_indent = indent
            group = body.rstrip(":").strip().lower()
            cat_indent = -1
            current = None
            continue
        if group not in out:
            continue
        if cat_indent < 0 or indent <= cat_indent:
            cat_indent = indent
            name = body.rstrip(":").strip()
            if name:
                current = {"name": name, "cond": {}}
                out[group].append(current)
            continue
        if current is None or ":" not in body:
            continue
        key, _, value = body.partition(":")
        key = key.strip().lower()
        value = value.strip().strip("'\"").strip()
        if key.startswith("cid"):
            continue  # 本项目按目录名定位，不需要 115 的目录 ID
        if key in _COND_KEYS and value:
            current["cond"][key] = {v.strip().upper() for v in value.split(",") if v.strip()}
    return out


def _values(info: Dict[str, Any], key: str) -> set[str]:
    raw = info.get(key)
    result: set[str] = set()
    if key == "genre_ids":
        for item in (info.get("genres") or raw or []):
            if isinstance(item, dict):
                gid = item.get("id")
                if gid not in (None, ""):
                    result.add(str(gid).upper())
            elif item not in (None, ""):
                result.add(str(item).upper())
    elif key == "production_countries":
        for item in (raw or []):
            if isinstance(item, dict):
                code = item.get("iso_3166_1") or item.get("name")
                if code:
                    result.add(str(code).upper())
            elif item:
                result.add(str(item).upper())
        for code in (info.get("origin_country") or []):
            result.add(str(code).upper())
    elif key == "origin_country":
        for code in (raw or []):
            result.add(str(code).upper())
        for item in (info.get("production_countries") or []):
            if isinstance(item, dict) and item.get("iso_3166_1"):
                result.add(str(item["iso_3166_1"]).upper())
    else:
        val = raw or info.get(key) or ""
        if isinstance(val, (list, tuple, set)):
            result = {str(x).upper() for x in val}
        elif val:
            result = {str(val).upper()}
    return result


def pick(policy: Dict[str, List[Dict[str, Any]]], media_type: str,
         info: Dict[str, Any], ext: str = "") -> str:
    """返回分类名（同时是目标目录名）。"""
    rules = policy.get("movie" if media_type == "movie" else "tv") or []
    if not rules:
        return ""
    for rule in rules:
        cond = rule["cond"]
        if not cond:
            continue  # 无条件的分类只作兜底，不参与优先匹配
        matched = True
        for key, wanted in cond.items():
            have = {str(ext).lstrip(".").upper()} if key == "ext" else _values(info, key)
            if not (have & wanted):
                matched = False
                break
        if matched:
            return rule["name"]
    return rules[-1]["name"]


def categories(policy: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    return {k: [r["name"] for r in v] for k, v in policy.items()}
