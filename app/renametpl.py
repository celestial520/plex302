"""CMS 风格重命名模板。

语法（对齐 wiki「变量说明 / 语法说明」）：
  {变量}              直接取值，支持格式规范 {season_num:02d}
  {变量.lower()}      支持常用字符串方法与简单表达式
  <...>               块：块内所有变量都非空时输出块内容，否则整块省略
                      例 {title}.{year}<.{resource_pix}><-{resource_team}>
  [tmdb={tmdb_id}]   方括号是普通字面量，里面的变量照常替换

表达式用受限 eval：变量来自识别结果，禁止访问内建/模块，避免配置里
手滑写入危险代码。配置由用户自己填写，这里仍然做一层防护。
"""
from __future__ import annotations

import re
from typing import Any, Dict

_TOKEN = re.compile(r"\{([^{}]+)\}")
_BLOCK = re.compile(r"<([^<>]*)>")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FORBIDDEN = ("__", "import", "open(", "exec(", "eval(", "os.", "sys.", "subprocess", "globals", "locals")

# 文件名里不能出现的字符（< > 已作为块语法被消费，不在这里）
_ILLEGAL = re.compile(r'[\\/:*?"|]')


def _eval(expr: str, ctx: Dict[str, Any]) -> Any:
    expr = (expr or "").strip()
    if not expr:
        return ""
    if any(bad in expr for bad in _FORBIDDEN):
        return ""
    # {season_num:02d} 这类格式规范
    if ":" in expr and " if " not in expr and "(" not in expr:
        name, _, fmt = expr.partition(":")
        value = _eval(name.strip(), ctx)
        if value in (None, ""):
            return ""
        try:
            return format(value, fmt)
        except (ValueError, TypeError):
            return str(value)
    try:
        return eval(expr, {"__builtins__": {}}, dict(ctx))  # noqa: S307
    except Exception:  # noqa: BLE001
        return ctx.get(expr, "")


def _expand(text: str, ctx: Dict[str, Any]) -> str:
    def sub(match: "re.Match[str]") -> str:
        value = _eval(match.group(1), ctx)
        return "" if value is None else str(value)

    return _TOKEN.sub(sub, text)


def _sanitize(text: str) -> str:
    text = _ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\.+\s*", ".", text)          # 空块留下的 ".." / " ."
    text = re.sub(r"\s*-\s*-\s*", "-", text)        # 空块留下的 "- -"
    return text.strip(" .-")


def render(template: str, ctx: Dict[str, Any]) -> str:
    """渲染模板；块内变量缺失时整块省略，最后清理空块残留。"""
    text = str(template or "")
    for _ in range(32):
        match = _BLOCK.search(text)
        if not match:
            break
        inner = match.group(1)
        names: set[str] = set()
        for expr in _TOKEN.findall(inner):
            names.update(_IDENT.findall(expr))
        # 块里只要有一个变量是空的，整块就不输出（CMS 语义）
        ok = all(str(ctx.get(name, "") or "") != "" for name in names)
        text = text[: match.start()] + (_expand(inner, ctx) if ok else "") + text[match.end():]
    return _sanitize(_expand(text, ctx))


def with_ext(name: str, ext: str) -> str:
    """模板不含扩展名时补上；ext 可带点也可不带。"""
    ext = (ext or "").lstrip(".")
    return f"{name}.{ext}" if ext else name
