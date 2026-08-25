"""线上只读预检：验证 Cookie 能否打开聊天页，但绝不点击或发送消息。"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from core.browser import get_browser
from core.tasks import (
    CONVERSATION_LIST_SELECTOR,
    configure_browser_context,
    retry_operation,
)
from utils.config import get_config, get_user_data


logger = logging.getLogger("app")
CHAT_URL = "https://www.douyin.com/chat"


class PreflightError(RuntimeError):
    """表示至少一个账号无法只读打开聊天列表。"""


def run_preflight(
    users: Optional[Sequence[Mapping[str, Any]]] = None,
    runtime_config: Optional[Mapping[str, Any]] = None,
    browser_factory: Optional[Callable[..., Tuple[Any, Any]]] = None,
) -> int:
    """逐账号验证登录态与聊天列表，并返回通过账号数量。

    预检只执行 ``new_context``、注入 Cookie、导航和等待列表容器四类操作。函数中
    没有好友点击、编辑器定位、键盘输入或 Enter 调用，因此适合在启用定时器前做
    线上验收。每个账号仍使用独立上下文，并在任意异常后保证关闭。
    """

    config = dict(runtime_config or get_config())
    task_users = list(users) if users is not None else list(get_user_data())
    factory = browser_factory or get_browser
    playwright = None
    browser = None
    passed_count = 0
    errors: List[str] = []

    try:
        playwright, browser = factory(runtime_config=config)
        for account_index, user in enumerate(task_users, start=1):
            context = None
            try:
                context = browser.new_context()
                configure_browser_context(context, config)
                context.add_cookies(list(user.get("cookies", [])))
                page = context.new_page()
                retry_operation(
                    "打开抖音聊天预检页面",
                    page.goto,
                    retries=config["taskRetryTimes"],
                    delay=config["friendListTimeout"] / 1000,
                    url=CHAT_URL,
                    # 抖音页面会持续保持部分连接，等待完整 load 在线上可能直到
                    # BROWSER_TIMEOUT 才失败；DOM 就绪后再单独等待列表更准确。
                    wait_until="domcontentloaded",
                )
                page.wait_for_selector(
                    CONVERSATION_LIST_SELECTOR,
                    timeout=config["browserTimeout"],
                )
                passed_count += 1
                logger.info("第 %s 个账号只读预检通过", account_index)
            except Exception as exc:
                # 只记录序号与异常类型，避免在部署日志里暴露用户名、抖音号或好友。
                errors.append(f"账号序号 {account_index}: {type(exc).__name__}")
                logger.exception("第 %s 个账号只读预检失败", account_index)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception as exc:
                        errors.append(
                            f"账号序号 {account_index} 上下文清理: {type(exc).__name__}"
                        )
                        logger.exception("第 %s 个账号预检上下文关闭失败", account_index)
    finally:
        # browser.close 失败也不能阻止 Playwright driver 停止；两种清理错误都纳入
        # 预检失败，防止残留 Chromium 被误认为部署可用。
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                errors.append(f"浏览器清理: {type(exc).__name__}")
                logger.exception("只读预检关闭浏览器失败")
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                errors.append(f"Playwright 清理: {type(exc).__name__}")
                logger.exception("只读预检停止 Playwright 失败")

    if errors:
        raise PreflightError(
            f"只读预检有 {len(errors)} 个错误，通过 {passed_count}/{len(task_users)} 个账号"
        )
    return passed_count
