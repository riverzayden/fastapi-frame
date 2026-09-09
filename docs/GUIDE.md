# 개발 가이드

수정할 일이 생겼을 때 이 문서만 보면 되도록 정리했습니다.

---

## 1. 30초 지도 — 어디를 건드리나

```
main.py                  라우트 (얇게 유지)
app/
  config.py         ★    모든 설정값. 상수를 코드에 박지 말고 전부 여기로
  schemas.py             요청/응답 모델
  core.py           ★★   after/real 공통 로직 — 평소 작업은 여기
  service.py             무거운 초기화(모델·설정) + After/Real 진입점
  plotting.py            matplotlib 스레드 안전 래퍼
  jobs/                  실행 인프라 — 열어볼 일이 거의 없어야 정상
    db.py                작업 저장소 (DB 교체 시 이 파일만)
    runtime.py           워커풀·하트비트·워치독·복구·상태 데코레이터
    deadline.py          시간 제한 / 협조적 취소
tests/                   python tests/test_pipeline.py, tests/test_api.py
deploy/gunicorn.conf.py  리눅스 운영 설정
```

**로직 수정 = `app/core.py`**, **설정 변경 = `app/config.py`**. 나머지는 대부분 그대로 둡니다.

---

## 2. ★ 반드시 지켜야 할 4가지

| 하고 싶은 것 | **반드시 이걸 쓰세요** | 쓰면 안 되는 것 | 어기면 |
|---|---|---|---|
| 백그라운드 실행 | `runtime.enqueue_and_submit(JOB_TYPE, data)` | `threading.Thread(...).start()`, `BackgroundTasks` | 재시작 복구·타임아웃·취소·하트비트가 **전부 안 걸림**. 서버 죽으면 작업 증발 |
| 대기 / 폴링 | `self.dl.sleep(n)` | `time.sleep(n)` | 1시간 제한이 걸려도 안 멈춤. 워치독이 `thread leaked` 를 남기고 스레드는 계속 점유 |
| 긴 루프 | `self.dl.check()` 를 루프 안에 | (없음) | 위와 동일 |
| 그림 그리기 | `plotting.make_figure()` | `import matplotlib.pyplot as plt` | 스레드끼리 그림이 섞이고 빈 PNG가 나옴 |

추가로 **`service` 인스턴스에 요청 데이터를 저장하지 마세요.** 모든 요청이 공유합니다.
요청별 상태는 전부 `FollowUpTask`(`app/core.py`)에 둡니다.

```python
# ❌ 요청 A의 데이터를 요청 B가 덮어씀 — 재현 안 되는 버그
def process(self, data):
    self.data = data
    return self._run()

# ✅
def process(self, data):
    return FollowUpTask(self, data, deadline).run()
```

---

## 3. 어디가 스레드로 도는가

```
POST /followafter ─┬─ [요청 스레드] db.enqueue()  ← 동기. 여기서 커밋돼야 복구 가능
                   └─ [요청 스레드] 202 즉시 반환
                          ↓ 넘김
                      [워커 스레드] claim(Q→R) → core.py 로직 → S/F/T/C
                                    ThreadPoolExecutor(job_max_workers)

POST /followreal ──── [요청 스레드] core.py 로직을 그대로 실행 → 응답
                      (FastAPI 가 def 핸들러를 스레드풀에서 돌립니다.
                       ★ async def 로 바꾸면 이벤트 루프가 통째로 막힙니다)

상시 데몬 스레드 2개:  heartbeat(살아있음 기록)  watchdog(기한 초과 감시)
```

**`app/core.py` 안의 코드는 워커 스레드에서도, 요청 스레드에서도 돕니다.**
그래서 전역 상태를 건드리면 안 되고(그래서 `plotting.py` 가 따로 있습니다),
`self.dl` 로 중단 가능해야 합니다.

---

## 4. 시나리오별 수정법

### A. 공통 로직 고치기 — 제일 흔한 작업

`app/core.py` 의 `FollowUpTask` 만 고칩니다.

```python
class FollowUpTask:
    def __init__(self, service, data, deadline):
        self.svc = service          # 공유 리소스 (읽기 전용)
        self.data = data
        self.dl = deadline
        self.내상태 = ...            # __init__ 이 길어져도 괜찮습니다

    def run(self):
        for step in self.svc.steps:
            self.dl.check()          # ← 잊지 마세요
            ...
            self.dl.sleep(30)        # ← time.sleep 금지
        return FollowUpResult(...)
```

After / Real 양쪽에 자동으로 반영됩니다. 실행 방식은 `service.py` 가 결정합니다.

### B. 설정값 추가

1. `app/config.py` 의 `Settings` 에 필드 추가
2. `.env.example` 에 `FOLLOWUP_<대문자>` 한 줄 추가
3. 코드에서 `settings.내값` 으로 사용

