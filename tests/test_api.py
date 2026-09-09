"""HTTP 레벨 확인 — 실제 서버를 띄우지 않고 앱을 그대로 호출합니다.
Windows / Linux 모두 동작합니다.

    python tests/test_api.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.update(
    FOLLOWUP_DB_PATH=os.path.join(tempfile.gettempdir(), "followup_api.db"),
    FOLLOWUP_HEARTBEAT_INTERVAL_SEC="1",
    FOLLOWUP_STALE_AFTER_SEC="3",
    FOLLOWUP_WATCHDOG_INTERVAL_SEC="1",
    FOLLOWUP_CANCEL_GRACE_SEC="2",
    FOLLOWUP_REAL_TIMEOUT_SEC="10",
)
if os.path.exists(os.environ["FOLLOWUP_DB_PATH"]):
    os.remove(os.environ["FOLLOWUP_DB_PATH"])

from fastapi.testclient import TestClient

from main import app

PASS, FAIL = "  [OK]  ", "  [FAIL]"


def check(name, actual, expected):
    ok = actual == expected
    print(f"{PASS if ok else FAIL} {name}: {actual} (기대값 {expected})")
    return ok


def main():
    results = []
    # with 블록에 들어갈 때 lifespan 이 돌면서 워커/복구가 기동합니다
    with TestClient(app) as client:

        # 1. Real — 동기 응답 ──────────────────────────────────
        r = client.post("/followreal", json={
            "target_id": "real-1", "params": {"poll_interval_sec": 0.1},
        })
        results.append(check("1. POST /followreal", r.status_code, 200))
        print(f"         {r.json()}")

        # 2. Real — 제한 시간 초과는 504 ───────────────────────
        r = client.post("/followreal", json={
            "target_id": "real-slow", "timeout_sec": 1,
            "params": {"poll_interval_sec": 5},
        })
        results.append(check("2. Real 시간 초과 → 504", r.status_code, 504))

        # 3. After — 즉시 202 ─────────────────────────────────
        started = time.time()
        r = client.post("/followafter", json={
            "target_id": "after-1", "params": {"poll_interval_sec": 0.3},
        })
        accepted_in = time.time() - started
        results.append(check("3. POST /followafter", r.status_code, 202))
        job_id = r.json()["job_id"]
        print(f"         {accepted_in * 1000:.0f}ms 만에 202 반환 (작업은 백그라운드)")

        # 4. 폴링으로 완료 확인 ────────────────────────────────
        status, seen = None, []
        deadline = time.time() + 15
        while time.time() < deadline:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] not in seen:
                seen.append(job["status"])
            status = job["status"]
            if status in ("S", "F", "T", "C"):
                break
            time.sleep(0.1)
        results.append(check(f"4. 상태 전이 {' → '.join(seen)}", status, "S"))

        # 5. 없는 작업은 404 ──────────────────────────────────
        results.append(check("5. 없는 job 조회 → 404",
                             client.get("/jobs/does-not-exist").status_code, 404))

        # 6. 잘못된 요청은 422 (Pydantic 검증) ─────────────────
        results.append(check("6. target_id 누락 → 422",
                             client.post("/followafter", json={}).status_code, 422))

        # 7. 취소 ─────────────────────────────────────────────
        r = client.post("/followafter", json={
            "target_id": "after-cancel", "timeout_sec": 60,
            "params": {"poll_interval_sec": 30},
        })
        job_id = r.json()["job_id"]
        time.sleep(0.5)
        client.post(f"/jobs/{job_id}/cancel")
        status = None
        deadline = time.time() + 10
        while time.time() < deadline:
            status = client.get(f"/jobs/{job_id}").json()["status"]
            if status in ("S", "F", "T", "C"):
                break
            time.sleep(0.1)
        results.append(check("7. 실행 중 작업 취소", status, "C"))

        # 8. health ───────────────────────────────────────────
        results.append(check("8. GET /health",
                             client.get("/health").json()["status"], "ok"))

    print()
    print(f"  {sum(results)}/{len(results)} 통과")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
