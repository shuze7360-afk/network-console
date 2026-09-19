"""离线测试公共工具：把数据目录重定向到临时目录。

appconfig 的路径函数每次调用动态读取 NETWORK_CONSOLE_DATA_DIR，
因此测试在任意时刻调用 fresh_data() 都能整体重定向（含审计、配置、
快照、通知状态、日志），不依赖导入顺序。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="netconsole-public-offline-"))


def fresh_data(sub: str) -> Path:
    """启用独立临时数据目录并返回路径（每个用例独立子目录，互不串扰）。"""
    d = TMP / sub
    d.mkdir(parents=True, exist_ok=True)
    os.environ["NETWORK_CONSOLE_DATA_DIR"] = str(d)
    return d


def reset_env() -> None:
    os.environ.pop("NETWORK_CONSOLE_DATA_DIR", None)
