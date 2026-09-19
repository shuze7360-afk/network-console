"""统一执行器（公开版 2.0.0）：所有修改的唯一入口。

- **来源校验（2.0.0 起，最外层闸门）**：所有修改类动作默认 source="background"，
  一律返回 manual-required；仅界面点击事件显式传 source="ui"。启动、定时检查、
  快照刷新与通知点击不代用户修改网络——只诊断、提醒与解释。
- 跨进程命名互斥 + 进程内 RLock 串行；占用返回明确的 busy 结果并审计。
- 写前读旧值、写后回读；**清理/恢复的成功以回读结果为准**（写入未生效返回
  verify-failed，不谎报成功）。状态文件损坏（state-unconfirmed）禁止启停写入。
- 代理归属判断只经 proxyaddr 三态精确解析（empty/ok/invalid）；解析失败与
  混合指向一律不取得清理权限；其他代理一律不动。
- 内部探针统一接收 (host, port) 并可注入，支持非本机端点。
- 操作代际：关闭/停用请求使在途启停操作在检查点自行失效。
- 仅作用于用户在配置中明确指定的程序与端口；未配置或配置无效一律拒绝。
"""
from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import subprocess
import threading
import time
import urllib.request
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import appconfig, proxyaddr
from .diagnostics.base import CREATE_NO_WINDOW, http_get
from .logutil import get_logger

MUTATING = {"cleanup_stale_proxy", "restore_bypass", "restore_snapshot",
            "feature_enable", "feature_disable"}


def _run(args: list[str], timeout: int = 30) -> str:
    proc = subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)
    out = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    for enc in ("utf-8", "gbk"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("utf-8", "replace")


def read_wininet() -> dict:
    out = _run(["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"])
    values = {"ProxyEnable": 0, "ProxyServer": "", "ProxyOverride": "", "AutoConfigURL": ""}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] in values:
            raw = parts[2].strip()
            if parts[1] == "REG_DWORD":
                try:
                    values[parts[0]] = int(raw, 16)
                except ValueError:
                    pass
            else:
                values[parts[0]] = raw
    return values


def _write_wininet(name: str, vtype: str, value: str) -> None:
    _run(["reg", "add",
          r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings",
          "/v", name, "/t", vtype, "/d", value, "/f"])


