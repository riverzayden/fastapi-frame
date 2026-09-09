"""작업 저장소 (SQLite).

★ 나중에 PostgreSQL/MySQL 로 바꿀 때 이 파일만 고치면 됩니다.
  다른 파일은 아래 함수 시그니처만 알고 있습니다.

SQLite 주의:
  - 쓰기는 한 번에 하나만 가능합니다. WAL + busy_timeout 으로 대기시켰습니다.
  - 커넥션은 스레드마다 따로 씁니다 (sqlite3 커넥션은 스레드 공유가 위험).
  - 동시 쓰기가 많아지면 그때 PostgreSQL 로 옮기세요. claim 쿼리는 거의 그대로입니다.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings

# 상태값: Q 대기 / R 실행중 / S 성공 / F 실패 / T 시간초과 / C 취소
TERMINAL = ("S", "F", "T", "C")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS followup_job (
    id            TEXT    PRIMARY KEY,
    job_type      TEXT    NOT NULL,          -- 어느 핸들러가 처리할 작업인지
    payload       TEXT    NOT NULL,          -- 재실행의 핵심. 이게 없으면 복구 불가.
    status        TEXT    NOT NULL,
    owner         TEXT,
    heartbeat_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    result        TEXT,
    error         TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_status ON followup_job (status, heartbeat_at);
"""


def utcnow() -> str:
    """ISO8601 UTC. 문자열 비교만으로 시간 비교가 됩니다."""
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            settings.db_path,
            check_same_thread=False,
            timeout=settings.db_timeout_sec,
            isolation_level=None,          # 오토커밋. 트랜잭션은 필요할 때 명시.
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(settings.db_timeout_sec * 1000)}")
        _local.conn = conn
    return conn


def init() -> None:
    _conn().executescript(SCHEMA)


# ── 등록 ──────────────────────────────────────────────────────────

def enqueue(payload: dict[str, Any], job_type: str) -> str:
    """요청 스레드에서 동기로 호출. 여기서 커밋되어야 재시작 복구가 성립합니다."""
    job_id = str(uuid.uuid4())
    now = utcnow()
    _conn().execute(
        "INSERT INTO followup_job"
        " (id, job_type, payload, status, attempts, created_at, updated_at)"
        " VALUES (?, ?, ?, 'Q', 0, ?, ?)",
        (job_id, job_type, json.dumps(payload, ensure_ascii=False), now, now),
    )
    return job_id


# ── 워커 ──────────────────────────────────────────────────────────

def claim(job_id: str, owner: str) -> bool:
    """Q -> R 로 원자적 전환. False 면 다른 워커가 이미 가져간 것입니다.

    WHERE status='Q' 조건 하나가 중복 실행을 막습니다.
    (PostgreSQL 로 옮겨도 이 쿼리는 그대로 동작합니다.)
    """
    now = utcnow()
    cur = _conn().execute(
        "UPDATE followup_job"
        "   SET status='R', owner=?, heartbeat_at=?, attempts=attempts+1, updated_at=?"
        " WHERE id=? AND status='Q'",
        (owner, now, now, job_id),
    )
    return cur.rowcount == 1


def heartbeat(job_ids: list[str]) -> None:
    """살아있음 신호. 이게 있어야 '도는 중 R' 과 '죽은 채 남은 R' 을 구분합니다."""
    if not job_ids:
        return
    now = utcnow()
    placeholders = ",".join("?" * len(job_ids))
    _conn().execute(
        f"UPDATE followup_job SET heartbeat_at=? WHERE id IN ({placeholders}) AND status='R'",
        (now, *job_ids),
    )


def finish(
    job_id: str,
    status: str,
    result: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    now = utcnow()
    _conn().execute(
        "UPDATE followup_job"
        "   SET status=?, result=?, error=?, owner=NULL, heartbeat_at=NULL, updated_at=?"
        " WHERE id=?",
        (
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            error,
            now,
            job_id,
        ),
    )


# ── 복구 ──────────────────────────────────────────────────────────

def revive_stale() -> int:
    """하트비트 끊긴 R 을 Q 로 되돌립니다.

    살아있는 워커가 돌리는 중인 R 은 하트비트가 최신이라 건드리지 않습니다.
    그래서 gunicorn -w 4 처럼 여러 프로세스가 동시에 시작해도 안전합니다.
    """
    cur = _conn().execute(
        "UPDATE followup_job SET status='Q', owner=NULL, updated_at=?"
        " WHERE status='R' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
        (utcnow(), _ago(settings.stale_after_sec)),
    )
    return cur.rowcount


def fail_exhausted() -> int:
    """계속 죽는 작업은 포기시킵니다. 없으면 재시작→재실행→크래시 무한루프."""
    cur = _conn().execute(
        "UPDATE followup_job SET status='F', error='최대 재시도 횟수 초과', updated_at=?"
        " WHERE status='Q' AND attempts >= ?",
        (utcnow(), settings.job_max_attempts),
    )
    return cur.rowcount


def fetch_queued(limit: int) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT id, job_type, payload FROM followup_job"
        " WHERE status='Q' ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r["id"], "job_type": r["job_type"], "payload": json.loads(r["payload"])}
        for r in rows
    ]


# ── 조회 ──────────────────────────────────────────────────────────

def get(job_id: str) -> Optional[dict[str, Any]]:
    row = _conn().execute(
        "SELECT * FROM followup_job WHERE id=?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "job_id": row["id"],
        "job_type": row["job_type"],
        "status": row["status"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
    }
