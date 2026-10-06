from __future__ import annotations

import atexit
import ctypes
import logging
import multiprocessing
import os
import queue
import threading
import time
import traceback
import uuid
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing.connection import wait as wait_connections

from change_logic import ChangeResult, GridComparator
from screen import is_visible_on_monitors, visible_pixels
from windowmatch import matches_target, preferred_candidate_index


logger = logging.getLogger("lookupwindows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p
HMONITOR = ctypes.c_void_p


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
WPF_ASYNCWINDOWPLACEMENT = 0x0004
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DWMWA_CLOAKED = 14
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
HALFTONE = 4

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetPropW.argtypes = [HWND, wintypes.LPCWSTR, ctypes.c_void_p]
user32.SetPropW.restype = wintypes.BOOL
user32.GetPropW.argtypes = [HWND, wintypes.LPCWSTR]
# A raw address, not LPCWSTR: the property value is an opaque handle, and ctypes
# would convert it into a Python string it has no way to interpret.
user32.GetPropW.restype = ctypes.c_void_p
user32.RemovePropW.argtypes = [HWND, wintypes.LPCWSTR]
user32.RemovePropW.restype = wintypes.BOOL
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_window_long_ptr = user32.GetWindowLongPtrW
    _get_window_long_ptr.restype = ctypes.c_ssize_t
else:
    _get_window_long_ptr = user32.GetWindowLongW
    _get_window_long_ptr.restype = ctypes.c_long
_get_window_long_ptr.argtypes = [HWND, ctypes.c_int]
user32.GetWindow.argtypes = [HWND, wintypes.UINT]
user32.GetWindow.restype = HWND
user32.GetForegroundWindow.restype = HWND
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindowAsync.argtypes = [HWND, ctypes.c_int]
user32.ShowWindowAsync.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [HWND, HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = HMONITOR
user32.MonitorFromWindow.argtypes = [HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = HMONITOR
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HMONITOR, HDC, ctypes.c_void_p, wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsProcessInJob.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = HBITMAP
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.SetStretchBltMode.argtypes = [HDC, ctypes.c_int]
gdi32.SetStretchBltMode.restype = ctypes.c_int
gdi32.StretchBlt.argtypes = [
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.StretchBlt.restype = wintypes.BOOL

dwmapi.DwmGetWindowAttribute.argtypes = [HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetMonitorInfoW.argtypes = [HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

# Test-only fault injection.  The runtime gates in tools/runtime_smoke.py need to
# reach failure paths of the frozen artifact, which cannot be monkeypatched, so
# a hook is reachable through the environment.  It is inert unless explicitly
# requested and it is documented as a test hook in README.md.
TEST_HOOK_ENV = "LOOKUPWINDOWS_TEST_HOOKS"
HELPER_ARMED_TOKEN = b"\x01"


def _test_hook_enabled(name: str) -> bool:
    raw = os.environ.get(TEST_HOOK_ENV, "")
    return any(part.strip() == name for part in raw.replace(";", ",").split(","))


class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class CaptureHelperJob:
    """Kill-on-close Job Object that owns the capture helper processes.

    A capture helper can be blocked inside a native ``PrintWindow`` call, so it
    cannot observe pipe EOF, Python daemon cleanup or the supervisor timeout
    while LookUp is being hard-terminated.  Binding the helpers to a Job Object
    moves that ownership into the OS: the job handle exists only inside the
    LookUp process, therefore every helper is killed as soon as the process
    dies, for any reason, without running a single line of our code.

    The binding is also a *precondition for using a helper at all*: a helper
    that could not be bound is terminated again instead of being fed capture
    work, because an unbound helper is exactly the orphan this class exists to
    prevent.
    """

    def __init__(self, name: str | None = None):
        # The job is deliberately unnamed: a named job object is shared with
        # every other process that opens the same name, and kill-on-close then
        # depends on an unrelated handle staying open.  An unnamed job belongs
        # to exactly this process, so its lifetime is exactly the app lifetime.
        self._handle = 0
        handle = int(kernel32.CreateJobObjectW(None, name) or 0)
        if not handle:
            logger.warning("Capture helper job could not be created; helpers stay unbound")
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("Capture helper job limit could not be set; helpers stay unbound")
            kernel32.CloseHandle(handle)
            return
        self._handle = handle

    @property
    def available(self) -> bool:
        return bool(self._handle)

    def adopt(self, process) -> bool:
        """Bind a freshly started helper process to this job."""
        if _test_hook_enabled("job_adopt_fail"):
            # Fault injection for the runtime gate that proves the unbound
            # helper is never used (see tools/runtime_smoke.py --scenario jobfail).
            logger.error(
                "Capture helper job adoption was forced to fail by %s", TEST_HOOK_ENV
            )
            return False
        pid = int(getattr(process, "pid", 0) or 0)
        if not self._handle or not pid:
            return False
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not handle:
            logger.warning("Capture helper PID %s could not be opened for job assignment", pid)
            return False
        try:
            if kernel32.AssignProcessToJobObject(self._handle, handle):
                return True
            # Access denied here means an outer job forbids nesting; the helper
            # then relies on the supervisor paths only, so say so explicitly.
            logger.warning(
                "Capture helper PID %s was not bound to the kill-on-close job (error %s)",
                pid,
                ctypes.get_last_error(),
            )
            return False
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self) -> None:
        if not self._handle:
            return
        try:
            kernel32.TerminateJobObject(self._handle, 1)
        finally:
            kernel32.CloseHandle(self._handle)
            self._handle = 0


_capture_job_lock = threading.Lock()
_capture_job: CaptureHelperJob | None = None


def capture_helper_job() -> CaptureHelperJob:
    global _capture_job
    with _capture_job_lock:
        if _capture_job is None:
            _capture_job = CaptureHelperJob()
        return _capture_job


def _terminate_capture_helper_job() -> None:
    job = _capture_job
    if job is not None and job.available:
        try:
            job.terminate()
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            logger.debug("Capture helper job termination failed", exc_info=True)


atexit.register(_terminate_capture_helper_job)


gdi32.GetDIBits.argtypes = [
    HDC,
    HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(BITMAPINFOHEADER),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
        ("rcDevice", wintypes.RECT),
    ]


user32.GetWindowPlacement.argtypes = [HWND, wintypes.LPVOID]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.SetWindowPlacement.argtypes = [HWND, wintypes.LPVOID]
user32.SetWindowPlacement.restype = wintypes.BOOL


class WINDOWPLACEMENT_LEGACY(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


@dataclass(frozen=True)
class OrphanSweep:
    """What one sweep of the LookUp parking position actually established.

    Four separate answers, because "the sweep returned nothing" is the answer that
    used to hide a window nobody had recovered yet:

    * ``recovered`` - windows that are provably back on a monitor;
    * ``pending`` - windows that are still where LookUp parked them;
    * ``unknown`` - windows whose state could not be established;
    * ``complete`` - whether the desktop could be enumerated at all.
    """

    recovered: tuple[int, ...] = ()
    pending: tuple[int, ...] = ()
    unknown: tuple[int, ...] = ()
    complete: bool = True

    @property
    def decided(self) -> bool:
        """Whether this sweep proved that nothing is parked any more."""
        return self.complete and not self.pending and not self.unknown

    def describe(self) -> str:
        return (
            f"recovered={list(self.recovered)} pending={list(self.pending)} "
            f"unknown={list(self.unknown)} complete={self.complete}"
        )


@dataclass
class ParkedWindowState:
    placement: WINDOWPLACEMENT | WINDOWPLACEMENT_LEGACY
    # GetWindowPlacement uses workspace coordinates for ordinary top-level
    # windows, while SetWindowPos uses screen coordinates.  Keep the actual
    # pre-park screen rectangle as an independent recovery path instead of
    # trying to reinterpret rcNormalPosition.
    screen_rect: tuple[int, int, int, int]
    pid: int = 0
    class_name: str = ""
    process_name: str = ""
    process_created: int | None = None
    # The rectangle this park puts the window into, together with the virtual
    # desktop origin it was derived from.  Both are durable (see the journal
    # record), because the parking signature computed from the *current* topology
    # stops matching as soon as a monitor is plugged in, removed or re-arranged.
    park_rect: tuple[int, int, int, int] = ()
    # The virtual desktop origin ``park_rect`` was derived from, kept with it so the
    # position stays interpretable after the monitor layout has changed.
    park_origin: tuple[int, int] = (0, 0)
    # Identity of *this* park operation.  It is stamped on the window itself, so a
    # later process can prove the window is the one it parked even when the
    # geometry it parked it at no longer means anything.
    operation_id: str = ""


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    class_name: str
    rect: tuple[int, int, int, int]
    minimized: bool
    process_created: int | None = None
    process_name: str = ""
    process_access_denied: bool = False


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: int | None
    name: str
    access_denied: bool = False


@dataclass
class WindowCandidate:
    info: WindowInfo
    process_name: str

    @property
    def hwnd(self) -> int:
        return self.info.hwnd

    @property
    def title(self) -> str:
        return self.info.title


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return hr == 0 and value.value != 0


def get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def probe_class_name(hwnd: int) -> str | None:
    """The window class, or ``None`` when the query itself produced no answer.

    An empty class name is not "a class name nobody has ever seen": every real
    window has one, so an empty answer means the query failed.  Recovery uses this
    distinction to tell "this handle belongs to something else" apart from "I could
    not ask", which are very different answers.
    """
    buf = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(hwnd, buf, 256):
        return None
    return buf.value or None


def query_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return query_window_rect(hwnd) or (0, 0, 0, 0)


def get_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def probe_pid(hwnd: int) -> int | None:
    """The owning process id, or ``None`` when the query produced no answer.

    PID 0 is not a process: it is what Win32 reports for a handle it could not be
    asked about.  Comparing that against a recorded PID would "prove" that a live
    window was somebody else's and delete the only record of its park.
    """
    pid = wintypes.DWORD(0)
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not thread or not pid.value:
        return None
    return int(pid.value)


_PROCESS_CACHE_TTL = 10.0
_ACCESS_DENIED_TTL = 2.0
_PROCESS_CACHE_LIMIT = 512
_process_names: dict[tuple[int, int], tuple[float, str]] = {}
_process_access_denied: dict[int, float] = {}
_process_cache_lock = threading.Lock()


def _prune_process_caches(now: float) -> None:
    with _process_cache_lock:
        if len(_process_names) > _PROCESS_CACHE_LIMIT:
            cutoff = now - _PROCESS_CACHE_TTL
            for key, (stamp, _value) in list(_process_names.items()):
                if stamp < cutoff:
                    _process_names.pop(key, None)
        if len(_process_access_denied) > _PROCESS_CACHE_LIMIT:
            cutoff = now - _ACCESS_DENIED_TTL
            for cached_pid, stamp in list(_process_access_denied.items()):
                if stamp < cutoff:
                    _process_access_denied.pop(cached_pid, None)


def process_access_denied(pid: int) -> bool:
    pid = int(pid)
    now = time.monotonic()
    with _process_cache_lock:
        stamp = _process_access_denied.get(pid)
        if stamp is None:
            return False
        if now - stamp <= _ACCESS_DENIED_TTL:
            return True
        _process_access_denied.pop(pid, None)
    return False


def _query_process_identity(pid: int, *, query_name: bool = True) -> ProcessIdentity:
    """Query creation time and image name using one process handle.

    Names are cached by ``(pid, creation_time)`` rather than PID alone, so a
    recycled PID cannot inherit the previous process name.  Callers that scan a
    desktop should additionally memoize this result per PID for that scan.
    """
    pid = int(pid)
    if pid <= 0:
        return ProcessIdentity(pid=pid, created=None, name="")

    now = time.monotonic()
    with _process_cache_lock:
        denied_at = _process_access_denied.get(pid)
        if denied_at is not None:
            if now - denied_at <= _ACCESS_DENIED_TTL:
                return ProcessIdentity(pid=pid, created=None, name="", access_denied=True)
            _process_access_denied.pop(pid, None)

    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        denied = ctypes.get_last_error() == 5
        if denied:
            with _process_cache_lock:
                _process_access_denied[pid] = now
        _prune_process_caches(now)
        return ProcessIdentity(pid=pid, created=None, name="", access_denied=denied)

    created_value: int | None = None
    name = ""
    try:
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            created_value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)

        if query_name and created_value is not None:
            key = (pid, created_value)
            with _process_cache_lock:
                cached = _process_names.get(key)
                if cached is not None and now - cached[0] <= _PROCESS_CACHE_TTL:
                    name = cached[1]
            if not name:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    full = buf.value
                    name = full.rsplit("\\", 1)[-1] if full else ""
                    if name:
                        with _process_cache_lock:
                            _process_names[key] = (now, name)
        elif query_name:
            # Creation time is the cache key that protects against PID reuse.
            # If it is unavailable, query the name but deliberately do not cache it.
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                full = buf.value
                name = full.rsplit("\\", 1)[-1] if full else ""
    finally:
        kernel32.CloseHandle(handle)

    with _process_cache_lock:
        _process_access_denied.pop(pid, None)
    _prune_process_caches(now)
    return ProcessIdentity(pid=pid, created=created_value, name=name)


def get_process_creation_time(pid: int) -> int | None:
    """Return a stable process creation timestamp for PID-reuse protection."""
    return _query_process_identity(pid, query_name=False).created


def get_process_name(pid: int, process_created: int | None = None) -> str:
    pid = int(pid)
    if pid <= 0:
        return ""
    now = time.monotonic()
    if process_created is not None:
        with _process_cache_lock:
            cached = _process_names.get((pid, int(process_created)))
            if cached is not None and now - cached[0] <= _PROCESS_CACHE_TTL:
                return cached[1]
    identity = _query_process_identity(pid, query_name=True)
    if process_created is not None and identity.created != int(process_created):
        return ""
    return identity.name


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def build_window_info(hwnd: int) -> WindowInfo | None:
    if not is_window(hwnd):
        return None
    pid = get_pid(hwnd)
    identity = _query_process_identity(pid)
    return WindowInfo(
        hwnd=int(hwnd),
        title=get_window_text(hwnd),
        pid=pid,
        class_name=get_class_name(hwnd),
        rect=get_window_rect(hwnd),
        minimized=is_minimized(hwnd),
        process_created=identity.created,
        process_name=identity.name,
        process_access_denied=identity.access_denied,
    )


def _monitor_work_area(monitor: int) -> tuple[int, int, int, int] | None:
    if not monitor:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    rect = info.rcWork
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def work_area_for_point(x: int, y: int) -> tuple[int, int, int, int]:
    monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    area = _monitor_work_area(monitor)
    if area is not None:
        return area
    return _primary_work_area()


def work_area_for_window(hwnd: int) -> tuple[int, int, int, int]:
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) if hwnd else None
    area = _monitor_work_area(monitor)
    if area is not None:
        return area
    return work_area()


def _primary_work_area() -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return (0, 0, 1920, 1080)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def work_area() -> tuple[int, int, int, int]:
    point = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        return work_area_for_point(point.x, point.y)
    return _primary_work_area()


def set_foreground(hwnd: int, *, async_restore: bool = False) -> bool:
    """Bring a foreign window to the foreground.

    ``async_restore`` must be used for every call made from LookUp's UI/native
    dispatch thread.  A synchronous ``ShowWindow`` sends WM_SHOWWINDOW to the
    owning thread and waits for it, so a hung target freezes the whole LookUp
    UI; Microsoft documents ``ShowWindowAsync`` exactly for this case.  Worker
    threads may keep the synchronous variant because they must observe the
    restored state before continuing.
    """
    if not is_window(hwnd):
        return False
    if user32.IsIconic(hwnd):
        if async_restore:
            user32.ShowWindowAsync(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        return ok
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


def is_desktop_foreground() -> bool:
    hwnd = get_foreground_hwnd()
    if not hwnd:
        return True
    class_name = get_class_name(hwnd)
    if class_name in {"Progman", "WorkerW", "Shell_TrayWnd"}:
        return True
    return False


def _copy_placement(value: WINDOWPLACEMENT | WINDOWPLACEMENT_LEGACY):
    return type(value).from_buffer_copy(value)


def get_window_placement(hwnd: int) -> WINDOWPLACEMENT | WINDOWPLACEMENT_LEGACY | None:
    if not is_window(hwnd):
        return None
    # Current Windows SDKs expose rcDevice; legacy SDK/OS combinations use the
    # older structure without it. Accept both layouts so the feature does not
    # depend on the SDK generation used by the target machine.
    for cls in (WINDOWPLACEMENT, WINDOWPLACEMENT_LEGACY):
        placement = cls()
        placement.length = ctypes.sizeof(cls)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return placement
    return None


def _parking_position(width: int, height: int) -> tuple[int, int]:
    virtual_left, virtual_top = virtual_screen_origin()
    # Keep only a 1x1 corner inside the virtual desktop.  To the user the source
    # is effectively gone, while DWM still has a tiny on-screen intersection;
    # this is friendlier to applications/compositors that throttle windows which
    # are completely outside every monitor.  SetWindowPos itself does not clamp
    # these coordinates (unlike SetWindowPlacement).
    return (
        virtual_left - max(1, int(width)) + 1,
        virtual_top - max(1, int(height)) + 1,
    )


def virtual_screen_origin() -> tuple[int, int]:
    """The top-left corner of the virtual desktop, as this moment's topology."""
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
    )


def park_rect_for(width: int, height: int) -> tuple[int, int, int, int]:
    """The rectangle a park of this size puts a window into."""
    left, top = _parking_position(width, height)
    return (left, top, left + max(1, int(width)), top + max(1, int(height)))


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    left, top = virtual_screen_origin()
    width = max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)))
    height = max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))
    return (left, top, left + width, top + height)


