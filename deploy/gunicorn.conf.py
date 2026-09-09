"""gunicorn 운영 설정 (Linux 전용).

    gunicorn -c deploy/gunicorn.conf.py main:app

★ Windows 에서는 gunicorn 이 import 조차 되지 않습니다 (fcntl 의존).
  윈도우 개발/테스트는 uvicorn 을 직접 쓰세요:
      python -m uvicorn main:app --host 0.0.0.0 --port 8000
"""

import multiprocessing
import os
import sys

# 저장소 루트를 import 경로에 추가 (gunicorn -c deploy/gunicorn.conf.py 로 실행)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402

# ── 바인딩 ────────────────────────────────────────────────────────
bind = os.getenv("BIND", "0.0.0.0:8000")
backlog = 2048

# ── 워커 ──────────────────────────────────────────────────────────
# FastAPI(ASGI) 는 uvicorn 워커로 돌립니다. pip install uvicorn-worker
worker_class = "uvicorn_worker.UvicornWorker"
# Flask(WSGI) 라면 아래 두 줄로 교체:
#   worker_class = "gthread"
#   threads = 8

workers = int(os.getenv("WEB_CONCURRENCY", max(1, multiprocessing.cpu_count() // 2)))

# ⚠️ 실제 동시 작업 수 = workers × FOLLOWUP_JOB_MAX_WORKERS
#    workers=4, JOB_MAX_WORKERS=16 이면 최대 64개가 동시에 돕니다.
#    DB 커넥션과 메모리를 여기에 맞춰 잡으세요.

# ── 타임아웃 ──────────────────────────────────────────────────────
# gthread/sync 워커에서는 '요청 처리 시간 상한' 입니다. 기본 30초라 Real(5분)이 죽습니다.
# UvicornWorker 에서는 워커 하트비트 기준이라 긴 요청이 곧바로 죽지는 않지만,
# 이벤트 루프가 막히면 하트비트도 멈추므로 넉넉히 잡는 편이 안전합니다.
timeout = settings.real_timeout_sec + 60

# 배포 시 진행 중인 요청이 끝날 시간을 줍니다.
# ⚠️ 백그라운드 작업(최대 1시간)까지 기다려주지는 않습니다.
#    남은 작업은 종료 후 R 로 남고, 다음 기동 때 복구 로직이 다시 실행합니다.
graceful_timeout = int(os.getenv("GRACEFUL_TIMEOUT", "60"))
keepalive = 5

# ── ★ 이 두 개는 기본값을 유지하세요 ──────────────────────────────
# 워커를 주기적으로 재활용하면 실행 중인 백그라운드 작업이 통째로 죽습니다.
max_requests = 0
max_requests_jitter = 0

# preload 를 켜면 마스터에서 앱을 만든 뒤 fork 합니다.
# 스레드·DB 커넥션·matplotlib 상태가 fork 를 건너가면서 깨질 수 있습니다.
preload_app = False

# ── 로그 ──────────────────────────────────────────────────────────
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'


def on_starting(server):
    server.log.info(
        "followup 기동: workers=%s job_max_workers=%s job_timeout=%ss real_timeout=%ss",
        workers, settings.job_max_workers, settings.job_timeout_sec, settings.real_timeout_sec,
    )
