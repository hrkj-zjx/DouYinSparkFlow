"""Docker 权限边界的静态回归测试。

这些断言专门覆盖普通 ``bash -n`` 无法发现的用户切换与配置加载路径问题；真实
镜像仍应在有 Docker 的 CI 中执行完整 smoke test。
"""

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DockerScriptTests(unittest.TestCase):
    """验证 cron 先完成 root 重定向，再以最小权限执行应用。"""

    def test_cron_redirects_as_root_then_drops_to_douyin(self):
        """低权限用户不能直接打开 root PID 1 的文件描述符。"""

        entrypoint = (REPOSITORY_ROOT / "docker" / "entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "root /usr/sbin/runuser -u douyin -- /app/docker/run-task.sh",
            entrypoint,
        )
        self.assertNotIn(
            "* * * douyin /app/docker/run-task.sh >> /proc/1/fd/1",
            entrypoint,
        )

    def test_runtime_marks_environment_preloaded(self):
        """shell 格式运行时文件不得再交给 python-dotenv 二次解析。"""

        run_task = (REPOSITORY_ROOT / "docker" / "run-task.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("export DOUYIN_ENV_PRELOADED=1", run_task)
        self.assertNotIn("export DOUYIN_ENV_FILE=", run_task)


if __name__ == "__main__":
    unittest.main()