# Window properties contain scalar values readable by another process. Never
# put a local pointer or GlobalAlloc handle here: both die with the owner process.
# Eight 16-bit chunks carry the UUID on both 32-bit and 64-bit Windows. Adding
# one reserves zero for a missing property; the final property commits the mark.
PARK_OPERATION_PROP = "LookUpWindows.ParkOperation.v2"
_PARK_OPERATION_PARTS = tuple(f"{PARK_OPERATION_PROP}.{i}" for i in range(8))


def park_operation_id(hwnd: int) -> str:
    """Read the UUID mark without dereferencing memory in another process."""
    if not hwnd or not is_window(hwnd):
        return ""
    try:
        if int(user32.GetPropW(hwnd, PARK_OPERATION_PROP) or 0) != 1:
            return ""
        parts = [int(user32.GetPropW(hwnd, key) or 0) for key in _PARK_OPERATION_PARTS]
        if any(part < 1 or part > 65536 for part in parts):
            return ""
        return "".join(f"{part - 1:04x}" for part in parts)
    except (OSError, ValueError):
        return ""


def apply_park_operation(hwnd: int, operation_id: str) -> bool:
    """Commit a durable window mark before any move is authorized."""
    text = (operation_id or "").strip().lower()
    if not hwnd or len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
        return False
    clear_park_operation(hwnd)
    for index, key in enumerate(_PARK_OPERATION_PARTS):
        value = int(text[index * 4:index * 4 + 4], 16) + 1
        if not user32.SetPropW(hwnd, key, ctypes.c_void_p(value)):
            clear_park_operation(hwnd)
            return False
    if not user32.SetPropW(hwnd, PARK_OPERATION_PROP, ctypes.c_void_p(1)):
        clear_park_operation(hwnd)
        return False
    return park_operation_id(hwnd) == text


