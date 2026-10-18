"""Keeps the solver below the user's RAM cap. Three layers: CP-SAT's own soft limit (set by the caller),
a watchdog that kills the solver near the cap or when the whole system runs low, and on Windows a Job
Object that makes the OS refuse any allocation beyond the cap."""

import ctypes
import sys
from dataclasses import dataclass

import psutil

WATCHDOG_FRACTION = 0.92
SOLVER_SOFT_FRACTION = 0.75
SYSTEM_FLOOR_MB = 700
MB = 1024 * 1024


@dataclass(frozen=True)
class MemoryVerdict:
    kill: bool
    reason: str
    used_mb: int


def process_memory_mb(process: psutil.Process) -> int:
    """Private (committed) memory on Windows, resident memory elsewhere."""
    info = process.memory_info()
    used = getattr(info, "private", 0) or info.rss
    return int(used // MB)


def installed_ram_mb() -> int:
    return int(psutil.virtual_memory().total // MB)


def available_ram_mb() -> int:
    return int(psutil.virtual_memory().available // MB)


def solver_soft_limit_mb(cap_mb: int) -> int:
    return max(64, int(cap_mb * SOLVER_SOFT_FRACTION))


def check(process: psutil.Process, cap_mb: int) -> MemoryVerdict:
    used = process_memory_mb(process)
    if used >= cap_mb * WATCHDOG_FRACTION:
        return MemoryVerdict(
            True, f"solver used {used} MB, close to the {cap_mb} MB limit", used
        )
    if available_ram_mb() < SYSTEM_FLOOR_MB:
        return MemoryVerdict(
            True, f"the computer has under {SYSTEM_FLOOR_MB} MB free", used
        )
    return MemoryVerdict(False, "", used)


class WindowsJobLimit:
    """Hard per-process memory cap through a Windows Job Object; the solver also dies with the job handle,
    so it cannot outlive the app."""

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self, cap_mb: int) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsJobLimit only works on Windows")
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._job = self._kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limits.ProcessMemoryLimit = cap_mb * MB
        ok = self._kernel32.SetInformationJobObject(
            wintypes.HANDLE(self._job),
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        from ctypes import wintypes

        handle = self._kernel32.OpenProcess(
            self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, pid
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(self._job), wintypes.HANDLE(handle)
            ):
                raise OSError(
                    ctypes.get_last_error(), "AssignProcessToJobObject failed"
                )
        finally:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))

    def close(self) -> None:
        from ctypes import wintypes

        if self._job:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._job))
            self._job = None


def lower_priority(process: psutil.Process) -> None:
    """Keep the user's computer responsive while the solver runs."""
    if sys.platform == "win32":
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    else:
        process.nice(10)
