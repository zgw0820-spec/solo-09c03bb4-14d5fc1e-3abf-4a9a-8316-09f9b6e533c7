"""线性校时计算与后台重算线程。

一致性设计：
- 任何上传/锚点变更都会递增 state_generation。
- 重算线程按一致快照计算，提交前复核 state_generation：
  若已变化（期间有新上传/更正），本次计算作废重算 —— 迟到的计算绝不提交，
  因此结果不会回退；校时版本号 calc_version 单调递增。
- 每次提交在单个事务内整体替换 calibrated 表：读数跨日时的“旧日移除 +
  新日加入”同时生效；计算或提交失败时旧版本原样保留，绝不提供半版。
- state_generation 持久化，服务重启后启动线程会比对代次并补算，
  所有已接纳且处于锚点范围的点最终都会进入当前版。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .db import Database
from .timeutil import fmt_utc, parse_utc, utcnow_iso

log = logging.getLogger("recalc")


class StaleState(Exception):
    """提交时状态代次已变化：本次计算过期，需丢弃重算。"""


def compute_calibration(readings, anchors: dict) -> list[dict]:
    """纯函数：对每枚落在锚点区间内的读数做线性校时。

    readings: 含 id/segment/local_seconds 的行
    anchors:  segment -> 含 local1/_ts1/local2/_ts2/version 的 dict
    """
    out = []
    for r in readings:
        a = anchors.get(r["segment"])
        if a is None:
            continue
        if a["local1"] <= r["local_seconds"] <= a["local2"]:
            span = a["local2"] - a["local1"]
            frac = (r["local_seconds"] - a["local1"]) / span
            ts = a["_ts1"] + frac * (a["_ts2"] - a["_ts1"])
            ts = round(ts * 1000) / 1000  # 毫秒精度
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            out.append({
                "reading_id": r["id"],
                "utc_time": fmt_utc(dt),
                "utc_date": dt.date().isoformat(),
                "anchor_version": a["version"],
            })
    return out


def load_snapshot(db: Database):
    """在锁内一次读出 代次 + 全部读数 + 全部锚点，保证快照一致。"""
    def fn(conn):
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        state_gen = int(meta.get("state_generation", "0"))
        calibrated_gen = int(meta.get("calibrated_generation", "-1"))
        readings = conn.execute(
            "SELECT id, segment, local_seconds FROM readings ORDER BY id"
        ).fetchall()
        anchors = {}
        for a in conn.execute("SELECT * FROM anchors").fetchall():
            d = dict(a)
            d["_ts1"] = parse_utc(d["utc1"]).timestamp()
            d["_ts2"] = parse_utc(d["utc2"]).timestamp()
            anchors[d["segment"]] = d
        return state_gen, calibrated_gen, readings, anchors

    return db.snapshot(fn)


def commit_calc(db: Database, generation: int, rows: list[dict]) -> int:
    """单事务整体换版。代次不符则抛 StaleState（不提交）。"""
    with db.tx() as cur:
        cur_gen = int(
            cur.execute("SELECT value FROM meta WHERE key = 'state_generation'")
            .fetchone()["value"]
        )
        if cur_gen != generation:
            raise StaleState(f"代次已变化：{generation} -> {cur_gen}")
        new_version = cur.execute(
            "SELECT version FROM calc_state WHERE id = 1"
        ).fetchone()["version"] + 1
        cur.execute("DELETE FROM calibrated")
        cur.executemany(
            "INSERT INTO calibrated(reading_id, utc_time, utc_date, anchor_version, calc_version)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (r["reading_id"], r["utc_time"], r["utc_date"], r["anchor_version"], new_version)
                for r in rows
            ],
        )
        cur.execute(
            "UPDATE calc_state SET version = ?, status = 'idle', last_error = '', updated_at = ?"
            " WHERE id = 1",
            (new_version, utcnow_iso()),
        )
        cur.execute(
            "UPDATE meta SET value = ? WHERE key = 'calibrated_generation'", (str(generation),)
        )
    return new_version


def set_status(db: Database, status: str, last_error: str = "") -> None:
    db.execute(
        "UPDATE calc_state SET status = ?, last_error = ?, updated_at = ? WHERE id = 1",
        (status, last_error, utcnow_iso()),
    )


class RecalcWorker:
    """后台重算线程：事件触发，循环重算直至 已校代次 == 状态代次。"""

    def __init__(self, db: Database, delay: float = 0.0, max_failures: int = 3):
        self.db = db
        self.delay = delay            # 提交前人为延迟，仅用于演示/测试并发窗口
        self.max_failures = max_failures
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="recalc-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def trigger(self) -> None:
        self._event.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._event.wait(timeout=10)  # 周期性兜底唤醒
            self._event.clear()
            if self._stop.is_set():
                break
            try:
                self.run_until_quiet()
            except Exception:  # pragma: no cover - 防御
                log.exception("重算循环出现异常")

    def run_until_quiet(self) -> None:
        """重算直至无待处理变更；失败重试 max_failures 次后保持失败状态等待下次触发。"""
        while not self._stop.is_set():
            state_gen, calibrated_gen, readings, anchors = load_snapshot(self.db)
            if calibrated_gen == state_gen:
                self._failures = 0
                return
            set_status(self.db, "running")
            try:
                rows = compute_calibration(readings, anchors)
                if self.delay > 0:
                    time.sleep(self.delay)  # 放大并发窗口（演示/测试）
                commit_calc(self.db, state_gen, rows)
                self._failures = 0
            except StaleState:
                log.info("状态已变化，丢弃过期计算并重算")
                continue
            except Exception as exc:  # 计算/提交失败：旧版原样保留
                self._failures += 1
                log.warning("重算失败（第 %s 次）：%s", self._failures, exc)
                set_status(self.db, "failed", str(exc)[:500])
                if self._failures >= self.max_failures:
                    return
                time.sleep(0.2)
