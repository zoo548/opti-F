# -*- coding: utf-8 -*-
"""MaaS 환승점 분석 엔진 (CLI/지도 제거, 웹 서버용)."""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from .api_cache import (
    TTL_LANE_SEC,
    TTL_ODSAY_SEC,
    TTL_TMAP_SEC,
    bump,
    cache_get,
    cache_set,
    reset_stats,
    snapshot_stats,
)
from .congestion_io import load_congestion_sheets, resolve_congestion_path
from .timeutil import format_seoul_iso, format_tmap_time, now_seoul, parse_iso_to_seoul, to_seoul

log = logging.getLogger(__name__)

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(_BACKEND_DIR, "data")

SUBWAY_COLORS = {
    "1": "#0052A4", "2": "#00A84D", "3": "#EF7C1C", "4": "#00A5DE",
    "5": "#996CAC", "6": "#CD7C2F", "7": "#747F00", "8": "#E6186C", "9": "#BDB092",
    "신분당": "#D4003B", "분당": "#F5A200", "수인분당": "#F5A200",
    "경의중앙": "#77C4A3", "경춘": "#0C8E72", "공항철도": "#0090D2",
    "에버라인": "#56AD21", "우이신설": "#B7C452", "신림": "#6789CA", "GTX": "#9A6292",
}
SUBWAY_DEFAULT = "#3d5bab"
BUS_DEFAULT = "#3D5BAB"

NET_ERRORS = ("Timeout", "ConnectionError", "ConnectTimeout",
              "ReadTimeout", "MaxRetryError", "SSLError")
# ── 혼잡도 자료 ──────────────────────────────────────────────────
CONGESTION_FILE = "subway_congestion.xlsx"
CONGESTION_THRESHOLD = 110.0     # 이 값 이상이면 '혼잡'

# ── 환승저항 (v6.2 신규) ─────────────────────────────────────────
TRANSFER_PENALTY = 11.24         # 분/회. SP 설문 추정치
OTHER_TIME_WEIGHT = 1.0          # '기타(환승대기 등)' 시간 가중치 ω


def is_network_error(e):
    return any(k in type(e).__name__ for k in NET_ERRORS)

def to_wgs84(coords):
    if not coords:
        return []
    x0, y0 = coords[0]
    if 120 < float(x0) < 135 and 30 < float(y0) < 45:
        return [(float(x), float(y)) for x, y in coords]
    try:
        from pyproj import Transformer
        tf = Transformer.from_crs("EPSG:5181", "EPSG:4326", always_xy=True)
        return [tf.transform(float(x), float(y)) for x, y in coords]
    except Exception:
        return [(float(x), float(y)) for x, y in coords]

def _norm_station(name):
    """역명 정규화 — 괄호·공백 제거, 끝의 '역' 제거."""
    s = re.sub(r"\([^)]*\)", "", str(name))
    s = re.sub(r"\s+", "", s).strip()
    s = re.sub(r"역$", "", s)
    return s


def _parse_time_bins(columns):
    """'05:30~06:00' 형태 컬럼을 (시작분, 종료분, 컬럼명)으로 변환."""
    bins = []
    for c in columns:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})\s*", str(c))
        if not m:
            continue
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        bins.append((h1 * 60 + m1, h2 * 60 + m2, c))
    return sorted(bins)


def load_congestion(path=None, verbose=True):
    """
    혼잡도 엑셀을 읽어 조회용 구조로 변환.
    반환: {'평일': {(호선, 역명정규화, 방향): {bin_col: 값}}, ...}, bins
    """
    if path is None:
        path = resolve_congestion_path(DATA_DIR)
    if path is None or not os.path.exists(path):
        if verbose:
            log.info(f"  혼잡도 파일을 찾지 못했습니다: {CONGESTION_FILE}")
            log.info("  → 지하철 시간을 전부 '여유'로 계산합니다.")
        return None

    sheets = load_congestion_sheets(path)
    table, bins = {}, None
    for sheet in ("평일", "토요일", "일요일"):
        if sheet not in sheets:
            continue
        df = sheets[sheet]
        if bins is None:
            bins = _parse_time_bins(df.columns)
        cols = [b[2] for b in bins]

        df["_line"] = df["호선"].astype(str).str.extract(r"(\d+)")[0]
        df["_st"] = df["역명"].map(_norm_station)
        df["_dir"] = df["상하구분"].astype(str).str.strip()

        # 방향별로 따로 보관하고, 방향을 모를 때 쓸 최대값도 함께 저장한다.
        # (상·하선을 평균하면 첨두 혼잡이 상쇄되어 임계값을 거의 넘지 못한다)
        rec = {}
        for (ln, st, dr), grp in df.groupby(["_line", "_st", "_dir"]):
            rec[(ln, st, dr)] = grp[cols].mean().to_dict()
        for (ln, st), grp in df.groupby(["_line", "_st"]):
            rec[(ln, st, "_MAX")] = grp[cols].max().to_dict()
        table[sheet] = rec

    # 역번호(진행 방향 추정용)와 노선별 방향 표기 수집
    base = sheets.get("평일")
    if base is None:
        base = next(iter(sheets.values()))
    base["_line"] = base["호선"].astype(str).str.extract(r"(\d+)")[0]
    base["_st"] = base["역명"].map(_norm_station)
    station_order = (base.dropna(subset=["_line"])
                         .groupby(["_line", "_st"])["역번호"].min().to_dict())
    directions = (base.dropna(subset=["_line"])
                      .groupby("_line")["상하구분"]
                      .apply(lambda s: sorted(set(s.astype(str).str.strip()))).to_dict())

    if verbose and table:
        n = sum(len(v) for v in table.values())
        log.info(f"  혼잡도 자료 로드: {os.path.basename(path)} "
              f"({', '.join(table)}) / {n:,}개 항목")
        log.info(f"  임계값 {CONGESTION_THRESHOLD:.0f} 이상이면 '혼잡'으로 계산")
        log.info("  주의: 자료는 1~8호선만 포함. 9호선·신분당·공항철도 등은")
        log.info("        판정 불가이므로 전 구간 '여유'로 계산됩니다.")
    return {"table": table, "bins": bins, "path": path,
            "station_order": station_order, "directions": directions}


def _sheet_for(dt):
    local = to_seoul(dt)
    wd = local.weekday()
    return "평일" if wd <= 4 else ("토요일" if wd == 5 else "일요일")


def congestion_at(cong, line_no, station, dt, direction=None):
    """특정 역·시각의 혼잡도. 자료가 없으면 None."""
    if not cong or line_no is None:
        return None
    dt = to_seoul(dt)
    tb = cong["table"].get(_sheet_for(dt))
    if not tb:
        return None
    key = (str(line_no), _norm_station(station))
    rec = tb.get(key + (direction,)) if direction else None
    if rec is None:
        rec = tb.get(key + ("_MAX",))
    if rec is None:
        return None

    minute = dt.hour * 60 + dt.minute
    if minute < 5 * 60:                 # 새벽 0~5시는 전날 24시 이후 구간으로
        minute += 24 * 60
    for a, b, col in cong["bins"]:
        if a <= minute < b:
            v = rec.get(col)
            return None if pd.isna(v) else float(v)
    return None


def line_number(name):
    """'수도권 2호선' → '2'. 1~8호선이 아니면 None(혼잡도 자료 없음)."""
    m = re.search(r"(\d+)호선", str(name))
    if not m:
        return None
    n = m.group(1)
    return n if n in {"1", "2", "3", "4", "5", "6", "7", "8"} else None


