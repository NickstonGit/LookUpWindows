from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HWND = ctypes.c_void_p
HDC = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
HFONT = ctypes.c_void_p
HMENU = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HCURSOR = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p
HRGN = ctypes.c_void_p
HPEN = ctypes.c_void_p
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

CS_DBLCLKS = 0x0008

WS_POPUP = 0x80000000
WS_CLIPSIBLINGS = 0x04000000

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)

LWA_ALPHA = 0x0002

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_KEYDOWN = 0x0100
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_WINDOWPOSCHANGED = 0x0047
WM_SETCURSOR = 0x0020
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_DESTROY = 0x0002

HTCLIENT = 1
VK_ESCAPE = 0x1B

IDC_ARROW = 32512
IDC_HAND = 32649
IDC_SIZEWE = 32644
IDC_SIZENS = 32645
IDC_SIZENESW = 32643
IDC_SIZENWSE = 32642

FW_NORMAL = 400
FW_BOLD = 700
DEFAULT_CHARSET = 1
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
CLEARTYPE_QUALITY = 5
DEFAULT_PITCH = 0
TRANSPARENT = 1

PS_SOLID = 0
NULL_BRUSH = 5

DT_LEFT = 0x0000
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800
DT_WORDBREAK = 0x0010
DT_END_ELLIPSIS = 0x8000

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
SMTO_ABORTIFHUNG = 0x0002
MB_OK = 0x00000000
MB_ICONWARNING = 0x00000030


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", wintypes.BYTE * 32),
    ]


user32.DefWindowProcW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HWND,
    ctypes.c_void_p,
    HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = HWND
