"""控制台单实例保护：第二次启动不产生第二个监测实例，只请求已有实例显示主面板。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

MUTEX_NAME = "Local\\NetworkConsoleSingleInstance"
EVENT_NAME = "Local\\NetworkConsoleShowEvent"
EVENT_MODIFY_STATE = 0x0002

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateMutexW.restype = wintypes.HANDLE
_kernel.CreateEventW.restype = wintypes.HANDLE
_kernel.OpenEventW.restype = wintypes.HANDLE
_kernel.SetEvent.argtypes = [wintypes.HANDLE]
_kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]


def acquire() -> tuple[bool, int, int]:
    """尝试成为第一个实例。返回 (is_first, mutex_handle, show_event_handle)。"""
    mutex = _kernel.CreateMutexW(None, False, MUTEX_NAME)
    first = bool(mutex) and ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    if first:
        event = _kernel.CreateEventW(None, False, False, EVENT_NAME)
        return True, int(mutex or 0), int(event or 0)
    # 已有实例：通知它显示主面板
    event = _kernel.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
    if event:
        _kernel.SetEvent(event)
        _kernel.CloseHandle(event)
    if mutex:
        _kernel.CloseHandle(mutex)
    return False, 0, 0


def pop_show_signal(event_handle: int) -> bool:
    """第一实例轮询：收到第二次启动的显示请求则返回 True（并复位事件）。"""
    if not event_handle:
        return False
    if _kernel.WaitForSingleObject(wintypes.HANDLE(event_handle), 0) == 0:
        _kernel.ResetEvent(wintypes.HANDLE(event_handle))
        return True
    return False


def release(mutex_handle: int) -> None:
    if mutex_handle:
        _kernel.CloseHandle(wintypes.HANDLE(mutex_handle))
