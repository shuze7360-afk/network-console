"""proxyaddr 单元测试 v2：三态解析与严格归属（纯函数，全离线）。

2.0.0 审核反例覆盖：
- 远端与本机同端口不混淆；IPv4 与 IPv6 不合并；
- 非空非法地址、混合有效与非法片段 → 不得取得清理权限；
- 状态展示「引用受控端点」与「修改权限」分开判断；
- 空值三态区分：真正空值保留空地址残留处理，解析失败不算空。
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
    case("PA1 三态：真正空值/有效/非法", lambda:
         proxyaddr.parse_wininet_server(None).kind == "empty"
         and proxyaddr.parse_wininet_server("   ").kind == "empty"
         and proxyaddr.parse_wininet_server("127.0.0.1:8080").kind == "ok"
         and proxyaddr.parse_wininet_server("garbage").kind == "invalid")

    case("PA2 分协议形态与混合端点解析", lambda:
         proxyaddr.parse_wininet_server("http=127.0.0.1:8080;https=127.0.0.1:8080")
         == proxyaddr.ParseResult("ok", frozenset({("127.0.0.1", 8080)}))
         and proxyaddr.parse_wininet_server("127.0.0.1:8080;127.0.0.1:7890").endpoints
         == frozenset({("127.0.0.1", 8080), ("127.0.0.1", 7890)}))

    case("PA3 端口前缀陷阱：18080 / 80806 不算受控", lambda:
         proxyaddr.wininet_references_controlled("127.0.0.1:18080", EP) is False
         and proxyaddr.env_references_controlled("http://127.0.0.1:18080", EP) is False
         and proxyaddr.wininet_references_controlled("127.0.0.1:80806", EP) is False)

    case("PA4 反例：远端与本机同端口不混淆", lambda:
         proxyaddr.wininet_points_at_controlled("192.0.2.10:8080", EP) is False
         and proxyaddr.wininet_references_controlled("192.0.2.10:8080", EP) is False)

    case("PA5 反例：IPv4 与 IPv6 不同监听不合并", lambda:
         proxyaddr.parse_wininet_server("[::1]:8080").endpoints == frozenset({("::1", 8080)})
         and proxyaddr.wininet_points_at_controlled("[::1]:8080", EP) is False
         and proxyaddr.wininet_points_at_controlled("localhost:8080", EP) is False)

    case("PA6 反例：非空非法地址与混合有效非法片段 → 无清理权限", lambda:
         proxyaddr.wininet_points_at_controlled("garbage", EP) is False
         and proxyaddr.wininet_points_at_controlled("127.0.0.1:8080;", EP) is False
         and proxyaddr.wininet_points_at_controlled("127.0.0.1:8080;xx", EP) is False
         and proxyaddr.wininet_references_controlled("127.0.0.1:8080;xx", EP) is False)

    case("PA7 展示与权限分离：混合指向引用受控但不可清理", lambda:
         proxyaddr.wininet_references_controlled("127.0.0.1:8080;127.0.0.1:7890", EP) is True
         and proxyaddr.wininet_points_at_controlled("127.0.0.1:8080;127.0.0.1:7890", EP) is False)

    case("PA8 空值语义：真空=可清（空地址残留），解析失败≠空", lambda:
         proxyaddr.wininet_points_at_controlled("", EP) is True
         and proxyaddr.wininet_points_at_controlled(None, EP) is True
         and proxyaddr.wininet_references_controlled("", EP) is False
         and proxyaddr.wininet_points_at_controlled(";;;", EP) is False)

    case("PA9 环境变量：清理权限=全部端点受控；混合保留", lambda:
         proxyaddr.env_points_at_controlled("http://127.0.0.1:8080", EP) is True
         and proxyaddr.env_points_at_controlled("http://127.0.0.1:8080 http://127.0.0.1:7890", EP) is False
         and proxyaddr.env_references_controlled("http://127.0.0.1:8080 http://127.0.0.1:7890", EP) is True
         and proxyaddr.env_references_controlled("http://127.0.0.1:7890", EP) is False)

    case("PA10 受控端点配置无效 → 一律拒绝（安全侧）", lambda:
         proxyaddr.wininet_points_at_controlled("127.0.0.1:8080", "not-an-endpoint") is False
         and proxyaddr.parse_endpoint("127.0.0.1:99999") is None
         and proxyaddr.parse_endpoint("") is None)

    case("PA11 probe_endpoints：全部端点逐一探测、可注入、非法 False", lambda:
         proxyaddr.probe_endpoints("http=127.0.0.1:8080;https=127.0.0.1:8080",
                                   lambda h, p: True) is True
         and proxyaddr.probe_endpoints("http=127.0.0.1:8080;https=192.0.2.11:8080",
                                       lambda h, p: h == "127.0.0.1") is False
         and proxyaddr.probe_endpoints("garbage", lambda h, p: True) is False
         and proxyaddr.probe_endpoints("", lambda h, p: True) is False)

    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"\n结果：{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
