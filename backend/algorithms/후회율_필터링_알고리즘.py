# -*- coding: utf-8 -*-
"""
MaaS 후회율 기반 경로 필터링 v1.0
====================================================================
maas_v62.py가 만든 '분석결과.xlsx'의 파레토 해와 GC를 입력으로,
사용자 희망 상한을 반영해 경로를 걸러내고 순위를 매긴다.

핵심 수식
---------
    d_k = (z_k − z̄_k) / (z_k^nadir − z_k^ideal)

  · 분자의 기준점 z̄_k 만 축마다 다르다
        GC          → 이상점 (후보 중 최선). d ≥ 0, '후회율'
        사용자 상한  → 그 상한.               d 부호가 갈림
  · 분모는 언제나 이상점~내점 폭.
    상한 기준으로 나누면(내점−상한) 상한이 느슨할 때 분모가 0으로 가면서
    미세한 위반이 폭발한다. 폭으로 나눠야 '가능한 변동폭의 몇 %'라는
    고정된 의미가 모든 축에 공통으로 붙는다.

  · 상한은 '넘으면 부담' 기준이므로 초과분만 감점한다.
        d = max(0, z − z̄) / span        (ψ = 0, 기본)
    "싸면 쌀수록 좋다"는 선형 선호는 이미 GC 안에 있다.
    상한 축이 그것을 또 세면 이중계상이므로, 상한 축은 순수하게
    '초과 부담'만 담당한다.

    여유의 가치를 인정하고 싶으면 ψ > 0 으로 두면 된다.
        d = ψ·(z − z̄)/span   (z ≤ z̄ 인 구간)
    도착시각처럼 여유가 지연 리스크를 줄여 주는 축에 쓸 만하다.

    ψ=0 이어도 충족 대안이 동점으로 뭉치지 않는다. 조건 축이 모두 0이면
    max가 d_GC를 집으므로 충족 대안끼리는 GC 후회율 순으로 정렬된다.

집계
----
    S_i = max_k(λ_k d_k,i) + ρ·Σ_k λ_k d_k,i      (증강 체비셰프)

  max 항은 '가장 나쁜 목표'를 통제한다. 가중합과 달리 파레토 전선의
  비볼록 구간에 놓인 해에도 도달할 수 있다.
  ρ항(=0.001)은 약효율해를 배제하는 용도이며 순위를 뒤집지 않는다.

  조건을 전부 충족하면 조건 축의 d는 모두 음수이고 d_GC만 0 이상이므로,
  max가 자동으로 GC 항을 집는다. → 충족 대안끼리는 GC 후회율 순으로
  줄세워지는 동작이 분기문 없이 수식에서 나온다.

미입력 조건
-----------
  '상관 없음'으로 보고 목표집합에서 제외한 뒤 가중치를 재정규화한다.
  0점으로 두고 계산에 남기면 결과가 왜곡된다.
  셋 다 미입력이면 λ_GC=1이 되어 순수 GC 순위로 자연스럽게 퇴화한다.

실행
----
  python maas_regret_filter.py                (분석결과.xlsx 자동 탐색)
  python maas_regret_filter.py 경로/분석결과.xlsx
====================================================================
"""

import os
import sys
import glob
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

RHO = 0.001
EPS = 1e-9
PSI_DEFAULT = 0.0     # 여유 보상 계수 ψ (상한 미달분을 얼마나 인정할지)

# (표시명, 컬럼, 단위, 사용자 상한을 받는가)
AXES = [
    ("GC", "GC", "원", False),
    ("Knee", "KneeScore", "", False),
    ("시간", "실소요시간", "분", True),
    ("비용", "비용", "원", True),
    ("환승", "환승계", "회", True),
]

QUALITY = ("GC", "Knee")          # 사용자 상한이 없는 '품질' 축

# 중복 판정에 쓰는 컬럼. 이 값들이 모두 같으면 모형이 구분할 수 없는 같은 경로다.
DEDUPE_COLS = ["유형", "가중치_적용_시간", "비용", "실소요시간", "환승계"]
DEDUPE_DECIMALS = 2
BETA_GC_DEFAULT = 30.0            # %
BETA_KNEE_DEFAULT = 20.0          # %


# =====================================================================
# 입력 유틸
# =====================================================================
def 숫자입력(문구, 기본=None, 허용빈값=False):
    while True:
        s = input(문구).strip().replace(",", "")
        for j in ("원", "분", "회", "%"):
            s = s.replace(j, "")
        if not s:
            if 허용빈값:
                return None
            if 기본 is not None:
                return 기본
            print("    값을 입력하세요.")
            continue
        try:
            v = float(s)
            if v < 0:
                raise ValueError
            return v
        except ValueError:
            print("    0 이상의 숫자를 입력하세요.")


