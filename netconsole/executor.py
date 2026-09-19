"""统一执行器（公开版）：所有修改的唯一入口。

- 跨进程命名互斥 + 进程内 RLock 串行；占用返回明确的 busy 结果并审计。
- 写前读旧值、写后回读；状态文件损坏（state-unconfirmed）禁止启停写入。
- 操作代际：关闭/停用请求使在途启停操作在检查点自行失效。
- 仅作用于用户在配置中明确指定的程序与端口；未配置一律拒绝。
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

from . import appconfig
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


def tcp_up(port: int, host: str = "127.0.0.1", timeout: float = 0.9) -> bool:
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

    # ---- 通用动作 ----
    def cleanup_stale_proxy(self, port_probe=None) -> OperationResult:
        """清理指向受控端点的死代理残留；其他地址的代理一律不动。"""
        clean = appconfig.cleanup_settings()
        controlled = str(clean.get("controlled_endpoint", "127.0.0.1:8080"))
        port = int(controlled.rpartition(":")[2] or 0)
        payload = {"controlled": controlled}
        try:
            with self._op_slot():
                before = read_wininet()
                probe = port_probe or tcp_up
                port_up = probe(port) if port else False
                changes: list[str] = []
                notes: list[str] = []
                server = str(before.get("ProxyServer", "") or "")
                ours = server in ("", controlled) or server.startswith(f"127.0.0.1:{port}")
                if before.get("ProxyEnable") and not port_up:
                    if ours:
                        _write_wininet("ProxyEnable", "REG_DWORD", "0")
                        changes.append(f"ProxyEnable 1->0（受控代理 {controlled} 未监听）")
                    else:
                        notes.append(f"系统代理指向非受控地址 {server!r}，未修改")
                elif before.get("ProxyEnable") and server and not ours:
                    notes.append(f"系统代理指向其他存活代理 {server!r}，不归一化、不修改")
                after = read_wininet()
                result = OperationResult(
                    self._new_id(), "cleanup_stale_proxy", True,
                    "cleaned" if changes else "noop",
                    "" if changes else ("；".join(notes) if notes else "系统代理状态健康"),
                    {"before": before, "after": after, "port_up": port_up,
                     "changes": changes, "notes": notes})
                self._audit("cleanup_stale_proxy", payload, result)
                return result
        except Executor._Busy:
            result = OperationResult(self._new_id(), "cleanup_stale_proxy", False,
                                     "busy", "另一进程正在操作")
            self._audit("cleanup_stale_proxy", payload, result)
            return result

    def restore_bypass(self) -> OperationResult:
        """把配置的直连例外合并进 ProxyOverride（不覆盖用户已有条目）。"""
        payload = {"action": "restore_bypass"}
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
                self._audit("restore_bypass", {}, result)
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
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def restore_snapshot(self, port_probe=None) -> OperationResult:
        payload = {}
        cfg = appconfig.load_config()
        if cfg["features"]["proxy"].get("enabled") is False:
            return self._simple("restore_snapshot", payload, False, "feature-disabled",
                                "代理功能未启用：恢复快照会写入代理设置，已跳过")
        snap = self.read_snapshot()
        if not snap:
            return self._simple("restore_snapshot", payload, False, "no-snapshot",
                                "尚无健康快照")
        try:
            with self._op_slot():
                before = read_wininet()
                for name, vtype, value in (
                    ("ProxyEnable", "REG_DWORD", str(int(snap.get("ProxyEnable", 0)))),
                    ("ProxyServer", "REG_SZ", snap.get("ProxyServer", "")),
                    ("ProxyOverride", "REG_SZ", snap.get("ProxyOverride", "")),
                ):
                    _write_wininet(name, vtype, value)
                after = read_wininet()
                ok = (after.get("ProxyServer") == snap.get("ProxyServer")
                      and after.get("ProxyOverride") == snap.get("ProxyOverride"))
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

    # ---- 功能启停（仅作用于配置中明确指定的本地程序）----
    def feature_enable(self, name: str, port_probe=None, forward_probe=None) -> OperationResult:
        return self._feature_toggle(name, True, port_probe, forward_probe)

    def feature_disable(self, name: str, port_probe=None) -> OperationResult:
        return self._feature_toggle(name, False, port_probe, None)

    def _feature_toggle(self, name: str, on: bool, port_probe, forward_probe=None) -> OperationResult:
        payload = {"feature": name, "on": on}
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
                port_s = str(fcfg.get("endpoint", "")).rpartition(":")[2]
                port = int(port_s) if port_s.isdigit() else 0
                probe = port_probe or tcp_up

                if not on:
                    feature_state_off = appconfig.set_feature_enabled(name, False)
                    targets = self._program_targets(exe)
                    stopped = self._kill_verified(targets)
                    port_down = self._wait_port(port, False, 15, probe)
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
                if probe(port) if port else False:
                    appconfig.set_feature_enabled(name, True)
                    fwd = (forward_probe or self._forward)(fcfg, port) if port else {"ok": True}
                    ok = fwd.get("ok") is True
                    return OperationResult(
                        self._new_id(), "feature_toggle", ok,
                        "already-running" if ok else "already-running-unverified",
                        "程序已在运行" + ("，探针通过" if ok else "，探针未通过"),
                        {"forward": fwd})
                import os as _os

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
                    if probe(port):
                        break
                    time.sleep(0.6)
                if not probe(port):
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

    def _wait_port(self, port: int, up: bool, timeout: float, probe=None) -> bool:
        probe = probe or tcp_up
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe(port) == up:
                return True
            time.sleep(0.6)
        return probe(port) == up

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
        if before.get("ProxyEnable") and (server == controlled or server == ""):
            _write_wininet("ProxyEnable", "REG_DWORD", "0")
        env_out = {}
        for n in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            v = _run(["powershell", "-NoProfile", "-Command",
                      f"[string][Environment]::GetEnvironmentVariable('{n}','User')"]).strip()
            env_out[n] = v
            if v and f":{port}" in v:
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
