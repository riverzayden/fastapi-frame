"""작업 실행 인프라 — 워커풀, 하트비트, 워치독, 재시작 복구, 상태 데코레이터.

복잡한 건 전부 여기 모여 있습니다. 평소에는 열어볼 일이 없어야 정상입니다.
비즈니스 로직은 core.py 에만 쓰세요.
"""

import functools
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from app.config import settings
from app.jobs import db
from app.jobs.deadline import Deadline, JobCancelled, JobTimeout, clamp_timeout

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

_executor: Optional[ThreadPoolExecutor] = None
_shutdown = threading.Event()

# job_type -> (핸들러, payload 파서). runtime.register() 로 채웁니다.
_handlers: dict[str, tuple[Callable, Callable]] = {}


# ── 실행 중인 작업 레지스트리 ────────────────────────────────────

class _Registry:
    """지금 이 프로세스에서 도는 작업들. 하트비트와 워치독, 취소 API 가 참조합니다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Deadline] = {}

    def add(self, job_id: str, dl: Deadline) -> None:
        with self._lock:
            self._jobs[job_id] = dl

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def get(self, job_id: str) -> Optional[Deadline]:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self) -> list[tuple[str, Deadline]]:
        with self._lock:
            return list(self._jobs.items())

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)


registry = _Registry()


# ── 상태 데코레이터 ──────────────────────────────────────────────

def track_status(fn: Callable) -> Callable:
    """시작/끝 DB 상태 기록 + 시간 제한을 한 겹에 묶습니다.

    감싼 메서드는 (self, data, deadline) 로 호출되고,
    바깥에서는 (self, job_id, data) 로 부릅니다.
    """

    @functools.wraps(fn)
    def wrapper(self, job_id: str, data, *args, **kwargs):
        if not db.claim(job_id, owner=WORKER_ID):
            logger.info("job %s 는 다른 워커가 처리 중 — 건너뜀", job_id)
            return None

        dl = Deadline(
            clamp_timeout(getattr(data, "timeout_sec", None), settings.job_timeout_sec)
        )
        registry.add(job_id, dl)
        try:
            result = fn(self, data, dl, *args, **kwargs)
        except JobTimeout as e:
            logger.warning("job %s 시간 초과: %s", job_id, e)
            db.finish(job_id, "T", error=str(e))
            return None
        except JobCancelled:
            logger.info("job %s 취소됨", job_id)
            db.finish(job_id, "C", error="cancelled")
            return None
        except Exception as e:                       # noqa: BLE001
            logger.exception("job %s 실패", job_id)
            db.finish(job_id, "F", error=f"{type(e).__name__}: {e}")
            return None
        else:
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            db.finish(job_id, "S", result=payload)
            return result
        finally:
            registry.remove(job_id)

    return wrapper


# ── 백그라운드 스레드 ────────────────────────────────────────────

def _heartbeat_loop() -> None:
    while not _shutdown.is_set():
        try:
            db.heartbeat(registry.ids())
        except Exception:                            # noqa: BLE001
            logger.exception("하트비트 갱신 실패")
        _shutdown.wait(settings.heartbeat_interval_sec)


def _watchdog_loop() -> None:
    """협조적 취소가 안 먹히는 경우의 안전망.

    작업 스레드가 C 확장 안에 갇혀 신호를 못 받아도 DB 상태만큼은 정확해집니다.
    'thread leaked' 로그가 보이면 그 구간에 dl.check() 가 빠진 것입니다.
    """
    while not _shutdown.is_set():
        for job_id, dl in registry.snapshot():
            if not dl.expired:
                continue
            if not dl.cancelled:
                logger.warning("job %s 기한 초과 — 취소 신호 발송", job_id)
                dl.cancel()
            elif dl.remaining < -settings.cancel_grace_sec:
                logger.error("job %s 가 취소를 무시함 (thread leaked)", job_id)
                db.finish(job_id, "T", error="timeout (thread leaked)")
                registry.remove(job_id)
        _shutdown.wait(settings.watchdog_interval_sec)


# ── 공개 API ─────────────────────────────────────────────────────

def register(job_type: str, handler: Callable, parse: Callable[[dict[str, Any]], Any]) -> None:
    """작업 종류를 등록합니다. 재시작 복구가 이 표를 보고 핸들러를 찾습니다.

        runtime.register("followup", service.process_and_store, FollowUpRequest.model_validate)

    등록하지 않으면 복구 시 "핸들러 없음" 으로 건너뜁니다.
    """
    _handlers[job_type] = (handler, parse)
    logger.info("작업 종류 등록: %s", job_type)


def submit(job_type: str, job_id: str, data) -> None:
    """이미 DB 에 등록된 작업을 워커 스레드에 투입합니다."""
    if _executor is None:
        raise RuntimeError("runtime.start() 를 먼저 호출하세요")
    if job_type not in _handlers:
        raise KeyError(f"등록되지 않은 작업 종류: {job_type} — runtime.register() 를 먼저 호출하세요")
    handler, _ = _handlers[job_type]
    _executor.submit(handler, job_id, data)


def enqueue_and_submit(job_type: str, data) -> str:
    """★ 백그라운드 작업을 시작하는 유일한 정식 경로입니다.

    ① DB 에 먼저 적고 ② 그 다음 실행 — 이 순서라야 재시작 복구가 성립합니다.
    threading.Thread(...).start() 로 직접 띄우면 복구·타임아웃·취소·하트비트가
    전부 걸리지 않습니다.
    """
    payload = data.model_dump() if hasattr(data, "model_dump") else dict(data)
    job_id = db.enqueue(payload, job_type)
    submit(job_type, job_id, data)
    return job_id


def cancel(job_id: str) -> bool:
    dl = registry.get(job_id)
    if dl is None:
        return False
    dl.cancel()
    return True


def recover() -> int:
    """재시작 복구: 죽은 워커가 남긴 R 을 되살려 다시 큐에 넣습니다."""
    revived = db.revive_stale()
    dropped = db.fail_exhausted()
    queued = db.fetch_queued(settings.recover_batch_size)
    resubmitted = 0
    for job in queued:
        entry = _handlers.get(job["job_type"])
        if entry is None:
            logger.error(
                "job %s: 등록되지 않은 종류 '%s' — 건너뜀 (runtime.register 확인)",
                job["id"], job["job_type"],
            )
            continue
        handler, parse = entry
        _executor.submit(handler, job["id"], parse(job["payload"]))
        resubmitted += 1
    if revived or dropped or resubmitted:
        logger.info(
            "복구: R→Q %d건, 재시도포기 %d건, 재투입 %d건", revived, dropped, resubmitted
        )
    return resubmitted


def start() -> None:
    """워커풀·하트비트·워치독 기동 + 재시작 복구.

    register() 를 모두 마친 뒤에 호출하세요 (복구가 핸들러 표를 참조합니다).
    """
    global _executor
    db.init()
    _executor = ThreadPoolExecutor(
        max_workers=settings.job_max_workers, thread_name_prefix="followup"
    )
    threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()
    threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True).start()
    if settings.recover_on_startup:
        recover()


def stop() -> None:
    _shutdown.set()
    for _, dl in registry.snapshot():
        dl.cancel()                    # 종료 중인 작업들에게 알림
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
