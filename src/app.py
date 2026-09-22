from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import tkinter as tk
from tkinter import simpledialog

try:
    import winsound
except ImportError:
    winsound = None

import winapi
import winui
import icon as icon_module
from config import APP_VERSION, AppConfig, Autostart, ConfigService, CropRect, TrackedWindow, app_dir, background_mode
from dwm import Thumbnail
from trayicon import TrayIcon
from winapi import AsyncChangeDetector, WindowCandidate, WindowFinder
from winui import (
    DT_CENTER,
    DT_END_ELLIPSIS,
    DT_NOPREFIX,
    DT_SINGLELINE,
    DT_VCENTER,
    DT_WORDBREAK,
    SW_SHOWNOACTIVATE,
    WM_CLOSE,
    WM_ERASEBKGND,
    WM_KEYDOWN,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_PAINT,
    WM_RBUTTONUP,
    WM_SETCURSOR,
    WM_TIMER,
    WM_WINDOWPOSCHANGED,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_POPUP,
    WS_CLIPSIBLINGS,
)

APP_NAME = "LookUp Windows"

BG = winui.rgb(0x1F, 0x20, 0x23)
CARD_BG = winui.rgb(0x23, 0x24, 0x29)
THUMB_BG = winui.rgb(0x15, 0x16, 0x1A)
HEADER_BG = winui.rgb(0x23, 0x24, 0x29)
FG = winui.rgb(0xDF, 0xE0, 0xE3)
DIM = winui.rgb(0x8A, 0x8B, 0x91)
ACCENT = winui.rgb(0x4F, 0x8C, 0xFF)
OK = winui.rgb(0x3D, 0xDC, 0x84)
WARN = winui.rgb(0xFF, 0x9F, 0x43)
BORDER = winui.rgb(0x33, 0x34, 0x3A)
DOT_IDLE = winui.rgb(0x57, 0x58, 0x5C)

BIG_BORDER = winui.rgb(0x5C, 0xD3, 0xFF)
BIG_BORDER_W = 2
BIG_RADIUS = 16
BIG_PAD = 8

CTRL_W = 340
CTRL_H = 42
BTN_W = 26
CARD_W = 280
CARD_MIN_W = 180
CARD_HEADER_H = 26
CARD_MIN_H = 86
CARD_BORDER_W = 2
CARD_RADIUS = 14
CARD_PAD = 6
HOST_MIN_H = 50
RESIZE_EDGE = 8
RESIZE_CORNER = 14
SNAP_PX = 14
REFRESH_MS = 2000

TIMER_REFRESH = 1
TIMER_CHANGE = 2
TIMER_SAVE_CONFIG = 3
TIMER_REFRESH_SOON = 4
TIMER_STARTUP = 5
TIMER_PARKED_WATCH = 6

CMD_SHOW = 1
CMD_HIDE = 2
CMD_ADD = 3
CMD_SETTINGS = 4
CMD_AUTOSTART = 5
CMD_QUIT = 6
CMD_PROFILES = 7
CMD_DISABLE_CLICKTHROUGH = 8
CMD_TOGGLE_CARDS = 9

MENU_OPEN = 1
MENU_BIG = 2
MENU_CROP = 3
MENU_FILTER = 4
MENU_REFRESH = 5
MENU_REMOVE = 6
MENU_COLLAPSE = 7
MENU_CLICKTHROUGH = 8
MENU_SIZE_SMALL = 9
MENU_SIZE_MEDIUM = 10
MENU_SIZE_LARGE = 11
MENU_DETECT_CHANGES = 12

HOTKEY_ADD_FOREGROUND = 1
HOTKEY_TOGGLE_PANEL = 2
HOTKEY_SELECTOR = 3
HOTKEY_DISABLE_CLICKTHROUGH = 4

_handlers: dict[int, object] = {}
_procs: dict[str, winui.WNDPROC] = {}


def _dispatch(class_name: str):
    proc = _procs.get(class_name)
    if proc is not None:
        return proc

    def wnd_proc(hwnd, message, wparam, lparam):
        handler = _handlers.get(int(hwnd or 0))
        if handler is not None:
            result = handler.on_message(message, wparam, lparam)
            if result is not None:
                return result
        return winui.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    proc = winui.WNDPROC(wnd_proc)
    _procs[class_name] = proc
    return proc


def _register_classes() -> None:
    winui.register_window_class("WPCtrl", _dispatch("WPCtrl"), BG, dblclk=False)
    winui.register_window_class("WPCard", _dispatch("WPCard"), CARD_BG)
    winui.register_window_class("WPBig", _dispatch("WPBig"), 0)


