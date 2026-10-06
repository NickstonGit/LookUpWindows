from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

logger = logging.getLogger(__name__)

HWND = ctypes.c_void_p
HICON = ctypes.c_void_p
HMENU = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
LRESULT = ctypes.c_ssize_t

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_NCDESTROY = 0x0082
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00
NIM_MODIFY = 0x01
NIM_DELETE = 0x02

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIIF_INFO = 0x01

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
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
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.IsWindow.argtypes = [HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.LoadIconW.argtypes = [HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = HICON
user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HWND,
    ctypes.POINTER(wintypes.RECT),
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = HWND
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = HINSTANCE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

TRAY_CLASS_NAME = "LookUpWindowsTrayHelper"
TRAY_ID = 1

MENU_ITEM = tuple[int, str, bool]

# RegisterClassExW stores one process-wide callback for the window class.  The
# callback therefore cannot be a bound method of the first TrayIcon instance.
# Dispatch through HWND instead, mirroring the main UI windows in app.py.
_instances: dict[int, "TrayIcon"] = {}
_instances_lock = threading.RLock()
_class_lock = threading.Lock()
_class_registered = False


def _dispatch_window_proc(hwnd, message, wparam, lparam):
    hwnd_value = int(hwnd or 0)
    with _instances_lock:
        instance = _instances.get(hwnd_value)
    if instance is not None:
        try:
            result = instance._handle_message(hwnd_value, int(message), wparam, lparam)
            if result is not None:
                return int(result)
        except Exception:
            logger.exception("Unhandled tray window message 0x%04X", int(message))
        finally:
            if int(message) == WM_NCDESTROY:
                with _instances_lock:
                    _instances.pop(hwnd_value, None)
                if instance.hwnd == hwnd_value:
                    instance.hwnd = 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


_WINDOW_PROC = WNDPROC(_dispatch_window_proc)


def _ensure_window_class() -> None:
    global _class_registered
    if _class_registered:
        return
    with _class_lock:
        if _class_registered:
            return
        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.lpfnWndProc = _WINDOW_PROC
        wndclass.hInstance = kernel32.GetModuleHandleW(None)
        wndclass.lpszClassName = TRAY_CLASS_NAME
        ctypes.set_last_error(0)
        atom = user32.RegisterClassExW(ctypes.byref(wndclass))
        if not atom:
            error = ctypes.get_last_error()
            if error != ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(error, f"RegisterClassExW({TRAY_CLASS_NAME}) failed")
        _class_registered = True


class TrayIcon:
    def __init__(
        self,
        tooltip: str,
        menu_provider: Callable[[], Iterable[MENU_ITEM | None]],
        on_command: Callable[[int], None],
        on_click: Callable[[], None],
        hicon: int = 0,
        *,
        defer: Callable,
    ):
        self.tooltip = tooltip[:127]
        self.menu_provider = menu_provider
        self.on_command = on_command
        self.on_click = on_click
        self.defer = defer
        self._hicon = hicon
        self.hwnd: int = 0
        self._owner_thread_id = 0
        self._nid: NOTIFYICONDATAW | None = None
        self._taskbar_created = int(user32.RegisterWindowMessageW("TaskbarCreated") or 0)

    def start(self) -> bool:
        _ensure_window_class()
        hwnd = user32.CreateWindowExW(
            0,
            TRAY_CLASS_NAME,
            "LookUpWindowsTrayHelper",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not hwnd:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateWindowExW for tray helper failed")
        self.hwnd = int(hwnd)
        self._owner_thread_id = int(kernel32.GetCurrentThreadId())
        with _instances_lock:
            _instances[self.hwnd] = self

        if not self._add_icon():
            self._destroy_window()
            return False
        return True

    def _add_icon(self) -> bool:
        if not self.hwnd:
            return False
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = TRAY_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon or user32.LoadIconW(None, IDI_APPLICATION)
        nid.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            logger.warning("Shell_NotifyIconW(NIM_ADD) failed: %s", ctypes.get_last_error())
            return False
        self._nid = nid
        return True

    def notify(self, title: str, text: str) -> None:
        if self._nid is None:
            return
        nid = self._nid
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_INFO
        if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            logger.warning("Shell_NotifyIconW(NIM_MODIFY) failed: %s", ctypes.get_last_error())

    def stop(self) -> None:
        if self._nid is not None:
            if not shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid)):
                logger.debug("Shell_NotifyIconW(NIM_DELETE) failed: %s", ctypes.get_last_error())
            self._nid = None
        if self._hicon:
            if not user32.DestroyIcon(self._hicon):
                logger.debug("DestroyIcon failed: %s", ctypes.get_last_error())
            self._hicon = 0
        self._destroy_window()

    def _destroy_window(self) -> None:
        hwnd = int(self.hwnd or 0)
        if not hwnd:
            return
        current_thread = int(kernel32.GetCurrentThreadId())
        if self._owner_thread_id and current_thread != self._owner_thread_id:
            # DestroyWindow is thread-affine.  WM_CLOSE lets the owner thread run
            # DefWindowProc -> DestroyWindow and WM_NCDESTROY cleans our map.
            if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                logger.warning("PostMessageW(WM_CLOSE) failed: %s", ctypes.get_last_error())
            return
        if not user32.DestroyWindow(hwnd):
            logger.warning("DestroyWindow(tray helper) failed: %s", ctypes.get_last_error())
            return
        with _instances_lock:
            _instances.pop(hwnd, None)
        self.hwnd = 0

    def _show_menu(self) -> None:
        if not self.hwnd:
            return
        menu = user32.CreatePopupMenu()
        if not menu:
            logger.warning("CreatePopupMenu failed: %s", ctypes.get_last_error())
            return
        command = 0
        previous_foreground = int(user32.GetForegroundWindow() or 0)
        try:
            for entry in self.menu_provider() or []:
                if entry is None:
                    if not user32.AppendMenuW(menu, MF_SEPARATOR, 0, None):
                        logger.debug("AppendMenuW(separator) failed: %s", ctypes.get_last_error())
                else:
                    item_id, text, checked = entry
                    flags = MF_STRING | (MF_CHECKED if checked else 0)
                    if not user32.AppendMenuW(menu, flags, item_id, text):
                        logger.debug("AppendMenuW(%s) failed: %s", item_id, ctypes.get_last_error())
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                logger.warning("GetCursorPos failed: %s", ctypes.get_last_error())
                return
            if not user32.SetForegroundWindow(self.hwnd):
                logger.debug("SetForegroundWindow(tray helper) was rejected")
            try:
                command = user32.TrackPopupMenu(
                    menu,
                    TPM_RIGHTBUTTON | TPM_RETURNCMD,
                    point.x,
                    point.y,
                    0,
                    self.hwnd,
                    None,
                )
            finally:
                # Required by the notification-area TrackPopupMenu contract so
                # subsequent context menus dismiss and reopen correctly.
                if self.hwnd:
                    user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
                # The helper is a hidden top-level window (intentionally, so it
                # receives TaskbarCreated broadcasts); return focus afterwards.
                if (
                    previous_foreground
                    and previous_foreground != self.hwnd
                    and user32.IsWindow(previous_foreground)
                ):
                    user32.SetForegroundWindow(previous_foreground)
        finally:
            if not user32.DestroyMenu(menu):
                logger.debug("DestroyMenu failed: %s", ctypes.get_last_error())
        if command:
            self.on_command(int(command))

    def _handle_message(self, hwnd: int, message: int, wparam, lparam) -> int | None:
        if self._taskbar_created and message == self._taskbar_created:
            # Explorer was restarted; the notification-area icon must be added
            # again.  Keep this helper top-level rather than HWND_MESSAGE because
            # message-only windows do not receive broadcast messages.
            self._add_icon()
            return 0
        if message == WM_TRAYICON:
            event = int(lparam) & 0xFFFF
            if event == WM_LBUTTONDBLCLK:
                self.on_click()
            elif event == WM_RBUTTONUP:
                self.defer(self._show_menu)
            return 0
        return None
