from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.regret_engine import rank_routes
from core.route_engine import analyze_routes
from core.sp_engine import create_survey, estimate_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="OPTI Route API")

JOB_TTL_SEC = 10 * 60
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_analyze_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analyze-job")


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
    depart_time: str | None = Field(default=None)
    params: dict | None = None


class RankLimitsIn(BaseModel):
    max_time_min: float | None = None
    max_cost_krw: float | None = None
    max_transfers: float | None = None
    arrive_by: str | None = None


class RankBetasIn(BaseModel):
    gc: float | None = None
    knee: float | None = None


class RankIn(BaseModel):
    candidates: list[dict]
    limits: RankLimitsIn
    weights: dict[str, float] | None = None
    betas: RankBetasIn | dict | None = None
    importance: dict | None = None
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


def _depart_from_body(body: AnalyzeIn) -> datetime:
    return _parse_depart(body.depart_time or body.departTime)


def _purge_jobs():
    now = time.time()
    with _jobs_lock:
        stale = [job_id for job_id, rec in _jobs.items() if now - rec["created"] > JOB_TTL_SEC]
        for job_id in stale:
            _jobs.pop(job_id, None)


def _set_job(job_id: str, **fields):
    with _jobs_lock:
        rec = _jobs.get(job_id)
        if rec is None:
            return
        rec.update(fields)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/routes/analyze")
def routes_analyze(body: AnalyzeIn):
    try:
        return analyze_routes(
            origin=body.origin.model_dump(),
            dest=body.dest.model_dump(),
            depart_dt=_depart_from_body(body),
            params=body.params,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/routes/analyze/jobs")
def routes_analyze_job_create(body: AnalyzeIn):
    _purge_jobs()
    job_id = uuid4().hex
    rec = {
        "status": "running",
        "progress": 0,
        "stage": "서버를 깨우는 중이에요",
        "result": None,
        "error": None,
        "created": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = rec

    origin = body.origin.model_dump()
    dest = body.dest.model_dump()
    depart_dt = _depart_from_body(body)
    params = body.params

    def run():
        def on_progress(progress: int, stage: str):
            _set_job(job_id, progress=progress, stage=stage)

        try:
            result = analyze_routes(
                origin=origin,
                dest=dest,
                depart_dt=depart_dt,
                params=params,
                progress_cb=on_progress,
            )
            _set_job(job_id, status="done", progress=100, result=result, stage="경로를 비교하고 있어요")
        except Exception as exc:
            _set_job(job_id, status="error", error=str(exc), stage="경로를 비교하고 있어요")

    _analyze_pool.submit(run)
    return {"job_id": job_id}


@app.get("/routes/analyze/jobs/{job_id}")
def routes_analyze_job_get(job_id: str):
    _purge_jobs()
    with _jobs_lock:
        rec = _jobs.get(job_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        return {
            "status": rec["status"],
            "progress": rec["progress"],
            "stage": rec["stage"],
            "result": rec["result"],
            "error": rec.get("error"),
        }


@app.post("/routes/rank")
def routes_rank(body: RankIn):
    try:
        betas = None
        if body.betas is not None:
            raw = body.betas.model_dump() if hasattr(body.betas, "model_dump") else dict(body.betas)
            betas = {k: v for k, v in raw.items() if v is not None} or None
        return rank_routes(
            candidates=body.candidates,
            limits=body.limits.model_dump(),
            weights=body.weights,
            psi=0.0 if body.psi is None else body.psi,
            betas=betas,
            importance=body.importance,
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
