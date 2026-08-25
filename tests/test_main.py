import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from utils.config import reset_config_cache


class MainCommandTests(unittest.TestCase):
    """验证 systemd 站外配置和无浏览器校验模式。"""

    def tearDown(self):
        reset_config_cache()

    def test_validate_loads_explicit_protected_environment_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "service.env"
            config_path.write_text(
                "TASKS='[{\"username\":\"测试\",\"unique_id\":\"demo_01\","
                "\"targets\":[\"目标\"]}]'\n"
                "COOKIES_DEMO_01='[{\"name\":\"sessionid\",\"value\":\"fake\","
                "\"domain\":\".example.test\",\"path\":\"/\"}]'\n"
                "LOG_LEVEL=INFO\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"DOUYIN_ENV_FILE": str(config_path)},
                clear=True,
            ):
                reset_config_cache()
                result = main.main(("--validate",))

        self.assertEqual(result, 0)

    def test_missing_explicit_environment_file_fails_before_task_import(self):
        with patch.dict(
            os.environ,
            {"DOUYIN_ENV_FILE": "/definitely/missing/douyin.env"},
            clear=True,
        ):
            result = main.main(("--validate",))

        self.assertEqual(result, 2)

    def test_preloaded_environment_skips_unreadable_default_file(self):
        """Docker 已 source 白名单环境后不应再次尝试读取仅 root 可读的文件。"""

        with patch.dict(
            os.environ,
            {
                "DOUYIN_ENV_PRELOADED": "1",
                "DOUYIN_ENV_FILE": "/definitely/missing/douyin.env",
            },
            clear=True,
        ):
            main._load_environment_file()

    def test_explicit_file_keeps_interpolation_syntax_literal(self):
        """消息或 Cookie 中的 ${NAME} 必须按字面读取，不能受宿主环境污染。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "literal.env"
            config_path.write_text(
                "MESSAGE_TEMPLATE='今日 #火花 ${HOME}'\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DOUYIN_ENV_FILE": str(config_path),
                    "HOME": "/sensitive/host/path",
                },
                clear=True,
            ):
                main._load_environment_file()
                self.assertEqual(
                    os.environ["MESSAGE_TEMPLATE"],
                    "今日 #火花 ${HOME}",
                )


if __name__ == "__main__":
    unittest.main()