def _guess_direction(cong, line_no, names):
    """경유 역 순서를 역번호와 대조해 진행 방향(상선/하선)을 추정."""
    if not cong or line_no is None or len(names) < 2:
        return None
    order = cong.get("station_order", {})
    nums = [order.get((str(line_no), _norm_station(n))) for n in names]
    nums = [x for x in nums if x is not None]
    if len(nums) < 2:
        return None
    dirs = cong.get("directions", {}).get(str(line_no), [])
    cands = ("하선", "내선") if nums[-1] > nums[0] else ("상선", "외선")
    for d in cands:
        if d in dirs:
            return d
    return None


def split_subway_congestion(seg, start_dt, cong, threshold=CONGESTION_THRESHOLD):
    """
    지하철 구간의 시간을 (혼잡, 여유)로 분리.
    반환: (혼잡시간, 여유시간, 판정된역수, 혼잡역수, 판정불가여부)
    ※ 역간 소요시간을 균등 배분한다(급행·장대구간은 오차 존재).
    """
    total = float(seg["시간(분)"] or 0)
    if total <= 0:
        return 0.0, 0.0, 0, 0, False

    ln = line_number(seg["수단"])
    names = [n for n in (seg.get("stop_names") or []) if n]
    if cong is None or ln is None or len(names) < 2:
        return 0.0, total, 0, 0, True      # 판정 불가

    direction = _guess_direction(cong, ln, names)

    hops = len(names) - 1
    per_hop = total / hops
    busy = free = 0.0
    checked = busy_cnt = 0

    for i in range(hops):
        at = start_dt + timedelta(minutes=per_hop * i)
        v = congestion_at(cong, ln, names[i], at, direction)
        if v is None:
            free += per_hop                 # 자료 없음 → 여유로 간주
            continue
        checked += 1
        if v >= threshold:
            busy += per_hop
            busy_cnt += 1
        else:
            free += per_hop
    return busy, free, checked, busy_cnt, (checked == 0)


ODSAY_ERRORS = {}      # {오류문구: 발생횟수}  — 한도 초과 등을 마지막에 요약
_ODSAY_ERR_LOCK = threading.Lock()
ANALYZE_WORKERS = 5


def _odsay_headers():
    referer = os.environ.get("ODSAY_REFERER", "").strip()
    if not referer:
        return {}
    return {"Referer": referer}


def _odsay_error(js):
    err = js.get("error") if isinstance(js, dict) else None
    if isinstance(err, list) and err:
        err = err[0]
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or err.get("msg")
        parts = [f"code={code}" if code is not None else None,
                 str(msg) if msg else None]
        return " ".join(p for p in parts if p) or None
    return None


def _odsay_error_code(js):
    err = js.get("error") if isinstance(js, dict) else None
    if isinstance(err, list) and err:
        err = err[0]
    if isinstance(err, dict) and err.get("code") is not None:
        return str(err.get("code"))
    return None


def _note_odsay_error(where, msg):
    key = f"{where}: {msg}"
    with _ODSAY_ERR_LOCK:
        ODSAY_ERRORS[key] = ODSAY_ERRORS.get(key, 0) + 1


def odsay_paths_raw(odsay_key, s_lat, s_lon, e_lat, e_lon):
    if not (odsay_key or "").strip():
        _note_odsay_error("searchPubTransPathT", "ODSAY_API_KEY 없음")
        return []
    try:
        res = requests.get(
            "https://api.odsay.com/v1/api/searchPubTransPathT",
            params={"apiKey": odsay_key, "SX": str(s_lon), "SY": str(s_lat),
                    "EX": str(e_lon), "EY": str(e_lat), "SearchPathType": 0},
            headers=_odsay_headers(), timeout=10)
        js = res.json()
        err = _odsay_error(js)
        if err:
            _note_odsay_error("searchPubTransPathT", err)
            return []
        if res.status_code == 200 and "result" in js:
            return js["result"].get("path") or []
        _note_odsay_error("searchPubTransPathT", f"HTTP {res.status_code}")
    except Exception as e:
        _note_odsay_error("searchPubTransPathT", type(e).__name__)
    return []


_LANE_CACHE = {}       # {mapObj: lanes}  — 같은 경로를 두 번 받지 않는다


def odsay_lanes(odsay_key, map_obj):
    """
    경로 형상(polyline)을 받아온다.
    반환: [{"class": 1|2|None, "coords": [(lon,lat), ...]}, ...]
      class 1 = 지하철, 2 = 버스 (ODsay lane.class)
    subPath와 순서가 어긋날 수 있으므로 class를 함께 돌려주어
    호출측에서 수단 종류로 대조할 수 있게 한다.
    실패 사유는 ODSAY_ERRORS에 기록해 마지막에 요약한다.
    """
    if not map_obj:
        return []
    if map_obj in _LANE_CACHE:
        return _LANE_CACHE[map_obj]
    pkey = f"lane:{map_obj}"
    cached = cache_get(pkey)
    if cached is not None:
        lanes = _lanes_from_cache(cached)
        _LANE_CACHE[map_obj] = lanes
        return lanes
    lanes = []
    cacheable = False
    try:
        if not (odsay_key or "").strip():
            _note_odsay_error("loadLane", "ODSAY_API_KEY 없음")
            _LANE_CACHE[map_obj] = []
            return []
        bump("lane_api")
        res = requests.get(
            "https://api.odsay.com/v1/api/loadLane",
            params={"apiKey": odsay_key, "mapObject": f"0:0@{map_obj}"},
            headers=_odsay_headers(), timeout=15)
        js = res.json()
        err = _odsay_error(js)
        if err or _odsay_error_code(js) == "-98":
            _note_odsay_error("loadLane", err or "code=-98")
        elif "result" not in js:
            _note_odsay_error("loadLane", f"HTTP {res.status_code} / result 없음")
        else:
            for lane in js["result"].get("lane", []):
                coords = []
                for sec in lane.get("section", []):
                    coords += [(p["x"], p["y"]) for p in sec.get("graphPos", [])]
                cls = lane.get("class")
                lanes.append({"class": int(cls) if cls is not None else None,
                              "coords": coords})
            if not lanes:
                _note_odsay_error("loadLane", "lane 배열이 비어 있음")
            else:
                cacheable = True
    except Exception as e:
        _note_odsay_error("loadLane", type(e).__name__)
    if cacheable:
        cache_set(pkey, "odsay", _lanes_to_cache(lanes), TTL_LANE_SEC)
    _LANE_CACHE[map_obj] = lanes
    return lanes


def top3_transit_routes(odsay_key, origin, dest):
    paths = odsay_paths_raw(odsay_key, origin["lat"], origin["lon"],
                            dest["lat"], dest["lon"])
    paths = sorted(paths, key=lambda p: p.get("info", {}).get("totalTime", 10 ** 9))
    return paths[:3]



def leg_style(sub):
    lane = (sub.get("lane") or [{}])[0]
    if sub.get("trafficType") == 1:
        name = lane.get("name") or "지하철"
        key = None
        m = re.search(r"(\d+)호선", name)
        if m:
            key = m.group(1)
        else:
            for k in SUBWAY_COLORS:
                if k in name.replace(" ", ""):
                    key = k
                    break
        return name, SUBWAY_COLORS.get(key, SUBWAY_DEFAULT), "지하철"
    if sub.get("trafficType") == 2:
        no = lane.get("busNo") or "버스"
        return f"{no}번", BUS_DEFAULT, "버스"
    return "기타", SUBWAY_DEFAULT, "버스"