def 시각입력(문구, 기준일):
    while True:
        s = input(문구).strip().replace("/", "-")
        if not s:
            return None
        for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H%M"):
            try:
                return datetime.strptime(s, f)
            except ValueError:
                pass
        for f in ("%H:%M", "%H%M"):
            try:
                t = datetime.strptime(s, f)
                return 기준일.replace(hour=t.hour, minute=t.minute,
                                    second=0, microsecond=0)
            except ValueError:
                pass
        print("    'HH:MM' 또는 'YYYY-MM-DD HH:MM' 형식으로 입력하세요.")


# =====================================================================
# 엑셀 로드
# =====================================================================
def find_excel(arg=None):
    if arg and os.path.exists(arg):
        return arg
    home = os.path.expanduser("~")
    pats = [os.path.join(d, "**", "분석결과*.xlsx")
            for d in (os.getcwd(), os.path.join(home, "Desktop"),
                      os.path.join(home, "바탕 화면"))]
    for od in glob.glob(os.path.join(home, "OneDrive*")):
        pats.append(os.path.join(od, "**", "분석결과*.xlsx"))
    hits = []
    for p in pats:
        hits += glob.glob(p, recursive=True)
    return max(hits, key=os.path.getmtime) if hits else None


def pareto_front(df, tcol="가중치_적용_시간", ccol="비용", tol=1e-9):
    t, c = df[tcol].to_numpy(float), df[ccol].to_numpy(float)
    m = np.zeros(len(df), dtype=bool)
    for i in range(len(df)):
        m[i] = not np.any((t <= t[i] + tol) & (c <= c[i] + tol) &
                          ((t < t[i] - tol) | (c < c[i] - tol)))
    return df.loc[m].copy()


def add_knee_score(df, tcol="가중치_적용_시간", ccol="비용"):
    """
    파레토 전선의 무릎 점수를 계산한다(없으면).

        A = (최저시간, 최대비용),  B = (최대시간, 최저비용)
        두 축을 [0,1] 정규화하면 A=(0,1), B=(1,0)이므로
        현으로부터의 부호 있는 거리는

            KneeScore = (t' + c' − 1) / √2

        음수 = 현 좌하단(시간·비용 둘 다 유리) / 양수 = 우상단
        최솟값이 무릎.

    엑셀에 이미 KneeScore가 있으면 그 값과 대조해 어긋나면 알린다.
    (v6.3 이후 산출물에는 들어 있고, 그 이전 산출물에는 없다)
    """
    t = df[tcol].to_numpy(float)
    c = df[ccol].to_numpy(float)
    ts, cs = t.max() - t.min(), c.max() - c.min()

    if len(df) < 3 or ts < EPS or cs < EPS:
        df["KneeScore"] = 0.0
        return df, False, "후보 3개 미만이거나 시간·비용 축이 퇴화"

    tn = (t - t.min()) / ts
    cn = (c - c.min()) / cs
    calc = (tn + cn - 1.0) / np.sqrt(2.0)

    if "KneeScore" in df.columns:
        gap = float(np.nanmax(np.abs(df["KneeScore"].to_numpy(float) - calc)))
        if gap > 1e-6:
            print(f"  ※ 엑셀의 KneeScore와 재계산값이 최대 {gap:.4f} 다릅니다.")
            print("    후보집합이 달라졌을 수 있어 재계산값을 사용합니다.")
    df["KneeScore"] = calc

    # 현 아래(음수) 해가 하나도 없으면 전선이 오목해 무릎이 존재하지 않는다.
    # 이때 KneeScore 최솟값은 끝점(0)이므로 d_Knee가 극단점에 0점(=최선)을
    # 주게 되어 의도와 정반대로 작동한다. 따라서 축 자체를 쓰지 않는다.
    if (calc < -EPS).sum() == 0:
        return df, False, "전선이 오목 — 현 아래 해가 없어 무릎이 존재하지 않음"
    return df, True, ""


