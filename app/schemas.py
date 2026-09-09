"""요청/응답 스키마. Pydantic 이 검증을 대신해 줍니다."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class FollowUpRequest(BaseModel):
    """/followafter, /followreal 공통 입력."""

    target_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    # 요청별 타임아웃. settings.job_timeout_sec 를 넘길 수는 없습니다(상한 클램프).
    timeout_sec: Optional[int] = None


class FollowUpResult(BaseModel):
    """공통 로직의 산출물. Real 은 이걸 그대로 응답하고, After 는 DB 에 넣습니다."""

    target_id: str
    score: float
    detail: dict[str, Any] = Field(default_factory=dict)


class JobAccepted(BaseModel):
    job_id: str
    status: str


class JobView(BaseModel):
    job_id: str
    status: str          # Q 대기 / R 실행중 / S 성공 / F 실패 / T 시간초과 / C 취소
    attempts: int
    created_at: str
    updated_at: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