def transit_segments(odsay_key, path_obj, start_dt, cong, threshold,
                     fetch_lane=False):
    """
    대중교통 경로 → 구간 리스트 + 수단별 총시간.

    fetch_lane=False (기본): 형상(polyline)을 받지 않는다.
      대안 수집 단계에서는 수백 개 경로를 만드는데 실제로 지도를 그리는 건
      파레토 해 십여 개뿐이다. 여기서 loadLane을 매번 부르면 ODsay 일일 한도를
      금방 소진하고, 한도를 넘긴 뒤에는 형상이 빈 값으로 돌아와 조용히
      '정류장 직선연결'로 떨어진다. 그래서 형상은 hydrate_geometry()에서
      그릴 대상에 대해서만 나중에 받는다.

    ★ v6.2: 총소요시간과 구간시간 합의 차이를 '기타'로 명시 집계한다.
       기타 = max(0, info.totalTime − (버스+지하철+도보))
    """
    subs_all = path_obj.get("subPath", [])
    lanes = (odsay_lanes(odsay_key, (path_obj.get("info") or {}).get("mapObj"))
             if fetch_lane else [])
    lane_used = [False] * len(lanes)

    def take_lane(traffic_type, pos):
        """
        subPath에 대응하는 lane을 고른다.
        1순위: 아직 안 쓴 lane 중 class가 같은 첫 번째 (순서 어긋남에 강함)
        2순위: 위치 기반 (class 정보가 없는 응답 대비)
        """
        for i, ln in enumerate(lanes):
            if not lane_used[i] and ln["class"] == traffic_type and len(ln["coords"]) >= 2:
                lane_used[i] = True
                return ln["coords"], "lane"
        if pos < len(lanes) and not lane_used[pos] and len(lanes[pos]["coords"]) >= 2:
            lane_used[pos] = True
            return lanes[pos]["coords"], "lane(위치매칭)"
        return None, None

    segments, seg_idx = [], 0
    bus_t = sub_free_t = sub_busy_t = walk_t = 0.0
    elapsed = 0.0          # 출발 후 누적 분
    n_checked = n_busy = n_unknown = 0

    for sub in subs_all:
        t = float(sub.get("sectionTime", 0) or 0)

        if sub.get("trafficType") == 3:
            walk_t += t
            segments.append({"순번": len(segments) + 1, "수단": "도보", "대분류": "도보",
                             "시간(분)": t, "혼잡시간": 0.0, "여유시간": 0.0,
                             "색": "#888888", "coords": [], "stops": [], "stop_names": []})
            elapsed += t
            continue

        name, color, category = leg_style(sub)
        stations = (sub.get("passStopList") or {}).get("stations", [])
        stops = to_wgs84([(s["x"], s["y"]) for s in stations])
        stop_names = [s.get("stationName", "") for s in stations]

        # ── 형상 좌표: lane API → 정류장 직선연결 → 없음 순으로 폴백 ──
        raw, src = take_lane(sub.get("trafficType"), seg_idx)
        if raw and len(raw) >= 2:
            coords, coord_src = to_wgs84(raw), src
        elif len(stops) >= 2:
            coords, coord_src = stops, "정류장직선"     # ★ 실제 노선형상 아님
        else:
            coords, coord_src = [], "없음"              # ★ 지도에 안 그려짐
        seg_idx += 1

        seg = {"순번": len(segments) + 1, "수단": name, "대분류": category,
               "시간(분)": t, "혼잡시간": 0.0, "여유시간": 0.0, "색": color,
               "coords": coords, "stops": stops, "stop_names": stop_names,
               "좌표원천": coord_src, "trafficType": sub.get("trafficType")}

        if category == "지하철":
            busy, free, chk, bcnt, unk = split_subway_congestion(
                seg, start_dt + timedelta(minutes=elapsed), cong, threshold)
            seg["혼잡시간"], seg["여유시간"] = busy, free
            sub_busy_t += busy
            sub_free_t += free
            n_checked += chk
            n_busy += bcnt
            n_unknown += int(unk)
        else:
            bus_t += t

        segments.append(seg)
        elapsed += t

    info = path_obj.get("info", {})
    total = float(info.get("totalTime", 0) or 0)
    comp_sum = bus_t + sub_free_t + sub_busy_t + walk_t
    residual = total - comp_sum          # ★ 음수도 그대로 보존(진단용)
    other_t = max(0.0, residual)

    # ── 도보 교차검증 ────────────────────────────────────────────
    # ODsay는 info.totalWalkTime을 따로 준다(자료 없으면 -1).
    # 도보 subPath의 sectionTime 합과 다르면, subPath에 안 잡힌 도보가
    # '기타'로 흘러들어간 것이다. 그 경우 해당 시간은 대기(ω=1.0)가 아니라
    # 도보(γ=2.35)로 계산되어야 하므로 가중치가 절반 이하로 과소평가된다.
    twt = info.get("totalWalkTime")
    twt = float(twt) if twt is not None else -1.0
    walk_gap = (twt - walk_t) if twt >= 0 else None

    transfer_cnt = max(0, sum(1 for s in subs_all if s.get("trafficType") in (1, 2)) - 1)
    agg = dict(버스=bus_t, 지하철여유=sub_free_t, 지하철혼잡=sub_busy_t, 도보=walk_t,
               기타=other_t,
               환승횟수=transfer_cnt, 비용=info.get("payment", 0) or 0,
               총시간=total, 성분합=comp_sum, 잔차=residual,
               도보_API=twt, 도보_차이=walk_gap,
               판정역수=n_checked, 혼잡역수=n_busy, 판정불가구간=n_unknown,
               mapObj=info.get("mapObj"))
    return segments, agg


def hydrate_geometry(odsay_key, geoms, ids, verbose=True):
    """
    지도를 그릴 대안에 대해서만 ODsay loadLane을 호출해 실제 노선 형상을 채운다.
    같은 mapObj는 캐시되므로 중복 호출이 없다.

    각 구간의 '좌표원천'이 여기서 최종 결정된다.
      lane          : ODsay가 준 실제 노선 형상 (정상)
      lane(위치매칭) : class 대조 실패, 순서로 맞춤 (대체로 정상)
      정류장직선     : loadLane 실패 → 정류장을 직선으로 이은 근사
      없음          : 형상도 정류장도 없음 → 지도에 안 그려짐
    """
    stats = {}
    before_lane = snapshot_stats()["lane_api"]
    for _id in ids:
        g = geoms.get(_id)
        if not g:
            continue
        map_obj = g.get("mapObj")
        segs = [s for s in g.get("segments", []) if s["대분류"] != "도보"]
        if not segs:
            continue

        lanes = odsay_lanes(odsay_key, map_obj) if map_obj else []

        used, pos = [False] * len(lanes), 0
        for sg in segs:
            tt = sg.get("trafficType")
            coords, src = None, None
            # 1순위: 수단 종류(class)가 같은 lane
            for i, ln in enumerate(lanes):
                if not used[i] and ln["class"] == tt and len(ln["coords"]) >= 2:
                    used[i] = True
                    coords, src = to_wgs84(ln["coords"]), "lane"
                    break
            # 2순위: 위치 매칭
            if coords is None and pos < len(lanes) and not used[pos] \
                    and len(lanes[pos]["coords"]) >= 2:
                used[pos] = True
                coords, src = to_wgs84(lanes[pos]["coords"]), "lane(위치매칭)"
            pos += 1

            if coords:
                sg["coords"], sg["좌표원천"] = coords, src
            elif len(sg.get("stops") or []) >= 2:
                sg["coords"], sg["좌표원천"] = sg["stops"], "정류장직선"
            else:
                sg["coords"], sg["좌표원천"] = [], "없음"
            key = sg["좌표원천"]
            stats[key] = stats.get(key, 0) + 1

    if verbose:
        n_call = snapshot_stats()["lane_api"] - before_lane
        total = sum(stats.values())
        log.info(f"  형상 수집: loadLane {n_call}회 호출 / 구간 {total}개")
        for k in ("lane", "lane(위치매칭)", "정류장직선", "없음"):
            if stats.get(k):
                log.info(f"    {k:<14} {stats[k]:>3}개"
                      f"{'   ← 근사' if k == '정류장직선' else ''}"
                      f"{'   ← 미표시' if k == '없음' else ''}")
    return stats


