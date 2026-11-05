"""CloudDrive2 gRPC 客户端（只用到三个方法）。"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

import grpc

from . import config, logger
from .proto import clouddrive_pb2 as pb
from .proto import clouddrive_pb2_grpc as pbg

_lock = threading.RLock()
_channel: grpc.Channel | None = None
_channel_target: str = ""
_stub: pbg.CloudDriveFileSrvStub | None = None


class CD2Error(Exception):
    pass


def _get_stub() -> pbg.CloudDriveFileSrvStub:
    global _channel, _channel_target, _stub
    cfg = config.load()["cd2"]
    target = cfg.get("address") or "127.0.0.1:19798"
    with _lock:
        if _stub is not None and _channel_target == target:
            return _stub
        if _channel is not None:
            try:
                _channel.close()
            except Exception:  # noqa: BLE001
                pass
        _channel = grpc.insecure_channel(
            target,
            options=[
                ("grpc.max_receive_message_length", 32 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 60000),
            ],
        )
        _channel_target = target
        _stub = pbg.CloudDriveFileSrvStub(_channel)
        return _stub


def _meta() -> list[tuple[str, str]]:
    token = config.load()["cd2"].get("token") or ""
    if token:
        return [("authorization", f"Bearer {token}")]
    return []


def system_info(timeout: float = 5.0) -> Dict[str, Any]:
    stub = _get_stub()
    try:
        info = stub.GetSystemInfo(pb.google_dot_protobuf_dot_empty__pb2.Empty(), metadata=_meta(), timeout=timeout)
        return {
            "ok": True,
            "logged_in": bool(info.IsLogin),
            "user_name": info.UserName,
            "ready": bool(info.SystemReady),
            "message": getattr(info, "SystemMessage", "") or "",
        }
    except Exception as err:  # noqa: BLE001
        detail = str(err.details() if hasattr(err, "details") else err)
        low = detail.lower()
        if "unavailable" in low or "connection refused" in low:
            message = "连接失败：CD2 未启动，或 gRPC 地址/端口不对"
        elif "unauthenticated" in low or "permission" in low:
            message = "连接失败：CD2 API Token 无效"
        else:
            message = f"连接失败：{detail[:100]}"
        return {"ok": False, "message": message}


def refresh_path(path: str, timeout: float = 12.0) -> bool:
    """强制 CD2 重新拉取某个目录的列表，让新整理进来的文件立刻可见。"""
    stub = _get_stub()
    req = pb.ListSubFileRequest(path=path, forceRefresh=True)
    try:
        call = stub.GetSubFiles(req, metadata=_meta(), timeout=timeout)
        count = 0
        for _ in call:
            count += 1
            if count > 2000:
                break
        return True
    except Exception as err:  # noqa: BLE001
        logger.warn(f"[CD2] 刷新目录失败 {path}: {err}")
        return False


def refresh_ancestors(path: str, root: str = "", timeout: float = 12.0) -> List[str]:
    """从挂载根到目标目录逐级刷新，最多 8 级。"""
    path = "/" + str(path or "").strip("/")
    root = "/" + str(root or "").strip("/")
    if root == "/":
        root = ""
    if not path.startswith(root):
        return []
    rel = path[len(root):].strip("/")
    parts = [x for x in rel.split("/") if x]
    done: List[str] = []
    current = root
    for part in parts[:8]:
        current = current + "/" + part if current else "/" + part
        if refresh_path(current, timeout=timeout):
            done.append(current)
    return done


def find_file(parent_path: str, name: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    stub = _get_stub()
    try:
        info = stub.FindFileByPath(
            pb.FindFileByPathRequest(parentPath=parent_path, path=name), metadata=_meta(), timeout=timeout
        )
        return {
            "name": info.name,
            "full_path": info.fullPathName,
            "size": int(info.size),
            "is_dir": int(info.fileType) == 0,
        }
    except Exception:  # noqa: BLE001
        return None


def download_url(path: str, timeout: float = 10.0) -> Dict[str, Any]:
    """向 CD2 要云盘直链；只有网盘/账号支持时才有 directUrl。"""
    stub = _get_stub()
    try:
        info = stub.GetDownloadUrlPath(
            pb.GetDownloadUrlPathRequest(path=path, preview=False, lazy_read=True, get_direct_url=True),
            metadata=_meta(),
            timeout=timeout,
        )
        return {
            "download_url_path": info.downloadUrlPath,
            "direct_url": getattr(info, "directUrl", "") or "",
            "user_agent": getattr(info, "userAgent", "") or "",
            "headers": dict(getattr(info, "additionalHeaders", {}) or {}),
            "expires_in": int(getattr(info, "expiresIn", 0) or 0),
        }
    except grpc.RpcError as err:
        detail = err.details() if hasattr(err, "details") else str(err)
        raise CD2Error(f"CD2 获取直链失败：{detail}") from err
    except Exception as err:  # noqa: BLE001
        raise CD2Error(f"CD2 获取直链失败：{err}") from err
