"""모든 튜닝 값은 여기 한 곳에.

환경변수 FOLLOWUP_* 또는 .env 파일로 덮어쓸 수 있습니다.
    FOLLOWUP_JOB_TIMEOUT_SEC=7200 python -m uvicorn main:app
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOLLOWUP_", env_file=".env", extra="ignore"
    )

    # ── 실행 시간 제한 ────────────────────────────────────────
    job_timeout_sec: int = 3600      # After 작업 최대 실행 시간 (1시간)
    real_timeout_sec: int = 300      # Real 동기 처리 최대 시간 (5분)
    cancel_grace_sec: int = 30       # 취소 신호 후 유예. 넘기면 스레드 누수로 간주

    # ── 워커 ──────────────────────────────────────────────────
    job_max_workers: int = 16        # 동시에 도는 After 작업 수
    job_max_attempts: int = 3        # 재시작 복구 재시도 한도 (크래시 루프 방지)

    # ── 상태 / 복구 ───────────────────────────────────────────
    heartbeat_interval_sec: int = 30
    stale_after_sec: int = 120       # 하트비트가 이만큼 끊기면 죽은 것으로 판단
    watchdog_interval_sec: int = 10
    recover_on_startup: bool = True
    recover_batch_size: int = 500

    # ── DB (SQLite) ───────────────────────────────────────────
    db_path: str = "followup.db"
    db_timeout_sec: float = 30.0

    # ── 그림 (matplotlib) ─────────────────────────────────────
    plot_dpi: int = 100
    plot_font_size: int = 10
    plot_output_dir: str = "output/plots"     # SQLite busy_timeout

    @model_validator(mode="after")
    def _validate(self):
        if self.stale_after_sec <= self.heartbeat_interval_sec * 2:
            raise ValueError(
                "stale_after_sec 은 heartbeat_interval_sec 의 2배보다 커야 합니다. "
                "그렇지 않으면 살아있는 작업을 죽은 것으로 오판합니다."
            )
        if self.cancel_grace_sec <= self.watchdog_interval_sec:
            raise ValueError("cancel_grace_sec 은 watchdog_interval_sec 보다 커야 합니다.")
        return self


settings = Settings()