class CardWnd:
    def __init__(self, app: "App", tracked: TrackedWindow, x: int, y: int):
        self.app = app
        self.tracked = tracked
        self.hwnd = 0
        self.thumb: Thumbnail | None = None
        self.src_hwnd = 0
        self.candidate: WindowCandidate | None = None
        self._source_size: tuple[int, int] = (0, 0)
        self._minimized = False
        self._parked_source: winapi.ParkedWindowState | None = None
        # A parked source may remain the foreground HWND for a short time because
        # PiP cards use WS_EX_NOACTIVATE.  Only treat a later foreground transition
        # back to the source as an explicit taskbar/Alt+Tab request to restore it.
        self._parked_seen_not_foreground = False
        self._source_action_pending: str | None = None
        self._source_action_id = 0
        self._changed = False
        self._active = False
        self._title = tracked.display_name()
        self._placeholder = "Окно недоступно\nОжидание..."
        self._drag: tuple[int, int, int, int] | None = None
        self._resize: tuple[str, int, int, int, int, int, int] | None = None
        self._ever_bound = False
        self._was_missing = False
        self._capture_issue = False
        self._last_active_at = time.monotonic()
        self._shape_size: tuple[int, int] = (0, 0)

        wa_left, wa_top, wa_right, wa_bottom = winapi.work_area_for_point(x, y)
        max_initial_width = max(CARD_MIN_W, wa_right - wa_left)
        initial_width = max(CARD_MIN_W, min(int(tracked.width or CARD_W), max_initial_width))
        initial_height = CARD_HEADER_H + 4 if tracked.collapsed else 120
        hwnd = winui.create_window(
            "WPCard",
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            WS_POPUP | WS_CLIPSIBLINGS,
            x,
            y,
            initial_width,
            initial_height,
            owner=0,
        )
        if not hwnd:
            raise OSError("card window creation failed")
        self.hwnd = hwnd
        _handlers[hwnd] = self
        winui.set_alpha(self.hwnd, app.config.opacity)
        winui.set_click_through(self.hwnd, tracked.click_through)
        self._apply_shape()

    def on_message(self, message, wparam, lparam):
        if message == WM_PAINT:
            self._paint()
            return 0
        if message == WM_ERASEBKGND:
            return 0
        if message == WM_LBUTTONDOWN:
            self._on_down(lparam)
            return 0
        if message == WM_MOUSEMOVE:
            self._on_move(lparam)
            return 0
        if message == WM_LBUTTONUP:
            if self._drag is not None or self._resize is not None:
                self._end_drag()
                return 0
            self._on_left_up(lparam)
            return 0
        if message == WM_LBUTTONDBLCLK:
            x, y = winui.xy_from_lparam(lparam)
            if y < CARD_HEADER_H:
                self.app.defer(self.toggle_collapsed)
            elif self._in_thumb(x, y):
                self.app.defer(self.app.open_fullscreen, self)
            return 0
        if message == WM_RBUTTONUP:
            self.app.defer(self.app.card_menu, self)
            return 0
        if message == WM_SETCURSOR:
            if int(lparam & 0xFFFF) == winui.HTCLIENT:
                x, y = winui.get_cursor_client_pos(self.hwnd)
                resize_zone = self._resize_zone(x, y)
                if resize_zone is not None:
                    winui.set_resize_cursor(resize_zone)
                    return 1
                if self._in_thumb(x, y):
                    winui.set_hand_cursor()
                    return 1
        if message == WM_WINDOWPOSCHANGED:
            self._apply_shape()
            self.update_thumb_geometry()
        if message == winui.WM_DESTROY:
            _handlers.pop(self.hwnd, None)
        return None

    def _size(self) -> tuple[int, int]:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        return (right - left, bottom - top)

    def _apply_shape(self) -> None:
        width, height = self._size()
        if width > 0 and height > 0 and (width, height) != self._shape_size:
            winui.set_round_region(self.hwnd, width, height, CARD_RADIUS)
            self._shape_size = (width, height)

    def _in_thumb(self, x: int, y: int) -> bool:
        if self.tracked.collapsed:
            return False
        width, _height = self._size()
        return y >= CARD_HEADER_H and CARD_PAD <= x <= width - CARD_PAD

    def _resize_zone(self, x: int, y: int) -> str | None:
        """Return one of the eight proportional-resize zones for a client point."""
        if self.tracked.collapsed or self.tracked.click_through:
            return None
        width, height = self._size()
        if width <= 0 or height <= 0:
            return None

        # Keep the fullscreen/close header buttons easy to click. The right edge
        # remains resizable below the button strip.
        in_header_buttons = y < CARD_HEADER_H and x >= width - 48

        left = x <= RESIZE_EDGE
        right = x >= width - RESIZE_EDGE and not in_header_buttons
        top = y <= RESIZE_EDGE and not in_header_buttons
        bottom = y >= height - RESIZE_EDGE

        # Corners get a slightly larger target than plain edges.
        near_left = x <= RESIZE_CORNER
        near_right = x >= width - RESIZE_CORNER and not in_header_buttons
        near_top = y <= RESIZE_CORNER and not in_header_buttons
        near_bottom = y >= height - RESIZE_CORNER
        if near_left and near_top:
            return "top_left"
        if near_right and near_top:
            return "top_right"
        if near_left and near_bottom:
            return "bottom_left"
        if near_right and near_bottom:
            return "bottom_right"
        if left:
            return "left"
        if right:
            return "right"
        if top:
            return "top"
        if bottom:
            return "bottom"
        return None

    def _paint(self) -> None:
        width, height = self._size()
        with winui.paint(self.hwnd) as hdc:
            winui.fill_rect(hdc, (0, 0, width, height), CARD_BG)
            if not self.tracked.collapsed:
                winui.fill_rect(
                    hdc,
                    (CARD_PAD, CARD_HEADER_H, width - CARD_PAD, height - CARD_PAD),
                    THUMB_BG,
                )
            dot = ACCENT if self._source_action_pending else (WARN if (self._changed or self._capture_issue) else (OK if self._active else DOT_IDLE))
            winui.draw_text(
                hdc, "●", (2, 2, 16, CARD_HEADER_H), dot, winui.font(11),
                DT_CENTER | DT_SINGLELINE | DT_VCENTER,
            )
            title_right = width - 48
            if self._ever_bound and not self._active:
                age_minutes = int(max(0.0, time.monotonic() - self._last_active_at) // 60)
                if age_minutes >= 1:
                    title_right = width - 82
                    winui.draw_text(
                        hdc, f"{age_minutes}м", (width - 82, 0, width - 46, CARD_HEADER_H), DIM,
                        winui.font(8), DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                    )
            display_title = self._title
            if self._source_action_pending == "park":
                display_title = f"Скрываю… · {display_title}"
            elif self._source_action_pending == "restore":
                display_title = f"Возвращаю… · {display_title}"
            winui.draw_text(
                hdc, display_title, (18, 0, title_right, CARD_HEADER_H), FG, winui.font(12),
                DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
            )
            winui.draw_text(
                hdc, "⛶", (width - 44, 2, width - 24, 22), DIM, winui.font(11),
                DT_CENTER | DT_SINGLELINE | DT_VCENTER,
            )
            winui.draw_text(
                hdc, "✕", (width - 22, 2, width - 2, 22), DIM, winui.font(11),
                DT_CENTER | DT_SINGLELINE | DT_VCENTER,
            )
            if self._placeholder and not self.tracked.collapsed:
                winui.draw_text(
                    hdc, self._placeholder, (CARD_PAD, CARD_HEADER_H, width - CARD_PAD, height - CARD_PAD), DIM,
                    winui.font(11), DT_CENTER | DT_VCENTER | DT_WORDBREAK,
                )
            winui.round_rect_outline(
                hdc,
                (1, 1, width - 1, height - 1),
                ACCENT,
                CARD_BORDER_W,
                CARD_RADIUS,
            )

    def _on_down(self, lparam) -> None:
        x, y = winui.xy_from_lparam(lparam)
        width, height = self._size()
        cursor_x, cursor_y = winui.get_cursor_pos()
        resize_zone = self._resize_zone(x, y)
        if resize_zone is not None:
            left, top, right, bottom = winui.get_window_rect(self.hwnd)
            self._resize = (resize_zone, cursor_x, cursor_y, left, top, right, bottom)
            winui.user32.SetCapture(self.hwnd)
            return
        if y >= CARD_HEADER_H or width - 44 <= x:
            return
        left, top, _right, _bottom = winui.get_window_rect(self.hwnd)
        self._drag = (cursor_x - left, cursor_y - top, cursor_x, cursor_y)
        winui.user32.SetCapture(self.hwnd)

    def _on_move(self, _lparam) -> None:
        cursor_x, cursor_y = winui.get_cursor_pos()
        if self._resize is not None:
            zone, start_x, start_y, start_left, start_top, start_right, start_bottom = self._resize
            start_width = start_right - start_left
            start_height = start_bottom - start_top
            delta_x = cursor_x - start_x
            delta_y = cursor_y - start_y

            horizontal_width = start_width
            if "left" in zone:
                horizontal_width = start_width - delta_x
            elif "right" in zone:
                horizontal_width = start_width + delta_x

            vertical_height = start_height
            if "top" in zone:
                vertical_height = start_height - delta_y
            elif "bottom" in zone:
                vertical_height = start_height + delta_y
            vertical_width = self.width_for_height(vertical_height)

            if zone in ("left", "right"):
                requested_width = horizontal_width
            elif zone in ("top", "bottom"):
                requested_width = vertical_width
            else:
                # At a corner, whichever axis moved farther from the start size
                # drives the proportional resize. This makes both horizontal and
                # vertical corner drags feel direct instead of lagging behind.
                if abs(horizontal_width - start_width) >= abs(vertical_width - start_width):
                    requested_width = horizontal_width
                else:
                    requested_width = vertical_width

            wa_left, wa_top, wa_right, wa_bottom = winapi.work_area_for_point(
                (start_left + start_right) // 2, (start_top + start_bottom) // 2
            )
            max_width = self._max_width_for_area(wa_left, wa_top, wa_right, wa_bottom)
            width = max(CARD_MIN_W, min(int(requested_width), max_width))
            height = self.desired_height(width)

            center_x = (start_left + start_right) // 2
            center_y = (start_top + start_bottom) // 2
            if "left" in zone:
                left = start_right - width
            elif "right" in zone:
                left = start_left
            else:
                left = center_x - width // 2

            if "top" in zone:
                top = start_bottom - height
            elif "bottom" in zone:
                top = start_top
            else:
                top = center_y - height // 2

            # Keep the whole proportional card inside its current monitor.
            left = max(wa_left, min(wa_right - width, left))
            top = max(wa_top, min(wa_bottom - height, top))
            winui.move_window(self.hwnd, left, top, width, height)
            self._apply_shape()
            self.update_thumb_geometry()
            return
        if self._drag is None:
            return
        offset_x, offset_y, start_x, start_y = self._drag
        if abs(cursor_x - start_x) > 2 or abs(cursor_y - start_y) > 2:
            width, height = self._size()
            winui.move_window(self.hwnd, cursor_x - offset_x, cursor_y - offset_y, width, height)

    def _end_drag(self) -> None:
        self._drag = None
        self._resize = None
        winui.user32.ReleaseCapture()
        left, top, right, bottom = winui.get_window_rect(self.hwnd)
        width = right - left
        height = bottom - top
        wa_left, wa_top, wa_right, wa_bottom = winapi.work_area_for_point(left, top)
        if abs(left - wa_left) <= SNAP_PX:
            left = wa_left
        if abs((left + width) - wa_right) <= SNAP_PX:
            left = wa_right - width
        if abs(top - wa_top) <= SNAP_PX:
            top = wa_top
        if abs((top + height) - wa_bottom) <= SNAP_PX:
            top = wa_bottom - height
        left = max(wa_left, min(wa_right - width, left))
        top = max(wa_top, min(wa_bottom - height, top))
        winui.move_window(self.hwnd, left, top, width, height)
        changed = self.tracked.x != left or self.tracked.y != top or self.tracked.width != width
        self.tracked.x = left
        self.tracked.y = top
        self.tracked.width = width
        if changed:
            self.app.schedule_save_config()

    def _on_left_up(self, lparam) -> None:
        x, y = winui.xy_from_lparam(lparam)
        width, _height = self._size()
        if width - 22 <= x <= width - 2 and 2 <= y <= 22:
            self.app.defer(self.app.remove_card, self)
        elif width - 44 <= x <= width - 24 and 2 <= y <= 22:
            self.app.defer(self.app.open_fullscreen, self)
        elif self._in_thumb(x, y):
            self.app.defer(self.app.toggle_card_source, self)

    def set_candidate(self, candidate: WindowCandidate | None, foreground_hwnd: int) -> None:
        previous_source_size = self._source_size
        self.candidate = candidate
        if candidate is None:
            self._source_action_id += 1
            self._source_action_pending = None
            if self._parked_source is not None and self.src_hwnd and winapi.is_window(self.src_hwnd):
                # Never orphan a still-running application outside the virtual desktop
                # if matching/revalidation temporarily fails.
                self.app.restore_parked_source(self, activate=False)
            if self._ever_bound:
                self._was_missing = True
            self.detach()
            self._minimized = False
            self._parked_source = None
            self._parked_seen_not_foreground = False
            self._active = False
            self._capture_issue = False
            self._title = self.tracked.display_name()
            self._placeholder = "Окно недоступно\nОжидание..."
            self._invalidate()
            return

        self._title = candidate.title or self.tracked.display_name()
        source_changed = self.src_hwnd != candidate.hwnd
        if source_changed:
            self._source_action_id += 1
            self._source_action_pending = None
            if self._parked_source is not None and self.src_hwnd:
                self.app.restore_parked_source(self, activate=False)
            self._parked_source = None
            self._parked_seen_not_foreground = False
            if self.src_hwnd:
                self.app.detector.forget(self.src_hwnd)
                self.app._capture_failures.pop(self.src_hwnd, None)
                self.app._capture_notified.discard(self.src_hwnd)
                self.app._detector_quiet.pop(self.src_hwnd, None)
                self.app._detector_next_due.pop(self.src_hwnd, None)
            if self.app.big is not None and self.app.big.card is self:
                self.app.close_fullscreen()
            self.detach()
            self._attach(candidate.hwnd)
        returned = self._was_missing and self._ever_bound
        self._ever_bound = True
        self._was_missing = False
        if returned:
            self.app.on_window_returned(self)
        self._minimized = candidate.info.minimized
        self._active = candidate.hwnd == foreground_hwnd and self._parked_source is None
        if self._active:
            self._last_active_at = time.monotonic()

        if self.thumb is not None:
            size = self.thumb.source_size()
            if size[0] > 0 and size[1] > 0:
                self._source_size = size

        if self._source_size != previous_source_size and self._source_size != (0, 0):
            self.apply_size()

        if self._minimized:
            self._placeholder = "Окно свернуто"
        elif self.thumb is None:
            self._placeholder = "Не удалось создать превью (DWM)"
        else:
            self._placeholder = ""
        self.update_thumb_geometry()
        self._invalidate()

    def _attach(self, source_hwnd: int) -> None:
        try:
            self.thumb = Thumbnail(self.hwnd, source_hwnd)
        except (OSError, ValueError):
            self.thumb = None
            return
        self.src_hwnd = source_hwnd
        self._source_size = self.thumb.source_size()

    def detach(self) -> None:
        if self.thumb is not None:
            self.thumb.close()
        self.thumb = None
        self.src_hwnd = 0
        self._source_size = (0, 0)

    def update_thumb_geometry(self) -> None:
        if self.thumb is None:
            return
        if self.tracked.collapsed:
            self.thumb.update(dest_rect=(0, 0, 1, 1), visible=False)
            return
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        width = right - left - CARD_PAD * 2
        height = bottom - top - CARD_HEADER_H - CARD_PAD
        if width <= 1 or height <= 1:
            return

        crop = self.tracked.crop
        if crop is not None and self._source_size[0] > 0 and self._source_size[1] > 0:
            crop = crop.clamped(*self._source_size)
        if crop is not None and crop.is_valid():
            source_w, source_h = crop.width, crop.height
            source_rect: tuple[int, int, int, int] | bool = crop.as_rect()
        elif self._source_size[0] > 0 and self._source_size[1] > 0:
            source_w, source_h = self._source_size
            source_rect = (0, 0, source_w, source_h)
        elif self.candidate is not None and not self._minimized:
            cl, ct, cr, cb = self.candidate.info.rect
            source_w, source_h = cr - cl, cb - ct
            source_rect = False
        else:
            return

        if source_w <= 0 or source_h <= 0:
            return

        scale = min(width / source_w, height / source_h)
        dest_w = max(1, int(source_w * scale))
        dest_h = max(1, int(source_h * scale))
        dest_left = CARD_PAD + (width - dest_w) // 2
        dest_top = CARD_HEADER_H + (height - dest_h) // 2
        self.thumb.update(
            dest_rect=(dest_left, dest_top, dest_left + dest_w, dest_top + dest_h),
            source_rect=source_rect,
            visible=not self._minimized,
        )

    def desired_height(self, width: int | None = None) -> int:
        if self.tracked.collapsed:
            return CARD_HEADER_H + 4
        width = width or self._size()[0] or self.tracked.width or CARD_W
        source_w, source_h = self._effective_size()
        host_w = max(40, width - CARD_PAD * 2)
        host_h = int(host_w * source_h / source_w) if source_w > 0 else 120
        host_h = max(HOST_MIN_H, host_h)
        return host_h + CARD_HEADER_H + CARD_PAD * 2

    def width_for_height(self, height: int) -> int:
        """Inverse of desired_height() used when dragging top/bottom edges."""
        if self.tracked.collapsed:
            return self._size()[0] or self.tracked.width or CARD_W
        source_w, source_h = self._effective_size()
        chrome_h = CARD_HEADER_H + CARD_PAD * 2
        host_h = max(HOST_MIN_H, int(height) - chrome_h)
        if source_h <= 0:
            return self._size()[0] or self.tracked.width or CARD_W
        host_w = int(round(host_h * source_w / source_h))
        return max(CARD_MIN_W, host_w + CARD_PAD * 2)

    def _max_width_for_area(self, left: int, top: int, right: int, bottom: int) -> int:
        """Largest proportional card width that fits the monitor work area."""
        area_w = max(CARD_MIN_W, right - left)
        area_h = max(self.desired_height(CARD_MIN_W), bottom - top)
        lo = CARD_MIN_W
        hi = area_w
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.desired_height(mid) <= area_h:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def apply_size(self, width: int | None = None) -> None:
        left, top, right, _bottom = winui.get_window_rect(self.hwnd)
        current_width = right - left
        wa_left, wa_top, wa_right, wa_bottom = winapi.work_area_for_point(left, top)
        monitor_width = max(CARD_MIN_W, wa_right - wa_left)
        monitor_height = max(CARD_HEADER_H + 4, wa_bottom - wa_top)
        width = max(CARD_MIN_W, min(int(width or self.tracked.width or current_width or CARD_W), monitor_width))
        height = min(self.desired_height(width), monitor_height)
        left = max(wa_left, min(wa_right - width, left))
        top = max(wa_top, min(wa_bottom - height, top))
        self.tracked.width = width
        winui.move_window(self.hwnd, left, top, width, height)
        self._apply_shape()
        self.update_thumb_geometry()

    def set_width_preset(self, width: int) -> None:
        self.tracked.width = max(CARD_MIN_W, int(width))
        self.apply_size(self.tracked.width)
        self.app.schedule_save_config()

    def toggle_collapsed(self) -> None:
        self.tracked.collapsed = not self.tracked.collapsed
        self.apply_size()
        self._invalidate()
        self.app.schedule_save_config()

    def set_click_through(self, enabled: bool) -> None:
        self.tracked.click_through = bool(enabled)
        winui.set_click_through(self.hwnd, self.tracked.click_through)
        self._invalidate()
        self.app.schedule_save_config()

    def set_capture_issue(self, issue: bool) -> None:
        issue = bool(issue)
        if issue != self._capture_issue:
            self._capture_issue = issue
            self._invalidate()

    def _effective_size(self) -> tuple[int, int]:
        crop = self.tracked.crop
        if crop is not None and self._source_size[0] > 0 and self._source_size[1] > 0:
            crop = crop.clamped(*self._source_size)
        if crop is not None and crop.is_valid():
            return (crop.width, crop.height)
        if self._source_size[0] > 0 and self._source_size[1] > 0:
            return self._source_size
        if self.candidate is not None:
            left, top, right, bottom = self.candidate.info.rect
            if right - left > 0 and bottom - top > 0:
                return (right - left, bottom - top)
        return (16, 9)

    def source_size_for_crop(self) -> tuple[int, int] | None:
        if self._source_size[0] > 0 and self._source_size[1] > 0:
            return self._source_size
        if self.candidate is not None:
            left, top, right, bottom = self.candidate.info.rect
            if right - left > 0 and bottom - top > 0:
                return (right - left, bottom - top)
        return None

    def set_changed(self, changed: bool) -> None:
        self._changed = changed
        self._invalidate()

    def is_changed(self) -> bool:
        return self._changed

    def _invalidate(self) -> None:
        winui.invalidate(self.hwnd)

    def destroy(self) -> None:
        self._source_action_id += 1
        self._source_action_pending = None
        if self._parked_source is not None:
            self.app.restore_parked_source(self, activate=False)
        self._parked_seen_not_foreground = False
        self.detach()
        if self.hwnd:
            winui.destroy_window(self.hwnd)
            self.hwnd = 0


class BigPreviewWnd:
    def __init__(self, app: "App", card: CardWnd):
        self.app = app
        self.card = card
        self.thumb: Thumbnail | None = None
        self._source_size: tuple[int, int] = (0, 0)
        self._source_rect: tuple[int, int, int, int] | bool = False
        self._error = ""

        left, top, right, bottom = winapi.work_area_for_window(card.hwnd)
        width = int((right - left) * 0.9)
        height = int((bottom - top) * 0.9)
        x = left + (right - left - width) // 2
        y = top + (bottom - top - height) // 2

        hwnd = winui.create_window(
            "WPBig",
            WS_EX_TOOLWINDOW | WS_EX_LAYERED,
            WS_POPUP | WS_CLIPSIBLINGS,
            x,
            y,
            width,
            height,
            owner=0,
        )
        if not hwnd:
            raise OSError("big preview window creation failed")
        self.hwnd = hwnd
        _handlers[hwnd] = self
        self._apply_shape()
        winui.set_alpha(self.hwnd, app.config.opacity)
        winui.show_window(self.hwnd)
        winui.set_topmost(self.hwnd, app.config.always_on_top)
        winapi.set_foreground(self.hwnd)

        try:
            self.thumb = Thumbnail(self.hwnd, card.src_hwnd)
        except (OSError, ValueError):
            self.thumb = None
            self._error = "Не удалось создать превью (DWM)"
            return

        crop = card.tracked.crop
        if crop is not None:
            source_size = card.source_size_for_crop()
            if source_size is not None:
                crop = crop.clamped(*source_size)
        if crop is not None and crop.is_valid():
            self._source_size = (crop.width, crop.height)
            self._source_rect = crop.as_rect()
        else:
            size = self.thumb.source_size()
            if size[0] > 0 and size[1] > 0:
                self._source_size = size
                self._source_rect = (0, 0, size[0], size[1])
        self._update()

    def _close_button_rect(self) -> tuple[int, int, int, int]:
        width, _height = self._size()
        return (width - 116, 10, width - 14, 38)

    def on_message(self, message, wparam, lparam):
        if message == WM_PAINT:
            self._paint()
            return 0
        if message == WM_ERASEBKGND:
            return 0
        if message == WM_LBUTTONUP:
            x, y = winui.xy_from_lparam(lparam)
            left, top, right, bottom = self._close_button_rect()
            if left <= x < right and top <= y < bottom:
                self.close()
            return 0
        if message == WM_LBUTTONDBLCLK:
            self.close()
            return 0
        if message == WM_RBUTTONUP:
            if winui.track_popup_menu([(1, "Закрыть", False)], self.hwnd):
                self.close()
            return 0
        if message == WM_KEYDOWN and wparam == winui.VK_ESCAPE:
            self.close()
            return 0
        if message == WM_CLOSE:
            self.close()
            return 0
        if message == WM_SETCURSOR:
            if int(lparam & 0xFFFF) == winui.HTCLIENT:
                x, y = winui.get_cursor_client_pos(self.hwnd)
                left, top, right, bottom = self._close_button_rect()
                if left <= x < right and top <= y < bottom:
                    winui.set_hand_cursor()
                    return 1
        if message == WM_WINDOWPOSCHANGED:
            self._apply_shape()
            self._update()
        if message == winui.WM_DESTROY:
            _handlers.pop(self.hwnd, None)
            self.app.big = None
        return None

    def _apply_shape(self) -> None:
        width, height = self._size()
        if width > 0 and height > 0:
            winui.set_round_region(self.hwnd, width, height, BIG_RADIUS)

    def _size(self) -> tuple[int, int]:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        return (right - left, bottom - top)

    def _paint(self) -> None:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        width = right - left
        with winui.paint(self.hwnd) as hdc:
            winui.fill_rect(hdc, (left, top, right, bottom), 0)
            if self._error:
                winui.draw_text(
                    hdc, self._error, (left, top, right, bottom),
                    winui.rgb(0x99, 0x99, 0x99), winui.font(12),
                    DT_CENTER | DT_VCENTER | DT_SINGLELINE,
                )
            btn_left, btn_top, btn_right, btn_bottom = self._close_button_rect()
            winui.fill_rect(hdc, (btn_left, btn_top, btn_right, btn_bottom), winui.rgb(0x2B, 0x2C, 0x31))
            winui.frame_rect(hdc, (btn_left, btn_top, btn_right - 1, btn_bottom - 1), winui.rgb(0x4A, 0x4B, 0x52))
            winui.draw_text(
                hdc, "✕  Закрыть", (btn_left, btn_top, btn_right, btn_bottom),
                winui.rgb(0xE8, 0xE8, 0xE8), winui.font(12),
                DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
            )
            winui.draw_text(
                hdc, "Esc или двойной клик — тоже закрыть",
                (left, bottom - 34, right, bottom),
                winui.rgb(0x77, 0x77, 0x77), winui.font(11),
                DT_CENTER | DT_SINGLELINE | DT_VCENTER,
            )
            winui.round_rect_outline(
                hdc,
                (left + 1, top + 1, right - 1, bottom - 1),
                BIG_BORDER,
                BIG_BORDER_W,
                BIG_RADIUS,
            )

    def _update(self) -> None:
        if self.thumb is None:
            return
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        width = right - left
        height = bottom - top
        if width <= 1 or height <= 1 or self._source_size[0] <= 0 or self._source_size[1] <= 0:
            return
        inset = BIG_BORDER_W + BIG_PAD
        avail_w = width - inset * 2
        avail_h = height - inset * 2
        if avail_w <= 1 or avail_h <= 1:
            return
        source_w, source_h = self._source_size
        scale = min(avail_w / source_w, avail_h / source_h)
        dest_w = max(1, int(source_w * scale))
        dest_h = max(1, int(source_h * scale))
        dest_left = inset + (avail_w - dest_w) // 2
        dest_top = inset + (avail_h - dest_h) // 2
        self.thumb.update(
            dest_rect=(dest_left, dest_top, dest_left + dest_w, dest_top + dest_h),
            source_rect=self._source_rect,
            visible=True,
        )

    def close(self) -> None:
        if self.thumb is not None:
            self.thumb.close()
            self.thumb = None
        if self.hwnd:
            winui.destroy_window(self.hwnd)
            self.hwnd = 0


class ControlWnd:
    def __init__(self, app: "App", x: int, y: int):
        self.app = app
        self._drag: tuple[int, int, int, int] | None = None
        self._wake_message = winui.register_wake_message()

        hwnd = winui.create_window(
            "WPCtrl",
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            WS_POPUP | WS_CLIPSIBLINGS,
            x,
            y,
            CTRL_W,
            CTRL_H,
        )
        if not hwnd:
            raise OSError("control window creation failed")
        self.hwnd = hwnd
        _handlers[hwnd] = self
        winui.set_alpha(self.hwnd, app.config.opacity)

    def on_message(self, message, wparam, lparam):
        if self._wake_message and message == self._wake_message:
            self.app.defer(self.app.show_panel)
            return 1
        if message == WM_CLOSE:
            self.app.hide_panel()
            return 0
        if message == WM_PAINT:
            self._paint()
            return 0
        if message == WM_ERASEBKGND:
            return 0
        if message == WM_LBUTTONDOWN:
            self._on_down(lparam)
            return 0
        if message == WM_MOUSEMOVE:
            self._on_move(lparam)
            return 0
        if message == WM_LBUTTONUP:
            if self._drag is not None:
                self._end_drag()
            return 0
        if message == WM_RBUTTONUP:
            self.app.defer(self.app.ctrl_menu)
            return 0
        if message == winui.WM_HOTKEY:
            self.app.on_hotkey(int(wparam))
            return 0
        if message == WM_TIMER:
            self.app.on_timer(int(wparam))
            return 0
        if message == winui.WM_DESTROY:
            _handlers.pop(self.hwnd, None)
        return None

    def _size(self) -> tuple[int, int]:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        return (right - left, bottom - top)

    def _button_rects(self) -> list[tuple[int, int, int, int, str]]:
        width, _height = self._size()
        return [
            (width - BTN_W, 4, width - 4, CTRL_H - 4, "✕"),
            (width - 2 * BTN_W - 4, 4, width - BTN_W - 4, CTRL_H - 4, "⚙"),
            (width - 3 * BTN_W - 8, 4, width - 2 * BTN_W - 8, CTRL_H - 4, "+"),
        ]

    def _paint(self) -> None:
        width, height = self._size()
        with winui.paint(self.hwnd) as hdc:
            winui.fill_rect(hdc, (0, 0, width, height), HEADER_BG)
            winui.frame_rect(hdc, (0, 0, width - 1, height - 1), BORDER)
            buttons_left = width - 3 * BTN_W - 8
            version_width = 92
            version_left = max(120, buttons_left - version_width - 8)
            if not self.app._startup_ready:
                panel_title = f"{APP_NAME} · запуск…"
            else:
                panel_title = APP_NAME if self.app.cards else f"{APP_NAME} · нет окон"
            winui.draw_text(
                hdc, panel_title, (12, 0, version_left - 8, height), FG,
                winui.font(13, bold=True),
                DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
            )
            winui.draw_text(
                hdc, APP_VERSION, (version_left, 0, buttons_left - 6, height), DIM,
                winui.font(8), DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
            )
            for left, top, right, bottom, glyph in self._button_rects():
                winui.draw_text(
                    hdc, glyph, (left, top, right, bottom), FG, winui.font(12),
                    DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                )

    def _on_down(self, lparam) -> None:
        x, y = winui.xy_from_lparam(lparam)
        for index, (left, top, right, bottom, _glyph) in enumerate(self._button_rects()):
            if left <= x < right and top <= y < bottom:
                if index == 0:
                    self.app.hide_panel()
                elif index == 1:
                    self.app.defer(self.app.show_settings)
                else:
                    self.app.defer(self.app.show_selector)
                return
        cursor_x, cursor_y = winui.get_cursor_pos()
        left, top, _right, _bottom = winui.get_window_rect(self.hwnd)
        self._drag = (cursor_x - left, cursor_y - top, cursor_x, cursor_y)
        winui.user32.SetCapture(self.hwnd)

    def _on_move(self, _lparam) -> None:
        if self._drag is None:
            return
        offset_x, offset_y, start_x, start_y = self._drag
        cursor_x, cursor_y = winui.get_cursor_pos()
        if abs(cursor_x - start_x) > 2 or abs(cursor_y - start_y) > 2:
            width, height = self._size()
            winui.move_window(self.hwnd, cursor_x - offset_x, cursor_y - offset_y, width, height)

    def _end_drag(self) -> None:
        self._drag = None
        winui.user32.ReleaseCapture()
        left, top, _right, _bottom = winui.get_window_rect(self.hwnd)
        if self.app.config.ctrl_x != left or self.app.config.ctrl_y != top:
            self.app.config.ctrl_x = left
            self.app.config.ctrl_y = top
            self.app.schedule_save_config()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config_service = ConfigService()
        self.config = self.config_service.load()
        self.autostart = Autostart()
        registry_autostart = self.autostart.is_enabled()
        if getattr(sys, "frozen", False) and registry_autostart and not self.autostart.is_current():
            self.autostart.set_enabled(True)
            registry_autostart = self.autostart.is_current()
        self.config.autostart = registry_autostart
        self.finder = WindowFinder(own_pid=os.getpid())
        self.detector = AsyncChangeDetector()

        self.cards: list[CardWnd] = []
        self.big: BigPreviewWnd | None = None
        self.tray: TrayIcon | None = None
        # The control panel is a management surface, not part of the normal
        # monitoring workspace.  Start it hidden; PiP cards have their own
        # visibility lifecycle and are shown independently after startup.
        self._hidden = True
        self._cards_hidden = False
        self._panel_hide_notice_done = False
        self._all_hidden_notice_done = False
        self._last_balloon = 0.0
        self._save_pending = False
        self._detector_stall_notified = False
        self._capture_failures: dict[int, int] = {}
        self._capture_notified: set[int] = set()
        self._detector_quiet: dict[int, int] = {}
        self._detector_next_due: dict[int, float] = {}
        self._registered_hotkeys: set[int] = set()
        self._deferred: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.SimpleQueue()
        self._defer_timer_id: str | None = None
        self._defer_closed = False
        self._startup_ready = False
        self._startup_started_at = time.monotonic()
        self._shutting_down = False

        _register_classes()
        ctrl_x, ctrl_y = self._default_ctrl_pos()
        self.panel = ControlWnd(self, ctrl_x, ctrl_y)
        self._build_tray()
        # Card creation, window enumeration and DWM binding are intentionally
        # deferred until the native panel has had a chance to paint.

    def _default_ctrl_pos(self) -> tuple[int, int]:
        if self.config.ctrl_x is not None and self.config.ctrl_y is not None:
            left, top, right, bottom = winapi.work_area_for_point(self.config.ctrl_x, self.config.ctrl_y)
            x = max(left, min(right - CTRL_W, self.config.ctrl_x))
            y = max(top, min(bottom - CTRL_H, self.config.ctrl_y))
            return (x, y)
        left, top, right, bottom = winapi.work_area()
        return (right - CTRL_W - 12, top + 60)

    def _default_card_pos(self, index: int) -> tuple[int, int]:
        ctrl_x, ctrl_y = self._default_ctrl_pos()
        left, top, right, bottom = winapi.work_area_for_point(ctrl_x, ctrl_y)
        x0 = max(left, min(right - CARD_W, ctrl_x + CTRL_W - CARD_W))
        y0 = max(top, ctrl_y + CTRL_H + 10)
        default_height = int((CARD_W - 12) * 9 / 16) + CARD_HEADER_H + 12
        step_y = default_height + 10
        rows = max(1, (max(y0 + step_y, bottom) - y0) // step_y)
        row = index % rows
        column = index // rows
        x = max(left, x0 - column * (CARD_W + 10))
        y = min(bottom - CARD_MIN_H, y0 + row * step_y)
        return (x, y)

    def defer(self, fn: Callable[..., Any], *args: Any) -> None:
        """Передать работу из native WNDPROC в безопасный Tcl/Tk-контекст."""
        if not self._defer_closed:
            self._deferred.put((fn, args))

    def _pump_deferred(self) -> None:
        """Drain native->Tk mailbox from a Tcl timer callback."""
        self._defer_timer_id = None
        if self._defer_closed:
            return

        processed = 0
        max_batch = 64
        while processed < max_batch:
            try:
                fn, args = self._deferred.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except tk.TclError:
                if self._defer_closed:
                    return
                traceback.print_exc()
            except Exception:
                traceback.print_exc()
            processed += 1

        if self._defer_closed:
            return
        delay_ms = 1 if processed == max_batch else 30
        try:
            self._defer_timer_id = self.root.after(delay_ms, self._pump_deferred)
        except tk.TclError:
            self._defer_timer_id = None
            self._defer_closed = True

    def _stop_deferred_pump(self) -> None:
        self._defer_closed = True
        if self._defer_timer_id is not None:
            try:
                self.root.after_cancel(self._defer_timer_id)
            except tk.TclError:
                pass
            self._defer_timer_id = None

    def start(self) -> None:
        # Keep the management panel hidden on a normal launch.  The tray helper
        # exists already, and PiP cards are shown as soon as startup binding is
        # complete.  One-file builds use the boot splash for immediate feedback.
        self.apply_config()
        self._defer_closed = False
        try:
            self._defer_timer_id = self.root.after(10, self._pump_deferred)
        except tk.TclError:
            self._defer_timer_id = None
            self._defer_closed = True
        if background_mode():
            self.hide_all_to_tray(notify=False)
        else:
            self._hidden = True
            winui.hide_window(self.panel.hwnd)
        winui.set_timer(self.panel.hwnd, TIMER_STARTUP, 50)

    def _finish_startup(self) -> None:
        if self._startup_ready or self._shutting_down:
            return
        self.rebuild_cards()
        self.apply_config()
        self._do_refresh()
        self._startup_ready = True
        winui.set_timer(self.panel.hwnd, TIMER_REFRESH, REFRESH_MS)
        winui.set_timer(self.panel.hwnd, TIMER_CHANGE, int(self.config.change_interval_sec * 1000))
        # PiP visibility is intentionally independent from panel visibility.
        if not self._cards_hidden:
            for card in self.cards:
                winui.show_window(card.hwnd)
        winui.invalidate(self.panel.hwnd)
        if self.config.first_run_selector and not self.config.windows and not background_mode():
            self.defer(self.show_selector)
        self._close_boot_splash()

    def _close_boot_splash(self) -> None:
        try:
            import pyi_splash  # type: ignore[import-not-found]
            pyi_splash.close()
        except (ImportError, RuntimeError):
            pass

    def run(self) -> None:
        self.root.mainloop()

    def _build_tray(self) -> None:
        hicon = 0
        try:
            hicon = icon_module.make_tray_icon()
        except OSError:
            hicon = 0
        try:
            tray = TrayIcon(
                f"{APP_NAME} {APP_VERSION}",
                self._tray_menu_items,
                self._queue_tray_command,
                self._queue_tray_click,
                hicon,
            )
            if not tray.start():
                if hicon:
                    icon_module.destroy_tray_icon(hicon)
                return
            self.tray = tray
        except OSError:
            if hicon:
                icon_module.destroy_tray_icon(hicon)
            self.tray = None

    def _queue_tray_command(self, command: int) -> None:
        self.defer(self._on_tray_command, command)

    def _queue_tray_click(self) -> None:
        self.defer(self.toggle_panel)

    def _tray_menu_items(self):
        items = [
            (CMD_SHOW, "Показать панель", False),
            (CMD_HIDE, "Скрыть всё в трей", False),
            (CMD_TOGGLE_CARDS, "Показать карточки" if self._cards_hidden else "Скрыть карточки", self._cards_hidden),
            None,
            (CMD_ADD, "Добавить окно...", False),
            (CMD_PROFILES, "Профили...", False),
            (CMD_SETTINGS, "Настройки...", False),
        ]
        if any(card.tracked.click_through for card in self.cards):
            items.extend([None, (CMD_DISABLE_CLICKTHROUGH, "Отключить click-through у всех", False)])
        items.extend([
            None,
            (CMD_AUTOSTART, "Автозапуск", bool(self.config.autostart)),
            None,
            (CMD_QUIT, "Выход", False),
        ])
        return items

    def _on_tray_command(self, command: int) -> None:
        if command == CMD_SHOW:
            self.show_panel()
        elif command == CMD_HIDE:
            self.hide_all_to_tray()
        elif command == CMD_TOGGLE_CARDS:
            self.toggle_cards()
        elif command == CMD_ADD:
            self.show_selector()
        elif command == CMD_SETTINGS:
            self.show_settings()
        elif command == CMD_PROFILES:
            self.show_profiles()
        elif command == CMD_DISABLE_CLICKTHROUGH:
            self.disable_all_click_through()
        elif command == CMD_AUTOSTART:
            self.toggle_autostart()
        elif command == CMD_QUIT:
            self.quit()

    def save_config(self) -> None:
        if getattr(self, "panel", None) is not None and self.panel.hwnd:
            winui.kill_timer(self.panel.hwnd, TIMER_SAVE_CONFIG)
        self._save_pending = False
        self._save_config_now()

    def schedule_save_config(self, delay_ms: int = 500) -> None:
        if not getattr(self, "panel", None) or not self.panel.hwnd:
            self._save_config_now()
            return
        winui.kill_timer(self.panel.hwnd, TIMER_SAVE_CONFIG)
        self._save_pending = bool(winui.set_timer(self.panel.hwnd, TIMER_SAVE_CONFIG, max(1, int(delay_ms))))
        if not self._save_pending:
            self._save_config_now()

    def _flush_scheduled_save(self) -> None:
        if getattr(self, "panel", None) is not None and self.panel.hwnd:
            winui.kill_timer(self.panel.hwnd, TIMER_SAVE_CONFIG)
        if self._save_pending:
            self._save_pending = False
            self._save_config_now()

    def _save_config_now(self) -> None:
        if not self.config_service.save(self.config) and self.tray is not None:
            self.tray.notify("LookUp Windows", "Не удалось сохранить настройки")

    def apply_config(self) -> None:
        self.apply_alpha()
        self.apply_topmost()
        if self.panel.hwnd:
            self._register_hotkeys()

    def apply_alpha(self) -> None:
        winui.set_alpha(self.panel.hwnd, self.config.opacity)
        for card in self.cards:
            winui.set_alpha(card.hwnd, self.config.opacity)
        if self.big is not None:
            winui.set_alpha(self.big.hwnd, self.config.opacity)

    def apply_topmost(self) -> None:
        on = bool(self.config.always_on_top)
        winui.set_topmost(self.panel.hwnd, on)
        for card in self.cards:
            winui.set_topmost(card.hwnd, on)
        if self.big is not None:
            winui.set_topmost(self.big.hwnd, on)

    def _register_hotkeys(self) -> None:
        if not getattr(self, "panel", None) or not self.panel.hwnd:
            return
        for hotkey_id in tuple(self._registered_hotkeys):
            winui.unregister_hotkey(self.panel.hwnd, hotkey_id)
        self._registered_hotkeys.clear()
        if not self.config.hotkeys_enabled:
            return
        modifiers = winui.MOD_CONTROL | winui.MOD_ALT | winui.MOD_NOREPEAT
        combos = [
            (HOTKEY_ADD_FOREGROUND, modifiers, ord("A")),
            (HOTKEY_TOGGLE_PANEL, modifiers, ord("H")),
            (HOTKEY_SELECTOR, modifiers, ord("S")),
            (HOTKEY_DISABLE_CLICKTHROUGH, modifiers, ord("C")),
        ]
        failed = 0
        for hotkey_id, mods, vk in combos:
            if winui.register_hotkey(self.panel.hwnd, hotkey_id, mods, vk):
                self._registered_hotkeys.add(hotkey_id)
            else:
                failed += 1
        if failed and self.tray is not None:
            self.tray.notify("LookUp Windows", "Часть глобальных hotkeys занята другим приложением")

    def on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == HOTKEY_ADD_FOREGROUND:
            self.defer(self.add_foreground_window)
        elif hotkey_id == HOTKEY_TOGGLE_PANEL:
            self.defer(self.toggle_panel)
        elif hotkey_id == HOTKEY_SELECTOR:
            self.defer(self.show_selector)
        elif hotkey_id == HOTKEY_DISABLE_CLICKTHROUGH:
            self.defer(self.disable_all_click_through)

    def add_foreground_window(self) -> None:
        hwnd = winapi.get_foreground_hwnd()
        candidate = self.finder.candidate(hwnd)
        if candidate is None or candidate.process_name in {"?", "<нет доступа>"}:
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Не удалось определить активное окно")
            return
        same_process = [c for c in self.finder.list_windows() if c.process_name.lower() == candidate.process_name.lower()]
        title_filter = candidate.title if len(same_process) > 1 else ""
        tracked = TrackedWindow(process=candidate.process_name, title_contains=title_filter)
        if any(existing.same_target(tracked) for existing in self.config.windows):
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Активное окно уже отслеживается")
            return
        self.config.windows.append(tracked)
        try:
            card = CardWnd(self, tracked, 0, 0)
        except OSError:
            self.config.windows.remove(tracked)
            return
        x, y = self._card_pos(tracked, len(self.cards))
        current_w, current_h = card._size()
        winui.move_window(card.hwnd, x, y, current_w, current_h)
        card.apply_size(tracked.width)
        self.cards.append(card)
        if not self._cards_hidden:
            winui.show_window(card.hwnd)
        self.apply_topmost()
        self.save_config()
        self._do_refresh()

    def disable_all_click_through(self) -> None:
        changed = False
        for card in self.cards:
            if card.tracked.click_through:
                card.set_click_through(False)
                changed = True
        if changed:
            self.save_config()

    def on_window_returned(self, card: CardWnd) -> None:
        if self.config.notify_window_return and self.tray is not None:
            self.tray.notify("LookUp Windows", f"Окно снова найдено: {card._title}")

    def save_profile(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        self.config.profiles[name] = [window.clone() for window in self.config.windows]
        self.save_config()

    def load_profile(self, name: str) -> None:
        source = self.config.profiles.get(name)
        if source is None:
            return
        self.close_fullscreen()
        self.detector.clear()
        self.config.windows = [window.clone() for window in source]
        self.rebuild_cards()
        if not self._cards_hidden:
            for card in self.cards:
                winui.show_window(card.hwnd)
        self.apply_config()
        self.save_config()
        self._do_refresh()

    def delete_profile(self, name: str) -> None:
        if name in self.config.profiles:
            del self.config.profiles[name]
            self.save_config()

    def show_profiles(self) -> None:
        dialog = ProfilesDialog(self.root, sorted(self.config.profiles, key=str.casefold))
        action, name = dialog.show(self.root)
        if action == "save":
            profile_name = simpledialog.askstring("Профиль", "Имя профиля:", parent=self.root)
            if profile_name:
                self.save_profile(profile_name)
        elif action == "load" and name:
            self.load_profile(name)
        elif action == "delete" and name:
            self.delete_profile(name)

    def rebuild_cards(self) -> None:
        for card in self.cards:
            self.restore_parked_source(card, activate=False)
            card.destroy()
        self.cards = []
        for index, tracked in enumerate(self.config.windows):
            try:
                card = CardWnd(self, tracked, 0, 0)
            except OSError:
                continue
            x, y = self._card_pos(tracked, index)
            current_w, current_h = card._size()
            winui.move_window(card.hwnd, x, y, current_w, current_h)
            card.apply_size(tracked.width)
            card.update_thumb_geometry()
            self.cards.append(card)

    def _card_pos(self, tracked: TrackedWindow, index: int) -> tuple[int, int]:
        if tracked.x is not None and tracked.y is not None:
            left, top, right, bottom = winapi.work_area_for_point(tracked.x, tracked.y)
            monitor_width = max(CARD_MIN_W, right - left)
            width = min(max(CARD_MIN_W, int(tracked.width or CARD_W)), monitor_width)
            x = max(left, min(right - width, tracked.x))
            y = max(top, min(bottom - CARD_MIN_H, tracked.y))
            return (x, y)
        return self._default_card_pos(index)

    def _card_height(self, card: CardWnd) -> int:
        left, top, right, bottom = winapi.work_area_for_window(card.hwnd)
        monitor_width = max(CARD_MIN_W, right - left)
        monitor_height = max(CARD_MIN_H, bottom - top)
        width = min(max(CARD_MIN_W, int(card.tracked.width or CARD_W)), monitor_width)
        return min(card.desired_height(width), monitor_height)

    def on_timer(self, timer_id: int) -> None:
        if timer_id == TIMER_REFRESH:
            self._do_refresh()
        elif timer_id == TIMER_CHANGE:
            self._change_tick()
        elif timer_id == TIMER_SAVE_CONFIG:
            self._flush_scheduled_save()
        elif timer_id == TIMER_REFRESH_SOON:
            winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
            if self._startup_ready:
                self._do_refresh()
        elif timer_id == TIMER_STARTUP:
            winui.kill_timer(self.panel.hwnd, TIMER_STARTUP)
            self._finish_startup()
        elif timer_id == TIMER_PARKED_WATCH:
            self._watch_parked_sources()

    def _watch_parked_sources(self) -> None:
        """Fast, cheap taskbar/Alt+Tab watcher for parked source windows.

        This intentionally does not enumerate windows or touch DWM.  It only
        checks foreground/minimized state so a user request to bring a parked
        source back is noticed in ~100 ms instead of waiting for REFRESH_MS.
        """
        parked = [card for card in self.cards if card._parked_source is not None]
        if not parked:
            winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)
            return
        foreground = winapi.get_foreground_hwnd()
        for card in parked:
            if card._source_action_pending is not None or not card.src_hwnd:
                continue
            if winapi.is_minimized(card.src_hwnd):
                self.activate_card(card)
            elif foreground != card.src_hwnd:
                card._parked_seen_not_foreground = True
            elif card._parked_seen_not_foreground:
                self.activate_card(card)

    def _do_refresh(self) -> None:
        foreground = winapi.get_foreground_hwnd()
        desktop_active = winapi.is_desktop_foreground()
        # Missing cards used to call WindowFinder.find() independently, causing
        # a full EnumWindows/process-name pass per card.  Cache one enumeration
        # per refresh and reuse it for all auto-refind matches.
        refind_candidates: list[WindowCandidate] | None = None
        for card in self.cards:
            try:
                if card._parked_source is not None:
                    if card.src_hwnd and winapi.is_minimized(card.src_hwnd):
                        # Clicking a taskbar button while the parked HWND is
                        # still considered foreground can minimize it instead
                        # of producing a foreground transition.  Treat that as
                        # an explicit user request to recover the source.
                        self.activate_card(card)
                    elif card.src_hwnd != foreground:
                        card._parked_seen_not_foreground = True
                    elif card._parked_seen_not_foreground:
                        # Restore only on a real foreground transition back to the
                        # parked source. Immediately after a PiP click the source may
                        # still be the foreground HWND because the PiP itself is
                        # WS_EX_NOACTIVATE; restoring in that transient state made
                        # parking look like it never happened.
                        self.activate_card(card)
                candidate = None
                if card.src_hwnd:
                    candidate = self.finder.revalidate(card.src_hwnd, card.tracked)
                if (
                    candidate is not None
                    and candidate.info.minimized
                    and card._parked_source is None
                    and self.config.restore_minimized
                    and not desktop_active
                ):
                    winapi.show_window_noactivate(candidate.hwnd)
                    candidate = self.finder.revalidate(card.src_hwnd, card.tracked)
                if candidate is None:
                    if card.src_hwnd:
                        self.detector.forget(card.src_hwnd)
                    if self.config.auto_refind:
                        if refind_candidates is None:
                            refind_candidates = self.finder.list_windows()
                        candidate = next(
                            (item for item in refind_candidates if self.finder.matches(item, card.tracked)),
                            None,
                        )
                        if (
                            candidate is not None
                            and candidate.info.minimized
                            and card._parked_source is None
                            and self.config.restore_minimized
                            and not desktop_active
                        ):
                            winapi.show_window_noactivate(candidate.hwnd)
                            candidate = self.finder.revalidate(candidate.hwnd, card.tracked)
                if (
                    candidate is not None
                    and card._parked_source is None
                    and card._source_action_pending is None
                    and winapi.looks_like_lookup_parked(candidate.hwnd)
                ):
                    # Safety migration for windows stranded by an older LookUp
                    # process.  Exact placement is unavailable after restart,
                    # but leaving an application permanently off-screen is worse;
                    # recover it to a visible monitor once, outside the UI thread.
                    card._source_action_id += 1
                    action_id = card._source_action_id
                    card._source_action_pending = "restore"
                    threading.Thread(
                        target=self._recover_orphan_source_worker,
                        args=(card, candidate.hwnd, action_id),
                        name="LookUpWindows-RecoverSource",
                        daemon=True,
                    ).start()
                card.set_candidate(candidate, foreground)
                if candidate is not None and candidate.hwnd == foreground and card.is_changed():
                    card.set_changed(False)
                    self.detector.forget(candidate.hwnd)
                if self.big is not None and self.big.card is card and card.src_hwnd == 0:
                    self.close_fullscreen()
            except Exception:
                continue
        winui.invalidate(self.panel.hwnd)

    def _change_tick(self) -> None:
        if not self.config.change_detection:
            self.detector.clear()
            self._detector_quiet.clear()
            self._detector_next_due.clear()
            self._capture_failures.clear()
            self._capture_notified.clear()
            for card in self.cards:
                card.set_capture_issue(False)
            return

        foreground = winapi.get_foreground_hwnd()
        triggered_cards: list[CardWnd] = []
        cards_by_hwnd = {card.src_hwnd: card for card in self.cards if card.src_hwnd}

        for hwnd, result in self.detector.poll_results():
            card = cards_by_hwnd.get(hwnd)
            if card is None or card.candidate is None or not card.tracked.detect_changes:
                self.detector.forget(hwnd)
                self._capture_failures.pop(hwnd, None)
                self._capture_notified.discard(hwnd)
                continue
            if card.candidate.info.minimized or hwnd == foreground:
                self.detector.forget(hwnd)
                continue
            now_mono = time.monotonic()
            base_interval = self.config.change_interval_sec
            if result.status in {"capture_failed", "black_frame"}:
                failures = self._capture_failures.get(hwnd, 0) + 1
                self._capture_failures[hwnd] = failures
                self._detector_next_due[hwnd] = now_mono + min(15.0, base_interval * 2)
                if failures >= 3:
                    card.set_capture_issue(True)
                    if hwnd not in self._capture_notified and self.tray is not None:
                        detail = "получается чёрный кадр" if result.status == "black_frame" else "захват не отвечает"
                        self.tray.notify("LookUp Windows", f"Детект недоступен ({detail}): {card._title}")
                        self._capture_notified.add(hwnd)
                continue
            self._capture_failures.pop(hwnd, None)
            self._capture_notified.discard(hwnd)
            card.set_capture_issue(False)
            score = result.score
            changed = (
                result.status == "ok"
                and score is not None
                and (score > self.config.change_threshold or result.changed_fraction >= 0.03)
            )
            if changed:
                self._detector_quiet[hwnd] = 0
                self._detector_next_due[hwnd] = now_mono + base_interval
                if not card.is_changed():
                    card.set_changed(True)
                    triggered_cards.append(card)
            elif result.status == "ok":
                quiet = min(8, self._detector_quiet.get(hwnd, 0) + 1)
                self._detector_quiet[hwnd] = quiet
                factor = 1 if quiet < 2 else (2 if quiet < 4 else 4)
                self._detector_next_due[hwnd] = now_mono + base_interval * factor
            else:
                self._detector_next_due[hwnd] = now_mono + base_interval

        eligible: list[int] = []
        now_mono = time.monotonic()
        for card in self.cards:
            try:
                if card.src_hwnd == 0 or card.candidate is None or not card.tracked.detect_changes:
                    continue
                if card.candidate.info.minimized or card.src_hwnd == foreground:
                    self.detector.forget(card.src_hwnd)
                    self._detector_quiet.pop(card.src_hwnd, None)
                    self._detector_next_due.pop(card.src_hwnd, None)
                    continue
                if now_mono < self._detector_next_due.get(card.src_hwnd, 0.0):
                    continue
                eligible.append(card.src_hwnd)
            except Exception:
                continue
        self.detector.schedule(eligible)

        stalled_for = self.detector.busy_for()
        if stalled_for > max(10.0, self.config.change_interval_sec * 4):
            if not self._detector_stall_notified and self.tray is not None:
                self.tray.notify(
                    "LookUp Windows",
                    "Детектор изменений долго ждёт ответ одного из окон. Превью продолжат работать.",
                )
                self._detector_stall_notified = True
        elif stalled_for == 0:
            self._detector_stall_notified = False

        if triggered_cards and self.config.notify_sound and winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except RuntimeError:
                pass
        if triggered_cards and self.tray is not None and time.time() - self._last_balloon > 30:
            if len(triggered_cards) == 1:
                text = f"Изменение: {triggered_cards[0]._title}"
            else:
                text = f"Обнаружены изменения в {len(triggered_cards)} окнах"
            self.tray.notify("LookUp Windows", text)
            self._last_balloon = time.time()

    def show_panel(self) -> None:
        """Показать только управляющую панель; PiP живут независимо."""
        self._hidden = False
        winui.show_window(self.panel.hwnd)
        self.apply_topmost()
        if self._startup_ready:
            winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
            winui.set_timer(self.panel.hwnd, TIMER_REFRESH_SOON, 80)

    def hide_panel(self, notify: bool = True) -> None:
        """Скрыть только панель. PiP-карточки и Big Preview не трогать."""
        self._hidden = True
        winui.hide_window(self.panel.hwnd)
        if notify and self.tray is not None and not self._panel_hide_notice_done:
            self.tray.notify(
                "LookUp Windows",
                "Панель скрыта. PiP продолжают работать; панель можно вернуть из трея.",
            )
            self._panel_hide_notice_done = True

    def hide_all_to_tray(self, notify: bool = True) -> None:
        """Явная команда скрыть панель и все превью."""
        self._hidden = True
        self._cards_hidden = True
        self.close_fullscreen()
        for card in self.cards:
            winui.hide_window(card.hwnd)
        winui.hide_window(self.panel.hwnd)
        if notify and self.tray is not None and not self._all_hidden_notice_done:
            self.tray.notify(
                "LookUp Windows",
                "Панель и PiP скрыты в трей. Карточки и панель можно вернуть отдельно.",
            )
            self._all_hidden_notice_done = True

    def toggle_panel(self) -> None:
        if self._hidden:
            self.show_panel()
        else:
            self.hide_panel()

    def toggle_cards(self) -> None:
        self._cards_hidden = not self._cards_hidden
        for card in self.cards:
            if self._cards_hidden:
                winui.hide_window(card.hwnd)
            else:
                winui.show_window(card.hwnd)
        self.apply_topmost()

    def restore_parked_source(self, card: CardWnd, *, activate: bool) -> bool:
        """Safety restore used by detach/remove/shutdown paths.

        Interactive PiP/taskbar restores run on a worker via ``activate_card``.
        Cleanup paths must not throw away the saved placement before the window
        is observably back on-screen, so they use the verified synchronous
        routine and clear state only after success.
        """
        state = card._parked_source
        hwnd = card.src_hwnd
        if state is None:
            if activate and hwnd and winapi.is_window(hwnd):
                return winapi.set_foreground(hwnd)
            return False
        if not hwnd or not winapi.is_window(hwnd):
            card._parked_source = None
            card._parked_seen_not_foreground = False
            return False

        ok = winapi.restore_parked_window_sync(hwnd, state)
        if not ok:
            return False
        card._parked_source = None
        card._parked_seen_not_foreground = False
        card._source_action_pending = None
        card._minimized = False
        card._active = bool(activate)
        card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
        card.set_changed(False)
        self.detector.forget(hwnd)
        card.update_thumb_geometry()
        card._invalidate()
        if not any(item is not card and item._parked_source is not None for item in self.cards):
            winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)
        if activate:
            winapi.set_foreground(hwnd)
        return True

    def _recover_orphan_source_worker(self, card: CardWnd, hwnd: int, action_id: int) -> None:
        ok = winapi.recover_orphaned_lookup_park(hwnd)
        self.defer(self._finish_orphan_source_recovery, card, hwnd, action_id, ok)

    def _finish_orphan_source_recovery(
        self, card: CardWnd, hwnd: int, action_id: int, ok: bool
    ) -> None:
        if card.hwnd == 0:
            return
        if action_id != card._source_action_id:
            # The initial bind can legitimately advance the card generation
            # while this one-shot recovery is in flight.  The native recovery
            # still happened; make sure the next refresh observes it.
            if ok:
                winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
                winui.set_timer(self.panel.hwnd, TIMER_REFRESH_SOON, 80)
            return
        card._source_action_pending = None
        if ok:
            card._minimized = False
            card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
            card.update_thumb_geometry()
            self.detector.forget(hwnd)
            winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
            winui.set_timer(self.panel.hwnd, TIMER_REFRESH_SOON, 80)
        elif self.tray is not None:
            self.tray.notify("LookUp Windows", "Не удалось вернуть окно, оставшееся за экраном")
        card._invalidate()

    def _park_source_worker(self, card: CardWnd, hwnd: int, action_id: int) -> None:
        state = winapi.park_window_offscreen_sync(hwnd)
        # If the card disappeared while the foreign window was being moved, put
        # the application back immediately rather than orphaning it off-screen.
        if state is not None and (self._shutting_down or card.hwnd == 0 or card.src_hwnd != hwnd or card not in self.cards):
            winapi.restore_parked_window_sync(hwnd, state)
            return
        self.defer(self._finish_park_source, card, hwnd, action_id, state)

    def _finish_park_source(
        self, card: CardWnd, hwnd: int, action_id: int, state: winapi.ParkedWindowState | None
    ) -> None:
        if card.hwnd == 0 or card.src_hwnd != hwnd or action_id != card._source_action_id:
            if state is not None:
                threading.Thread(
                    target=winapi.restore_parked_window_sync, args=(hwnd, state), daemon=True
                ).start()
            return
        card._source_action_pending = None
        if state is None:
            card._invalidate()
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Не удалось скрыть исходное окно")
            return
        card._parked_source = state
        card._parked_seen_not_foreground = winapi.get_foreground_hwnd() != hwnd
        card._minimized = False
        card._active = False
        card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
        self.detector.forget(hwnd)
        card.update_thumb_geometry()
        card._invalidate()
        winui.set_timer(self.panel.hwnd, TIMER_PARKED_WATCH, 100)

    def _restore_source_worker(
        self, card: CardWnd, hwnd: int, action_id: int, state: winapi.ParkedWindowState, activate: bool
    ) -> None:
        ok = winapi.restore_parked_window_sync(hwnd, state)
        self.defer(self._finish_restore_source, card, hwnd, action_id, ok, activate)

    def _finish_restore_source(
        self, card: CardWnd, hwnd: int, action_id: int, ok: bool, activate: bool
    ) -> None:
        if card.hwnd == 0 or card.src_hwnd != hwnd or action_id != card._source_action_id:
            return
        card._source_action_pending = None
        if not ok:
            card._invalidate()
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Не удалось вернуть исходное окно")
            return
        card._parked_source = None
        card._parked_seen_not_foreground = False
        card._minimized = False
        card._active = bool(activate)
        card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
        card.set_changed(False)
        self.detector.forget(hwnd)
        card.update_thumb_geometry()
        card._invalidate()
        if not any(item._parked_source is not None for item in self.cards):
            winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)
        if activate:
            winapi.set_foreground(hwnd)

    def toggle_card_source(self, card: CardWnd) -> None:
        """PiP click: hide/show the source without blocking LookUp's UI.

        Cross-process placement is executed on a short-lived daemon worker.  The
        worker uses a synchronous, verified Win32 sequence, so maximized/RDP/1C
        windows cannot race SW_RESTORE against the off-screen move as they did
        with two asynchronous requests.
        """
        hwnd = card.src_hwnd
        if not hwnd or not winapi.is_window(hwnd) or card._source_action_pending is not None:
            return

        card._source_action_id += 1
        action_id = card._source_action_id
        if card._parked_source is not None:
            state = card._parked_source
            card._source_action_pending = "restore"
            card._invalidate()
            threading.Thread(
                target=self._restore_source_worker,
                args=(card, hwnd, action_id, state, True),
                name="LookUpWindows-RestoreSource",
                daemon=True,
            ).start()
            return

        if winapi.is_minimized(hwnd):
            winapi.set_foreground(hwnd)
            card._minimized = False
            card._active = True
            card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
            card.set_changed(False)
            self.detector.forget(hwnd)
            card.update_thumb_geometry()
            card._invalidate()
            return

        card._source_action_pending = "park"
        card._active = False
        card._invalidate()
        threading.Thread(
            target=self._park_source_worker,
            args=(card, hwnd, action_id),
            name="LookUpWindows-ParkSource",
            daemon=True,
        ).start()

    def activate_card(self, card: CardWnd) -> None:
        """Explicit open action: always restore/activate, never hide."""
        if not card.src_hwnd or card._source_action_pending is not None:
            return
        if card._parked_source is not None:
            card._source_action_id += 1
            action_id = card._source_action_id
            state = card._parked_source
            card._source_action_pending = "restore"
            card._invalidate()
            threading.Thread(
                target=self._restore_source_worker,
                args=(card, card.src_hwnd, action_id, state, True),
                name="LookUpWindows-RestoreSource",
                daemon=True,
            ).start()
        else:
            winapi.set_foreground(card.src_hwnd)
            card.set_changed(False)
            self.detector.forget(card.src_hwnd)

    def remove_card(self, card: CardWnd) -> None:
        self.restore_parked_source(card, activate=False)
        if self.big is not None and self.big.card is card:
            self.close_fullscreen()
        if card.src_hwnd:
            self.detector.forget(card.src_hwnd)
            self._capture_failures.pop(card.src_hwnd, None)
            self._capture_notified.discard(card.src_hwnd)
            self._detector_quiet.pop(card.src_hwnd, None)
            self._detector_next_due.pop(card.src_hwnd, None)
        if card in self.cards:
            self.cards.remove(card)
        if card.tracked in self.config.windows:
            self.config.windows.remove(card.tracked)
        card.destroy()
        self.save_config()
        winui.invalidate(self.panel.hwnd)

    def open_fullscreen(self, card: CardWnd) -> None:
        if card.src_hwnd == 0:
            return
        self.close_fullscreen()
        try:
            self.big = BigPreviewWnd(self, card)
        except OSError:
            self.big = None

    def close_fullscreen(self) -> None:
        if self.big is not None:
            self.big.close()
            self.big = None

    def card_menu(self, card: CardWnd) -> None:
        command = winui.track_popup_menu(
            [
                (MENU_OPEN, "Открыть окно", False),
                (MENU_BIG, "Большое превью", False),
                (MENU_COLLAPSE, "Свернуть карточку" if not card.tracked.collapsed else "Развернуть карточку", card.tracked.collapsed),
                None,
                (MENU_CROP, "Область окна (crop)...", False),
                (MENU_FILTER, "Фильтр окна...", False),
                (MENU_CLICKTHROUGH, "Click-through", card.tracked.click_through),
                (MENU_DETECT_CHANGES, "Детект изменений", card.tracked.detect_changes),
                None,
                (MENU_SIZE_SMALL, "Размер: маленький", card.tracked.width == 220),
                (MENU_SIZE_MEDIUM, "Размер: средний", card.tracked.width == 280),
                (MENU_SIZE_LARGE, "Размер: большой", card.tracked.width == 420),
                None,
                (MENU_REFRESH, "Обновить", False),
                (MENU_REMOVE, "Убрать", False),
            ],
            self.panel.hwnd,
        )
        if command == MENU_OPEN:
            self.activate_card(card)
        elif command == MENU_BIG:
            self.open_fullscreen(card)
        elif command == MENU_COLLAPSE:
            card.toggle_collapsed()
        elif command == MENU_CROP:
            self.show_crop_dialog(card)
        elif command == MENU_FILTER:
            self.show_filter_dialog(card)
        elif command == MENU_CLICKTHROUGH:
            card.set_click_through(not card.tracked.click_through)
            if card.tracked.click_through and self.tray is not None:
                self.tray.notify("LookUp Windows", "Click-through включён. Ctrl+Alt+C отключает его у всех карточек.")
        elif command == MENU_DETECT_CHANGES:
            card.tracked.detect_changes = not card.tracked.detect_changes
            if not card.tracked.detect_changes and card.src_hwnd:
                self.detector.forget(card.src_hwnd)
                self._capture_failures.pop(card.src_hwnd, None)
                self._capture_notified.discard(card.src_hwnd)
                self._detector_quiet.pop(card.src_hwnd, None)
                self._detector_next_due.pop(card.src_hwnd, None)
                card.set_capture_issue(False)
                card.set_changed(False)
            self.schedule_save_config()
        elif command == MENU_SIZE_SMALL:
            card.set_width_preset(220)
        elif command == MENU_SIZE_MEDIUM:
            card.set_width_preset(280)
        elif command == MENU_SIZE_LARGE:
            card.set_width_preset(420)
        elif command == MENU_REFRESH:
            self._do_refresh()
        elif command == MENU_REMOVE:
            self.remove_card(card)

    def ctrl_menu(self) -> None:
        items = [
            (1, "Добавить окно...", False),
            (2, "Профили...", False),
            (3, "Настройки...", False),
            (7, "Показать карточки" if self._cards_hidden else "Скрыть карточки", self._cards_hidden),
        ]
        if any(card.tracked.click_through for card in self.cards):
            items.extend([None, (5, "Отключить click-through у всех", False)])
        items.extend([
            None,
            (4, f"Автозапуск ({'вкл' if self.config.autostart else 'выкл'})", self.config.autostart),
            None,
            (6, "Выход", False),
        ])
        command = winui.track_popup_menu(items, self.panel.hwnd)
        if command == 1:
            self.show_selector()
        elif command == 2:
            self.show_profiles()
        elif command == 3:
            self.show_settings()
        elif command == 4:
            self.toggle_autostart()
        elif command == 7:
            self.toggle_cards()
        elif command == 5:
            self.disable_all_click_through()
        elif command == 6:
            self.quit()

    def show_selector(self) -> None:
        dialog = WindowSelectorDialog(self.root, self.finder, self.config.windows)
        picked = dialog.show(self.root)
        if not picked:
            return
        added = False
        for process, title in picked:
            safe_process = "" if process in {"?", "<нет доступа>"} else (process or "")
            tracked = TrackedWindow(process=safe_process, title_contains=title or "")
            if any(existing.same_target(tracked) for existing in self.config.windows):
                continue
            self.config.windows.append(tracked)
            try:
                card = CardWnd(self, tracked, 0, 0)
            except OSError:
                self.config.windows.remove(tracked)
                continue
            x, y = self._card_pos(tracked, len(self.cards))
            current_w, current_h = card._size()
            winui.move_window(card.hwnd, x, y, current_w, current_h)
            card.apply_size(tracked.width)
            if not self._cards_hidden:
                winui.show_window(card.hwnd)
            card.update_thumb_geometry()
            self.cards.append(card)
            added = True
        if added:
            self.save_config()
            self.apply_topmost()
            self._do_refresh()

    def show_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.config)
        if not dialog.show(self.root):
            return
        if not self.autostart.set_enabled(self.config.autostart):
            self.config.autostart = self.autostart.is_enabled()
        winui.kill_timer(self.panel.hwnd, TIMER_CHANGE)
        winui.set_timer(self.panel.hwnd, TIMER_CHANGE, int(self.config.change_interval_sec * 1000))
        self.apply_config()
        self.save_config()

    def toggle_autostart(self) -> None:
        enabled = not self.config.autostart
        if self.autostart.set_enabled(enabled):
            self.config.autostart = enabled
            self.save_config()

    def show_filter_dialog(self, card: CardWnd) -> None:
        self.restore_parked_source(card, activate=False)
        dialog = FilterDialog(self.root, card.tracked)
        if not dialog.show(self.root):
            return
        process, title_contains = dialog.values()
        if not process and not title_contains:
            return
        old_hwnd = card.src_hwnd
        card.tracked.process = process
        card.tracked.title_contains = title_contains
        if old_hwnd:
            self.detector.forget(old_hwnd)
            self._capture_failures.pop(old_hwnd, None)
            self._capture_notified.discard(old_hwnd)
            self._detector_quiet.pop(old_hwnd, None)
            self._detector_next_due.pop(old_hwnd, None)
        if self.big is not None and self.big.card is card:
            self.close_fullscreen()
        card.detach()
        self.save_config()
        self._do_refresh()

    def show_crop_dialog(self, card: CardWnd) -> None:
        dialog = CropDialog(self.root, card.tracked, card.source_size_for_crop())
        if not dialog.show(self.root):
            return
        card.tracked.crop = dialog.crop()
        self.save_config()
        card.apply_size()
        card.update_thumb_geometry()

    def quit(self) -> None:
        self._shutting_down = True
        self.save_config()
        self._stop_deferred_pump()
        for hotkey_id in tuple(self._registered_hotkeys):
            winui.unregister_hotkey(self.panel.hwnd, hotkey_id)
        self._registered_hotkeys.clear()
        winui.kill_timer(self.panel.hwnd, TIMER_REFRESH)
        winui.kill_timer(self.panel.hwnd, TIMER_CHANGE)
        winui.kill_timer(self.panel.hwnd, TIMER_SAVE_CONFIG)
        winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
        winui.kill_timer(self.panel.hwnd, TIMER_STARTUP)
        winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)
        self.detector.close()
        self.close_fullscreen()
        for card in self.cards:
            self.restore_parked_source(card, activate=False)
            card.destroy()
        self.cards = []
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.panel.hwnd:
            winui.destroy_window(self.panel.hwnd)
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def make_button(parent, text, command, **kwargs) -> tk.Button:
    options = dict(
        relief="flat",
        bd=0,
        bg="#2b2c31",
        fg="#dfe0e3",
        activebackground="#35363c",
        activeforeground="#dfe0e3",
        font=("Segoe UI", 9),
        padx=12,
        pady=4,
        cursor="hand2",
        takefocus=0,
    )
    options.update(kwargs)
    return tk.Button(parent, text=text, command=command, **options)


