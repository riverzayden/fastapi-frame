"""무거운 초기화는 여기. 프로세스당 한 번만 실행됩니다.

원래 구조의 FollowUpBase / FollowUpAfter / FollowUpReal 이 여기로 합쳐졌습니다.
After 와 Real 의 차이는 이제 @track_status 데코레이터 하나뿐입니다.
"""

import logging

from app.config import settings
from app.core import FollowUpTask
from app.jobs.deadline import Deadline, clamp_timeout
from app.jobs import runtime
from app.jobs.runtime import track_status
from app.schemas import FollowUpRequest, FollowUpResult

logger = logging.getLogger(__name__)


class FollowUpService:
    def __init__(self, config=settings):
        self.config = config

        # ── __init__ 에 할 게 아무리 많아도 여기라면 괜찮습니다 ──
        #    서버 시작 시 한 번만 돌기 때문입니다.
        #    self.model = load_model(config.model_path)
        #    self.tokenizer = ...
        #    self.rules = load_rules(...)
        self.steps = ["load", "analyze", "score"]
        self.default_params = {"poll_interval_sec": 1}
        logger.info("FollowUpService 초기화 완료 (steps=%d)", len(self.steps))

        # ⚠️ 여기서 만든 것은 모든 요청이 공유합니다. 읽기 전용으로만 쓰세요.
        #    요청 데이터를 self 에 저장하면 동시 요청끼리 서로 덮어씁니다.

    # ── Real: 동기 처리, 결과를 응답으로 ─────────────────────
    def process(self, data: FollowUpRequest, deadline: Deadline = None) -> FollowUpResult:
        if deadline is None:
            # 요청이 더 짧게 요구하면 존중하되, real_timeout_sec 을 넘길 수는 없습니다.
            deadline = Deadline(
                clamp_timeout(data.timeout_sec, self.config.real_timeout_sec)
            )
        return FollowUpTask(self, data, deadline).run()

    # ── After: 백그라운드 처리, 결과를 DB 로 ─────────────────
    #    데코레이터가 claim → R → (성공 S / 실패 F / 초과 T / 취소 C) 를 담당합니다.
    @track_status
    def process_and_store(self, data: FollowUpRequest, deadline: Deadline) -> FollowUpResult:
        return self.process(data, deadline)


JOB_TYPE = "followup"

service = FollowUpService()

# ★ 등록하지 않으면 재시작 복구가 이 작업을 되살리지 못합니다.
runtime.register(JOB_TYPE, service.process_and_store, FollowUpRequest.model_validate)