# =====================================================================
# TMAP 택시
# =====================================================================
def _parse_tmap(js):
    props = js["features"][0]["properties"]
    coords = []
    for f in js.get("features", []):
        g = f.get("geometry", {})
        if g.get("type") == "LineString":
            coords += [tuple(c) for c in g.get("coordinates", [])]
    return props.get("totalTime", 0) / 60, props.get("taxiFare", 0), to_wgs84(coords)


_TMAP_PRED_LOCK = threading.Lock()
_TMAP_PREDICTION_UNAVAILABLE = False
_TMAP_PRED_NOTES: list[str] = []


def _parse_depart_iso(depart_iso):
    return parse_iso_to_seoul(depart_iso)


def _depart_near_now(depart_iso, window_sec=30 * 60):
    dt = _parse_depart_iso(depart_iso)
    if dt is None:
        return True
    return abs((dt - now_seoul()).total_seconds()) <= window_sec


def _tmap_pred_unavailable():
    with _TMAP_PRED_LOCK:
        return _TMAP_PREDICTION_UNAVAILABLE


def _mark_tmap_pred_unavailable():
    global _TMAP_PREDICTION_UNAVAILABLE
    with _TMAP_PRED_LOCK:
        _TMAP_PREDICTION_UNAVAILABLE = True


def _note_tmap_pred(note: str):
    with _TMAP_PRED_LOCK:
        _TMAP_PRED_NOTES.append(note)


def _tmap_pred_notes():
    with _TMAP_PRED_LOCK:
        return list(_TMAP_PRED_NOTES)


def _clear_tmap_pred_notes():
    with _TMAP_PRED_LOCK:
        _TMAP_PRED_NOTES.clear()


def _tmap_error_info(resp):
    code = None
    msg = ""
    try:
        js = resp.json()
        err = js.get("error") if isinstance(js, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("id") or "")
            msg = str(err.get("message") or err.get("msg") or "")
    except Exception:
        msg = (resp.text or "")[:180]
    return code, msg


def _tmap_auth_or_unsupported(status, code, msg):
    if status in (401, 403, 402):
        return True
    text = f"{code} {msg}"
    lower = text.lower()
    return any(n in lower for n in ("unauthorized", "not supported", "forbidden", "not authorized")) or any(
        n in text for n in ("권한", "미지원")
    )


def _tmap_live(tmap_key, s_lat, s_lon, e_lat, e_lon):
    r = requests.post(
        "https://apis.openapi.sk.com/tmap/routes?version=1&format=json",
        headers={"appKey": tmap_key},
        data={"startX": str(s_lon), "startY": str(s_lat),
              "endX": str(e_lon), "endY": str(e_lat),
              "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO"}, timeout=8)
    if r.status_code == 200:
        minutes, fare, coords = _parse_tmap(r.json())
        return minutes, fare, coords, "live"
    return None, None, [], None


def tmap_taxi(tmap_key, s_lat, s_lon, e_lat, e_lon, depart_iso):
    """※ 반환 시간은 '주행시간'이며 호출·배차 대기시간은 포함하지 않는다.

    네 번째 값은 경로 출처:
      prediction : TMAP 타임머신(지정 출발 시각)
      live       : 현재 시각 기준 일반 경로 (가까운 출발이거나 예측 생략)
      live_fallback : 예측 실패 후 실시간
      None       : 실패
    """
    use_prediction = not _depart_near_now(depart_iso) and not _tmap_pred_unavailable()
    if use_prediction:
        try:
            r = requests.post(
                "https://apis.openapi.sk.com/tmap/routes/prediction?version=1&format=json",
                headers={"appKey": tmap_key, "Content-Type": "application/json"},
                json={"routesInfo": {
                    "departure": {"name": "출발", "lon": str(s_lon), "lat": str(s_lat)},
                    "destination": {"name": "도착", "lon": str(e_lon), "lat": str(e_lat)},
                    "predictionType": "departure",
                    "predictionTime": depart_iso,
                    "searchOption": "00",
                }}, timeout=8)
            if r.status_code == 200:
                minutes, fare, coords = _parse_tmap(r.json())
                return minutes, fare, coords, "prediction"
            code, msg = _tmap_error_info(r)
            note = f"HTTP {r.status_code}" + (f" code={code}" if code else "")
            _note_tmap_pred(note)
            log.warning("TMAP prediction HTTP %s code=%s msg=%s", r.status_code, code, msg[:120])
            if _tmap_auth_or_unsupported(r.status_code, code, msg):
                _mark_tmap_pred_unavailable()
        except Exception as exc:
            _note_tmap_pred(type(exc).__name__)
            log.warning("TMAP prediction error: %s", type(exc).__name__)
        try:
            minutes, fare, coords, source = _tmap_live(tmap_key, s_lat, s_lon, e_lat, e_lon)
            if source == "live":
                return minutes, fare, coords, "live_fallback"
            return minutes, fare, coords, source
        except Exception:
            return None, None, [], None
    try:
        return _tmap_live(tmap_key, s_lat, s_lon, e_lat, e_lon)
    except Exception:
        return None, None, [], None


# =====================================================================
# 대안 생성 (PT / TP)
# =====================================================================
def _coord_key(lat, lon):
    return (round(float(lat), 4), round(float(lon), 4))


def _coord_token(lat, lon):
    return f"{round(float(lat), 4):.4f},{round(float(lon), 4):.4f}"


def _odsay_persist_key(s_lat, s_lon, e_lat, e_lon, sheet):
    return f"odsay:{_coord_token(s_lat, s_lon)}:{_coord_token(e_lat, e_lon)}:{sheet}"


def _tmap_persist_key(s_lat, s_lon, e_lat, e_lon, sheet, hour):
    return f"tmap:{_coord_token(s_lat, s_lon)}:{_coord_token(e_lat, e_lon)}:{sheet}:{hour}"


def _tmap_bucket(depart_iso, fallback_sheet, fallback_hour):
    dt = _parse_depart_iso(depart_iso)
    if dt is None:
        return fallback_sheet, fallback_hour
    local = to_seoul(dt)
    return _sheet_for(local), local.strftime("%H")


def _lanes_to_cache(lanes):
    out = []
    for lane in lanes:
        out.append({
            "class": lane.get("class"),
            "coords": [list(p) for p in (lane.get("coords") or [])],
        })
    return out


def _lanes_from_cache(raw):
    lanes = []
    for lane in raw or []:
        lanes.append({
            "class": lane.get("class"),
            "coords": [tuple(p) for p in (lane.get("coords") or [])],
        })
    return lanes


def _tmap_to_cache(minutes, fare, coords):
    return {
        "minutes": minutes,
        "fare": fare,
        "coords": [list(p) for p in (coords or [])],
    }


def _tmap_from_cache(raw):
    if not raw:
        return None, None, []
    coords = [tuple(p) for p in (raw.get("coords") or [])]
    return raw.get("minutes"), raw.get("fare"), coords


def _resolve_max_cand(P):
    raw = P.get("max_cand")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 20
    if val <= 0:
        val = 20
    return val