def make_check(parent, text, variable) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg="#1f2023",
        fg="#dfe0e3",
        activebackground="#1f2023",
        activeforeground="#dfe0e3",
        selectcolor="#26272b",
        highlightthickness=0,
        font=("Segoe UI", 9),
        anchor="w",
        cursor="hand2",
        takefocus=0,
    )


def make_spin(parent, variable, minimum, maximum, width=8, increment=1):
    return tk.Spinbox(
        parent,
        from_=minimum,
        to=maximum,
        increment=increment,
        textvariable=variable,
        width=width,
        bg="#26272b",
        fg="#dfe0e3",
        insertbackground="#dfe0e3",
        buttonbackground="#26272b",
        relief="flat",
        highlightthickness=1,
        highlightbackground="#33343a",
        highlightcolor="#4f8cff",
        font=("Segoe UI", 9),
        takefocus=0,
    )


def center_on_screen(widget) -> None:
    widget.update_idletasks()
    left, top, right, bottom = winapi.work_area()
    width = widget.winfo_reqwidth()
    height = widget.winfo_reqheight()
    x = left + max(0, (right - left - width) // 2)
    y = top + max(0, (bottom - top - height) // 2)
    widget.geometry(f"+{x}+{y}")


class WindowSelectorDialog:
    def __init__(self, master: tk.Misc, finder: WindowFinder, existing: list[TrackedWindow]):
        self.result: list[tuple[str, str]] | None = None
        self.finder = finder
        self.existing = existing
        self.candidates: list[WindowCandidate] = []
        self.shown: list[WindowCandidate] = []

        self.top = tk.Toplevel(master)
        self.top.title("Выбор окон для наблюдения")
        self.top.configure(bg="#1f2023")
        self.top.resizable(False, False)
        self.top.attributes("-topmost", True)

        tk.Label(
            self.top, text="Выберите окна для наблюдения", bg="#1f2023", fg="#dfe0e3",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))

        filter_row = tk.Frame(self.top, bg="#1f2023")
        filter_row.pack(fill="x", padx=14)
        tk.Label(filter_row, text="Фильтр:", bg="#1f2023", fg="#8a8b91", font=("Segoe UI", 9)).pack(side="left")
        self.filter_var = tk.StringVar()
        entry = tk.Entry(
            filter_row,
            textvariable=self.filter_var,
            bg="#26272b",
            fg="#dfe0e3",
            insertbackground="#dfe0e3",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#33343a",
            highlightcolor="#4f8cff",
            font=("Segoe UI", 9),
        )
        entry.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=2)
        entry.bind("<KeyRelease>", lambda _e: self._load())

        list_frame = tk.Frame(self.top, bg="#1f2023")
        list_frame.pack(fill="both", expand=True, padx=14, pady=8)
        self.listbox = tk.Listbox(
            list_frame,
            height=16,
            width=74,
            selectmode="extended",
            bg="#26272b",
            fg="#dfe0e3",
            selectbackground="#4f8cff",
            selectforeground="#ffffff",
            activestyle="none",
            highlightthickness=1,
            highlightbackground="#33343a",
            relief="flat",
            font=("Segoe UI", 9),
        )
        scrollbar = tk.Scrollbar(list_frame, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.listbox.bind("<Double-Button-1>", lambda _e: self._add())

        tk.Label(
            self.top,
            text="Ctrl / Shift — выбрать несколько. Двойной клик — добавить.",
            bg="#1f2023",
            fg="#8a8b91",
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14)

        buttons = tk.Frame(self.top, bg="#1f2023")
        buttons.pack(fill="x", padx=14, pady=10)
        make_button(buttons, "Добавить", self._add).pack(side="right", padx=(6, 0))
        make_button(buttons, "Отмена", self._cancel).pack(side="right")
        make_button(buttons, "Обновить список", self._load).pack(side="left")

        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.top.bind("<Return>", lambda _e: self._add())

        self._load()

    def _load(self) -> None:
        self.candidates = [
            candidate
            for candidate in self.finder.list_windows()
            if not any(self.finder.matches(candidate, tracked) for tracked in self.existing)
        ]
        needle = self.filter_var.get().strip().lower()
        self.shown = []
        self.listbox.delete(0, "end")
        for candidate in self.candidates:
            display = f"{candidate.process_name or '?'} — {candidate.title}"
            if needle and needle not in display.lower():
                continue
            self.shown.append(candidate)
            self.listbox.insert("end", display)

    def _add(self) -> None:
        indexes = self.listbox.curselection()
        if indexes:
            process_counts: dict[str, int] = {}
            for candidate in self.candidates:
                key = (candidate.process_name or "").casefold()
                process_counts[key] = process_counts.get(key, 0) + 1
            result: list[tuple[str, str]] = []
            for index in indexes:
                if index >= len(self.shown):
                    continue
                candidate = self.shown[index]
                process = candidate.process_name or ""
                # A single window of a process is more robust when tracked by process only.
                # Multiple same-process windows need a title discriminator.
                title = candidate.title if (process in {"?", "<нет доступа>"} or process_counts.get(process.casefold(), 0) > 1) else ""
                result.append((process, title))
            self.result = result
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self, master: tk.Misc) -> list[tuple[str, str]] | None:
        center_on_screen(self.top)
        self.top.grab_set()
        master.wait_window(self.top)
        return self.result


