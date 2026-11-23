"""CSV parsing for pump ledger uploads.

Accepted header names (case-insensitive, whitespace-tolerant), one pump per
row after the header:

    pump_id      泵号 / 泵编号 / pump_id / id
    entry_hours  入册累计运行小时 / 入册小时 / entry_hours / entry
    exit_hours   出册累计运行小时 / 出册小时 / exit_hours / exit
    event_type   结束方式 / 事件类型 / event_type / event
                 故障=failure, 预防更换=preventive, 观察结束=censored
"""
from __future__ import annotations

import csv
import io

from .lifetable import EVENT_TYPES, PumpRecord

HEADER_ALIASES = {
    "pump_id": {"泵号", "泵编号", "泵id", "pump_id", "pumpid", "id"},
    "entry_hours": {
        "入册累计运行小时", "入册运行小时", "入册小时", "入册时间",
        "entry_hours", "entryhours", "entry",
    },
    "exit_hours": {
        "出册累计运行小时", "出册运行小时", "出册小时", "出册时间",
        "exit_hours", "exithours", "exit",
    },
    "event_type": {"结束方式", "结束类型", "事件类型", "event_type", "eventtype", "event"},
}

EVENT_ALIASES = {
    "故障": "failure",
    "故障结束": "failure",
    "failure": "failure",
    "fail": "failure",
    "f": "failure",
    "预防更换": "preventive",
    "预防性更换": "preventive",
    "预防": "preventive",
    "preventive": "preventive",
    "prevent": "preventive",
    "p": "preventive",
    "观察结束": "censored",
    "观察": "censored",
    "删失": "censored",
    "截尾": "censored",
    "censored": "censored",
    "censor": "censored",
    "c": "censored",
}

REQUIRED_COLUMNS = ("pump_id", "entry_hours", "exit_hours", "event_type")


def _normalize(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "").replace("_", "")


def _map_headers(fieldnames: list[str]) -> dict[str, int]:
    normalized = {_normalize(name): idx for idx, name in enumerate(fieldnames)}
    aliases_norm = {
        col: {_normalize(a) for a in aliases} for col, aliases in HEADER_ALIASES.items()
    }
    mapping: dict[str, int] = {}
    for col in REQUIRED_COLUMNS:
        for alias in aliases_norm[col]:
            if alias in normalized:
                mapping[col] = normalized[alias]
                break
        if col not in mapping:
            raise ValueError(f"CSV 缺少必需列: {col}（可识别的表头见使用说明）")
    return mapping


def parse_csv(text: str) -> list[PumpRecord]:
    """Parse CSV text into PumpRecord objects.

    All rows are validated; a ValueError listing every problem is raised so
    the user gets the full error list in one round trip.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("CSV 文件为空")

    header, *data_rows = rows
    mapping = _map_headers(header)
    records: list[PumpRecord] = []
    errors: list[str] = []

    for line_no, row in enumerate(data_rows, start=2):
        try:
            pump_id = row[mapping["pump_id"]].strip()
            entry_raw = row[mapping["entry_hours"]].strip()
            exit_raw = row[mapping["exit_hours"]].strip()
            event_raw = row[mapping["event_type"]].strip()

            if not pump_id:
                raise ValueError("泵号为空")
            try:
                entry = float(entry_raw)
            except ValueError:
                raise ValueError(f"入册小时不是数字: {entry_raw!r}")
            try:
                exit_hours = float(exit_raw)
            except ValueError:
                raise ValueError(f"出册小时不是数字: {exit_raw!r}")

            event = EVENT_ALIASES.get(_normalize(event_raw))
            if event is None:
                raise ValueError(
                    f"结束方式无法识别: {event_raw!r}（允许: 故障/预防更换/观察结束）"
                )
            if entry < 0:
                raise ValueError(f"入册小时为负: {entry}")
            if exit_hours < entry:
                raise ValueError(f"出册小时 ({exit_hours}) 早于入册小时 ({entry})")

            records.append(PumpRecord(pump_id, entry, exit_hours, event))
        except IndexError:
            errors.append(f"第 {line_no} 行: 列数不足，缺少字段")
        except ValueError as exc:
            errors.append(f"第 {line_no} 行（泵号 {row[mapping['pump_id']].strip() or '?'}）: {exc}")

    if errors:
        raise ValueError("CSV 解析失败：\n" + "\n".join(errors))
    if not records:
        raise ValueError("CSV 没有有效数据行")
    return records
