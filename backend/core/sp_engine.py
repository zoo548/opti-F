# -*- coding: utf-8 -*-
"""대중교통·택시 복합경로 SP 개인화 엔진 (CLI 제거, 웹 서버용)."""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import asdict, dataclass, replace
from datetime import datetime

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit, logsumexp

AGE_GROUPS = ["20대 이하", "30대", "40대", "50대", "60세 이상"]
PURPOSES = ["여가", "통근", "업무", "기타", "귀가"]

REFERENCE_SAVING_MIN = 30
MIN_LEG_MINUTES = 4          # 이보다 짧은 구간으로는 쪼개지 않는다

# 택시 요금 단가를 두 종류로 나눈다.
#   거리요금 : 경로를 바꿔 택시 구간을 늘릴 때 (복합경로의 택시 분담 변화)
#   시간요금 : 같은 경로가 막혀서 오래 걸릴 때. 미터기는 거리요금이 이미
#              확정돼 있어 증가폭이 훨씬 작다. 이 비대칭 덕분에 택시
#              소요시간과 요금의 상관을 설계로 끊을 수 있다.
TAXI_FARE_KRW_PER_MIN = 700.0
TAXI_CONGESTION_FARE_KRW_PER_MIN = 250.0

# 택시 차내 1분이 붐비지 않는 지하철 1분보다 편하다는 제약(0 < w < 1).
# 착석·독립공간·환승 없음·대기 없음을 근거로 한다. 정체의 불쾌감은
# 택시 소요시간 자체가 늘어나는 것으로 이미 표현되므로 이중계산이 아니다.
# False로 두면 제약 없이(exp) 추정해 이 가정을 검정할 수 있다.
TAXI_WEIGHT_BOUNDED = True

# True 이면 VOT를 첫 지불사다리 응답에 고정하고 시간 가중치·환승 저항만
# 추정한다. 파라미터가 9개에서 8개로 줄어 다른 값들의 분산이 작아지지만,
# 진술 VOT가 틀렸을 때 그 오차가 다른 파라미터로 전가된다.
# --votcheck 로 두 방식을 직접 비교할 수 있다.
FIX_VOT_TO_STATED = False

DEFAULTS = {
    "vot": 400.0,
    "subway_crowded": 1.50,
    "bus": 1.25,
    "taxi": 0.75,
    "walk": 1.60,
    "transfer_min": 11.24,
}

PRIOR_SD = {
    "log_beta": 0.50,
    "log_vot": 0.35,
    "subway_crowded": 0.45,
    "bus": 0.45,
    # logit 척도는 w=0.75 부근에서 log 척도보다 약 4배 가파르다
    # (d logit/dw = 1/(w(1-w)) = 5.33 vs d log/dw = 1/w = 1.33).
    # 같은 폭의 w 사전분포를 유지하려면 제약을 걸 때 SD를 그만큼 키워야 한다.
    # 값은 복원검증으로 RMSE를 최소화하도록 정했다.
    "taxi": 1.30 if TAXI_WEIGHT_BOUNDED else 0.55,
    "walk": 0.45,
    "transfer_min": 0.45,
    "asc_combo": 0.35,
    "asc_taxi": 0.35,
}

MODE_LABEL = {"subway": "지하철", "bus": "버스", "taxi": "택시"}


# ---------------------------------------------------------------------------
# 자료구조
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    """한 대의 차량에 타고 있는 구간. ODsay subPath 하나에 대응한다.

    access_walk : 이 구간에 타기까지 걷는 시간.
                  첫 leg이면 출발지 접근도보, 그 외에는 환승도보.
    crowded_minutes : 지하철만 사용. 버스는 실시간 혼잡도 조회가 어려워 항상 0.
    """
    mode: str
    minutes: int
    crowded_minutes: int = 0
    access_walk: int = 0

    @property
    def uncrowded_minutes(self) -> int:
        return self.minutes - self.crowded_minutes


