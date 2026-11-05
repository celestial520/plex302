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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import (
    ai, auth, cd2, config, events, ingest, logger, notify, organize, plex, resolver,
    scanner, scheduler, store, tmdb, u115events, u115ops, u115web, watcher, wecom,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
VERSION = "1.4.0"

PUBLIC_PATHS = {
    "/login", "/api/login", "/health", "/favicon.ico", "/api/resolve", "/api/hook",
    "/api/wecom/callback",
}


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
    await events.event_watcher.start()
    await scheduler.scheduler.start()
    notify_task = asyncio.create_task(notify.flush_loop(), name="notify-flush")
    yield
    notify_task.cancel()
    await scheduler.scheduler.stop()
    await events.event_watcher.stop()
    await watcher.watcher.stop()
    await scanner.scanner.stop()
    logger.info("Plex302 已停止")


app = FastAPI(title="Plex302", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/emby/"):
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
            "u115": {"cookie_set": bool(cfg["u115"].get("cookie")), "cookie_hint": ("…" + cfg["u115"]["cookie"][-6:]) if cfg["u115"].get("cookie") else ""},
            "provider": cfg["provider"]["type"],
            "scan_watch_on": bool(cfg["scan"].get("watch_enabled")),
            "lan_redirect": bool(cfg["server"].get("redirect_lan")),
            "mounts": len(cfg["mounts"]),
            "watch": watcher.watcher.last_summary,
            "events": {
                **events.event_watcher.status(),
                "cookie_set": bool(cfg["u115"].get("cookie")),
                "enabled": cfg["events"]["enabled"],
                "interval": cfg["events"]["interval"],
            },
            "full": scanner.scanner.full_status(),
            "notify": {"wecom": bool(cfg["notify"]["wecom_enabled"])},
            "organize": {
                "enabled": bool(cfg["organize"]["enabled"]),
                "dry_run": bool(cfg["organize"]["dry_run"]),
                "running": organize.organizer.running,
                "summary": organize.organizer.last_summary,
                "history": store.organize_count(),
            },
            "ingest": {
                "enabled": bool(cfg["ingest"]["enabled"]),
                "target": cfg["ingest"]["target_dir"],
            },
            "scanner_pending": scanner.scanner.pending(),
            "stats": {
                "today": today_stat,
                "totals": totals,
                "scans_today": scans_today,
            },
            "default_password": auth.default_password_active(cfg),
            "webhook_url": _webhook_url(request, cfg),
            "emby_url": _emby_url(request, cfg),
            "wecom_callback": _wecom_url(request, cfg),
        }
    )


def _webhook_url(request: Request, cfg: Dict[str, Any]) -> str:
    base = cfg["server"].get("public_url") or str(request.base_url).rstrip("/")
    return f"{base}/api/hook?token={cfg['security']['webhook_token']}"


def _emby_url(request: Request, cfg: Dict[str, Any]) -> str:
    base = cfg["server"].get("public_url") or str(request.base_url).rstrip("/")
    return f"{base}/emby?api_key={cfg['security']['emby_token']}"


def _wecom_url(request: Request, cfg: Dict[str, Any]) -> str:
    base = cfg["server"].get("public_url") or str(request.base_url).rstrip("/")
    return f"{base}/api/wecom/callback"


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
    # 支持 ?full=1 或 {"full":true}：不传路径，直接全量扫一遍
    full_flag = str(request.query_params.get("full") or "").lower() in ("1", "true", "yes", "all")
    if isinstance(payload, dict) and str(payload.get("full") or "").lower() in ("1", "true", "yes", "all"):
        full_flag = True
    if full_flag and not paths:
        result = await scanner.scanner.submit_full(source="cms")
        logger.info(f"[CMS] 收到全量同步通知：{result}")
        return _ok({"full": True, **result})
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


# ------------------------------------------------------- 115 网页 Cookie / 操作事件
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
    # 顺带验证生活事件接口（真正的增量来源）
    try:
        ev = await asyncio.to_thread(u115events.test, cfg, 5)
        result["events"] = ev["count"]
        result["event_sample"] = ev.get("sample")
    except u115web.Web115Error as err:
        result["events_error"] = str(err)
    return _ok(result)


