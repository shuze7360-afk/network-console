"""应用配置与数据目录（公开版：一切按用户配置行事，默认不接管任何东西）。

- 数据目录默认 `~/.network-console-app`，可用环境变量 NETWORK_CONSOLE_DATA_DIR 覆盖
  （与任何已有安装互不重叠；测试可指向临时目录）。
- 配置文件 = 数据目录/config.json，可用 NETWORK_CONSOLE_CONFIG 指定其他位置。
- 原子写入 + 类型校验；损坏时 `config_state_ok()` 返回 False，调用方必须停止自动写入。
- 功能默认全部停用：是否启用由使用者决定，是否可用由实际检查决定。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR_ENV = "NETWORK_CONSOLE_DATA_DIR"
CONFIG_ENV = "NETWORK_CONSOLE_CONFIG"
DEFAULT_DIR_NAME = ".network-console-app"

FEATURE_KEYS = ("basic", "auth", "proxy", "service")

_DEFAULTS: dict = {
    "features": {
        "basic": {"enabled": True,
                  "probe_url": "http://www.msftconnecttest.com/connecttest.txt",
                  "ok_status": 200},
        "auth": {"enabled": False, "title": "网络认证",
                 "login_url": "", "match_prefixes": [], "hosts_expect": {}},
        "proxy": {"enabled": False, "title": "代理连接",
                  "endpoint": "127.0.0.1:8080",
                  "probe_url": "http://www.msftconnecttest.com/connecttest.txt",
                  "ok_status": 200,
                  "program": {"path": "", "args": [], "workdir": ""}},
        "service": {"enabled": False, "title": "本地服务",
                    "endpoint": "127.0.0.1:9000", "health_path": "/health",
                    "program": {"path": "", "args": [], "workdir": ""}},
    },
    "cleanup": {"controlled_endpoint": "127.0.0.1:8080",
                "bypass_defaults": ["localhost", "127.0.0.1", "<local>"]},
}


def data_dir() -> Path:
    env = os.environ.get(APP_DIR_ENV)
    return Path(env) if env else Path.home() / DEFAULT_DIR_NAME


def config_file() -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else data_dir() / "config.json"


def _raw_load() -> dict | None:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    feats = data.get("features")
    if feats is not None and not isinstance(feats, dict):
        return None
    for key in FEATURE_KEYS:
        f = (feats or {}).get(key)
        if f is not None and not isinstance(f, dict):
            return None
        if isinstance(f, dict) and "enabled" in f and not isinstance(f["enabled"], bool):
            return None
    return data



def config_state_ok() -> bool:
    """配置文件存在但损坏/非法 → False（调用方须停止自动写入）。"""
    if not config_file().exists():
        return True
    return _raw_load() is not None


def load_config() -> dict:
    data = _raw_load()
    if data is None:
        data = {}
    cfg = json.loads(json.dumps(_DEFAULTS))  # 深拷贝默认值
    feats = data.get("features") or {}
    for key in FEATURE_KEYS:
        if isinstance(feats.get(key), dict):
            cfg["features"][key].update(feats[key])
    if isinstance(data.get("cleanup"), dict):
        cfg["cleanup"].update(data["cleanup"])
    return cfg


def save_config(cfg: dict) -> None:
    config_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = config_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, config_file())


def set_feature_enabled(name: str, enabled: bool) -> dict:
    cfg = load_config()
    cfg["features"][name]["enabled"] = bool(enabled)
    save_config(cfg)
    return cfg


def feature(name: str) -> dict:
    return load_config()["features"][name]


def cleanup_settings() -> dict:
    return load_config()["cleanup"]