def dedupe_routes(df, verbose=True):
    """
    같은 경로가 여러 번 들어온 경우를 하나로 합친다.

    왜 중복이 생기나
      · 파레토 필터의 지배 판정은
            (t ≤ t_i) & (c ≤ c_i) & ((t < t_i) | (c < c_i))
        인데, 두 점이 완전히 같으면 마지막 strict 조건이 성립하지 않아
        서로를 지배하지 못한다. 결국 둘 다 전선에 남는다.
      · 인접한 환승 후보역이 같은 경로를 만들어내는 경우가 흔하다.
        (예: '강남'과 '강남역'이 같은 ODsay 경로로 귀결)
      · 경로순위 1·2·3에서 동일한 대안이 반복될 수도 있다.

    판정 기준
      유형 · 가중시간 · 비용 · 실소요시간 · 환승계가 모두 같으면 중복으로 본다.
      이 다섯이 같으면 GC도 KneeScore도 S도 같아지므로, 순위상 서로
      구분이 불가능하고 나열해봐야 화면만 채운다.

    삭제되는 대안의 환승지점은 '병합된_환승지점' 컬럼에 남겨
    정보가 사라지지 않게 한다. 라벨(P01…)은 지도 파일명과 대응하므로
    다시 매기지 않는다.
    """
    keys = [c for c in DEDUPE_COLS if c in df.columns]
    if len(keys) < 3 or len(df) < 2:
        return df, []

    sig = df[keys].copy()
    for c in keys:
        if pd.api.types.is_numeric_dtype(sig[c]):
            sig[c] = sig[c].astype(float).round(DEDUPE_DECIMALS)
    df = df.copy()
    df["_sig"] = sig.astype(str).agg("|".join, axis=1)

    keep, merged = [], []
    for _, grp in df.groupby("_sig", sort=False):
        keep.append(grp.index[0])
        if len(grp) > 1:
            labs = list(grp["라벨"]) if "라벨" in grp.columns else list(grp.index)
            sts = ([str(x) for x in grp["환승지점"]]
                   if "환승지점" in grp.columns else [])
            merged.append({"대표": labs[0], "삭제": labs[1:], "환승지점": sts})

    out = df.loc[sorted(keep)].drop(columns=["_sig"]).reset_index(drop=True)

    if merged:
        # 병합된 환승지점을 대표 행에 남긴다
        if "병합된_환승지점" not in out.columns:
            out["병합된_환승지점"] = ""
        for m in merged:
            uniq = sorted({x for x in m["환승지점"] if x and x != "-"})
            if uniq:
                out.loc[out["라벨"] == m["대표"], "병합된_환승지점"] = \
                    " / ".join(uniq)

    if verbose and merged:
        n_del = sum(len(m["삭제"]) for m in merged)
        print(f"  중복 경로 {n_del}개 제거 ({len(merged)}건 병합)")
        print("    판정: 유형·가중시간·비용·실소요시간·환승계가 모두 동일")
        for m in merged[:8]:
            uniq = sorted({x for x in m["환승지점"] if x and x != "-"})
            tail = f"  (환승지점: {' / '.join(uniq)})" if len(uniq) > 1 else ""
            print(f"    {m['대표']} ← {', '.join(m['삭제'])}{tail}")
        if len(merged) > 8:
            print(f"    ... 외 {len(merged)-8}건")
    elif verbose:
        print("  중복 경로 없음")
    return out, merged


def load_pareto(path, dedupe=True):
    xl = pd.ExcelFile(path)
    print(f"  파일: {os.path.basename(path)}")
    if "파레토" in xl.sheet_names:
        df, src = xl.parse("파레토"), "파레토 시트"
    elif "전체대안" in xl.sheet_names:
        df, src = pareto_front(xl.parse("전체대안")), "전체대안 → 파레토 재계산"
    else:
        raise ValueError("'파레토' 또는 '전체대안' 시트가 필요합니다.")

    miss = [c for c in ("실소요시간", "비용", "GC") if c not in df.columns]
    if miss:
        raise ValueError(f"필수 컬럼 없음: {', '.join(miss)}")
    if "환승계" not in df.columns:
        df["환승계"] = df.get("환승횟수", 0)
    if "가중치_적용_시간" not in df.columns:
        df["가중치_적용_시간"] = df["실소요시간"]
    if "예상도착시각" in df.columns:
        df["예상도착시각"] = pd.to_datetime(df["예상도착시각"])
    df = df.sort_values("가중치_적용_시간").reset_index(drop=True)
    if "라벨" not in df.columns:
        df["라벨"] = [f"P{i:02d}" for i in range(1, len(df) + 1)]

    n0 = len(df)
    if dedupe:
        df, _merged = dedupe_routes(df)

    vot = None
    if "파라미터" in xl.sheet_names:
        pm = xl.parse("파라미터")
        r = pm.loc[pm["항목"].astype(str).str.contains("VOT", na=False)]
        if len(r):
            vot = float(r.iloc[0]["값"])

    df, knee_ok, knee_why = add_knee_score(df)
    print(f"  후보 {len(df)}개 ({src}"
          + (f", 중복 {n0-len(df)}개 제거" if n0 != len(df) else "") + ")"
          + (f" · 기준 VOT {vot:,.0f}원/분" if vot else ""))
    if knee_ok:
        k = df.loc[df["KneeScore"].idxmin()]
        n_neg = int((df["KneeScore"] < -EPS).sum())
        print(f"  Knee: {k['라벨']} (score {k['KneeScore']:+.4f}) · "
              f"현 아래 해 {n_neg}개")
    else:
        print(f"  Knee 축 제외: {knee_why}")
        if "오목" in knee_why:
            print("    (무릎이 없으면 KneeScore 최솟값이 끝점이 되어,")
            print("     오히려 극단점에 최고점을 주는 역효과가 납니다)")
    return df, vot, knee_ok


