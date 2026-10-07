# -*- coding: utf-8 -*-
"""Asia/Seoul timezone helpers. Keep datetimes timezone-aware."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def now_seoul() -> datetime:
    return datetime.now(SEOUL)


def to_seoul(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SEOUL)
    return dt.astimezone(SEOUL)


def _normalize_iso(text: str) -> str:
    value = text.strip().replace("Z", "+00:00")
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    return value


def parse_iso_to_seoul(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(_normalize_iso(str(value)))
    except ValueError:
        return None
    return to_seoul(parsed)


def format_seoul_iso(dt: datetime) -> str:
    local = to_seoul(dt)
    return local.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def format_tmap_time(dt: datetime) -> str:
    local = to_seoul(dt)
    return local.strftime("%Y-%m-%dT%H:%M:%S+0900")


def arrive_budget_minutes(depart: datetime, arrive: datetime) -> float:
    dep = to_seoul(depart)
    arr = to_seoul(arrive)
    if arr < dep:
        arr += timedelta(days=1)
    return (arr - dep).total_seconds() / 60.0
