# fastapi-frame

같은 처리 로직을 **동기 응답(real)** 과 **백그라운드 작업(after)** 양쪽으로 서비스하기 위한 FastAPI 골격입니다.

- 공통 로직은 `app/core.py` **한 곳**에만 — 복붙 없음
- 백그라운드 작업은 **재시작해도 복구**됩니다 (프로세스가 죽어도 작업이 사라지지 않음)
- 작업당 **시간 제한**과 **취소** — `sleep` 중에도 즉시 반응
- matplotlib **스레드 안전** 처리
- 튜닝 값은 전부 `app/config.py` 한 파일 (환경변수로 덮어쓰기)

> 설계 배경과 수정 방법은 **[docs/GUIDE.md](docs/GUIDE.md)** 를 보세요.
> 새 작업 추가, DB 교체, 지켜야 할 규칙이 정리되어 있습니다.

---

## 구조

```
main.py                  라우트
app/
  config.py         ★    모든 설정값
  schemas.py             요청/응답 모델
  core.py           ★★   after/real 공통 로직 — 평소 작업은 여기
  service.py             무거운 초기화 + After/Real 진입점
  plotting.py            matplotlib 스레드 안전 래퍼
  jobs/                  실행 인프라 (거의 열어볼 일 없음)
    db.py                작업 저장소 — DB 교체 시 이 파일만
    runtime.py           워커풀·하트비트·워치독·복구
    deadline.py          시간 제한 / 협조적 취소
tests/                   test_pipeline.py (파이프라인) · test_api.py (HTTP)
deploy/gunicorn.conf.py  리눅스 운영 설정
```

### 왜 이렇게 나눴나

원래는 `FollowUpAfter` / `FollowUpReal` 두 클래스로 나뉘어 있었는데, 둘의 차이는
"무슨 일을 하는가" 가 아니라 **"결과를 어디로 보내는가"** 뿐이었습니다.
상속으로 나눌 축이 아니어서 공통 로직이 양쪽에 복사될 수밖에 없었습니다.

나누는 축을 **생명주기**로 바꿨습니다.

| 원래 | 지금 |
|---|---|
| `FollowUpBase` → `After` / `Real` | `FollowUpService` — 프로세스당 1개, 무거운 초기화 |
| 공통 로직이 두 곳에 복사됨 | `FollowUpTask` — 요청당 1개, 공통 로직 **한 곳** |
| `Thread` 상속 (Real 은 리턴이 필요한데 `Thread.run()` 리턴값은 버려짐) | 실행 방식은 라우트가 결정 |
| After/Real 차이 = `run()` 통째로 | After/Real 차이 = `@track_status` **한 줄** |

---

## 설치

```bash
conda create -n frame2 python=3.11 -y
conda activate frame2
pip install -r requirements.txt
```

## 실행 (Windows / Linux 공통)

```bash
python -m uvicorn main:app --reload
```

문서: http://127.0.0.1:8000/docs

## 테스트

```bash
python tests/test_pipeline.py
```
```bash
python tests/test_api.py
```

`test_pipeline.py` 는 서버 없이 정상 처리 / 시간 제한 / 취소 / **재시작 복구** / 동시 실행 /
matplotlib 동시 렌더를 검증합니다. `test_api.py` 는 HTTP 레벨(202·504·404·422·취소)을 확인합니다.

---

## API

| | |
|---|---|
| `POST /followafter` | 백그라운드 처리. **202** + `job_id` 즉시 반환 |
| `POST /followreal` | 동기 처리. 결과를 그대로 응답 (기본 제한 5분, 초과 시 **504**) |
| `GET /jobs/{job_id}` | 상태 조회 — `Q` 대기 / `R` 실행중 / `S` 성공 / `F` 실패 / `T` 시간초과 / `C` 취소 |
| `POST /jobs/{job_id}/cancel` | 실행 중 작업 취소 |
| `GET /health` | 상태 + 현재 실행 중인 작업 수 |

```bash
curl -X POST http://127.0.0.1:8000/followafter -H "Content-Type: application/json" -d "{\"target_id\":\"a-1\"}"
```

---

## 운영 (Linux)

```bash
pip install -r requirements-prod.txt
gunicorn -c deploy/gunicorn.conf.py main:app
```

