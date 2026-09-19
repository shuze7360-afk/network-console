"""运行日志：轮转文件，替代任何控制台窗口输出。

日志位置跟随应用数据目录（`~/.network-console-app/logs/console.log`，
可用 NETWORK_CONSOLE_DATA_DIR 覆盖）。MCP 服务器同样只写文件，不污染协议 stdout。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import appconfig


def get_logger(name: str = "netconsole") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        log_dir = appconfig.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "console.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    return logger