# =====================================================================
# [1] 경계 제시 — 상한을 받기 전에 반드시 보여준다
# =====================================================================
def anchors(df):
    """
    축별 이상점(최선)·내점(최악)·폭. 이후 모든 정규화의 분모가 된다.
    폭이 0인 축은 degenerate=True로 표시해 목표집합에서 빼도록 한다.
    """
    a = {}
    for name, col, unit, _ in AXES:
        if col not in df.columns:
            continue
        lo, hi = float(df[col].min()), float(df[col].max())
        a[name] = {"col": col, "unit": unit, "ideal": lo, "nadir": hi,
                   "span": max(hi - lo, EPS),
                   "degenerate": (hi - lo) < EPS}
    return a


def show_bounds(df, anc):
    print("\n" + "=" * 72)
    print("  이 구간에서 가능한 범위")
    print("  ※ 아래 최선~최악 폭이 곧 후회율의 분모입니다.")
    print("=" * 72)
    print(f"  {'항목':<8}{'최선(이상점)':>16}{'최악(내점)':>16}{'폭':>14}")
    print("  " + "-" * 56)
    for name, col, unit, _ in AXES:
        m = anc.get(name)
        if m is None:
            continue
        if name == "Knee":
            note = "  (퇴화 — 축 제외)" if m["degenerate"] else "  (음수=무릎쪽)"
            print(f"  {name:<8}{m['ideal']:>14.4f} {m['nadir']:>14.4f} "
                  f"{m['span']:>12.4f}{note}")
        else:
            print(f"  {name:<8}{m['ideal']:>14,.0f}{unit}"
                  f"{m['nadir']:>14,.0f}{unit}{m['span']:>12,.0f}{unit}")

    dep = None
    if "예상도착시각" in df.columns:
        dep = (df["예상도착시각"]
               - pd.to_timedelta(df["실소요시간"], unit="m")).min()
        if not pd.isna(dep):
            print(f"\n  출발 {dep:%m/%d %H:%M} → 도착 가능 시각 "
                  f"{df['예상도착시각'].min():%H:%M} ~ "
                  f"{df['예상도착시각'].max():%H:%M}")

    f = df.loc[df["실소요시간"].idxmin()]
    c = df.loc[df["비용"].idxmin()]
    g = df.loc[df["GC"].idxmin()]
    print(f"\n  최속  : {f['라벨']} {f['실소요시간']:.0f}분 / "
          f"{f['비용']:,.0f}원 ({f['유형']})")
    print(f"  최저가: {c['라벨']} {c['실소요시간']:.0f}분 / "
          f"{c['비용']:,.0f}원 ({c['유형']})")
    print(f"  GC최적: {g['라벨']} {g['실소요시간']:.0f}분 / "
          f"{g['비용']:,.0f}원 / GC {g['GC']:,.0f}원 ({g['유형']})")
    print("  ※ 시간과 비용은 상충합니다. 둘 다 최선인 대안은 없습니다.")
    return dep


# =====================================================================
# [2] 상한 입력 + 유효성 검사
# =====================================================================
def ask_limits(df, anc, dep):
    print("\n" + "=" * 72)
    print("  희망 상한 입력")
    print("  ※ 엔터 = '상관 없음'. 그 조건은 목표에서 빠지고 나머지로 재정규화됩니다.")
    print("  ※ 모두 만족하지 않아도 됩니다. 위반은 감점으로만 반영됩니다.")
    print("=" * 72)

    lim = {}
    base = dep if dep is not None else datetime.now()

    if "예상도착시각" in df.columns and dep is not None:
        t = 시각입력(f"  희망 도착시각 (예: {df['예상도착시각'].median():%H:%M}): ", base)
        if t is not None:
            if t < dep:
                t += timedelta(days=1)
            lim["시간"] = (t - dep).total_seconds() / 60.0    # 소요시간 상한으로 환산
            print(f"    → {t:%m/%d %H:%M} 까지 = 소요 {lim['시간']:.0f}분 이내")
    else:
        v = 숫자입력("  최대 소요시간 (분): ", 허용빈값=True)
        if v is not None:
            lim["시간"] = v

    v = 숫자입력("  최대 비용 (원): ", 허용빈값=True)
    if v is not None:
        lim["비용"] = v
    v = 숫자입력("  최대 환승 횟수 (회): ", 허용빈값=True)
    if v is not None:
        lim["환승"] = v

    # ── 유효성 검사 ────────────────────────────────────────────
    drop = []
    if lim:
        print("\n  상한 검토")
        for k, t in lim.items():
            m = anc[k]
            if t < m["ideal"] - EPS:
                print(f"    ★ {k} 상한 {t:,.0f}{m['unit']}는 달성 불가입니다 "
                      f"(최선이 {m['ideal']:,.0f}{m['unit']}).")
                print(f"       모든 대안이 위반하므로 사실상 '{k} 최소화'로 작동합니다.")
            elif t >= m["nadir"] - EPS:
                print(f"    ★ {k} 상한 {t:,.0f}{m['unit']}는 구속력이 없습니다 "
                      f"(최악이 {m['nadir']:,.0f}{m['unit']}).")
                print(f"       전 대안이 충족하므로 목표에서 제외합니다.")
                drop.append(k)
            else:
                below = (t - m["ideal"]) / m["span"]
                print(f"    {k}: 최선~최악 구간의 {below*100:.0f}% 지점 "
                      f"(여유쪽 {below*100:.0f}% / 초과쪽 {(1-below)*100:.0f}%)")
        for k in drop:
            lim.pop(k)
    return lim


