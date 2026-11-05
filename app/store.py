"""SQLite：扫描记录、监听文件状态、302 计数。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    config.ensure_dirs()
    conn = sqlite3.connect(config.CONFIG_DIR / "state.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            section_id TEXT,
            section_name TEXT,
            source TEXT,
            status TEXT,
            message TEXT,
            created_at REAL,
            finished_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watch_files (
            path TEXT PRIMARY KEY,
            mtime REAL,
            size INTEGER,
            first_seen REAL,
            scanned_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watch_roots (
            root TEXT PRIMARY KEY,
            indexed_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stats (
            day TEXT PRIMARY KEY,
            redirects INTEGER DEFAULT 0,
            proxied INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        )"""
    )
    conn.commit()
    _conn = conn
    return conn


def add_scan(path: str, section_id: str | None, section_name: str | None, source: str) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO scans(path, section_id, section_name, source, status, message, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, section_id, section_name, source, "queued", "", time.time()),
        )
        _db().commit()
        return int(cur.lastrowid)


def update_scan(scan_id: int, status: str, message: str = "") -> None:
    with _lock:
        _db().execute(
            "UPDATE scans SET status=?, message=?, finished_at=? WHERE id=?",
            (status, message[:1000], time.time(), scan_id),
        )
        _db().commit()


def recent_scans(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def scans_since(ts: float) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM scans WHERE created_at>=? ORDER BY id DESC", (ts,)).fetchall()
    return [dict(r) for r in rows]


def get_watch_file(path: str) -> dict[str, Any] | None:
    with _lock:
        row = _db().execute("SELECT * FROM watch_files WHERE path=?", (path,)).fetchone()
    return dict(row) if row else None


def upsert_watch_file(path: str, mtime: float, size: int, scanned_at: float | None = None) -> None:
    now = time.time()
    with _lock:
        _db().execute(
            "INSERT INTO watch_files(path, mtime, size, first_seen, scanned_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, scanned_at=excluded.scanned_at",
            (path, mtime, size, now, scanned_at),
        )
        _db().commit()


def watch_file_count() -> int:
    with _lock:
        row = _db().execute("SELECT COUNT(*) AS c FROM watch_files").fetchone()
    return int(row["c"] if row else 0)


def root_indexed(root: str) -> bool:
    with _lock:
        row = _db().execute("SELECT 1 FROM watch_roots WHERE root=?", (root,)).fetchone()
    return bool(row)


def mark_root(root: str) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO watch_roots(root, indexed_at) VALUES (?,?)", (root, time.time()))
        _db().commit()


def bump_stat(kind: str, amount: int = 1) -> None:
    day = time.strftime("%Y-%m-%d")
    field = {"redirect": "redirects", "proxy": "proxied", "error": "errors"}.get(kind)
    if not field:
        return
    with _lock:
        _db().execute("INSERT OR IGNORE INTO stats(day) VALUES (?)", (day,))
        _db().execute(f"UPDATE stats SET {field}={field}+? WHERE day=?", (amount, day))
        _db().commit()


def stats(days: int = 7) -> list[dict[str, Any]]:
    with _lock:
        rows = _db().execute("SELECT * FROM stats ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    return [dict(r) for r in rows]


def totals() -> dict[str, int]:
    with _lock:
        row = _db().execute(
            "SELECT COALESCE(SUM(redirects),0) AS r, COALESCE(SUM(proxied),0) AS p, "
            "COALESCE(SUM(errors),0) AS e FROM stats"
        ).fetchone()
    return {"redirects": int(row["r"]), "proxied": int(row["p"]), "errors": int(row["e"])} if row else {}


def cleanup_watch_files(days: int = 30) -> None:
    cutoff = time.time() - days * 86400
    with _lock:
        _db().execute("DELETE FROM watch_files WHERE first_seen < ? AND scanned_at IS NOT NULL", (cutoff,))
        _db().commit()


def db_path() -> Path:
    return config.CONFIG_DIR / "state.db"
