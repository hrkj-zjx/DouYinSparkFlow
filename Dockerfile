FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# 浏览器访问的是外部站点，定时任务不能以 root 身份运行。固定 UID 便于宿主机
# 预先授予日志目录权限，也避免不同构建产生漂移。
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron util-linux \
    && useradd --system --uid 10001 --create-home --home-dir /home/douyin douyin \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 采用运行文件白名单，确保即使 `.dockerignore` 配置失误，Cookie、文档和测试
# 数据也不会进入镜像层。
COPY --chown=douyin:douyin main.py /app/main.py
COPY --chown=douyin:douyin core /app/core
COPY --chown=douyin:douyin utils /app/utils
COPY --chown=douyin:douyin docker /app/docker

RUN mkdir -p /app/logs \
    && chown douyin:douyin /app/logs \
    && chmod 0755 /app/docker/entrypoint.sh /app/docker/run-task.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