`deploy/gunicorn.conf.py` 에 이미 반영된 것들:

| 설정 | 값 | 이유 |
|---|---|---|
| `worker_class` | `uvicorn_worker.UvicornWorker` | FastAPI 는 ASGI 라 gunicorn 기본 워커로는 못 돕니다 |
| `timeout` | `real_timeout_sec + 60` | **기본 30초.** gthread/sync 워커에서는 요청 시간 상한이라 5분짜리 Real 이 죽습니다. UvicornWorker 에서는 하트비트 기준이지만 넉넉히 잡는 편이 안전합니다 |
| `graceful_timeout` | 60 | 배포 시 진행 중인 **요청**이 끝날 시간. 백그라운드 작업(최대 1시간)까지 기다리지는 않습니다 — 남은 건 다음 기동 때 복구됩니다 |
| `max_requests` | **0** | 워커 재활용은 실행 중인 백그라운드 작업을 죽입니다. 켜지 마세요 |
| `preload_app` | **False** | fork 를 건너간 스레드·DB 커넥션이 깨집니다 |

```bash
WEB_CONCURRENCY=4 BIND=0.0.0.0:8000 gunicorn -c deploy/gunicorn.conf.py main:app
```

**⚠️ 동시 작업 수 = `workers` × `FOLLOWUP_JOB_MAX_WORKERS`**
`-w 4`, `JOB_MAX_WORKERS=16` 이면 최대 64개가 동시에 돕니다. 메모리와 DB 커넥션을 여기 맞추세요.
워커를 여러 프로세스로 늘릴 거라면 SQLite 대신 PostgreSQL 을 권합니다 (쓰기가 직렬화됨).

**앞단 타임아웃도 같이 올려야 합니다** — Real 이 5분까지 걸리므로:

| 지점 | 기본값 | 설정 |
|---|---|---|
| nginx | 60초 | `proxy_read_timeout 360s;` |
| AWS ALB | 60초 | idle timeout 상향 |
| 클라이언트 | 라이브러리마다 | `timeout=360` 명시 |

### Windows 에서는

**gunicorn 이 동작하지 않습니다** — `fcntl` 에 의존해서 import 조차 실패합니다
(`uvicorn-worker` 도 gunicorn 을 import 하므로 같이 실패). 그래서 `requirements-prod.txt` 로
분리했습니다. 윈도우에서는 uvicorn 을 직접 쓰면 됩니다:

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

개발·테스트는 윈도우에서 전부 가능합니다. gunicorn 은 리눅스 배포 시에만 필요합니다.

---

## 설정

전부 `app/config.py` 한 파일에 있고, `FOLLOWUP_*` 환경변수나 `.env` 로 덮어씁니다.
`.env.example` 을 복사해서 쓰세요.

| 항목 | 기본값 | |
|---|---|---|
| `FOLLOWUP_JOB_TIMEOUT_SEC` | 3600 | After 작업 최대 실행 시간 |
| `FOLLOWUP_REAL_TIMEOUT_SEC` | 300 | Real 동기 처리 최대 시간 |
| `FOLLOWUP_JOB_MAX_WORKERS` | 16 | 프로세스당 동시 작업 수 |
| `FOLLOWUP_JOB_MAX_ATTEMPTS` | 3 | 복구 재시도 한도 (크래시 루프 방지) |
| `FOLLOWUP_HEARTBEAT_INTERVAL_SEC` | 30 | 살아있음 신호 주기 |
| `FOLLOWUP_STALE_AFTER_SEC` | 120 | 이만큼 끊기면 죽은 것으로 판단 |
| `FOLLOWUP_DB_PATH` | `followup.db` | SQLite 경로 |

---

## 동작 확인된 것

```
[OK] 정상 처리                Q → R → S
[OK] 시간 제한                5초 sleep 중 2초 제한 → 2.0초 만에 T
[OK] 취소                     30초 sleep 중 취소 → 0.1초 만에 C
[OK] 재시작 복구              실행 중 프로세스 강제 종료 → 재기동 → 자동 재실행 → S (attempts=2)
[OK] 동시 실행                결과 섞임 없음
[OK] matplotlib 동시 렌더     스레드 8개 동시 → 그림 섞임 없음
[OK] HTTP                     202 / 504 / 404 / 422 / 취소 / health
```
