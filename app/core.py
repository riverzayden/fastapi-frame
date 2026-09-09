"""after / real 공통 처리 로직.

★ 이 파일이 처음 질문의 목적지입니다.
   FollowUpAfter.run() 과 FollowUpReal.run() 에 복붙돼 있던 로직이 여기 한 곳에만 있습니다.

이 파일은 DB 도, 스레드도, 웹 프레임워크도 모릅니다.
→ 서버 없이 그냥 import 해서 테스트할 수 있습니다.

    from core import FollowUpTask
    from deadline import Deadline
    result = FollowUpTask(service, request, Deadline(60)).run()
"""

import logging
from typing import Any

from app.jobs.deadline import Deadline
from app.schemas import FollowUpRequest, FollowUpResult

logger = logging.getLogger(__name__)


class FollowUpTask:
    """요청 하나를 처리하는 동안의 상태.

    __init__ 이 아무리 길어져도 괜찮습니다 — 요청마다 새로 만들어지는,
    이 요청만의 상태니까요. 무거운 리소스(모델·설정)는 service 에서 참조만 합니다.
    """

    def __init__(self, service, data: FollowUpRequest, deadline: Deadline):
        self.svc = service          # 공유 리소스 — 읽기 전용으로만 쓰세요
        self.data = data
        self.dl = deadline

        # ── 요청별 상태는 여기에 마음껏 ──────────────────
        self.target_id = data.target_id
        self.params = {**service.default_params, **data.params}
        self.buffer: list[Any] = []
        self.score = 0.0
        # ...

    # ── 공통 로직 본체 ───────────────────────────────────
    def run(self) -> FollowUpResult:
        self.prepare()
        self.dl.check()                     # ← 단계 경계마다 확인

        for step in self.svc.steps:
            self.dl.check()
            self.run_step(step)

        return self.finalize()

    def prepare(self) -> None:
        logger.info("[%s] 준비", self.target_id)
        # 여기에 전처리

    def run_step(self, step: str) -> None:
        logger.info("[%s] %s (남은 시간 %.0fs)", self.target_id, step, self.dl.remaining)

        # 여기에 after, real 공통으로 쓰는 로직

        # ★ time.sleep(n) 대신 self.dl.sleep(n) 을 쓰세요.
        #   자면서 취소 신호도 감시하므로, 1시간짜리 대기 중에도 즉시 빠져나옵니다.
        self.dl.sleep(self.params.get("poll_interval_sec", 1))
        self.score += 1.0

    def finalize(self) -> FollowUpResult:
        return FollowUpResult(
            target_id=self.target_id,
            score=self.score,
            detail={"steps": len(self.svc.steps), "params": self.params},
        )
