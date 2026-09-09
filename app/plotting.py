"""matplotlib 스레드 안전 사용.

문제:
    pyplot(plt.*) 은 "현재 figure/axes" 를 전역으로 들고 있는 상태 머신입니다.
    워커 스레드 여러 개가 동시에 plt.plot() 을 부르면
      - 한 그림의 선이 다른 그림에 섞이고
      - plt.savefig() 가 남의 figure 를 저장하고
      - 빈 PNG 가 나오거나 세그폴트가 납니다.
    게다가 기본 GUI 백엔드(TkAgg 등)는 메인 스레드에서만 동작합니다.

해결:
    1) Agg 백엔드 (GUI 없음, 파일 출력 전용)
    2) plt 를 아예 쓰지 않고 Figure 객체를 직접 생성 → 전역 상태를 건드리지 않음

★ 워커 코드에서 `import matplotlib.pyplot as plt` 를 하지 마세요.
   그리기가 필요하면 이 모듈의 make_figure() 만 쓰면 됩니다.
"""

import logging
import os
import tempfile
from contextlib import contextmanager
from typing import Iterator

import matplotlib

# ★ 반드시 pyplot 을 import 하는 어떤 코드보다도 먼저 실행되어야 합니다.
matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402

from app.config import settings  # noqa: E402

logger = logging.getLogger(__name__)

# rcParams / style.use() / rc_context() 는 모두 전역 상태입니다.
# 스레드가 도는 중에 바꾸면 다른 스레드의 그림이 영향을 받습니다.
# → 반드시 여기서, 서버 시작 시에 한 번만 설정하세요. (값은 app/config.py 에서)
matplotlib.rcParams.update(
    {
        "figure.dpi": settings.plot_dpi,
        "font.size": settings.plot_font_size,
        "savefig.bbox": "tight",
        "axes.grid": True,
    }
)


@contextmanager
def make_figure(figsize=(8, 5), **kwargs) -> Iterator[Figure]:
    """스레드 안전한 Figure. plt.figure() 대신 이걸 쓰세요.

        with make_figure() as fig:
            ax = fig.subplots()
            ax.plot(xs, ys)
            save(fig, "out.png")

    Figure 를 직접 만들면 pyplot 의 전역 figure 목록에 등록되지 않으므로
    plt.close() 로 정리할 필요도 없고, 스레드끼리 간섭하지도 않습니다.
    """
    fig = Figure(figsize=figsize, **kwargs)
    try:
        yield fig
    finally:
        fig.clear()          # 참조 순환 정리 (메모리 누수 방지)


def output_path(filename: str) -> str:
    """settings.plot_output_dir 기준 경로. 저장 위치는 config 에서 관리합니다."""
    return os.path.join(settings.plot_output_dir, filename)


def save(fig: Figure, path: str, **kwargs) -> str:
    """임시 파일에 쓰고 rename — 중간에 죽어도 반쪽짜리 PNG 가 남지 않습니다.

    작업이 재시작 복구로 다시 실행될 수 있으므로(멱등성),
    파일 출력은 이렇게 원자적으로 처리하는 편이 안전합니다.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".png")
    os.close(fd)
    try:
        fig.savefig(tmp, **kwargs)
        os.replace(tmp, path)          # 원자적 교체 (Windows 포함)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def warmup() -> None:
    """폰트 캐시를 서버 시작 시 미리 만듭니다.

    첫 렌더링 때 font_manager 가 캐시를 빌드하는데, 워커 스레드 여러 개가
    동시에 처음 그리면 이 지점에서 경합이 생깁니다. 미리 한 번 그려서 회피합니다.
    """
    with make_figure(figsize=(1, 1)) as fig:
        ax = fig.subplots()
        ax.plot([0, 1], [0, 1])
        ax.set_title("warmup")
        fig.canvas.draw()
    logger.info("matplotlib 준비 완료 (backend=%s)", matplotlib.get_backend())
