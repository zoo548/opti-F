# -*- coding: utf-8 -*-
"""MaaS 후회율 필터링 엔진 (CLI/엑셀 제거, 웹 서버용)."""

from __future__ import annotations

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

_LIMIT_KEYS = {
    "시간": "max_time_min",
    "비용": "max_cost_krw",
    "환승": "max_transfers",
}

_WEIGHT_ALIASES = {
    "GC": "GC", "gc": "GC",
    "Knee": "Knee", "knee": "Knee", "KneeScore": "Knee", "knee_score": "Knee",
    "시간": "시간", "time": "시간", "max_time_min": "시간",
    "비용": "비용", "cost": "비용", "max_cost_krw": "비용",
    "환승": "환승", "transfers": "환승", "max_transfers": "환승",
}


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


def _py(v):
    if v is None:
        return None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (np.generic,)):
        v = v.item()
    if isinstance(v, float):
        return float(v)
    if isinstance(v, (int, bool, str)):
        return v
    return v


def _pick(row, *keys, default=None):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _candidates_to_df(candidates):
    rows = []
    for item in candidates:
        rows.append({
            "라벨": str(_pick(item, "id", "라벨", default="")),
            "유형": _pick(item, "유형", "type", default=""),
            "가중치_적용_시간": _pick(item, "가중치_적용_시간", "weighted_time"),
            "비용": _pick(item, "비용", "cost"),
            "실소요시간": _pick(item, "실소요시간", "total_time"),
            "환승계": _pick(item, "환승계", "transfers", default=0),
            "GC": _pick(item, "GC", "gc"),
            "KneeScore": _pick(item, "KneeScore", "knee_score"),
            "환승지점": _pick(item, "환승지점", "transfer_station", default="-"),
        })
    df = pd.DataFrame(rows)
    miss = [c for c in ("실소요시간", "비용", "GC") if c not in df.columns]
    if miss:
        raise ValueError(f"필수 컬럼 없음: {', '.join(miss)}")
    for col in ("실소요시간", "비용", "GC", "가중치_적용_시간", "환승계", "KneeScore"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if df["GC"].isna().all():
        raise ValueError("필수 컬럼 없음: GC")
    if "환승계" not in df.columns:
        df["환승계"] = 0
    if "가중치_적용_시간" not in df.columns or df["가중치_적용_시간"].isna().all():
        df["가중치_적용_시간"] = df["실소요시간"]
    else:
        df["가중치_적용_시간"] = df["가중치_적용_시간"].fillna(df["실소요시간"])
    df["환승계"] = df["환승계"].fillna(0)
    df = df.dropna(subset=["실소요시간", "비용", "GC"]).reset_index(drop=True)
    if "라벨" not in df.columns:
        df["라벨"] = [f"P{i:02d}" for i in range(1, len(df) + 1)]
    df = df.sort_values("가중치_적용_시간").reset_index(drop=True)
    return df


def _parse_arrive_by(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _limits_from_request(limits, df):
    limits = dict(limits or {})
    lim = {}
    applied_src = {}

    arrive_by = limits.get("arrive_by")
    if arrive_by:
        t = _parse_arrive_by(arrive_by)
        if "예상도착시각" in df.columns:
            dep = (df["예상도착시각"]
                   - pd.to_timedelta(df["실소요시간"], unit="m")).min()
        else:
            dep = datetime.now().replace(second=0, microsecond=0)
        if t < dep:
            t += timedelta(days=1)
        lim["시간"] = (t - dep).total_seconds() / 60.0
        applied_src["시간"] = "arrive_by"
    elif limits.get("max_time_min") is not None:
        lim["시간"] = float(limits["max_time_min"])
        applied_src["시간"] = "max_time_min"

    if limits.get("max_cost_krw") is not None:
        lim["비용"] = float(limits["max_cost_krw"])
        applied_src["비용"] = "max_cost_krw"
    if limits.get("max_transfers") is not None:
        lim["환승"] = float(limits["max_transfers"])
        applied_src["환승"] = "max_transfers"
    return lim, applied_src, arrive_by


def _drop_nonbinding(lim, anc):
    drop = []
    if lim:
        for k, t in lim.items():
            m = anc[k]
            if t < m["ideal"] - EPS:
                continue
            elif t >= m["nadir"] - EPS:
                drop.append(k)
        for k in drop:
            lim.pop(k)
    return lim


def _default_weights(lim, use_knee):
    bg = BETA_GC_DEFAULT / 100.0
    bk = (BETA_KNEE_DEFAULT / 100.0) if use_knee else 0.0
    if not lim:
        tot = bg + bk
        if tot < EPS:
            bg, bk = (1.0, 0.0) if not use_knee else (0.5, 0.5)
            tot = 1.0
        w = {"GC": bg / tot}
        if use_knee:
            w["Knee"] = bk / tot
        return w, (bg, bk)

    if bg + bk >= 1.0 - EPS:
        sc = 0.9 / max(bg + bk, EPS)
        bg, bk = bg * sc, bk * sc

    rest = 1.0 - bg - bk
    imp = {k: 3.0 for k in lim}
    si = sum(imp.values()) or 1.0

    w = {"GC": bg}
    if use_knee:
        w["Knee"] = bk
    for k, v in imp.items():
        w[k] = rest * (v / si)
    return w, (bg, bk)


def _weights_from_request(weights, lim, use_knee):
    axes = ["GC"] + (["Knee"] if use_knee else []) + list(lim.keys())
    mapped = {}
    for key, val in (weights or {}).items():
        name = _WEIGHT_ALIASES.get(str(key), str(key))
        if name in axes:
            mapped[name] = float(val)
    w = {k: mapped.get(k, 0.0) for k in axes}
    tot = sum(w.values())
    if tot < EPS:
        return {"GC": 1.0}, (1.0, 0.0)
    w = {k: v / tot for k, v in w.items()}
    return w, (w.get("GC", 0.0), w.get("Knee", 0.0))


def _robust_flag(o, dev, lim, betas, use_knee=True):
    if not lim:
        return True
    bg, bk = betas
    q = bg + bk
    if q < EPS:
        return True
    rg, rk = bg / q, bk / q
    cond = {k: 1.0 for k in lim}
    cs = sum(cond.values())
    tops = []
    for qq in (min(q + 0.25, 0.95), q, max(q - 0.25, 0.0)):
        w = {"GC": qq * rg}
        if use_knee:
            w["Knee"] = qq * rk
        for k, v in cond.items():
            w[k] = (1.0 - qq) * (v / cs)
        tops.append(score(o, dev, w).sort_values("S").iloc[0]["라벨"])
    return len(set(tops)) == 1


def _applied_limits(lim, applied_src, arrive_by):
    out = {}
    if "시간" in lim:
        out["max_time_min"] = _py(lim["시간"])
        if applied_src.get("시간") == "arrive_by" and arrive_by:
            out["arrive_by"] = arrive_by
    if "비용" in lim:
        out["max_cost_krw"] = _py(lim["비용"])
    if "환승" in lim:
        out["max_transfers"] = _py(lim["환승"])
    return out


def _over_dict(row, lim):
    over = {}
    if "시간" in lim and float(row.get("초과_시간", 0) or 0) > EPS:
        over["time_min"] = _py(float(row["초과_시간"]))
    if "비용" in lim and float(row.get("초과_비용", 0) or 0) > EPS:
        over["cost_krw"] = _py(float(row["초과_비용"]))
    if "환승" in lim and float(row.get("초과_환승", 0) or 0) > EPS:
        over["transfers"] = _py(float(row["초과_환승"]))
    return over


def rank_routes(candidates: list[dict], limits: dict, weights: dict | None = None, psi: float = 0.0) -> dict:
    """analyze candidates와 상한으로 후회율 순위를 매긴다."""
    if not candidates:
        return {"ranking": [], "robust": True, "applied_limits": {}}

    df = _candidates_to_df(candidates)
    df = pareto_front(df)
    df, _merged = dedupe_routes(df, verbose=False)
    if len(df) == 0:
        return {"ranking": [], "robust": True, "applied_limits": {}}

    df, knee_ok, _knee_why = add_knee_score(df)
    anc = anchors(df)
    use_knee = knee_ok and not anc.get("Knee", {}).get("degenerate", True)

    lim, applied_src, arrive_by = _limits_from_request(limits, df)
    lim = _drop_nonbinding(lim, anc)

    if weights is None:
        w, betas = _default_weights(lim, use_knee)
    else:
        w, betas = _weights_from_request(weights, lim, use_knee)

    psi = min(max(float(psi if psi is not None else PSI_DEFAULT), 0.0), 1.0)
    out, dev = regret_matrix(df, anc, lim, psi, use_knee=use_knee)
    o = score(out, dev, w)
    ranked = o.sort_values("S").reset_index(drop=True)

    robustness(out, dev, lim, betas, use_knee=use_knee)
    robust = _robust_flag(out, dev, lim, betas, use_knee=use_knee)

    ranking = []
    for i, row in ranked.iterrows():
        ranking.append({
            "id": str(row["라벨"]),
            "rank": int(i) + 1,
            "score": _py(float(row["S"])),
            "meets_all": bool(row["충족"]),
            "over": _over_dict(row, lim),
        })

    return {
        "ranking": ranking,
        "robust": bool(robust),
        "applied_limits": _applied_limits(lim, applied_src, arrive_by),
    }
