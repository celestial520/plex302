"""Plex302 WebUI + 内部 API。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, cd2, config, logger, plex, recent, resolver, scanner, store, u115, u115web, watcher

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
VERSION = "1.1.0"

PUBLIC_PATHS = {"/login", "/api/login", "/health", "/favicon.ico", "/api/resolve", "/api/hook"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    cfg = config.load(force=True)
    cfg, created = auth.ensure_default_password(cfg)
    config.write_runtime(cfg)
    logger.info(f"Plex302 v{VERSION} 启动，配置目录 {config.CONFIG_DIR}")
    if created:
        logger.warn("WebUI 初始账号 admin/admin，请登录后立即修改密码")
    await scanner.scanner.start()
    await watcher.watcher.start()
    await recent.recent_watcher.start()
    yield
    await recent.recent_watcher.stop()
    await watcher.watcher.stop()
    await scanner.scanner.stop()
    logger.info("Plex302 已停止")


app = FastAPI(title="Plex302", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    cfg = config.load()
    if auth.check_session(cfg, request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "未登录或登录已过期"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


def _ok(data: Any = None, **extra) -> Dict[str, Any]:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


def _err(message: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=code)


# --------------------------------------------------------------------- 页面
@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    cfg = config.load()
    if auth.check_session(cfg, request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "version": VERSION, "time": int(time.time())}


@app.get("/favicon.ico")
async def favicon() -> Any:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return JSONResponse({}, status_code=404)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------- 登录
@app.post("/api/login")
async def api_login(request: Request) -> Any:
    cfg = config.load()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    password = str(body.get("password") or "")
    if not auth.verify_password(password, cfg["security"].get("password_hash", "")):
        logger.warn(f"[WebUI] 登录失败，来源 {request.client.host if request.client else '-'}")
        return _err("密码错误", 401)
    token = auth.make_session(cfg)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    logger.info(f"[WebUI] 登录成功，来源 {request.client.host if request.client else '-'}")
    return resp


@app.post("/api/logout")
async def api_logout() -> Any:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/password")
async def api_password(request: Request) -> Any:
    cfg = config.load()
    body = await request.json()
    old = str(body.get("old") or "")
    new = str(body.get("new") or "")
    if not auth.verify_password(old, cfg["security"].get("password_hash", "")):
        return _err("原密码不正确")
    if len(new) < 6:
        return _err("新密码至少 6 位")
    cfg["security"]["password_hash"] = auth.hash_password(new)
    config.save(cfg)
    logger.info("[WebUI] 密码已修改")
    return _ok()


# --------------------------------------------------------------------- 状态
@app.get("/api/status")
async def api_status(request: Request) -> Any:
    cfg = config.load()
    plex_status: Dict[str, Any]
    try:
        info = await asyncio.wait_for(asyncio.to_thread(plex.identity), timeout=6)
        plex_status = {
            "ok": True,
            "version": info.get("version", ""),
            "name": info.get("friendlyName", ""),
            "machine_identifier": info.get("machineIdentifier", ""),
        }
    except Exception as err:  # noqa: BLE001
        plex_status = {"ok": False, "error": str(err)}
    cd2_status = await asyncio.to_thread(cd2.system_info, 4.0)
    today = time.strftime("%Y-%m-%d")
    days = {row["day"]: row for row in store.stats(30)}
    today_stat = days.get(today, {"redirects": 0, "proxied": 0, "errors": 0})
    totals = store.totals()
    today_ts = time.mktime(time.strptime(today, "%Y-%m-%d"))
    scans_today = len(store.scans_since(today_ts))
    return _ok(
        {
            "version": VERSION,
            "plex": plex_status,
            "cd2": cd2_status,
            "u115": u115.client.token_status(),
            "provider": cfg["provider"]["type"],
            "mounts": len(cfg["mounts"]),
            "watch": watcher.watcher.last_summary,
            "recent": {**recent.recent_watcher.status(), "cookie_set": bool(cfg["u115"].get("cookie"))},
            "scanner_pending": scanner.scanner.pending(),
            "stats": {
                "today": today_stat,
                "totals": totals,
                "scans_today": scans_today,
            },
            "default_password": auth.default_password_active(cfg),
            "webhook_url": _webhook_url(request, cfg),
        }
    )


def _webhook_url(request: Request, cfg: Dict[str, Any]) -> str:
    base = cfg["server"].get("public_url") or str(request.base_url).rstrip("/")
    return f"{base}/api/hook?token={cfg['security']['webhook_token']}"


# --------------------------------------------------------------------- 配置
@app.get("/api/config")
async def api_get_config() -> Any:
    return _ok(config.masked(config.load(force=True)))


@app.post("/api/config")
async def api_set_config(request: Request) -> Any:
    old = config.load()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _err("请求体不是 JSON")
    if not isinstance(body, dict):
        return _err("配置格式错误")
    body.pop("version", None)
    merged = config.merge_secrets(body, old)
    # secret 不允许通过接口覆盖
    merged.setdefault("security", {})["secret"] = old["security"].get("secret", "")
    merged["security"]["webhook_token"] = old["security"].get("webhook_token") or merged["security"].get("webhook_token")
    try:
        cfg = config.save(merged)
    except Exception as err:  # noqa: BLE001
        logger.error(f"[配置] 保存失败：{err}")
        return _err(f"保存失败：{err}")
    resolver.resolver.reload()
    resolver.resolver.part_cache.clear()
    resolver.resolver.url_cache.clear()
    plex.invalidate_sections()
    _reload_nginx()
    logger.info("[配置] 已保存")
    return _ok(config.masked(cfg))


@app.post("/api/plex/test")
async def api_plex_test() -> Any:
    try:
        return _ok(await asyncio.to_thread(plex.test_connection))
    except Exception as err:  # noqa: BLE001
        return _err(str(err))


@app.get("/api/plex/sections")
async def api_sections() -> Any:
    cfg = config.load()
    try:
        secs = await asyncio.to_thread(plex.sections, True)
        counts = await asyncio.to_thread(plex.library_counts)
    except Exception as err:  # noqa: BLE001
        return _err(str(err))
    watch = set(cfg["scan"].get("watch_sections", []))
    for sec in secs:
        sec["count"] = counts.get(sec["key"], 0)
        sec["watching"] = (not watch) or sec["key"] in watch
    return _ok(secs)


@app.post("/api/watch/sections")
async def api_watch_sections(request: Request) -> Any:
    body = await request.json()
    sections = body.get("sections")
    if not isinstance(sections, list):
        return _err("sections 必须是数组")
    cfg = config.load()
    cfg["scan"]["watch_sections"] = [str(x) for x in sections]
    config.save(cfg)
    logger.info(f"[监听] 监控媒体库更新为：{cfg['scan']['watch_sections'] or '全部'}")
    return _ok({"sections": cfg["scan"]["watch_sections"]})


# --------------------------------------------------------------------- 扫描
@app.post("/api/scan")
async def api_scan(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    raw_path = str(body.get("path") or "").strip()
    if not raw_path:
        return _err("请填写要扫描的路径")
    path = _cloud_to_plex(config.load(), raw_path)
    section_id = body.get("section_id")
    result = await scanner.scanner.submit(
        path, str(section_id) if section_id else None, source=str(body.get("source") or "manual"),
        force=bool(body.get("force")),
    )
    if not result.get("ok"):
        return _err(str(result.get("error") or "提交失败"))
    return _ok(result)


@app.get("/api/scan/history")
async def api_scan_history(limit: int = 50) -> Any:
    return _ok(store.recent_scans(max(1, min(int(limit), 200))))


@app.post("/api/watch/run")
async def api_watch_run() -> Any:
    summary = await watcher.watcher.run_once()
    return _ok(summary)


@app.get("/api/stats")
async def api_stats(days: int = 7) -> Any:
    return _ok({"days": store.stats(max(1, min(days, 90))), "totals": store.totals()})


# --------------------------------------------------------------------- CMS / 外部通知
PATH_KEYS = {
    "path", "paths", "file", "files", "dir", "directory", "folder", "save_path",
    "savepath", "file_path", "filepath", "save_dir", "savedir", "target",
    "target_path", "targetpath", "output", "output_path", "media_path",
    "mediapath", "dest", "destination", "src", "source_path",
}


def _collect_paths(obj: Any, depth: int = 0, out: List[str] | None = None) -> List[str]:
    out = out if out is not None else []
    if depth > 6 or len(out) > 50:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in PATH_KEYS:
                if isinstance(value, str):
                    out.append(value)
                elif isinstance(value, list):
                    out.extend(str(x) for x in value if isinstance(x, str))
            _collect_paths(value, depth + 1, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_paths(item, depth + 1, out)
    return out


def _cloud_to_plex(cfg: Dict[str, Any], path: str) -> str:
    path = scanner.scanner.normalize(path)
    for mount in cfg["mounts"]:
        prefix = mount["plex_prefix"]
        if path == prefix or path.startswith(prefix + "/"):
            return path
    for mount in cfg["mounts"]:
        cloud = mount.get("cloud_prefix") or "/"
        if path == cloud or path.startswith(cloud.rstrip("/") + "/"):
            rel = path[len(cloud.rstrip("/")):].lstrip("/")
            return scanner.scanner.normalize(mount["plex_prefix"] + ("/" + rel if rel else ""))
    return path


@app.get("/api/hook")
@app.post("/api/hook")
async def api_hook(request: Request) -> Any:
    cfg = config.load()
    token = request.query_params.get("token") or request.headers.get("X-Webhook-Token") or ""
    if token != cfg["security"]["webhook_token"]:
        return _err("token 不正确", 403)

    payload: Any = {}
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        raw = await request.body()
        text = raw.decode("utf-8", "ignore").strip()
        if "json" in content_type:
            try:
                import json

                payload = json.loads(text or "{}")
            except ValueError:
                payload = {"path": text}
        elif "form" in content_type:
            form = await request.form()
            payload = dict(form)
        else:
            payload = {"path": text} if text.startswith("/") else {}
    else:
        payload = dict(request.query_params)

    paths = _collect_paths(payload)
    # 支持 ?path=/电影/xxx 这种最简调用
    for key in ("path", "file", "dir"):
        value = request.query_params.get(key)
        if value:
            paths.append(value)
    paths = [p for p in dict.fromkeys(paths) if p and p.startswith("/")]
    if not paths:
        return _err("没有从请求里找到绝对路径（可用 ?path=/电影/xxx 或 JSON {\"path\":\"...\"}）")

    results = []
    for raw_path in paths[:20]:
        path = _cloud_to_plex(cfg, raw_path)
        # 文件 -> 目录（电影取父目录，剧集取剧/季目录）
        ext = os.path.splitext(path)[1].lower()
        if ext in tuple(cfg["scan"]["video_exts"]):
            try:
                section = await asyncio.to_thread(plex.section_for_path, path)
            except plex.PlexError:
                section = None
            path = scanner.scanner.derive_scan_path(path, section)
        result = await scanner.scanner.submit(path, source="cms")
        results.append({"input": raw_path, "path": path, **result})
    accepted = sum(1 for x in results if x.get("ok"))
    logger.info(f"[CMS] 收到通知，{accepted}/{len(results)} 条进入扫描队列")
    return _ok({"accepted": accepted, "results": results})


# --------------------------------------------------------------------- 302 决策（njs 调用）
@app.post("/api/resolve")
async def api_resolve(request: Request) -> Any:
    cfg = config.load()
    internal = request.headers.get("X-Internal-Token", "")
    if internal != cfg["security"]["secret"][:48]:
        return _err("internal token 不正确", 403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err("请求体不是 JSON")
    result = await resolver.resolver.resolve(payload)
    return JSONResponse(result)


# --------------------------------------------------------------------- 测试
@app.post("/api/test/link")
async def api_test_link(request: Request) -> Any:
    body = await request.json()
    path = str(body.get("path") or "").strip()
    if not path:
        return _err("请填写 Plex 内的文件路径")
    result = await asyncio.to_thread(resolver.resolver.explain, path, str(body.get("ip") or "8.8.8.8"), str(body.get("ua") or "Plex302-test"))
    return _ok(result)


@app.post("/api/cd2/test")
async def api_cd2_test() -> Any:
    return _ok(await asyncio.to_thread(cd2.system_info, 5.0))


# --------------------------------------------------------------------- 115
@app.post("/api/115/qr")
async def api_115_qr() -> Any:
    try:
        data = await asyncio.to_thread(u115.client.qr_create)
        return _ok(data)
    except u115.U115Error as err:
        return _err(str(err))


@app.get("/api/115/qr/status")
async def api_115_qr_status() -> Any:
    try:
        data = await asyncio.to_thread(u115.client.qr_status)
        return _ok(data)
    except u115.U115Error as err:
        return _err(str(err))


@app.get("/api/115/user")
async def api_115_user() -> Any:
    if not u115.client.is_authorized():
        return _ok({"authorized": False})
    try:
        info = await asyncio.to_thread(u115.client.user_info, True)
        return _ok({"authorized": True, **info})
    except u115.U115Error as err:
        return _err(str(err))


@app.post("/api/115/logout")
async def api_115_logout() -> Any:
    u115.client.logout()
    logger.info("[115] 已退出登录")
    return _ok()


# ------------------------------------------------------- 115 网页 Cookie / 最近文件
@app.post("/api/115/cookie/test")
async def api_115_cookie_test(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    cookie = str((body or {}).get("cookie") or "").strip()
    cfg = config.load()
    if cookie:
        cfg["u115"]["cookie"] = u115web.normalize_cookie(cookie)
    try:
        result = await asyncio.to_thread(u115web.test, cfg)
    except u115web.Web115Error as err:
        return _err(str(err))
    return _ok(result)


@app.post("/api/115/recent/run")
async def api_115_recent_run() -> Any:
    summary = await recent.recent_watcher.run_once(force=True)
    if summary.get("ok") is False:
        return _err(summary.get("error") or "拉取 115 最近文件失败")
    return _ok(summary)


@app.get("/api/115/recent/events")
async def api_115_recent_events(limit: int = 20) -> Any:
    return _ok(store.recent_events(max(1, min(int(limit), 200))))


# --------------------------------------------------------------------- 日志
@app.get("/api/logs")
async def api_logs(lines: int = 300, keyword: str = "") -> Any:
    data = logger.tail(lines, keyword)
    if not data:
        data = logger.read_file_tail(lines)
    return _ok(data)


def _reload_nginx() -> None:
    if os.getenv("PLEX302_NO_RELOAD"):
        return
    try:
        subprocess.run(["nginx", "-s", "reload"], timeout=6, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError) as err:
        logger.warn(f"[配置] nginx reload 失败（可手动重启容器）：{err}")