@dataclass(frozen=True)
class Alternative:
    kind: str                     # "transit" | "combo" | "taxi"
    name: str
    cost: int
    legs: tuple[Leg, ...]
    egress_walk: int = 0

    # ---- 유도 속성. 저장하지 않는다. -------------------------------------
    @property
    def transfers(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def subway_uncrowded(self) -> int:
        return sum(l.uncrowded_minutes for l in self.legs if l.mode == "subway")

    @property
    def subway_crowded(self) -> int:
        return sum(l.crowded_minutes for l in self.legs if l.mode == "subway")

    @property
    def bus(self) -> int:
        return sum(l.minutes for l in self.legs if l.mode == "bus")

    @property
    def taxi(self) -> int:
        return sum(l.minutes for l in self.legs if l.mode == "taxi")

    @property
    def access_walk(self) -> int:
        return self.legs[0].access_walk if self.legs else 0

    @property
    def transfer_walk(self) -> int:
        return sum(l.access_walk for l in self.legs[1:])

    @property
    def walk(self) -> int:
        return sum(l.access_walk for l in self.legs) + self.egress_walk

    @property
    def in_vehicle_time(self) -> int:
        return sum(l.minutes for l in self.legs)

    @property
    def door_to_door_time(self) -> int:
        return self.in_vehicle_time + self.walk


def validate_alternative(alternative: Alternative) -> list[str]:
    problems = []
    if not alternative.legs:
        problems.append(f"{alternative.name}: 구간이 없습니다.")
    for index, leg in enumerate(alternative.legs, start=1):
        if leg.minutes < MIN_LEG_MINUTES:
            problems.append(
                f"{alternative.name}: {index}번 구간이 {leg.minutes}분으로 너무 짧습니다."
            )
        if leg.crowded_minutes > leg.minutes:
            problems.append(f"{alternative.name}: {index}번 구간 혼잡시간 초과.")
        if leg.mode != "subway" and leg.crowded_minutes:
            problems.append(f"{alternative.name}: 지하철이 아닌 구간에 혼잡시간.")
        if leg.access_walk < 0:
            problems.append(f"{alternative.name}: {index}번 구간 도보 음수.")
    # 같은 수단이 연속되는 것은 허용한다(노선 갈아타기). 다만 택시는 1구간만.
    if sum(1 for l in alternative.legs if l.mode == "taxi") > 1:
        problems.append(f"{alternative.name}: 택시 구간이 둘 이상입니다.")
    return problems


def describe_legs(alternative: Alternative) -> str:
    parts = []
    for index, leg in enumerate(alternative.legs):
        if index == 0:
            parts.append(f"도보 {leg.access_walk}분")
        else:
            parts.append(f"환승(도보 {leg.access_walk}분)")
        label = MODE_LABEL[leg.mode]
        if leg.crowded_minutes:
            parts.append(f"{label} {leg.minutes}분(혼잡 {leg.crowded_minutes}분)")
        else:
            parts.append(f"{label} {leg.minutes}분")
    if alternative.egress_walk:
        parts.append(f"도보 {alternative.egress_walk}분")
    return " → ".join(parts)


# ---------------------------------------------------------------------------
# leg 조작
# ---------------------------------------------------------------------------

def _replace_leg(alternative: Alternative, index: int, **changes) -> Alternative:
    legs = list(alternative.legs)
    legs[index] = replace(legs[index], **changes)
    return replace(alternative, legs=tuple(legs))


def _longest_leg_index(alternative: Alternative, mode: str) -> int | None:
    candidates = [
        (leg.minutes, index)
        for index, leg in enumerate(alternative.legs)
        if leg.mode == mode
    ]
    return max(candidates)[1] if candidates else None


def make_crowded(alternative: Alternative, minutes: int) -> Alternative:
    """가장 긴 지하철 구간의 일부를 혼잡구간으로 바꾼다. 총시간은 불변."""
    index = _longest_leg_index(alternative, "subway")
    if index is None:
        return alternative
    leg = alternative.legs[index]
    crowded = min(leg.minutes, leg.crowded_minutes + minutes)
    return _replace_leg(alternative, index, crowded_minutes=crowded)


def shift_between_modes(
    alternative: Alternative, source_mode: str, target_mode: str, minutes: int
) -> Alternative:
    """구간 개수는 그대로 두고 두 수단 사이에 시간만 옮긴다.

    환승횟수가 변하지 않으므로 수단 가중치와 환승저항이 섞이지 않는다.
    택시가 관여하면 미터요금을 함께 조정한다.
    """
    source = _longest_leg_index(alternative, source_mode)
    target = _longest_leg_index(alternative, target_mode)
    if source is None or target is None:
        return alternative
    source_leg = alternative.legs[source]
    moved = int(min(minutes, source_leg.minutes - MIN_LEG_MINUTES))
    if moved <= 0:
        return alternative
    updated = _replace_leg(
        alternative, source,
        minutes=source_leg.minutes - moved,
        crowded_minutes=min(source_leg.crowded_minutes, source_leg.minutes - moved),
    )
    updated = _replace_leg(
        updated, target, minutes=updated.legs[target].minutes + moved
    )
    if "taxi" in (source_mode, target_mode):
        sign = 1 if target_mode == "taxi" else -1
        updated = replace(
            updated,
            cost=int(round(updated.cost + sign * TAXI_FARE_KRW_PER_MIN * moved)),
        )
    return updated


def change_taxi_minutes(alternative: Alternative, delta: int) -> Alternative:
    """같은 경로가 막히거나 뚫린 상황. 시간요금만 붙으므로 요금 증가폭이 작다."""
    index = _longest_leg_index(alternative, "taxi")
    if index is None:
        return alternative
    leg = alternative.legs[index]
    minutes = max(MIN_LEG_MINUTES, leg.minutes + delta)
    updated = _replace_leg(alternative, index, minutes=minutes)
    fare = TAXI_CONGESTION_FARE_KRW_PER_MIN * (minutes - leg.minutes)
    return replace(updated, cost=max(1000, int(round(updated.cost + fare))))


def split_longest_leg(alternative: Alternative, transfer_walk: int = 0) -> Alternative:
    """가장 긴 차내구간을 두 구간으로 쪼갠다 = 환승 1회 추가.

    총 차내시간은 보존된다. 환승도보를 0으로 두면 같은 승강장 환승이 되어
    '환승 그 자체의 저항'을 도보시간과 분리해 측정할 수 있다.
    """
    candidates = [
        (leg.minutes, index)
        for index, leg in enumerate(alternative.legs)
        if leg.mode != "taxi" and leg.minutes >= 2 * MIN_LEG_MINUTES
    ]
    if not candidates:
        return alternative
    _, index = max(candidates)
    leg = alternative.legs[index]
    first = leg.minutes // 2
    second = leg.minutes - first
    crowded_first = min(leg.crowded_minutes, first)
    crowded_second = leg.crowded_minutes - crowded_first
    legs = list(alternative.legs)
    legs[index : index + 1] = [
        replace(leg, minutes=first, crowded_minutes=crowded_first),
        Leg(leg.mode, second, crowded_second, transfer_walk),
    ]
    return replace(alternative, legs=tuple(legs))


def add_transfer_walk(alternative: Alternative, minutes: int) -> Alternative:
    """기존 환승들의 도보시간만 늘린다. 구간 개수는 불변."""
    if len(alternative.legs) < 2:
        return replace(alternative, egress_walk=alternative.egress_walk + minutes)
    legs = list(alternative.legs)
    legs[1] = replace(legs[1], access_walk=legs[1].access_walk + minutes)
    return replace(alternative, legs=tuple(legs))


# ---------------------------------------------------------------------------
# 기준카드 (피벗). ODsay/TMAP 결과를 이 형식으로 그대로 옮기면 된다.
# ---------------------------------------------------------------------------

BASE_ROUTES: dict[str, tuple[Alternative, Alternative, Alternative]] = {
    "대중교통 우회": (
        Alternative("transit", "A. 대중교통", 1500, (
            Leg("subway", 30, crowded_minutes=4, access_walk=5),
            Leg("bus", 8, access_walk=3),
        )),
        Alternative("combo", "B. 지하철+택시", 8500, (
            Leg("subway", 16, access_walk=3),
            Leg("taxi", 9, access_walk=2),
        )),
        Alternative("taxi", "C. 택시", 17000, (
            Leg("taxi", 27, access_walk=2),
        )),
    ),
    "광역버스·반복 환승": (
        Alternative("transit", "A. 광역버스 경로", 2800, (
            Leg("bus", 24, access_walk=4),
            Leg("subway", 16, access_walk=2),
            Leg("subway", 10, access_walk=2),
        )),
        Alternative("combo", "B. 지하철+택시", 12000, (
            Leg("subway", 12, access_walk=3),
            Leg("subway", 10, access_walk=2),
            Leg("taxi", 10, access_walk=1),
        )),
        Alternative("taxi", "C. 택시", 21000, (
            Leg("taxi", 38, access_walk=2),
        )),
    ),
    "도착 마감시각": (
        Alternative("transit", "A. 대중교통", 1500, (
            Leg("subway", 31, crowded_minutes=5, access_walk=5),
            Leg("bus", 8, access_walk=3),
        )),
        Alternative("combo", "B. 지하철+택시", 8200, (
            Leg("subway", 16, access_walk=3),
            Leg("taxi", 9, access_walk=2),
        )),
        Alternative("taxi", "C. 택시", 16500, (
            Leg("taxi", 26, access_walk=2),
        )),
    ),
    "혼잡 지하철 구간": (
        Alternative("transit", "A. 대중교통", 1500, (
            Leg("subway", 36, crowded_minutes=10, access_walk=4),
            Leg("bus", 8, access_walk=3),
        )),
        Alternative("combo", "B. 지하철+택시", 8300, (
            Leg("subway", 16, crowded_minutes=4, access_walk=3),
            Leg("taxi", 10, access_walk=2),
        )),
        Alternative("taxi", "C. 택시", 16800, (
            Leg("taxi", 28, access_walk=2),
        )),
    ),
}

USE_CASES = list(BASE_ROUTES)

SCENARIO_TEXT = {
    "대중교통 우회": "대중교통 경로가 직선거리보다 크게 돌아가는 통행입니다.",
    "광역버스·반복 환승": "광역버스 이용 후 짧은 환승이 반복되는 통행입니다.",
    "도착 마감시각": "정해진 시각까지 도착해야 하는 통행입니다.",
    "혼잡 지하철 구간": "지하철 혼잡구간을 지나는 통행입니다.",
}


# ---------------------------------------------------------------------------
# 블록 A: 2^3 완전요인 (복합요금 × 택시요금 × 대중교통 차내시간)
# ---------------------------------------------------------------------------

# 블록 A: 2^(4-1) 해상도 IV.
#   A 복합요금 / B 택시요금 / C 택시 소요시간 / D=ABC 대중교통 차내시간
# 택시 소요시간을 요인으로 넣은 것이 v4와의 핵심 차이다. v4에서는 순수
# 택시 대안의 시간이 한 번도 변하지 않아 w_taxi가 복합 대안 4장에서만,
# 그것도 요금과 엉킨 채로 추정됐다.
BLOCK_A_LEVELS = {"combo_cost": 2000, "taxi_cost": 4000,
                  "taxi_time": 7, "transit_time": 8}


def _block_a_rows() -> list[dict[str, int]]:
    rows = []
    for a in (-1, 1):
        for b in (-1, 1):
            for c in (-1, 1):
                d = a * b * c
                rows.append({
                    "combo_cost": a * BLOCK_A_LEVELS["combo_cost"],
                    "taxi_cost": b * BLOCK_A_LEVELS["taxi_cost"],
                    "taxi_time": c * BLOCK_A_LEVELS["taxi_time"],
                    "transit_time": d * BLOCK_A_LEVELS["transit_time"],
                })
    return rows


# ---------------------------------------------------------------------------
# 블록 B: 2^(4-1) 해상도 IV. 총시간·환승횟수 고정, 구성만 재배분.
#   A 지하철 혼잡전환 / B 버스↔지하철 배분 / C 접근도보 / D=ABC 복합 택시↔지하철
# ---------------------------------------------------------------------------

BLOCK_B_LEVELS = {"crowd": 10, "bus_share": 8, "access_walk": 5, "combo_taxi": 6}


def _block_b_rows() -> list[dict[str, bool]]:
    rows = []
    for a in (-1, 1):
        for b in (-1, 1):
            for c in (-1, 1):
                rows.append({"crowd": a > 0, "bus_share": b > 0,
                             "access_walk": c > 0, "combo_taxi": a * b * c > 0})
    return rows


# ---------------------------------------------------------------------------
# 블록 C: 2^(4-1) 해상도 IV. 환승·지각.
#   A transit 노선분할(+2환승, 도보 0분) / B transit 환승도보 +4분
#   C transit 지각 4분 / D=ABC combo 노선분할(+1환승, 도보 0분)
# 환승'횟수'와 환승'도보'를 서로 다른 요인으로 흔들어 교락을 끊는다.
# ---------------------------------------------------------------------------

BLOCK_C_LEVELS = {"transit_split": 2, "transfer_walk": 3,
                  "late": 3, "combo_split": 1}



def _block_c_rows() -> list[dict[str, bool]]:
    rows = []
    for a in (-1, 1):
        for b in (-1, 1):
            for c in (-1, 1):
                rows.append({"transit_split": a > 0, "transfer_walk": b > 0,
                             "late": c > 0, "combo_split": a * b * c > 0})
    return rows


# ---------------------------------------------------------------------------
# 기준값 기반 효용균형 (utility balance)
# ---------------------------------------------------------------------------
# 기준값(DEFAULTS) 응답자에게 세 대안의 일반화비용이 같아지도록 요금을 역산한다.
#     GC_j = 등가시간_j + 요금_j / VOT
#     요금_j* = 요금_j - (GC_j - GC_anchor) * VOT
# 손으로 요금을 맞추면 어느 한 대안이 구조적으로 불리해지기 쉽다. 균형점에서
# 출발해야 세 대안의 선택확률이 1/3 근처가 되고, 문항당 정보량이 최대가 된다.
# 요금은 상황(할증·심야·경로)에 따라 실제로 변하는 값이라 균형 조정 대상으로
# 적절하며, 시간을 건드리면 검증하려는 구성 자체가 흔들린다.

BALANCE_ANCHOR = "transit"
FARE_ROUNDING = 100
MIN_FARE = 1000

# 요금 조정 상한. 실제 경로를 쓸 때 이 값을 넘겨 조정하면 화면의 요금이
# 응답자가 아는 실제 요금과 어긋나고, 응답자는 표시값이 아니라 자기가 아는
# 요금으로 판단하게 된다. 서울 기준 심야할증 20% + 호출료 정도가 현실적
# 상한이라 0.35로 둔다. 이 한도로 균형이 안 맞는 OD는 조정할 것이 아니라
# 애초에 설문 대상에서 빼야 한다(screen_candidates 참고).
FARE_ADJUSTMENT_CAP = 0.35


def reference_parameters() -> dict:
    return theta_to_parameters(prior_theta(DEFAULTS["vot"]))


def generalized_cost(alternative: Alternative, parameters: dict) -> float:
    """기준값 기준 일반화비용(분). 요금을 VOT로 나눠 시간 단위로 환산."""
    return (equivalent_time(alternative, parameters)
            + alternative.cost / parameters["vot"])


def _shift_cost(alternative: Alternative, delta_minutes: float,
                parameters: dict, cap: float | None = -1.0) -> Alternative:
    """일반화비용을 delta_minutes 만큼 바꾸는 요금 조정. 상한을 넘으면 자른다.

    cap 기본값은 sentinel(-1.0)이다. 기본인자로 FARE_ADJUSTMENT_CAP을 직접
    쓰면 함수 정의 시점에 값이 묶여, 나중에 모듈 상수를 바꿔도 반영되지
    않는다. 호출 시점에 읽도록 한다.
    """
    if cap == -1.0:
        cap = FARE_ADJUSTMENT_CAP
    raw = alternative.cost + delta_minutes * parameters["vot"]
    if cap is not None:
        raw = min(max(raw, alternative.cost * (1 - cap)),
                  alternative.cost * (1 + cap))
    cost = max(MIN_FARE, int(round(raw / FARE_ROUNDING) * FARE_ROUNDING))
    return replace(alternative, cost=cost)


def balance_alternatives(alternatives: tuple[Alternative, ...],
                         parameters: dict) -> tuple[Alternative, ...]:
    """anchor 대안과 일반화비용이 같아지도록 나머지 대안의 요금을 맞춘다."""
    anchor = next(a for a in alternatives if a.kind == BALANCE_ANCHOR)
    target = generalized_cost(anchor, parameters)
    return tuple(
        a if a.kind == BALANCE_ANCHOR
        else _shift_cost(a, target - generalized_cost(a, parameters), parameters)
        for a in alternatives
    )


def balance_block(tasks: list[ChoiceTask], parameters: dict) -> list[ChoiceTask]:
    """블록 평균 기준으로 균형을 맞춘다.

    블록 내에서는 상수 요금이동이므로 블록 내 요인들과 교락되지 않는다.
    블록 B·C처럼 anchor의 매력도가 체계적으로 변하는 구간에서 작동점을
    자동으로 되돌린다(v4에서 손으로 넣었던 요금할증을 대체한다).
    """
    means: dict[str, float] = {}
    for kind in {a.kind for t in tasks for a in t.alternatives}:
        values = [generalized_cost(a, parameters)
                  for t in tasks for a in t.alternatives if a.kind == kind]
        means[kind] = float(np.mean(values))
    target = means[BALANCE_ANCHOR]
    return [
        replace(task, alternatives=tuple(
            a if a.kind == BALANCE_ANCHOR
            else _shift_cost(a, target - means[a.kind], parameters)
            for a in task.alternatives))
        for task in tasks
    ]


def calibrated_base(use_case: str) -> tuple[Alternative, ...]:
    return balance_alternatives(BASE_ROUTES[use_case], reference_parameters())


# ---------------------------------------------------------------------------
# 카드 생성
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChoiceTask:
    block: str
    scenario: str
    description: str
    alternatives: tuple[Alternative, ...]


def _index_of_kind(alternatives: tuple[Alternative, ...], kind: str) -> int:
    for index, alternative in enumerate(alternatives):
        if alternative.kind == kind:
            return index
    raise KeyError(kind)


def build_tasks(use_case: str) -> tuple[ChoiceTask, ...]:
    if use_case not in BASE_ROUTES:
        raise ValueError(f"알 수 없는 사용 사례: {use_case}")
    reference = reference_parameters()
    base = calibrated_base(use_case)
    scenario = SCENARIO_TEXT[use_case]
    i_t = _index_of_kind(base, "transit")
    i_c = _index_of_kind(base, "combo")
    i_x = _index_of_kind(base, "taxi")
    tasks: list[ChoiceTask] = []

    # ---- 블록 A ----------------------------------------------------------
    for row in _block_a_rows():
        alternatives = list(base)
        index = _longest_leg_index(alternatives[i_t], "subway")
        if index is not None:
            leg = alternatives[i_t].legs[index]
            alternatives[i_t] = _replace_leg(
                alternatives[i_t], index,
                minutes=max(MIN_LEG_MINUTES, leg.minutes + row["transit_time"]),
            )
        alternatives[i_c] = replace(
            alternatives[i_c],
            cost=max(1000, alternatives[i_c].cost + row["combo_cost"]))
        alternatives[i_x] = change_taxi_minutes(
            alternatives[i_x], row["taxi_time"])
        alternatives[i_x] = replace(
            alternatives[i_x],
            cost=max(1000, alternatives[i_x].cost + row["taxi_cost"]))
        tasks.append(ChoiceTask(
            "A", use_case,
            f"{scenario} 도로 상황에 따라 택시 소요시간과 요금이 달라집니다.",
            tuple(alternatives)))

    # ---- 블록 B ----------------------------------------------------------
    for row in _block_b_rows():
        alternatives = list(base)
        transit = alternatives[i_t]
        if row["bus_share"]:
            transit = shift_between_modes(
                transit, "subway", "bus", BLOCK_B_LEVELS["bus_share"])
        if row["crowd"]:
            transit = make_crowded(transit, BLOCK_B_LEVELS["crowd"])
        if row["access_walk"]:
            transit = _replace_leg(
                transit, 0,
                access_walk=transit.legs[0].access_walk
                + BLOCK_B_LEVELS["access_walk"])
        alternatives[i_t] = transit
        if row["combo_taxi"]:
            alternatives[i_c] = shift_between_modes(
                alternatives[i_c], "subway", "taxi", BLOCK_B_LEVELS["combo_taxi"])
        tasks.append(ChoiceTask(
            "B", use_case,
            f"{scenario} 총 소요시간과 환승 횟수는 대체로 같고, "
            "혼잡도·수단·도보의 구성만 다릅니다.",
            tuple(alternatives)))

    # ---- 블록 C ----------------------------------------------------------
    for row in _block_c_rows():
        alternatives = list(base)
        transit = alternatives[i_t]
        if row["transit_split"]:
            for _ in range(BLOCK_C_LEVELS["transit_split"]):
                transit = split_longest_leg(transit, transfer_walk=0)
        if row["transfer_walk"]:
            transit = add_transfer_walk(transit, BLOCK_C_LEVELS["transfer_walk"])
        alternatives[i_t] = transit
        if row["combo_split"]:
            alternatives[i_c] = split_longest_leg(
                alternatives[i_c], transfer_walk=0)
        tasks.append(ChoiceTask(
            "C", use_case,
            f"{scenario} 환승 횟수와 환승 도보시간이 다릅니다.",
            tuple(alternatives)))

    balanced: list[ChoiceTask] = []
    for block in ("A", "B", "C"):
        balanced.extend(
            balance_block([t for t in tasks if t.block == block], reference))
    assert len(balanced) == 24
    return tuple(balanced)


# ---------------------------------------------------------------------------
# 버전 분할 (응답자당 부담 절감)
# ---------------------------------------------------------------------------
# 블록당 8행을 요인균형이 유지되는 4+4로 쪼갠다. 각 요인이 각 버전에서
# 정확히 2/4회 고수준이 되므로 버전 안에서도 주효과가 직교한다.
# 응답자당 12문항으로 줄이고 응답자를 2배로 모으면 풀링 정보량은 동일하다.
# 개인 수준 추정치는 어차피 사전분포가 지배하므로(복원검증 참고) 12문항으로
# 줄여도 개인 RMSE는 거의 변하지 않는다.

BLOCK_FACTOR_KEYS = {
    "A": ("combo_cost", "taxi_cost", "taxi_time", "transit_time"),
    "B": ("crowd", "bus_share", "access_walk", "combo_taxi"),
    "C": ("transit_split", "transfer_walk", "combo_split"),
}


def _is_high(value) -> bool:
    return value > 0 if isinstance(value, (int, float)) else bool(value)


BLOCK_ROW_BUILDERS = {"A": _block_a_rows, "B": _block_b_rows, "C": _block_c_rows}

# 블록당 문항 수 → 전체 문항 수. 어느 쪽이든 각 요인이 균형을 유지한다.
LENGTH_OPTIONS = {
    "짧게 (6문항)": 2,
    "보통 (12문항)": 4,
    "길게 (24문항)": 8,
}


def balanced_partition(block: str, size: int) -> list[list[int]]:
    """8행을 size개씩, 각 요인이 절반씩 고수준이 되도록 나눈 그룹 목록.

    size=8 → 전체 1개, size=4 → 2개, size=2 → 4개(대칭쌍).
    각 그룹 안에서 모든 요인이 균형을 이루므로 주효과 직교성이 유지된다.
    """
    rows = BLOCK_ROW_BUILDERS[block]()
    keys = BLOCK_FACTOR_KEYS[block]
    total = len(rows)
    if size >= total:
        return [list(range(total))]

    def balanced(group) -> bool:
        return all(sum(1 for i in group if _is_high(rows[i][k])) == size // 2
                   for k in keys)

    groups: list[list[int]] = []
    remaining = set(range(total))
    while remaining:
        found = next(
            (list(c) for c in itertools.combinations(sorted(remaining), size)
             if balanced(c)), None)
        if found is None:
            raise RuntimeError(f"블록 {block}: {size}행 균형 분할 실패.")
        groups.append(found)
        remaining -= set(found)
    return groups


def select_subset(tasks: tuple[ChoiceTask, ...], per_block: int,
                  index: int) -> tuple[ChoiceTask, ...]:
    """블록당 per_block 문항을, index 번째 그룹으로 뽑는다."""
    if per_block >= 8:
        return tasks
    selected: list[ChoiceTask] = []
    for block in ("A", "B", "C"):
        rows = [t for t in tasks if t.block == block]
        groups = balanced_partition(block, per_block)
        selected.extend(rows[i] for i in groups[index % len(groups)])
    return tuple(selected)


# ---------------------------------------------------------------------------
# 성의 검사 문항 (trap)
# ---------------------------------------------------------------------------
# 한 대안이 모든 속성에서 명백히 열등한 카드를 하나 섞는다. 주의를 기울인
# 응답자는 절대 고르지 않으므로, 이 대안을 고른 응답은 밀어찍기로 본다.
# 이 문항은 추정에 사용하지 않는다(block="Q").

def build_trap_task(use_case: str) -> ChoiceTask:
    base = calibrated_base(use_case)
    transit = next(a for a in base if a.kind == "transit")
    taxi = next(a for a in base if a.kind == "taxi")
    taxi_minutes = taxi.legs[0].minutes
    decoy = Alternative(
        "combo", "B. 지하철+택시", int(taxi.cost * 1.4), (
            Leg("subway", 20, access_walk=taxi.legs[0].access_walk + 3),
            Leg("taxi", taxi_minutes + 3, access_walk=2),
        ))
    return ChoiceTask(
        "Q", use_case,
        f"{SCENARIO_TEXT[use_case]} 요금과 총 소요시간을 비교하세요.",
        (transit, decoy, taxi))


def trap_is_failed(task: ChoiceTask, choice: int) -> bool:
    """열등 대안을 골랐는지. 지배관계로 판정하므로 카드가 바뀌어도 유효하다."""
    vectors = [_attribute_vector(a) for a in task.alternatives]
    picked = vectors[choice]
    return any(
        np.all(other <= picked) and np.any(other < picked)
        for index, other in enumerate(vectors) if index != choice
    )


# ---------------------------------------------------------------------------
# 응답 품질 검사
# ---------------------------------------------------------------------------

STRAIGHTLINE_SHARE = 0.80     # 한 대안 비중이 이 이상이면 경고
STRAIGHTLINE_RUN = 8          # 같은 번호 연속이 이 이상이면 경고


def response_quality(tasks: tuple[ChoiceTask, ...], choices: list[int],
                     trap: tuple[ChoiceTask, int] | None = None) -> list[str]:
    warnings: list[str] = []
    if not choices:
        return ["응답이 없습니다."]

    counts = np.bincount(choices, minlength=3)
    share = counts.max() / len(choices)
    if share >= STRAIGHTLINE_SHARE:
        name = tasks[0].alternatives[int(counts.argmax())].name
        warnings.append(
            f"한 대안({name}) 비중이 {share:.0%}입니다. "
            "밀어찍기이거나, 이 응답자에게는 카드가 균형점에서 크게 벗어나 있습니다.")

    longest = current = 1
    for previous, nxt in zip(choices, choices[1:]):
        current = current + 1 if nxt == previous else 1
        longest = max(longest, current)
    if longest >= STRAIGHTLINE_RUN:
        warnings.append(f"같은 번호를 {longest}회 연속 선택했습니다.")

    if trap is not None and trap_is_failed(*trap):
        warnings.append("성의 검사 문항에서 명백히 열등한 대안을 골랐습니다.")

    # 블록 B는 요금·총시간이 거의 같고 구성만 다르다. 여기서 응답이 전혀
    # 갈리지 않으면 구성 속성을 아예 보지 않았을 가능성이 높다.
    block_b = [c for t, c in zip(tasks, choices) if t.block == "B"]
    if len(block_b) >= 4 and len(set(block_b)) == 1:
        warnings.append(
            "블록 B에서 선택이 전혀 갈리지 않았습니다. "
            "혼잡·수단·도보 구성을 보지 않았을 수 있습니다(속성 무시).")
    return warnings


# ---------------------------------------------------------------------------
# 모형
# ---------------------------------------------------------------------------

# 0 log_beta  1 log_vot  2 subway_crowded  3 bus  4 taxi
# 5 walk      6 transfer_min             7 asc_combo  8 asc_taxi
PARAM_NAMES = ["log_beta", "log_vot", "subway_crowded", "bus", "taxi",
               "walk", "transfer_min", "asc_combo", "asc_taxi"]
N_PARAMS = len(PARAM_NAMES)


def prior_theta(vot_direct: float) -> np.ndarray:
    return np.array([
        np.log(0.10), np.log(vot_direct),
        np.log(DEFAULTS["subway_crowded"] - 1.0), np.log(DEFAULTS["bus"]),
        logit(DEFAULTS["taxi"]) if TAXI_WEIGHT_BOUNDED else np.log(DEFAULTS["taxi"]),
        np.log(DEFAULTS["walk"] - 1.0),
        np.log(DEFAULTS["transfer_min"]), 0.0, 0.0,
    ])


def theta_to_parameters(theta: np.ndarray) -> dict:
    return {
        "beta": float(np.exp(theta[0])),
        "vot": float(np.exp(theta[1])),
        "weights": {
            "subway_uncrowded": 1.0,
            "subway_crowded": float(1.0 + np.exp(theta[2])),
            "bus": float(np.exp(theta[3])),
            "taxi": float(expit(theta[4]) if TAXI_WEIGHT_BOUNDED
                          else np.exp(theta[4])),
            "walk": float(1.0 + np.exp(theta[5])),
        },
        "transfer_min": float(np.exp(theta[6])),
        "asc": {"transit": 0.0, "combo": float(theta[7]), "taxi": float(theta[8])},
    }


def equivalent_time(alternative: Alternative, parameters: dict) -> float:
    w = parameters["weights"]
    return (
        alternative.subway_uncrowded * w["subway_uncrowded"]
        + alternative.subway_crowded * w["subway_crowded"]
        + alternative.bus * w["bus"]
        + alternative.taxi * w["taxi"]
        + alternative.walk * w["walk"]
        + alternative.transfers * parameters["transfer_min"]
    )


def systematic_utility(alternative: Alternative, parameters: dict) -> float:
    generalized = (equivalent_time(alternative, parameters)
                   + alternative.cost / parameters["vot"])
    return parameters["asc"][alternative.kind] - parameters["beta"] * generalized


def choice_probabilities(task: ChoiceTask, parameters: dict) -> np.ndarray:
    utility = np.array([systematic_utility(a, parameters) for a in task.alternatives])
    return np.exp(utility - logsumexp(utility))


FIELD_TO_PARAM = {"subway_crowded": "subway_crowded", "bus": "bus", "taxi": "taxi",
                  "walk": "walk", "transfers": "transfer_min"}


def active_parameter_mask(tasks: tuple[ChoiceTask, ...]) -> np.ndarray:
    mask = np.ones(N_PARAMS, dtype=bool)
    for field, name in FIELD_TO_PARAM.items():
        spread = sum(
            max(getattr(a, field) for a in t.alternatives)
            - min(getattr(a, field) for a in t.alternatives) for t in tasks)
        if spread < 1e-9:
            mask[PARAM_NAMES.index(name)] = False
    kinds = {a.kind for t in tasks for a in t.alternatives}
    if "combo" not in kinds:
        mask[PARAM_NAMES.index("asc_combo")] = False
    if "taxi" not in kinds:
        mask[PARAM_NAMES.index("asc_taxi")] = False
    return mask


OUTPUT_LABELS = ["vot", "subway_crowded", "bus", "taxi", "walk",
                 "transfer_min"]


def _outputs(theta: np.ndarray) -> np.ndarray:
    p = theta_to_parameters(theta)
    w = p["weights"]
    return np.array([p["vot"], w["subway_crowded"], w["bus"], w["taxi"],
                     w["walk"], p["transfer_min"]])


def estimate(tasks: tuple[ChoiceTask, ...], choices: list[int],
             vot_direct: float, fix_vot: bool = False) -> dict:
    """fix_vot=True 이면 VOT를 지불사다리 응답값에 고정하고 나머지만 추정한다.

    파라미터가 하나 줄어 다른 값들의 분산이 작아지지만, 진술 VOT가 틀리면
    그 오차가 다른 파라미터로 새어 들어간다. 대가는 --votcheck 로 잴 수 있다.
    """
    theta0 = prior_theta(vot_direct)
    sd = np.array([PRIOR_SD[name] for name in PARAM_NAMES])
    mask = active_parameter_mask(tasks)
    if fix_vot:
        mask[PARAM_NAMES.index("log_vot")] = False
    free = np.where(mask)[0]

    def expand(free_theta: np.ndarray) -> np.ndarray:
        theta = theta0.copy()
        theta[free] = free_theta
        return theta

    def negative_log_posterior(free_theta: np.ndarray) -> float:
        theta = expand(free_theta)
        parameters = theta_to_parameters(theta)
        total = 0.0
        for task, choice in zip(tasks, choices):
            utility = np.array(
                [systematic_utility(a, parameters) for a in task.alternatives])
            total += logsumexp(utility) - utility[choice]
        penalty = np.sum(((theta[free] - theta0[free]) / sd[free]) ** 2) / 2.0
        return float(total + penalty)

    result = minimize(negative_log_posterior, x0=theta0[free], method="BFGS",
                      options={"maxiter": 5000, "gtol": 1e-5})
    theta_hat = expand(result.x)
    covariance = np.zeros((N_PARAMS, N_PARAMS))
    covariance[np.ix_(free, free)] = np.atleast_2d(result.hess_inv)

    step = 1e-5
    jacobian = np.zeros((len(OUTPUT_LABELS), N_PARAMS))
    for index in range(N_PARAMS):
        plus, minus = theta_hat.copy(), theta_hat.copy()
        plus[index] += step
        minus[index] -= step
        jacobian[:, index] = (_outputs(plus) - _outputs(minus)) / (2 * step)
    output_cov = jacobian @ covariance @ jacobian.T

    return {
        "parameters": theta_to_parameters(theta_hat),
        "posterior_sd": {label: float(np.sqrt(max(output_cov[i, i], 0.0)))
                         for i, label in enumerate(OUTPUT_LABELS)},
        "inactive": [PARAM_NAMES[i] for i in range(N_PARAMS) if not mask[i]],
        "converged": bool(result.success),
    }


# ---------------------------------------------------------------------------
# 설계 진단
# ---------------------------------------------------------------------------

ATTRIBUTE_LABELS = ["cost", "subway_unc", "subway_crd", "bus", "taxi",
                    "acc_walk", "trf_walk", "transfers"]


def _attribute_vector(a: Alternative) -> np.ndarray:
    return np.array([a.cost, a.subway_uncrowded, a.subway_crowded, a.bus, a.taxi,
                     a.access_walk + a.egress_walk, a.transfer_walk,
                     a.transfers], dtype=float)

@dataclass
class PersonalProfile:
    level: int
    age_group: str
    purpose: str
    use_case: str | None
    vot_direct_krw_per_min: float
    vot_posterior_krw_per_min: float
    weights_relative_to_uncrowded_subway: dict[str, float]
    transfer_cost_krw: float | None
    posterior_sd: dict[str, float]
    inactive_parameters: list[str]
    created_at: str


def _assemble_profile(level, age_group, purpose, use_case, vot_direct,
                      parameters, posterior_sd, inactive) -> PersonalProfile:
    weights = dict(parameters["weights"])
    weights["transfer_min"] = parameters["transfer_min"]
    vot = parameters["vot"]
    return PersonalProfile(
        level=level, age_group=age_group, purpose=purpose, use_case=use_case,
        vot_direct_krw_per_min=vot_direct, vot_posterior_krw_per_min=vot,
        weights_relative_to_uncrowded_subway=weights,
        transfer_cost_krw=None if "transfer_min" in inactive
        else parameters["transfer_min"] * vot,
        posterior_sd=posterior_sd, inactive_parameters=inactive,
        created_at=datetime.now().isoformat(timespec="seconds"))


import base64

_LENGTH_TO_PER_BLOCK = {6: 2, 12: 4, 24: 8}

_AGE_MAP = {
    "10대": "20대 이하",
    "20대": "20대 이하",
    "20대 이하": "20대 이하",
    "30대": "30대",
    "40대": "40대",
    "50대": "50대",
    "50대 이상": "50대",
    "60세 이상": "60세 이상",
}

_PURPOSE_MAP = {
    "통근": "통근",
    "통학": "통근",
    "통근/통학": "통근",
    "쇼핑": "여가",
    "여가": "여가",
    "쇼핑/여가": "여가",
    "업무": "업무",
    "비즈니스": "업무",
    "업무/비즈니스": "업무",
    "의료": "기타",
    "병원": "기타",
    "의료/병원": "기타",
    "기타": "기타",
    "귀가": "귀가",
}

_ENGINE_DEFAULTS = {
    "vot": 400.0,
    "alpha_bus": 1.18,
    "beta_sub": 1.0,
    "beta_sub_c": 1.55,
    "gamma_walk": 2.35,
    "delta_taxi": 0.88,
    "transfer_penalty": 5.0,
}


def _map_age(age_group: str) -> str:
    key = str(age_group).strip()
    if key in _AGE_MAP:
        return _AGE_MAP[key]
    if "10" in key or "20" in key:
        return "20대 이하"
    if "30" in key:
        return "30대"
    if "40" in key:
        return "40대"
    if "50" in key or "60" in key:
        return "50대"
    raise ValueError(f"알 수 없는 연령대: {age_group}")


def _map_purpose(purpose: str) -> str:
    key = str(purpose).strip()
    if key in _PURPOSE_MAP:
        return _PURPOSE_MAP[key]
    if "통근" in key or "통학" in key:
        return "통근"
    if "쇼핑" in key or "여가" in key:
        return "여가"
    if "업무" in key or "비즈니스" in key:
        return "업무"
    return "기타"


def _encode_token(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_token(token: str) -> dict:
    padded = token + "=" * ((4 - len(token) % 4) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    if not isinstance(data, dict):
        raise ValueError("설문 토큰을 확인할 수 없습니다.")
    return data


def _per_block_from_length(length: int) -> int:
    if length in _LENGTH_TO_PER_BLOCK:
        return _LENGTH_TO_PER_BLOCK[int(length)]
    if int(length) in LENGTH_OPTIONS.values():
        return int(length)
    raise ValueError("문항 수는 6, 12, 24 중 하나여야 합니다.")


def _build_sequence(use_case: str, per_block: int, version: int, trap_position: int | None = None):
    full = build_tasks(use_case)
    tasks = select_subset(full, per_block, version)
    return tasks, None, list(tasks)


def _serialize_alternative(index: int, alternative: Alternative) -> dict:
    return {
        "index": index,
        "name": alternative.name,
        "cost": int(alternative.cost),
        "total_min": int(alternative.door_to_door_time),
        "transfers": int(alternative.transfers),
        "in_vehicle_min": int(alternative.in_vehicle_time),
        "walk_min": int(alternative.walk),
        "legs": [
            {
                "mode": leg.mode,
                "minutes": int(leg.minutes),
                "crowded_minutes": int(leg.crowded_minutes),
                "access_walk": int(leg.access_walk),
            }
            for leg in alternative.legs
        ],
    }


def _serialize_card(number: int, task: ChoiceTask) -> dict:
    return {
        "number": number,
        "alternatives": [
            _serialize_alternative(index, alternative)
            for index, alternative in enumerate(task.alternatives)
        ],
    }


def _route_params(parameters: dict, inactive: list[str]) -> dict:
    inactive_set = set(inactive or [])
    weights = parameters.get("weights") or {}
    params = {
        "vot": float(parameters["vot"]),
        "alpha_bus": float(weights["bus"]),
        "beta_sub": 1.0,
        "beta_sub_c": float(weights["subway_crowded"]),
        "gamma_walk": float(weights["walk"]),
        "delta_taxi": float(weights["taxi"]),
        "transfer_penalty": float(parameters["transfer_min"]),
    }
    if "log_vot" in inactive_set:
        params["vot"] = _ENGINE_DEFAULTS["vot"]
    if "bus" in inactive_set:
        params["alpha_bus"] = _ENGINE_DEFAULTS["alpha_bus"]
    if "subway_crowded" in inactive_set:
        params["beta_sub_c"] = _ENGINE_DEFAULTS["beta_sub_c"]
    if "walk" in inactive_set:
        params["gamma_walk"] = _ENGINE_DEFAULTS["gamma_walk"]
    if "taxi" in inactive_set:
        params["delta_taxi"] = _ENGINE_DEFAULTS["delta_taxi"]
    if "transfer_min" in inactive_set:
        params["transfer_penalty"] = _ENGINE_DEFAULTS["transfer_penalty"]
    return params


def create_survey(age_group, purpose, vot_direct, length) -> dict:
    age = _map_age(age_group)
    mapped_purpose = _map_purpose(purpose)
    per_block = _per_block_from_length(int(length))
    vot = float(vot_direct)
    use_case = random.choice(USE_CASES)
    group_count = len(balanced_partition("A", per_block))
    version = random.randrange(group_count)
    full = build_tasks(use_case)
    tasks = select_subset(full, per_block, version)
    token = _encode_token({
        "use_case": use_case,
        "per_block": per_block,
        "version": version,
        "age_group": age,
        "purpose": mapped_purpose,
        "vot_direct": vot,
    })
    return {
        "survey_token": token,
        "scenario_text": SCENARIO_TEXT[use_case],
        "cards": [_serialize_card(number, task) for number, task in enumerate(tasks, start=1)],
    }


def estimate_profile(survey_token: str, responses: list[int]) -> dict:
    payload = _decode_token(survey_token)
    use_case = payload["use_case"]
    per_block = int(payload["per_block"])
    version = int(payload["version"])
    age_group = payload["age_group"]
    purpose = payload["purpose"]
    vot_direct = float(payload["vot_direct"])
    tasks, _, sequence = _build_sequence(use_case, per_block, version)
    if len(responses) != len(sequence):
        raise ValueError(f"응답 수가 문항 수와 다릅니다. ({len(responses)}/{len(sequence)})")
    choices = [int(x) for x in responses]
    outcome = estimate(tasks, choices, vot_direct, fix_vot=FIX_VOT_TO_STATED)
    profile = _assemble_profile(
        2, age_group, purpose, use_case, vot_direct,
        outcome["parameters"], outcome["posterior_sd"], outcome["inactive"],
    )
    quality_warnings = response_quality(tasks, choices, None)
    return {
        "profile": asdict(profile),
        "quality_warnings": quality_warnings,
        "trap_failed": False,
        "route_params": _route_params(outcome["parameters"], outcome["inactive"]),
    }

