"""集中读取并校验 DouYinSparkFlow 的运行配置。

本模块只负责把环境变量转换为可信的内部结构，不启动浏览器，也不输出任何
Cookie、好友列表或消息正文。所有校验都在真实任务开始前完成，避免配置错误
导致脚本运行到一半才失败，或在零发送的情况下错误地报告成功。
"""

import json
import logging
import os
import re
import sys
from enum import Enum
from typing import Any, Dict, List, Optional

from utils import norm


logger = logging.getLogger(__name__)

_config_cache: Optional[Dict[str, Any]] = None
_user_data_cache: Optional[List[Dict[str, Any]]] = None

COOKIE_ENV_SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
SUPPORTED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SUPPORTED_HITOKOTO_TYPES = {
    "动画",
    "漫画",
    "游戏",
    "文学",
    "原创",
    "来自网络",
    "其他",
    "影视",
    "诗词",
    "哲学",
    "抖机灵",
}


class ConfigError(ValueError):
    """表示启动前即可确定的配置错误。"""


class Environment(Enum):
    """保留原有运行环境枚举，供浏览器启动策略和外部调用兼容使用。"""

    GITHUBACTION = "GITHUB_ACTION"
    LOCAL = "LOCAL"
    PACKED = "PACKED"

    def __str__(self) -> str:
        return self.value


def get_environment() -> Environment:
    """识别打包、GitHub Actions 与普通运行环境。"""

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    if os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    return Environment.LOCAL


