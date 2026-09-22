from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p
HMONITOR = ctypes.c_void_p

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
WPF_ASYNCWINDOWPLACEMENT = 0x0004
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
user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
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
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

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


@dataclass
class ParkedWindowState:
    placement: WINDOWPLACEMENT | WINDOWPLACEMENT_LEGACY
    # GetWindowPlacement uses workspace coordinates for ordinary top-level
    # windows, while SetWindowPos uses screen coordinates.  Keep the actual
    # pre-park screen rectangle as an independent recovery path instead of
    # trying to reinterpret rcNormalPosition.
    screen_rect: tuple[int, int, int, int]


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    class_name: str
    rect: tuple[int, int, int, int]
    minimized: bool


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


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def build_window_info(hwnd: int) -> WindowInfo | None:
    if not is_window(hwnd):
        return None
    return WindowInfo(
        hwnd=int(hwnd),
        title=get_window_text(hwnd),
        pid=get_pid(hwnd),
        class_name=get_class_name(hwnd),
        rect=get_window_rect(hwnd),
        minimized=is_minimized(hwnd),
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


def set_foreground(hwnd: int) -> bool:
    if not is_window(hwnd):
        return False
    if user32.IsIconic(hwnd):
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
    cls = type(value)
    copy = cls()
    ctypes.memmove(ctypes.byref(copy), ctypes.byref(value), ctypes.sizeof(value))
    return copy


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
    virtual_left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    virtual_top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    # Keep only a 1x1 corner inside the virtual desktop.  To the user the source
    # is effectively gone, while DWM still has a tiny on-screen intersection;
    # this is friendlier to applications/compositors that throttle windows which
    # are completely outside every monitor.  SetWindowPos itself does not clamp
    # these coordinates (unlike SetWindowPlacement).
    return (
        virtual_left - max(1, int(width)) + 1,
        virtual_top - max(1, int(height)) + 1,
    )


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)))
    height = max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))
    return (left, top, left + width, top + height)


