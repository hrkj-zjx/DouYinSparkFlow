"""一言接口客户端。

外部接口失败时必须返回可安全发送的兜底文案，不能把内部错误标记、异常详情或
响应正文拼进发给好友的消息。
"""

import logging
from typing import Dict, List, Tuple

import requests

from utils.config import get_config


logger = logging.getLogger(__name__)

HITOKOTO_API_URL = "https://v1.hitokoto.cn/"
HITOKOTO_TYPE_CODES: Dict[str, str] = {
    "动画": "a",
    "漫画": "b",
    "游戏": "c",
    "文学": "d",
    "原创": "e",
    "来自网络": "f",
    "其他": "g",
    "影视": "h",
    "诗词": "i",
    "哲学": "k",
    "抖机灵": "l",
}


def _build_category_params(selected_types: List[str]) -> List[Tuple[str, str]]:
    """构建重复 ``c`` 参数，避免手工拼接 URL 造成转义或分隔错误。"""

    return [
        ("c", HITOKOTO_TYPE_CODES[type_name])
        for type_name in selected_types
        if type_name in HITOKOTO_TYPE_CODES
    ]


def request_hitokoto() -> str:
    """请求一言内容；任何不可信响应都降级为本地兜底文案。"""

    config = get_config()
    fallback = config["hitokotoFallback"]

    try:
        response = requests.get(
            HITOKOTO_API_URL,
            params=_build_category_params(config["hitokotoTypes"]),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("响应根节点不是对象")

        content = data.get("hitokoto")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("响应缺少 hitokoto 文本")

        source = data.get("from")
        author = data.get("from_who")
        safe_source = source.strip() if isinstance(source, str) and source.strip() else "未知来源"
        safe_author = author.strip() if isinstance(author, str) and author.strip() else "未知作者"
        return f"{content.strip()} —— {safe_source} ({safe_author})"
    except (requests.RequestException, ValueError, TypeError) as exc:
        # 日志只保留异常类型，不记录第三方响应正文，避免意外写入不可信或敏感内容。
        logger.warning("一言接口不可用，已使用本地兜底文案：%s", type(exc).__name__)
        return fallback