# =====================================================================
# [3] 가중치
# =====================================================================
def ask_psi(lim):
    """
    ψ = 상한 미달분(여유)을 얼마나 인정할지.
      0.0 : 상한만 넘지 않으면 그만. 여유가 많든 적든 동일. (기본)
      0.3 : 여유를 약하게 인정. 도착시각처럼 버퍼가 지연 리스크를
            줄여 주는 축에서 유용하다.
      1.0 : 상한을 단순 기준점으로 보고 대칭 취급.
    ψ를 올릴수록 '싸면 쌀수록 좋다'를 GC와 중복해서 세게 되므로
    이중계상 위험이 커진다.
    """
    if not lim:
        return PSI_DEFAULT
    print("\n" + "-" * 72)
    print("  여유 보상 계수 ψ")
    print("  상한은 '넘으면 부담' 기준입니다. 상한 아래의 여유를 점수로")
    print("  인정할지 정합니다. 0이면 넘지만 않으면 동일하게 봅니다.")
    print("  ※ '적을수록 좋다'는 선형 선호는 이미 GC가 담고 있습니다.")
    print("    ψ를 올리면 그 선호를 이중으로 세게 됩니다.")
    print("-" * 72)
    psi = 숫자입력(f"  ψ (0~1, 기본 {PSI_DEFAULT:.1f}): ", PSI_DEFAULT)
    psi = min(max(psi, 0.0), 1.0)
    if psi <= EPS:
        print("    → 초과분만 감점합니다. 충족 대안끼리는 GC 후회율로 정렬됩니다.")
    else:
        print(f"    → 여유 1단위를 초과 1단위의 {psi:.0%}로 인정합니다.")
    return psi


def ask_weights_direct(lim, use_knee=True):
    """
    λ를 축별로 직접 입력받는다. 재정규화·클램프 없이 입력값 그대로 쓴다.

    체비셰프 S = max_k(λ_k d_k) + ρ·Σ λ_k d_k 에서 모든 λ에 같은 상수를
    곱하면 S도 같은 배로 스케일될 뿐 순위는 바뀌지 않는다.
    따라서 λ의 합이 1이 아니어도 순위는 유효하다. 다만 S의 절대값을
    다른 실행과 비교하려면 합을 1로 맞춰 두는 편이 낫다.
    """
    axes = ["GC"] + (["Knee"] if use_knee else []) + list(lim.keys())
    print("\n" + "-" * 72)
    print("  λ 직접 입력")
    print("  각 축의 가중치를 그대로 씁니다. 재정규화도, 상한 클램프도 없습니다.")
    print("  ※ 모든 λ에 같은 배수를 곱해도 순위는 동일합니다(체비셰프의 성질).")
    print("    합을 1로 맞추면 S 값을 다른 실행과 비교하기 편합니다.")
    print("-" * 72)

    w = {}
    for k in axes:
        tag = "[품질]" if k in QUALITY else "[조건]"
        w[k] = 숫자입력(f"  λ_{k:<5} {tag} : ", 0.0)

    tot = sum(w.values())
    if tot < EPS:
        print("    ★ 모든 λ가 0입니다. 기본값(GC 1.0)으로 대체합니다.")
        return {"GC": 1.0}, (1.0, 0.0)

    print(f"\n  입력 합계 {tot:.3f}")
    if abs(tot - 1.0) > 1e-6:
        if 예아니오_간단("  합을 1로 정규화할까요? (Y/n): "):
            w = {k: v / tot for k, v in w.items()}
            print("    → 정규화했습니다.")
        else:
            print("    → 입력값 그대로 사용합니다. (순위는 동일, S 절대값만 달라짐)")

    print("\n  적용 가중치 (합 %.3f)" % sum(w.values()))
    for k, v in w.items():
        tag = "[품질]" if k in QUALITY else "[조건]"
        print(f"    {k:<6} {v:.3f} {tag}")
    return w, (w.get("GC", 0.0), w.get("Knee", 0.0))


def 예아니오_간단(문구, 기본=True):
    t = input(문구).strip().lower()
    if not t:
        return 기본
    return t not in ("n", "no", "아니오", "아니요", "0", "f", "false")


