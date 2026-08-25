#!/bin/bash
set -euo pipefail

source /etc/douyin-spark-flow.env
# 运行时文件由 root 用 shell 安全引用格式生成，适合 source，但不保证符合 dotenv
# 的全部语法。显式标记环境已加载，让 main.py 跳过只有 root 可读的 /app/.env，
# 既避免权限失败，也避免同一值被第二种解析器再次解释后发生变化。
export DOUYIN_ENV_PRELOADED=1
umask 077

# flock 的文件描述符会在进程退出时自动释放，即使 Python 或 Chromium 崩溃也
# 不会留下永久锁；禁止重叠运行可避免同一批好友收到重复消息。
exec 9>/tmp/douyin-spark-flow.lock
if ! flock -n 9; then
  echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') 上一轮任务仍在运行，本轮跳过"
  exit 0
fi

echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') 开始定时任务"
if [[ "${CRON_SECOND:-0}" != "0" ]]; then
  sleep "$CRON_SECOND"
fi

cd /app
set +e
# 为异常页面设置总运行上限；TERM 后仍未退出时再 KILL，防止浏览器长期占用资源。
timeout --signal=TERM --kill-after=30s \
  "${TASK_MAX_RUNTIME_SECONDS:-1500}" python main.py
task_status=$?
set -e

if [[ $task_status -eq 0 ]]; then
  echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') 定时任务完成"
else
  echo "[docker] $(date '+%Y-%m-%d %H:%M:%S') 定时任务失败，退出码：$task_status" >&2
fi

exit "$task_status"