class SettingsDialog:
    def __init__(self, master: tk.Misc, config: AppConfig):
        self.config = config
        self.saved = False

        self.top = tk.Toplevel(master)
        self.top.title("Настройки — LookUp Windows")
        self.top.configure(bg="#1f2023")
        self.top.resizable(False, False)
        self.top.attributes("-topmost", True)

        self.opacity_var = tk.IntVar(value=round(config.opacity * 100))
        self.topmost_var = tk.BooleanVar(value=config.always_on_top)
        self.autostart_var = tk.BooleanVar(value=config.autostart)
        self.refind_var = tk.BooleanVar(value=config.auto_refind)
        self.restore_var = tk.BooleanVar(value=config.restore_minimized)
        self.detect_var = tk.BooleanVar(value=config.change_detection)
        self.interval_var = tk.DoubleVar(value=config.change_interval_sec)
        self.threshold_var = tk.IntVar(value=round(config.change_threshold * 100))
        self.sound_var = tk.BooleanVar(value=config.notify_sound)
        self.return_var = tk.BooleanVar(value=config.notify_window_return)
        self.hotkeys_var = tk.BooleanVar(value=config.hotkeys_enabled)
        self.first_run_var = tk.BooleanVar(value=config.first_run_selector)

        opacity_row = tk.Frame(self.top, bg="#1f2023")
        opacity_row.pack(fill="x", padx=14, pady=(14, 0))
        tk.Label(opacity_row, text="Прозрачность:", bg="#1f2023", fg="#dfe0e3", font=("Segoe UI", 9)).pack(side="left")
        scale = tk.Scale(
            opacity_row,
            from_=30,
            to=100,
            orient="horizontal",
            variable=self.opacity_var,
            bg="#1f2023",
            fg="#dfe0e3",
            troughcolor="#26272b",
            highlightthickness=0,
            showvalue=False,
            length=200,
            command=self._on_opacity,
        )
        scale.pack(side="left", padx=(8, 6))
        self._opacity_label = tk.Label(
            opacity_row, text=f"{self.opacity_var.get()}%", bg="#1f2023", fg="#8a8b91",
            font=("Segoe UI", 9), width=5,
        )
        self._opacity_label.pack(side="left")

        make_check(self.top, "Поверх остальных окон", self.topmost_var).pack(fill="x", padx=14, pady=(12, 0))
        make_check(self.top, "Запускать вместе с Windows", self.autostart_var).pack(fill="x", padx=14)
        make_check(self.top, "Автоматически искать пропавшие окна", self.refind_var).pack(fill="x", padx=14)
        make_check(self.top, "Разворачивать свёрнутые окна", self.restore_var).pack(fill="x", padx=14)
        make_check(self.top, "Детект изменений в окнах", self.detect_var).pack(fill="x", padx=14, pady=(8, 0))

        detect_row = tk.Frame(self.top, bg="#1f2023")
        detect_row.pack(fill="x", padx=34, pady=(2, 0))
        tk.Label(detect_row, text="Интервал, сек:", bg="#1f2023", fg="#8a8b91", font=("Segoe UI", 8)).pack(side="left")
        make_spin(detect_row, self.interval_var, 1.0, 60.0, width=6, increment=0.5).pack(side="left", padx=(6, 14))
        tk.Label(detect_row, text="Порог, %:", bg="#1f2023", fg="#8a8b91", font=("Segoe UI", 8)).pack(side="left")
        make_spin(detect_row, self.threshold_var, 1, 50, width=5).pack(side="left", padx=(6, 0))
        make_check(self.top, "Звуковой сигнал при изменении", self.sound_var).pack(fill="x", padx=34)
        make_check(self.top, "Уведомлять, когда пропавшее окно снова найдено", self.return_var).pack(fill="x", padx=14, pady=(8, 0))
        make_check(self.top, "Глобальные hotkeys (Ctrl+Alt+A/H/S/C)", self.hotkeys_var).pack(fill="x", padx=14)
        make_check(self.top, "Открывать выбор окон при первом запуске", self.first_run_var).pack(fill="x", padx=14)

        buttons = tk.Frame(self.top, bg="#1f2023")
        buttons.pack(fill="x", padx=14, pady=14)
        make_button(buttons, "Сохранить", self._save).pack(side="right", padx=(6, 0))
        make_button(buttons, "Отмена", self._cancel).pack(side="right")

        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())

    def _on_opacity(self, value) -> None:
        self._opacity_label.configure(text=f"{int(float(value))}%")

    def _int(self, variable, fallback: int) -> int:
        try:
            return int(variable.get())
        except (ValueError, tk.TclError):
            return fallback

    def _float(self, variable, fallback: float) -> float:
        try:
            return float(variable.get())
        except (ValueError, tk.TclError):
            return fallback

    def _save(self) -> None:
        config = self.config
        config.opacity = self._int(self.opacity_var, round(config.opacity * 100)) / 100
        config.always_on_top = bool(self.topmost_var.get())
        config.autostart = bool(self.autostart_var.get())
        config.auto_refind = bool(self.refind_var.get())
        config.restore_minimized = bool(self.restore_var.get())
        config.change_detection = bool(self.detect_var.get())
        config.change_interval_sec = self._float(self.interval_var, config.change_interval_sec)
        config.change_threshold = self._int(self.threshold_var, round(config.change_threshold * 100)) / 100
        config.notify_sound = bool(self.sound_var.get())
        config.notify_window_return = bool(self.return_var.get())
        config.hotkeys_enabled = bool(self.hotkeys_var.get())
        config.first_run_selector = bool(self.first_run_var.get())
        config.clamped()
        self.saved = True
        self.top.destroy()

    def _cancel(self) -> None:
        self.top.destroy()

    def show(self, master: tk.Misc) -> bool:
        center_on_screen(self.top)
        self.top.grab_set()
        master.wait_window(self.top)
        return self.saved