def _rect_visible_pixels(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = rect
    vleft, vtop, vright, vbottom = _virtual_screen_rect()
    width = max(0, min(right, vright) - max(left, vleft))
    height = max(0, min(bottom, vbottom) - max(top, vtop))
    return (width, height)


def looks_like_lookup_parked(hwnd: int) -> bool:
    """Return True for the characteristic 1x1 parking position used by LookUp.

    This is intentionally strict.  It is used both to verify restore and to
    recover windows stranded by an earlier LookUp process that exited after
    parking them.
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


def is_effectively_onscreen(hwnd: int, min_visible: int = 24) -> bool:
    if not is_window(hwnd) or is_minimized(hwnd) or looks_like_lookup_parked(hwnd):
        return False
    width, height = _rect_visible_pixels(get_window_rect(hwnd))
    return width >= int(min_visible) and height >= int(min_visible)


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
    state = ParkedWindowState(_copy_placement(placement), rect)

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
    if not is_window(hwnd):
        return False
    placement = _copy_placement(state.placement)
    placement.length = ctypes.sizeof(placement)
    placement.flags = int(placement.flags) | WPF_ASYNCWINDOWPLACEMENT
    return bool(user32.SetWindowPlacement(hwnd, ctypes.byref(placement)))



def park_window_offscreen_sync(hwnd: int) -> ParkedWindowState | None:
    """Reliably park a foreign window from a worker thread.

    Unlike :func:`park_window_offscreen`, this function deliberately performs
    the restore/move synchronously.  Call it only outside the UI thread.  The
    synchronous sequence is important for maximized windows: posting
    ``SW_RESTORE`` and ``SetWindowPos`` back-to-back can race in the target
    thread, leaving the window apparently unchanged.
    """
    if not is_window(hwnd):
        return None
    placement = get_window_placement(hwnd)
    if placement is None:
        return None
    original_rect = get_window_rect(hwnd)
    state = ParkedWindowState(_copy_placement(placement), original_rect)

    if int(placement.showCmd) == SW_SHOWMAXIMIZED or is_minimized(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # Read the normal/restored size after ShowWindow.  Keeping the size intact
    # minimizes application layout work; only the position changes.
    left, top, right, bottom = get_window_rect(hwnd)
    width = max(1, right - left)
    height = max(1, bottom - top)
    park_x, park_y = _parking_position(width, height)

    flags = SWP_NOZORDER | SWP_NOACTIVATE | 0x0001  # SWP_NOSIZE
    for attempt in range(3):
        if not is_window(hwnd):
            return None
        ok = bool(user32.SetWindowPos(hwnd, None, park_x, park_y, 0, 0, flags))
        if ok:
            cur_left, cur_top, _cur_right, _cur_bottom = get_window_rect(hwnd)
            if abs(cur_left - park_x) <= 4 and abs(cur_top - park_y) <= 4:
                return state
        if attempt < 2:
            time.sleep(0.025 * (attempt + 1))
    return None


def restore_parked_window_sync(hwnd: int, state: ParkedWindowState) -> bool:
    """Reliably restore a parked window from a worker thread.

    SetWindowPlacement returning TRUE only means the request was accepted.  A
    few applications (notably some RDP/1C/maximized-window paths) can still be
    left at the parking coordinates.  Therefore restore is transactional: do
    not report success until the window is observably back on a real monitor.
    """
    if not is_window(hwnd):
        return False
    placement = _copy_placement(state.placement)
    placement.length = ctypes.sizeof(placement)
    # The caller is already off the UI thread, so do not request asynchronous
    # placement here; we want completion before reporting success.
    placement.flags = int(placement.flags) & ~WPF_ASYNCWINDOWPLACEMENT
    for attempt in range(3):
        if not is_window(hwnd):
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
                    return True
        if attempt < 2:
            time.sleep(0.025 * (attempt + 1))

    # Fallback: restore from the exact pre-park *screen* rectangle.  Do not use
    # WINDOWPLACEMENT.rcNormalPosition here because Microsoft documents that it
    # may be in workspace coordinates, while SetWindowPos consumes screen
    # coordinates.
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
                return True
    return False


def recover_orphaned_lookup_park(hwnd: int) -> bool:
    """Recover a source window parked by an older LookUp process.

    Once the original process is gone its exact WINDOWPLACEMENT is unavailable,
    so recovery favours safety: keep the current size where practical and put
    the window visibly on the monitor nearest the cursor.
    """
    if not looks_like_lookup_parked(hwnd):
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
    return ok and is_effectively_onscreen(hwnd)

def minimize_window(hwnd: int) -> bool:
    """Legacy helper retained for callers that explicitly need real minimize."""
    if not is_window(hwnd):
        return False
    return bool(user32.ShowWindowAsync(hwnd, SW_MINIMIZE))


def show_window_noactivate(hwnd: int) -> None:
    if is_window(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def enumerate_windows(include_minimized: bool = False) -> list[WindowInfo]:
    hwnds: list[int] = []

    @WNDENUMPROC
    def callback(hwnd, lparam):
        value = int(hwnd or 0)
        if value:
            hwnds.append(value)
        return True

    user32.EnumWindows(callback, 0)

    result: list[WindowInfo] = []
    for hwnd in hwnds:
        # include_minimized means "include visible iconic windows", not hidden helper windows.
        if not user32.IsWindowVisible(hwnd):
            continue
        minimized = is_minimized(hwnd)
        if minimized and not include_minimized:
            continue
        if is_cloaked(hwnd):
            continue
        title = get_window_text(hwnd)
        if not title.strip():
            continue
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
            continue
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if owner and not ex_style & WS_EX_APPWINDOW:
            continue
        result.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                pid=get_pid(hwnd),
                class_name=get_class_name(hwnd),
                rect=get_window_rect(hwnd),
                minimized=minimized,
            )
        )
    return result


_PROCESS_CACHE_TTL = 10.0
_process_names: dict[int, tuple[float, str]] = {}
_process_access_denied: dict[int, float] = {}


def get_process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    now = time.monotonic()
    cached = _process_names.get(pid)
    if cached is not None and now - cached[0] <= _PROCESS_CACHE_TTL:
        return cached[1]

    name = ""
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                full = buf.value
                name = full.rsplit("\\", 1)[-1] if full else ""
        finally:
            kernel32.CloseHandle(handle)
        _process_access_denied.pop(pid, None)
    elif ctypes.get_last_error() == 5:
        _process_access_denied[pid] = now

    if name:
        _process_names[pid] = (now, name)
    else:
        _process_names.pop(pid, None)
    # Keep the tiny cache bounded even across long sessions/PID churn.
    if len(_process_names) > 512:
        cutoff = now - _PROCESS_CACHE_TTL
        for cached_pid, (stamp, _value) in list(_process_names.items()):
            if stamp < cutoff:
                _process_names.pop(cached_pid, None)
    return name


def process_access_denied(pid: int) -> bool:
    stamp = _process_access_denied.get(pid)
    return stamp is not None and time.monotonic() - stamp <= _PROCESS_CACHE_TTL


class WindowFinder:
    def __init__(self, own_pid: int = 0):
        self.own_pid = own_pid

    def list_windows(self) -> list[WindowCandidate]:
        result: list[WindowCandidate] = []
        for info in enumerate_windows(include_minimized=True):
            if self.own_pid and info.pid == self.own_pid:
                continue
            process_name = get_process_name(info.pid)
            if not process_name:
                process_name = "<нет доступа>" if process_access_denied(info.pid) else "?"
            result.append(WindowCandidate(info=info, process_name=process_name))
        result.sort(key=lambda cand: cand.title.lower())
        return result

    def matches(self, candidate: WindowCandidate, tracked) -> bool:
        process = getattr(tracked, "process", "") or ""
        title_contains = getattr(tracked, "title_contains", "") or ""
        if process and candidate.process_name.lower() != process.lower():
            return False
        if title_contains and title_contains.lower() not in candidate.title.lower():
            return False
        return True

    def find(self, tracked) -> WindowCandidate | None:
        for info in enumerate_windows(include_minimized=True):
            if self.own_pid and info.pid == self.own_pid:
                continue
            candidate = WindowCandidate(info=info, process_name=get_process_name(info.pid) or "?")
            if self.matches(candidate, tracked):
                return candidate
        return None

    def revalidate(self, hwnd: int, tracked) -> WindowCandidate | None:
        if not is_window(hwnd) or is_cloaked(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        info = build_window_info(hwnd)
        if info is None:
            return None
        if self.own_pid and info.pid == self.own_pid:
            return None
        process_name = get_process_name(info.pid) or "?"
        candidate = WindowCandidate(info=info, process_name=process_name)
        return candidate if self.matches(candidate, tracked) else None

    def candidate(self, hwnd: int) -> WindowCandidate | None:
        if not is_window(hwnd) or is_cloaked(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        info = build_window_info(hwnd)
        if info is None or (self.own_pid and info.pid == self.own_pid):
            return None
        process_name = get_process_name(info.pid)
        if not process_name:
            process_name = "<нет доступа>" if process_access_denied(info.pid) else "?"
        return WindowCandidate(info=info, process_name=process_name)


@dataclass(frozen=True)
class ChangeResult:
    status: str
    score: float | None = None
    changed_fraction: float = 0.0


class ChangeDetector:
    def __init__(self, grid_w: int = 48, grid_h: int = 27, small_w: int = 128):
        self.grid_w = grid_w
        self.grid_h = grid_h
        self.small_w = small_w
        self._last: dict[int, bytes] = {}

    def _capture_grid(self, hwnd: int) -> bytes | None:
        left, top, right, bottom = get_window_rect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        screen_dc = user32.GetDC(None)
        if not screen_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        small_dc = gdi32.CreateCompatibleDC(screen_dc)
        small_w = max(16, min(self.small_w, width))
        small_h = max(16, int(small_w * height / width))
        full_bmp = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        small_bmp = gdi32.CreateCompatibleBitmap(screen_dc, small_w, small_h)
        raw: bytes | None = None
        try:
            if not mem_dc or not small_dc or not full_bmp or not small_bmp:
                return None
            old_full = gdi32.SelectObject(mem_dc, full_bmp)
            printed = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))
            old_small = gdi32.SelectObject(small_dc, small_bmp)
            gdi32.SetStretchBltMode(small_dc, HALFTONE)
            stretched = bool(
                gdi32.StretchBlt(small_dc, 0, 0, small_w, small_h, mem_dc, 0, 0, width, height, SRCCOPY)
            )
            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = small_w
            header.biHeight = -small_h
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            buf = ctypes.create_string_buffer(small_w * small_h * 4)
            lines = gdi32.GetDIBits(small_dc, small_bmp, 0, small_h, buf, ctypes.byref(header), 0)
            gdi32.SelectObject(small_dc, old_small)
            gdi32.SelectObject(mem_dc, old_full)
            if printed and stretched and lines == small_h:
                raw = buf.raw
        finally:
            for obj in (full_bmp, small_bmp):
                if obj:
                    gdi32.DeleteObject(obj)
            for dc in (mem_dc, small_dc):
                if dc:
                    gdi32.DeleteDC(dc)
            user32.ReleaseDC(None, screen_dc)

        if raw is None:
            return None

        grid = bytearray(self.grid_w * self.grid_h)
        for gy in range(self.grid_h):
            y0 = gy * small_h // self.grid_h
            y1 = max(y0 + 1, (gy + 1) * small_h // self.grid_h)
            for gx in range(self.grid_w):
                x0 = gx * small_w // self.grid_w
                x1 = max(x0 + 1, (gx + 1) * small_w // self.grid_w)
                total = 0
                count = 0
                for y in range(y0, y1):
                    base = (y * small_w + x0) * 4
                    for x in range(x0, x1):
                        off = base + (x - x0) * 4
                        total += (raw[off + 2] * 77 + raw[off + 1] * 150 + raw[off] * 29) >> 8
                        count += 1
                grid[gy * self.grid_w + gx] = total // count if count else 0
        return bytes(grid)

    def diff(self, hwnd: int) -> ChangeResult:
        grid = self._capture_grid(hwnd)
        previous = self._last.get(hwnd)
        if grid is None:
            return ChangeResult(status="capture_failed")
        if grid and max(grid) <= 2:
            return ChangeResult(status="black_frame")
        self._last[hwnd] = grid
        if previous is None or len(grid) != len(previous):
            return ChangeResult(status="baseline")
        total = 0
        changed_cells = 0
        for a, b in zip(previous, grid):
            delta = abs(a - b)
            total += delta
            if delta >= 24:
                changed_cells += 1
        return ChangeResult(
            status="ok",
            score=total / (len(grid) * 255),
            changed_fraction=changed_cells / len(grid),
        )

    def forget(self, hwnd: int) -> None:
        self._last.pop(hwnd, None)

    def clear(self) -> None:
        self._last.clear()


class AsyncChangeDetector:
    """Run the blocking PrintWindow detector away from the UI/message thread.

    The worker is deliberately a daemon thread. Some third-party windows can keep
    PrintWindow blocked for a long time; a daemon prevents such a target window
    from making LookUp Windows itself impossible to close.
    """

    def __init__(self, detector: ChangeDetector | None = None):
        self._detector = detector or ChangeDetector()
        self._tasks: queue.Queue[tuple[int, tuple[int, ...]] | None] = queue.Queue(maxsize=1)
        self._results: queue.Queue[tuple[int, int, ChangeResult]] = queue.Queue()
        self._state_lock = threading.Lock()
        self._resets: set[int] = set()
        self._clear_requested = False
        self._generation = 0
        self._pending = False
        self._submitted_at = 0.0
        self._closed = False
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
        with self._state_lock:
            if self._closed or self._pending:
                return False
            self._pending = True
            self._submitted_at = time.monotonic()
            generation = self._generation
        try:
            self._tasks.put_nowait((generation, unique))
            return True
        except queue.Full:
            with self._state_lock:
                self._pending = False
            return False

    def poll_results(self) -> list[tuple[int, ChangeResult]]:
        results: list[tuple[int, ChangeResult]] = []
        with self._state_lock:
            generation = self._generation
        while True:
            try:
                result_generation, hwnd, score = self._results.get_nowait()
                if result_generation == generation:
                    results.append((hwnd, score))
            except queue.Empty:
                break
        return results

    def forget(self, hwnd: int) -> None:
        if not hwnd:
            return
        with self._state_lock:
            self._resets.add(int(hwnd))

    def clear(self) -> None:
        with self._state_lock:
            self._clear_requested = True
            self._resets.clear()
            self._generation += 1
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                break

    def busy_for(self) -> float:
        with self._state_lock:
            if not self._pending:
                return 0.0
            return max(0.0, time.monotonic() - self._submitted_at)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._tasks.put_nowait(None)
        except queue.Full:
            pass

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

    def _run(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:
                return
            generation, task = item
            try:
                self._apply_resets()
                for hwnd in task:
                    self._apply_resets()
                    try:
                        result = self._detector.diff(hwnd)
                    except Exception:
                        result = ChangeResult(status="capture_failed")
                    self._results.put((generation, hwnd, result))
            finally:
                with self._state_lock:
                    self._pending = False
