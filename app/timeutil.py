"""UTC 时间解析与格式化。系统内所有时间一律按 UTC 处理。"""
from __future__ import annotations

from datetime import datetime, timezone

_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_utc(s: object) -> datetime:
    """解析用户输入的 UTC 时间。

    接受：``2026-01-01 08:00:00``、``2026-01-01T08:00:00Z``、``2026-01-01T08:00:00+00:00``
    以及带毫秒的形式。不带时区的一律视为 UTC。
    """
    raw = str(s or "").strip()
    if not raw:
        raise ValueError("时间不能为空")
    dt: datetime | None = None
    t = raw.replace("T", " ").replace("t", " ")
    if t.endswith(("Z", "z")):
        t = t[:-1].strip()
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            pass
    if dt is None and not raw.endswith(("Z", "z")):
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"无法解析时间 {raw!r}，请使用如 2026-01-01 08:00:00 或 2026-01-01T08:00:00Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    """格式化为 ISO 8601（毫秒精度，Z 结尾）。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utcnow_iso() -> str:
    return fmt_utc(datetime.now(timezone.utc))