class _ApiMeter:
    """같은 요청 안에서는 동일한 역 쌍 API를 한 번만 호출한다."""

    def __init__(self, time_key=None):
        self.lock = threading.Lock()
        self.time_key = time_key or ("평일", "00")
        self.odsay_n = 0
        self.odsay_s = 0.0
        self.tmap_n = 0
        self.tmap_s = 0.0
        self.tmap_live_fallback = 0
        self.tmap_pred_ok = 0
        self.odsay_cache = {}
        self.tmap_cache = {}

    def odsay_paths(self, odsay_key, s_lat, s_lon, e_lat, e_lon):
        sheet = self.time_key[0] if isinstance(self.time_key, tuple) else str(self.time_key)
        ck = (_coord_key(s_lat, s_lon), _coord_key(e_lat, e_lon), sheet)
        with self.lock:
            hit = self.odsay_cache.get(ck)
        if hit is not None:
            return hit
        pkey = _odsay_persist_key(s_lat, s_lon, e_lat, e_lon, sheet)
        cached = cache_get(pkey)
        if cached is not None:
            with self.lock:
                self.odsay_cache[ck] = cached
            return cached
        t0 = time.perf_counter()
        bump("odsay_api")
        paths = odsay_paths_raw(odsay_key, s_lat, s_lon, e_lat, e_lon)
        dt = time.perf_counter() - t0
        with self.lock:
            self.odsay_n += 1
            self.odsay_s += dt
            self.odsay_cache[ck] = paths
        if paths:
            cache_set(pkey, "odsay", paths, TTL_ODSAY_SEC)
        return paths

    def taxi(self, tmap_key, s_lat, s_lon, e_lat, e_lon, depart_iso):
        sheet, hour = _tmap_bucket(
            depart_iso,
            self.time_key[0] if isinstance(self.time_key, tuple) else "평일",
            self.time_key[1] if isinstance(self.time_key, tuple) and len(self.time_key) > 1 else "00",
        )
        ck = (_coord_key(s_lat, s_lon), _coord_key(e_lat, e_lon), sheet, hour)
        with self.lock:
            hit = self.tmap_cache.get(ck)
        if hit is not None:
            return hit
        pkey = _tmap_persist_key(s_lat, s_lon, e_lat, e_lon, sheet, hour)
        cached = cache_get(pkey)
        if cached is not None:
            result = _tmap_from_cache(cached)
            with self.lock:
                self.tmap_cache[ck] = result
            return result
        t0 = time.perf_counter()
        bump("tmap_api")
        minutes, fare, coords, source = tmap_taxi(tmap_key, s_lat, s_lon, e_lat, e_lon, depart_iso)
        dt = time.perf_counter() - t0
        result = (minutes, fare, coords)
        with self.lock:
            self.tmap_n += 1
            self.tmap_s += dt
            if source == "live_fallback":
                self.tmap_live_fallback += 1
            if source == "prediction":
                self.tmap_pred_ok += 1
            self.tmap_cache[ck] = result
        if minutes is not None:
            cache_set(pkey, "tmap", _tmap_to_cache(minutes, fare, coords), TTL_TMAP_SEC)
        return result


