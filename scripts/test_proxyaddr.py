"""proxyaddr 单元测试：精确解析与归属判定（纯函数，全离线，端点来自参数）。

背景：v1 按startswith("127.0.0.1:{port}") / ":{port}" in v 判断归属，
127.0.0.1:18080 会被误判为受控。本套件锁定解析函数的行为边界。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from netconsole import proxyaddr  # noqa: E402

EP = "127.0.0.1:8080"
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
    case("PA1 简单形态解析", lambda:
         proxyaddr.parse_wininet_server("127.0.0.1:8080") == {("127.0.0.1", 8080)})

    case("PA2 分协议形态解析", lambda:
         proxyaddr.parse_wininet_server("http=127.0.0.1:8080;https=127.0.0.1:8080")
         == {("127.0.0.1", 8080)})

    case("PA3 端口前缀陷阱：18080 / 80806 不算受控", lambda:
         proxyaddr.wininet_references_controlled("127.0.0.1:18080", EP) is False
         and proxyaddr.env_references_controlled("http://127.0.0.1:18080", EP) is False
         and proxyaddr.wininet_references_controlled("127.0.0.1:80806", EP) is False)

    case("PA4 混合指向（受控+其他）不算受控，清理必须拒绝", lambda:
         proxyaddr.wininet_points_at_controlled("127.0.0.1:8080;127.0.0.1:7890", EP) is False)

    case("PA5 空串：清理权限视为可清（退化态），状态展示不算接入", lambda:
         proxyaddr.wininet_points_at_controlled("", EP) is True
         and proxyaddr.wininet_references_controlled("", EP) is False)

    case("PA6 环境变量：http:// 前缀、localhost、多项、非受控", lambda:
         proxyaddr.env_references_controlled("http://127.0.0.1:8080", EP) is True
         and proxyaddr.env_references_controlled("localhost:8080", EP) is True
         and proxyaddr.env_references_controlled("http://127.0.0.1:7890", EP) is False)

    case("PA7 乱串不崩溃、返回空集", lambda:
         proxyaddr.parse_wininet_server("::garbage::") == set()
         and proxyaddr.parse_env_proxy("") == set()
         and proxyaddr.parse_wininet_server(None) == set())  # type: ignore[arg-type]

    case("PA8 localhost:8080 属于受控主机；自定义端点同样生效", lambda:
         proxyaddr.wininet_references_controlled("localhost:8080", EP) is True
         and proxyaddr.wininet_references_controlled("192.0.2.10:3128", "192.0.2.10:3128") is True
         and proxyaddr.wininet_references_controlled("192.0.2.10:3128", EP) is False)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