user32.DestroyWindow.argtypes = [HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.InvalidateRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
user32.InvalidateRect.restype = wintypes.BOOL
user32.BeginPaint.argtypes = [HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = HDC
user32.EndPaint.argtypes = [HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.SetCapture.argtypes = [HWND]
user32.SetCapture.restype = HWND
user32.ReleaseCapture.restype = wintypes.BOOL
user32.LoadCursorW.argtypes = [HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = HCURSOR
user32.SetCursor.argtypes = [HCURSOR]
user32.SetCursor.restype = HCURSOR
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, ctypes.POINTER(wintypes.RECT)]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_window_long_ptr = user32.GetWindowLongPtrW
    _set_window_long_ptr = user32.SetWindowLongPtrW
else:
    # Get/SetWindowLongPtr are C macros that map to Get/SetWindowLong on 32-bit Windows.
    _get_window_long_ptr = user32.GetWindowLongW
    _set_window_long_ptr = user32.SetWindowLongW
_get_window_long_ptr.argtypes = [HWND, ctypes.c_int]
_get_window_long_ptr.restype = ctypes.c_ssize_t
_set_window_long_ptr.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_window_long_ptr.restype = ctypes.c_ssize_t
user32.RegisterHotKey.argtypes = [HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = LRESULT
user32.MessageBoxW.argtypes = [HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.DeleteDC.argtypes = [HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = HBRUSH
gdi32.CreateFontW.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.LPCWSTR,
]
gdi32.CreateFontW.restype = HFONT
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.CreatePen.restype = HPEN
gdi32.RoundRect.argtypes = [
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
]
gdi32.RoundRect.restype = wintypes.BOOL
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = HGDIOBJ
gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
]
gdi32.CreateRoundRectRgn.restype = HRGN
user32.SetWindowRgn.argtypes = [HWND, HRGN, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.SetTextColor.argtypes = [HDC, wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF
gdi32.SetBkMode.argtypes = [HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int

user32.FillRect.argtypes = [HDC, ctypes.POINTER(wintypes.RECT), HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.FrameRect.argtypes = [HDC, ctypes.POINTER(wintypes.RECT), HBRUSH]
user32.FrameRect.restype = wintypes.BOOL
user32.DrawTextW.argtypes = [HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = HINSTANCE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = HWND
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT

ERROR_ALREADY_EXISTS = 183
WAKE_MESSAGE_NAME = "LookUpWindows-Wake"
QUIT_MESSAGE_NAME = "LookUpWindows-Quit"


_single_instance_mutex: int = 0
_wake_message_id: int = 0
_quit_message_id: int = 0


def register_wake_message() -> int:
    global _wake_message_id
    if not _wake_message_id:
        _wake_message_id = int(user32.RegisterWindowMessageW(WAKE_MESSAGE_NAME) or 0)
    return _wake_message_id


def register_quit_message() -> int:
    global _quit_message_id
    if not _quit_message_id:
        _quit_message_id = int(user32.RegisterWindowMessageW(QUIT_MESSAGE_NAME) or 0)
    return _quit_message_id


def request_graceful_quit(timeout_ms: int = 1500) -> bool:
    """Ask a running instance to shut down through its normal restore path.

    Force-killing a running LookUp can leave a parked foreign window off-screen,
    so tooling (build scripts, smoke tests, installers) must use this protocol
    instead.  Returns False when no instance answered in time.
    """
    quit_message = register_quit_message()
    hwnd = user32.FindWindowW("WPCtrl", None)
    if not hwnd or not quit_message:
        return False
    result = ctypes.c_size_t(0)
    sent = user32.SendMessageTimeoutW(
        hwnd, quit_message, 0, 0, SMTO_ABORTIFHUNG, max(100, int(timeout_ms)), ctypes.byref(result)
    )
    return bool(sent) and bool(result.value)


def acquire_single_instance(name: str) -> bool:
    global _single_instance_mutex
    _single_instance_mutex = int(kernel32.CreateMutexW(None, False, name) or 0)
    if not _single_instance_mutex:
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def wake_first_instance(timeout_ms: int = 1500) -> bool:
    wake = register_wake_message()
    hwnd = user32.FindWindowW("WPCtrl", None)
    if not hwnd or not wake:
        return False
    result = ctypes.c_size_t(0)
    sent = user32.SendMessageTimeoutW(
        hwnd, wake, 0, 0, SMTO_ABORTIFHUNG, max(100, int(timeout_ms)), ctypes.byref(result)
    )
    return bool(sent) and bool(result.value)


def message_box(text: str, title: str = "LookUp Windows") -> None:
    user32.MessageBoxW(None, text, title, MB_OK | MB_ICONWARNING)

_brush_cache: dict[int, int] = {}
_pen_cache: dict[tuple[int, int], int] = {}
_font_cache: dict[tuple[int, bool], int] = {}
_classes: dict[str, WNDPROC] = {}


def rgb(r: int, g: int, b: int) -> int:
    return (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)


def brush(color: int) -> int:
    cached = _brush_cache.get(color)
    if cached is not None:
        return cached
    handle = int(gdi32.CreateSolidBrush(color) or 0)
    if handle:
        _brush_cache[color] = handle
    return handle


def pen(color: int, width: int = 1) -> int:
    key = (color, max(1, int(width)))
    cached = _pen_cache.get(key)
    if cached is not None:
        return cached
    handle = int(gdi32.CreatePen(PS_SOLID, max(1, int(width)), color) or 0)
    if handle:
        _pen_cache[key] = handle
    return handle


def round_rect_outline(
    hdc: int,
    rect: tuple[int, int, int, int],
    color: int,
    pen_width: int = 1,
    radius: int = 16,
) -> None:
    left, top, right, bottom = rect
    if right - left <= 1 or bottom - top <= 1:
        return
    radius = max(1, int(radius))
    old_pen = gdi32.SelectObject(hdc, pen(color, pen_width))
    old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
    gdi32.RoundRect(hdc, left, top, right, bottom, radius * 2, radius * 2)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.SelectObject(hdc, old_pen)


def set_round_region(hwnd: int, width: int, height: int, radius: int) -> None:
    if width <= 0 or height <= 0 or not hwnd:
        return
    radius = max(1, int(radius))
    region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
    if not region:
        return
    if not user32.SetWindowRgn(hwnd, region, True):
        gdi32.DeleteObject(region)


def font(size_px: int, bold: bool = False) -> int:
    key = (size_px, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    handle = int(
        gdi32.CreateFontW(
            -size_px,
            0,
            0,
            0,
            FW_BOLD if bold else FW_NORMAL,
            0,
            0,
            0,
            DEFAULT_CHARSET,
            OUT_DEFAULT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY,
            DEFAULT_PITCH,
            "Segoe UI",
        )
        or 0
    )
    if handle:
        _font_cache[key] = handle
    return handle


def fill_rect(hdc: int, rect: tuple[int, int, int, int], color: int) -> None:
    user32.FillRect(hdc, ctypes.byref(wintypes.RECT(*rect)), brush(color))


def frame_rect(hdc: int, rect: tuple[int, int, int, int], color: int) -> None:
    user32.FrameRect(hdc, ctypes.byref(wintypes.RECT(*rect)), brush(color))


def draw_text(hdc: int, text: str, rect: tuple[int, int, int, int], color: int, hfont: int, flags: int) -> None:
    old_font = gdi32.SelectObject(hdc, hfont)
    gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, color)
    user32.DrawTextW(hdc, text, -1, ctypes.byref(wintypes.RECT(*rect)), flags)
    gdi32.SelectObject(hdc, old_font)


class _Paint:
    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.struct = PAINTSTRUCT()

    def __enter__(self) -> int:
        self.hdc = int(user32.BeginPaint(self.hwnd, ctypes.byref(self.struct)) or 0)
        return self.hdc

    def __exit__(self, *exc) -> None:
        user32.EndPaint(self.hwnd, ctypes.byref(self.struct))


def paint(hwnd: int) -> _Paint:
    return _Paint(hwnd)


def get_client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return (int(point.x), int(point.y))


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_window_x(hwnd: int) -> int:
    return get_window_rect(hwnd)[0]


def get_window_y(hwnd: int) -> int:
    return get_window_rect(hwnd)[1]


def get_window_width(hwnd: int) -> int:
    left, _top, right, _bottom = get_window_rect(hwnd)
    return right - left


def get_cursor_client_pos(hwnd: int) -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    return (int(point.x), int(point.y))


def register_window_class(name: str, proc: WNDPROC, background: int, dblclk: bool = True) -> bool:
    if name in _classes:
        return True
    wndclass = WNDCLASSEXW()
    wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wndclass.style = CS_DBLCLKS if dblclk else 0
    wndclass.lpfnWndProc = proc
    wndclass.hInstance = kernel32.GetModuleHandleW(None)
    wndclass.hCursor = user32.LoadCursorW(None, IDC_ARROW)
    wndclass.hbrBackground = brush(background)
    wndclass.lpszClassName = name
    if not user32.RegisterClassExW(ctypes.byref(wndclass)):
        return False
    _classes[name] = proc
    return True


def create_window(
    class_name: str,
    ex_style: int,
    style: int,
    x: int,
    y: int,
    width: int,
    height: int,
    owner: int = 0,
) -> int:
    hwnd = user32.CreateWindowExW(
        ex_style,
        class_name,
        None,
        style,
        x,
        y,
        width,
        height,
        owner,
        None,
        kernel32.GetModuleHandleW(None),
        None,
    )
    return int(hwnd or 0)


def show_window(hwnd: int, how: int = SW_SHOWNOACTIVATE) -> None:
    user32.ShowWindow(hwnd, how)


def hide_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_HIDE)


def is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    user32.SetWindowPos(hwnd, None, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)


def set_topmost(hwnd: int, topmost: bool = True) -> None:
    user32.SetWindowPos(
        hwnd, HWND_TOPMOST if topmost else HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def set_alpha(hwnd: int, alpha: float) -> None:
    user32.SetLayeredWindowAttributes(hwnd, 0, int(max(0.0, min(1.0, alpha)) * 255), LWA_ALPHA)


def set_click_through(hwnd: int, enabled: bool) -> None:
    style = int(_get_window_long_ptr(hwnd, GWL_EXSTYLE))
    desired = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
    if desired != style:
        _set_window_long_ptr(hwnd, GWL_EXSTYLE, desired)
        user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )


def register_hotkey(hwnd: int, hotkey_id: int, modifiers: int, vk: int) -> bool:
    return bool(user32.RegisterHotKey(hwnd, hotkey_id, modifiers, vk))


def unregister_hotkey(hwnd: int, hotkey_id: int) -> None:
    user32.UnregisterHotKey(hwnd, hotkey_id)


def set_timer(hwnd: int, timer_id: int, ms: int) -> bool:
    return bool(user32.SetTimer(hwnd, timer_id, ms, None))


def kill_timer(hwnd: int, timer_id: int) -> None:
    user32.KillTimer(hwnd, timer_id)


def invalidate(hwnd: int) -> None:
    user32.InvalidateRect(hwnd, None, False)


def destroy_window(hwnd: int) -> None:
    user32.DestroyWindow(hwnd)


def set_hand_cursor() -> None:
    user32.SetCursor(user32.LoadCursorW(None, IDC_HAND))


def set_size_cursor() -> None:
    user32.SetCursor(user32.LoadCursorW(None, IDC_SIZEWE))


def set_resize_cursor(zone: str = "bottom_right") -> None:
    if zone in ("left", "right"):
        cursor_id = IDC_SIZEWE
    elif zone in ("top", "bottom"):
        cursor_id = IDC_SIZENS
    elif zone in ("top_right", "bottom_left"):
        cursor_id = IDC_SIZENESW
    else:
        cursor_id = IDC_SIZENWSE
    user32.SetCursor(user32.LoadCursorW(None, cursor_id))


def track_popup_menu(items, hwnd: int) -> int:
    menu = user32.CreatePopupMenu()
    if not menu:
        return 0
    command = 0
    try:
        for entry in items or []:
            if entry is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                item_id, text, checked = entry
                flags = MF_STRING | (MF_CHECKED if checked else 0)
                user32.AppendMenuW(menu, flags, item_id, text)
        point_x, point_y = get_cursor_pos()
        user32.SetForegroundWindow(hwnd)
        try:
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point_x, point_y, 0, hwnd, None
            )
        finally:
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
    finally:
        user32.DestroyMenu(menu)
    return int(command)


def xy_from_lparam(lparam) -> tuple[int, int]:
    return (ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)


def enable_dpi_awareness() -> None:
    try:
        setter = ctypes.windll.user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        if setter(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        setter = ctypes.windll.shcore.SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        if setter(2) == 0:  # S_OK; PROCESS_PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        legacy = ctypes.windll.user32.SetProcessDPIAware
        legacy.restype = wintypes.BOOL
        legacy()
    except (AttributeError, OSError, ValueError):
        pass
