from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable, Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HWND = ctypes.c_void_p
HICON = ctypes.c_void_p
HMENU = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
LRESULT = ctypes.c_ssize_t

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002

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

TRAY_CLASS_NAME = "LookUpWindowsTrayHelper"
TRAY_ID = 1

MENU_ITEM = tuple[int, str, bool]


class TrayIcon:
    _class_registered = False

    def __init__(
        self,
        tooltip: str,
        menu_provider: Callable[[], Iterable[MENU_ITEM | None]],
        on_command: Callable[[int], None],
        on_click: Callable[[], None],
        hicon: int = 0,
    ):
        self.tooltip = tooltip[:127]
        self.menu_provider = menu_provider
        self.on_command = on_command
        self.on_click = on_click
        self._hicon = hicon
        self._wndproc = WNDPROC(self._wnd_proc)
        self.hwnd: int = 0
        self._nid: NOTIFYICONDATAW | None = None
        self._taskbar_created = int(user32.RegisterWindowMessageW("TaskbarCreated") or 0)

    def start(self) -> bool:
        if not TrayIcon._class_registered:
            wndclass = WNDCLASSEXW()
            wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wndclass.lpfnWndProc = self._wndproc
            wndclass.hInstance = kernel32.GetModuleHandleW(None)
            wndclass.lpszClassName = TRAY_CLASS_NAME
            if not user32.RegisterClassExW(ctypes.byref(wndclass)):
                raise OSError(f"RegisterClassExW failed: {ctypes.get_last_error()}")
            TrayIcon._class_registered = True

        hwnd = user32.CreateWindowExW(
            0, TRAY_CLASS_NAME, "WindowPreviewTray", 0, 0, 0, 0, 0, None, None,
            kernel32.GetModuleHandleW(None), None,
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        self.hwnd = int(hwnd)

        ok = self._add_icon()
        if not ok:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
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
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def stop(self) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None
        if self._hicon:
            user32.DestroyIcon(self._hicon)
            self._hicon = 0
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = 0

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            for entry in self.menu_provider() or []:
                if entry is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    item_id, text, checked = entry
                    flags = MF_STRING | (MF_CHECKED if checked else 0)
                    user32.AppendMenuW(menu, flags, item_id, text)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self.hwnd, None
            )
        finally:
            user32.DestroyMenu(menu)
        if command:
            self.on_command(int(command))

    def _wnd_proc(self, hwnd, message, wparam, lparam):
        try:
            if self._taskbar_created and message == self._taskbar_created:
                self._add_icon()
                return 0
            if message == WM_TRAYICON:
                event = int(lparam) & 0xFFFF
                # A single tray click should not unexpectedly open/close the
                # control panel.  The application uses the conventional
                # double-click gesture for its primary tray action.
                if event == WM_LBUTTONDBLCLK:
                    self.on_click()
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
        except Exception:
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)
