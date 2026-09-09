"""FastAPI 진입점. 라우트는 얇게 — 로직은 전부 service/core 에 있습니다.

실행:
    uvicorn main:app --reload
    uvicorn main:app --workers 4        # 운영
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import plotting
from app.jobs import db, runtime
from app.jobs.deadline import JobTimeout
from app.schemas import FollowUpRequest, FollowUpResult, JobAccepted, JobView
from app.service import JOB_TYPE, service


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    )
    plotting.warmup()
    # 워커풀 · 하트비트 · 워치독 기동 + 재시작 복구(R → Q → 재실행).
    # 작업 종류 등록(runtime.register)은 app.service import 시점에 끝나 있습니다.
    runtime.start()
    yield
    runtime.stop()


app = FastAPI(title="followup", lifespan=lifespan)


@app.exception_handler(JobTimeout)
async def _timeout_handler(request: Request, exc: JobTimeout):
    return JSONResponse(status_code=504, content={"detail": str(exc)})


# ── After: 비동기. DB 에 먼저 적고 → 202 → 워커가 처리 ──────────
@app.post("/followafter", response_model=JobAccepted, status_code=202)
def follow_after(data: FollowUpRequest):
    # DB 등록(①) → 워커 투입(②) 을 한 번에. 백그라운드 실행의 유일한 정식 경로입니다.
    job_id = runtime.enqueue_and_submit(JOB_TYPE, data)
    return JobAccepted(job_id=job_id, status="Q")


# ── Real: 동기. 결과를 그대로 응답 ────────────────────────────
#    ⚠️ async def 로 바꾸지 마세요. 동기 코드가 이벤트 루프를 통째로 막습니다.
#       def 로 두면 FastAPI 가 알아서 스레드풀에서 돌립니다.
@app.post("/followreal", response_model=FollowUpResult)
def follow_real(data: FollowUpRequest):
    return service.process(data)


# ── 조회 / 취소 ────────────────────────────────────────────────
@app.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str):
    job = db.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobView(**job)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if runtime.cancel(job_id):
        return {"status": "cancelling"}
    raise HTTPException(status_code=404, detail="이 프로세스에서 실행 중이 아닙니다")


@app.get("/health")
def health():
    return {"status": "ok", "running": len(runtime.registry.ids())}