def clear_park_operation(hwnd: int) -> None:
    """Remove scalar properties; there are no memory handles to free."""
    if not hwnd or not is_window(hwnd):
        return
    try:
        for key in (PARK_OPERATION_PROP, *_PARK_OPERATION_PARTS):
            user32.RemovePropW(hwnd, key)
    except (OSError, ValueError):
        pass


# Monitor layouts change rarely, while the accessibility predicate is asked
# after every restore attempt and on every refresh pass.  A short cache keeps the
# answer honest (a monitor that was just plugged in is seen within a second)
# without enumerating the display list on every poll.
_MONITOR_CACHE_TTL_SEC = 1.0
_monitor_cache: tuple[float, tuple[tuple[int, int, int, int], ...]] = (0.0, ())
_monitor_cache_lock = threading.Lock()


def display_monitor_rects() -> tuple[tuple[int, int, int, int], ...]:
    """Every real monitor's display rectangle.

    The virtual screen's bounding box is *not* this: on an L-shaped layout the
    box contains a gap that belongs to no monitor, and a window parked in that
    gap is invisible to the user while still intersecting the box.
    """
    global _monitor_cache
    now = time.monotonic()
    with _monitor_cache_lock:
        stamp, rects = _monitor_cache
        if rects and now - stamp < _MONITOR_CACHE_TTL_SEC:
            return rects
    found: list[tuple[int, int, int, int]] = []

    @MONITORENUMPROC
    def collect(monitor, _hdc, _data, _lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            if rect.right > rect.left and rect.bottom > rect.top:
                found.append((int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
        return True

    try:
        user32.EnumDisplayMonitors(None, None, collect, 0)
    except (OSError, AttributeError):  # pragma: no cover - defensive
        logger.debug("Monitor enumeration failed", exc_info=True)
    if not found:
        # No display could be queried. A virtual bounding box would invent visible
        # pixels and could retire an obligation without proving accessibility.
        return ()
    rects = tuple(found)
    with _monitor_cache_lock:
        _monitor_cache = (now, rects)
    return rects


def visible_window_size(hwnd: int) -> tuple[int, int]:
    """How much of ``hwnd`` is on a real monitor (diagnostics for logs/smoke)."""
    return visible_pixels(get_window_rect(hwnd), display_monitor_rects())


def looks_like_lookup_parked(hwnd: int) -> bool:
    """Return True for the characteristic 1x1 parking position used by LookUp.

    This is intentionally strict.  It is used both to verify restore and to
    recover windows stranded by an earlier LookUp process that exited after
    parking them.

    It is also *topology dependent*: the parking rectangle is derived from the
    virtual desktop's top-left corner, so a monitor change invalidates it.  That is
    why :func:`is_stranded_park` - not this function - decides whether a window is
    one of ours.
    """
    if not is_window(hwnd):
        return False
    left, top, right, bottom = get_window_rect(hwnd)
    if right <= left or bottom <= top:
        return False
    vleft, vtop, _vright, _vbottom = _virtual_screen_rect()
    return (
        left < vleft
        and top < vtop
        and abs(right - (vleft + 1)) <= 6
        and abs(bottom - (vtop + 1)) <= 6
    )


def looks_like_parked_at(hwnd: int, rect) -> bool:
    """Whether ``hwnd`` still sits where a recorded park put it.

    Unlike :func:`looks_like_lookup_parked` this compares against the rectangle
    *the record carries*, so it survives plugging in, removing or re-arranging a
    monitor after the park.
    """
    try:
        left, top, right, bottom = (int(value) for value in rect)
    except (TypeError, ValueError):
        return False
    if right <= left or bottom <= top or not is_window(hwnd):
        return False
    current = get_window_rect(hwnd)
    return all(abs(actual - expected) <= 6 for actual, expected in zip(current, (left, top, right, bottom)))


def is_stranded_park(hwnd: int, *, operation_id: str = "", park_rect=()) -> bool:
    """Whether this window is one of LookUp's that is still where it was parked.

    Three independent proofs are accepted, because each of them fails in a
    different situation:

    * our own mark on the window (with the park operation id when the caller knows
      it) - survives every monitor change;
    * the rectangle the record says the window was parked at - survives a lost
      mark;
    * the characteristic corner of the *current* topology - the only proof left for
      a record that carries neither.

    A window that is observably on a monitor is never treated as stranded: the user
    can see it, so it is not lost, and moving it would fight them.
    """
    if not is_window(hwnd):
        return False
    if is_effectively_onscreen(hwnd):
        return False
    if operation_id:
        return park_operation_id(hwnd) == operation_id
    if park_rect:
        return looks_like_parked_at(hwnd, park_rect)
    return bool(park_operation_id(hwnd)) or looks_like_lookup_parked(hwnd)


def is_window_shown(hwnd: int) -> bool:
    """Whether the window is actually presented to the user.

    A window handle, a normal rectangle and "not minimized" are not enough:
    ``IsWindowVisible`` says whether the window has ``WS_VISIBLE`` *and* all of
    its parents do, and the DWM cloak attribute covers a window that belongs to
    another virtual desktop or was hidden by the compositor.  Reporting such a
    window as visible is what let a recovery record be discharged while the
    user's window stayed exactly where LookUp put it.
    """
    if not is_window(hwnd):
        return False
    if not user32.IsWindowVisible(hwnd):
        return False
    return not is_cloaked(hwnd)


def is_effectively_onscreen(hwnd: int, min_visible: int = 24) -> bool:
    """True when the user can actually see this window.

    The test is per *monitor*, not per virtual bounding box: on any layout that is
    not a plain grid (two monitors side by side, or one in the upper right corner)
    the bounding box contains empty desktop that is off every display.  Visibility
    is also part of the answer, not just geometry: a hidden or cloaked window with
    an ordinary rectangle is not accessible to the user, and treating it as
    restored is how an obligation got discharged without a window coming back.
    """
    if not is_window_shown(hwnd) or is_minimized(hwnd) or looks_like_lookup_parked(hwnd):
        return False
    rect = get_window_rect(hwnd)
    return is_visible_on_monitors(rect, display_monitor_rects(), min_visible)


def _clamp_screen_rect_to_monitor(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = rect
    width = max(160, right - left)
    height = max(100, bottom - top)
    cx = left + max(1, width) // 2
    cy = top + max(1, height) // 2
    work_left, work_top, work_right, work_bottom = work_area_for_point(cx, cy)
    work_width = max(160, work_right - work_left)
    work_height = max(100, work_bottom - work_top)
    width = min(width, work_width)
    height = min(height, work_height)
    left = max(work_left, min(work_right - width, left))
    top = max(work_top, min(work_bottom - height, top))
    return (left, top, left + width, top + height)


def _predict_park_rect(hwnd: int, placement, rect: tuple[int, int, int, int]):
    """Where the park of this window is about to put it.

    Recorded *before* the move so the journal carries a rectangle the window will
    really occupy.  For a maximized or minimized window the move first restores it,
    so the size comes from the placement's normal position instead of the current
    one - that is the rectangle the restored window ends up with.
    """
    if int(placement.showCmd) == SW_SHOWMAXIMIZED or is_minimized(hwnd):
        normal = placement.rcNormalPosition
        left, top, right, bottom = normal.left, normal.top, normal.right, normal.bottom
        if right <= left or bottom <= top:
            return ()
        rect = (left, top, right, bottom)
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    return park_rect_for(width, height)


def _parked_state(hwnd: int, placement, rect: tuple[int, int, int, int]) -> ParkedWindowState:
    pid = get_pid(hwnd)
    identity = _query_process_identity(pid)
    park_rect = _predict_park_rect(hwnd, placement, rect)
    return ParkedWindowState(
        placement=_copy_placement(placement),
        screen_rect=rect,
        pid=pid,
        class_name=get_class_name(hwnd),
        process_name=identity.name,
        process_created=identity.created,
        park_rect=park_rect,
        park_origin=virtual_screen_origin() if park_rect else (0, 0),
        operation_id=uuid.uuid4().hex,
    )


VERIFY_MATCH = "match"
VERIFY_GONE = "gone"
VERIFY_REUSED = "reused"
VERIFY_UNKNOWN = "unknown"


def classify_window_identity(
    hwnd: int,
    *,
    recorded_pid: int = 0,
    recorded_class_name: str = "",
    recorded_created: int | None = None,
    recorded_process_name: str = "",
) -> str:
    """Whether ``hwnd`` is provably still the window a record describes.

    This is the *one* identity classifier: the application's recovery paths and the
    executor's assessment both go through it, so the two can never disagree about
    whether a window is still ours.

    Two rules make it fail closed:

    * an identity field that was recorded is only compared when the query actually
      answered - a zero PID and an empty class name are the *absence* of an answer,
      not a mismatch, and they yield ``unknown``;
    * ``reused`` requires a successful query whose value differs.

    Only ``gone`` and ``reused`` may retire an obligation.
    """
    if not is_window(hwnd):
        return VERIFY_GONE
    pid = get_pid(hwnd)
    class_name = get_class_name(hwnd)
    unanswered = bool(
        (recorded_pid and not pid) or (recorded_class_name and not class_name)
    )
    if recorded_pid and pid and int(pid) != int(recorded_pid):
        return VERIFY_REUSED
    if recorded_class_name and class_name and class_name != recorded_class_name:
        return VERIFY_REUSED
    identity = _query_process_identity(pid or 0, query_name=bool(recorded_process_name))
    if recorded_created is not None:
        if identity.created is None:
            # Could not open the process: the window exists, so it may still be
            # the parked one.  Deferring costs a retry; guessing costs a window.
            return VERIFY_UNKNOWN if is_window(hwnd) else VERIFY_GONE
        if identity.created != recorded_created:
            return VERIFY_REUSED
    if unanswered:
        return VERIFY_UNKNOWN if is_window(hwnd) else VERIFY_GONE
    if recorded_process_name and identity.name:
        if identity.name.casefold() != recorded_process_name.casefold():
            return VERIFY_REUSED
    return VERIFY_MATCH


def classify_parked_window(hwnd: int, state: ParkedWindowState) -> str:
    """Whether ``hwnd`` is still the window ``state`` describes.

    The distinction that matters is between "provably not ours any more" and
    "cannot be judged right now".  A single ``False`` cannot carry both: an
    access-denied ``OpenProcess``, a window that is being created, a query that
    failed outright, or a target that is momentarily busy all look exactly like
    "this is somebody else's window" to a two-valued predicate - and a recovery
    record that is dropped on that evidence is the one and only proof that a user's
    window was moved off-screen.

    Therefore:

    * ``gone`` - the handle does not exist at all;
    * ``reused`` - the handle exists but provably belongs to something else;
    * ``unknown`` - the window exists and nothing contradicts it, but its
      identity could not be established right now;
    * ``match`` - the window is provably the one that was parked.

    Only ``gone`` and ``reused`` may retire an obligation.
    """
    return classify_window_identity(
        hwnd,
        recorded_pid=int(state.pid or 0),
        recorded_class_name=state.class_name or "",
        recorded_created=state.process_created,
        recorded_process_name=state.process_name or "",
    )


def window_matches_parked_state(hwnd: int, state: ParkedWindowState) -> bool:
    """Reject reused HWNDs before moving a foreign window.

    The destructive callers (park and restore) need a yes/no answer: anything but
    a proven match is refused, so this stays the conservative form of
    :func:`classify_parked_window`.
    """
    return classify_parked_window(hwnd, state) == VERIFY_MATCH


def park_window_offscreen(hwnd: int) -> ParkedWindowState | None:
    """Move a foreign window off-screen without minimizing it.

    The window remains WS_VISIBLE, so DWM thumbnails can keep using the live
    source. All state needed to restore it is captured before the move. The
    cross-thread move is posted asynchronously so a busy target cannot stall
    LookUp's UI thread.
    """
    if not is_window(hwnd):
        return None
    placement = get_window_placement(hwnd)
    if placement is None:
        return None
    rect = get_window_rect(hwnd)
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    state = _parked_state(hwnd, placement, rect)

    # A maximized window ignores ordinary moves on many applications. Restore
    # it asynchronously first, then queue the off-screen position. Both calls
    # return immediately to the PiP UI.
    if int(placement.showCmd) == SW_SHOWMAXIMIZED:
        user32.ShowWindowAsync(hwnd, SW_RESTORE)

    park_x, park_y = _parking_position(width, height)
    ok = user32.SetWindowPos(
        hwnd, None, park_x, park_y, width, height,
        SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
    )
    return state if ok else None


def restore_parked_window(hwnd: int, state: ParkedWindowState) -> bool:
    """Restore the exact pre-park show state/normal placement asynchronously."""
    if not window_matches_parked_state(hwnd, state):
        return False
    placement = _copy_placement(state.placement)
    placement.length = ctypes.sizeof(placement)
    placement.flags = int(placement.flags) | WPF_ASYNCWINDOWPLACEMENT
    return bool(user32.SetWindowPlacement(hwnd, ctypes.byref(placement)))



def park_window_offscreen_sync(hwnd: int, *, before_park=None) -> ParkedWindowState | None:
    """Reliably park a foreign window from a worker thread.

    Unlike :func:`park_window_offscreen`, this function deliberately performs
    the restore/move synchronously.  Call it only outside the UI thread.  The
    synchronous sequence is important for maximized windows: posting
    ``SW_RESTORE`` and ``SetWindowPos`` back-to-back can race in the target
    thread, leaving the window apparently unchanged.

    ``before_park`` is invoked with the captured pre-park state *before* any
    window is moved.  It is the durable recovery hook: if ownership of the move
    cannot be registered there (for example because the on-disk journal
    failed), the park is abandoned so that no window is ever left off-screen
    without a way back.  The park operation id the state carries is stamped on
    the window next, so recovery can still recognise it after a monitor change.

    Contract: a returned state means a park is (or may still be) in effect and
    the caller keeps recovery ownership; ``None`` means no park is in effect.
    """
    if not is_window(hwnd):
        return None
    placement = get_window_placement(hwnd)
    if placement is None:
        return None
    original_rect = get_window_rect(hwnd)
    state = _parked_state(hwnd, placement, original_rect)

    if before_park is not None and not before_park(state):
        logger.warning("Park of hwnd=%s aborted: recovery ownership could not be recorded", hwnd)
        return None

    # Our own mark on the window, applied before the move and checked: it is the
    # only proof that survives the monitor layout the park position was derived
    # from changing under us.
    marked = apply_park_operation(hwnd, state.operation_id)
    if not marked:
        logger.error("Park of hwnd=%s refused: operation mark could not be committed", hwnd)
        return None
    try:
        if int(placement.showCmd) == SW_SHOWMAXIMIZED or is_minimized(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        # Read the normal/restored size after ShowWindow.  Keeping the size intact
        # minimizes application layout work; only the position changes.
        left, top, right, bottom = get_window_rect(hwnd)
        width = max(1, right - left)
        height = max(1, bottom - top)
        park_x, park_y = _parking_position(width, height)
        expected = (park_x, park_y, park_x + width, park_y + height)
        if marked:
            state.park_rect = expected

        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE
        for attempt in range(3):
            verdict = classify_parked_window(hwnd, state)
            if verdict != VERIFY_MATCH:
                return None if verdict in (VERIFY_GONE, VERIFY_REUSED) else state
            ok = bool(user32.SetWindowPos(hwnd, None, park_x, park_y, 0, 0, flags))
            if ok:
                cur_left, cur_top, cur_right, cur_bottom = get_window_rect(hwnd)
                if abs(cur_left - park_x) <= 4 and abs(cur_top - park_y) <= 4:
                    state.park_rect = (cur_left, cur_top, cur_right, cur_bottom)
                    return state
            if attempt < 2:
                time.sleep(0.025 * (attempt + 1))
    except Exception:
        # The native sequence failed *after* the move may have happened.  The mark
        # is already on the window and the journal already owns the obligation, so
        # the state is returned: the caller keeps recovery ownership instead of
        # treating the window as never parked.
        logger.exception("Park of hwnd=%s failed after it was authorized", hwnd)
        if not marked:
            apply_park_operation(hwnd, state.operation_id)
        return state
    # SetWindowPos can succeed and still fail our coordinate verification.  The
    # source has already been moved at this point, so returning None without a
    # rollback can strand it outside the virtual desktop.  A state is returned
    # whenever a park may still be in effect, so the caller keeps ownership.
    if restore_parked_window_sync(hwnd, state):
        return None
    logger.error(
        "Park of hwnd=%s could not be verified and rollback failed; caller keeps recovery ownership",
        hwnd,
    )
    return state


def restore_parked_window_sync(hwnd: int, state: ParkedWindowState) -> bool:
    """Reliably restore a parked window from a worker thread.

    SetWindowPlacement returning TRUE only means the request was accepted.  A
    few applications (notably some RDP/1C/maximized-window paths) can still be
    left at the parking coordinates.  Therefore restore is transactional: do
    not report success until the window is observably back on a real monitor.

    The window's park mark is dropped only on that same proof, so a failed
    attempt still leaves the window identifiable as ours.
    """
    if not window_matches_parked_state(hwnd, state):
        return False
    placement = _copy_placement(state.placement)
    placement.length = ctypes.sizeof(placement)
    # The caller is already off the UI thread, so do not request asynchronous
    # placement here; we want completion before reporting success.
    placement.flags = int(placement.flags) & ~WPF_ASYNCWINDOWPLACEMENT
    for attempt in range(3):
        if not window_matches_parked_state(hwnd, state):
            return False
        if user32.SetWindowPlacement(hwnd, ctypes.byref(placement)):
            # Reinforce the requested show state.  Some applications accept
            # placement but defer their state transition until ShowWindow.
            show_cmd = int(placement.showCmd)
            if show_cmd == SW_SHOWMAXIMIZED:
                user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
            else:
                user32.ShowWindow(hwnd, SW_RESTORE)
            for delay in (0.0, 0.025, 0.06):
                if delay:
                    time.sleep(delay)
                if is_effectively_onscreen(hwnd):
                    clear_park_operation(hwnd)
                    return True
        if attempt < 2:
            time.sleep(0.025 * (attempt + 1))

    # Fallback: restore from the exact pre-park *screen* rectangle.  Do not use
    # WINDOWPLACEMENT.rcNormalPosition here because Microsoft documents that it
    # may be in workspace coordinates, while SetWindowPos consumes screen
    # coordinates.
    if not window_matches_parked_state(hwnd, state):
        return False
    left, top, right, bottom = _clamp_screen_rect_to_monitor(state.screen_rect)
    width = max(1, right - left)
    height = max(1, bottom - top)
    user32.ShowWindow(hwnd, SW_RESTORE)
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if user32.SetWindowPos(hwnd, None, left, top, width, height, flags):
        if int(state.placement.showCmd) == SW_SHOWMAXIMIZED:
            user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        for delay in (0.0, 0.025, 0.06, 0.12):
            if delay:
                time.sleep(delay)
            if is_effectively_onscreen(hwnd):
                clear_park_operation(hwnd)
                return True
    return False


def recover_orphaned_lookup_park(hwnd: int, *, operation_id: str = "", park_rect=()) -> bool:
    """Recover a source window parked by an older LookUp process.

    Once the original process is gone its exact WINDOWPLACEMENT is unavailable,
    so recovery favours safety: keep the current size where practical and put
    the window visibly on the monitor nearest the cursor.

    The window has to *prove* it is one of ours first - by the park operation
    stamped on it, by the rectangle the record says it was parked at, or by the
    parking signature of the current monitor layout.  Anything else is left alone,
    because moving a stranger is worse than leaving a window off-screen.
    """
    if not is_stranded_park(hwnd, operation_id=operation_id, park_rect=park_rect):
        return False
    left, top, right, bottom = get_window_rect(hwnd)
    width = max(320, right - left)
    height = max(200, bottom - top)
    point = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        work_left, work_top, work_right, work_bottom = work_area_for_point(point.x, point.y)
    else:
        work_left, work_top, work_right, work_bottom = _primary_work_area()
    width = min(width, max(320, work_right - work_left))
    height = min(height, max(200, work_bottom - work_top))
    x = work_left + max(0, (work_right - work_left - width) // 2)
    y = work_top + max(0, (work_bottom - work_top - height) // 2)
    user32.ShowWindow(hwnd, SW_RESTORE)
    ok = bool(user32.SetWindowPos(hwnd, None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE))
    if ok and is_effectively_onscreen(hwnd):
        clear_park_operation(hwnd)
        return True
    return False


def enumerate_recovery_windows() -> tuple[tuple[int, ...], bool]:
    """Every top-level window on this desktop, and whether that is the whole story.

    Recovery is the one enumeration that must not apply the UI filters: a hidden,
    cloaked, owned, tool or untitled window is *exactly* the kind of window a user
    loses, so filtering it out of the sweep means "there is nothing left to
    recover" is concluded from an incomplete list.

    The boolean is the honest part.  ``EnumWindows`` fails on a desktop that is
    being switched, locked or torn down, and a failed enumeration is *unknown* -
    never "no windows are parked".
    """
    found: list[int] = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        value = int(hwnd or 0)
        if value:
            found.append(value)
        return True

    try:
        complete = bool(user32.EnumWindows(callback, 0))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return tuple(found), False
    if not complete:
        logger.error(
            "The desktop could not be enumerated completely (%s window(s) seen); the "
            "sweep cannot conclude that nothing is parked",
            len(found),
        )
    return tuple(found), complete


def recover_all_orphaned_parks(exclude=()) -> "OrphanSweep":
    """Bring back every window that still sits where LookUp parked it.

    This is the recovery path for a journal that can no longer say what it owed.
    It needs no usable record, which is exactly why it is safe: a window is only
    moved when :func:`is_stranded_park` accepts it as one of ours, and
    :func:`recover_orphaned_lookup_park` re-checks that immediately before moving
    anything.  Windows of this process and the ones the caller is already
    executing are skipped.

    The result says what happened, not merely what was touched: an empty list of
    recovered windows means "nothing left" only when the enumeration was complete
    *and* no candidate failed to come back.  A restore that did not work, and an
    enumeration that could not run, are both reported - flattening them into an
    empty list is what let a damaged journal be called "empty".

    Blocking Win32 work: call it from a worker, never from the UI thread.
    """
    skip = {int(hwnd) for hwnd in exclude or ()}
    own_pid = os.getpid()
    recovered: list[int] = []
    pending: list[int] = []
    unknown: list[int] = []
    handles, complete = enumerate_recovery_windows()
    for hwnd in handles:
        if not hwnd or hwnd in skip:
            continue
        if not is_window(hwnd):
            continue
        pid = get_pid(hwnd)
        rect = query_window_rect(hwnd)
        if not pid or rect is None:
            if is_window(hwnd):
                unknown.append(hwnd)
            continue
        if pid == own_pid:
            # LookUp's own windows are never parked; this only keeps a bug from
            # turning recovery into a self-inflicted move.
            continue
        if not is_stranded_park(hwnd):
            continue
        if recover_orphaned_lookup_park(hwnd):
            logger.warning("Recovered a window orphaned in the LookUp parking position: hwnd=%s", hwnd)
            recovered.append(hwnd)
        elif is_stranded_park(hwnd):
            # It is still off-screen where we put it: the obligation is alive.
            pending.append(hwnd)
        else:
            # Its state could not be established (the window died, or a query
            # failed), so nothing may be concluded about it.
            unknown.append(hwnd)
    return OrphanSweep(
        recovered=tuple(recovered),
        pending=tuple(pending),
        unknown=tuple(unknown),
        complete=complete,
    )

def minimize_window(hwnd: int) -> bool:
    """Legacy helper retained for callers that explicitly need real minimize."""
    if not is_window(hwnd):
        return False
    return bool(user32.ShowWindowAsync(hwnd, SW_MINIMIZE))


def show_window_noactivate(hwnd: int) -> None:
    """Re-show a foreign window without activating it.

    Called from the UI dispatch path during refresh, so the request is posted
    asynchronously: a busy or hung target must never block LookUp's paint and
    input handling (the periodic refresh only needs the request to be queued).
    """
    if is_window(hwnd):
        user32.ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE)


def enumerate_windows(include_minimized: bool = False) -> list[WindowInfo]:
    # Apply cheap window-only filters inside EnumWindows so hidden/tool/owned
    # helpers never pay title/process queries.  Process identity is memoized per
    # PID for this scan, reducing OpenProcess to once per distinct process.
    hwnds: list[tuple[int, bool]] = []

    @WNDENUMPROC
    def callback(hwnd, lparam):
        value = int(hwnd or 0)
        if not value or not user32.IsWindowVisible(value):
            return True
        minimized = is_minimized(value)
        if minimized and not include_minimized:
            return True
        ex_style = int(_get_window_long_ptr(value, GWL_EXSTYLE))
        if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
            return True
        owner = user32.GetWindow(value, GW_OWNER)
        if owner and not ex_style & WS_EX_APPWINDOW:
            return True
        hwnds.append((value, minimized))
        return True

    user32.EnumWindows(callback, 0)

    identities: dict[int, ProcessIdentity] = {}
    result: list[WindowInfo] = []
    for hwnd, minimized in hwnds:
        title = get_window_text(hwnd)
        if not title.strip():
            continue
        if is_cloaked(hwnd):
            continue
        pid = get_pid(hwnd)
        identity = identities.get(pid)
        if identity is None:
            identity = _query_process_identity(pid)
            identities[pid] = identity
        result.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                pid=pid,
                class_name=get_class_name(hwnd),
                rect=get_window_rect(hwnd),
                minimized=minimized,
                process_created=identity.created,
                process_name=identity.name,
                process_access_denied=identity.access_denied,
            )
        )
    return result


class WindowFinder:
    def __init__(self, own_pid: int = 0):
        self.own_pid = own_pid

    @staticmethod
    def _candidate_from_info(info: WindowInfo) -> WindowCandidate:
        process_name = info.process_name
        if not process_name:
            process_name = "<нет доступа>" if info.process_access_denied else "?"
        return WindowCandidate(info=info, process_name=process_name)

    def list_windows(self) -> list[WindowCandidate]:
        result = [
            self._candidate_from_info(info)
            for info in enumerate_windows(include_minimized=True)
            if not self.own_pid or info.pid != self.own_pid
        ]
        result.sort(key=lambda cand: cand.title.lower())
        return result

    def matches(self, candidate: WindowCandidate, tracked) -> bool:
        return matches_target(tracked, candidate.process_name, candidate.title)

    def find(self, tracked, candidates: list[WindowCandidate] | None = None) -> WindowCandidate | None:
        pool = candidates if candidates is not None else self.list_windows()
        return next((candidate for candidate in pool if self.matches(candidate, tracked)), None)

    def find_unique(
        self, tracked, candidates: list[WindowCandidate] | None = None
    ) -> WindowCandidate | None:
        """Return a match only when refinding is unambiguous.

        Process-only profiles are convenient while an application has one top-level
        window, but silently rebinding them to an arbitrary window once several exist
        is worse than temporarily showing the source as unavailable.
        """
        pool = candidates if candidates is not None else self.list_windows()
        matches = [candidate for candidate in pool if self.matches(candidate, tracked)]
        return matches[0] if len(matches) == 1 else None

    def find_preferred(
        self, tracked, candidates: list[WindowCandidate] | None = None, *, exclude_hwnds=None
    ) -> WindowCandidate | None:
        """Return the best compatible candidate for auto-refind.

        ``title_contains`` remains the strict user-defined matcher.  Remembered
        title/class hints only rank compatible candidates and therefore cannot
        make a configured PiP disappear after a normal title change.

        ``exclude_hwnds`` keeps windows that are already bound to another PiP
        card out of the result.  Without it two entries whose filters both match
        a set of identically titled windows (the same project opened twice in
        VS Code) would resolve to the same window and show the same source twice.
        """
        pool = candidates if candidates is not None else self.list_windows()
        blocked = {int(hwnd) for hwnd in exclude_hwnds} if exclude_hwnds else set()
        matches = [
            candidate
            for candidate in pool
            if candidate.hwnd not in blocked and self.matches(candidate, tracked)
        ]
        index = preferred_candidate_index(
            [(candidate.title or "", candidate.info.class_name or "") for candidate in matches],
            title_hint=str(getattr(tracked, "title_hint", "") or ""),
            class_hint=str(getattr(tracked, "class_hint", "") or ""),
        )
        return matches[index] if index is not None else None

    def revalidate(self, hwnd: int, tracked) -> WindowCandidate | None:
        if not is_window(hwnd) or is_cloaked(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        info = build_window_info(hwnd)
        if info is None or (self.own_pid and info.pid == self.own_pid):
            return None
        candidate = self._candidate_from_info(info)
        return candidate if self.matches(candidate, tracked) else None

    def candidate(self, hwnd: int) -> WindowCandidate | None:
        if not is_window(hwnd) or is_cloaked(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        info = build_window_info(hwnd)
        if info is None or (self.own_pid and info.pid == self.own_pid):
            return None
        return self._candidate_from_info(info)


class ChangeDetector:
    def __init__(
        self,
        grid_w: int = 48,
        grid_h: int = 27,
        small_w: int = 128,
        small_h: int = 128,
    ):
        self.grid_w = max(1, int(grid_w))
        self.grid_h = max(1, int(grid_h))
        # Kept for backward-compatible construction and worker protocol.  The
        # capture path now lets GDI downscale directly to the comparison grid.
        self.small_w = max(self.grid_w, int(small_w))
        self.small_h = max(self.grid_h, int(small_h))
        self._comparator = GridComparator(cell_delta_threshold=24, blank_max_value=2)
        self._mem_dc = None
        self._small_dc = None
        self._full_bmp = None
        self._full_bmp_size: tuple[int, int] = (0, 0)
        self._small_bmp = None
        self._small_bmp_size: tuple[int, int] = (0, 0)
        self.last_capture_error: str | None = None

    def _capture_error(self, operation: str) -> None:
        code = int(ctypes.get_last_error() or 0)
        self.last_capture_error = f"{operation} failed" + (f" (winerror={code})" if code else "")

    def _ensure_capture_dcs(self, source_dc) -> bool:
        if self._mem_dc and self._small_dc:
            return True
        self.close_capture_resources()
        self._mem_dc = gdi32.CreateCompatibleDC(source_dc)
        self._small_dc = gdi32.CreateCompatibleDC(source_dc)
        if not self._mem_dc or not self._small_dc:
            self._capture_error("CreateCompatibleDC")
            self.close_capture_resources()
            return False
        return True

    def _cached_bitmap(self, source_dc, *, small: bool, width: int, height: int):
        attr = "_small_bmp" if small else "_full_bmp"
        size_attr = "_small_bmp_size" if small else "_full_bmp_size"
        bitmap = getattr(self, attr)
        if bitmap and getattr(self, size_attr) == (width, height):
            return bitmap
        if bitmap:
            gdi32.DeleteObject(bitmap)
        bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
        setattr(self, attr, bitmap or None)
        setattr(self, size_attr, (width, height) if bitmap else (0, 0))
        if not bitmap:
            self._capture_error("CreateCompatibleBitmap")
        return bitmap

    def close_capture_resources(self) -> None:
        for attr in ("_full_bmp", "_small_bmp"):
            bitmap = getattr(self, attr, None)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            setattr(self, attr, None)
        self._full_bmp_size = (0, 0)
        self._small_bmp_size = (0, 0)
        for attr in ("_mem_dc", "_small_dc"):
            dc = getattr(self, attr, None)
            if dc:
                gdi32.DeleteDC(dc)
            setattr(self, attr, None)

    def _capture_grid(self, hwnd: int) -> bytes | None:
        self.last_capture_error = None
        left, top, right, bottom = get_window_rect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            self.last_capture_error = "invalid window rectangle"
            return None

        # Use the target window's DC as the compatibility source.  The helper
        # process is per-monitor-DPI-aware, so GetWindowRect is in physical
        # monitor pixels and the capture bitmap uses the target's device format.
        ctypes.set_last_error(0)
        source_dc = user32.GetDC(hwnd)
        if not source_dc:
            self._capture_error("GetDC")
            return None

        raw: bytes | None = None
        full_bmp = None
        small_bmp = None
        old_full = None
        old_small = None
        cache_full = width * height <= 3_000_000
        try:
            if not self._ensure_capture_dcs(source_dc):
                return None
            full_bmp = (
                self._cached_bitmap(source_dc, small=False, width=width, height=height)
                if cache_full
                else gdi32.CreateCompatibleBitmap(source_dc, width, height)
            )
            small_bmp = self._cached_bitmap(
                source_dc, small=True, width=self.grid_w, height=self.grid_h
            )
            if not full_bmp or not small_bmp:
                if not self.last_capture_error:
                    self._capture_error("CreateCompatibleBitmap")
                return None

            old_full = gdi32.SelectObject(self._mem_dc, full_bmp)
            old_small = gdi32.SelectObject(self._small_dc, small_bmp)
            if not old_full or not old_small:
                self._capture_error("SelectObject")
                return None

            ctypes.set_last_error(0)
            printed = bool(user32.PrintWindow(hwnd, self._mem_dc, PW_RENDERFULLCONTENT))
            if not printed:
                self._capture_error("PrintWindow")
                return None

            gdi32.SetStretchBltMode(self._small_dc, HALFTONE)
            ctypes.set_last_error(0)
            stretched = bool(
                gdi32.StretchBlt(
                    self._small_dc,
                    0,
                    0,
                    self.grid_w,
                    self.grid_h,
                    self._mem_dc,
                    0,
                    0,
                    width,
                    height,
                    SRCCOPY,
                )
            )
            if not stretched:
                self._capture_error("StretchBlt")
                return None

            # GetDIBits requires the bitmap not to be selected into a DC.
            gdi32.SelectObject(self._small_dc, old_small)
            old_small = None
            gdi32.SelectObject(self._mem_dc, old_full)
            old_full = None

            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = self.grid_w
            header.biHeight = -self.grid_h
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            buf = ctypes.create_string_buffer(self.grid_w * self.grid_h * 4)
            ctypes.set_last_error(0)
            lines = gdi32.GetDIBits(
                self._small_dc,
                small_bmp,
                0,
                self.grid_h,
                buf,
                ctypes.byref(header),
                0,
            )
            if lines != self.grid_h:
                self._capture_error("GetDIBits")
                return None
            raw = buf.raw
        finally:
            if old_small:
                gdi32.SelectObject(self._small_dc, old_small)
            if old_full:
                gdi32.SelectObject(self._mem_dc, old_full)
            if full_bmp and not cache_full:
                gdi32.DeleteObject(full_bmp)
            user32.ReleaseDC(hwnd, source_dc)

        if raw is None:
            return None

        # GDI already performed the expensive resampling.  Convert one pixel
        # per comparison cell to luma: 48x27 -> only 1296 Python iterations.
        grid = bytearray(self.grid_w * self.grid_h)
        for index in range(len(grid)):
            off = index * 4
            grid[index] = (raw[off + 2] * 77 + raw[off + 1] * 150 + raw[off] * 29) >> 8
        return bytes(grid)

    def compare_grid(self, hwnd: int, grid: bytes | None) -> ChangeResult:
        return self._comparator.compare(hwnd, grid)

    def diff(self, hwnd: int) -> ChangeResult:
        return self.compare_grid(hwnd, self._capture_grid(hwnd))

    def forget(self, hwnd: int) -> None:
        self._comparator.forget(hwnd)

    def clear(self) -> None:
        self._comparator.clear()


def _enable_capture_dpi_awareness() -> None:
    """Set DPI awareness inside a spawned capture helper before any HWND work."""
    try:
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        if setter(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = shcore.SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        if setter(2) == 0:  # S_OK; PROCESS_PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        legacy = user32.SetProcessDPIAware
        legacy.restype = wintypes.BOOL
        legacy()
    except (AttributeError, OSError, ValueError):
        pass


def _capture_process_main(
    connection,
    grid_w: int,
    grid_h: int,
    small_w: int,
    small_h: int,
) -> None:
    """Persistent killable PrintWindow helper used by AsyncChangeDetector.

    The helper does not touch a single window until the parent has confirmed -
    with one byte on the pipe - that the kill-on-close job accepted it.  That
    closes the start/adopt gap: an unbound helper never captures anything, so it
    can never be an orphan doing real work if LookUp dies.
    """
    try:
        if connection.recv_bytes(1) != HELPER_ARMED_TOKEN:
            return
    except (EOFError, OSError, ValueError):
        # The owner died before it could arm us: there is nothing to capture and
        # nothing to clean up beyond this process' own exit.
        return
    _enable_capture_dpi_awareness()
    detector = ChangeDetector(grid_w=grid_w, grid_h=grid_h, small_w=small_w, small_h=small_h)
    try:
        while True:
            try:
                command = connection.recv()
            except (EOFError, OSError):
                return
            if command is None:
                return
            task_id, hwnd = command
            try:
                grid = detector._capture_grid(int(hwnd))
                connection.send((task_id, grid, detector.last_capture_error))
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
                try:
                    connection.send((task_id, None, detail))
                except (BrokenPipeError, EOFError, OSError):
                    return
    finally:
        detector.close_capture_resources()
        try:
            connection.close()
        except OSError:
            pass


@dataclass(frozen=True)
class _CaptureRequest:
    generation: int
    reset_token: int
    hwnd: int
    submitted_at: float


class _CaptureProcessSlot:
    def __init__(self, index: int):
        self.index = index
        self.process = None
        self.connection = None
        self.task_id = 0
        self.request: _CaptureRequest | None = None
        self.started_at = 0.0
        self.retired = False
    @property
    def busy(self) -> bool:
        return self.request is not None and bool(self.task_id)

    def clear_task(self) -> None:
        self.task_id = 0
        self.request = None
        self.started_at = 0.0


class AsyncChangeDetector:
    """Rolling process-pool supervisor for potentially blocking PrintWindow calls.

    Requests are tracked per HWND rather than as one monolithic batch.  A slow
    window therefore cannot prevent later windows from being queued, resets have
    per-window tokens, and global clear cancels the old generation immediately.
    """

    def __init__(
        self,
        detector: ChangeDetector | None = None,
        *,
        max_workers: int = 2,
        capture_timeout: float = 3.0,
    ):
        self._detector = detector or ChangeDetector()
        self._results: queue.Queue[tuple[int, int, int, ChangeResult]] = queue.Queue()
        self._state_lock = threading.Lock()
        self._requests: deque[_CaptureRequest] = deque()
        self._pending_since: dict[tuple[int, int], float] = {}
        self._resets: set[int] = set()
        self._reset_tokens: dict[int, int] = {}
        self._reset_sequence = 0
        self._clear_requested = False
        self._generation = 0
        self._closed = False
        self._worker_error: str | None = None
        # Set when a helper had to be destroyed because it could not be bound to
        # the kill-on-close job.  The detector then stays deliberately degraded
        # instead of trading the orphan guarantee for a preview.
        self._lifetime_binding_failed = False
        self._max_workers = max(1, min(3, int(max_workers)))
        self._capture_timeout = max(0.5, float(capture_timeout))
        self._ctx = multiprocessing.get_context("spawn")
        self._task_sequence = 0
        self._wake = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            name="LookUpWindows-ChangeDetector",
            daemon=True,
        )
        self._worker.start()

    def schedule(self, hwnds) -> bool:
        unique = tuple(dict.fromkeys(int(hwnd) for hwnd in hwnds if hwnd))
        if not unique:
            return False
        now = time.monotonic()
        added = False
        with self._state_lock:
            if self._closed:
                return False
            generation = self._generation
            for hwnd in unique:
                key = (generation, hwnd)
                if key in self._pending_since:
                    continue
                self._reset_sequence += 1
                self._reset_tokens[hwnd] = self._reset_sequence
                request = _CaptureRequest(
                    generation=generation,
                    reset_token=self._reset_tokens.get(hwnd, 0),
                    hwnd=hwnd,
                    submitted_at=now,
                )
                self._requests.append(request)
                self._pending_since[key] = now
                added = True
        if added:
            self._wake.set()
        return added

    def poll_results(self) -> list[tuple[int, ChangeResult]]:
        results: list[tuple[int, ChangeResult]] = []
        while True:
            try:
                generation, reset_token, hwnd, result = self._results.get_nowait()
            except queue.Empty:
                break
            with self._state_lock:
                valid = (
                    generation == self._generation
                    and reset_token == self._reset_tokens.get(hwnd, 0)
                )
            if valid:
                results.append((hwnd, result))
        return results

    def forget(self, hwnd: int) -> None:
        hwnd = int(hwnd or 0)
        if not hwnd:
            return
        with self._state_lock:
            # Scheduled requests always carry a positive, unique token.
            # Removing the binding rejects all their late results without
            # retaining a tombstone for every HWND seen during the session.
            self._reset_tokens.pop(hwnd, None)
            self._resets.add(hwnd)
        self._wake.set()

    def clear(self) -> None:
        with self._state_lock:
            self._clear_requested = True
            self._resets.clear()
            self._generation += 1
            self._reset_tokens.clear()
            self._requests.clear()
            self._pending_since.clear()
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                break
        self._wake.set()

    def busy_for(self) -> float:
        with self._state_lock:
            generation = self._generation
            pending = [
                submitted
                for (item_generation, _hwnd), submitted in self._pending_since.items()
                if item_generation == generation
            ]
        if not pending:
            return 0.0
        return max(0.0, time.monotonic() - min(pending))

    def close(self, timeout: float = 1.5) -> bool:
        with self._state_lock:
            self._closed = True
            self._requests.clear()
            self._pending_since.clear()
            worker = self._worker
        self._wake.set()
        if worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(timeout)))
        stopped = not worker.is_alive()
        if not stopped:
            logger.warning("Change detector supervisor did not stop before deadline")
        return stopped

    def healthy(self) -> bool:
        with self._state_lock:
            if self._closed:
                return not self._worker.is_alive()
            return self._worker.is_alive() and self._worker_error is None

    def lifetime_binding_degraded(self) -> bool:
        """True when capture work was refused because helpers could not be bound."""
        with self._state_lock:
            return self._lifetime_binding_failed

    def _is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _current_generation(self) -> int:
        with self._state_lock:
            return self._generation

    def _request_is_current(self, request: _CaptureRequest) -> bool:
        with self._state_lock:
            return (
                not self._closed
                and request.generation == self._generation
                and request.reset_token == self._reset_tokens.get(request.hwnd, 0)
            )

    def _apply_resets(self) -> None:
        with self._state_lock:
            clear_requested = self._clear_requested
            resets = tuple(self._resets)
            self._clear_requested = False
            self._resets.clear()
        if clear_requested:
            self._detector.clear()
        else:
            for hwnd in resets:
                self._detector.forget(hwnd)

    def _take_request(self) -> _CaptureRequest | None:
        while True:
            with self._state_lock:
                if not self._requests:
                    return None
                request = self._requests.popleft()
                key = (request.generation, request.hwnd)
                current = (
                    not self._closed
                    and request.generation == self._generation
                    and request.reset_token == self._reset_tokens.get(request.hwnd, 0)
                )
                if not current:
                    self._pending_since.pop(key, None)
            if current:
                return request

    def _finish_request(self, request: _CaptureRequest) -> None:
        with self._state_lock:
            key = (request.generation, request.hwnd)
            self._pending_since.pop(key, None)

    def _start_slot(self, slot: _CaptureProcessSlot) -> bool:
        # A process that survived TerminateProcess is deliberately not replaced:
        # reducing detector capacity is safer than accumulating live orphan helpers.
        if slot.retired:
            return False
        if slot.process is not None:
            try:
                if slot.process.is_alive():
                    return False
                slot.process.close()
            except (OSError, ValueError):
                pass
            slot.process = None
        parent_conn = child_conn = None
        process = None
        try:
            parent_conn, child_conn = self._ctx.Pipe(duplex=True)
            process = self._ctx.Process(
                target=_capture_process_main,
                args=(
                    child_conn,
                    self._detector.grid_w,
                    self._detector.grid_h,
                    self._detector.small_w,
                    self._detector.small_h,
                ),
                name=f"LookUpWindows-Capture-{slot.index + 1}",
                daemon=True,
            )
            process.start()
            child_conn.close()
            # Bind the helper to the kill-on-close job before it can do any
            # work: if LookUp is hard-terminated later, the OS still removes the
            # helper even while it is blocked inside PrintWindow.  A helper that
            # cannot be bound is destroyed here and the slot is retired - it is
            # never armed, never fed a request and never left running unbound.
            if not capture_helper_job().adopt(process):
                self._discard_unbound_helper(
                    slot,
                    process,
                    "capture helper could not be bound to the kill-on-close job",
                    parent_conn,
                    child_conn,
                )
                return False
            parent_conn.send_bytes(HELPER_ARMED_TOKEN)
            slot.process = process
            slot.connection = parent_conn
            slot.clear_task()
            return True
        except (OSError, RuntimeError):
            logger.exception("Failed to start capture helper slot %s", slot.index + 1)
            if process is not None and process.pid is not None:
                self._discard_unbound_helper(slot, process, "helper startup failed", parent_conn, child_conn)
                return False
            for conn in (parent_conn, child_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
            slot.process = None
            slot.connection = None
            slot.clear_task()
            return False

    def _discard_unbound_helper(
        self,
        slot: _CaptureProcessSlot,
        process,
        reason: str,
        *connections,
    ) -> None:
        """Kill a helper that must never be used, and give up the slot.

        The failure mode this removes is the interesting one: a helper that is
        alive, blocked in ``PrintWindow`` and not covered by any OS-level
        lifetime binding, i.e. the orphan that survives a hard owner death.
        Losing change detection for the rest of the session is the price.
        """
        with self._state_lock:
            self._lifetime_binding_failed = True
        # Close the pipes first: the helper is still waiting for its arming
        # token, so a closed pipe makes it return and exit on its own instead of
        # having to be killed while it does real work.
        for conn in connections:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)
        except (OSError, ValueError):
            pass
        slot.process = None
        slot.connection = None
        slot.clear_task()
        try:
            alive = process.is_alive()
        except (OSError, ValueError):
            alive = False
        pid = None
        try:
            pid = getattr(process, "pid", None)
        except (OSError, ValueError):
            # A closed multiprocessing.Process refuses to report its pid.
            pid = "<unknown>"
        # The slot is retired either way: the job is a process-wide resource, so
        # a binding failure is not a per-slot hiccup.  Retiring it also stops the
        # detector from spawning a process per refresh just to kill it again.
        slot.retired = True
        if alive:
            logger.error(
                "Capture helper PID %s could not be terminated after %s; slot %s stays retired "
                "(it is not bound to the kill-on-close job and is not used)",
                pid,
                reason,
                slot.index + 1,
            )
            return
        try:
            process.close()
        except (OSError, ValueError):
            pass
        logger.error(
            "Capture helper PID %s was discarded: %s (change detection stays disabled)",
            pid,
            reason,
        )

    def _stop_slot(self, slot: _CaptureProcessSlot, *, graceful: bool) -> bool:
        process = slot.process
        connection = slot.connection
        stopped = True
        if graceful and connection is not None and process is not None:
            try:
                if process.is_alive():
                    connection.send(None)
                    process.join(timeout=0.15)
            except (BrokenPipeError, EOFError, OSError, ValueError):
                pass
        if process is not None:
            try:
                alive = process.is_alive()
            except (OSError, ValueError):
                alive = False
            if alive:
                try:
                    process.terminate()
                except (OSError, ValueError):
                    pass
                process.join(timeout=0.75)
                try:
                    alive = process.is_alive()
                except (OSError, ValueError):
                    alive = False
                if alive:
                    stopped = False
                    slot.retired = True
                    logger.error(
                        "Capture helper PID %s did not terminate; slot %s is retired "
                        "(it stays bound to the kill-on-close job and dies with LookUp)",
                        getattr(process, "pid", None),
                        slot.index + 1,
                    )
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        slot.connection = None
        slot.clear_task()
        if process is not None and stopped:
            try:
                process.close()
            except (OSError, ValueError):
                pass
            slot.process = None
        return stopped

    def _restart_slot(self, slot: _CaptureProcessSlot) -> bool:
        if not self._stop_slot(slot, graceful=False):
            return False
        if self._is_closed():
            return False
        return self._start_slot(slot)

    def _assign_request(self, slot: _CaptureProcessSlot, request: _CaptureRequest) -> bool:
        for _attempt in range(2):
            process = slot.process
            connection = slot.connection
            if process is None or connection is None:
                if not self._start_slot(slot):
                    return False
                process = slot.process
                connection = slot.connection
            else:
                try:
                    if not process.is_alive():
                        if not self._restart_slot(slot):
                            return False
                        process = slot.process
                        connection = slot.connection
                except (OSError, ValueError):
                    if not self._restart_slot(slot):
                        return False
                    process = slot.process
                    connection = slot.connection

            self._task_sequence += 1
            task_id = self._task_sequence
            try:
                connection.send((task_id, request.hwnd))
            except (BrokenPipeError, EOFError, OSError):
                if not self._restart_slot(slot):
                    return False
                continue
            slot.task_id = task_id
            slot.request = request
            slot.started_at = time.monotonic()
            return True
        return False

    def _emit_grid(
        self,
        request: _CaptureRequest,
        grid: bytes | None,
        error_detail: str | None,
    ) -> None:
        with self._state_lock:
            current = (
                not self._closed
                and request.generation == self._generation
                and request.reset_token == self._reset_tokens.get(request.hwnd, 0)
            )
            if not current:
                return
            try:
                result = self._detector.compare_grid(request.hwnd, grid)
            except Exception:
                logger.exception("Change comparison failed for hwnd=%s", request.hwnd)
                self._detector.forget(request.hwnd)
                result = ChangeResult(status="capture_failed")
        if error_detail:
            logger.debug("Capture failed for hwnd=%s: %s", request.hwnd, error_detail)
        self._results.put(
            (request.generation, request.reset_token, request.hwnd, result)
        )

    def _emit_status(self, request: _CaptureRequest, status: str) -> None:
        with self._state_lock:
            current = (
                not self._closed
                and request.generation == self._generation
                and request.reset_token == self._reset_tokens.get(request.hwnd, 0)
            )
            if not current:
                return
            # Timeout/pipe/process failures did not pass through compare_grid(None),
            # so invalidate the baseline here.  Otherwise the next good frame is
            # compared with a pre-failure image and can create a false alert.
            if status in {"capture_failed", "capture_timeout"}:
                self._detector.forget(request.hwnd)
        self._results.put(
            (
                request.generation,
                request.reset_token,
                request.hwnd,
                ChangeResult(status=status),
            )
        )

    def _cancel_stale_generation_slots(self, slots: list[_CaptureProcessSlot]) -> None:
        generation = self._current_generation()
        for slot in slots:
            request = slot.request
            if request is None or request.generation == generation:
                continue
            slot.clear_task()
            self._stop_slot(slot, graceful=False)

    def _fill_slots(self, slots: list[_CaptureProcessSlot]) -> None:
        usable_slots = [slot for slot in slots if not slot.retired]
        if not usable_slots:
            # A helper surviving TerminateProcess is pathological.  Do not leak an
            # unbounded request queue when every slot has been retired.
            while True:
                request = self._take_request()
                if request is None:
                    return
                self._emit_status(request, "capture_failed")
                self._finish_request(request)
        for slot in usable_slots:
            if slot.busy:
                continue
            request = self._take_request()
            if request is None:
                return
            if not self._assign_request(slot, request):
                self._emit_status(request, "capture_failed")
                self._finish_request(request)

    def _collect_ready(self, slots: list[_CaptureProcessSlot], ready_connections: set) -> bool:
        made_progress = False
        for slot in slots:
            if not slot.busy or slot.connection not in ready_connections:
                continue
            request = slot.request
            task_id = slot.task_id
            connection = slot.connection
            if request is None or connection is None:
                continue
            try:
                received_id, grid, error_detail = connection.recv()
            except (EOFError, OSError):
                received_id, grid, error_detail = task_id, None, "capture pipe closed"
            slot.clear_task()
            if received_id == task_id:
                self._emit_grid(request, grid, error_detail)
            else:
                self._emit_status(request, "capture_failed")
            self._finish_request(request)
            made_progress = True
        return made_progress

    def _check_slots(self, slots: list[_CaptureProcessSlot]) -> bool:
        made_progress = False
        now = time.monotonic()
        for slot in slots:
            if not slot.busy:
                continue
            request = slot.request
            process = slot.process
            if request is None:
                slot.clear_task()
                continue
            try:
                alive = process is not None and process.is_alive()
            except (OSError, ValueError):
                alive = False
            if not alive:
                slot.clear_task()
                self._emit_status(request, "capture_failed")
                self._finish_request(request)
                self._restart_slot(slot)
                made_progress = True
                continue
            if now - slot.started_at >= self._capture_timeout:
                slot.clear_task()
                self._emit_status(request, "capture_timeout")
                self._finish_request(request)
                self._restart_slot(slot)
                made_progress = True
        return made_progress

    def _wait_for_activity(self, slots: list[_CaptureProcessSlot]) -> set:
        connections = [slot.connection for slot in slots if slot.busy and slot.connection is not None]
        if not connections:
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            return set()
        remaining = [
            max(0.0, self._capture_timeout - (time.monotonic() - slot.started_at))
            for slot in slots
            if slot.busy
        ]
        timeout = min([0.05, *remaining]) if remaining else 0.05
        try:
            ready = set(wait_connections(connections, timeout=max(0.0, timeout)))
        except (OSError, ValueError):
            ready = set()
        self._wake.clear()
        return ready

    def _run(self) -> None:
        slots = [_CaptureProcessSlot(index) for index in range(self._max_workers)]
        try:
            while not self._is_closed():
                try:
                    self._apply_resets()
                    self._cancel_stale_generation_slots(slots)
                    self._fill_slots(slots)
                    ready = self._wait_for_activity(slots)
                    if ready:
                        self._collect_ready(slots, ready)
                    self._check_slots(slots)
                except Exception as exc:
                    with self._state_lock:
                        self._worker_error = repr(exc)
                    logger.exception("Change detector supervisor iteration crashed")
                    time.sleep(0.05)
                else:
                    with self._state_lock:
                        self._worker_error = None
        finally:
            for slot in slots:
                request = slot.request
                if request is not None:
                    self._finish_request(request)
                self._stop_slot(slot, graceful=False)