def _parse_json_env(name: str, default: str) -> Any:
    """解析 JSON 环境变量，并把原始解析异常转换为不泄密的错误信息。"""

    raw_value = os.getenv(name, default)
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        # 只报告变量名和位置，绝不把可能包含 Cookie 的原文写入日志。
        raise ConfigError(
            f"{name} 不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc


def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取有明确安全范围的整数配置。"""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc

    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} 必须位于 {minimum} 到 {maximum} 之间")
    return value


def _parse_bool_env(name: str, default: bool) -> bool:
    """读取布尔开关，拒绝容易产生歧义的拼写。"""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized_value = raw_value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是 true 或 false")


def get_config() -> Dict[str, Any]:
    """读取、校验并缓存非账号类配置。"""

    global _config_cache
    if _config_cache is not None:
        return _config_cache

    hitokoto_types = _parse_json_env(
        "HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]'
    )
    if not isinstance(hitokoto_types, list) or not all(
        isinstance(item, str) and item in SUPPORTED_HITOKOTO_TYPES
        for item in hitokoto_types
    ):
        raise ConfigError("HITOKOTO_TYPES 必须是仅包含受支持分类名称的 JSON 数组")

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in SUPPORTED_LOG_LEVELS:
        raise ConfigError(
            "LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL"
        )

    message_template = os.getenv(
        "MESSAGE_TEMPLATE",
        "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
    ).strip()
    if not message_template:
        raise ConfigError("MESSAGE_TEMPLATE 不能为空")

    _config_cache = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", "").strip(),
        "messageTemplate": message_template,
        "hitokotoTypes": hitokoto_types,
        "hitokotoFallback": os.getenv(
            "HITOKOTO_FALLBACK", "愿你今天也有好心情"
        ).strip(),
        "browserTimeout": _parse_int_env(
            "BROWSER_TIMEOUT", 120000, 5000, 300000
        ),
        "friendListTimeout": _parse_int_env(
            "FRIEND_LIST_WAIT_TIME", 2000, 500, 120000
        ),
        "taskRetryTimes": _parse_int_env("TASK_RETRY_TIMES", 3, 1, 5),
        "logLevel": log_level,
        # 服务器部署默认使用无头模式；需要本地排障时必须显式关闭，避免配置名
        # DEBUG 间接改变浏览器行为而造成难以复现的环境差异。
        "browserHeadless": _parse_bool_env("BROWSER_HEADLESS", True),
        # 聊天任务只依赖 DOM、脚本和接口数据。默认拦截图片、媒体与字体，可明显
        # 降低抖音页面的网络和内存占用；出现兼容问题时可单独关闭此开关。
        "blockBrowserResources": _parse_bool_env(
            "BLOCK_BROWSER_RESOURCES", True
        ),
    }

    if "[API]" in message_template and not _config_cache["hitokotoFallback"]:
        raise ConfigError("模板使用 [API] 时 HITOKOTO_FALLBACK 不能为空")

    return _config_cache


def _normalize_same_site(value: Any) -> Optional[str]:
    """把常见浏览器扩展导出值转换为 Playwright 接受的枚举。"""

    if not isinstance(value, str):
        return None
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }
    return mapping.get(value.strip().lower())


def sanitize_cookies(cookies: Any) -> List[Dict[str, Any]]:
    """把浏览器扩展导出的 Cookie 转换为 Playwright 所需的最小字段集。

    Chrome 扩展常会附带 ``hostOnly``、``session``、``storeId`` 和
    ``expirationDate`` 等字段。Playwright 会拒绝未知字段，因此这里采用白名单
    重建对象，并把 ``expirationDate`` 映射为 ``expires``。函数不会修改调用方
    传入的原始列表。
    """

    if not isinstance(cookies, list) or not cookies:
        raise ConfigError("Cookie 配置必须是非空 JSON 数组")

    sanitized: List[Dict[str, Any]] = []
    for index, cookie in enumerate(cookies, start=1):
        if not isinstance(cookie, dict):
            raise ConfigError(f"Cookie 第 {index} 项必须是 JSON 对象")

        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"Cookie 第 {index} 项缺少有效 name")
        if not isinstance(value, str):
            raise ConfigError(f"Cookie 第 {index} 项缺少有效 value")

        normalized_cookie: Dict[str, Any] = {"name": name, "value": value}
        url = cookie.get("url")
        domain = cookie.get("domain")
        if isinstance(url, str) and url:
            normalized_cookie["url"] = url
        elif isinstance(domain, str) and domain:
            normalized_cookie["domain"] = domain
            normalized_cookie["path"] = (
                cookie.get("path") if isinstance(cookie.get("path"), str) else "/"
            )
        else:
            raise ConfigError(f"Cookie 第 {index} 项必须包含 url 或 domain")

        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0:
            normalized_cookie["expires"] = float(expires)

        for boolean_key in ("httpOnly", "secure"):
            if isinstance(cookie.get(boolean_key), bool):
                normalized_cookie[boolean_key] = cookie[boolean_key]

        same_site = _normalize_same_site(cookie.get("sameSite"))
        if same_site is not None:
            normalized_cookie["sameSite"] = same_site

        partition_key = cookie.get("partitionKey")
        if isinstance(partition_key, str) and partition_key:
            normalized_cookie["partitionKey"] = partition_key

        sanitized.append(normalized_cookie)

    return sanitized


def _normalize_targets(raw_targets: Any, task_index: int) -> List[str]:
    """校验目标列表并按原顺序去重，避免同一轮重复发送。"""

    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError(f"TASKS 第 {task_index} 项的 targets 必须是非空数组")

    normalized_targets: List[str] = []
    seen = set()
    for target in raw_targets:
        if not isinstance(target, str) or not norm(target):
            raise ConfigError(
                f"TASKS 第 {task_index} 项的 targets 只能包含非空字符串"
            )
        normalized_target = norm(target)
        if normalized_target not in seen:
            seen.add(normalized_target)
            normalized_targets.append(normalized_target)
    return normalized_targets


def get_user_data() -> List[Dict[str, Any]]:
    """读取账号任务与对应 Cookie；任一账号无效时整体拒绝启动。"""

    global _user_data_cache
    if _user_data_cache is not None:
        return _user_data_cache

    tasks = _parse_json_env("TASKS", "[]")
    if not isinstance(tasks, list) or not tasks:
        raise ConfigError("TASKS 必须是非空 JSON 数组")

    parsed_users: List[Dict[str, Any]] = []
    seen_unique_ids = set()
    for task_index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ConfigError(f"TASKS 第 {task_index} 项必须是 JSON 对象")

        unique_id_raw = task.get("unique_id")
        unique_id = str(unique_id_raw).strip() if unique_id_raw is not None else ""
        if not unique_id:
            raise ConfigError(f"TASKS 第 {task_index} 项缺少 unique_id")
        if not COOKIE_ENV_SUFFIX_PATTERN.fullmatch(unique_id):
            raise ConfigError(
                f"TASKS 第 {task_index} 项的 unique_id 只能包含字母、数字和下划线"
            )
        # Cookie 环境变量名会统一转成大写，因此账号标识也必须按不区分大小写
        # 判重；否则 ``demo`` 与 ``DEMO`` 会读取同一个 Secret，造成账号串用。
        normalized_unique_id = unique_id.upper()
        if normalized_unique_id in seen_unique_ids:
            raise ConfigError(f"TASKS 中存在重复 unique_id：{unique_id}")
        seen_unique_ids.add(normalized_unique_id)

        username_raw = task.get("username", f"账号{task_index}")
        username = norm(username_raw) if isinstance(username_raw, str) else ""
        if not username:
            raise ConfigError(f"TASKS 第 {task_index} 项的 username 必须是非空字符串")

        cookies_key = f"COOKIES_{unique_id}".upper()
        cookies_raw = os.getenv(cookies_key)
        if not cookies_raw:
            raise ConfigError(f"TASKS 第 {task_index} 项缺少对应的 {cookies_key}")

        cookies = _parse_json_env(cookies_key, "[]")
        parsed_users.append(
            {
                "unique_id": unique_id,
                "username": username,
                "cookies": sanitize_cookies(cookies),
                "targets": _normalize_targets(task.get("targets"), task_index),
            }
        )

    _user_data_cache = parsed_users
    return _user_data_cache


def reset_config_cache() -> None:
    """清空缓存，供测试或显式重新加载环境变量时使用。"""

    global _config_cache, _user_data_cache
    _config_cache = None
    _user_data_cache = None


# 保留旧函数名，避免已有外部脚本在升级后立即失效。
def get_userData() -> List[Dict[str, Any]]:  # noqa: N802
    """兼容旧版驼峰命名；新代码应使用 :func:`get_user_data`。"""

    return get_user_data()


def main() -> int:
    """执行无副作用的启动前校验，仅输出脱敏后的账号数量。"""

    try:
        get_config()
        users = get_user_data()
    except ConfigError as exc:
        print(f"配置校验失败：{exc}", file=sys.stderr)
        return 2

    print(f"配置校验通过：共 {len(users)} 个账号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
