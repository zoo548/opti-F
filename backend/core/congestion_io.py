# -*- coding: utf-8 -*-
"""혼잡도 원본(csv/xlsx)을 엔진이 기대하는 시트·컬럼 형식으로 맞춘다."""

from __future__ import annotations

import os
import re

import pandas as pd

SHEETS = ("평일", "토요일", "일요일")
CANDIDATE_NAMES = ("subway_congestion.xlsx", "subway_congestion.csv")
_TIME_LABEL = re.compile(r"^\s*(\d{1,2})\s*시\s*(\d{2})\s*분\s*$")
_TIME_RANGE = re.compile(r"\s*\d{1,2}:\d{2}\s*~\s*\d{1,2}:\d{2}\s*")


def resolve_congestion_path(data_dir: str, explicit: str | None = None) -> str | None:
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(data_dir, explicit)
        return path if os.path.exists(path) else None
    for name in CANDIDATE_NAMES:
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            return path
    return None


def _read_csv(path: str) -> pd.DataFrame:
    last_error = None
    for encoding in ("cp949", "utf-8-sig", "utf-8", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except Exception as exc:
            last_error = exc
    raise last_error


def _day_column(df: pd.DataFrame) -> str | None:
    for name in ("구분", "요일구분", "요일"):
        if name in df.columns:
            return name
    return None


def _time_column_map(columns) -> dict[str, str]:
    already = [c for c in columns if _TIME_RANGE.fullmatch(str(c))]
    if already:
        return {}
    parsed = []
    for col in columns:
        match = _TIME_LABEL.match(str(col))
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        start = hour * 60 + minute
        if hour == 0:
            start += 24 * 60
        parsed.append((start, col))
    parsed.sort()
    mapping = {}
    for index, (start, col) in enumerate(parsed):
        if index + 1 < len(parsed):
            end = parsed[index + 1][0]
        else:
            end = max(start + 30, 24 * 60 + 5 * 60 + 30)
        start_h, start_m = divmod(start, 60)
        end_h, end_m = divmod(end, 60)
        mapping[col] = f"{start_h:02d}:{start_m:02d}~{end_h:02d}:{end_m:02d}"
    return mapping


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    mapping = _time_column_map(out.columns)
    if mapping:
        out = out.rename(columns=mapping)
    return out


def load_congestion_sheets(path: str) -> dict[str, pd.DataFrame]:
    """평일/토요일/일요일 DataFrame. 컬럼은 호선·역명·상하구분·역번호·05:30~06:00..."""
    ext = os.path.splitext(path)[1].lower()
    sheets: dict[str, pd.DataFrame] = {}
    if ext in {".xlsx", ".xls"}:
        xl = pd.ExcelFile(path)
        for name in SHEETS:
            if name in xl.sheet_names:
                sheets[name] = _normalize_frame(xl.parse(name))
        if sheets:
            return sheets
        first = xl.parse(xl.sheet_names[0])
        return _split_by_day(first)
    df = _read_csv(path)
    return _split_by_day(df)


def _split_by_day(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    day_col = _day_column(df)
    out: dict[str, pd.DataFrame] = {}
    if day_col is None:
        out["평일"] = _normalize_frame(df)
        return out
    for name in SHEETS:
        part = df[df[day_col].astype(str).str.strip() == name]
        if len(part):
            out[name] = _normalize_frame(part.drop(columns=[day_col]))
    return out
