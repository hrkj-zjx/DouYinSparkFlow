import json
import os
import unittest
from unittest.mock import patch

from utils.config import (
    ConfigError,
    get_config,
    get_user_data,
    reset_config_cache,
    sanitize_cookies,
)


class ConfigTests(unittest.TestCase):
    """验证配置会在浏览器启动前完成严格且不泄密的校验。"""

    def tearDown(self):
        # 配置模块会缓存解析结果；每个用例后清空，确保环境变量互不污染。
        reset_config_cache()

    def test_browser_extension_cookies_are_rebuilt_with_playwright_fields(self):
        original = [
            {
                "name": "sessionid",
                "value": "仅用于单元测试的中文值",
                "domain": ".example.test",
                "path": "/",
                "expirationDate": 1813212648.5,
                "httpOnly": True,
                "secure": True,
                "sameSite": "no_restriction",
                "hostOnly": False,
                "session": False,
                "storeId": "0",
            }
        ]

        result = sanitize_cookies(original)

        self.assertEqual(result[0]["value"], "仅用于单元测试的中文值")
        self.assertEqual(result[0]["expires"], 1813212648.5)
        self.assertEqual(result[0]["sameSite"], "None")
        self.assertNotIn("expirationDate", result[0])
        self.assertNotIn("hostOnly", result[0])
        self.assertNotIn("session", result[0])
        self.assertNotIn("storeId", result[0])
        # 转换函数不能原地删除字段，否则缓存或重试会拿到残缺 Cookie。
        self.assertIn("hostOnly", original[0])

    def test_user_data_preserves_unicode_and_deduplicates_targets(self):
        task_value = [
            {
                "username": "测试账号",
                "unique_id": "account_01",
                "targets": [" 好友Ａ ", "好友A", "另一位好友"],
            }
        ]
        cookie_value = [
            {
                "name": "sessionid",
                "value": "中文不会乱码",
                "domain": ".example.test",
                "path": "/",
            }
        ]
        environment = {
            "TASKS": json.dumps(task_value, ensure_ascii=False),
            "COOKIES_ACCOUNT_01": json.dumps(cookie_value, ensure_ascii=False),
        }

        with patch.dict(os.environ, environment, clear=True):
            reset_config_cache()
            users = get_user_data()

        self.assertEqual(users[0]["targets"], ["好友A", "另一位好友"])
        self.assertEqual(users[0]["cookies"][0]["value"], "中文不会乱码")

    def test_secret_json_error_does_not_echo_secret_value(self):
        environment = {
            "TASKS": json.dumps(
                [{"username": "u", "unique_id": "one", "targets": ["t"]}]
            ),
            "COOKIES_ONE": "not-json-SENSITIVE-CANARY",
        }

        with patch.dict(os.environ, environment, clear=True):
            reset_config_cache()
            with self.assertRaises(ConfigError) as caught:
                get_user_data()

        self.assertIn("COOKIES_ONE", str(caught.exception))
        self.assertNotIn("SENSITIVE-CANARY", str(caught.exception))

    def test_unique_id_is_case_insensitively_unique(self):
        """大小写不同但映射到同一 Cookie 变量的账号必须在启动前被拒绝。"""

        environment = {
            "TASKS": json.dumps(
                [
                    {"username": "一号", "unique_id": "demo", "targets": ["甲"]},
                    {"username": "二号", "unique_id": "DEMO", "targets": ["乙"]},
                ],
                ensure_ascii=False,
            ),
            "COOKIES_DEMO": json.dumps(
                [
                    {
                        "name": "session",
                        "value": "仅用于测试",
                        "domain": ".example.test",
                    }
                ],
                ensure_ascii=False,
            ),
        }

        with patch.dict(os.environ, environment, clear=True):
            reset_config_cache()
            with self.assertRaises(ConfigError) as caught:
                get_user_data()

        self.assertIn("重复 unique_id", str(caught.exception))

    def test_numeric_limits_fail_fast(self):
        with patch.dict(os.environ, {"BROWSER_TIMEOUT": "4999"}, clear=True):
            reset_config_cache()
            with self.assertRaisesRegex(ConfigError, "BROWSER_TIMEOUT"):
                get_config()


if __name__ == "__main__":
    unittest.main()