```python
# app/config.py
retry_backoff_sec: int = 5
```
```bash
FOLLOWUP_RETRY_BACKOFF_SEC=10   # 환경변수로 덮어쓰기
```

값끼리 관계가 있으면 `@model_validator` 에 검증을 추가하세요
(예: `stale_after_sec` 은 `heartbeat_interval_sec` 의 2배보다 커야 함).

### C. ★ 다른 목적의 작업 클래스 추가하기

예: 리포트를 만드는 `ReportJob` 을 추가한다고 합시다. **5단계입니다.**

**① 스키마** — `app/schemas.py`
```python
class ReportRequest(BaseModel):
    report_id: str
    period: str
    timeout_sec: int | None = None      # 있으면 요청별 제한이 걸립니다

class ReportResult(BaseModel):
    report_id: str
    file_path: str
```

**② 로직** — `app/report.py` (새 파일)
```python
class ReportTask:
    def __init__(self, service, data: ReportRequest, deadline: Deadline):
        self.svc, self.data, self.dl = service, data, deadline

    def run(self) -> ReportResult:
        self.dl.check()
        with plotting.make_figure() as fig:      # ★ plt 금지
            ...
            path = plotting.save(fig, plotting.output_path(f"{self.data.report_id}.png"))
        self.dl.sleep(1)                          # ★ time.sleep 금지
        return ReportResult(report_id=self.data.report_id, file_path=path)
```

**③ 서비스** — `app/service.py` 에 추가
```python
class ReportService:
    def __init__(self):
        self.template = load_template()          # 무거운 초기화는 여기 (1회)

    def process(self, data: ReportRequest, deadline=None) -> ReportResult:
        deadline = deadline or Deadline(
            clamp_timeout(data.timeout_sec, settings.real_timeout_sec))
        return ReportTask(self, data, deadline).run()

    @track_status                                 # ★ 이 한 줄이 DB 상태 기록 + 시간 제한
    def process_and_store(self, data: ReportRequest, deadline: Deadline) -> ReportResult:
        return self.process(data, deadline)


REPORT_JOB_TYPE = "report"
report_service = ReportService()

# ★★ 이걸 빠뜨리면 재시작 복구가 이 작업을 되살리지 못합니다
runtime.register(REPORT_JOB_TYPE, report_service.process_and_store,
                 ReportRequest.model_validate)
```

**④ 라우트** — `main.py`
```python
@app.post("/report", response_model=JobAccepted, status_code=202)
def create_report(data: ReportRequest):
    job_id = runtime.enqueue_and_submit(REPORT_JOB_TYPE, data)   # ★ 정식 경로
    return JobAccepted(job_id=job_id, status="Q")
```

**⑤ 확인** — `/jobs/{job_id}` 조회와 취소는 **그대로 동작합니다.** 새로 만들 필요 없습니다.

> **`@track_status` 가 해주는 일**
> `claim`(Q→R, 중복 실행 차단) → 실행 → 결과에 따라 `S`/`F`/`T`/`C` 기록 → 레지스트리 정리.
> 직접 `db.finish()` 를 부르지 마세요. 상태가 어긋납니다.

**"같은 로직인데 전달 방식만 다른" 경우**라면 새 클래스가 아니라 **메서드 하나만** 추가하세요.
`FollowUpAfter`/`FollowUpReal` 을 하나로 합친 이유가 이것입니다.

### D. 그림 추가

```python
from app import plotting

with plotting.make_figure(figsize=(8, 5)) as fig:
    ax = fig.subplots()
    ax.plot(xs, ys)
    plotting.save(fig, plotting.output_path("chart.png"))
```

`plotting.save()` 는 임시파일에 쓰고 rename 합니다 — 중간에 죽어도 반쪽짜리 PNG 가 남지 않습니다.
복구로 작업이 **처음부터 다시 돌 수 있으므로**, 외부 부수효과는 이렇게 멱등하게 만드세요.

---

## 5. DB 를 바꾼다면

### 고치는 파일: `app/jobs/db.py` **하나뿐입니다**

다른 파일은 아래 함수 시그니처만 알고 있습니다. 이대로 구현하면 나머지는 그대로 동작합니다.

| 함수 | 계약 |
|---|---|
| `init()` | 테이블 생성 |
| `enqueue(payload, job_type) -> job_id` | 상태 `Q` 로 삽입. **반환 전에 커밋되어야 함** |
| `claim(job_id, owner) -> bool` | `Q → R` **원자적** 전환. 이미 남이 가져갔으면 `False` |
| `heartbeat(job_ids)` | `heartbeat_at` 갱신 (status='R' 인 것만) |
| `finish(job_id, status, result, error)` | 종료 상태 기록 |
| `revive_stale() -> int` | 하트비트 끊긴 `R` → `Q` |
| `fail_exhausted() -> int` | `attempts >= max` 인 `Q` → `F` |
| `fetch_queued(limit) -> [{id, job_type, payload}]` | 대기 목록 |
| `get(job_id) -> dict \| None` | 조회 |

