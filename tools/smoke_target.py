"""Foreign test window used by ``tools/runtime_smoke.py``.

Creates a real top-level window in its own process so the application has a
foreign HWND to bind, park and restore.  With ``--hang`` the window procedure
sleeps while processing ``WM_SHOWWINDOW``, which reproduces the failure
mode: a synchronous ``ShowWindow``/``SetWindowPos`` against a non-responsive
target blocks the caller.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WS_OVERLAPPEDWINDOW = 0x00CF0000
CW_USEDEFAULT = 0x80000000
SW_SHOW = 5
WM_DESTROY = 0x0002
WM_SHOWWINDOW = 0x0018
WM_WINDOWPOSCHANGING = 0x0046
WM_WINDOWPOSCHANGED = 0x0047
WM_CLOSE = 0x0010
SW_MINIMIZE = 6

user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

# Reported to the smoke so a scenario can suspend this process and reproduce an
# application that stays frozen for good: a suspended thread never pumps messages,
# so every cross-process call to its window blocks indefinitely.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--class-name", default="LUWSmokeTarget")
    parser.add_argument(
        "--hang",
        type=float,
        default=0.0,
        help="seconds the window procedure stalls on the chosen message",
    )
    parser.add_argument(
        "--stall-on",
        choices=("show", "move", "moved", "both"),
        default="show",
        help="message that stalls: WM_SHOWWINDOW (restore), WM_WINDOWPOSCHANGING "
        "(park request) or WM_WINDOWPOSCHANGED (window already parked)",
    )
    parser.add_argument("--minimized", action="store_true", help="start minimized")
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=320)
    args = parser.parse_args()

    hang = max(0.0, float(args.hang))
    stall_show = args.stall_on in {"show", "both"}
    stall_move = args.stall_on in {"move", "both"}
    stall_moved = args.stall_on in {"moved", "both"}

    def wnd_proc(hwnd, message, wparam, lparam):
        if hang and (
            (stall_show and message == WM_SHOWWINDOW)
            or (stall_move and message == WM_WINDOWPOSCHANGING)
            or (stall_moved and message == WM_WINDOWPOSCHANGED)
        ):
            # A genuinely unresponsive target for the stall period.
            time.sleep(hang)
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASS()
    window_class.lpfnWndProc = proc
    window_class.hInstance = instance
    window_class.lpszClassName = args.class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        print(json.dumps({"error": "RegisterClassW failed", "code": ctypes.get_last_error()}), flush=True)
        return 1

    hwnd = user32.CreateWindowExW(
        0,
        args.class_name,
        args.title,
        WS_OVERLAPPEDWINDOW,
        120,
        90,
        args.width,
        args.height,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        print(json.dumps({"error": "CreateWindowExW failed", "code": ctypes.get_last_error()}), flush=True)
        return 1
    user32.ShowWindow(hwnd, SW_SHOW)
    if args.minimized:
        # Start minimized so a restore has to issue SW_RESTORE, which is the
        # synchronous cross-process call the async path avoids.
        user32.ShowWindow(hwnd, SW_MINIMIZE)
    user32.UpdateWindow(hwnd)
    # The window thread id is reported so a scenario can suspend this process and
    # reproduce an application that stays frozen for good (a suspended thread never
    # pumps messages, so every cross-process call to the window blocks).
    print(
        json.dumps(
            {
                "hwnd": int(hwnd),
                "title": args.title,
                "tid": int(kernel32.GetCurrentThreadId()),
            }
        ),
        flush=True,
    )

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
