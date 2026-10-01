# -*- coding: utf-8 -*-
"""
MaaS 환승점 분석 파이프라인 v6.3  (Knee Score 도입)
====================================================================
v6.2 대비 변경점
  0) Knee Score 신설 — VOT를 쓰지 않는 두 번째 선정 기준
     파레토 전선의 양 끝점 A(최저시간·최대비용), B(최대시간·최저비용)를
     이은 현(chord)으로부터의 부호 있는 거리.
     두 축을 [0,1]로 정규화하면 A=(0,1), B=(1,0)이 되어

         KneeScore = (t' + c' − 1) / √2

     · 음수 = 현보다 좌하단 → 시간·비용 둘 다 유리 (볼록하게 패임)
     · 양수 = 현보다 우상단 → 둘 다 불리
     · 최솟값 = 무릎(knee) = 이 기준의 최적해
     GC와 달리 VOT가 개입하지 않으므로, 두 기준의 결론을 비교하면
     VOT 설정이 결과를 좌우하는지 진단할 수 있다.
     또한 가중합(GC)이 구조적으로 도달할 수 없는 비지지해도 평가한다.
  0b) 추천을 두 계열로 산출 — GC 상위 3개 / Knee 상위 3개

v6.1 대비 변경점
  1) 환승저항 τ(분/회) 도입
       T_w = α·버스 + β·지하철(여유) + β_c·지하철(혼잡) + γ·도보
             + δ·택시 + ω·기타 + τ·환승계
     · 환승계 = ODsay 환승횟수 + 모드전환(PT/TP의 대중교통↔택시, 옵션)
  2) '기타' 시간 성분 신설
     · ODsay info.totalTime 과 subPath sectionTime 합의 차이(환승대기 등)를
       버리지 않고 별도 성분으로 잡아 ω 가중치를 적용한다.
       (v6.1에서는 이 시간이 T_w에서 가중치 0으로 소멸했음)
  3) 대표 3개(A/B/C) 중복 제거
     · A(가중시간 최소)·B(GC 최소)·C(비용 최소)가 같은 대안일 때
       파레토 전선의 다음 후보로 대체한다.
  4) 'GC 상위 3개' 별도 산출
     · A/B/C는 파레토 전선의 극단·최적점이라 GC 상위 3개와 다르다.
       두 관점을 모두 출력·저장한다.
  5) 혼잡도 판정 불가 노선을 명시적으로 집계(9호선·신분당 등은 자료 없음)

필요 패키지:
  conda install -c conda-forge osmnx geopandas pandas matplotlib openpyxl requests
====================================================================
"""

import os
import re
import sys
import time
import math
import importlib
import subprocess
import warnings
from datetime import datetime, timedelta

# =====================================================================
# 부트스트랩 — 필요한 패키지를 자동으로 설치한다.
#   · 서드파티 import보다 반드시 위에 있어야 한다.
#   · 건너뛰려면 환경변수 MAAS_SKIP_BOOTSTRAP=1
#   · 강제 재설치/업그레이드:  python maas_v63.py --setup
# =====================================================================
REQUIRED_PACKAGES = {
    # import 이름 : pip 패키지 이름
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
    "matplotlib": "matplotlib",
    "openpyxl": "openpyxl",
    "pyproj": "pyproj",
    "geopandas": "geopandas",
    "osmnx": "osmnx>=2.0",
    "contextily": "contextily",
}


def ensure_packages(packages=None, force=False, verbose=True):
    """
    누락 패키지를 현재 인터프리터(sys.executable)에 설치한다.
    conda 환경이면 conda-forge를 먼저 시도하고, 실패 시 pip으로 넘어간다.
    (geopandas·osmnx는 Windows에서 pip 빌드가 깨지는 경우가 있어 conda가 안전)
    """
    packages = packages or REQUIRED_PACKAGES
    todo = []
    for mod, spec in packages.items():
        if force:
            todo.append(spec)
            continue
        try:
            importlib.import_module(mod)
        except Exception:
            todo.append(spec)

    if not todo:
        if verbose and force is False:
            pass
        return True

    if verbose:
        print(f"[setup] 설치 대상: {', '.join(todo)}")
        print("[setup] 처음 한 번은 몇 분 걸릴 수 있습니다...", flush=True)

    in_conda = bool(os.environ.get("CONDA_PREFIX"))
    ok = False

    if in_conda:
        conda = os.environ.get("CONDA_EXE") or "conda"
        # conda는 '>=' 표기를 그대로 받으므로 그대로 넘긴다
        cmd = [conda, "install", "-y", "-c", "conda-forge", *todo]
        try:
            subprocess.check_call(cmd)
            ok = True
        except Exception as e:
            if verbose:
                print(f"[setup] conda 설치 실패({type(e).__name__}) → pip으로 재시도")

    if not ok:
        base = [sys.executable, "-m", "pip", "install", "--upgrade"]
        for extra in ([], ["--break-system-packages"], ["--user"]):
            try:
                subprocess.check_call(base + extra + todo)
                ok = True
                break
            except subprocess.CalledProcessError:
                continue
        if not ok:
            print("[setup] pip 설치 실패")
            print("[setup] 아래를 직접 실행하세요:")
            print("    conda install -c conda-forge " + " ".join(todo))
            print("  또는")
            print(f"    {sys.executable} -m pip install " + " ".join(todo))
            return False

    importlib.invalidate_caches()

    still = [m for m in packages
             if not _importable(m)]
    if still:
        print(f"[setup] 설치했으나 아직 임포트되지 않는 패키지: {', '.join(still)}")
        print("[setup] 파이썬을 재시작한 뒤 다시 실행하세요.")
        return False
    if verbose:
        print("[setup] 완료\n")
    return True


def _importable(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


if os.environ.get("MAAS_SKIP_BOOTSTRAP", "") != "1":
    ensure_packages(force=("--setup" in sys.argv))

import requests
import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

warnings.filterwarnings("ignore")

# =====================================================================
# ★ API 키 설정
# =====================================================================
KAKAO_REST_API_KEY = ""
TMAP_API_KEY       = ""
ODSAY_API_KEY      = ""

# =====================================================================
# 색상·상수
# =====================================================================
PAL = {
    "bg": "#f7f5f2", "water": "#c4dcea", "green": "#dbe8d2", "rail": "#c9bdd6",
    "road_s": "#e4e0da", "road_m": "#d2ccc4", "road_l": "#bbb2a7",
    "taxi": "#d62728", "origin": "#2ecc71", "transfer": "#f39c12",
}
유형색 = {"Only 택시": "#E74C3C", "Only 대중교통": "#2ECC71",
        "PT(대중교통+택시)": "#3498DB", "TP(택시+대중교통)": "#9B59B6"}

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

# ★ osmnx 2.x는 settings.overpass_url 에 '/interpreter'를 스스로 붙인다.
#   (osmnx/_overpass.py: url = settings.overpass_url.rstrip("/") + "/interpreter")
#   따라서 여기에는 반드시 '베이스 URL'만 넣어야 한다.
#   v6.2 이전처럼 '.../api/interpreter'를 넣으면 '.../api/interpreter/interpreter'가
#   호출되어 전부 404가 나고, 배경 레이어가 통째로 비게 된다.
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api",
    "https://overpass-api.de/api",
    "https://overpass.private.coffee/api",
]

# ── 혼잡도 자료 ──────────────────────────────────────────────────
CONGESTION_FILE = "서울교통공사_지하철혼잡도정보_20260630.xlsx"
CONGESTION_THRESHOLD = 110.0     # 이 값 이상이면 '혼잡'

# ── 환승저항 (v6.2 신규) ─────────────────────────────────────────
TRANSFER_PENALTY = 11.24         # 분/회. SP 설문 추정치
OTHER_TIME_WEIGHT = 1.0          # '기타(환승대기 등)' 시간 가중치 ω


# ---------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------
def setup_font():
    for name in ["Malgun Gothic", "AppleGothic", "NanumGothic",
                 "Noto Sans CJK KR", "DejaVu Sans"]:
        try:
            matplotlib.font_manager.findfont(name, fallback_to_default=False)
            plt.rcParams["font.family"] = name
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


def is_network_error(e):
    return any(k in type(e).__name__ for k in NET_ERRORS)