class FilterDialog:
    def __init__(self, master: tk.Misc, tracked: TrackedWindow):
        self.saved = False

        self.top = tk.Toplevel(master)
        self.top.title("Фильтр окна")
        self.top.configure(bg="#1f2023")
        self.top.resizable(False, False)
        self.top.attributes("-topmost", True)

        tk.Label(
            self.top,
            text="Окно ищется по имени процесса и подстроке заголовка.\n"
            "Если заголовок меняется, оставьте только постоянную часть.",
            bg="#1f2023",
            fg="#8a8b91",
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 8))

        self.process_var = tk.StringVar(value=tracked.process)
        self.title_var = tk.StringVar(value=tracked.title_contains)

        process_row = tk.Frame(self.top, bg="#1f2023")
        process_row.pack(fill="x", padx=14)
        tk.Label(process_row, text="Процесс:", bg="#1f2023", fg="#dfe0e3", font=("Segoe UI", 9), width=16, anchor="w").pack(side="left")
        self._entry(process_row, self.process_var).pack(side="left", fill="x", expand=True)

        title_row = tk.Frame(self.top, bg="#1f2023")
        title_row.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(title_row, text="Заголовок содержит:", bg="#1f2023", fg="#dfe0e3", font=("Segoe UI", 9), width=16, anchor="w").pack(side="left")
        self._entry(title_row, self.title_var).pack(side="left", fill="x", expand=True)

        buttons = tk.Frame(self.top, bg="#1f2023")
        buttons.pack(fill="x", padx=14, pady=12)
        make_button(buttons, "Сохранить", self._save_ok).pack(side="right", padx=(6, 0))
        make_button(buttons, "Отмена", self._cancel).pack(side="right")

        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.top.bind("<Return>", lambda _e: self._save_ok())

    def _entry(self, parent, variable) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg="#26272b",
            fg="#dfe0e3",
            insertbackground="#dfe0e3",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#33343a",
            highlightcolor="#4f8cff",
            font=("Segoe UI", 9),
        )

    def _save_ok(self) -> None:
        self.saved = True
        self.top.destroy()

    def _cancel(self) -> None:
        self.top.destroy()

    def values(self) -> tuple[str, str]:
        return (self.process_var.get().strip(), self.title_var.get().strip())

    def show(self, master: tk.Misc) -> bool:
        center_on_screen(self.top)
        self.top.grab_set()
        master.wait_window(self.top)
        return self.saved


