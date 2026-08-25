"""DouYinSparkFlow 命令行入口。

导入本模块不会加载账号、启动 Chromium 或执行发送任务；只有显式调用 ``main``
或直接运行文件才会开始工作。
"""

from __future__ import annotations

import os
import sys


def _load_environment_file() -> None:
    """加载显式线上配置或项目目录中的可选 ``.env``。

    systemd 通过 ``DOUYIN_ENV_FILE`` 指向权限为 0640、仅 root 与专用组可读的
    站外配置文件。程序自行
    读取它，而不是使用 systemd ``EnvironmentFile``，可避免 Cookie 长期出现在
    服务管理器的环境转储中。未设置时仍兼容项目根目录的本地 ``.env``。
    """

    # Docker 定时脚本先 source 由入口脚本生成的严格白名单环境，再设置此内部
    # 标志。shell 引用语法与 dotenv 并不完全相同，已预加载时绝不能重复解析。
    if os.getenv("DOUYIN_ENV_PRELOADED") == "1":
        return

    configured_path = os.getenv("DOUYIN_ENV_FILE")
    env_path = configured_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env"
    )
    if configured_path and not os.path.isfile(env_path):
        raise FileNotFoundError(f"线上配置文件不存在：{env_path}")
    if not os.path.isfile(env_path):
        return

    from dotenv import load_dotenv

    # 显式文件代表部署者选定的唯一配置源，应覆盖继承的同名变量；本地默认文件
    # 保持 python-dotenv 的非覆盖语义，避免意外改写调用方主动设置的环境。
    # 配置和 Cookie 都应按字面值读取，不支持 ${NAME} 插值；否则消息正文或
    # Cookie 中偶然出现同样片段时会被运行环境静默改写。
    load_dotenv(
        env_path,
        override=bool(configured_path),
        interpolate=False,
    )


def main(arguments=()) -> int:
    """校验、只读预检或运行任务，并返回 systemd 可识别的退出码。"""

    try:
        _load_environment_file()
    except (OSError, ValueError) as exc:
        print(f"配置文件加载失败：{exc}", file=sys.stderr)
        return 2

    # 延迟导入确保 .env 先于配置缓存加载，同时保证 ``import main`` 没有任务副作用。
    from core.tasks import TaskBatchError, runTasks
    from utils.config import ConfigError, get_config, get_user_data

    try:
        if tuple(arguments) == ("--validate",):
            get_config()
            users = get_user_data()
            print(f"配置校验通过：共 {len(users)} 个账号")
        elif tuple(arguments) == ("--preflight",):
            from core.preflight import run_preflight

            passed_count = run_preflight()
            print(f"只读预检通过：共 {passed_count} 个账号，未执行消息发送")
        elif not arguments:
            runTasks()
        else:
            print(
                "用法：python main.py [--validate|--preflight]",
                file=sys.stderr,
            )
            return 2
    except ConfigError as exc:
        print(f"配置校验失败：{exc}", file=sys.stderr)
        return 2
    except TaskBatchError as exc:
        print(f"任务执行失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("任务已由用户中断", file=sys.stderr)
        return 130
    except Exception as exc:
        # 未预见的启动错误（例如 Chromium 缺失）仍必须返回非零，避免 cron 把零
        # 发送误判成任务成功；这里只输出异常摘要，详细堆栈由任务日志负责记录。
        print(f"任务启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