def collect_alternatives(P, origin, dest, cong, progress_cb=None):
    rows, geoms = [], {}
    rid = 0
    rid_lock = threading.Lock()
    meter = _ApiMeter(time_key=(_sheet_for(P["dep_dt"]), P["dep_dt"].strftime("%H")))
    stats = {
        "station_s": 0.0,
        "hybrid_s": 0.0,
        "raw_stations": 0,
        "pt_planned": 0,
        "tp_planned": 0,
    }

    def nid():
        nonlocal rid
        with rid_lock:
            rid += 1
            return f"R{rid:04d}"

    def report(progress, stage):
        if progress_cb:
            try:
                progress_cb(int(max(0, min(100, progress))), stage)
            except Exception:
                pass

    dep_iso = format_tmap_time(P["dep_dt"])
    thr = P["threshold"]
    max_cand = _resolve_max_cand(P)

    # ── Only 택시 ───────────────────────────────────────────────
    log.info("\n  Only 택시 직행")
    report(4, "환승 지점을 찾고 있어요")
    tt, tf, tcoords = meter.taxi(
        P["tmap_key"], origin["lat"], origin["lon"], dest["lat"], dest["lon"], dep_iso)
    if tt is not None:
        _id = nid()
        rows.append(dict(id=_id, 유형="Only 택시", 경로순위=0, 환승지점="-", 모드전환=0,
                         버스=0.0, 지하철여유=0.0, 지하철혼잡=0.0, 도보=0.0,
                         택시=round(tt, 1), 기타=0.0,
                         환승횟수=0, 비용=tf or 0,
                         실소요시간=round(tt, 1), 구간요약="택시 직행",
                         판정불가구간=0))
        geoms[_id] = {"segments": [], "taxi": tcoords, "transfer": None}
        log.info(f"    OK {tt:.1f}분 / {tf:,}원")
    else:
        log.info("    실패")

    # ── Only 대중교통 (최속 3개) ────────────────────────────────
    log.info("\n  대중교통 최속 경로 3개 조회")
    t_st = time.perf_counter()
    report(8, "환승 지점을 찾고 있어요")
    routes = meter.odsay_paths(
        P["odsay_key"], origin["lat"], origin["lon"], dest["lat"], dest["lon"])
    routes = sorted(routes, key=lambda p: p.get("info", {}).get("totalTime", 10 ** 9))[:3]
    log.info(f"    {len(routes)}개 수신")

    jobs = []
    raw_stations = 0
    for rank, path in enumerate(routes, 1):
        segs, agg = transit_segments(P["odsay_key"], path, P["dep_dt"], cong, thr)
        summary = " -> ".join(f"{s['수단']}({s['시간(분)']:.0f}분)"
                              for s in segs if s["대분류"] != "도보")

        _id = nid()
        rows.append(dict(id=_id, 유형="Only 대중교통", 경로순위=rank, 환승지점="-", 모드전환=0,
                         버스=agg["버스"], 지하철여유=agg["지하철여유"],
                         지하철혼잡=agg["지하철혼잡"], 도보=agg["도보"], 택시=0.0,
                         기타=agg["기타"],
                         환승횟수=agg["환승횟수"], 비용=agg["비용"],
                         실소요시간=agg["총시간"], 구간요약=summary,
                         판정불가구간=agg["판정불가구간"]))
        geoms[_id] = {"segments": segs, "taxi": [], "transfer": None,
                       "mapObj": agg["mapObj"]}
        log.info(f"    [{rank}] Only대중교통: {agg['총시간']:.0f}분 "
              f"(성분합 {agg['성분합']:.0f} + 기타 {agg['기타']:.0f}) / "
              f"{agg['비용']:,}원 / 환승{agg['환승횟수']}회")
        log.info(f"        지하철 혼잡{agg['지하철혼잡']:.0f}분·여유{agg['지하철여유']:.0f}분 "
              f"(판정 {agg['판정역수']}역 중 혼잡 {agg['혼잡역수']}역, "
              f"판정불가 {agg['판정불가구간']}구간)")

        stations = [s for seg in segs if seg["대분류"] in ("버스", "지하철")
                    for s in zip(seg["stop_names"], seg["stops"])]
        uniq, seen = [], set()
        for name, xy in stations:
            if name in seen or not name:
                continue
            seen.add(name)
            uniq.append((name, xy))
        cand = uniq[:max_cand]
        raw_stations += len(cand)
        log.info(f"        환승후보 {len(cand)}개 (PT/TP 각각, max_cand={max_cand})")
        for name, xy in cand:
            if not xy or len(xy) < 2:
                continue
            jobs.append(("PT", rank, name, xy[0], xy[1]))
        for name, xy in cand:
            if not xy or len(xy) < 2:
                continue
            jobs.append(("TP", rank, name, xy[0], xy[1]))

    stats["station_s"] = time.perf_counter() - t_st
    stats["raw_stations"] = raw_stations
    stats["pt_planned"] = sum(1 for j in jobs if j[0] == "PT")
    stats["tp_planned"] = sum(1 for j in jobs if j[0] == "TP")
    log.info(
        f"  환승 후보역 탐색 {stats['station_s']:.2f}s / 경로별 합 {raw_stations} / "
        f"PT {stats['pt_planned']} / TP {stats['tp_planned']}"
    )

    planned = max(1, len(jobs))
    done_hyb = 0
    done_lock = threading.Lock()

    def bump():
        nonlocal done_hyb
        with done_lock:
            done_hyb += 1
            frac = done_hyb / planned
            report(12 + int(frac * 72), "경로를 비교하고 있어요")

    def build_pt(rank, name, clon, clat):
        legs = meter.odsay_paths(P["odsay_key"], origin["lat"], origin["lon"], clat, clon)
        if not legs:
            return None
        leg = sorted(legs, key=lambda p: p["info"]["totalTime"])[0]
        lsegs, lagg = transit_segments(P["odsay_key"], leg, P["dep_dt"], cong, thr)
        t_min = lagg["총시간"]
        taxi_iso = format_tmap_time(P["dep_dt"] + timedelta(minutes=t_min))
        stt, stf, st_coords = meter.taxi(P["tmap_key"], clat, clon, dest["lat"], dest["lon"], taxi_iso)
        if stt is None:
            return None
        lsum = " -> ".join(f"{s['수단']}({s['시간(분)']:.0f}분)"
                           for s in lsegs if s["대분류"] != "도보")
        row = dict(유형="PT(대중교통+택시)", 경로순위=rank, 환승지점=name,
                   모드전환=1,
                   버스=lagg["버스"], 지하철여유=lagg["지하철여유"],
                   지하철혼잡=lagg["지하철혼잡"], 도보=lagg["도보"],
                   택시=round(stt, 1), 기타=lagg["기타"],
                   환승횟수=lagg["환승횟수"],
                   비용=(lagg["비용"] or 0) + (stf or 0),
                   실소요시간=round(t_min + stt, 1),
                   구간요약=f"{lsum} -> 택시({stt:.0f}분)",
                   판정불가구간=lagg["판정불가구간"])
        geom = {"segments": lsegs, "taxi": st_coords,
                "transfer": (clon, clat), "mapObj": lagg["mapObj"]}
        return row, geom

    def build_tp(rank, name, clon, clat):
        stt2, stf2, st2 = meter.taxi(
            P["tmap_key"], origin["lat"], origin["lon"], clat, clon, dep_iso)
        if stt2 is None:
            return None
        legs2 = meter.odsay_paths(P["odsay_key"], clat, clon, dest["lat"], dest["lon"])
        if not legs2:
            return None
        leg2 = sorted(legs2, key=lambda p: p["info"]["totalTime"])[0]
        lsegs2, lagg2 = transit_segments(
            P["odsay_key"], leg2, P["dep_dt"] + timedelta(minutes=stt2), cong, thr)
        lsum2 = " -> ".join(f"{s['수단']}({s['시간(분)']:.0f}분)"
                            for s in lsegs2 if s["대분류"] != "도보")
        row = dict(유형="TP(택시+대중교통)", 경로순위=rank, 환승지점=name,
                   모드전환=1,
                   버스=lagg2["버스"], 지하철여유=lagg2["지하철여유"],
                   지하철혼잡=lagg2["지하철혼잡"], 도보=lagg2["도보"],
                   택시=round(stt2, 1), 기타=lagg2["기타"],
                   환승횟수=lagg2["환승횟수"],
                   비용=(stf2 or 0) + (lagg2["비용"] or 0),
                   실소요시간=round(stt2 + lagg2["총시간"], 1),
                   구간요약=f"택시({stt2:.0f}분) -> {lsum2}",
                   판정불가구간=lagg2["판정불가구간"])
        geom = {"segments": lsegs2, "taxi": st2,
                "transfer": (clon, clat), "mapObj": lagg2["mapObj"]}
        return row, geom

    t_hyb = time.perf_counter()
    report(12, "경로를 비교하고 있어요")
    results = [None] * len(jobs)

    def run_job(idx, kind, rank, name, clon, clat):
        try:
            if kind == "PT":
                return idx, build_pt(rank, name, clon, clat)
            return idx, build_tp(rank, name, clon, clat)
        except Exception as exc:
            log.warning("    %s %s 실패: %s", kind, name, exc)
            return idx, None

    if jobs:
        with ThreadPoolExecutor(max_workers=ANALYZE_WORKERS) as pool:
            futs = [
                pool.submit(run_job, idx, kind, rank, name, clon, clat)
                for idx, (kind, rank, name, clon, clat) in enumerate(jobs)
            ]
            for fut in as_completed(futs):
                idx, item = fut.result()
                results[idx] = item
                bump()

    for item in results:
        if not item:
            continue
        row, geom = item
        _id = nid()
        row = dict(row, id=_id)
        rows.append(row)
        geoms[_id] = geom

    stats["hybrid_s"] = time.perf_counter() - t_hyb
    stats["odsay_n"] = meter.odsay_n
    stats["odsay_s"] = meter.odsay_s
    stats["tmap_n"] = meter.tmap_n
    stats["tmap_s"] = meter.tmap_s
    stats["tmap_live_fallback"] = meter.tmap_live_fallback
    stats["tmap_pred_ok"] = meter.tmap_pred_ok
    log.info(
        f"  ODsay 호출 {meter.odsay_n}회 / {meter.odsay_s:.2f}s, "
        f"TMAP 호출 {meter.tmap_n}회 / {meter.tmap_s:.2f}s, "
        f"하이브리드 {stats['hybrid_s']:.2f}s (workers={ANALYZE_WORKERS})"
    )
    return rows, geoms, stats


# =====================================================================
# 가중시간 · GC
# =====================================================================
def build_dataframe(rows, P):
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    for c in ("모드전환", "기타", "판정불가구간"):
        if c not in df.columns:
            df[c] = 0

    # ── 환승저항 ────────────────────────────────────────────────
    df["환승계"] = df["환승횟수"] + (df["모드전환"] if P["count_mc"] else 0)
    df["환승저항(분)"] = P["tau_tr"] * df["환승계"]

    # ── 가중치 적용 시간 ────────────────────────────────────────
    df["가중치_적용_시간"] = (P["alpha_bus"] * df["버스"]
                          + P["beta_sub"] * df["지하철여유"]
                          + P["beta_sub_c"] * df["지하철혼잡"]
                          + P["gamma_walk"] * df["도보"]
                          + P["delta_taxi"] * df["택시"]
                          + P["omega_other"] * df["기타"]
                          + df["환승저항(분)"])

    df["지하철"] = df["지하철여유"] + df["지하철혼잡"]
    df["성분합"] = (df["버스"] + df["지하철"] + df["도보"]
                  + df["택시"] + df["기타"])
    df["시간정합오차"] = (df["실소요시간"] - df["성분합"]).round(2)

    df["GC"] = P["vot"] * df["가중치_적용_시간"] + df["비용"]
    df["-GC(효용)"] = -df["GC"]
    df["예상도착시각"] = df["실소요시간"].apply(
        lambda m: P["dep_dt"] + timedelta(minutes=float(m)))
    return df


