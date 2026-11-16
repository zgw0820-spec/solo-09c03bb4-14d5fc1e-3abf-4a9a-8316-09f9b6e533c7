"""CSV 上传解析与校验。

格式：三列 —— 开机段号、本地秒数、温度。
- 支持表头（中/英文别名，列序任意），也支持无表头（固定顺序）。
- 行号为数据行序号（从 1 起，不含表头）。
"""
from __future__ import annotations

import csv
import io
import math

HEADER_ALIASES = {
    "segment": {"segment", "segment_no", "boot_segment", "seg", "seg_no", "开机段号", "段号"},
    "local_seconds": {"local_seconds", "seconds", "local_sec", "sec", "本地秒数", "秒数"},
    "temperature": {"temperature", "temp", "温度"},
}

MAX_ROWS_PER_FILE = 2000  # 与累计上限一致，超出即拒绝


def _looks_numeric(cell: str) -> bool:
    try:
        float(cell.strip())
        return True
    except ValueError:
        return False


def _match_header(cells: list[str]) -> dict[str, int] | None:
    norm = [c.strip().lower() for c in cells]
    mapping: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for idx, cell in enumerate(norm):
            if cell in aliases:
                mapping[field] = idx
                break
        else:
            return None
    return mapping


def parse_csv_text(text: str, max_rows: int = MAX_ROWS_PER_FILE):
    """返回 (rows, errors)。

    rows: [{"row_no", "segment", "local_seconds", "temperature"}, ...]
    errors: [{"row", "message"}, ...]；有任何错误时 rows 为空（整批拒绝）。
    """
    errors: list[dict] = []
    rows: list[dict] = []

    records = [rec for rec in csv.reader(io.StringIO(text)) if any(c.strip() for c in rec)]
    if not records:
        return [], [{"row": 0, "message": "CSV 内容为空"}]

    header = _match_header(records[0])
    if header is not None:
        data_records = records[1:]
        col = header
    elif any(not _looks_numeric(c) for c in records[0][:3]):
        return [], [{
            "row": 0,
            "message": "无法识别表头：需要列 segment,local_seconds,temperature"
            "（或中文别名 开机段号,本地秒数,温度）；无表头时按此顺序排列",
        }]
    else:
        data_records = records
        col = {"segment": 0, "local_seconds": 1, "temperature": 2}

    if not data_records:
        return [], [{"row": 0, "message": "CSV 中没有数据行"}]

    seen: dict[tuple[int, int], int] = {}
    for row_no, rec in enumerate(data_records, start=1):
        if len(rows) >= max_rows:
            errors.append({"row": row_no, "message": f"超过单文件最大行数 {max_rows}"})
            break
        if len(rec) <= max(col.values()):
            errors.append({"row": row_no, "message": "列数不足，应为 3 列"})
            continue

        seg_raw = rec[col["segment"]].strip()
        sec_raw = rec[col["local_seconds"]].strip()
        tmp_raw = rec[col["temperature"]].strip()

        try:
            segment = int(seg_raw)
            if segment < 0:
                raise ValueError
        except ValueError:
            errors.append({"row": row_no, "message": f"开机段号须为非负整数，实际为 {seg_raw!r}"})
            continue
        try:
            seconds = int(sec_raw)
            if seconds < 0:
                raise ValueError
        except ValueError:
            errors.append({"row": row_no, "message": f"本地秒数须为非负整数，实际为 {sec_raw!r}"})
            continue
        try:
            temperature = float(tmp_raw)
            if not math.isfinite(temperature):
                raise ValueError
        except ValueError:
            errors.append({"row": row_no, "message": f"温度须为有限数值，实际为 {tmp_raw!r}"})
            continue

        key = (segment, seconds)
        if key in seen:
            errors.append({
                "row": row_no,
                "message": f"文件内重复：段 {segment} 秒数 {seconds}（首次出现于第 {seen[key]} 行）",
            })
            continue
        seen[key] = row_no
        rows.append({
            "row_no": row_no,
            "segment": segment,
            "local_seconds": seconds,
            "temperature": temperature,
        })

    if errors:
        return [], errors
    return rows, []