@app.post("/api/115/events/run")
async def api_115_events_run() -> Any:
    summary = await events.event_watcher.run_once(force=True)
    if summary.get("ok") is False:
        return _err(summary.get("error") or "拉取 115 操作记录失败")
    return _ok(summary)


@app.get("/api/115/events")
async def api_115_events(limit: int = 30) -> Any:
    return _ok(store.events(max(1, min(int(limit), 200))))


@app.get("/api/115/events/summary")
async def api_115_events_summary() -> Any:
    return _ok(events.event_watcher.status())


# ------------------------------------------------------- 115 目录浏览（选整理目录用）
@app.get("/api/115/browse")
async def api_115_browse(path: str = "/") -> Any:
    cfg = config.load()
    if not u115web.has_cookie(cfg):
        return _err("未配置 115 Cookie")
    try:
        cid = await asyncio.to_thread(u115ops.find_dir, cfg, path)
        if not cid:
            return _err(f"目录不存在：{path}")
        items = await asyncio.to_thread(u115web.list_dir, cfg, cid, True)
    except u115web.Web115Error as err:
        return _err(str(err))
    dirs = []
    for item in items:
        name = str(u115ops._pick(item, "n", "file_name", "name", default=""))  # noqa: SLF001
        is_dir = bool(u115ops._pick(item, "is_dir", default=False)) or not str(  # noqa: SLF001
            u115ops._pick(item, "pc", "pick_code", default="")  # noqa: SLF001
        )
        if name and is_dir:
            dirs.append({"name": name, "path": path.rstrip("/") + "/" + name})
    return _ok({"path": path, "dirs": sorted(dirs, key=lambda x: x["name"])[:300]})