# =====================================================================
# 파레토 · 선정
# =====================================================================
def pareto_front(df, tcol="가중치_적용_시간", ccol="비용", tol=1e-9):
    t, c = df[tcol].to_numpy(float), df[ccol].to_numpy(float)
    n = len(df)
    mask = np.zeros(n, dtype=bool)
    for i in range(n):
        dominated = np.any((t <= t[i] + tol) & (c <= c[i] + tol) &
                           ((t < t[i] - tol) | (c < c[i] - tol)))
        mask[i] = not dominated
    return df.loc[mask].copy()


def label_pareto(pareto_df):
    """
    파레토 해를 가중시간 오름차순으로 정렬하고 P01, P02, ... 라벨을 붙인다.
    이 라벨이 지도 파일명·파레토 그림의 주석과 1:1로 대응한다.
    """
    pf = pareto_df.sort_values("가중치_적용_시간").reset_index(drop=True)
    pf.insert(0, "라벨", [f"P{i:02d}" for i in range(1, len(pf) + 1)])
    return pf


def knee_score(pareto_df, tcol="가중치_적용_시간", ccol="비용"):
    """
    파레토 전선의 무릎(knee)을 부호 있는 점수로 구한다.

    양 끝점
        A = (최저 시간, 최대 비용)   전선의 좌상단 끝
        B = (최대 시간, 최저 비용)   전선의 우하단 끝
    두 축의 단위가 다르므로(분 vs 원) 먼저 [0,1]로 정규화한다.
    정규화하면 A=(0,1), B=(1,0)이 되어 현(chord)이 반대각선이 되고,
    점 P=(t', c')의 부호 있는 거리는 외적에서 바로 떨어진다.

        cross = (x_B−x_A)(c'−y_A) − (y_B−y_A)(t'−x_A)
              = 1·(c'−1) − (−1)·t'
              = t' + c' − 1

        KneeScore = (t' + c' − 1) / √2

    부호 규약
        음수 : 현보다 좌하단 → 시간·비용 둘 다 현보다 유리 (볼록하게 패임)
        0    : 현 위 (양 끝점 포함)
        양수 : 현보다 우상단 → 둘 다 불리

    따라서 KneeScore 최솟값이 무릎이며, 이 점수 기준의 최적해다.

    반환: (라벨 붙은 df, A행, B행)
    """
    out = pareto_df.copy()
    t = out[tcol].to_numpy(float)
    c = out[ccol].to_numpy(float)

    t_span = t.max() - t.min()
    c_span = c.max() - c.min()
    if len(out) < 3 or t_span < 1e-9 or c_span < 1e-9:
        out["KneeScore"] = 0.0
        out["Knee거리"] = 0.0
        return out, None, None

    tn = (t - t.min()) / t_span
    cn = (c - c.min()) / c_span

    out["KneeScore"] = (tn + cn - 1.0) / math.sqrt(2.0)
    # 참고용: 정규화 공간에서의 절대 수직거리
    out["Knee거리"] = np.abs(out["KneeScore"])

    A = out.loc[out[tcol].idxmin()]      # 최저 시간 = 최대 비용 쪽 끝
    B = out.loc[out[tcol].idxmax()]      # 최대 시간 = 최저 비용 쪽 끝
    return out, A, B


def top_by_knee(pareto_df, k=3):
    """
    KneeScore 오름차순(=현에서 좌하단으로 가장 깊이 패인 순) 상위 k개.
    KneeScore가 0 이상인 해는 현 위이거나 우상단이므로 무릎이 아니다.
    그런 해만 남으면(전선이 오목) 빈 리스트를 반환해 호출측이 알 수 있게 한다.
    """
    if "KneeScore" not in pareto_df.columns:
        return []
    s = pareto_df.loc[pareto_df["KneeScore"] < -1e-9].sort_values("KneeScore")
    return [s.iloc[i] for i in range(min(k, len(s)))]


def top_by_utility(pareto_df, k=3):
    """
    파레토 해 중 효용(-GC)이 큰 순으로 최대 k개.
    -GC 내림차순 == GC 오름차순이며, 전역 GC 최소해는 항상 파레토 위에 있다
    (지배당한 점은 두 축 모두 열등하므로 GC가 반드시 더 크다).
    파레토로 한정했으므로 추천 2·3위도 지배당한 해가 아님이 보장된다.
    """
    s = pareto_df.sort_values("-GC(효용)", ascending=False)
    return [s.iloc[i] for i in range(min(k, len(s)))]


_CONG_CACHE = {"loaded": False, "data": None}


def get_congestion():
    """혼잡도 자료를 backend/data 에서 최초 1회만 로드해 캐시한다."""
    if not _CONG_CACHE["loaded"]:
        path = resolve_congestion_path(DATA_DIR)
        _CONG_CACHE["data"] = load_congestion(path=path, verbose=True)
        _CONG_CACHE["loaded"] = True
    return _CONG_CACHE["data"]


_TYPE_MAP = {
    "Only 택시": "TT",
    "Only 대중교통": "PP",
    "PT(대중교통+택시)": "PT",
    "TP(택시+대중교통)": "TP",
}

_DEFAULT_PARAMS = dict(
    alpha_bus=1.18,
    beta_sub=1.0,
    beta_sub_c=1.55,
    gamma_walk=2.35,
    delta_taxi=0.88,
    omega_other=OTHER_TIME_WEIGHT,
    tau_tr=TRANSFER_PENALTY,
    count_mc=True,
    vot=400.0,
    threshold=CONGESTION_THRESHOLD,
    max_cand=20,
)

_PARAM_ALIASES = {
    "TRANSFER_PENALTY": "tau_tr",
    "transfer_penalty": "tau_tr",
    "alpha": "alpha_bus",
    "beta": "beta_sub",
    "beta_c": "beta_sub_c",
    "gamma": "gamma_walk",
    "delta": "delta_taxi",
    "omega": "omega_other",
    "VOT": "vot",
}


def _env_key(*names):
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _as_point(p):
    if p.get("lng") is not None and p.get("lon") is None:
        lon = p["lng"]
    else:
        lon = p.get("lon")
    return {"name": p.get("name") or "", "lat": float(p["lat"]), "lon": float(lon)}


def _py(v):
    if v is None:
        return None
    if isinstance(v, (np.generic,)):
        v = v.item()
    if isinstance(v, np.ndarray):
        return [_py(x) for x in v.tolist()]
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return float(v)
    if isinstance(v, (int, bool, str)):
        return v
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _coords(seq):
    out = []
    for pt in seq or []:
        if pt is None or len(pt) < 2:
            continue
        out.append([float(pt[0]), float(pt[1])])
    return out


def _legs(row, geom):
    geom = geom or {}
    segs = list(geom.get("segments") or [])
    taxi_coords = _coords(geom.get("taxi") or [])
    taxi_leg = None
    if float(row.get("택시") or 0) > 0 or taxi_coords:
        taxi_leg = {
            "mode": "택시",
            "name": "택시",
            "minutes": _py(float(row.get("택시") or 0)),
            "color": "#d62728",
            "coords": taxi_coords,
        }
    built = []
    for s in segs:
        built.append({
            "mode": s.get("대분류") or "",
            "name": s.get("수단") or "",
            "minutes": _py(float(s.get("시간(분)") or 0)),
            "color": s.get("색") or "",
            "coords": _coords(s.get("coords") or []),
        })
    유형 = str(row.get("유형") or "")
    if 유형.startswith("TP") and taxi_leg:
        return [taxi_leg] + built
    if taxi_leg:
        return built + [taxi_leg]
    return built