def ask_weight_mode():
    print("\n" + "-" * 72)
    print("  가중치 입력 방식")
    print("    1) 비중 % 방식 (기본)")
    print("       GC·Knee의 %를 주면 나머지를 조건들이 중요도(1~5)로 나눕니다.")
    print("    2) λ 직접 입력")
    print("       다섯 축의 λ를 하나씩 직접 찍습니다. 재정규화·클램프 없음.")
    print("-" * 72)
    v = input("  방식 (1/2, 기본 1): ").strip()
    return "direct" if v == "2" else "ratio"


def ask_weights(lim, use_knee=True):
    """
    가중치 배분
        λ_GC   = β_GC
        λ_Knee = β_Knee            (Knee 축을 쓸 때만)
        λ_k    = (1 − β_GC − β_Knee) · w_k / Σw     (입력된 상한들이 나눠 가짐)

    GC와 Knee는 성격이 다른 '품질' 축이다.
      GC   : VOT를 써서 시간을 돈으로 환산한 경제적 지표
      Knee : VOT를 쓰지 않고 전선의 기하만 보는 지표
    둘을 함께 두면 VOT 설정에 대한 의존을 부분적으로 희석할 수 있다.
    기본값은 GC 30 / Knee 20 — GC가 경제적 해석을 갖는 주 지표이므로
    더 큰 몫을 주고, Knee는 보완 지표로 둔다.

    상한이 하나도 없으면 품질 축만으로 배분한다.
    """
    quality = ["GC"] + (["Knee"] if use_knee else [])

    print("\n" + "-" * 72)
    print("  비중 배분")
    print("    GC   : VOT 기반 일반화비용 후회율 (경제적 지표)")
    if use_knee:
        print("    Knee : 전선 기하 기반 무릎 점수 후회율 (VOT 무관)")
    print("    조건 : 사용자가 준 상한들 (입력한 것만, 중요도로 분배)")
    print("-" * 72)

    bg = 숫자입력(f"  GC 비중 % (기본 {BETA_GC_DEFAULT:.0f}): ",
                 BETA_GC_DEFAULT) / 100.0
    bk = 0.0
    if use_knee:
        bk = 숫자입력(f"  Knee 비중 % (기본 {BETA_KNEE_DEFAULT:.0f}): ",
                     BETA_KNEE_DEFAULT) / 100.0

    if not lim:
        # 조건이 없으면 품질 축끼리 재정규화
        tot = bg + bk
        if tot < EPS:
            bg, bk = (1.0, 0.0) if not use_knee else (0.5, 0.5)
            tot = 1.0
        w = {"GC": bg / tot}
        if use_knee:
            w["Knee"] = bk / tot
        print("\n  상한이 없습니다. 품질 축만으로 순위를 매깁니다.")
        print("  적용 가중치")
        for k, v in w.items():
            print(f"    {k:<6} {v:.3f}")
        return w, (bg, bk)

    # 조건이 있으면 품질 합이 1을 넘지 않도록 제한
    if bg + bk >= 1.0 - EPS:
        sc = 0.9 / max(bg + bk, EPS)      # 조건 몫 10%는 남긴다
        print(f"\n    ★ 품질 비중 합이 {(bg+bk)*100:.0f}%로 100%에 닿습니다.")
        print(f"      조건 몫이 사라지므로 {sc:.3f}배로 축소해 10%를 남깁니다.")
        bg, bk = bg * sc, bk * sc

    rest = 1.0 - bg - bk
    print(f"\n  → 조건 몫 {rest*100:.0f}%")
    print("\n  조건별 중요도 1~5 (기본 3)")
    imp = {k: 숫자입력(f"    {k}: ", 3.0) for k in lim}
    si = sum(imp.values()) or 1.0

    w = {"GC": bg}
    if use_knee:
        w["Knee"] = bk
    for k, v in imp.items():
        w[k] = rest * (v / si)

    print("\n  적용 가중치 (합 %.3f)" % sum(w.values()))
    for k, v in w.items():
        tagg = " [품질]" if k in QUALITY else " [조건]"
        print(f"    {k:<6} {v:.3f}{tagg}")
    return w, (bg, bk)


