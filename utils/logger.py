import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
DEFAULT_LOG_FILE = "logs/app.log"


def resolve_log_level(level):
    if isinstance(level, int):
        return level

    if isinstance(level, str):
        mapping = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        return mapping.get(level.lower(), logging.INFO)

    return logging.INFO


def setup_logger(name="app", level="Info", log_file: Optional[str] = None):
    """创建控制台与轮转文件日志器。

    ``LOG_FILE`` 允许 systemd 把日志写入专用可写目录，而不是给应用源码目录写
    权限。文件处理器使用延迟打开，单纯导入模块或执行配置校验不会创建空日志。
    """

    resolved_level = resolve_log_level(level)
    resolved_log_file = log_file or os.getenv("LOG_FILE", DEFAULT_LOG_FILE)
    os.makedirs(os.path.dirname(resolved_log_file) or ".", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    if not logger.handlers:
        console_handler = logging.StreamHandler()
        file_handler = RotatingFileHandler(
            resolved_log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    for handler in logger.handlers:
        handler.setLevel(resolved_level)
        handler.setFormatter(formatter)

    return logger


if __name__ == "__main__":
    logger = setup_logger(level="Debug")
    logger.debug("这是一个调试信息")
    logger.info("这是一个普通信息")
    logger.warning("这是一个警告信息")
    logger.error("这是一个错误信息")
    logger.critical("这是一个严重错误信息")