def _merge_params(params):
    P = dict(_DEFAULT_PARAMS)
    P["kakao_key"] = _env_key("KAKAO_REST_API_KEY")
    P["tmap_key"] = _env_key("TMAP_API_KEY")
    P["odsay_key"] = _env_key("ODSAY_API_KEY")
    extra = dict(params or {})
    for src, dst in _PARAM_ALIASES.items():
        if src in extra and dst not in extra:
            extra[dst] = extra[src]
    for k, v in extra.items():
        if k in _PARAM_ALIASES:
            continue
        P[k] = v
    return P


def _unknown_lines(geoms):
    names = []
    seen = set()
    for g in (geoms or {}).values():
        for s in g.get("segments") or []:
            if s.get("대분류") != "지하철":
                continue
            nm = s.get("수단") or ""
            if line_number(nm) is None and nm not in seen:
                seen.add(nm)
                names.append(nm)
    return names


def analyze_routes(
    origin: dict,
    dest: dict,
    depart_dt: datetime,
    params: dict | None = None,
    progress_cb=None,
) -> dict:
    """좌표가 주어진 출발/도착에 대해 복합경로·GC·파레토·Knee를 계산한다."""
    ODSAY_ERRORS.clear()
    reset_stats()
    t_all = time.perf_counter()

    def report(progress, stage):
        if progress_cb:
            try:
                progress_cb(int(max(0, min(100, progress))), stage)
            except Exception:
                pass

    report(1, "서버를 깨우는 중이에요")
    P = _merge_params(params)
    P["dep_dt"] = to_seoul(depart_dt)
    _clear_tmap_pred_notes()
    origin = _as_point(origin)
    dest = _as_point(dest)

    t0 = time.perf_counter()
    cong = get_congestion()
    cong_s = time.perf_counter() - t0
    report(6, "환승 지점을 찾고 있어요")
    rows, geoms, api_stats = collect_alternatives(P, origin, dest, cong, progress_cb=report)
    if not rows:
        warnings = []
        if cong is None:
            warnings.append(f"혼잡도 파일을 찾지 못했습니다: {CONGESTION_FILE}")
        for k, v in sorted(ODSAY_ERRORS.items(), key=lambda x: -x[1]):
            warnings.append(f"{v}회 {k}")
        report(100, "경로를 비교하고 있어요")
        return {
            "candidates": [],
            "top_gc": [],
            "top_knee": [],
            "anchors": {"fastest": None, "cheapest": None},
            "warnings": warnings,
            "cache_stats": snapshot_stats(),
            "applied_depart_time": format_seoul_iso(P["dep_dt"]),
            "congestion_sheet": _sheet_for(P["dep_dt"]),
            "congestion_clock": P["dep_dt"].strftime("%H:%M"),
            "tmap_prediction": "none",
            "tmap_prediction_error": None,
        }

    report(90, "경로를 비교하고 있어요")
    t1 = time.perf_counter()
    df = build_dataframe(rows, P)
    df = df[df["실소요시간"] > 0].reset_index(drop=True)

    pareto = pareto_front(df).sort_values("가중치_적용_시간").reset_index(drop=True)
    pareto = label_pareto(pareto)
    pareto, _knee_A, _knee_B = knee_score(pareto)
    tops = top_by_utility(pareto, k=3)
    knees = top_by_knee(pareto, k=3)

    hydrate_geometry(P["odsay_key"], geoms, list(df["id"]), verbose=True)
    score_s = time.perf_counter() - t1

    pareto_ids = set(pareto["id"].tolist())
    knee_map = {r["id"]: float(r["KneeScore"]) for _, r in pareto.iterrows()} if "KneeScore" in pareto.columns else {}

    candidates = []
    for _, row in df.iterrows():
        rid = row["id"]
        candidates.append({
            "id": str(rid),
            "type": _TYPE_MAP.get(str(row["유형"]), str(row["유형"])),
            "total_time": _py(float(row["실소요시간"])),
            "weighted_time": _py(float(row["가중치_적용_시간"])),
            "cost": _py(float(row["비용"])),
            "transfers": _py(int(row["환승계"])),
            "gc": _py(float(row["GC"])),
            "knee_score": _py(knee_map.get(rid)),
            "is_pareto": rid in pareto_ids,
            "transfer_station": None if str(row["환승지점"]) == "-" else str(row["환승지점"]),
            "legs": _legs(row, geoms.get(rid)),
        })

    warnings = []
    if cong is None:
        warnings.append(f"혼잡도 파일을 찾지 못했습니다: {CONGESTION_FILE}")
    unk = _unknown_lines(geoms)
    if unk:
        warnings.append("혼잡도 판정 불가 노선: " + ", ".join(unk))
    if "판정불가구간" in df.columns and int((df["판정불가구간"] > 0).sum()) > 0:
        warnings.append(
            f"혼잡도 판정불가 지하철 구간 포함 대안: {int((df['판정불가구간'] > 0).sum())}개"
        )
    for k, v in sorted(ODSAY_ERRORS.items(), key=lambda x: -x[1]):
        warnings.append(f"{v}회 {k}")
    if abs((P["dep_dt"] - now_seoul()).total_seconds()) > 15 * 60:
        warnings.append("ODsay 대중교통 경로는 출발 시각 지정을 지원하지 않아 현재 시각 기준 경로를 사용합니다.")
    tmap_notes = _tmap_pred_notes()
    if api_stats.get("tmap_live_fallback"):
        extra = tmap_notes[0] if tmap_notes else ""
        warnings.append(
            f"택시 시간은 현재 교통 기준 ({extra})" if extra else "택시 시간은 현재 교통 기준"
        )
    if api_stats.get("tmap_pred_ok"):
        tmap_prediction = "success"
    elif api_stats.get("tmap_live_fallback"):
        tmap_prediction = "fallback"
    else:
        tmap_prediction = "live_now"

    fastest = None
    cheapest = None
    if len(df):
        fastest = str(df.loc[df["실소요시간"].idxmin(), "id"])
        cheapest = str(df.loc[df["비용"].idxmin(), "id"])

    type_counts = df["유형"].value_counts().to_dict() if len(df) else {}
    log.info(
        "  단계 시간: 혼잡도 %.2fs / 환승후보 %.2fs / ODsay %d회 %.2fs / "
        "TMAP %d회 %.2fs / 혼잡·GC·파레토 %.2fs / 전체 %.2fs",
        cong_s,
        api_stats.get("station_s", 0.0),
        api_stats.get("odsay_n", 0),
        api_stats.get("odsay_s", 0.0),
        api_stats.get("tmap_n", 0),
        api_stats.get("tmap_s", 0.0),
        score_s,
        time.perf_counter() - t_all,
    )
    log.info(
        "  후보 수: 전체 %d / %s / 외부 API %d회 (ODsay %d + TMAP %d) / tau_tr=%.2f",
        len(df),
        type_counts,
        api_stats.get("odsay_n", 0) + api_stats.get("tmap_n", 0),
        api_stats.get("odsay_n", 0),
        api_stats.get("tmap_n", 0),
        float(P.get("tau_tr") or 0),
    )
    report(100, "경로를 비교하고 있어요")

    return {
        "candidates": candidates,
        "top_gc": [str(r["id"]) for r in tops],
        "top_knee": [str(r["id"]) for r in knees],
        "anchors": {"fastest": fastest, "cheapest": cheapest},
        "warnings": warnings,
        "cache_stats": snapshot_stats(),
        "applied_depart_time": format_seoul_iso(P["dep_dt"]),
        "congestion_sheet": _sheet_for(P["dep_dt"]),
        "congestion_clock": P["dep_dt"].strftime("%H:%M"),
        "tmap_prediction": tmap_prediction,
        "tmap_prediction_error": tmap_notes[0] if tmap_notes else None,
    }
