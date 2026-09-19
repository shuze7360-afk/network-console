"""静默静态审计：扫描 netconsole/ 包内所有 subprocess 调用点必须带 creationflags。

防回归：今后任何人新增子进程调用（PowerShell/netsh/taskkill/...），
不带 CREATE_NO_WINDOW 就会在本审计中失败。
白名单：UI 里用户主动触发的 explorer（打开文件夹本来就该可见）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PKG = Path(__file__).resolve().parents[1] / "netconsole"
WHITELIST = {("main_window.py", "open_reports")}  # 用户点击触发，可见是预期


def enclosing_function(node) -> str | None:
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        node = getattr(node, "parent", None)
    return None


def add_parents(tree) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]


def main() -> int:
    bad: list[str] = []
    checked = 0
    for py in sorted(PKG.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        add_parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "Popen", "check_output", "call")):
                continue
            checked += 1
            has_flag = any(kw.arg == "creationflags" for kw in node.keywords)
            if has_flag:
                continue
            rel = py.name
            fn = enclosing_function(node) or "?"
            if (rel, fn) in WHITELIST:
                continue
            bad.append(f"{py.name}:{node.lineno} in {fn}() 缺少 creationflags")
    print(f"扫描 subprocess 调用点：{checked} 处；缺 creationflags 且不在白名单：{len(bad)} 处")
    for b in bad:
        print("  ✗", b)
    print("结果：", "PASS" if not bad else "FAIL")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