class ProfilesDialog:
    def __init__(self, master: tk.Misc, names: list[str]):
        self.action = ""
        self.name = ""
        self.top = tk.Toplevel(master)
        self.top.title("Профили — LookUp Windows")
        self.top.configure(bg="#1f2023")
        self.top.resizable(False, False)
        self.top.attributes("-topmost", True)

        tk.Label(
            self.top,
            text="Профиль — сохранённый снимок набора окон, crop, размеров и позиций.",
            bg="#1f2023",
            fg="#8a8b91",
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14, pady=(12, 8))

        frame = tk.Frame(self.top, bg="#1f2023")
        frame.pack(fill="both", expand=True, padx=14)
        self.listbox = tk.Listbox(
            frame, width=46, height=10, bg="#26272b", fg="#dfe0e3",
            selectbackground="#4f8cff", relief="flat", highlightthickness=1,
            highlightbackground="#33343a", font=("Segoe UI", 9),
        )
        for name in names:
            self.listbox.insert("end", name)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._choose("load"))

        buttons = tk.Frame(self.top, bg="#1f2023")
        buttons.pack(fill="x", padx=14, pady=12)
        make_button(buttons, "Сохранить текущий...", lambda: self._choose("save")).pack(side="left")
        make_button(buttons, "Удалить", lambda: self._choose("delete")).pack(side="left", padx=(6, 0))
        make_button(buttons, "Загрузить", lambda: self._choose("load")).pack(side="right", padx=(6, 0))
        make_button(buttons, "Закрыть", self._cancel).pack(side="right")
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())

    def _selected(self) -> str:
        selected = self.listbox.curselection()
        if not selected:
            return ""
        return str(self.listbox.get(selected[0]))

    def _choose(self, action: str) -> None:
        name = self._selected()
        if action in {"load", "delete"} and not name:
            return
        self.action = action
        self.name = name
        self.top.destroy()

    def _cancel(self) -> None:
        self.action = ""
        self.name = ""
        self.top.destroy()

    def show(self, master: tk.Misc) -> tuple[str, str]:
        center_on_screen(self.top)
        self.top.grab_set()
        master.wait_window(self.top)
        return self.action, self.name


