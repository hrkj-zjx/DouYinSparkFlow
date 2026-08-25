import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.export_github_env import filter_allowed_items, main


class GithubEnvironmentExportTests(unittest.TestCase):
    """确保工作流只向浏览器进程暴露本应用所需的最小配置。"""

    def test_filter_rejects_unrelated_repository_secret(self):
        result = filter_allowed_items(
            {"LOG_LEVEL": "INFO"},
            {
                "COOKIES_TEST_01": "fake-cookie-json",
                "UNRELATED_DEPLOY_TOKEN": "SENSITIVE-CANARY",
            },
        )

        self.assertEqual(
            result,
            {"LOG_LEVEL": "INFO", "COOKIES_TEST_01": "fake-cookie-json"},
        )

    def test_main_reads_only_dedicated_config_and_never_creates_dotenv(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            github_env_path = Path(temporary_directory) / "github-env"
            environment = {
                "GITHUB_ENV": str(github_env_path),
                "DOUYIN_CONFIG_JSON": json.dumps(
                    {
                        "LOG_LEVEL": "INFO",
                        "TASKS": [],
                        "COOKIES_TEST_01": "fake-cookie-json",
                    }
                ),
                # 即使 Runner 环境另有敏感值，导出器也不会枚举或读取它。
                "UNRELATED_DEPLOY_TOKEN": "SENSITIVE-CANARY",
            }

            with patch.dict(os.environ, environment, clear=True):
                with patch("utils.export_github_env.print"):
                    main()

            content = github_env_path.read_text(encoding="utf-8")
            self.assertIn("COOKIES_TEST_01", content)
            self.assertIn("TASKS", content)
            self.assertNotIn("UNRELATED_DEPLOY_TOKEN", content)
            self.assertNotIn("SENSITIVE-CANARY", content)
            self.assertFalse((Path(temporary_directory) / ".env").exists())

    def test_main_rejects_unknown_key_inside_dedicated_secret(self):
        """专用配置中出现未知键时应失败，且错误不能包含对应的敏感值。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = {
                "GITHUB_ENV": str(Path(temporary_directory) / "github-env"),
                "DOUYIN_CONFIG_JSON": json.dumps(
                    {"UNRELATED_DEPLOY_TOKEN": "SENSITIVE-CANARY"}
                ),
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch("utils.export_github_env.print") as mocked_print:
                    with self.assertRaises(SystemExit):
                        main()

            rendered_calls = " ".join(str(call) for call in mocked_print.call_args_list)
            self.assertNotIn("SENSITIVE-CANARY", rendered_calls)


if __name__ == "__main__":
    unittest.main()
