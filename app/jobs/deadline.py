"""작업 시간 제한 — 협조적 취소.

파이썬은 실행 중인 스레드를 강제 종료할 수 없습니다.
그래서 작업이 스스로 확인하고 빠져나오는 방식을 씁니다:

    dl.check()        # 단계 사이에서 확인
    dl.sleep(30)      # time.sleep 대신 (자면서 취소 신호도 감시)

sleep 이 긴 작업이라면 time.sleep 을 dl.sleep 으로 바꾸는 것만으로
1시간짜리 작업도 취소 신호에 즉시 반응합니다.
"""

import threading
import time


def clamp_timeout(requested, cap: int) -> int:
    """요청이 더 짧은 값을 원하면 존중하되, 설정 상한은 절대 넘지 못하게 합니다."""
    if not requested or requested <= 0:
        return cap
    return min(int(requested), cap)


class JobTimeout(Exception):
    """제한 시간 초과. 재시도하지 않습니다."""


class JobCancelled(Exception):
    """외부에서 취소 요청. 재시도하지 않습니다."""


class Deadline:
    def __init__(self, timeout_sec: float):
        self.timeout = float(timeout_sec)
        self._expires_at = time.monotonic() + self.timeout
        self._cancelled = threading.Event()

    @property
    def remaining(self) -> float:
        """남은 시간(초). 이미 지났으면 음수."""
        return self._expires_at - time.monotonic()

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def cancel(self) -> None:
        """다른 스레드에서 호출. sleep 중이어도 즉시 깨웁니다."""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        """긴 루프나 단계 경계마다 호출하세요."""
        if self._cancelled.is_set():
            raise JobCancelled()
        if self.expired:
            raise JobTimeout(f"제한 시간 {self.timeout:.0f}초 초과")

    def sleep(self, seconds: float) -> None:
        """time.sleep 대체. 취소되거나 기한이 지나면 즉시 예외."""
        self.check()
        wait_for = min(seconds, max(0.0, self.remaining))
        if self._cancelled.wait(wait_for):
            raise JobCancelled()
        self.check()
