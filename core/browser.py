"""Playwright/Chromium 的惰性启动与异常安全清理。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Mapping, Optional, Tuple

from utils.config import get_config


logger = logging.getLogger(__name__)
PLAYWRIGHT_BROWSERS_PATH = "../chrome"


class BrowserStartupError(RuntimeError):
    """表示 Playwright 或 Chromium 在任务开始前无法启动。"""


def _configure_bundled_browser_path() -> None:
    """仅在确有随程序分发的浏览器目录时设置 Playwright 路径。

    源码部署通常使用 Playwright 默认缓存，Docker 也可能显式设置自己的路径；本
    函数不会覆盖现有环境变量。PyInstaller 包或仓库旁确实存在 ``chrome`` 目录时
    才采用兼容旧版的相对路径，避免把服务器安装位置强行重定向到不存在的目录。
    """

    if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        return

    if getattr(sys, "frozen", False):
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH)
        )
    else:
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH)
        )
    if os.path.isdir(candidate):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate


def _start_playwright() -> Any:
    """惰性导入 Playwright，使配置检查和纯单测不依赖浏览器包。"""

    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def install_browser() -> None:
    """用当前 Python 环境显式安装 Chromium；安装失败会保留非零异常。"""

    _configure_bundled_browser_path()
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )


def get_browser(
    runtime_config: Optional[Mapping[str, Any]] = None,
) -> Tuple[Any, Any]:
    """启动 Playwright 与 Chromium，并在部分启动失败时停止运行时。

    无头模式完全由 ``BROWSER_HEADLESS`` 对应的 ``browserHeadless`` 配置控制，不再
    由 DEBUG 或 GitHub Actions 环境隐式改变。函数不会在定时任务里自动下载浏览器；
    缺失时会给出显式安装命令并失败退出，避免每次调度产生不可控网络与磁盘开销。
    """

    config = dict(runtime_config or get_config())
    _configure_bundled_browser_path()
    playwright = None

    try:
        playwright = _start_playwright()
        browser = playwright.chromium.launch(
            headless=bool(config.get("browserHeadless", True))
        )
        return playwright, browser
    except BaseException as exc:
        # Chromium launch 失败时 Playwright 进程已经可能存在，必须先停止它；停止
        # 失败只能记日志，不能覆盖更有诊断价值的原始启动异常。
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                logger.exception("Chromium 启动失败后停止 Playwright 运行时也失败")

        if not isinstance(exc, Exception):
            raise
        if "Executable doesn't exist" in str(exc):
            raise BrowserStartupError(
                "Chromium 尚未安装，请先执行：python -m playwright install chromium"
            ) from exc
        raise BrowserStartupError(f"Chromium 启动失败：{exc}") from exc
