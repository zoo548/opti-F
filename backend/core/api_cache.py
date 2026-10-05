# -*- coding: utf-8 -*-
"""Process LRU + optional Supabase API response cache. Failures never abort routing."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

LRU_SIZE = 2000
TTL_ODSAY_SEC = 7 * 24 * 3600
TTL_TMAP_SEC = 24 * 3600
TTL_LANE_SEC = 30 * 24 * 3600

_lru_lock = threading.Lock()
_lru: OrderedDict[str, tuple[object, float]] = OrderedDict()
_sb_lock = threading.Lock()
_sb_client = None
_sb_tried = False

_stats_lock = threading.Lock()
_stats = {
    "odsay_api": 0,
    "tmap_api": 0,
    "lane_api": 0,
    "memory_hits": 0,
    "db_hits": 0,
}


def reset_stats() -> None:
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0


def bump(field: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[field] = _stats.get(field, 0) + n


def snapshot_stats() -> dict[str, int]:
    with _stats_lock:
        return dict(_stats)


def _project_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest/v1/"):
        if url.endswith(suffix.rstrip("/")):
            url = url[: -len(suffix.rstrip("/"))]
            break
    return url.rstrip("/")


def _supabase():
    global _sb_client, _sb_tried
    with _sb_lock:
        if _sb_tried:
            return _sb_client
        _sb_tried = True
        url = _project_url(os.environ.get("SUPABASE_URL", ""))
        key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
        if not url or not key:
            log.info("Supabase cache disabled: missing SUPABASE_URL or SUPABASE_SERVICE_KEY")
            return None
        try:
            from supabase import create_client
            _sb_client = create_client(url, key)
        except Exception as exc:
            log.warning("Supabase client init failed: %s", type(exc).__name__)
            _sb_client = None
        return _sb_client


def _lru_get(key: str):
    now = time.time()
    with _lru_lock:
        item = _lru.get(key)
        if item is None:
            return None
        value, exp = item
        if exp <= now:
            _lru.pop(key, None)
            return None
        _lru.move_to_end(key)
        return value


def _lru_set(key: str, value, ttl_sec: int) -> None:
    exp = time.time() + max(1, int(ttl_sec))
    with _lru_lock:
        _lru[key] = (value, exp)
        _lru.move_to_end(key)
        while len(_lru) > LRU_SIZE:
            _lru.popitem(last=False)


def _parse_exp(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _db_get(key: str):
    client = _supabase()
    if client is None:
        return None
    try:
        res = (
            client.table("api_cache")
            .select("response,expires_at")
            .eq("cache_key", key)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        row = rows[0]
        exp = _parse_exp(row.get("expires_at"))
        if exp is not None and exp <= time.time():
            return None
        return row.get("response")
    except Exception as exc:
        log.warning("Supabase cache get failed: %s", type(exc).__name__)
        return None


def _db_set(key: str, provider: str, value, ttl_sec: int) -> None:
    client = _supabase()
    if client is None:
        return
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "cache_key": key,
            "provider": provider,
            "response": value,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=max(1, int(ttl_sec)))).isoformat(),
        }
        client.table("api_cache").upsert(payload).execute()
    except Exception as exc:
        log.warning("Supabase cache set failed: %s", type(exc).__name__)


def cache_get(key: str):
    hit = _lru_get(key)
    if hit is not None:
        bump("memory_hits")
        return hit
    hit = _db_get(key)
    if hit is not None:
        bump("db_hits")
        ttl_left = TTL_ODSAY_SEC
        if key.startswith("tmap:"):
            ttl_left = TTL_TMAP_SEC
        elif key.startswith("lane:"):
            ttl_left = TTL_LANE_SEC
        _lru_set(key, hit, ttl_left)
        return hit
    return None


def cache_set(key: str, provider: str, value, ttl_sec: int) -> None:
    _lru_set(key, value, ttl_sec)
    _db_set(key, provider, value, ttl_sec)