def desktop_dirs():
    """OneDrive 동기화까지 고려한 바탕화면 후보."""
    home = os.path.expanduser("~")
    cands = [os.path.join(home, "Desktop"), os.path.join(home, "바탕 화면")]
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
            cands.insert(0, os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
        except Exception:
            pass
    import glob
    for od in glob.glob(os.path.join(home, "OneDrive*")):
        cands += [os.path.join(od, "Desktop"), os.path.join(od, "바탕 화면")]

    seen, out = set(), []
    for c in cands:
        c = os.path.normpath(c)
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            out.append(c)
    return out


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


def parse_dt(text):
    text = str(text).strip().replace("/", "-")
    for f in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(text, f)
        except ValueError:
            continue
    for f in ["%H:%M:%S", "%H:%M", "%H%M"]:
        try:
            t = datetime.strptime(text, f)
            now = datetime.now()
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            continue
    raise ValueError(f"시각 형식을 인식할 수 없습니다: '{text}'")


def 숫자입력(문구, 기본=None):
    while True:
        원문 = input(문구).strip().replace(",", "")
        if not 원문 and 기본 is not None:
            return 기본
        try:
            v = float(원문)
            if v < 0:
                raise ValueError
            return v
        except ValueError:
            print("  0 이상의 숫자를 입력하세요.")


def 정수입력(문구, 기본=None):
    while True:
        원문 = input(문구).strip().replace(",", "")
        if not 원문 and 기본 is not None:
            return 기본
        try:
            v = int(원문)
            if v < 0:
                raise ValueError
            return v
        except ValueError:
            print("  0 이상의 정수를 입력하세요.")


def 예아니오(문구, 기본=True):
    s = input(문구).strip().lower()
    if not s:
        return 기본
    return s not in ("n", "no", "아니오", "아니요", "0", "f", "false")


# =====================================================================
# 혼잡도 자료
# =====================================================================
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
        for d in desktop_dirs():
            p = os.path.join(d, CONGESTION_FILE)
            if os.path.exists(p):
                path = p
                break
    if path is None or not os.path.exists(path):
        if verbose:
            print(f"  혼잡도 파일을 찾지 못했습니다: {CONGESTION_FILE}")
            print("  → 지하철 시간을 전부 '여유'로 계산합니다.")
        return None

    xl = pd.ExcelFile(path)
    table, bins = {}, None
    for sheet in ("평일", "토요일", "일요일"):
        if sheet not in xl.sheet_names:
            continue
        df = xl.parse(sheet)
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
    base = xl.parse("평일" if "평일" in xl.sheet_names else xl.sheet_names[0])
    base["_line"] = base["호선"].astype(str).str.extract(r"(\d+)")[0]
    base["_st"] = base["역명"].map(_norm_station)
    station_order = (base.dropna(subset=["_line"])
                         .groupby(["_line", "_st"])["역번호"].min().to_dict())
    directions = (base.dropna(subset=["_line"])
                      .groupby("_line")["상하구분"]
                      .apply(lambda s: sorted(set(s.astype(str).str.strip()))).to_dict())

    if verbose and table:
        n = sum(len(v) for v in table.values())
        print(f"  혼잡도 자료 로드: {os.path.basename(path)} "
              f"({', '.join(table)}) / {n:,}개 항목")
        print(f"  임계값 {CONGESTION_THRESHOLD:.0f} 이상이면 '혼잡'으로 계산")
        print("  주의: 자료는 1~8호선만 포함. 9호선·신분당·공항철도 등은")
        print("        판정 불가이므로 전 구간 '여유'로 계산됩니다.")
    return {"table": table, "bins": bins, "path": path,
            "station_order": station_order, "directions": directions}


def _sheet_for(dt):
    wd = dt.weekday()
    return "평일" if wd <= 4 else ("토요일" if wd == 5 else "일요일")


def congestion_at(cong, line_no, station, dt, direction=None):
    """특정 역·시각의 혼잡도. 자료가 없으면 None."""
    if not cong or line_no is None:
        return None
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


# =====================================================================
# [1] 파라미터 입력
# =====================================================================
def _resolve_key(label, hardcoded, env_name):
    key = os.environ.get(env_name, "").strip() or (hardcoded or "").strip()
    if key:
        masked = key[:4] + "*" * max(0, len(key) - 8) + key[-4:] if len(key) > 8 else "****"
        src = "환경변수" if os.environ.get(env_name, "").strip() else "코드 상단 설정"
        print(f"  {label} 키: {masked}  ({src})")
        return key
    return input(f"  {label} API 키: ").strip()


def ask_params():
    print("=" * 72)
    print("  MaaS 환승 분석 v6.3 — 파라미터 입력")
    print("=" * 72)

    print("\n[API 키]")
    kakao_key = _resolve_key("KAKAO REST", KAKAO_REST_API_KEY, "KAKAO_REST_API_KEY")
    tmap_key = _resolve_key("TMAP", TMAP_API_KEY, "TMAP_API_KEY")
    odsay_key = _resolve_key("ODsay", ODSAY_API_KEY, "ODSAY_API_KEY")

    print("\n[출발/도착]")
    origin_addr = input("  출발지 주소: ").strip()
    dest_addr = input("  도착지 주소: ").strip()

    print("\n[시간]")
    dep_raw = input("  출발 시각 (YYYY-MM-DD HH:MM): ").strip()
    dep_dt = parse_dt(dep_raw)
    wd = ["월", "화", "수", "목", "금", "토", "일"][dep_dt.weekday()]
    print(f"    → {dep_dt.strftime('%Y-%m-%d %H:%M')} ({wd}요일, "
          f"혼잡도 '{_sheet_for(dep_dt)}' 시트 적용)")

    print("\n[수단별 시간 가중치]")
    print("  T_w = α·버스 + β·지하철(여유) + β_c·지하철(혼잡)")
    print("        + γ·도보 + δ·택시 + ω·기타 + τ·환승계")
    alpha_bus = 숫자입력("  버스 가중치 α (기본 1.18): ", 1.18)
    beta_sub = 숫자입력("  지하철(여유) 가중치 β (기본 1.0): ", 1.0)
    beta_sub_c = 숫자입력("  지하철(혼잡) 가중치 β_c (기본 1.55): ", 1.55)
    gamma_walk = 숫자입력("  도보 가중치 γ (기본 2.35): ", 2.35)
    delta_taxi = 숫자입력("  택시 가중치 δ (기본 0.88): ", 0.88)

    print("\n  [기타 시간] ODsay 총소요시간에는 있으나 수단별 구간시간 합에는")
    print("  잡히지 않는 잔여시간(환승 대기 등). v6.1에서는 가중치 0으로 소멸했음.")
    omega_other = 숫자입력(f"  기타 가중치 ω (기본 {OTHER_TIME_WEIGHT}): ", OTHER_TIME_WEIGHT)

    print("\n[환승저항]")
    tau_tr = 숫자입력(f"  환승저항 τ (분/회, 기본 {TRANSFER_PENALTY}): ", TRANSFER_PENALTY)
    count_mc = 예아니오("  PT/TP의 대중교통↔택시 전환도 환승 1회로 계산? (Y/n): ", True)

    vot = 숫자입력("\n  전체 VOT (원/분, 기본 400): ", 400.0)
    print(f"    → 환승 1회 = {tau_tr:.2f}분 = {tau_tr * vot:,.0f}원 상당"
          f" / 모드전환 {'포함' if count_mc else '제외'}")

    print(f"\n[혼잡 판정] 혼잡도 {CONGESTION_THRESHOLD:.0f} 이상이면 '혼잡'으로 계산")
    thr = 숫자입력(f"  임계값 변경 (엔터 시 {CONGESTION_THRESHOLD:.0f}): ",
                   CONGESTION_THRESHOLD)

    print("\n[대안 생성]")
    max_cand = 정수입력("  경로당 최대 환승후보 수 (기본 20): ", 20)

    print("\n[지도 출력] 파레토 해 전부에 대해 경로도를 생성합니다.")
    print("  파레토가 크면 시간이 오래 걸리므로 상한을 둘 수 있습니다(가중시간 오름차순).")
    max_maps = 정수입력("  최대 지도 수 (0=제한 없음, 기본 0): ", 0)

    print("\n  [배경] auto = 타일(contextily) 우선, 실패 시 OSM 벡터(osmnx)")
    print("         tile / vector / none 으로 강제할 수도 있습니다.")
    bg_mode = (input("  배경 모드 (auto/tile/vector/none, 기본 auto): ").strip().lower()
               or "auto")
    if bg_mode not in ("auto", "tile", "vector", "none"):
        print(f"    '{bg_mode}'는 알 수 없는 값이라 auto로 진행합니다.")
        bg_mode = "auto"
    bg_detail = False
    if bg_mode in ("auto", "vector"):
        bg_detail = 예아니오("  벡터 배경에 이면도로까지 포함? (느립니다) (y/N): ", False)

    print("=" * 72)
    return dict(
        kakao_key=kakao_key, tmap_key=tmap_key, odsay_key=odsay_key,
        origin_addr=origin_addr, dest_addr=dest_addr,
        dep_dt=dep_dt,
        alpha_bus=alpha_bus, beta_sub=beta_sub, beta_sub_c=beta_sub_c,
        gamma_walk=gamma_walk, delta_taxi=delta_taxi,
        omega_other=omega_other, tau_tr=tau_tr, count_mc=count_mc,
        vot=vot, threshold=thr, max_cand=max_cand, max_maps=max_maps,
        bg_mode=bg_mode, bg_detail=bg_detail,
    )


# =====================================================================
# Kakao 지오코딩
# =====================================================================
def clean_address(addr):
    addr = re.sub(r"\([^)]*\)", "", str(addr).strip())
    return re.sub(r"\s+", " ", addr).strip()


def geocode(kakao_key, label, raw_address):
    resp = requests.get(
        "https://dapi.kakao.com/v2/local/search/address.json",
        headers={"Authorization": f"KakaoAK {kakao_key}"},
        params={"query": clean_address(raw_address), "analyze_type": "similar"},
        timeout=20)
    if resp.status_code != 200:
        raise ValueError(f"[{label}] 지오코딩 실패: HTTP {resp.status_code}")
    docs = resp.json().get("documents", [])
    if not docs:
        raise ValueError(f"[{label}] 주소 검색결과 없음: {raw_address}")
    first = docs[0]
    matched = (first.get("road_address") or {}).get("address_name") \
              or (first.get("address") or {}).get("address_name")
    lat, lon = float(first["y"]), float(first["x"])
    print(f"  [{label}] OK {matched}  ({lat:.6f}, {lon:.6f})")
    return {"name": matched or raw_address, "lat": lat, "lon": lon}


# =====================================================================
# ODsay
# =====================================================================
ODSAY_ERRORS = {}      # {오류문구: 발생횟수}  — 한도 초과 등을 마지막에 요약


def _odsay_error(js):
    err = js.get("error") if isinstance(js, dict) else None
    if isinstance(err, list) and err:
        err = err[0]
    if isinstance(err, dict):
        return f"code={err.get('code')} {err.get('msg')}"
    return None


def _note_odsay_error(where, msg):
    key = f"{where}: {msg}"
    ODSAY_ERRORS[key] = ODSAY_ERRORS.get(key, 0) + 1


def odsay_paths_raw(odsay_key, s_lat, s_lon, e_lat, e_lon):
    try:
        res = requests.get(
            "https://api.odsay.com/v1/api/searchPubTransPathT",
            params={"apiKey": odsay_key, "SX": str(s_lon), "SY": str(s_lat),
                    "EX": str(e_lon), "EY": str(e_lat), "SearchPathType": 0},
            headers={"Referer": "http://localhost"}, timeout=10)
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
    lanes = []
    try:
        res = requests.get(
            "https://api.odsay.com/v1/api/loadLane",
            params={"apiKey": odsay_key, "mapObject": f"0:0@{map_obj}"},
            headers={"Referer": "http://localhost"}, timeout=15)
        js = res.json()
        err = _odsay_error(js)
        if err:
            _note_odsay_error("loadLane", err)
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
    except Exception as e:
        _note_odsay_error("loadLane", type(e).__name__)
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
    n_call = 0
    for _id in ids:
        g = geoms.get(_id)
        if not g:
            continue
        map_obj = g.get("mapObj")
        segs = [s for s in g.get("segments", []) if s["대분류"] != "도보"]
        if not segs:
            continue

        before = len(_LANE_CACHE)
        lanes = odsay_lanes(odsay_key, map_obj) if map_obj else []
        if len(_LANE_CACHE) > before:
            n_call += 1

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
        total = sum(stats.values())
        print(f"  형상 수집: loadLane {n_call}회 호출 / 구간 {total}개")
        for k in ("lane", "lane(위치매칭)", "정류장직선", "없음"):
            if stats.get(k):
                print(f"    {k:<14} {stats[k]:>3}개"
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


def tmap_taxi(tmap_key, s_lat, s_lon, e_lat, e_lon, depart_iso):
    """※ 반환 시간은 '주행시간'이며 호출·배차 대기시간은 포함하지 않는다."""
    try:
        r = requests.post(
            "https://apis.openapi.sk.com/tmap/routes/prediction?version=1&format=json",
            headers={"appKey": tmap_key, "Content-Type": "application/json"},
            json={"routesInfo": {"departure": {"lon": str(s_lon), "lat": str(s_lat)},
                                 "destination": {"lon": str(e_lon), "lat": str(e_lat)},
                                 "predictionType": "departure",
                                 "predictionTime": depart_iso,
                                 "searchOption": "0"}}, timeout=8)
        if r.status_code == 200:
            return _parse_tmap(r.json())
    except Exception:
        pass
    try:
        r = requests.post(
            "https://apis.openapi.sk.com/tmap/routes?version=1&format=json",
            headers={"appKey": tmap_key},
            data={"startX": str(s_lon), "startY": str(s_lat),
                  "endX": str(e_lon), "endY": str(e_lat),
                  "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO"}, timeout=8)
        if r.status_code == 200:
            return _parse_tmap(r.json())
    except Exception:
        pass
    return None, None, []


# =====================================================================
# 대안 생성 (PT / TP)
# =====================================================================
def collect_alternatives(P, origin, dest, cong):
    rows, geoms = [], {}
    rid = 0

    def nid():
        nonlocal rid
        rid += 1
        return f"R{rid:04d}"

    dep_iso = P["dep_dt"].strftime("%Y-%m-%dT%H:%M:%S+0900")
    thr = P["threshold"]

    # ── Only 택시 ───────────────────────────────────────────────
    print("\n  Only 택시 직행")
    tt, tf, tcoords = tmap_taxi(P["tmap_key"], origin["lat"], origin["lon"],
                                dest["lat"], dest["lon"], dep_iso)
    if tt is not None:
        _id = nid()
        rows.append(dict(id=_id, 유형="Only 택시", 경로순위=0, 환승지점="-", 모드전환=0,
                         버스=0.0, 지하철여유=0.0, 지하철혼잡=0.0, 도보=0.0,
                         택시=round(tt, 1), 기타=0.0,
                         환승횟수=0, 비용=tf or 0,
                         실소요시간=round(tt, 1), 구간요약="택시 직행",
                         판정불가구간=0))
        geoms[_id] = {"segments": [], "taxi": tcoords, "transfer": None}
        print(f"    OK {tt:.1f}분 / {tf:,}원")
    else:
        print("    실패")

    # ── Only 대중교통 (최속 3개) ────────────────────────────────
    print("\n  대중교통 최속 경로 3개 조회")
    routes = top3_transit_routes(P["odsay_key"], origin, dest)
    print(f"    {len(routes)}개 수신")

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
        print(f"    [{rank}] Only대중교통: {agg['총시간']:.0f}분 "
              f"(성분합 {agg['성분합']:.0f} + 기타 {agg['기타']:.0f}) / "
              f"{agg['비용']:,}원 / 환승{agg['환승횟수']}회")
        print(f"        지하철 혼잡{agg['지하철혼잡']:.0f}분·여유{agg['지하철여유']:.0f}분 "
              f"(판정 {agg['판정역수']}역 중 혼잡 {agg['혼잡역수']}역, "
              f"판정불가 {agg['판정불가구간']}구간)")

        # 환승 후보역 수집
        stations = [s for seg in segs if seg["대분류"] in ("버스", "지하철")
                    for s in zip(seg["stop_names"], seg["stops"])]
        uniq, seen = [], set()
        for name, xy in stations:
            if name in seen or not name:
                continue
            seen.add(name)
            uniq.append((name, xy))
        cand = uniq[:P["max_cand"]]
        print(f"        환승후보 {len(cand)}개 (PT/TP 각각)")

        # PT: 출발지 -대중교통-> 후보역 -택시-> 목적지
        for name, (clon, clat) in cand:
            legs = odsay_paths_raw(P["odsay_key"], origin["lat"], origin["lon"], clat, clon)
            if not legs:
                continue
            leg = sorted(legs, key=lambda p: p["info"]["totalTime"])[0]
            lsegs, lagg = transit_segments(P["odsay_key"], leg, P["dep_dt"], cong, thr)
            t_min = lagg["총시간"]
            taxi_iso = (P["dep_dt"] + timedelta(minutes=t_min)).strftime("%Y-%m-%dT%H:%M:%S+0900")
            stt, stf, st_coords = tmap_taxi(P["tmap_key"], clat, clon,
                                            dest["lat"], dest["lon"], taxi_iso)
            if stt is None:
                continue
            _id = nid()
            lsum = " -> ".join(f"{s['수단']}({s['시간(분)']:.0f}분)"
                               for s in lsegs if s["대분류"] != "도보")
            rows.append(dict(id=_id, 유형="PT(대중교통+택시)", 경로순위=rank, 환승지점=name,
                             모드전환=1,
                             버스=lagg["버스"], 지하철여유=lagg["지하철여유"],
                             지하철혼잡=lagg["지하철혼잡"], 도보=lagg["도보"],
                             택시=round(stt, 1), 기타=lagg["기타"],
                             환승횟수=lagg["환승횟수"],
                             비용=(lagg["비용"] or 0) + (stf or 0),
                             실소요시간=round(t_min + stt, 1),
                             구간요약=f"{lsum} -> 택시({stt:.0f}분)",
                             판정불가구간=lagg["판정불가구간"]))
            geoms[_id] = {"segments": lsegs, "taxi": st_coords,
                          "transfer": (clon, clat), "mapObj": lagg["mapObj"]}
            time.sleep(0.1)

        # TP: 출발지 -택시-> 후보역 -대중교통-> 목적지
        for name, (clon, clat) in cand:
            stt2, stf2, st2 = tmap_taxi(P["tmap_key"], origin["lat"], origin["lon"],
                                        clat, clon, dep_iso)
            if stt2 is None:
                continue
            legs2 = odsay_paths_raw(P["odsay_key"], clat, clon, dest["lat"], dest["lon"])
            if not legs2:
                continue
            leg2 = sorted(legs2, key=lambda p: p["info"]["totalTime"])[0]
            # 택시로 이동한 뒤부터 대중교통을 타므로 시작시각을 뒤로 민다
            lsegs2, lagg2 = transit_segments(
                P["odsay_key"], leg2, P["dep_dt"] + timedelta(minutes=stt2), cong, thr)
            _id = nid()
            lsum2 = " -> ".join(f"{s['수단']}({s['시간(분)']:.0f}분)"
                                for s in lsegs2 if s["대분류"] != "도보")
            rows.append(dict(id=_id, 유형="TP(택시+대중교통)", 경로순위=rank, 환승지점=name,
                             모드전환=1,
                             버스=lagg2["버스"], 지하철여유=lagg2["지하철여유"],
                             지하철혼잡=lagg2["지하철혼잡"], 도보=lagg2["도보"],
                             택시=round(stt2, 1), 기타=lagg2["기타"],
                             환승횟수=lagg2["환승횟수"],
                             비용=(stf2 or 0) + (lagg2["비용"] or 0),
                             실소요시간=round(stt2 + lagg2["총시간"], 1),
                             구간요약=f"택시({stt2:.0f}분) -> {lsum2}",
                             판정불가구간=lagg2["판정불가구간"]))
            geoms[_id] = {"segments": lsegs2, "taxi": st2,
                          "transfer": (clon, clat), "mapObj": lagg2["mapObj"]}
            time.sleep(0.1)

    return rows, geoms


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





# =====================================================================
# 지도
# =====================================================================
def square_extent(pts, min_km):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    km_lat, km_lon = 111.0, 111.0 * math.cos(math.radians(cy))
    half = max(min_km, (max(xs) - min(xs)) * km_lon * 1.2,
               (max(ys) - min(ys)) * km_lat * 1.2) / 2
    return (cx - half / km_lon, cy - half / km_lat,
            cx + half / km_lon, cy + half / km_lat), half * 2


def check_map_backend():
    """배경 렌더링에 필요한 패키지 상태를 한 번에 진단해 출력한다."""
    st = {}
    for mod in ("osmnx", "geopandas", "contextily"):
        try:
            m = __import__(mod)
            st[mod] = getattr(m, "__version__", "?")
        except Exception as e:
            st[mod] = f"없음 ({type(e).__name__})"
    print("  배경 패키지 상태")
    for k, v in st.items():
        ok = not str(v).startswith("없음")
        print(f"    {'OK ' if ok else '-- '} {k:<11} {v}")
    if str(st["osmnx"])[0].isdigit() and int(str(st["osmnx"]).split(".")[0]) < 2:
        print("    ★ osmnx 2.x가 필요합니다: pip install -U osmnx")
    if str(st["contextily"]).startswith("없음"):
        print("    (타일 배경을 쓰려면: pip install contextily)")
    return st


def pick_overpass_mirror(verbose=True):
    """
    Overpass 미러를 고른다.
    osmnx 버전에 따라 settings.overpass_url이 베이스 URL을 기대할 수도,
    전체 엔드포인트를 기대할 수도 있으므로 두 형태를 모두 실제로 시험한다.
    아주 작은 bbox로 osmnx를 직접 호출해 보는 것이 유일하게 확실한 검증이다.
    """
    import osmnx as ox
    tiny = (126.9770, 37.5650, 126.9790, 37.5670)   # 서울시청 인근 극소 박스

    for base in OVERPASS_MIRRORS:
        host = base.split("/")[2]

        # 1단계: 서버가 살아 있는지 직접 확인
        try:
            r = requests.post(base.rstrip("/") + "/interpreter",
                              data={"data": "[out:json][timeout:10];out count;"},
                              timeout=30)
            if r.status_code != 200:
                if verbose:
                    print(f"    {host}: HTTP {r.status_code}")
                continue
        except Exception as e:
            if verbose:
                print(f"    {host}: {type(e).__name__}")
            continue

        # 2단계: osmnx가 URL을 올바로 조립하는 형태를 찾는다
        for cand in (base.rstrip("/"), base.rstrip("/") + "/interpreter"):
            ox.settings.overpass_url = cand
            try:
                ox.features.features_from_bbox(bbox=tiny, tags={"building": True})
                if verbose:
                    print(f"    Overpass OK: {host}  (overpass_url={cand})")
                return cand
            except Exception as e:
                # 결과 0건은 통신 성공을 뜻하므로 합격 처리
                if type(e).__name__ in ("InsufficientResponseError", "EmptyGraphError"):
                    if verbose:
                        print(f"    Overpass OK: {host}  (overpass_url={cand}, 결과 0건)")
                    return cand
                if verbose:
                    print(f"    {host}: osmnx 경유 실패 {type(e).__name__} "
                          f"(url={cand})")
    return None


def load_layers(bbox, cache_dir, detail=False):
    """
    OSM 배경 레이어를 받아온다. 실패는 전부 화면에 남긴다(예전엔 조용히 삼켰음).
    detail=True면 이면도로(tertiary/residential)까지 받는다 — 매우 느리다.
    반환: {레이어명: GeoDataFrame}
    """
    try:
        import osmnx as ox
    except ImportError:
        print("  osmnx 미설치 → 벡터 배경 없음 (pip install osmnx)")
        return {}
    try:
        import geopandas as gpd
    except ImportError:
        print("  geopandas 미설치 → 벡터 배경 없음")
        return {}
    if int(str(ox.__version__).split(".")[0]) < 2:
        print(f"  OSMnx {ox.__version__} (2.x 필요) → 벡터 배경 없음")
        return {}

    os.makedirs(cache_dir, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = cache_dir
    ox.settings.overpass_rate_limit = True
    ox.settings.requests_timeout = 600        # 오래 걸려도 되므로 넉넉히

    print("  Overpass 미러 확인")
    if pick_overpass_mirror() is None:
        print("  ★ 모든 Overpass 미러 실패 → 벡터 배경 없음")
        return {}

    layers = {}
    tag = f"{bbox[0]:.3f}_{bbox[1]:.3f}_{bbox[2]:.3f}_{bbox[3]:.3f}"

    # ── 도로망 (그래프) ──────────────────────────────────────────
    road_specs = [
        ("road_l", '["highway"~"motorway|trunk"]', "고속·간선"),
        ("road_m", '["highway"~"primary|secondary"]', "주간선·보조간선"),
    ]
    if detail:
        road_specs.append(
            ("road_s", '["highway"~"tertiary|residential|unclassified"]', "이면도로"))

    for key, cfilter, desc in road_specs:
        cache = os.path.join(cache_dir, f"bg_{key}_{tag}.graphml")
        try:
            if os.path.exists(cache):
                G = ox.io.load_graphml(cache)
                src = "캐시"
            else:
                print(f"    {desc} 내려받는 중...", flush=True)
                G = ox.graph_from_bbox(bbox=bbox, custom_filter=cfilter,
                                       retain_all=True, truncate_by_edge=True,
                                       simplify=True)
                ox.io.save_graphml(G, cache)
                src = "신규"
            gdf = ox.convert.graph_to_gdfs(G, nodes=False, edges=True)
            layers[key] = gdf
            print(f"    OK {key:<7} {len(gdf):>7,}개 ({desc}, {src})")
        except Exception as e:
            print(f"    -- {key:<7} 실패: {type(e).__name__} — {e}")
            if is_network_error(e):
                print("       네트워크 오류이므로 이후 레이어를 건너뜁니다.")
                break

    # ── 면·선 피처 ──────────────────────────────────────────────
    feat_specs = [
        ("water", {"natural": "water",
                   "waterway": ["river", "riverbank", "stream"]}, "수계"),
        ("green", {"leisure": "park",
                   "landuse": ["forest", "grass", "meadow"]}, "녹지"),
        ("rail",  {"railway": ["rail", "subway", "light_rail"]}, "철도"),
    ]
    for key, tags, desc in feat_specs:
        cache = os.path.join(cache_dir, f"bg_{key}_{tag}.geojson")
        try:
            if os.path.exists(cache):
                gdf = gpd.read_file(cache)
                src = "캐시"
            else:
                print(f"    {desc} 내려받는 중...", flush=True)
                gdf = ox.features.features_from_bbox(bbox=bbox, tags=tags)
                # 배경에는 형상만 필요하다. 속성 컬럼에 리스트가 섞여 있으면
                # GeoJSON 저장이 실패하므로 geometry만 남긴다.
                gdf = gdf[["geometry"]].reset_index(drop=True)
                if len(gdf):
                    gdf.to_file(cache, driver="GeoJSON")
                src = "신규"
            if len(gdf):
                layers[key] = gdf
                print(f"    OK {key:<7} {len(gdf):>7,}개 ({desc}, {src})")
            else:
                print(f"    -- {key:<7} 결과 0건 ({desc})")
        except Exception as e:
            name = type(e).__name__
            if name == "InsufficientResponseError":
                print(f"    -- {key:<7} 해당 범위에 {desc} 없음")
            else:
                print(f"    -- {key:<7} 실패: {name} — {e}")

    if not layers:
        print("  ★ 배경 레이어를 하나도 받지 못했습니다.")
    return layers


TILE_PROVIDERS = [
    "CartoDB.PositronNoLabels",
    "CartoDB.Positron",
    "Esri.WorldGrayCanvas",
    "OpenStreetMap.Mapnik",
]


def add_tile_basemap(ax, providers=None, zoom="auto"):
    """
    contextily로 래스터 타일 배경을 깐다.
    제공자 여러 곳을 순서대로 시도한다 — 한 CDN이 막혀도 다른 데서 받아온다.
    ※ 반드시 ax의 xlim/ylim을 먼저 설정한 뒤 호출할 것.
    반환: (성공여부, 메시지)
    """
    try:
        import contextily as ctx
    except ImportError:
        return False, "contextily 미설치 (conda install -c conda-forge contextily)"

    tried = []
    for name in (providers or TILE_PROVIDERS):
        try:
            src = ctx.providers
            for part in name.split("."):
                src = getattr(src, part)
        except AttributeError:
            tried.append(f"{name}: 제공자 없음")
            continue
        try:
            kw = dict(crs="EPSG:4326", source=src, attribution=False, zorder=0)
            if zoom != "auto":
                kw["zoom"] = zoom
            ctx.add_basemap(ax, **kw)
            return True, name
        except Exception as e:
            msg = str(e)
            tried.append(f"{name}: {type(e).__name__} {msg[:70]}")
    return False, " | ".join(tried)


def draw_scalebar(ax, xlim, ylim, lat):
    km_lon = 111.0 * math.cos(math.radians(lat))
    width_km = (xlim[1] - xlim[0]) * km_lon
    bar_km = next((c for c in [1, 2, 5, 10, 20, 50] if c >= width_km / 5), 50)
    bar_deg = bar_km / km_lon
    x0 = xlim[1] - (xlim[1] - xlim[0]) * 0.05 - bar_deg
    y0 = ylim[0] + (ylim[1] - ylim[0]) * 0.045
    ax.plot([x0, x0 + bar_deg], [y0, y0], color="#333", lw=3.2,
            zorder=16, solid_capstyle="butt")
    ax.text(x0 + bar_deg / 2, y0 + (ylim[1] - ylim[0]) * 0.009, f"{bar_km} km",
            ha="center", va="bottom", fontsize=9.5, color="#333", zorder=16)


def draw_route_map(layers, tag, row, geom, origin, dest, out_dir,
                   best_rank=None, knee_rank=None, bg_mode="auto", diag=None):
    """
    bg_mode: 'auto'(타일 우선, 실패 시 벡터) / 'tile' / 'vector' / 'none'
    diag: 실패 사유를 모아둘 dict (호출측에서 한 번만 출력)
    """
    segments, taxi = geom.get("segments", []), geom.get("taxi", [])
    if not segments and not taxi:
        return None
    if diag is None:
        diag = {}

    pts = [(origin["lon"], origin["lat"]), (dest["lon"], dest["lat"])] + list(taxi)
    for sg in segments:
        pts += sg["coords"] + sg["stops"]
    (w, s, e, n), span_km = square_extent(pts, 30)
    lat_c = (s + n) / 2

    fig, ax = plt.subplots(figsize=(12, 12))
    ax.set_facecolor(PAL["bg"])

    # ★ 배경보다 먼저 범위를 확정한다.
    #   contextily는 '현재 축 범위'를 보고 타일을 받아오므로 순서가 중요하다.
    ax.set_xlim(w, e)
    ax.set_ylim(s, n)
    ax.set_aspect(1.0 / math.cos(math.radians(lat_c)))

    # ── 배경 ────────────────────────────────────────────────────
    tile_ok = False
    if bg_mode in ("auto", "tile"):
        tile_ok, msg = add_tile_basemap(ax)
        if not tile_ok:
            diag.setdefault("tile", msg)

    used_vector = False
    if bg_mode == "vector" or (bg_mode == "auto" and not tile_ok):
        def clip(key, **kw):
            nonlocal used_vector
            gdf = layers.get(key)
            if gdf is None or not len(gdf):
                return
            try:
                sub = gdf.cx[w:e, s:n]
                if len(sub):
                    sub.plot(ax=ax, **kw)
                    used_vector = True
            except Exception as ex:
                diag.setdefault(f"clip:{key}", f"{type(ex).__name__}: {ex}")

        clip("green", color=PAL["green"], edgecolor="none", alpha=0.85, zorder=1)
        clip("water", color=PAL["water"], edgecolor="none", alpha=0.9, zorder=2)
        clip("rail", color=PAL["rail"], linewidth=0.8, alpha=0.8, zorder=3)
        clip("road_s", color=PAL["road_s"], linewidth=0.4, zorder=3)
        clip("road_m", color=PAL["road_m"], linewidth=0.9, zorder=4)
        clip("road_l", color=PAL["road_l"], linewidth=1.7, zorder=5)
        # geopandas가 범위를 다시 잡을 수 있으므로 복원
        ax.set_xlim(w, e)
        ax.set_ylim(s, n)

    if bg_mode != "none" and not tile_ok and not used_vector:
        diag.setdefault("배경없음", "타일·벡터 모두 실패")

    approx_used = False
    for sg in segments:
        cs = sg["coords"]
        if sg["대분류"] == "도보":
            continue
        if len(cs) < 2:
            # 형상이 아예 없는 구간. 조용히 빠지면 '택시만 그려진' 것처럼 보이므로
            # 정류장 점이라도 찍어 존재를 남긴다.
            if sg.get("stops"):
                sx = [p[0] for p in sg["stops"]]
                sy = [p[1] for p in sg["stops"]]
                ax.scatter(sx, sy, s=26, c=sg["색"], marker="x",
                           linewidths=1.4, zorder=9)
                approx_used = True
            continue

        approx = sg.get("좌표원천") == "정류장직선"
        approx_used = approx_used or approx
        xs, ys = [p[0] for p in cs], [p[1] for p in cs]
        ax.plot(xs, ys, color="white", lw=6.5, alpha=0.9, zorder=8, solid_capstyle="round")
        busy = sg.get("혼잡시간", 0) or 0
        ax.plot(xs, ys, color=sg["색"], lw=3.8, alpha=0.97, zorder=9,
                solid_capstyle="round",
                linestyle=(0, (6, 3)) if approx else "-")   # 근사 형상은 파선
        if busy > 0:
            ax.plot(xs, ys, color="#c0392b", lw=1.6, alpha=0.9, zorder=10,
                    linestyle=(0, (2, 2)))
        mx, my = cs[len(cs) // 2]
        label = sg["수단"] + (f"\n혼잡 {busy:.0f}분" if busy > 0 else "")
        if approx:
            label += "\n(형상 근사)"
        ax.annotate(label, (mx, my), fontsize=9, fontweight="bold", color=sg["색"],
                    zorder=14, ha="center", xytext=(0, 8), textcoords="offset points",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white",
                              ec=sg["색"], lw=1.0, alpha=0.9))

    if taxi:
        xs, ys = [p[0] for p in taxi], [p[1] for p in taxi]
        ax.plot(xs, ys, color="white", lw=6.5, alpha=0.9, zorder=8, solid_capstyle="round")
        ax.plot(xs, ys, color=PAL["taxi"], lw=3.8, alpha=0.97, zorder=9, solid_capstyle="round")

    ax.scatter(origin["lon"], origin["lat"], s=240, c=PAL["origin"], marker="s",
               edgecolors="white", linewidths=2.2, zorder=12)
    ax.scatter(dest["lon"], dest["lat"], s=430, c="#1a1a1a", marker="*",
               edgecolors="white", linewidths=1.6, zorder=12)

    if geom.get("transfer"):
        tx, ty = geom["transfer"]
        ax.scatter(tx, ty, s=320, c=PAL["transfer"], marker="o",
                   edgecolors="#c0392b", linewidths=2.8, zorder=13)
        ax.annotate(f"  {row['환승지점']}", (tx, ty), fontsize=11.5, fontweight="bold",
                    color="#a93226", zorder=15,
                    bbox=dict(boxstyle="round,pad=0.35", fc="white",
                              ec="#e67e22", alpha=0.93))

    handles = [
        Line2D([0], [0], marker="s", color="w", markerfacecolor=PAL["origin"],
               markersize=11, label="출발지"),
        Line2D([0], [0], marker="*", color="w", markerfacecolor="#1a1a1a",
               markersize=16, label="도착지"),
    ]
    used = set()
    for sg in segments:
        if sg["대분류"] != "도보" and sg["수단"] not in used:
            used.add(sg["수단"])
            handles.append(Line2D([0], [0], color=sg["색"], lw=3.6, label=sg["수단"]))
    if any((sg.get("혼잡시간", 0) or 0) > 0 for sg in segments):
        handles.append(Line2D([0], [0], color="#c0392b", lw=1.8,
                              linestyle=(0, (2, 2)), label="혼잡 구간"))
    if approx_used:
        handles.append(Line2D([0], [0], color="#777777", lw=2.6,
                              linestyle=(0, (6, 3)), label="형상 근사(정류장 연결)"))
    if taxi:
        handles.append(Line2D([0], [0], color=PAL["taxi"], lw=3.6, label="택시(TMAP 주행경로)"))
    ax.legend(handles=handles, fontsize=10, loc="upper left", framealpha=0.94,
              facecolor="white", edgecolor="#cccccc")

    RANK_COLOR = {1: "#16A085", 2: "#2E86C1", 3: "#8E44AD"}
    mark = f"  ◆ GC {best_rank}위" if best_rank else ""
    if knee_rank:
        mark += f"  ▼ Knee {knee_rank}위"
    hi = RANK_COLOR.get(best_rank) or ("#E67E22" if knee_rank else None)
    ax.set_title(
        f"[{tag}]{mark} {row['유형']} - {row['환승지점']}\n"
        f"가중시간 {row['가중치_적용_시간']:.1f}분 (실 {row['실소요시간']:.1f}분) / "
        f"{row['비용']:,.0f}원 / GC {row['GC']:,.0f}원\n"
        f"버스 {row['버스']:.0f} · 지하철 여유 {row['지하철여유']:.0f}/혼잡 {row['지하철혼잡']:.0f} · "
        f"도보 {row['도보']:.0f} · 택시 {row['택시']:.0f} · 기타 {row['기타']:.0f} (분)\n"
        f"환승 {int(row['환승계'])}회 → 저항 {row['환승저항(분)']:.1f}분",
        fontsize=11, pad=14,
        color=hi if hi else "black")

    ax.set_xlim(w, e)
    ax.set_ylim(s, n)
    ax.set_aspect(1.0 / math.cos(math.radians(lat_c)))
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_edgecolor(hi if hi else "#bbbbbb")
        sp.set_linewidth(2.6 if hi else 0.8)
    draw_scalebar(ax, (w, e), (s, n), lat_c)

    credit = None
    if tile_ok:
        credit = "(c) OpenStreetMap contributors  (c) CARTO"
    elif used_vector:
        credit = "(c) OpenStreetMap contributors"
    if credit:
        ax.text(0.995, 0.006, credit, transform=ax.transAxes,
                ha="right", va="bottom", fontsize=7.5, color="#666", zorder=17)

    safe = re.sub(r'[\\/*?:"<>|]', "_", f"{tag}_{row['환승지점']}")
    suffix = (f"_GC{best_rank}" if best_rank else "") + \
             (f"_KNEE{knee_rank}" if knee_rank else "")
    fname = f"route_{safe}{suffix}.png"
    fig.savefig(os.path.join(out_dir, fname), dpi=200, bbox_inches="tight",
                pad_inches=0.4, facecolor="white")
    plt.close(fig)
    print(f"  지도 저장: {fname}")
    return fname


def _overlap_ratio(a, b, tol_m=50.0):
    """
    폴리라인 a의 점들 중 폴리라인 b의 점에서 tol_m 이내인 비율.
    1.0에 가까우면 두 선이 사실상 같은 경로다.
    ※ 선분이 아니라 꼭짓점까지의 거리로 근사한다(형상이 조밀하면 충분).
    """
    if not a or not b:
        return 0.0
    A = np.asarray(a, float)
    B = np.asarray(b, float)
    if A.ndim != 2 or B.ndim != 2 or len(A) == 0 or len(B) == 0:
        return 0.0
    lat0 = float(np.mean(A[:, 1]))
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_540.0
    if len(A) > 300:
        A = A[:: max(1, len(A) // 300)]
    if len(B) > 2000:
        B = B[:: max(1, len(B) // 2000)]
    d = np.hypot(A[:, 0, None] * kx - B[None, :, 0] * kx,
                 A[:, 1, None] * ky - B[None, :, 1] * ky).min(axis=1)
    return float((d <= tol_m).mean())


def _polyline_len_km(coords):
    if not coords or len(coords) < 2:
        return 0.0
    A = np.asarray(coords, float)
    lat0 = float(np.mean(A[:, 1]))
    dx = np.diff(A[:, 0]) * 111.320 * math.cos(math.radians(lat0))
    dy = np.diff(A[:, 1]) * 110.540
    return float(np.hypot(dx, dy).sum())


def _dist_m(a, b):
    """두 좌표 사이 거리(m). 위경도 국소 평면 근사."""
    if a is None or b is None:
        return None
    lat0 = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * 111_320.0 * math.cos(lat0)
    dy = (a[1] - b[1]) * 110_540.0
    return float(math.hypot(dx, dy))


def _transit_ends(segments):
    """대중교통 구간 전체의 첫 좌표와 마지막 좌표."""
    cs = [s["coords"] for s in segments
          if s["대분류"] != "도보" and len(s.get("coords") or []) >= 2]
    if not cs:
        return None, None
    return cs[0][0], cs[-1][-1]


def check_transfer_seam(row, geom, tol_m=400.0):
    """
    환승점 이음새 검사.

    PT는 '대중교통으로 환승역까지 → 거기서 택시'이므로
    대중교통 구간의 끝점과 택시의 시작점이 같은 자리여야 한다.
    TP는 그 반대다. ODsay가 후보 좌표와 다른 지점으로 경로를 끊어주면
    지도에서 두 선이 이어지지 않은 채 그려지는데, 그동안 이를
    확인하는 절차가 없었다.

    반환: (간격 m, 설명) 또는 (None, 사유)
    """
    taxi = geom.get("taxi") or []
    segs = geom.get("segments") or []
    if not taxi or not segs:
        return None, "단일수단(이음새 없음)"
    first, last = _transit_ends(segs)
    if first is None:
        return None, "대중교통 형상 없음"

    typ = str(row.get("유형", ""))
    if typ.startswith("PT"):
        return _dist_m(last, taxi[0]), "대중교통 끝 → 택시 시작"
    if typ.startswith("TP"):
        return _dist_m(taxi[-1], first), "택시 끝 → 대중교통 시작"
    return None, "복합수단 아님"


def geometry_report(targets, geoms, limit=None):
    """
    지도에 그려질 선들이 정말 API가 준 형상인지 감사한다.

    확인 항목
      · 좌표원천: lane(실제 노선형상) / 정류장직선(근사) / 없음(미표시)
      · 점 개수와 폴리라인 길이
      · 택시 폴리라인과의 중복도 — 버스·지하철이 택시와 같은 선으로
        그려지는 이상 현상을 수치로 잡아낸다
    """
    print("\n  === 경로 기하 감사 ===")
    print("  라벨  구간            원천           점수    길이km  택시중복")
    print("  " + "-" * 64)

    warn_same, warn_none, warn_fallback, warn_seam = [], [], [], []
    rows = list(targets.iterrows())
    if limit:
        rows = rows[:limit]

    for _, r in rows:
        g = geoms.get(r["id"], {})
        taxi = g.get("taxi", [])
        for sg in g.get("segments", []):
            if sg["대분류"] == "도보":
                continue
            src = sg.get("좌표원천", "?")
            cs = sg.get("coords", [])
            ov = _overlap_ratio(cs, taxi) if taxi else 0.0
            flag = ""
            if src == "없음":
                flag = "  <- 지도에 안 그려짐"
                warn_none.append((r["라벨"], sg["수단"]))
            elif src == "정류장직선":
                flag = "  <- 정류장 직선연결(근사)"
                warn_fallback.append((r["라벨"], sg["수단"]))
            if ov > 0.8:
                flag += "  ★택시와 사실상 동일"
                warn_same.append((r["라벨"], sg["수단"], ov))
            print(f"  {r['라벨']:<5} {sg['수단']:<14} {src:<14} "
                  f"{len(cs):>5}  {_polyline_len_km(cs):>6.1f}  "
                  f"{ov*100:>6.0f}%{flag}")
        if taxi:
            print(f"  {r['라벨']:<5} {'택시':<14} {'TMAP':<14} "
                  f"{len(taxi):>5}  {_polyline_len_km(taxi):>6.1f}       -")

        gap, why = check_transfer_seam(r, g)
        if gap is not None:
            ok = gap <= 400.0
            print(f"  {r['라벨']:<5} {'환승 이음새':<14} {why:<20} "
                  f"{gap:>7.0f}m  {'OK' if ok else '★어긋남'}")
            if not ok:
                warn_seam.append((r["라벨"], why, gap))

    print("  " + "-" * 64)
    if warn_none:
        print(f"  ★ 형상 없음 {len(warn_none)}건 — ODsay가 loadLane도 passStopList도")
        print("    주지 않은 구간입니다. 지도에 선이 빠져 택시만 보이게 됩니다.")
    if warn_fallback:
        print(f"  ★ 정류장 직선연결 {len(warn_fallback)}건 — loadLane 실패.")
        print("    실제 노선 형상이 아니라 정류장을 직선으로 이은 근사입니다.")
    if warn_same:
        print(f"  ★ 택시와 중복도 80% 초과 {len(warn_same)}건:")
        for lab, mode, ov in warn_same[:10]:
            print(f"      {lab} / {mode}: {ov*100:.0f}%")
        print("    같은 간선도로를 쓰는 버스면 정상일 수 있으나,")
        print("    지하철에서 나오면 형상 매칭이 잘못된 것입니다.")
    if warn_seam:
        print(f"  ★ 환승 이음새 어긋남 {len(warn_seam)}건:")
        for lab, why, gap in warn_seam[:10]:
            print(f"      {lab}: {why} 간격 {gap:,.0f}m")
        print("    대중교통 구간의 끝과 택시 시작점이 떨어져 있습니다.")
        print("    ODsay가 후보역이 아닌 다른 지점으로 경로를 끊었을 가능성이 큽니다.")
        print("    지도에서 두 선이 이어지지 않은 채 그려집니다.")
    if not (warn_none or warn_fallback or warn_same or warn_seam):
        print("  이상 없음 — 모든 구간이 API 제공 형상이고 이음새도 맞습니다.")


def color_of(유형):
    return 유형색.get(유형, "#3498DB")


def pareto_plot(all_df, pareto_df, out_path, title,
                mode="none", tops=None, knees=None, vot=None):
    """
    파레토 분포도를 그린다. mode에 따라 표기만 달라지고 전선 자체는 동일하다.

      mode="none" : 표기 없음. 전선과 P번호 라벨만 (원자료 확인용)
      mode="gc"   : GC 상위 3개 + 기울기 -VOT 등GC선
      mode="knee" : Knee 상위 3개 + 양 끝점을 잇는 기준선(현)

    GC 순위와 Knee 순위는 서로 독립적으로 산출되므로(공통 배제 없음)
    한 대안이 양쪽에 동시에 나타날 수 있다. 세 장을 따로 그리는 이유는
    두 기준의 지목 영역을 겹치지 않게 비교하기 위함이다.
    """
    GC_COLOR = {1: "#16A085", 2: "#2E86C1", 3: "#8E44AD"}
    KNEE_COLOR = {1: "#E67E22", 2: "#D4AC0D", 3: "#CA6F1E"}

    fig, ax = plt.subplots(figsize=(13, 8))
    ax.scatter(all_df["가중치_적용_시간"], all_df["비용"], s=90,
               color="#cccccc", edgecolors="#aaaaaa", alpha=0.5, zorder=2,
               label="전체 대안")

    pf = pareto_df.sort_values("가중치_적용_시간")
    ax.plot(pf["가중치_적용_시간"], pf["비용"], color="#e67e22",
            lw=1.5, ls="--", zorder=3)
    for 유형 in pf["유형"].unique():
        sub = pf.loc[pf["유형"] == 유형]
        ax.scatter(sub["가중치_적용_시간"], sub["비용"], s=140,
                   color=color_of(유형), edgecolors="k", zorder=4, label=유형)

    # 지도 파일과 매칭되는 P번호는 모든 모드에 공통으로 붙인다
    if "라벨" in pf.columns:
        for _, r in pf.iterrows():
            ax.annotate(r["라벨"], (r["가중치_적용_시간"], r["비용"]),
                        xytext=(6, 5), textcoords="offset points",
                        fontsize=7.5, color="#666666", zorder=5)

    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    handles, _ = ax.get_legend_handles_labels()

    # ── GC 모드 ────────────────────────────────────────────────
    if mode == "gc" and tops:
        for i, r in enumerate(tops, 1):
            col = GC_COLOR[i]
            bx, by = r["가중치_적용_시간"], r["비용"]
            ax.scatter(bx, by, s=560 - 70 * (i - 1), facecolors="none",
                       edgecolors=col, linewidths=3.2 - 0.6 * (i - 1), zorder=7)
            ax.annotate(
                f"GC {i}위  {r['유형']}\n{r['환승지점']} / GC {r['GC']:,.0f}원",
                (bx, by), xytext=(16, -20 - 6 * i), textcoords="offset points",
                fontsize=9.5, fontweight="bold", color=col, zorder=8,
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=col, alpha=0.94),
                arrowprops=dict(arrowstyle="->", color=col, lw=1.3))
        if vot:
            bx, by = tops[0]["가중치_적용_시간"], tops[0]["비용"]
            xs = np.linspace(*xlim, 2)
            ax.plot(xs, by + vot * (bx - xs), color=GC_COLOR[1],
                    lw=1.2, ls=":", zorder=6)
            handles.append(Line2D([0], [0], color=GC_COLOR[1], lw=1.2, ls=":",
                                  label=f"등GC선 (기울기 -VOT = -{vot:,.0f})"))
        for i in range(1, len(tops) + 1):
            handles.append(Line2D([0], [0], marker="o", color="w",
                                  markerfacecolor="none",
                                  markeredgecolor=GC_COLOR[i], markeredgewidth=2.4,
                                  markersize=13, label=f"GC {i}위"))

    # ── Knee 모드 ──────────────────────────────────────────────
    if mode == "knee" and knees and len(pf) >= 3:
        A = pf.loc[pf["가중치_적용_시간"].idxmin()]
        B = pf.loc[pf["가중치_적용_시간"].idxmax()]
        ax.plot([A["가중치_적용_시간"], B["가중치_적용_시간"]],
                [A["비용"], B["비용"]],
                color="#7f8c8d", lw=1.4, ls="-.", zorder=3.5)
        for i, r in enumerate(knees, 1):
            col = KNEE_COLOR[i]
            kx, ky = r["가중치_적용_시간"], r["비용"]
            ax.scatter(kx, ky, s=360 - 70 * (i - 1), marker="v",
                       color=col, edgecolors="k", linewidths=1.3, zorder=9)
            ax.annotate(
                f"Knee {i}위  {r['유형']}\n{r['환승지점']} / "
                f"score {r['KneeScore']:+.3f}",
                (kx, ky), xytext=(-16, 24 + 10 * i), textcoords="offset points",
                ha="right", fontsize=9.5, fontweight="bold", color=col, zorder=10,
                bbox=dict(boxstyle="round,pad=0.32", fc="white", ec=col, alpha=0.94),
                arrowprops=dict(arrowstyle="->", color=col, lw=1.25))
        handles.append(Line2D([0], [0], color="#7f8c8d", lw=1.4, ls="-.",
                              label="Knee 기준선 (양 끝점 연결)"))
        for i in range(1, len(knees) + 1):
            handles.append(Line2D([0], [0], marker="v", color="w",
                                  markerfacecolor=KNEE_COLOR[i],
                                  markeredgecolor="k", markersize=12,
                                  label=f"Knee {i}위"))

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.legend(handles=handles, fontsize=9.5, loc="upper left")
    ax.set_title(title, fontsize=13)
    ax.set_xlabel("가중치 적용 시간(분)  ※ 환승저항 포함", fontsize=11)
    ax.set_ylabel("비용(원)", fontsize=11)
    ax.grid(True, linestyle=":", alpha=0.6)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    return out_path


def background_selftest():
    """
    배경만 따로 시험한다.  실행:  python maas_v63.py --bgtest
    파이프라인 전체를 돌리지 않고 원인을 좁히기 위한 진단 모드.
    """
    setup_font()
    print("=" * 72)
    print("  배경 자가진단")
    print("=" * 72)
    st = check_map_backend()

    out = desktop_dirs()[0] if desktop_dirs() else os.path.expanduser("~")
    bbox = (126.95, 37.48, 127.10, 37.58)      # 강남 일대 테스트 박스
    print(f"\n  테스트 범위: W{bbox[0]} S{bbox[1]} E{bbox[2]} N{bbox[3]}")
    print(f"  결과 저장 위치: {out}")

    # ── 1) 타일 배경 ────────────────────────────────────────────
    print("\n[1] contextily 타일 배경")
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ok, msg = add_tile_basemap(ax)
    print(f"  결과: {'성공' if ok else '실패'} — {msg}")
    if not ok:
        if "미설치" in msg:
            print("  → pip install contextily")
        elif "403" in msg or "Forbidden" in msg:
            print("  → 타일 서버가 요청을 거부했습니다. 사내망/방화벽/VPN을 의심하세요.")
        else:
            print("  → 네트워크에서 basemaps.cartocdn.com 접근이 막혔을 수 있습니다.")
    ax.set_title(f"tile basemap: {'OK' if ok else 'FAIL'}")
    p1 = os.path.join(out, "_bgtest_tile.png")
    fig.savefig(p1, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {p1}")

    # ── 2) Overpass / osmnx 벡터 배경 ───────────────────────────
    print("\n[2] osmnx 벡터 배경 (Overpass)")
    if str(st["osmnx"]).startswith("없음"):
        print("  osmnx 미설치 — 건너뜀")
        return
    cache = r"C:\condaenvs\_osm_cache" if os.name == "nt" else "/tmp/_osm_cache"
    layers = load_layers(bbox, cache, detail=False)

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    drawn = 0
    for key, kw in [("green", dict(color=PAL["green"], edgecolor="none")),
                    ("water", dict(color=PAL["water"], edgecolor="none")),
                    ("rail", dict(color=PAL["rail"], linewidth=0.8)),
                    ("road_m", dict(color=PAL["road_m"], linewidth=0.9)),
                    ("road_l", dict(color=PAL["road_l"], linewidth=1.7))]:
        gdf = layers.get(key)
        if gdf is None or not len(gdf):
            continue
        try:
            sub = gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]
            if len(sub):
                sub.plot(ax=ax, **kw)
                drawn += 1
                print(f"  {key} 그리기 OK ({len(sub):,}개)")
        except Exception as ex:
            print(f"  {key} 그리기 실패: {type(ex).__name__} — {ex}")
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.set_title(f"vector basemap: {drawn} layers drawn")
    p2 = os.path.join(out, "_bgtest_vector.png")
    fig.savefig(p2, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"  저장: {p2}")

    print("\n" + "=" * 72)
    print("  두 PNG를 열어 어느 쪽이 비어 있는지 확인하세요.")
    print("  둘 다 비었다면 네트워크(방화벽·프록시) 문제일 가능성이 큽니다.")
    print("=" * 72)


# =====================================================================
# main
# =====================================================================
def main():
    setup_font()
    P = ask_params()

    out_dir = os.path.join(os.path.expanduser("~"), "Desktop", "MaaS_v63_결과")
    map_dir = os.path.join(out_dir, "maps")
    cache_dir = r"C:\condaenvs\_osm_cache" if os.name == "nt" else "/tmp/_osm_cache"
    os.makedirs(map_dir, exist_ok=True)

    print("\n[0/6] 혼잡도 자료")
    cong = load_congestion()

    print("\n[1/6] 지오코딩")
    origin = geocode(P["kakao_key"], "출발지", P["origin_addr"])
    dest = geocode(P["kakao_key"], "도착지", P["dest_addr"])

    print("\n[2-3/6] 대안 수집 (PT+TP+Only대중교통x3+Only택시)")
    rows, geoms = collect_alternatives(P, origin, dest, cong)
    if not rows:
        print("수집된 대안이 없습니다.")
        return

    df = build_dataframe(rows, P)
    df = df[df["실소요시간"] > 0].reset_index(drop=True)

    print(f"\n[4/6] 대안 {len(df)}개 / 진단")
    print(f"  지하철 혼잡시간 > 0 인 대안: {(df['지하철혼잡'] > 0).sum()}개")
    print(f"  혼잡도 판정불가 지하철 구간 포함 대안: {(df['판정불가구간'] > 0).sum()}개"
          f"  (9호선·신분당 등 → 전부 여유로 계산됨)")
    print(f"\n  [시간 성분 진단]")
    print(f"    '기타' 평균 {df['기타'].mean():.1f}분 / 최대 {df['기타'].max():.1f}분")

    # 잔차가 음수인 경우 = 구간시간 합이 총시간을 초과 → 데이터 불일치
    if "잔차" in df.columns:
        neg = int((df["잔차"] < -0.05).sum())
        if neg:
            print(f"    ★ 잔차 음수 {neg}개 (구간합 > 총시간). 최소 {df['잔차'].min():.1f}분")
            print("       ODsay 응답 불일치입니다. 성분 분해를 신뢰하기 어렵습니다.")

    # 도보 교차검증: '기타'의 정체가 대기인지 도보인지 가른다
    if "도보_차이" in df.columns:
        gap = df["도보_차이"].dropna()
        if len(gap) == 0:
            print("    도보 교차검증: ODsay가 totalWalkTime을 주지 않았습니다(-1).")
        else:
            mg = float(gap.mean())
            print(f"    도보 교차검증: API totalWalkTime − 구간합 = 평균 {mg:+.1f}분")
            if abs(mg) < 0.5:
                print("       → 도보가 subPath에 온전히 잡혀 있습니다.")
                print("          '기타'는 도보가 아닌 다른 시간(대기 등)입니다.")
            elif mg > 0.5:
                print("       ★ subPath에 안 잡힌 도보가 있습니다.")
                print("          그 시간이 '기타'로 흘러들어가 ω(=1.0)로 계산됩니다.")
                print(f"          도보 가중치 γ={P['gamma_walk']}를 적용해야 할 시간입니다.")
                print("          → ω를 γ에 가깝게 올리거나, 도보를 API 값으로 대체하세요.")
            else:
                print("       ★ 구간합이 API 도보시간보다 큽니다. 이중계상 의심.")
    err = df["시간정합오차"].abs()
    print(f"  시간정합 오차(실소요 − 성분합) 최대 {err.max():.2f}분 "
          f"→ {'정상' if err.max() < 0.6 else '★확인 필요'}")
    print(f"  환승저항 평균 {df['환승저항(분)'].mean():.1f}분 "
          f"(= {df['환승저항(분)'].mean() * P['vot']:,.0f}원 상당)")

    print("\n[5/6] 파레토 · 선정")
    pareto = pareto_front(df).sort_values("가중치_적용_시간").reset_index(drop=True)
    print(f"  파레토 {len(pareto)}개 (전체 {len(df)}개)")

    pareto = label_pareto(pareto)
    pareto, knee_A, knee_B = knee_score(pareto)
    tops = top_by_utility(pareto, k=3)
    top_ids = {r["id"]: i for i, r in enumerate(tops, 1)}
    knees = top_by_knee(pareto, k=3)
    knee_ids = {r["id"]: i for i, r in enumerate(knees, 1)}


    def _line(r):
        return (f"       GC {r['GC']:,.0f}원 · 가중시간 {r['가중치_적용_시간']:.1f}분 · "
                f"비용 {r['비용']:,.0f}원 · 실 {r['실소요시간']:.1f}분\n"
                f"       버스 {r['버스']:.0f} / 지하철 여유 {r['지하철여유']:.0f}·혼잡 {r['지하철혼잡']:.0f} / "
                f"도보 {r['도보']:.0f} / 택시 {r['택시']:.0f} / 기타 {r['기타']:.0f} (분)\n"
                f"       환승 {int(r['환승계'])}회 → 저항 {r['환승저항(분)']:.1f}분")

    print("\n  === 파레토 전선 (지도 전부 생성) ===")
    for _, r in pareto.iterrows():
        rk = top_ids.get(r["id"])
        mark = f" ◆추천{rk}위" if rk else ""
        kk = knee_ids.get(r["id"])
        if kk:
            mark += f" ▼Knee{kk}위"
        print(f"  [{r['라벨']}]{mark} {r['유형']} / {r['환승지점']}  "
              f"T_w {r['가중치_적용_시간']:.1f}분 · {r['비용']:,.0f}원 · "
              f"-GC {r['-GC(효용)']:,.0f} · Knee {r['KneeScore']:+.4f}")

    # ── Knee Score ──────────────────────────────────────────────
    if knee_A is not None:
        print("\n  === Knee Score ===")
        print(f"  기준선: {knee_A['라벨']}(최저시간 {knee_A['가중치_적용_시간']:.1f}분/"
              f"{knee_A['비용']:,.0f}원) ↔ "
              f"{knee_B['라벨']}(최대시간 {knee_B['가중치_적용_시간']:.1f}분/"
              f"{knee_B['비용']:,.0f}원)")
        print("  KneeScore = (t' + c' − 1)/√2   (t', c'는 [0,1] 정규화값)")
        print("  음수 = 현 좌하단(유리) / 0 = 현 위 / 양수 = 우상단(불리)")

        ks = pareto.sort_values("KneeScore")
        for _, r in ks.iterrows():
            kk = knee_ids.get(r["id"])
            tag = f"  ← Knee 추천 {kk}위" if kk else ""
            print(f"    {r['라벨']} {str(r['유형'])[:16]:<17} "
                  f"{r['KneeScore']:+.4f}{tag}")

        n_neg = int((pareto["KneeScore"] < -1e-9).sum())
        print(f"  현 아래(볼록하게 패인) 해: {n_neg}개")

        if not knees:
            print("    ★ 전선이 오목합니다. 무릎이 존재하지 않아 Knee 추천을")
            print("      제시하지 않습니다. 극단점 선택이 불가피한 형태입니다.")
        else:
            print(f"\n  === Knee 추천 {len(knees)}개 ===")
            for i, r in enumerate(knees, 1):
                print(f"  [{i}위] {r['라벨']}  {r['유형']} / 환승지점 {r['환승지점']}")
                print(_line(r))
                print(f"       KneeScore {r['KneeScore']:+.4f} · "
                      f"GC {r['GC']:,.0f}원")
                print(f"       구간: {r['구간요약']}")

            # ── 두 기준 교차 진단 ────────────────────────────────
            gc_top = [r["라벨"] for r in tops]
            kn_top = [r["라벨"] for r in knees]
            common = [x for x in kn_top if x in gc_top]
            print("\n  === GC vs Knee 교차 진단 ===")
            print(f"    GC   상위3: {', '.join(gc_top)}")
            print(f"    Knee 상위3: {', '.join(kn_top)}")
            print(f"    공통: {', '.join(common) if common else '없음'}")
            if kn_top[0] == gc_top[0]:
                print(f"    ★ 1위가 일치({kn_top[0]}). Knee는 VOT를 쓰지 않으므로,")
                print("      이 결론은 VOT 설정과 무관하게 강건합니다.")
            else:
                print(f"    ※ 1위가 다릅니다. GC={gc_top[0]} / Knee={kn_top[0]}")
                print("      Knee는 VOT를 쓰지 않으므로, 두 결과가 갈린다는 것은")
                print("      VOT 값이 결론을 좌우하고 있다는 신호입니다.")
                print("      VOT 민감도를 확인한 뒤 어느 기준을 쓸지 정하세요.")
            if not common:
                print("    ★ 상위 3개가 하나도 겹치지 않습니다. 두 기준이 완전히")
                print("      다른 영역을 지목하고 있어 해석에 주의가 필요합니다.")

    # ── 저장 ────────────────────────────────────────────────────
    def _write_excel(path):
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            df.drop(columns=["id"]).to_excel(w, sheet_name="전체대안", index=False)
            pareto.drop(columns=["id"]).to_excel(w, sheet_name="파레토", index=False)
            pd.DataFrame([{"추천순위": i, **{c: v for c, v in r.items() if c != "id"}}
                          for i, r in enumerate(tops, 1)]).to_excel(
                w, sheet_name="추천3개_GC", index=False)
            if knees:
                pd.DataFrame([{"Knee순위": i,
                               **{c: v for c, v in r.items() if c != "id"}}
                              for i, r in enumerate(knees, 1)]).to_excel(
                    w, sheet_name="추천3개_Knee", index=False)
            pd.DataFrame([
                {"항목": "버스 가중치 α", "값": P["alpha_bus"]},
                {"항목": "지하철(여유) 가중치 β", "값": P["beta_sub"]},
                {"항목": "지하철(혼잡) 가중치 β_c", "값": P["beta_sub_c"]},
                {"항목": "도보 가중치 γ", "값": P["gamma_walk"]},
                {"항목": "택시 가중치 δ", "값": P["delta_taxi"]},
                {"항목": "기타(환승대기) 가중치 ω", "값": P["omega_other"]},
                {"항목": "환승저항 τ(분/회)", "값": P["tau_tr"]},
                {"항목": "모드전환 환승 계산", "값": "포함" if P["count_mc"] else "제외"},
                {"항목": "환승 1회 화폐가치(원)", "값": P["tau_tr"] * P["vot"]},
                {"항목": "VOT(원/분)", "값": P["vot"]},
                {"항목": "혼잡 임계값", "값": P["threshold"]},
                {"항목": "혼잡도 시트", "값": _sheet_for(P["dep_dt"])},
                {"항목": "혼잡도 파일", "값": (cong or {}).get("path", "없음")},
            ]).to_excel(w, sheet_name="파라미터", index=False)

    xlsx = os.path.join(out_dir, "분석결과.xlsx")
    try:
        _write_excel(xlsx)
    except PermissionError:
        alt = os.path.join(out_dir,
                           f"분석결과_{datetime.now().strftime('%m%d_%H%M%S')}.xlsx")
        print(f"\n  '{os.path.basename(xlsx)}'가 열려 있어 저장하지 못했습니다.")
        try:
            _write_excel(alt)
            xlsx = alt
        except Exception as e:
            print(f"  엑셀 저장 실패: {type(e).__name__} — 지도만 생성합니다.")
            xlsx = None
    if xlsx:
        print(f"\n  저장: {xlsx}")

    label = f"{origin['name']} -> {dest['name']} | {P['dep_dt'].strftime('%m/%d %H:%M')} 출발"
    base = f"[{label}]  τ={P['tau_tr']}분/회, VOT={P['vot']:,.0f}원/분"

    # 분포도 3종 — GC와 Knee는 서로 독립적으로 산출된 순위이므로
    # 한 장에 겹쳐 그리면 어느 기준의 지목인지 흐려진다. 따로 그린다.
    plots = [
        ("파레토_표기없음.png", "none",
         f"{base}\n파레토 전선 (표기 없음)"),
        ("파레토_GC.png", "gc",
         f"{base}\n파레토 전선 + GC 상위 3개"),
    ]
    if knees:
        plots.append(("파레토_Knee.png", "knee",
                      f"{base}\n파레토 전선 + Knee 상위 3개"))

    print("\n  분포도 생성")
    for fn, mode, ttl in plots:
        pareto_plot(df, pareto, os.path.join(out_dir, fn), ttl,
                    mode=mode, tops=tops, knees=knees, vot=P["vot"])
        print(f"    {fn}")
    if not knees:
        print("    (Knee 추천이 없어 파레토_Knee.png는 생성하지 않았습니다)")

    # ── 지도: 파레토 해 전부 ────────────────────────────────────
    targets = pareto if P["max_maps"] <= 0 else pareto.head(P["max_maps"])
    if len(targets) < len(pareto):
        print(f"\n[6/6] 지도 생성 — 파레토 {len(pareto)}개 중 상위 {len(targets)}개만 "
              f"(max_maps 설정)")
        # 추천 3개가 잘려나가면 강제로 포함시킨다
        missing = [r for r in tops if r["id"] not in set(targets["id"])]
        if missing:
            targets = pd.concat([targets, pd.DataFrame(missing)], ignore_index=True)
            print(f"  → 추천 {len(missing)}개가 범위 밖이라 별도로 추가")
    else:
        print(f"\n[6/6] 지도 생성 — 파레토 {len(pareto)}개 전부")

    print(f"  배경 모드: {P['bg_mode']}"
          f"{' (이면도로 포함)' if P['bg_detail'] else ''}")
    check_map_backend()

    # 그릴 대안에 대해서만 실제 노선 형상을 받아온다 (ODsay 호출 최소화)
    print("\n  경로 형상 수집")
    hydrate_geometry(P["odsay_key"], geoms, list(targets["id"]))
    geometry_report(targets, geoms)

    layers = {}
    if P["bg_mode"] in ("auto", "vector"):
        # 배경 레이어는 파레토 전체를 덮는 bbox로 한 번만 받아 모든 지도에 재사용한다
        all_pts = [(origin["lon"], origin["lat"]), (dest["lon"], dest["lat"])]
        for _, r in targets.iterrows():
            g = geoms.get(r["id"], {})
            all_pts += list(g.get("taxi", []))
            for sg in g.get("segments", []):
                all_pts += sg["coords"] + sg["stops"]
        bbox, _ = square_extent(all_pts, 30)
        print(f"  배경 bbox: W{bbox[0]:.4f} S{bbox[1]:.4f} E{bbox[2]:.4f} N{bbox[3]:.4f}")
        layers = load_layers(bbox, cache_dir, detail=P["bg_detail"])

    diag, made = {}, 0
    for _, r in targets.iterrows():
        g = geoms.get(r["id"])
        if not g:
            continue
        if draw_route_map(layers, r["라벨"], r, g, origin, dest, map_dir,
                          best_rank=top_ids.get(r["id"]),
                          knee_rank=knee_ids.get(r["id"]),
                          bg_mode=P["bg_mode"], diag=diag):
            made += 1

    print(f"\n  지도 {made}개 생성")
    print(f"    GC   추천: {', '.join(r['라벨'] for r in tops)}")
    print(f"    Knee 추천: {', '.join(r['라벨'] for r in knees) if knees else '없음'}")
    if diag:
        print("  배경 관련 메시지")
        for k, v in diag.items():
            print(f"    - {k}: {v}")

    if ODSAY_ERRORS:
        print("\n  ODsay 오류 요약")
        for k, v in sorted(ODSAY_ERRORS.items(), key=lambda x: -x[1]):
            print(f"    {v:>4}회  {k}")
        if any("500" in k or "한도" in k or "초과" in k for k in ODSAY_ERRORS):
            print("    ★ 일일 호출 한도로 보입니다. 한도가 풀린 뒤 다시 실행하거나")
            print("      '경로당 최대 환승후보 수'를 줄이세요.")

    print(f"\n완료 -> {out_dir}")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        # 부트스트랩은 이미 파일 상단에서 force=True로 돌았다
        print("패키지 설치/업그레이드 완료. 배경 점검을 이어서 실행합니다.\n")
        background_selftest()
    elif "--bgtest" in sys.argv:
        background_selftest()
    else:
        main()