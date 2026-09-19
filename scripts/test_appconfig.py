"""应用配置层单元测试（全离线，临时数据目录）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole import appconfig  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="appconfig-"))
RESULTS: list[tuple[str, bool, str]] = []


def case(name: str, fn) -> None:
    try:
        got = fn()
        ok_, detail = bool(got), "" if got is True else f"实际 {got}"
    except Exception as exc:  # noqa: BLE001
        ok_, detail = False, f"{type(exc).__name__}: {exc}"
    RESULTS.append((name, ok_, detail))
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + ("" if ok_ else f" — {detail}"))


def main() -> int:
    def setup(name: str, content: str | None):
        appconfig.CONFIG_ENV = None  # 不用环境变量
        appconfig.APP_DIR_ENV = None
        appconfig.DEFAULT_DIR_NAME = TMP / name / ".nc-app"
        # 直接固定配置文件路径
        appconfig.config_file = lambda: TMP / f"{name}.json"  # type: ignore[method-assign]
        if content is None:
            if appconfig.config_file().exists():
                appconfig.config_file().unlink()
        else:
            appconfig.config_file().write_text(content, encoding="utf-8")

    def t1_默认全停用():
        setup("t1", None)
        cfg = appconfig.load_config()
        return (cfg["features"]["basic"]["enabled"] is True
                and cfg["features"]["auth"]["enabled"] is False
                and cfg["features"]["proxy"]["enabled"] is False
                and cfg["features"]["service"]["enabled"] is False)
    case("T1 默认：仅基础网络启用，其余停用", t1_默认全停用)

    def t2_开关翻转与持久化():
        setup("t2", None)
        appconfig.set_feature_enabled("proxy", True)
        cfg = appconfig.load_config()
        ok1 = cfg["features"]["proxy"]["enabled"] is True
        appconfig.set_feature_enabled("proxy", False)
        cfg = appconfig.load_config()
        return ok1 and cfg["features"]["proxy"]["enabled"] is False
    case("T2 开关翻转并持久化", t2_开关翻转与持久化)

    def t3_损坏检测():
        setup("t3", "{corrupted!!!")
        return appconfig.config_state_ok() is False
    case("T3 损坏配置 → config_state_ok=False", t3_损坏检测)

    def t4_损坏时读取降级为默认且不抛异常():
        setup("t4", "{corrupted!!!")
        cfg = appconfig.load_config()
        return cfg["features"]["proxy"]["enabled"] is False
    case("T4 损坏时读取降级为默认值", t4_损坏时读取降级为默认且不抛异常)

    def t5_原子替换无临时残留():
        setup("t5", None)
        appconfig.set_feature_enabled("auth", True)
        leftovers = list(TMP.glob("t5.json.tmp"))
        data = appconfig._raw_load()
        return not leftovers and isinstance(data, dict)
    case("T5 原子写入无临时残留", t5_原子替换无临时残留)

    def t6_非法类型视为损坏():
        setup("t6", '{"features": {"proxy": {"enabled": "yes"}}}')
        return appconfig.config_state_ok() is False
    case("T6 非布尔 enabled 视为非法配置", t6_非法类型视为损坏)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
