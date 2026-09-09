"""파이프라인 동작 확인 — 웹서버 없이 돌립니다. Windows / Linux 모두 동작합니다.

    python tests/test_pipeline.py

검증 항목:
    1. 정상 처리          Q → R → S
    2. 시간 제한          제한 초과 시 sleep 도중에 깨어나 T
    3. 취소               취소 요청 시 즉시 C
    4. 재시작 복구        죽은 워커가 남긴 R → Q → 재실행 → S
    5. 동시 실행          여러 작업이 서로 간섭하지 않음
    6. matplotlib         스레드 8개가 동시에 그려도 그림이 섞이지 않음
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 콘솔에서 한글이 깨지지 않도록
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ★ config import 전에 설정을 주입합니다 (테스트용 짧은 주기)
os.environ.update(
    FOLLOWUP_DB_PATH=os.path.join(tempfile.gettempdir(), "followup_smoke.db"),
    FOLLOWUP_HEARTBEAT_INTERVAL_SEC="1",
    FOLLOWUP_STALE_AFTER_SEC="3",
    FOLLOWUP_WATCHDOG_INTERVAL_SEC="1",
    FOLLOWUP_CANCEL_GRACE_SEC="2",
    FOLLOWUP_JOB_MAX_WORKERS="8",
)
if os.path.exists(os.environ["FOLLOWUP_DB_PATH"]):
    os.remove(os.environ["FOLLOWUP_DB_PATH"])

import logging
import threading

from app import plotting
from app.jobs import db, runtime
from app.schemas import FollowUpRequest
from app.service import JOB_TYPE, service

logging.basicConfig(level=logging.WARNING)

PASS, FAIL = "  [OK]  ", "  [FAIL]"


def wait_for(job_id, statuses, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = db.get(job_id)
        if job and job["status"] in statuses:
            return job
        time.sleep(0.1)
    return db.get(job_id)


def check(name, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL} {name}: {actual} (기대값 {expected})")
    return ok


def test_plotting_threadsafe() -> bool:
    """pyplot 을 썼다면 여기서 그림이 섞이거나 빈 PNG 가 나옵니다."""
    outdir = os.path.join(tempfile.gettempdir(), "followup_smoke_plots")
    os.makedirs(outdir, exist_ok=True)
    plotting.warmup()

    errors: list[str] = []
    paths: dict[int, str] = {}
    barrier = threading.Barrier(8)          # 8개가 동시에 그리도록 강제

    def draw(i: int):
        try:
            barrier.wait(timeout=10)
            with plotting.make_figure(figsize=(4, 3)) as fig:
                ax = fig.subplots()
                for k in range(i + 1):      # 그림마다 선 개수가 다름
                    ax.plot([0, 1, 2], [k, k + 1, k])
                ax.set_title(f"job-{i}")
                fig.canvas.draw()
                # ★ 전역 상태가 섞였다면 남의 선이 들어와 개수가 어긋납니다
                if len(ax.lines) != i + 1:
                    errors.append(f"thread {i}: 선 {len(ax.lines)}개 (기대 {i + 1}개)")
                paths[i] = plotting.save(fig, os.path.join(outdir, f"plot-{i}.png"))
        except Exception as e:              # noqa: BLE001
            errors.append(f"thread {i}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=draw, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    blobs = []
    for i in range(8):
        path = paths.get(i)
        if not path or not os.path.exists(path):
            errors.append(f"thread {i}: 파일 없음")
            continue
        data = open(path, "rb").read()
        if not data.startswith(b"\x89PNG"):
            errors.append(f"thread {i}: 깨진 PNG")
        blobs.append(data)

    if len(set(blobs)) != 8:
        errors.append(f"서로 다른 그림이어야 하는데 {len(set(blobs))}종류만 나옴")

    for e in errors:
        print(f"         ! {e}")
    print(f"         PNG 8개 저장: {outdir}")
    return not errors


def main():
    runtime.start()
    results = []

    # 1. 정상 처리 ─────────────────────────────────────────────
    data = FollowUpRequest(target_id="ok-1", params={"poll_interval_sec": 0.1})
    job_id = runtime.enqueue_and_submit(JOB_TYPE, data)
    job = wait_for(job_id, ("S", "F", "T", "C"))
    results.append(check("1. 정상 처리", job["status"], "S"))
    print(f"         결과: {job['result']}")

    # 2. 시간 제한 ─────────────────────────────────────────────
    #    단계마다 5초 자는 작업에 2초 제한 → sleep 도중 깨어나야 함
    data = FollowUpRequest(target_id="timeout-1", timeout_sec=2,
                           params={"poll_interval_sec": 5})
    started = time.time()
    job_id = runtime.enqueue_and_submit(JOB_TYPE, data)
    job = wait_for(job_id, ("S", "F", "T", "C"))
    elapsed = time.time() - started
    results.append(check("2. 시간 제한", job["status"], "T"))
    print(f"         {elapsed:.1f}초 만에 종료 (5초 sleep 을 중단하고 빠져나옴)")

    # 3. 취소 ──────────────────────────────────────────────────
    data = FollowUpRequest(target_id="cancel-1", timeout_sec=60,
                           params={"poll_interval_sec": 30})
    job_id = runtime.enqueue_and_submit(JOB_TYPE, data)
    time.sleep(0.5)
    started = time.time()
    cancelled = runtime.cancel(job_id)
    job = wait_for(job_id, ("S", "F", "T", "C"))
    results.append(check("3. 취소", job["status"], "C"))
    print(f"         취소 신호 후 {time.time() - started:.1f}초 만에 반응 "
          f"(30초 sleep 중이었음, cancel 호출={cancelled})")

    # 4. 재시작 복구 ───────────────────────────────────────────
    #    죽은 워커가 남긴 것처럼 오래된 R 레코드를 심습니다
    data = FollowUpRequest(target_id="orphan-1", params={"poll_interval_sec": 0.1})
    job_id = db.enqueue(data.model_dump(), JOB_TYPE)
    db._conn().execute(
        "UPDATE followup_job SET status='R', owner='dead-worker', heartbeat_at=? WHERE id=?",
        (db._ago(600), job_id),
    )
    before = db.get(job_id)["status"]
    runtime.recover()
    job = wait_for(job_id, ("S", "F", "T", "C"))
    results.append(check(f"4. 재시작 복구 ({before} → 재실행)", job["status"], "S"))

    # 5. 동시 실행 ─────────────────────────────────────────────
    ids = []
    for i in range(5):
        d = FollowUpRequest(target_id=f"conc-{i}", params={"poll_interval_sec": 0.2})
        jid = runtime.enqueue_and_submit(JOB_TYPE, d)
        ids.append((jid, f"conc-{i}"))
    ok = True
    for jid, target in ids:
        j = wait_for(jid, ("S", "F", "T", "C"))
        # 결과가 자기 target_id 를 담고 있어야 함 (상태 섞임 없음)
        ok = ok and j["status"] == "S" and j["result"]["target_id"] == target
    results.append(check("5. 동시 실행 5건 (결과 섞임 없음)", ok, True))

    # 6. matplotlib 스레드 안전 ────────────────────────────────
    results.append(check("6. matplotlib 동시 렌더 8건", test_plotting_threadsafe(), True))

    runtime.stop()
    print()
    print(f"  {sum(results)}/{len(results)} 통과")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
