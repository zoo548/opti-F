from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.regret_engine import rank_routes
from core.route_engine import analyze_routes
from core.sp_engine import create_survey, estimate_profile

app = FastAPI(title="OPTI Route API")


def _allowed_origins() -> list[str]:
    origins = [
        "http://localhost:8443",
        "http://127.0.0.1:8443",
        "https://opti-5xg1.vercel.app",
    ]
    extra = os.environ.get("ALLOWED_ORIGINS", "")
    for part in extra.split(","):
        origin = part.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlaceIn(BaseModel):
    name: str = ""
    lat: float
    lng: float


class AnalyzeIn(BaseModel):
    origin: PlaceIn
    dest: PlaceIn
    departTime: str | None = Field(default=None)
    params: dict | None = None


class RankLimitsIn(BaseModel):
    max_time_min: float | None = None
    max_cost_krw: float | None = None
    max_transfers: float | None = None
    arrive_by: str | None = None


class RankIn(BaseModel):
    candidates: list[dict]
    limits: RankLimitsIn
    weights: dict[str, float] | None = None
    psi: float | None = Field(default=0.0)


class SurveyIn(BaseModel):
    age_group: str
    purpose: str
    vot_direct: float
    length: int


class EstimateIn(BaseModel):
    survey_token: str
    responses: list[int]


def _parse_depart(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="출발 시각 형식을 확인할 수 없습니다.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/routes/analyze")
def routes_analyze(body: AnalyzeIn):
    try:
        return analyze_routes(
            origin=body.origin.model_dump(),
            dest=body.dest.model_dump(),
            depart_dt=_parse_depart(body.departTime),
            params=body.params,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/routes/rank")
def routes_rank(body: RankIn):
    try:
        return rank_routes(
            candidates=body.candidates,
            limits=body.limits.model_dump(),
            weights=body.weights,
            psi=0.0 if body.psi is None else body.psi,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/sp/tasks")
def sp_tasks(body: SurveyIn):
    try:
        return create_survey(
            age_group=body.age_group,
            purpose=body.purpose,
            vot_direct=body.vot_direct,
            length=body.length,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/sp/estimate")
def sp_estimate(body: EstimateIn):
    try:
        return estimate_profile(body.survey_token, body.responses)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
