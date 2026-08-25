"""根据配置模板构建最终发送文本。"""

from utils.config import get_config
from utils.hitokoto import request_hitokoto


def build_message() -> str:
    """替换动态占位符并返回非空消息。

    一言请求函数自身保证失败时返回安全兜底文案，因此这里不会把 ``[error]``
    等内部状态发送给好友。模板中的字面量 ``\\n`` 会由发送层统一转换为换行。
    """

    message = get_config()["messageTemplate"]
    if "[API]" in message:
        message = message.replace("[API]", request_hitokoto())
    return message.strip()
