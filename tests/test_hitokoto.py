import unittest
from unittest.mock import Mock, patch

from core.msg_builder import build_message
from utils.hitokoto import request_hitokoto


class HitokotoTests(unittest.TestCase):
    """验证外部文案接口不会把内部错误直接发送给好友。"""

    @patch("utils.hitokoto.requests.get")
    @patch("utils.hitokoto.get_config")
    def test_request_uses_structured_category_parameters(self, get_config_mock, get_mock):
        get_config_mock.return_value = {
            "hitokotoTypes": ["文学", "影视"],
            "hitokotoFallback": "安全兜底",
        }
        response = Mock()
        response.json.return_value = {
            "hitokoto": "测试句子",
            "from": "测试来源",
            "from_who": "测试作者",
        }
        get_mock.return_value = response

        result = request_hitokoto()

        self.assertEqual(result, "测试句子 —— 测试来源 (测试作者)")
        self.assertEqual(get_mock.call_args.kwargs["params"], [("c", "d"), ("c", "h")])
        self.assertEqual(get_mock.call_args.kwargs["timeout"], 10)

    @patch("utils.hitokoto.requests.get", side_effect=OSError("network down"))
    @patch("utils.hitokoto.get_config")
    def test_unexpected_os_error_is_not_swallowed(self, get_config_mock, _get_mock):
        # 非 requests 异常代表本机编程或系统问题，应显式失败，而不是伪装成接口降级。
        get_config_mock.return_value = {
            "hitokotoTypes": [],
            "hitokotoFallback": "安全兜底",
        }
        with self.assertRaises(OSError):
            request_hitokoto()

    @patch("core.msg_builder.request_hitokoto", return_value="安全兜底")
    @patch("core.msg_builder.get_config")
    def test_message_builder_replaces_all_api_placeholders(
        self, get_config_mock, request_mock
    ):
        get_config_mock.return_value = {
            "messageTemplate": "开头\\n[API]\\n[API]"
        }

        result = build_message()

        self.assertEqual(result, "开头\\n安全兜底\\n安全兜底")
        request_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