# =====================================================================
# [4] 후회율
# =====================================================================
def regret_matrix(df, anc, lim, psi=PSI_DEFAULT, use_knee=True):
    """
    품질 축 : d = (z − 이상점) / span          항상 ≥ 0 (후회율)
        GC   이상점 = 최소 GC
        Knee 이상점 = 최소 KneeScore (= 무릎)
        두 축이 같은 형태라 같은 자로 잰다.
    상한 축 : d = max(0, z − 상한) / span      초과분만 (ψ=0)
              ψ > 0 이면 미달분도 ψ배로 인정
    """
    out = df.copy()
    dev = {}

    m = anc["GC"]
    dev["GC"] = (out["GC"] - m["ideal"]) / m["span"]
    out["d_GC"] = dev["GC"]
    out["GC후회액"] = out["GC"] - m["ideal"]

    if use_knee and "Knee" in anc and not anc["Knee"]["degenerate"]:
        mk = anc["Knee"]
        dev["Knee"] = (out["KneeScore"] - mk["ideal"]) / mk["span"]
        out["d_Knee"] = dev["Knee"]
        out["Knee후회"] = out["KneeScore"] - mk["ideal"]

    for k, t in lim.items():
        m = anc[k]
        z = out[m["col"]].astype(float)
        raw = (z - t) / m["span"]
        # 초과분은 그대로, 미달분은 ψ배 (ψ=0이면 0)
        d = raw.where(raw > 0, psi * raw)
        dev[k] = d.mask(d.abs() < EPS, 0.0)      # -0.0 표시 정리
        out[f"d_{k}"] = dev[k]
        out[f"초과_{k}"] = (z - t).clip(lower=0.0)
        out[f"여유_{k}"] = (t - z).clip(lower=0.0)

    if lim:
        out["위반수"] = sum((out[f"초과_{k}"] > EPS).astype(int) for k in lim)
        out["충족"] = out["위반수"] == 0
    else:
        out["위반수"], out["충족"] = 0, True
    return out, dev


def score(out, dev, w, rho=RHO):
    names = [k for k in dev if k in w]
    V = np.column_stack([dev[k].to_numpy(float) * w[k] for k in names])
    o = out.copy()
    o["S"] = V.max(axis=1) + rho * V.sum(axis=1)
    o["최악목표"] = [names[i] for i in V.argmax(axis=1)]
    return o


# =====================================================================
# [5] 출력
# =====================================================================
def show_ranking(o, lim, top=None):
    print("\n" + "=" * 72)
    print("  전체 후보 순위  (S 작을수록 좋음)")
    print("=" * 72)
    cols = [c for c in ("d_GC", "d_Knee") if c in o.columns] \
           + [f"d_{k}" for k in lim]
    head = f"  {'순위':<4}{'라벨':<6}{'유형':<18}{'S':>8}"
    for c in cols:
        head += f"{c:>9}"
    head += "  최악목표"
    print(head)
    print("  " + "-" * (len(head) + 4))

    s = o.sort_values("S").reset_index(drop=True)
    rows = s if top is None else s.head(top)
    for i, r in rows.iterrows():
        mark = "*" if (lim and r["충족"]) else " "
        line = (f"  {i+1:<4}{r['라벨']:<6}{str(r['유형'])[:17]:<18}"
                f"{r['S']:>8.3f}")
        for c in cols:
            line += f"{r[c]:>+9.3f}"
        line += f"  {r['최악목표']}{mark}"
        print(line)
    if lim:
        n = int(o["충족"].sum())
        print(f"\n  * = 상한 전부 충족 ({n}/{len(o)}개)")
        if n == 0:
            print("    ★ 전부 충족하는 대안이 없습니다. 위반이 작은 순으로 제시됩니다.")
    return s


def detail(s, lim, anc, betas, w, psi, out_path=None):
    print("\n" + "=" * 72)
    print("  상위 3안 상세")
    print("=" * 72)
    rows = []
    for i, r in s.head(3).iterrows():
        print(f"\n[{i+1}] {r['라벨']} {r['유형']} / 환승지점 {r.get('환승지점','-')}")
        print(f"    소요 {r['실소요시간']:.0f}분 · {r['비용']:,.0f}원 · "
              f"환승 {int(r['환승계'])}회")
        if "예상도착시각" in r.index and pd.notna(r["예상도착시각"]):
            print(f"    도착 {pd.Timestamp(r['예상도착시각']):%m/%d %H:%M}")
        print(f"    GC   {r['GC']:,.0f}원 — 최적 대비 +{r['GC후회액']:,.0f}원 "
              f"(후회율 {r['d_GC']*100:.1f}%)")
        if "d_Knee" in r.index:
            knee_tag = " ← 무릎" if abs(r["d_Knee"]) < EPS else ""
            print(f"    Knee {r['KneeScore']:+.4f} — 무릎 대비 "
                  f"+{r['Knee후회']:.4f} (후회율 {r['d_Knee']*100:.1f}%)"
                  f"{knee_tag}")
        for k in lim:
            u = anc[k]["unit"]
            if r[f"초과_{k}"] > EPS:
                print(f"      초과 {k}: {r[f'초과_{k}']:,.0f}{u} "
                      f"(d={r[f'd_{k}']:+.3f})")
            else:
                tail = "점수 미반영" if abs(r[f'd_{k}']) < EPS else f"d={r[f'd_{k}']:+.3f}"
                print(f"      OK   {k}: 여유 {r[f'여유_{k}']:,.0f}{u} ({tail})")
        print(f"    종합 S {r['S']:.3f} · 가장 아쉬운 목표: {r['최악목표']}")
        if "구간요약" in r.index:
            print(f"    경로: {r['구간요약']}")
        rows.append({"순위": i + 1, **{k: v for k, v in r.items()}})

    if out_path:
        with pd.ExcelWriter(out_path) as wr:
            pd.DataFrame(rows).to_excel(wr, sheet_name="상위3안", index=False)
            s.to_excel(wr, sheet_name="전체순위", index=False)
            pd.DataFrame([{"항목": "GC 비중 β_GC", "값": betas[0]},
                           {"항목": "Knee 비중 β_Knee", "값": betas[1]}]
                         + [{"항목": f"가중치 {k}", "값": v}
                            for k, v in w.items()]
                         + [{"항목": f"상한 {k}", "값": v}
                            for k, v in lim.items()]
                         + [{"항목": "ψ (여유 보상계수)", "값": psi},
                            {"항목": "ρ (증강계수)", "값": RHO}]
                         ).to_excel(wr, sheet_name="설정", index=False)
        print(f"\n  저장: {out_path}")


