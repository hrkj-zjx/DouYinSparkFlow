#!/bin/bash
set -euo pipefail

CONFIG_ENV_PATH="/app/.env"
RUNTIME_ENV_PATH="/etc/douyin-spark-flow.env"
SCHEDULE_PATH="/tmp/douyin-spark-flow.schedule"

if [[ ! -f "$CONFIG_ENV_PATH" ]]; then
  echo "[docker] 配置文件不存在：$CONFIG_ENV_PATH" >&2
  exit 1
fi

# 只把应用需要的变量写给定时任务，避免容器中的无关密钥被浏览器子进程继承。
# Python 同时负责范围、时区和变量名校验，生成文件采用原子替换且限制为 0640。
python - <<'PY'
import os
import pwd
import re
import shlex
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

config_path = "/app/.env"
runtime_env_path = Path("/etc/douyin-spark-flow.env")
schedule_path = Path("/tmp/douyin-spark-flow.schedule")

allowed_exact = {
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
    "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONUNBUFFERED",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}
cookie_name_pattern = re.compile(r"^COOKIES_[A-Z0-9_]+$")


def is_allowed(name: str) -> bool:
    """仅允许已知配置项和按账号生成的 Cookie 变量。"""

    return name in allowed_exact or cookie_name_pattern.fullmatch(name) is not None


def parse_integer(name: str, default: str, minimum: int, maximum: int) -> int:
    """解析定时相关整数，拒绝换行注入和 cron 无法识别的范围。"""

    raw_value = merged_values.get(name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"[docker] {name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise SystemExit(
            f"[docker] {name} 必须位于 {minimum} 到 {maximum} 之间"
        )
    return value


file_values = {
    key: value
    # 所有配置都按字面值处理，禁止 ${NAME} 插值静默改变消息或 Cookie。
    for key, value in dotenv_values(config_path, interpolate=False).items()
    if value is not None
}
unknown_names = sorted(name for name in file_values if not is_allowed(name))
if unknown_names:
    raise SystemExit(
        "[docker] 配置包含不受支持的变量：" + ", ".join(unknown_names)
    )

# 文件配置优先于 compose 环境变量，但两侧都必须经过同一允许列表。
merged_values = {
    key: value for key, value in os.environ.items() if is_allowed(key)
}
merged_values.update(file_values)

hour = parse_integer("CRON_HOUR", "9", 0, 23)
minute = parse_integer("CRON_MINUTE", "0", 0, 59)
second = parse_integer("CRON_SECOND", "0", 0, 59)
max_runtime = parse_integer("TASK_MAX_RUNTIME_SECONDS", "1500", 60, 7200)
merged_values["TASK_MAX_RUNTIME_SECONDS"] = str(max_runtime)

timezone = str(merged_values.get("TZ", "Asia/Shanghai")).strip()
try:
    ZoneInfo(timezone)
except (ZoneInfoNotFoundError, ValueError) as exc:
    raise SystemExit(f"[docker] TZ 不是有效时区：{timezone}") from exc
merged_values["TZ"] = timezone

account = pwd.getpwnam("douyin")
runtime_env_path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=runtime_env_path.parent, delete=False
) as env_file:
    temp_env_path = Path(env_file.name)
    for key in sorted(merged_values):
        env_file.write(f"export {key}={shlex.quote(str(merged_values[key]))}\n")

os.chmod(temp_env_path, 0o640)
os.chown(temp_env_path, 0, account.pw_gid)
os.replace(temp_env_path, runtime_env_path)

# 定时文件不含账号数据，仅供入口脚本读取标准化后的数值。
schedule_path.write_text(
    f"{minute} {hour} {second} {timezone}\n", encoding="utf-8"
)
os.chmod(schedule_path, 0o644)
PY

read -r CRON_MINUTE CRON_HOUR CRON_SECOND TZ < "$SCHEDULE_PATH"
export TZ

# 挂载的日志目录可能由 Docker 以 root 创建；只调整该专用目录，不触碰宿主机
# 其他路径。真实配置仍保持 root:douyin 0640。
mkdir -p /app/logs
chown douyin:douyin /app/logs

# 在注册定时任务前，以实际运行用户执行完整应用配置校验，但不启动浏览器。
runuser -u douyin -- /bin/bash -c \
  "source '$RUNTIME_ENV_PATH'; cd /app; python -m utils.config"

cat > /etc/cron.d/douyin-spark-flow <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HOME=/home/douyin
CRON_TZ=${TZ}
# cron 的重定向由条目用户先打开。使用 root 只负责打开同 UID 的 PID 1 文件描述
# 符，实际任务随即由 runuser 降权为 douyin；若直接把条目用户写成 douyin，Linux
# 的 /proc 跨 UID 检查会在脚本启动前拒绝重定向。
${CRON_MINUTE} ${CRON_HOUR} * * * root /usr/sbin/runuser -u douyin -- /app/docker/run-task.sh >> /proc/1/fd/1 2>> /proc/1/fd/2
EOF

chmod 0644 /etc/cron.d/douyin-spark-flow

echo "[docker] 时区：${TZ}"
echo "[docker] 定时：每天 ${CRON_HOUR}:${CRON_MINUTE}:${CRON_SECOND}"
echo "[docker] 配置校验通过，等待定时执行"

exec cron -f
