"""Identity of experiment workers, including their birth time to avoid PID reuse."""
from __future__ import annotations
import os, signal, subprocess, sys, time
from pathlib import Path


def identity(pid):
    if not pid or pid < 1:
        return None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding='utf-8').rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return None
        except OSError:
            return "unknown"
        return None if fields[0] == "Z" else "proc:" + fields[19]
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        lib.OpenProcess.restype = wintypes.HANDLE
        lib.CloseHandle.argtypes = [wintypes.HANDLE]
        lib.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        lib.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        handle = lib.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 87 else "unknown"
        try:
            code = wintypes.DWORD()
            if not lib.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown"
            if code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not lib.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return "unknown"
            return "win:" + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            lib.CloseHandle(handle)
    try:
        process = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="], capture_output=True, text=True)
    except OSError:
        return "unknown"
    value = process.stdout.strip()
    if process.returncode == 1:
        return None
    if process.returncode or not value:
        return "unknown"
    return None if value.split()[-1].startswith("Z") else "ps:" + value.rsplit(None, 1)[0]


def alive(pid, expected=None):
    current = identity(pid)
    if current == "unknown":
        return True
    return current is not None and (expected in (None, "unknown") or current == expected)


def detach_options():
    if os.name == "nt":
        return dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    return dict(start_new_session=True)


def stop_owned(pid, expected, timeout=60):
    """Stop a recorded Linux worker, allowing its resource cleanup to run first."""
    if type(pid) is not int or pid <= 1 or not isinstance(expected, str) or not expected.startswith('proc:'):
        raise ValueError('worker has no verified Linux process identity')
    current = identity(pid)
    if current is None:
        return
    if current != expected:
        raise ValueError('worker PID identity changed; refusing to signal it')
    if not sys.platform.startswith('linux'):
        raise ValueError('live evaluation cancellation requires Linux')
    try:
        group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while identity(pid) == expected and time.monotonic() < deadline:
        time.sleep(.05)
    current = identity(pid)
    if current not in (None, expected):
        raise ValueError('worker identity became unverifiable during cancellation')
    # Detached workers own their group; remove descendants even if their leader
    # has exited. Never signal the caller's group for an attached test worker.
    try:
        if group:
            os.killpg(pid, signal.SIGKILL)
        elif current == expected:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while identity(pid) == expected and time.monotonic() < deadline:
        time.sleep(.05)
    if identity(pid) is not None:
        raise RuntimeError('evaluation worker did not stop')