def robustness(o, dev, lim, betas, use_knee=True):
    """
    품질 몫(GC+Knee)을 ±25%p 흔들어 1위가 유지되는지 본다.
    GC:Knee 내부 비율은 고정한 채 블록 전체를 움직인다.
    """
    if not lim:
        return
    bg, bk = betas
    q = bg + bk
    if q < EPS:
        return
    rg, rk = bg / q, bk / q          # 품질 축 내부 비율

    cond = {k: 1.0 for k in lim}
    cs = sum(cond.values())

    print("\n  [비중 강건성] 품질 몫(GC+Knee)을 바꿨을 때의 1위")
    res = []
    for qq in (min(q + 0.25, 0.95), q, max(q - 0.25, 0.0)):
        w = {"GC": qq * rg}
        if use_knee:
            w["Knee"] = qq * rk
        for k, v in cond.items():
            w[k] = (1.0 - qq) * (v / cs)
        top = score(o, dev, w).sort_values("S").iloc[0]["라벨"]
        res.append((qq, top))
        print(f"    품질 {qq*100:>3.0f}% (GC {qq*rg*100:.0f} / "
              f"Knee {qq*rk*100:.0f}) → {top}")
    if len({t for _, t in res}) == 1:
        print("  → 비중을 흔들어도 동일합니다. 강건한 추천입니다.")
    else:
        print("  → 비중에 따라 결론이 갈립니다. 사용자 선호가 결과를 좌우합니다.")

    # GC만 / Knee만 썼을 때의 1위를 대조군으로 보여준다
    if use_knee:
        wg = {"GC": q}
        wk = {"Knee": q}
        for k, v in cond.items():
            wg[k] = (1.0 - q) * (v / cs)
            wk[k] = (1.0 - q) * (v / cs)
        g1 = score(o, dev, wg).sort_values("S").iloc[0]["라벨"]
        k1 = score(o, dev, wk).sort_values("S").iloc[0]["라벨"]
        print(f"\n  [품질축 교차] GC만 → {g1} / Knee만 → {k1}")
        if g1 == k1:
            print("  → 두 품질 지표가 같은 해를 지목합니다.")
            print("    Knee는 VOT를 쓰지 않으므로 VOT 설정과 무관하게 강건합니다.")
        else:
            print("  → 두 지표가 다른 해를 지목합니다.")
            print("    VOT 값이 결론을 좌우하고 있다는 신호입니다.")


# =====================================================================
# main
# =====================================================================
def main():
    print("=" * 72)
    print("  MaaS 후회율 기반 경로 필터링")
    print("=" * 72)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dedupe = "--keep-dupes" not in sys.argv
    if not dedupe:
        print("  (--keep-dupes: 중복 경로를 남깁니다)")
    path = find_excel(args[0] if args else None)
    if not path:
        print("  분석결과.xlsx를 찾지 못했습니다.")
        print("  사용법: python maas_regret_filter_v3.py <엑셀경로> [--keep-dupes]")
        return

    df, vot, knee_ok = load_pareto(path, dedupe=dedupe)
    if len(df) < 2:
        print("  후보가 2개 미만이라 비교할 수 없습니다.")
        return

    anc = anchors(df)
    use_knee = knee_ok and not anc.get("Knee", {}).get("degenerate", True)
    dep = show_bounds(df, anc)
    lim = ask_limits(df, anc, dep)
    if ask_weight_mode() == "direct":
        w, betas = ask_weights_direct(lim, use_knee=use_knee)
    else:
        w, betas = ask_weights(lim, use_knee=use_knee)
    psi = ask_psi(lim)

    out, dev = regret_matrix(df, anc, lim, psi, use_knee=use_knee)
    o = score(out, dev, w)

    s = show_ranking(o, lim, top=15)
    robustness(out, dev, lim, betas, use_knee=use_knee)
    detail(s, lim, anc, betas, w, psi,
           os.path.join(os.path.dirname(path), "후회율_필터링.xlsx"))


if __name__ == "__main__":
    main()