def tcp_up(host: str, port: int, timeout: float = 0.9) -> bool:
    """内部统一探针：接收 (host, port)；测试可整体注入替换。"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


@dataclass
class OperationResult:
    op_id: str
    action: str
    ok: bool
    decision: str
    reason: str = ""
    evidence: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class Executor:
    def __init__(self) -> None:
        self.busy = False
        self._generation = 0
        self._lock = threading.RLock()
        self.log = get_logger("netconsole.executor")

    # ---- 互斥与串行 ----
    class _Busy(RuntimeError):
        pass

    class _OpMutex:
        NAME = "Local\\NetworkConsoleOpMutex"

        def __init__(self) -> None:
            self.handle = None
            self.acquired = False

        def __enter__(self) -> bool:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.handle = kernel.CreateMutexW(None, False, self.NAME)
            if not self.handle:
                return False
            # 占用判定不能用 ERROR_ALREADY_EXISTS：同名空闲互斥也会置位。
            wait = kernel.WaitForSingleObject(wintypes.HANDLE(self.handle), 0)
            self.acquired = wait in (0, 0x80)
            return self.acquired

        def __exit__(self, *exc) -> None:
            if self.handle:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
                if self.acquired:
                    kernel.ReleaseMutex(wintypes.HANDLE(self.handle))
                kernel.CloseHandle(wintypes.HANDLE(self.handle))
                self.handle = None
                self.acquired = False

    @contextlib.contextmanager
    def _op_slot(self):
        mutex = Executor._OpMutex()
        if not mutex.__enter__():
            raise Executor._Busy("another operation in progress")
        self.busy = True
        try:
            with self._lock:
                yield
        finally:
            self.busy = False
            mutex.__exit__(None, None, None)

    @property
    def is_busy(self) -> bool:
        return self.busy

    def _new_id(self) -> str:
        return f"op-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def _audit(self, action: str, payload: dict, result: OperationResult) -> None:
        log_dir = appconfig.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": payload, **result.to_dict()}
        with (log_dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _require_ui(self, action: str, payload: dict, source: str) -> OperationResult | None:
        """来源闸门（最外层）：来源检查先于配置、意图、进程或网络设置写入。"""
        if source != "ui":
            return self._simple(
                action, payload, False, "manual-required",
                "后台会话不能修改网络；请在控制台界面操作（诊断与提醒不受影响）",
                {"source": source})
        return None

    # ---- 通用动作 ----
    def cleanup_stale_proxy(self, port_probe=None, source: str = "background") -> OperationResult:
        """清理指向受控端点的死代理残留；仅界面触发。

        归属（proxyaddr 三态）：真空地址=可清理的空地址残留；全部端点恰为
        受控端点=可清理；解析失败或混合指向=非受控，不修改。
        成功以回读为准：开关确实关闭才返回 cleaned，写入未生效返回 verify-failed。
        """
        clean = appconfig.cleanup_settings()
        controlled = str(clean.get("controlled_endpoint", "127.0.0.1:8080"))
        ep = proxyaddr.parse_endpoint(controlled)
        payload = {"controlled": controlled, "source": source}
        gate = self._require_ui("cleanup_stale_proxy", payload, source)
        if gate:
            return gate
        if ep is None:
            return self._simple("cleanup_stale_proxy", payload, False, "invalid-config",
                                f"受控端点配置无法解析：{controlled!r}；未修改任何设置")
        chost, cport = ep
        try:
            with self._op_slot():
                before = read_wininet()
                probe = port_probe or tcp_up
                port_up = probe(chost, cport) if cport else False
                changes: list[str] = []
                notes: list[str] = []
                server = str(before.get("ProxyServer", "") or "")
                parse = proxyaddr.parse_wininet_server(server)
                ours = proxyaddr.wininet_points_at_controlled(server, controlled)
                if before.get("ProxyEnable") and not port_up:
                    if ours:
                        _write_wininet("ProxyEnable", "REG_DWORD", "0")
                        changes.append(f"ProxyEnable 1->0（受控代理 {controlled} 未监听）")
                    elif parse.kind == proxyaddr.INVALID:
                        notes.append(f"系统代理地址无法解析（{server!r}），按非受控处理，未修改")
                    else:
                        notes.append(f"系统代理指向非受控地址 {server!r}，未修改")
                elif before.get("ProxyEnable") and server and not ours:
                    notes.append(f"系统代理指向其他存活代理 {server!r}，不归一化、不修改")
                after = read_wininet()
                if changes:
                    # 成功以回读为准：写入被忽略/覆盖（开关仍开启）→ verify-failed
                    ok = after.get("ProxyEnable") == 0
                    decision = "cleaned" if ok else "verify-failed"
                    reason = "" if ok else "写入未生效：回读显示系统代理开关仍开启"
                else:
                    ok = True
                    decision = "noop"
                    reason = "；".join(notes) if notes else "系统代理状态健康"
                result = OperationResult(
                    self._new_id(), "cleanup_stale_proxy", ok, decision, reason,
                    {"before": before, "after": after, "port_up": port_up,
                     "changes": changes, "notes": notes})
                self._audit("cleanup_stale_proxy", payload, result)
                return result
        except Executor._Busy:
            result = OperationResult(self._new_id(), "cleanup_stale_proxy", False,
                                     "busy", "另一进程正在操作")
            self._audit("cleanup_stale_proxy", payload, result)
            return result

    def restore_bypass(self, source: str = "background") -> OperationResult:
        """把配置的直连例外合并进 ProxyOverride（不覆盖用户已有条目；仅界面触发）。"""
        payload = {"action": "restore_bypass", "source": source}
        gate = self._require_ui("restore_bypass", payload, source)
        if gate:
            return gate
        defaults = list(appconfig.cleanup_settings().get("bypass_defaults", []))
        try:
            with self._op_slot():
                before = read_wininet()
                current = [x for x in before.get("ProxyOverride", "").split(";") if x]
                missing = [x for x in defaults if x not in current]
                if not missing:
                    result = OperationResult(self._new_id(), "restore_bypass", True, "noop",
                                             "直连例外已齐全", {"before": before})
                else:
                    merged = ";".join(current + missing)
                    _write_wininet("ProxyOverride", "REG_SZ", merged)
                    after = read_wininet()
                    result = OperationResult(
                        self._new_id(), "restore_bypass", after.get("ProxyOverride") == merged,
                        "merged" if after.get("ProxyOverride") == merged else "verify-failed",
                        f"已合并写入 {len(missing)} 条直连例外",
                        {"added": missing, "after": after})
                self._audit("restore_bypass", payload, result)
                return result
        except Executor._Busy:
            result = OperationResult(self._new_id(), "restore_bypass", False, "busy",
                                     "另一进程正在操作")
            self._audit("restore_bypass", payload, result)
            return result

    def save_snapshot(self) -> dict:
        snap = read_wininet()
        path = appconfig.data_dir() / "wininet-last-good.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        import os

        os.replace(tmp, path)
        return snap

    def read_snapshot(self) -> dict | None:
        path = appconfig.data_dir() / "wininet-last-good.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def restore_snapshot(self, port_probe=None, source: str = "background") -> OperationResult:
        """恢复最近健康快照；仅界面触发。

        可信闸门：快照格式或地址解析不完整（invalid）→ 拒绝；启用代理的快照，
        其**全部**将恢复端点必须逐一通过注入探针（any 不可用），否则拒绝
        （unsafe-snapshot，零写入）。恢复成功以三字段回读为准（含 ProxyEnable）。
        """
        payload = {"source": source}
        gate = self._require_ui("restore_snapshot", payload, source)
        if gate:
            return gate
        cfg = appconfig.load_config()
        if cfg["features"]["proxy"].get("enabled") is False:
            return self._simple("restore_snapshot", payload, False, "feature-disabled",
                                "代理功能未启用：恢复快照会写入代理设置，已跳过")
        snap = self.read_snapshot()
        if snap is None:
            snap_file = appconfig.data_dir() / "wininet-last-good.json"
            decision = "invalid-snapshot" if snap_file.exists() else "no-snapshot"
            return self._simple("restore_snapshot", payload, False, decision,
                                "快照文件损坏，已拒绝恢复" if decision == "invalid-snapshot"
                                else "尚无健康快照")
        snap_enable = int(snap.get("ProxyEnable", 0) or 0)
        snap_server = str(snap.get("ProxyServer", "") or "")
        parse = proxyaddr.parse_wininet_server(snap_server)
        if snap_enable == 1 and (parse.kind != proxyaddr.OK or not parse.endpoints):
            return self._simple("restore_snapshot", payload, False, "unsafe-snapshot",
                                f"快照代理地址不完整或无法解析（{snap_server!r}）；"
                                "恢复会产生不可确认的设置，已拒绝",
                                {"snapshot": snap})
        probe = port_probe or tcp_up
        if snap_enable == 1 and not all(probe(host, port) for host, port in sorted(parse.endpoints)):
            return self._simple("restore_snapshot", payload, False, "unsafe-snapshot",
                                f"快照代理目标 {snap_server!r} 并非全部可达；"
                                "恢复会产生死代理，已拒绝。",
                                {"snapshot": snap})
        try:
            with self._op_slot():
                before = read_wininet()
                for name, vtype, value in (
                    ("ProxyEnable", "REG_DWORD", str(snap_enable)),
                    ("ProxyServer", "REG_SZ", snap_server),
                    ("ProxyOverride", "REG_SZ", str(snap.get("ProxyOverride", "") or "")),
                ):
                    _write_wininet(name, vtype, value)
                after = read_wininet()
                # 回读必须包含 ProxyEnable，三字段一致才算恢复成功。
                ok = (after.get("ProxyEnable") == snap_enable
                      and after.get("ProxyServer") == snap_server
                      and after.get("ProxyOverride") == (snap.get("ProxyOverride", "") or ""))
                result = OperationResult(
                    self._new_id(), "restore_snapshot", ok,
                    "restored" if ok else "verify-failed",
                    "已恢复到最近健康快照" if ok else "回读不一致",
                    {"snapshot": snap, "before": before, "after": after})
                self._audit("restore_snapshot", payload, result)
                return result
        except Executor._Busy:
            result = OperationResult(self._new_id(), "restore_snapshot", False, "busy",
                                     "另一进程正在操作")
            self._audit("restore_snapshot", payload, result)
            return result

    # ---- 功能启停（仅作用于配置中明确指定的本地程序；仅界面触发）----
    def feature_enable(self, name: str, port_probe=None, forward_probe=None,
                       source: str = "background") -> OperationResult:
        return self._feature_toggle(name, True, port_probe, forward_probe, source)

    def feature_disable(self, name: str, port_probe=None,
                        source: str = "background") -> OperationResult:
        return self._feature_toggle(name, False, port_probe, None, source)

    def _feature_toggle(self, name: str, on: bool, port_probe,
                        forward_probe=None, source: str = "background") -> OperationResult:
        payload = {"feature": name, "on": on, "source": source}
        gate = self._require_ui("feature_toggle", payload, source)
        if gate:
            return gate
        if name not in appconfig.FEATURE_KEYS or name == "basic":
            return self._simple("feature_toggle", payload, False, "unknown-feature",
                                f"未知功能 {name}")
        if not appconfig.config_state_ok():
            return self._simple("feature_toggle", payload, False, "state-unconfirmed",
                                "配置文件损坏不可确认；已禁止启停写入")
        gen0 = self._generation
        try:
            with self._op_slot():
                self._generation += 1 if not on else 0
                fcfg = appconfig.load_config()["features"][name]
                if name == "auth":
                    appconfig.set_feature_enabled("auth", on)
                    result = OperationResult(self._new_id(), "feature_toggle", True,
                                             "enabled" if on else "closed",
                                             "网络认证管理已" + ("开启" if on else "关闭"),
                                             {"intent": on})
                    self._audit("feature_toggle", payload, result)
                    return result

                prog = fcfg.get("program", {}) or {}
                exe = str(prog.get("path", "") or "")
                ep = proxyaddr.parse_endpoint(str(fcfg.get("endpoint", "")))
                port = ep[1] if ep else 0
                fhost = ep[0] if ep else "127.0.0.1"
                probe = port_probe or tcp_up

                if not on:
                    feature_state_off = appconfig.set_feature_enabled(name, False)
                    targets = self._program_targets(exe)
                    stopped = self._kill_verified(targets)
                    port_down = (not port) or self._wait_port(fhost, port, False, 15, probe)
                    if not port_down:
                        return OperationResult(
                            self._new_id(), "feature_toggle", False, "incomplete",
                            "程序未能停止（可能需要管理员权限）；设置未清理",
                            {"stopped": stopped, "port_down": False})
                    self._cleanup_settings_for(port)
                    result = OperationResult(
                        self._new_id(), "feature_toggle", True, "closed",
                        f"「{fcfg.get('title', name)}」已停止并核验端口下线；相关残留已清理",
                        {"stopped": stopped, "port_down": True})
                    self._audit("feature_toggle", payload, result)
                    return result

                # 开启
                if not exe:
                    result = OperationResult(self._new_id(), "feature_toggle", False,
                                             "not-configured",
                                             "未配置可执行程序路径；请在配置文件中设置",
                                             {})
                    self._audit("feature_toggle", payload, result)
                    return result
                if port and probe(fhost, port):
                    appconfig.set_feature_enabled(name, True)
                    fwd = (forward_probe or self._forward)(fcfg, port) if port else {"ok": True}
                    ok = fwd.get("ok") is True
                    return OperationResult(
                        self._new_id(), "feature_toggle", ok,
                        "already-running" if ok else "already-running-unverified",
                        "程序已在运行" + ("，探针通过" if ok else "，探针未通过"),
                        {"forward": fwd})

                try:
                    subprocess.Popen([exe] + list(prog.get("args", [])),
                                     cwd=prog.get("workdir") or None,
                                     stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=CREATE_NO_WINDOW)
                except OSError as exc:
                    result = OperationResult(self._new_id(), "feature_toggle", False,
                                             "failed", f"启动失败：{exc}", {"stage": "launch"})
                    self._audit("feature_toggle", payload, result)
                    return result
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if self._generation != gen0:
                        return OperationResult(self._new_id(), "feature_toggle", False,
                                               "cancelled", "等待期间出现停用请求，已取消",
                                               {"stage": "port-wait"})
                    if not port or probe(fhost, port):
                        break
                    time.sleep(0.6)
                if port and not probe(fhost, port):
                    result = OperationResult(self._new_id(), "feature_toggle", False,
                                             "failed", "程序已启动但端口未在预期时间内就绪",
                                             {"stage": "port-wait"})
                    self._audit("feature_toggle", payload, result)
                    return result
                appconfig.set_feature_enabled(name, True)
                fwd = (forward_probe or self._forward)(fcfg, port) if port else {"ok": True}
                ok = fwd.get("ok") is True
                result = OperationResult(
                    self._new_id(), "feature_toggle", ok,
                    "started" if ok else "started-unverified",
                    "已启动" + ("，探针通过" if ok else "，探针未通过"),
                    {"stages": {"launch": "ok", "port": "up"}, "forward": fwd})
                self._audit("feature_toggle", payload, result)
                return result
        except Executor._Busy:
            result = OperationResult(self._new_id(), "feature_toggle", False, "busy",
                                     "另一进程正在操作")
            self._audit("feature_toggle", payload, result)
            return result

    # ---- 程序进程（仅配置中明确指定的 exe）----
    def _program_targets(self, exe: str) -> list[dict]:
        if not exe:
            return []
        name = Path(exe).name
        hint = str(Path(exe).parent)
        rows = self._pids_by(name)
        return [{"pid": r["ProcessId"], "name": r.get("Name", ""),
                 "hint": hint, "created": r.get("CreationDate")} for r in rows]

    def _pids_by(self, exe: str) -> list[dict]:
        name = Path(exe).name
        out = _run(["powershell", "-NoProfile", "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                    f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
                    "Select-Object ProcessId,Name,ExecutablePath,CreationDate | ConvertTo-Json -Depth 2"])
        import json as _json

        try:
            rows = _json.loads(out)
        except ValueError:
            return []
        rows = rows if isinstance(rows, list) else ([rows] if rows else [])
        parent_dir = str(Path(exe).parent).lower()
        return [r for r in rows
                if str(r.get("ExecutablePath") or "").lower().startswith(parent_dir.lower())
                or (r.get("ExecutablePath") or "").lower().find(parent_dir.lower()) != -1]

    def _proc_sig(self, pid: int) -> dict | None:
        import json as _json

        out = _run(["powershell", "-NoProfile", "-Command",
                    f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\"; "
                    "if ($p) { $p | Select-Object Name,ExecutablePath,CreationDate | ConvertTo-Json -Compress }"])
        try:
            d = _json.loads(out)
            return d if d else None
        except ValueError:
            return None

    def _kill_verified(self, targets: list[dict]) -> dict:
        out: dict = {}
        for t in targets:
            pid = int(t["pid"])
            sig = self._proc_sig(pid)
            if sig is None:
                out[str(pid)] = "already-exited"
                continue
            name_ok = sig.get("Name", "") == t["name"]
            path = (sig.get("ExecutablePath") or "").lower()
            hint_ok = (not t.get("hint")) or (t["hint"].lower() in path)
            created_ok = (not t.get("created")) or (str(sig.get("CreationDate")) == str(t.get("created")))
            if not (name_ok and hint_ok and created_ok):
                out[str(pid)] = f"skip: pid 复用或身份变化（现为 {sig.get('Name')}）"
                continue
            r = _run(["powershell", "-NoProfile", "-Command",
                      f"try{{Stop-Process -Id {pid} -Force -ErrorAction Stop;exit 0}}"
                      "catch{Write-Output $_.Exception.Message;exit 1}"])
            out[str(pid)] = r.strip()[:120] or "killed"
        return out

    def _wait_port(self, host: str, port: int, up: bool, timeout: float, probe=None) -> bool:
        probe = probe or tcp_up
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe(host, port) == up:
                return True
            time.sleep(0.6)
        return probe(host, port) == up

    def _forward(self, fcfg: dict, port: int) -> dict:
        probe_url = fcfg.get("probe_url") or "http://www.msftconnecttest.com/connecttest.txt"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"}))
        return http_get(probe_url, timeout=10.0, opener=opener)

    def _cleanup_settings_for(self, port: int) -> dict:
        controlled = f"127.0.0.1:{port}"
        backup_dir = appconfig.data_dir() / "settings-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        before = read_wininet()
        (backup_dir / f"wininet-{stamp}.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=1), encoding="utf-8")
        evidence = {"wininet_before": before}
        server = str(before.get("ProxyServer", "") or "")
        if (before.get("ProxyEnable")
                and proxyaddr.wininet_points_at_controlled(server, controlled)):
            _write_wininet("ProxyEnable", "REG_DWORD", "0")
        elif server:
            evidence["other_proxy_left"] = server
        env_out = {}
        for n in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            v = _run(["powershell", "-NoProfile", "-Command",
                      f"[string][Environment]::GetEnvironmentVariable('{n}','User')"]).strip()
            env_out[n] = v
            # 混合指向（含非受控条目）的环境变量整体保留，避免误删其他项目配置
            if v and proxyaddr.env_points_at_controlled(v, controlled):
                _run(["powershell", "-NoProfile", "-Command",
                      f"[Environment]::SetEnvironmentVariable('{n}', $null, 'User')"])
        evidence["env_before"] = env_out
        evidence["wininet_after"] = read_wininet()
        return evidence

    def _simple(self, action: str, payload: dict, ok: bool, decision: str,
                reason: str, evidence: dict | None = None) -> OperationResult:
        result = OperationResult(self._new_id(), action, ok, decision, reason, evidence or {})
        self._audit(action, payload, result)
        return result