# ------------------------------------------------------- 整理
@app.post("/api/organize/run")
async def api_organize_run(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    dry = body.get("dry_run")
    limit = int(body.get("limit") or 0)
    try:
        summary = await asyncio.to_thread(organize.organizer.run, None if dry is None else bool(dry), limit)
    except organize.OrganizeError as err:
        return _err(str(err))
    except (u115web.Web115Error, tmdb.TmdbError, ai.AiError) as err:
        return _err(str(err))
    return _ok(summary)


@app.get("/api/organize/status")
async def api_organize_status() -> Any:
    return _ok(organize.organizer.status())


@app.get("/api/organize/history")
async def api_organize_history(limit: int = 50) -> Any:
    return _ok(store.organize_history(max(1, min(int(limit), 300))))


# ------------------------------------------------------- 链接入库
@app.post("/api/ingest")
async def api_ingest(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = str(body.get("text") or "")
    if not text.strip():
        return _err("请粘贴 115 分享链接 / 磁力 / ed2k")
    result = await asyncio.to_thread(ingest.ingest, text, None, bool(body.get("organize", True)))
    if not result.get("ok"):
        return _err(str(result.get("error") or "入库失败"))
    # 转存/离线是异步落盘的，等一会儿再整理；dry_run 由配置决定，不在这里强行真跑
    if body.get("organize", True) and config.load()["organize"].get("enabled"):
        async def _later() -> None:
            await asyncio.sleep(20)
            try:
                await asyncio.to_thread(organize.organizer.run, None, 0)
            except Exception as err:  # noqa: BLE001
                logger.warn(f"[入库] 自动整理跳过：{err}")

        asyncio.create_task(_later())
    return _ok(result)


# ------------------------------------------------------- 企业微信回调（接收消息）
@app.get("/api/wecom/callback")
async def api_wecom_verify(request: Request) -> Any:
    cfg = config.load()
    try:
        plain = wecom.verify_url(cfg, dict(request.query_params))
    except wecom.WecomError as err:
        logger.warn(f"[微信] 回调校验失败：{err}")
        return Response(content=str(err), status_code=403, media_type="text/plain")
    return Response(content=plain, media_type="text/plain")


@app.post("/api/wecom/callback")
async def api_wecom_callback(request: Request) -> Any:
    cfg = config.load()
    body = await request.body()
    try:
        reply = await asyncio.to_thread(wecom.handle_message, cfg, body, dict(request.query_params))
    except wecom.WecomError as err:
        logger.warn(f"[微信] 消息处理失败：{err}")
        return Response(content=str(err), status_code=403, media_type="text/plain")
    return Response(content=reply, media_type="application/xml")


@app.post("/api/tmdb/test")
async def api_tmdb_test() -> Any:
    try:
        return _ok(await asyncio.to_thread(tmdb.test, config.load()))
    except tmdb.TmdbError as err:
        return _err(str(err))


@app.post("/api/ai/test")
async def api_ai_test() -> Any:
    try:
        return _ok(await asyncio.to_thread(ai.test, config.load()))
    except ai.AiError as err:
        return _err(str(err))


# ------------------------------------------------------- 全量同步
@app.post("/api/sync/full")
async def api_sync_full() -> Any:
    result = await scanner.scanner.submit_full(source="manual")
    if not result.get("ok"):
        return _err(result.get("error") or "全量同步启动失败")
    logger.info("[全量] 手动触发全量同步")
    return _ok(result)


@app.get("/api/sync/full")
async def api_sync_full_status() -> Any:
    return _ok(scanner.scanner.full_status())


# ------------------------------------------------------- 通知
@app.post("/api/notify/test")
async def api_notify_test() -> Any:
    result = await asyncio.to_thread(notify.test)
    if not result.get("ok"):
        return _err(result.get("error") or "发送失败")
    return _ok({"sent": True})


# ------------------------------------------------------- CMS 兼容入口（Emby）
def _emby_token(request: Request) -> str:
    return str(
        request.query_params.get("api_key")
        or request.headers.get("X-Emby-Token")
        or request.headers.get("X-MediaBrowser-Token")
        or ""
    )


def _emby_ok(request: Request, cfg: Dict[str, Any]) -> bool:
    token = _emby_token(request)
    return bool(token) and token in (cfg["security"]["emby_token"], cfg["security"]["webhook_token"])


@app.get("/emby/System/Info")
@app.get("/emby/System/Info/Public")
async def emby_system_info(request: Request) -> Any:
    cfg = config.load()
    if not _emby_ok(request, cfg):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {
        "ServerName": cfg["server"].get("name") or "Plex302",
        "Version": "4.8.0.0",
        "ProductName": "Plex302",
        "Id": cfg["security"]["secret"][:32],
        "OperatingSystem": "Linux",
        "StartupWizardCompleted": True,
    }


@app.post("/emby/Library/Refresh")
@app.post("/emby/Library/Sections/{section_id}/Refresh")
async def emby_library_refresh(request: Request, section_id: str = "") -> Any:
    """CMS 的「通知 Emby 扫库」直接打到这里，转成 Plex 扫描。

    CMS 里把 EMBY_HOST_PORT 指向本容器 WebUI 端口、EMBY_API_KEY 填 emby_token，
    整理完就会自动触发；不用再额外装 webhook 插件。
    """
    cfg = config.load()
    if not _emby_ok(request, cfg):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if section_id:
        try:
            sections = await asyncio.to_thread(plex.sections)
        except plex.PlexError as err:
            return JSONResponse({"error": str(err)}, status_code=502)
        target = next((s for s in sections if s["key"] == str(section_id)), None)
        if target:
            logger.info(f"[CMS] Emby 兼容通知：扫描媒体库 {target['title']}")
            await scanner.scanner.submit(
                (target.get("locations") or [target["title"]])[0],
                target["key"],
                source="cms-emby",
                force=True,
            )
        return Response(status_code=204)
    logger.info("[CMS] Emby 兼容通知：触发全量同步")
    await scanner.scanner.submit_full(source="cms-emby")
    return Response(status_code=204)


@app.post("/emby/Items/{item_id}/Refresh")
async def emby_item_refresh(request: Request, item_id: str) -> Any:
    cfg = config.load()
    if not _emby_ok(request, cfg):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 拿不到 Emby 的 item -> 路径映射，退化成「10 分钟内只触发一次全量」
    last = float(store.state_get("emby_item_full_at", "0") or 0)
    if time.time() - last > 600:
        store.state_set("emby_item_full_at", str(time.time()))
        logger.info(f"[CMS] Emby 兼容通知（item {item_id}）：触发全量同步")
        await scanner.scanner.submit_full(source="cms-emby")
    return Response(status_code=204)


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