### PostgreSQL 로 옮길 때

- **`claim` 쿼리는 그대로 동작합니다** (`UPDATE ... WHERE id=? AND status='Q'`).
  `cursor.rowcount == 1` 판정도 동일합니다.
- 삭제할 SQLite 전용 코드: `PRAGMA journal_mode/synchronous/busy_timeout` 3줄,
  `check_same_thread=False`, 스레드별 커넥션(`threading.local`) → 커넥션 풀로 교체
- 플레이스홀더 `?` → `%s`, `TEXT` 타임스탬프 → `TIMESTAMPTZ`
- 워커가 여러 개면 `fetch_queued` 를 `SELECT ... FOR UPDATE SKIP LOCKED` 로 바꾸면
  claim 실패로 인한 헛일이 줄어듭니다 (지금도 정확성에는 문제 없습니다)

**언제 옮겨야 하나:** SQLite 는 쓰기가 직렬화됩니다. `gunicorn -w 4` 처럼 여러 **프로세스**가
동시에 쓰기 시작하면 `database is locked` 가 늘어납니다. 그때가 신호입니다.

### 교체하면 불필요해지는 것

| 교체 | 고치는 곳 | 불필요해지는 것 |
|---|---|---|
| SQLite → PostgreSQL | `app/jobs/db.py` 내부만 | **없음.** 파일 구조 그대로 |
| ThreadPool → **Celery / RQ** | `app/jobs/runtime.py` 를 태스크 정의로 교체 | `runtime.py` 의 워커풀·하트비트·워치독·`recover()` **전부**, `db.py` 의 `claim`·`heartbeat`·`revive_stale`·`fail_exhausted`, `config.py` 의 `heartbeat_interval_sec`·`stale_after_sec`·`watchdog_interval_sec`·`job_max_attempts` (큐가 대신 해줍니다) |
| 〃 | `deadline.py` | Celery `soft_time_limit` 으로 일부 대체 가능. 다만 **`dl.sleep()` 의 즉시 취소는 유지하는 편이 낫습니다** |

`app/core.py` 와 `app/service.py` 는 **어느 경우에도 그대로 재사용됩니다.** 그러려고 나눈 구조입니다.

---

## 6. 상태값과 디버깅

| 상태 | 의미 | 재시작 시 |
|---|---|---|
| `Q` | 대기 | 다시 투입 |
| `R` | 실행 중 (하트비트 갱신 중) | 하트비트 끊겼으면 `Q` 로 복구, 살아있으면 그대로 |
| `S` | 성공 | — |
| `F` | 실패 (예외) | — |
| `T` | 시간 초과 | 재시도 안 함 |
| `C` | 취소됨 | 재시도 안 함 |

**로그에서 찾을 것**

| 메시지 | 뜻 | 조치 |
|---|---|---|
| `복구: R→Q n건, ...` | 재시작 복구가 동작함 | 정상 |
| `thread leaked` | 작업이 취소 신호를 무시함 | 그 구간에 `dl.check()` / `dl.sleep()` 이 빠졌습니다 |
| `등록되지 않은 종류 '...'` | `runtime.register()` 누락 | 4-C ③ 확인 |
| `다른 워커가 처리 중 — 건너뜀` | `claim` 실패 | 정상 (중복 실행 차단이 동작한 것) |
| `최대 재시도 횟수 초과` | 계속 죽는 작업 | payload 를 보고 원인 파악 |

**직접 확인**
```bash
sqlite3 followup.db "SELECT status, count(*) FROM followup_job GROUP BY status;"
curl http://127.0.0.1:8000/health          # 지금 실행 중인 작업 수
curl http://127.0.0.1:8000/jobs/<job_id>
```

---

## 7. 자주 하는 실수

1. **`time.sleep()` 을 남겨둠** → 시간 제한이 안 걸립니다. 가장 흔합니다.
2. **`runtime.register()` 누락** → 평소엔 잘 돌다가 **재시작 후에만** 작업이 안 살아납니다.
3. **`async def` 로 라우트 선언** → 동기 로직이 이벤트 루프를 막아 서버 전체가 멈춥니다.
4. **`service` 에 요청 상태 저장** → 동시 요청에서만 재현되는 버그.
5. **`plt` 사용** → 그림이 섞입니다. 단독 테스트에서는 통과하고 부하가 걸리면 터집니다.
6. **gunicorn `max_requests` 설정** → 워커 재활용이 실행 중인 백그라운드 작업을 죽입니다.
7. **작업이 멱등하지 않음** → 복구로 재실행될 때 부수효과가 두 번 발생합니다.