class CropDialog:
    def __init__(self, master: tk.Misc, tracked: TrackedWindow, source_size: tuple[int, int] | None):
        self.saved = False
        crop = tracked.crop if (tracked.crop is not None and tracked.crop.is_valid()) else None

        self.top = tk.Toplevel(master)
        self.top.title("Область окна (crop)")
        self.top.configure(bg="#1f2023")
        self.top.resizable(False, False)
        self.top.attributes("-topmost", True)

        self.enabled_var = tk.BooleanVar(value=crop is not None)
        self.relative_var = tk.BooleanVar(value=bool(crop and crop.is_relative()))
        self.source_size = source_size
        self._last_relative = bool(self.relative_var.get())
        default_w = source_size[0] if source_size else 0
        default_h = source_size[1] if source_size else 0
        if crop and crop.is_relative():
            values = (crop.x * 100, crop.y * 100, crop.width * 100, crop.height * 100)
        elif crop:
            values = (crop.x, crop.y, crop.width, crop.height)
        else:
            values = (0, 0, default_w, default_h)
        self.x_var = tk.DoubleVar(value=values[0])
        self.y_var = tk.DoubleVar(value=values[1])
        self.w_var = tk.DoubleVar(value=values[2])
        self.h_var = tk.DoubleVar(value=values[3])

        make_check(self.top, "Показывать только часть окна", self.enabled_var).pack(anchor="w", padx=14, pady=(12, 4))
        make_check(self.top, "Относительно размера окна (%)", self.relative_var).pack(anchor="w", padx=14, pady=(0, 6))

        grid = tk.Frame(self.top, bg="#1f2023")
        grid.pack(padx=20, anchor="w")
        labels = ["X / left:", "Y / top:", "Ширина:", "Высота:"]
        variables = [self.x_var, self.y_var, self.w_var, self.h_var]
        self._labels: list[tk.Label] = []
        self._spins: list[tk.Spinbox] = []
        for row_index, (label, variable) in enumerate(zip(labels, variables)):
            widget = tk.Label(grid, text=label, bg="#1f2023", fg="#dfe0e3", font=("Segoe UI", 9), width=12, anchor="w")
            widget.grid(row=row_index, column=0, sticky="w", pady=2)
            spin = make_spin(grid, variable, 0, 32767, width=10, increment=1)
            spin.grid(row=row_index, column=1, sticky="w")
            self._labels.append(widget)
            self._spins.append(spin)

        if source_size:
            info = f"Размер источника: {source_size[0]} x {source_size[1]} px"
        else:
            info = "Источник недоступен — для relative crop можно использовать проценты"
        tk.Label(self.top, text=info, bg="#1f2023", fg="#8a8b91", font=("Segoe UI", 8)).pack(
            anchor="w", padx=14, pady=(8, 0)
        )
        tk.Label(
            self.top,
            text="Relative crop устойчив к изменению размера окна и рекомендуется для RDP/1С/браузера.",
            bg="#1f2023", fg="#8a8b91", font=("Segoe UI", 8), wraplength=390, justify="left"
        ).pack(anchor="w", padx=14, pady=(4, 0))

        buttons = tk.Frame(self.top, bg="#1f2023")
        buttons.pack(fill="x", padx=14, pady=12)
        make_button(buttons, "Сохранить", self._save).pack(side="right", padx=(6, 0))
        make_button(buttons, "Отмена", self._cancel).pack(side="right")

        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())

        self._crop: CropRect | None = crop
        self.enabled_var.trace_add("write", lambda *_args: self._refresh_mode())
        self.relative_var.trace_add("write", lambda *_args: self._refresh_mode())
        self._refresh_mode()

    def _refresh_mode(self) -> None:
        state = "normal" if self.enabled_var.get() else "disabled"
        relative = bool(self.relative_var.get())
        if relative != self._last_relative and self.source_size:
            try:
                sw, sh = self.source_size
                x, y, w, h = [float(v.get()) for v in (self.x_var, self.y_var, self.w_var, self.h_var)]
                if relative:
                    self.x_var.set(round(x * 100.0 / sw, 2) if sw else 0)
                    self.y_var.set(round(y * 100.0 / sh, 2) if sh else 0)
                    self.w_var.set(round(w * 100.0 / sw, 2) if sw else 0)
                    self.h_var.set(round(h * 100.0 / sh, 2) if sh else 0)
                else:
                    self.x_var.set(round(x * sw / 100.0))
                    self.y_var.set(round(y * sh / 100.0))
                    self.w_var.set(round(w * sw / 100.0))
                    self.h_var.set(round(h * sh / 100.0))
            except (ValueError, tk.TclError):
                pass
        self._last_relative = relative
        for spin in self._spins:
            spin.configure(
                state=state,
                from_=0,
                to=100 if relative else 32767,
                increment=0.5 if relative else 1,
            )

    def _save(self) -> None:
        self._crop = None
        if self.enabled_var.get():
            try:
                values = [float(v.get()) for v in (self.x_var, self.y_var, self.w_var, self.h_var)]
                if self.relative_var.get():
                    crop = CropRect(
                        x=values[0] / 100.0,
                        y=values[1] / 100.0,
                        width=values[2] / 100.0,
                        height=values[3] / 100.0,
                        mode="relative",
                    )
                else:
                    crop = CropRect(x=values[0], y=values[1], width=values[2], height=values[3])
                if crop.is_valid():
                    self._crop = crop
            except (ValueError, tk.TclError):
                pass
        self.saved = True
        self.top.destroy()

    def _cancel(self) -> None:
        self.top.destroy()

    def crop(self) -> CropRect | None:
        return self._crop

    def show(self, master: tk.Misc) -> bool:
        center_on_screen(self.top)
        self.top.grab_set()
        master.wait_window(self.top)
        return self.saved

def main() -> None:
    winui.enable_dpi_awareness()
    if not winui.acquire_single_instance("Local\\LookUpWindows-SingleInstance"):
        if not winui.wake_first_instance():
            winui.message_box("LookUp Windows уже запущен, но первый экземпляр не отвечает.")
        return
    root = tk.Tk()
    root.withdraw()
    ico_path = app_dir() / "app.ico"
    # The native panel/cards do not need a Tk icon.  Avoid regenerating a disk
    # ICO on every frozen start (especially costly from protected/network paths).
    if not getattr(sys, "frozen", False):
        icon_module.ensure_ico_file(ico_path)
    if ico_path.exists():
        try:
            root.iconbitmap(str(ico_path))
        except tk.TclError:
            pass
    app = App(root)
    app.start()
    app.run()


if __name__ == "__main__":
    main()