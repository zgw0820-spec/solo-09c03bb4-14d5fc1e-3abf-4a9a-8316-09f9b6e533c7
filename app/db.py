"""SQLite 数据访问层。

单连接 + 可重入锁串行化所有访问；写操作走 ``tx()``（BEGIN IMMEDIATE 事务）。
数据量（≤2000 点）极小，简单可靠优先。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .timeutil import utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    rows_accepted INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    row_no        INTEGER NOT NULL,          -- CSV 内的原始行号（数据行从 1 起）
    segment       INTEGER NOT NULL,          -- 开机段号
    local_seconds INTEGER NOT NULL,          -- 记录仪本地秒数
    temperature   REAL NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (segment, local_seconds)          -- 同段秒数不能重复
);

CREATE TABLE IF NOT EXISTS anchors (
    segment    INTEGER PRIMARY KEY,
    local1     INTEGER NOT NULL,
    utc1       TEXT NOT NULL,
    local2     INTEGER NOT NULL,
    utc2       TEXT NOT NULL,
    version    INTEGER NOT NULL,             -- 每次更正 +1，更正须携带所见版本
    updated_at TEXT NOT NULL
);

-- 当前生效的校时结果（整体换版，永远完整）
CREATE TABLE IF NOT EXISTS calibrated (
    reading_id     INTEGER PRIMARY KEY REFERENCES readings(id),
    utc_time       TEXT NOT NULL,
    utc_date       TEXT NOT NULL,
    anchor_version INTEGER NOT NULL,
    calc_version   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calibrated_date ON calibrated(utc_date);

CREATE TABLE IF NOT EXISTS calc_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version    INTEGER NOT NULL,             -- 校时结果版本号，单调递增
    status     TEXT NOT NULL,                -- idle / running / failed
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('state_generation', '0')"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('calibrated_generation', '-1')"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO calc_state(id, version, status, updated_at)"
                " VALUES (1, 0, 'idle', ?)",
                (utcnow_iso(),),
            )

    @contextmanager
    def tx(self):
        """写事务：BEGIN IMMEDIATE，异常自动回滚（不留半版）。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

    def snapshot(self, fn):
        """在锁内执行只读操作，保证读到一致快照。"""
        with self._lock:
            return fn(self._conn)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
