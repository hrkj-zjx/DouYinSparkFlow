"""把单一 GitHub Secret 中的应用配置安全地传给后续任务步骤。

工作流只把 ``DOUYIN_CONFIG_JSON`` 这一个专用 Secret 交给本脚本，不再枚举
仓库或 Environment 的全部 Secrets。脚本继续执行键名白名单，并直接写入
``GITHUB_ENV``，不会在工作区额外生成包含 Cookie 的 ``.env`` 文件。
"""

import json
import os
import re
import secrets
import sys
from typing import Any, Dict, TextIO


ALLOWED_EXACT_KEYS = {
    "PROXY_ADDRESS",
    "CRON_HOUR",
    "CRON_MINUTE",
    "CRON_SECOND",
    "TZ",
    "MESSAGE_TEMPLATE",
    "HITOKOTO_TYPES",
    "HITOKOTO_FALLBACK",
    "BROWSER_TIMEOUT",
    "FRIEND_LIST_WAIT_TIME",
    "TASK_RETRY_TIMES",
    "TASK_MAX_RUNTIME_SECONDS",
    "LOG_LEVEL",
    "LOG_FILE",
    "TASKS",
    "BROWSER_HEADLESS",
    "BLOCK_BROWSER_RESOURCES",
}
COOKIE_KEY_PATTERN = re.compile(r"^COOKIES_[A-Z0-9_]+$")


def fail(message: str) -> None:
    """以不包含原始配置值的错误结束工作流。"""

    print(message, file=sys.stderr)
    raise SystemExit(1)


def is_allowed_key(key: str) -> bool:
    """判断环境变量是否属于应用最小权限集合。"""

    return key in ALLOWED_EXACT_KEYS or COOKIE_KEY_PATTERN.fullmatch(key) is not None


def as_env_string(value: Any) -> str:
    """保留字符串原值，其他 JSON 类型使用紧凑格式编码。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def append_github_env_block(env_file: TextIO, key: str, value: str) -> None:
    """用随机且不与内容冲突的分隔符写入 GitHub 多行环境格式。"""

    delimiter = f"__DOUYIN_ENV_{secrets.token_hex(16)}__"
    while delimiter in value:
        delimiter = f"__DOUYIN_ENV_{secrets.token_hex(16)}__"
    env_file.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def parse_json_object(name: str, raw_value: str) -> Dict[str, Any]:
    """解析 GitHub context，并要求根节点为对象。"""

    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        fail(f"{name} 不是有效 JSON：第 {exc.lineno} 行，第 {exc.colno} 列")
    if not isinstance(value, dict):
        fail(f"{name} 必须是 JSON 对象")
    return value


def filter_allowed_items(*mappings: Dict[str, Any]) -> Dict[str, str]:
    """合并允许项；后传入的 Secrets 可覆盖同名普通 Variables。"""

    filtered: Dict[str, str] = {}
    for mapping in mappings:
        for raw_key, value in mapping.items():
            key = str(raw_key)
            if is_allowed_key(key):
                filtered[key] = as_env_string(value)
    return filtered


def export_items(env_file: TextIO, items: Dict[str, str]) -> None:
    """按键名稳定排序，便于测试且不向控制台打印任何敏感标识。"""

    for key in sorted(items):
        append_github_env_block(env_file, key, items[key])


def main() -> None:
    """校验并导出单一应用配置 Secret，不读取任何其他仓库密钥。"""

    github_env = os.getenv("GITHUB_ENV")
    if not github_env:
        fail("GITHUB_ENV 未设置")

    raw_config = os.getenv("DOUYIN_CONFIG_JSON", "")
    if not raw_config:
        fail("DOUYIN_CONFIG_JSON 未设置")

    config_map = parse_json_object("DOUYIN_CONFIG_JSON", raw_config)
    unknown_keys = sorted(
        str(key) for key in config_map if not is_allowed_key(str(key))
    )
    if unknown_keys:
        # 只报告键名，不输出可能包含 Cookie 的配置值。专用 Secret 中出现未知键
        # 通常代表复制错了对象，直接失败比静默丢弃更容易发现部署配置偏差。
        fail("DOUYIN_CONFIG_JSON 包含不受支持的键：" + ", ".join(unknown_keys))

    filtered_items = filter_allowed_items(config_map)
    if not filtered_items:
        fail("没有找到可供 DouYinSparkFlow 使用的允许配置")

    with open(github_env, "a", encoding="utf-8") as env_file:
        export_items(env_file, filtered_items)

    # 只输出数量，不暴露抖音号、Cookie 变量名或任何配置内容。
    print(f"已导出 {len(filtered_items)} 个应用配置")


if __name__ == "__main__":
    main()
