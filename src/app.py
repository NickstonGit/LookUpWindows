from __future__ import annotations

import logging
import logging.handlers
import multiprocessing
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
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
from config import (
    APP_VERSION,
    AppConfig,
    AsyncConfigSaver,
    Autostart,
    ConfigService,
    CropRect,
    TrackedWindow,
    app_dir,
    background_mode,
)
from dwm import Thumbnail
from recovery import (
    ParkRecord,
    RecoveryJournal,
    journal_path_for,
    new_executor_identity,
    record_from_state,
)
import restoreguard
from trayicon import TrayIcon
from winapi import AsyncChangeDetector, WindowCandidate, WindowFinder
from winui import (
    DT_CENTER,
    DT_END_ELLIPSIS,
    DT_NOPREFIX,
    DT_SINGLELINE,
    DT_VCENTER,
    DT_WORDBREAK,
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

logger = logging.getLogger("lookupwindows")

# Records handed to the logging worker; see _configure_logging for why an emit
# from a native callback must not touch the file itself.
_log_queue: queue.SimpleQueue = queue.SimpleQueue()
_log_listener: logging.handlers.QueueListener | None = None


def _configure_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) / "LookUpWindows" if local_app_data else app_dir()
    log_dir = base / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "lookupwindows.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        # A rotating handler reads and seeks its own file, and a rollover renames
        # it, so an emit is blocking file I/O.  Native window procedures and timer
        # callbacks log from the dispatch thread, which is exactly where a
        # synchronous disk write may not happen; the queue keeps the write on a
        # worker while the caller only ever hands over a formatted record.
        queue_handler: logging.Handler = logging.handlers.QueueHandler(_log_queue)
        queue_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        logger.addHandler(queue_handler)
        queue_listener = logging.handlers.QueueListener(
            _log_queue, handler, respect_handler_level=True
        )
        queue_listener.start()
        _log_listener = queue_listener
    except OSError:
        # Logging must never prevent the utility from starting.
        logger.addHandler(logging.NullHandler())

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

CTRL_W = 460
CTRL_HEADER_H = 42
CTRL_ROW_H = 38
CTRL_FOOTER_H = 30
CTRL_EMPTY_H = 48
CTRL_MAX_ROWS = 8
CTRL_H = CTRL_HEADER_H  # header height; full panel height is dynamic
BTN_W = 26
WM_MOUSEWHEEL = 0x020A
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

# Shutdown restore barrier and the handover that follows it.  The barrier stays
# short: a stalled foreign window must not delay the exit, because the handover
# below is what actually guarantees the restore.  The handover timeout is
# generous because a frozen build has to unpack itself before the guardian
# answers - paying for it only happens when a handover is actually needed.
SHUTDOWN_RESTORE_DEADLINE_SEC = 1.5
RESTORE_HANDOVER_TIMEOUT_SEC = 15.0
RESTORE_FALLBACK_WAIT_SEC = 25.0
# How long the startup pass may work on journaled obligations before it hands
# them to a guardian of the same journal.  It never discards anything: a record
# that outlives this budget still has a live executor (the guardian this starts,
# or the shutdown handover), it is simply not worth blocking a background worker
# on - and not worth blocking the UI either.
STARTUP_RECOVERY_BUDGET_SEC = 20.0
STARTUP_GUARDIAN_ATTEMPTS = 3
# The recovery supervisor is long-lived: it watches every outstanding obligation of
# this journal and the liveness of the executor that serves them, and it ends only
# when there is provably nothing left to execute.  The two intervals are its idle
# poll (while an executor is alive) and the ceiling of the backoff it uses when the
# journal itself cannot be read.
SUPERVISOR_IDLE_POLL_SEC = 5.0
SUPERVISOR_POLL_CEILING_SEC = 30.0
# A park is only allowed once a confirmed executor exists for it, so a hard kill
# still leaves somebody who restores the window.  The supervisor tries this before
# every first park and keeps trying until it succeeds; the park itself waits for
# this bounded confirmation instead of moving a window nobody can put back.
SESSION_GUARDIAN_CONFIRM_TIMEOUT_SEC = 20.0
# The shutdown barrier runs on its own worker, started with the application, so
# the UI thread only ever *publishes* the request.
SHUTDOWN_HANDOFF_TIMEOUT_SEC = 120.0
# How long ``quit`` waits for the shutdown worker to say it is waiting for a
# request.  It is started with the application, so this is a formality; the wait is
# bounded because it happens on the UI thread.
SHUTDOWN_WORKER_READY_TIMEOUT_SEC = 2.0

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
MENU_DISABLE_PIP = 13

HOTKEY_ADD_FOREGROUND = 1
HOTKEY_TOGGLE_PANEL = 2
HOTKEY_SELECTOR = 3
HOTKEY_DISABLE_CLICKTHROUGH = 4

_handlers: dict[int, object] = {}
_procs: dict[str, winui.WNDPROC] = {}


@dataclass
class RecoveryEntry:
    state: winapi.ParkedWindowState
    label: str
    attempts: int = 0
    retry_at: float = 0.0
    needs_retry: bool = False
    # The instant of the durable record this entry belongs to.  Carrying it here
    # is what lets the record be retired from a worker: reading it back from the
    # journal would put file I/O on the UI/native dispatch thread, and dropping
    # "whatever record has this HWND" would let a newer obligation be erased by
    # a decision made about an older one.
    recorded_at: float | None = None


def _dispatch(class_name: str):
    proc = _procs.get(class_name)
    if proc is not None:
        return proc

    def wnd_proc(hwnd, message, wparam, lparam):
        try:
            handler = _handlers.get(int(hwnd or 0))
            if handler is not None:
                result = handler.on_message(message, wparam, lparam)
                if result is not None:
                    return result
        except Exception:
            logger.exception(
                "Unhandled WNDPROC error: class=%s hwnd=%s message=%s",
                class_name,
                int(hwnd or 0),
                int(message),
            )
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
        self._orphan_restore_attempts = 0
        self._orphan_retry_at = 0.0
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
            # SetWindowRgn drops the update region Windows queued for the resize,
            # so a layered card would keep stale pixels on the freshly exposed
            # strip (right border, header buttons) until something else happens to
            # invalidate it.  Repaint explicitly after the new region is applied.
            self._invalidate()

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
            monitor_height = max(CARD_HEADER_H + 4, wa_bottom - wa_top)
            height = min(self.desired_height(width), monitor_height)

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
            self.app.defer(self.app.disable_card, self)
        elif width - 44 <= x <= width - 24 and 2 <= y <= 22:
            self.app.defer(self.app.open_fullscreen, self)
        elif self._in_thumb(x, y):
            self.app.defer(self.app.toggle_card_source, self)

    def set_candidate(self, candidate: WindowCandidate | None, foreground_hwnd: int) -> None:
        previous_candidate = self.candidate
        previous_rect = previous_candidate.info.rect if previous_candidate is not None else None
        previous_source_size = self._source_size
        previous_visual = (self._title, self._minimized, self._active, self._placeholder)
        previous_hwnd = self.src_hwnd
        self.candidate = candidate

        if candidate is None:
            had_runtime_state = bool(
                previous_candidate is not None
                or self.src_hwnd
                or self._parked_source is not None
                or self._source_action_pending is not None
            )
            if had_runtime_state:
                self._source_action_id += 1
                self._source_action_pending = None
            if self._parked_source is not None and self.src_hwnd and winapi.is_window(self.src_hwnd):
                # Never orphan a still-running application outside the virtual desktop
                # if matching/revalidation temporarily fails.
                self.app.restore_parked_source(self, activate=False)
            if self._ever_bound:
                self._was_missing = True
            if self.src_hwnd or self.thumb is not None:
                self.detach()
            self._minimized = False
            self._parked_source = None
            self._parked_seen_not_foreground = False
            self._active = False
            self._capture_issue = False
            # The configured target is the strict process/title filter; the live
            # binding is what the card is currently showing.
            self.tracked.source_hwnd = None
            self._title = self.tracked.display_name()
            self._placeholder = "Окно недоступно\nОжидание..."
            if had_runtime_state or previous_visual != (
                self._title, self._minimized, self._active, self._placeholder
            ):
                self._invalidate()
            return

        self._title = candidate.title or self.tracked.display_name()
        # Keep the configured entry pinned to the window this card really shows.
        # Several entries may share one process and an identical title (the same
        # project opened twice); the live HWND is what tells them apart, and the
        # window selector uses it to offer only the still-unused windows.
        self.tracked.source_hwnd = candidate.hwnd
        previous_identity = None
        if previous_candidate is not None:
            previous_identity = (
                previous_candidate.info.pid,
                previous_candidate.info.class_name,
                previous_candidate.info.process_created,
            )
        candidate_identity = (
            candidate.info.pid,
            candidate.info.class_name,
            candidate.info.process_created,
        )
        source_changed = previous_hwnd != candidate.hwnd or (
            previous_identity is not None and previous_identity != candidate_identity
        )
        if source_changed:
            hint_changed = False
            if candidate.title and self.tracked.title_hint != candidate.title:
                self.tracked.title_hint = candidate.title
                hint_changed = True
            if candidate.info.class_name and self.tracked.class_hint != candidate.info.class_name:
                self.tracked.class_hint = candidate.info.class_name
                hint_changed = True
            if hint_changed:
                self.app.schedule_save_config()
            self._source_action_id += 1
            self._source_action_pending = None
            if self._parked_source is not None and previous_hwnd:
                self.app.restore_parked_source(self, activate=False)
            self._parked_source = None
            self._parked_seen_not_foreground = False
            if previous_hwnd:
                self.app.detector.forget(previous_hwnd)
                self.app._capture_failures.pop(previous_hwnd, None)
                self.app._capture_notified.discard(previous_hwnd)
                self.app._detector_quiet.pop(previous_hwnd, None)
                self.app._detector_next_due.pop(previous_hwnd, None)
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

        # Query DWM source size only when the bound window or its outer geometry
        # changed.  Content updates flow through the thumbnail automatically and
        # do not require a DwmQueryThumbnailSourceSize call every refresh.
        rect_changed = previous_rect != candidate.info.rect
        if self.thumb is not None and (source_changed or rect_changed or self._source_size == (0, 0)):
            size = self.thumb.source_size()
            if size[0] > 0 and size[1] > 0:
                self._source_size = size

        source_size_changed = self._source_size != previous_source_size
        if source_size_changed and self._source_size != (0, 0):
            self.apply_size()

        if self._minimized:
            self._placeholder = "Окно свернуто"
        elif self.thumb is None:
            self._placeholder = "Не удалось создать превью (DWM)"
        else:
            self._placeholder = ""

        current_visual = (self._title, self._minimized, self._active, self._placeholder)
        geometry_changed = source_changed or source_size_changed or rect_changed
        if geometry_changed:
            self.update_thumb_geometry()
        if geometry_changed or current_visual != previous_visual:
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
        area_h = max(CARD_HEADER_H + 4, bottom - top)
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
        changed = bool(changed)
        if changed != self._changed:
            self._changed = changed
            self._invalidate()

    def is_changed(self) -> bool:
        return self._changed

    def _invalidate(self) -> None:
        winui.invalidate(self.hwnd)

    def destroy(self) -> None:
        self._source_action_id += 1
        self._source_action_pending = None
        if self._parked_source is not None and not self.app._shutting_down:
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
        self._shape_size: tuple[int, int] = (0, 0)

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

    def _show_menu(self) -> None:
        if self.hwnd and winui.track_popup_menu([(1, "Закрыть", False)], self.hwnd):
            self.close()

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
            self.app.defer(self._show_menu)
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
        size = (width, height)
        if width > 0 and height > 0 and size != self._shape_size:
            winui.set_round_region(self.hwnd, width, height, BIG_RADIUS)
            self._shape_size = size
            winui.invalidate(self.hwnd)

    def _size(self) -> tuple[int, int]:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
        return (right - left, bottom - top)

    def _paint(self) -> None:
        left, top, right, bottom = winui.get_client_rect(self.hwnd)
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
        self._scroll = 0
        self._wake_message = winui.register_wake_message()
        self._quit_message = winui.register_quit_message()

        hwnd = winui.create_window(
            "WPCtrl",
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            WS_POPUP | WS_CLIPSIBLINGS,
            x,
            y,
            CTRL_W,
            self.desired_height(),
        )
        if not hwnd:
            raise OSError("control window creation failed")
        self.hwnd = hwnd
        _handlers[hwnd] = self
        winui.set_alpha(self.hwnd, app.config.opacity)

    def desired_height(self) -> int:
        count = len(self.app.config.windows)
        body_height = CTRL_EMPTY_H if count == 0 else min(count, CTRL_MAX_ROWS) * CTRL_ROW_H
        return CTRL_HEADER_H + body_height + CTRL_FOOTER_H

    def sync_layout(self) -> None:
        if not self.hwnd:
            return
        count = len(self.app.config.windows)
        max_scroll = max(0, count - CTRL_MAX_ROWS)
        self._scroll = max(0, min(self._scroll, max_scroll))
        left, top, right, _bottom = winui.get_window_rect(self.hwnd)
        width = max(CTRL_W, right - left)
        height = self.desired_height()
        wa_left, wa_top, wa_right, wa_bottom = winapi.work_area_for_point(left, top)
        left = max(wa_left, min(wa_right - width, left))
        top = max(wa_top, min(wa_bottom - height, top))
        winui.move_window(self.hwnd, left, top, width, height)
        winui.invalidate(self.hwnd)

    def on_message(self, message, wparam, lparam):
        if self._wake_message and message == self._wake_message:
            self.app.defer(self.app.show_panel)
            return 1
        if self._quit_message and message == self._quit_message:
            # Graceful shutdown request (build scripts, smoke tests, installers):
            # run the normal restore path instead of being killed.
            self.app.defer(self.app.quit)
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
        if message == WM_MOUSEWHEEL:
            delta = (int(wparam) >> 16) & 0xFFFF
            if delta & 0x8000:
                delta -= 0x10000
            self._scroll_rows(-1 if delta > 0 else 1)
            return 0
        if message == WM_RBUTTONUP:
            x, y = winui.xy_from_lparam(lparam)
            tracked = self._tracked_at(y)
            if tracked is not None:
                self.app.defer(self.app.tracked_window_menu, tracked)
            else:
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
            (width - BTN_W, 4, width - 4, CTRL_HEADER_H - 4, "✕"),
            (width - 2 * BTN_W - 4, 4, width - BTN_W - 4, CTRL_HEADER_H - 4, "⚙"),
            (width - 3 * BTN_W - 8, 4, width - 2 * BTN_W - 8, CTRL_HEADER_H - 4, "+"),
        ]

    def _visible_windows(self) -> list[TrackedWindow]:
        return self.app.config.windows[self._scroll:self._scroll + CTRL_MAX_ROWS]

    def _tracked_at(self, y: int) -> TrackedWindow | None:
        if y < CTRL_HEADER_H:
            return None
        row = (y - CTRL_HEADER_H) // CTRL_ROW_H
        visible = self._visible_windows()
        if 0 <= row < len(visible):
            row_top = CTRL_HEADER_H + row * CTRL_ROW_H
            if y < row_top + CTRL_ROW_H:
                return visible[row]
        return None

    def _scroll_rows(self, step: int) -> None:
        max_scroll = max(0, len(self.app.config.windows) - CTRL_MAX_ROWS)
        new_scroll = max(0, min(max_scroll, self._scroll + int(step)))
        if new_scroll != self._scroll:
            self._scroll = new_scroll
            winui.invalidate(self.hwnd)

    def _paint(self) -> None:
        width, height = self._size()
        with winui.paint(self.hwnd) as hdc:
            winui.fill_rect(hdc, (0, 0, width, height), BG)
            winui.fill_rect(hdc, (0, 0, width, CTRL_HEADER_H), HEADER_BG)
            winui.frame_rect(hdc, (0, 0, width - 1, height - 1), BORDER)

            enabled_count = sum(1 for item in self.app.config.windows if item.pip_enabled)
            total_count = len(self.app.config.windows)
            buttons_left = width - 3 * BTN_W - 8
            version_width = 96
            version_left = max(175, buttons_left - version_width - 8)
            if not self.app._startup_ready:
                panel_title = f"{APP_NAME} · запуск…"
            else:
                panel_title = f"{APP_NAME} · PiP {enabled_count}/{total_count}"
            winui.draw_text(
                hdc, panel_title, (12, 0, version_left - 8, CTRL_HEADER_H), FG,
                winui.font(13, bold=True),
                DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
            )
            winui.draw_text(
                hdc, APP_VERSION, (version_left, 0, buttons_left - 6, CTRL_HEADER_H), DIM,
                winui.font(8), DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
            )
            for left, top, right, bottom, glyph in self._button_rects():
                winui.draw_text(
                    hdc, glyph, (left, top, right, bottom), FG, winui.font(12),
                    DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                )

            visible = self._visible_windows()
            if not visible:
                winui.draw_text(
                    hdc, "Нет настроенных окон",
                    (12, CTRL_HEADER_H + 4, width - 12, CTRL_HEADER_H + 25),
                    FG, winui.font(11, bold=True),
                    DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                )
                winui.draw_text(
                    hdc, "Нажмите +, чтобы добавить окно для PiP",
                    (12, CTRL_HEADER_H + 23, width - 12, CTRL_HEADER_H + CTRL_EMPTY_H - 4),
                    DIM, winui.font(9),
                    DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
                )
            else:
                for row, tracked in enumerate(visible):
                    top = CTRL_HEADER_H + row * CTRL_ROW_H
                    bottom = top + CTRL_ROW_H
                    if row % 2:
                        winui.fill_rect(hdc, (1, top, width - 1, bottom), CARD_BG)
                    winui.fill_rect(hdc, (1, bottom - 1, width - 1, bottom), BORDER)

                    box = (12, top + 10, 29, top + 27)
                    if tracked.pip_enabled:
                        winui.fill_rect(hdc, box, ACCENT)
                        winui.draw_text(
                            hdc, "✓", box, FG, winui.font(10, bold=True),
                            DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                        )
                    else:
                        winui.frame_rect(hdc, box, DIM)

                    card = self.app.card_for_tracked(tracked)
                    if not tracked.pip_enabled:
                        status = "PiP выключен"
                        status_color = DIM
                    elif card is not None and card.src_hwnd:
                        status = "PiP включен"
                        status_color = OK
                    else:
                        status = "PiP включен · ожидание окна"
                        status_color = WARN

                    name = self.app.tracked_display_name(tracked)
                    winui.draw_text(
                        hdc, name, (40, top + 3, width - 132, top + 21), FG,
                        winui.font(10, bold=True),
                        DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
                    )
                    details = tracked.process or "Фильтр по заголовку"
                    if tracked.title_contains and tracked.process:
                        details = f"{tracked.process} · {tracked.title_contains}"
                    winui.draw_text(
                        hdc, details, (40, top + 19, width - 132, bottom - 2), DIM,
                        winui.font(8),
                        DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
                    )
                    winui.draw_text(
                        hdc, status, (width - 130, top, width - 10, bottom), status_color,
                        winui.font(8),
                        DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
                    )

            footer_top = height - CTRL_FOOTER_H
            winui.fill_rect(hdc, (1, footer_top, width - 1, height - 1), HEADER_BG)
            footer = f"{enabled_count} из {total_count} PiP включено"
            winui.draw_text(
                hdc, footer, (12, footer_top, width - 12, height), DIM, winui.font(8),
                DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
            )
            if total_count > CTRL_MAX_ROWS:
                page_end = min(total_count, self._scroll + CTRL_MAX_ROWS)
                page = f"{self._scroll + 1}–{page_end} из {total_count} · колесо мыши"
                winui.draw_text(
                    hdc, page, (width - 205, footer_top, width - 12, height), DIM, winui.font(8),
                    DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
                )

    def _on_down(self, lparam) -> None:
        x, y = winui.xy_from_lparam(lparam)
        if y < CTRL_HEADER_H:
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
            return

        tracked = self._tracked_at(y)
        if tracked is not None and 7 <= x <= 34:
            self.app.defer(self.app.toggle_tracked_pip, tracked)

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
        self._capture_binding_notified = False
        self._detector_error_notified = False
        self._detector_quiet: dict[int, int] = {}
        self._detector_next_due: dict[int, float] = {}
        self._registered_hotkeys: set[int] = set()
        self._deferred: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.SimpleQueue()
        self._defer_timer_id: str | None = None
        self._defer_closed = False
        self._defer_idle_passes = 0
        self._config_saver = AsyncConfigSaver(
            self.config_service,
            on_result=lambda ok: self.defer(self._on_config_saved, ok),
        )
        self._recovery_lock = threading.Lock()
        self._recovery_registry: dict[int, RecoveryEntry] = {}
        self._recovery_inflight: set[int] = set()
        # Durable ownership of every window this process has moved off-screen.
        # It is written before each native park and only cleared after a
        # verified restore, so a crash, a forced kill or a shutdown that misses
        # its restore deadline cannot strand a foreign window off-screen.
        self.recovery_journal = RecoveryJournal(journal_path_for(self.config_service.path))
        # Who this run is when it writes recovery records.  The run id is what a
        # record carries as its owner, so a later process can tell "my own
        # obligation" from "somebody else's", and the creation time is what makes
        # the PID in the record an identity instead of a locator.
        self._executor = new_executor_identity("main")
        # Set when a shutdown hands unfinished restores to the guardian process.
        # The reference is kept so the child is not reaped while it works.
        self._recovery_guardian: Any = None
        self._guardian_start_lock = threading.Lock()
        # The long-lived worker that keeps every obligation of this journal
        # attached to a live executor, and the event that stops it.
        self._recovery_supervisor: threading.Thread | None = None
        self._recovery_supervisor_stop = threading.Event()
        self._recovery_supervisor_wanted = threading.Event()
        self._shutdown_requested = threading.Event()
        self._shutdown_worker_ready = threading.Event()
        self._shutdown_worker_started = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_finished = False
        self._source_action_inflight = 0
        self._source_action_idle = threading.Condition(self._recovery_lock)
        self._error_log_at: dict[str, float] = {}
        self._restore_notice_at: dict[int, float] = {}
        self._startup_ready = False
        self._startup_started_at = time.monotonic()
        # Full desktop enumeration is cross-process Win32 work.  Keep it off the
        # UI/native dispatch thread and publish completed snapshots back through
        # the deferred queue.
        self._refind_lock = threading.Lock()
        self._refind_scan_inflight = False
        self._refind_candidates_cache: list[WindowCandidate] | None = None
        self._refind_candidates_at = 0.0
        self._shutting_down = False

        _register_classes()
        ctrl_x, ctrl_y = self._default_ctrl_pos()
        self.panel = ControlWnd(self, ctrl_x, ctrl_y)
        self._build_tray()
        # Card creation, window enumeration and DWM binding are intentionally
        # deferred until the native panel has had a chance to paint.

    def _default_ctrl_pos(self) -> tuple[int, int]:
        count = len(self.config.windows)
        body_height = CTRL_EMPTY_H if count == 0 else min(count, CTRL_MAX_ROWS) * CTRL_ROW_H
        panel_height = CTRL_HEADER_H + body_height + CTRL_FOOTER_H
        if self.config.ctrl_x is not None and self.config.ctrl_y is not None:
            left, top, right, bottom = winapi.work_area_for_point(self.config.ctrl_x, self.config.ctrl_y)
            x = max(left, min(right - CTRL_W, self.config.ctrl_x))
            y = max(top, min(bottom - panel_height, self.config.ctrl_y))
            return (x, y)
        left, top, right, bottom = winapi.work_area()
        return (right - CTRL_W - 12, min(bottom - panel_height, top + 60))
    def _default_card_pos(self, index: int) -> tuple[int, int]:
        ctrl_x, ctrl_y = self._default_ctrl_pos()
        left, top, right, bottom = winapi.work_area_for_point(ctrl_x, ctrl_y)
        x0 = max(left, min(right - CARD_W, ctrl_x + CTRL_W - CARD_W))
        panel_height = self.panel.desired_height() if getattr(self, "panel", None) is not None else CTRL_HEADER_H
        y0 = max(top, ctrl_y + panel_height + 10)
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
                logger.exception("Deferred Tk action failed: %s", getattr(fn, "__name__", repr(fn)))
            except Exception:
                logger.exception("Deferred action failed: %s", getattr(fn, "__name__", repr(fn)))
            processed += 1

        if self._defer_closed:
            return
        if processed:
            self._defer_idle_passes = 0
            delay_ms = 1 if processed == max_batch else 20
        else:
            self._defer_idle_passes = min(4, self._defer_idle_passes + 1)
            delay_ms = (30, 60, 120, 200)[self._defer_idle_passes - 1]
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

    def _log_exception_throttled(self, key: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._error_log_at.get(key, 0.0) < 30.0:
            return
        self._error_log_at[key] = now
        logger.exception(message, *args)

    def start(self) -> None:
        # Keep the management panel hidden on a normal launch.  The tray helper
        # exists already, and PiP cards are shown as soon as startup binding is
        # complete.  One-file builds use the boot splash for immediate feedback.
        self.apply_config()
        self._defer_closed = False
        self._recovery_supervisor_wanted.set()
        self._ensure_recovery_supervisor()
        # The shutdown worker exists before anybody asks to exit, so the exit itself
# never has to be performed on the UI thread (see ``quit``).
        self._start_shutdown_worker(wait=False)
        self._recover_journaled_sources()
        try:
            self._defer_timer_id = self.root.after(10, self._pump_deferred)
        except tk.TclError:
            self._defer_timer_id = None
            self._defer_closed = True
        if self.tray is None:
            # A failed notification-area registration must not leave the app
            # running with every management surface hidden.
            self.show_panel()
        elif background_mode():
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
        self._sync_change_timer()
        # PiP visibility is intentionally independent from panel visibility.
        if not self._cards_hidden:
            for card in self.cards:
                winui.show_window(card.hwnd)
        winui.invalidate(self.panel.hwnd)
        if self.config.first_run_selector and not self.config.windows and not background_mode():
            self.defer(self.show_selector)
        self._close_boot_splash()
        if self._recovery_supervisor_wanted.is_set() and not self._has_live_supervisor():
            # The supervisor is not optional, so its start is retried from here: a
            # transient thread failure must not leave the session without one.
            self._request_recovery_supervisor_from_timer()

    def _has_live_supervisor(self) -> bool:
        with self._recovery_lock:
            thread = self._recovery_supervisor
        return thread is not None and thread.is_alive()

    def _close_boot_splash(self) -> None:
        try:
            import pyi_splash  # type: ignore[import-not-found]
            pyi_splash.close()
        except (ImportError, RuntimeError):
            pass

    def _recover_journaled_sources(self) -> None:
        """Restore windows parked by an earlier LookUp process (startup recovery).

        Runs on a daemon worker: it touches foreign windows with blocking Win32
        calls, which must never happen on the UI/native dispatch thread.  The
        journal is the ownership record that survives a crash or a forced kill,
        so this pass is what makes an unclean exit recoverable.

        The work itself is delegated to :mod:`restoreguard`, which is also what the
        recovery guardian runs after a shutdown handover: a record must not be
        discharged by one path while another path still considers it open.

        The pass is only the first attempt.  Whatever it cannot finish is handed to
        the long-lived supervisor (:meth:`_ensure_recovery_supervisor`), which keeps
        retrying for as long as this process runs - a window belonging to a profile
        that no longer exists must not stay off-screen until the next exit, and a
        guardian that could not be started *yet* must be started as soon as that is
        possible again.
        """

        def worker() -> None:
            try:
                restoreguard.resolve_all(
                    self.recovery_journal,
                    self._executor,
                    context="startup",
                    budget_sec=STARTUP_RECOVERY_BUDGET_SEC,
                )
                resolution = restoreguard.resolve_journal_damage(self.recovery_journal)
            except Exception:  # pragma: no cover - the supervisor must not die
                logger.exception("Startup recovery crashed")
                resolution = None
            if resolution is not None and resolution.recovered:
                self.defer(self._notify_damaged_journal, list(resolution.recovered))
            self._ensure_recovery_supervisor()

        if not self._start_daemon_worker("LookUpWindows-StartupRecovery", worker, ()):
            # Without a worker nobody is executing anything, so the supervisor is
            # not optional: it is retried from the UI's own timer instead.
            logger.error(
                "Startup recovery could not be started; its worker supervisor will be "
                "retried from the UI timer"
            )
            self._recovery_supervisor_wanted.set()
            self._request_recovery_supervisor_from_timer()

    def _request_recovery_supervisor_from_timer(self) -> None:
        """Ask the UI to try again once the startup window has passed.

        A native timer, not a Tk timer: the supervisor start may block on the
        journal, and the UI thread must never wait for it.
        """
        if getattr(self, "panel", None) is None or not self.panel.hwnd:
            return
        winui.set_timer(self.panel.hwnd, TIMER_STARTUP, 250)

    def _ensure_recovery_supervisor(self) -> None:
        """Make sure one long-lived supervisor is watching this journal.

        Called from workers, never from the UI thread: starting it touches the
        journal and spawns a process.
        """
        self._recovery_supervisor_wanted.set()
        with self._recovery_lock:
            thread = self._recovery_supervisor
            if thread is not None and thread.is_alive():
                return
            self._recovery_supervisor_stop.clear()
            try:
                thread = threading.Thread(
                    target=self._recovery_supervisor_worker,
                    name="LookUpWindows-RecoverySupervisor",
                    daemon=True,
                )
                self._recovery_supervisor = thread
                thread.start()
            except Exception:  # pragma: no cover - defensive
                logger.exception("The recovery supervisor could not be started")
                self._recovery_supervisor = None

    def _recovery_supervisor_worker(self) -> None:
        """Keep every outstanding obligation attached to a live executor.

        It watches all of them - not just the ones the startup pass happened to see
        - so a record that belongs to no card and no profile is serviced too, and it
        keeps watching the guardian's liveness: an executor that died is replaced
        with a new one instead of being written off in a log line.  When it cannot
        start a guardian at all it executes the outstanding records itself, so the
        invariant "a live obligation has a live executor" survives a guardian that
        cannot be spawned yet.
        """
        stop = self._recovery_supervisor_stop
        attempt = 0
        while not stop.is_set() and not self._shutting_down:
            try:
                snapshot = self.recovery_journal.snapshot()
            except Exception:
                # An unreadable journal is not "nothing to do"; back off and look
                # again instead of ending the supervision.
                attempt += 1
                logger.warning(
                    "The recovery supervisor cannot read the recovery journal yet; "
                    "its state is unknown and it keeps watching"
                )
                stop.wait(min(2.0 * attempt, SUPERVISOR_POLL_CEILING_SEC))
                continue
            outstanding = snapshot.outstanding
            if not outstanding and not snapshot.damaged:
                # Everything this session started has been executed.  The supervisor
                # only exists to cover obligations, so it may end - the shutdown
                # barrier still checks the journal before the process exits.
                self._recovery_supervisor_wanted.clear()
                attempt = 0
                return
            if not self._supervisor_has_live_executor():
                if self._start_session_guardian():
                    attempt = 0
                    # A freshly started guardian may legitimately delegate to an
                    # already-running executor and exit immediately.  Never turn
                    # that success path into an unbounded spawn/poll loop.
                    stop.wait(SUPERVISOR_IDLE_POLL_SEC)
                    continue
                attempt += 1
                # No external executor could be started: do the work here rather
                # than let the obligation exist only on disk.
                logger.error(
                    "No recovery guardian could be started for %s outstanding obligation(s) "
                    "(attempt %s); this supervisor executes them itself",
                    len(outstanding),
                    attempt,
                )
                try:
                    restoreguard.resolve_journal_damage(self.recovery_journal)
                    restoreguard.resolve_all(
                        self.recovery_journal,
                        self._executor,
                        context="supervisor",
                        budget_sec=STARTUP_RECOVERY_BUDGET_SEC,
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception("The recovery supervisor pass failed")
                stop.wait(min(2.0 * attempt, SUPERVISOR_POLL_CEILING_SEC))
                continue
            attempt = 0
            stop.wait(SUPERVISOR_IDLE_POLL_SEC)
        self._recovery_supervisor_wanted.clear()

    def _supervisor_has_live_executor(self) -> bool:
        """Whether a guardian process of this journal is still running."""
        with self._recovery_lock:
            guardian = self._recovery_guardian
        if guardian is None:
            return False
        try:
            return guardian.poll() is None
        except Exception:  # pragma: no cover - defensive
            return False

    def _start_session_guardian(self) -> bool:
        """Start - or replace - the guardian that executes this journal.

        The guardian is what keeps executing after this process is killed: it
        watches this process's identity and restores whatever is left when the
        process is gone.  That is why it is started *before* the first park is
        allowed, not only at shutdown.
        """
        with self._guardian_start_lock:
            with self._recovery_lock:
                if self._recovery_guardian is not None:
                    try:
                        if self._recovery_guardian.poll() is None:
                            return True
                    except Exception:  # pragma: no cover - defensive
                        pass
                    self._recovery_guardian = None
            try:
                guardian = restoreguard.spawn_guardian(
                    self.recovery_journal.path,
                    owner_pid=self._executor.pid,
                    owner_created=self._executor.created or 0,
                    owner_run_id=self._executor.executor_id,
                    timeout=RESTORE_HANDOVER_TIMEOUT_SEC,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Starting the recovery guardian failed")
                return False
            if guardian is None:
                return False
            with self._recovery_lock:
                self._recovery_guardian = guardian
            logger.info("Recovery guardian pid=%s is executing %s", guardian.pid, self.recovery_journal.path)
            return True

    def _notify_damaged_journal(self, recovered) -> None:
        if self.tray is not None:
            self.tray.notify(
                "LookUp Windows",
                "Файл восстановления повреждён; окна, оставленные за экраном, "
                "возвращены автоматически",
            )
        logger.warning(
            "Recovery journal %s was unreadable; %s window(s) were recovered by scanning "
            "the LookUp parking position",
            self.recovery_journal.path,
            len(recovered),
        )

    def _recover_journaled_record(self, record: ParkRecord) -> bool:
        """Restore one journaled park; return True when it is no longer parked."""
        outcome = restoreguard.resolve(
            self.recovery_journal,
            record,
            self._executor,
            context="startup",
            budget_sec=STARTUP_RECOVERY_BUDGET_SEC,
        )
        return outcome.resolved

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
                defer=self.defer,
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
        if not self._config_saver.submit(self.config) and self.tray is not None:
            self.tray.notify("LookUp Windows", "Не удалось сохранить настройки")

    def _on_config_saved(self, ok: bool) -> None:
        if not ok and not self._shutting_down and self.tray is not None:
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
        tracked = TrackedWindow(
            process=candidate.process_name,
            title_contains=title_filter,
            title_hint=candidate.title or "",
            class_hint=candidate.info.class_name or "",
            # Pin the entry to the window the user actually pointed at, so a
            # second window of the same process can be added next to it.
            source_hwnd=candidate.hwnd or None,
        )
        if any(existing.same_target(tracked) for existing in self.config.windows):
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Активное окно уже отслеживается")
            return
        self.config.windows.append(tracked)
        card = self._create_card_for_tracked(tracked)
        if card is None:
            self.config.windows.remove(tracked)
            return
        self.panel.sync_layout()
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
        self.panel.sync_layout()
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
        for card in list(self.cards):
            self._destroy_runtime_card(card)
        self.cards = []
        for tracked in self.config.windows:
            if tracked.pip_enabled:
                self._create_card_for_tracked(tracked, show=False)
        if getattr(self, "panel", None) is not None:
            self.panel.sync_layout()
    def card_for_tracked(self, tracked: TrackedWindow) -> CardWnd | None:
        for card in self.cards:
            if card.tracked is tracked:
                return card
        return None

    def tracked_display_name(self, tracked: TrackedWindow) -> str:
        """Label for lists, disambiguating entries that print the same text.

        Two windows of one process can be tracked with an identical title (the
        same project opened twice in VS Code), and their names would otherwise be
        indistinguishable in the control panel and the tray menu.
        """
        base = tracked.display_name()
        peers = [item for item in self.config.windows if item.display_name() == base]
        if len(peers) < 2:
            return base
        for index, item in enumerate(peers, start=1):
            if item is tracked:
                return base if index == 1 else f"{base} ({index})"
        return base

    def _active_index_for_tracked(self, tracked: TrackedWindow) -> int:
        index = 0
        for item in self.config.windows:
            if item is tracked:
                break
            if item.pip_enabled:
                index += 1
        return index

    def _create_card_for_tracked(self, tracked: TrackedWindow, *, show: bool | None = None) -> CardWnd | None:
        if not tracked.pip_enabled:
            return None
        existing = self.card_for_tracked(tracked)
        if existing is not None:
            return existing
        try:
            card = CardWnd(self, tracked, 0, 0)
            # A window picked in the selector is bound exactly once, so several
            # cards can follow identically titled windows of the same process.
            selected_hwnd = int(tracked.source_hwnd or 0)
            if selected_hwnd:
                candidate = self.finder.candidate(selected_hwnd)
                if candidate is not None:
                    card.set_candidate(candidate, winapi.get_foreground_hwnd())
        except OSError:
            return None
        active_index = self._active_index_for_tracked(tracked)
        x, y = self._card_pos(tracked, active_index)
        current_w, current_h = card._size()
        winui.move_window(card.hwnd, x, y, current_w, current_h)
        card.apply_size(tracked.width)
        card.update_thumb_geometry()
        insert_at = min(active_index, len(self.cards))
        self.cards.insert(insert_at, card)
        winui.set_alpha(card.hwnd, self.config.opacity)
        winui.set_topmost(card.hwnd, bool(self.config.always_on_top))
        should_show = (self._startup_ready and not self._cards_hidden) if show is None else bool(show)
        if should_show:
            winui.show_window(card.hwnd)
        return card

    def _clear_card_detector_state(self, card: CardWnd) -> None:
        if not card.src_hwnd:
            return
        hwnd = card.src_hwnd
        self.detector.forget(hwnd)
        self._capture_failures.pop(hwnd, None)
        self._capture_notified.discard(hwnd)
        self._detector_quiet.pop(hwnd, None)
        self._detector_next_due.pop(hwnd, None)

    def _destroy_runtime_card(self, card: CardWnd) -> None:
        self.restore_parked_source(card, activate=False)
        if self.big is not None and self.big.card is card:
            self.close_fullscreen()
        self._clear_card_detector_state(card)
        if card in self.cards:
            self.cards.remove(card)
        card.destroy()

    def set_tracked_pip_enabled(self, tracked: TrackedWindow, enabled: bool) -> None:
        if not any(item is tracked for item in self.config.windows):
            return
        enabled = bool(enabled)
        if tracked.pip_enabled == enabled:
            return
        if enabled:
            tracked.pip_enabled = True
            if self._create_card_for_tracked(tracked) is None:
                tracked.pip_enabled = False
                if self.tray is not None:
                    self.tray.notify("LookUp Windows", f"Не удалось включить PiP: {tracked.display_name()}")
                self.panel.sync_layout()
                return
        else:
            tracked.pip_enabled = False
            card = self.card_for_tracked(tracked)
            if card is not None:
                self._destroy_runtime_card(card)
        self.panel.sync_layout()
        self.save_config()
        if self._startup_ready:
            self._do_refresh()

    def toggle_tracked_pip(self, tracked: TrackedWindow) -> None:
        self.set_tracked_pip_enabled(tracked, not tracked.pip_enabled)

    def disable_card(self, card: CardWnd) -> None:
        self.set_tracked_pip_enabled(card.tracked, False)

    def remove_tracked_window(self, tracked: TrackedWindow) -> None:
        if not any(item is tracked for item in self.config.windows):
            return
        card = self.card_for_tracked(tracked)
        if card is not None:
            self._destroy_runtime_card(card)
        self.config.windows = [item for item in self.config.windows if item is not tracked]
        self.panel.sync_layout()
        self.save_config()
        if self._startup_ready:
            self._do_refresh()

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
            if self._shutdown_started and not self._shutting_down:
                self.quit()
                return
            if self._shutting_down:
                self._retry_shutdown()
                return
            if self._recovery_supervisor_wanted.is_set() and not self._has_live_supervisor():
                # A supervisor that could not be started is retried: without one, a
                # record that belongs to no card has nobody executing it.
                logger.error("The recovery supervisor could not be started; retrying")
                self._ensure_recovery_supervisor()
                self._request_recovery_supervisor_from_timer()
        elif timer_id == TIMER_PARKED_WATCH:
            self._watch_parked_sources()

    def _watch_parked_sources(self) -> None:
        """Fast, cheap taskbar/Alt+Tab watcher for parked source windows.

        This intentionally does not enumerate windows or touch DWM.  It only
        checks foreground/minimized state so a user request to bring a parked
        source back is noticed in ~100 ms instead of waiting for REFRESH_MS.
        """
        parked = [card for card in self.cards if card._parked_source is not None]
        with self._recovery_lock:
            recovery_items = list(self._recovery_registry.items())
            inflight = set(self._recovery_inflight)
        if not parked and not recovery_items:
            winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)
            return
        foreground = winapi.get_foreground_hwnd()
        now = time.monotonic()
        recovery_by_hwnd = dict(recovery_items)
        for card in parked:
            if card._source_action_pending is not None or not card.src_hwnd:
                continue
            entry = recovery_by_hwnd.get(card.src_hwnd)
            if entry is not None and entry.needs_retry and entry.retry_at > now:
                continue
            if winapi.is_minimized(card.src_hwnd):
                self.activate_card(card)
            elif foreground != card.src_hwnd:
                card._parked_seen_not_foreground = True
            elif card._parked_seen_not_foreground:
                self.activate_card(card)

        for hwnd, entry in recovery_items:
            if not entry.needs_retry or hwnd in inflight or entry.retry_at > now:
                continue
            card = next(
                (
                    item
                    for item in self.cards
                    if item.src_hwnd == hwnd and item._parked_source is entry.state
                ),
                None,
            )
            # Nothing is decided here: whether this is still our window, and
            # whether the restore worked, is answered by the worker.  A timer
            # callback must not turn "cannot tell right now" into "not parked".
            self._queue_cleanup_restore(
                hwnd,
                entry.state,
                activate=False,
                label=entry.label,
                card=card,
            )

    def _notify_restore_failure_throttled(self, hwnd: int, text: str) -> None:
        """Avoid a notification storm while a foreign window refuses recovery."""
        if self.tray is None:
            return
        now = time.monotonic()
        last = self._restore_notice_at.get(int(hwnd), 0.0)
        if now - last < 30.0:
            return
        self._restore_notice_at[int(hwnd)] = now
        self.tray.notify("LookUp Windows", text)

    def _finish_refind_scan(self, candidates: list[WindowCandidate] | None) -> None:
        with self._refind_lock:
            self._refind_scan_inflight = False
            if candidates is not None:
                self._refind_candidates_cache = list(candidates)
                self._refind_candidates_at = time.monotonic()
        if self._shutting_down or not getattr(self, "panel", None) or not self.panel.hwnd:
            return
        winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
        winui.set_timer(self.panel.hwnd, TIMER_REFRESH_SOON, 50)

    def _request_refind_candidates(self) -> list[WindowCandidate] | None:
        """Return a recent desktop snapshot and start an async refresh if needed."""
        now = time.monotonic()
        start_scan = False
        with self._refind_lock:
            cached = self._refind_candidates_cache
            if cached is not None and now - self._refind_candidates_at <= max(2.0, REFRESH_MS / 1000.0 * 2):
                return list(cached)
            if not self._refind_scan_inflight and not self._shutting_down:
                self._refind_scan_inflight = True
                start_scan = True
            stale = list(cached) if cached is not None else None
        if not start_scan:
            return stale

        def scan() -> None:
            candidates = None
            try:
                candidates = self.finder.list_windows()
            except Exception:
                logger.exception("Background auto-refind enumeration failed")
            self.defer(self._finish_refind_scan, candidates)

        try:
            threading.Thread(
                target=scan, name="LookUpWindows-RefindScan", daemon=True
            ).start()
        except Exception:
            logger.exception("Failed to start background auto-refind enumeration")
            with self._refind_lock:
                self._refind_scan_inflight = False
        return stale

    def _do_refresh(self) -> None:
        foreground = winapi.get_foreground_hwnd()
        desktop_active = winapi.is_desktop_foreground()
        # Missing cards used to call WindowFinder.find() independently, causing
        # a full EnumWindows/process-name pass per card.  Cache one enumeration
        # per refresh and reuse it for all auto-refind matches.
        refind_candidates: list[WindowCandidate] | None = None
        # Windows that are already shown by some card must not be handed to a
        # second card: two tracked windows of one process can share a title, and
        # both would otherwise auto-refind onto the same source.
        claimed_hwnds: set[int] = {card.src_hwnd for card in self.cards if card.src_hwnd}
        for card in self.cards:
            try:
                if card._parked_source is not None:
                    with self._recovery_lock:
                        retry_entry = self._recovery_registry.get(card.src_hwnd)
                        retry_blocked = bool(
                            retry_entry is not None
                            and retry_entry.needs_retry
                            and retry_entry.retry_at > time.monotonic()
                        )
                    if retry_blocked:
                        pass
                    elif card.src_hwnd and winapi.is_minimized(card.src_hwnd):
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
                            refind_candidates = self._request_refind_candidates()
                        if refind_candidates is not None:
                            candidate = self.finder.find_preferred(
                                card.tracked,
                                refind_candidates,
                                exclude_hwnds=claimed_hwnds,
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
                needs_orphan_recovery = bool(
                    candidate is not None
                    and card._parked_source is None
                    and card._source_action_pending is None
                    and time.monotonic() >= card._orphan_retry_at
                    and winapi.looks_like_lookup_parked(candidate.hwnd)
                )
                # Bind first.  set_candidate() advances the action generation on
                # an initial/source-changed bind; scheduling recovery before that
                # used to invalidate the worker generation and allowed duplicate
                # recovery workers on the next refresh.
                card.set_candidate(candidate, foreground)
                if card.src_hwnd:
                    claimed_hwnds.add(card.src_hwnd)
                if (
                    needs_orphan_recovery
                    and candidate is not None
                    and card.src_hwnd == candidate.hwnd
                    and card._source_action_pending is None
                ):
                    card._source_action_id += 1
                    action_id = card._source_action_id
                    card._source_action_pending = "restore"
                    if not self._start_daemon_worker(
                        "LookUpWindows-RecoverSource",
                        self._recover_orphan_source_worker,
                        (card, candidate.hwnd, action_id),
                    ):
                        card._source_action_pending = None
                        card._invalidate()
                if candidate is not None and candidate.hwnd == foreground and card.is_changed():
                    card.set_changed(False)
                    self.detector.forget(candidate.hwnd)
                if self.big is not None and self.big.card is card and card.src_hwnd == 0:
                    self.close_fullscreen()
            except Exception:
                self._log_exception_throttled(
                    f"refresh:{card.src_hwnd}:{id(card)}",
                    "Refresh failed: hwnd=%s title=%r matcher=(%r, %r)",
                    card.src_hwnd,
                    card._title,
                    card.tracked.process,
                    card.tracked.title_contains,
                )
                continue
        winui.invalidate(self.panel.hwnd)

    def _reset_change_detection_state(self) -> None:
        self.detector.clear()
        self._detector_quiet.clear()
        self._detector_next_due.clear()
        self._capture_failures.clear()
        self._capture_notified.clear()
        for card in self.cards:
            card.set_capture_issue(False)

    def _sync_change_timer(self) -> None:
        if not getattr(self, "panel", None) or not self.panel.hwnd:
            return
        winui.kill_timer(self.panel.hwnd, TIMER_CHANGE)
        if self._startup_ready and self.config.change_detection:
            winui.set_timer(
                self.panel.hwnd,
                TIMER_CHANGE,
                max(250, int(self.config.change_interval_sec * 1000)),
            )
        elif not self.config.change_detection:
            self._reset_change_detection_state()

    def _change_tick(self) -> None:
        if not self.config.change_detection:
            self._reset_change_detection_state()
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
            if result.status in {"capture_failed", "capture_timeout"}:
                failures = self._capture_failures.get(hwnd, 0) + 1
                self._capture_failures[hwnd] = failures
                self._detector_next_due[hwnd] = now_mono + min(15.0, base_interval * 2)
                if failures >= 3:
                    card.set_capture_issue(True)
                    if hwnd not in self._capture_notified and self.tray is not None:
                        detail = "таймаут захвата" if result.status == "capture_timeout" else "захват недоступен"
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
                self._log_exception_throttled(
                    f"change:{card.src_hwnd}:{id(card)}",
                    "Change-detection eligibility failed: hwnd=%s title=%r",
                    card.src_hwnd,
                    card._title,
                )
                continue
        self.detector.schedule(eligible)

        stalled_for = self.detector.busy_for()
        if not self.detector.healthy() and not self._detector_error_notified:
            self._detector_error_notified = True
            logger.error("Change detector supervisor is unavailable")
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Детектор изменений остановился. Перезапустите приложение.")
        if self.detector.lifetime_binding_degraded() and not self._capture_binding_notified:
            # A helper that could not be bound to the kill-on-close job is never
            # used, so change detection is deliberately off.  Say so instead of
            # silently looking like a slow detector.
            self._capture_binding_notified = True
            logger.error(
                "Capture helpers are unavailable: they could not be bound to the kill-on-close "
                "job, so change detection stays disabled for this session"
            )
            if self.tray is not None:
                self.tray.notify(
                    "LookUp Windows",
                    "Детектор изменений недоступен: вспомогательные процессы не удалось привязать",
                )
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
        """Показать управляющую панель со списком настроенных PiP."""
        self._hidden = False
        self.panel.sync_layout()
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

    def _start_daemon_worker(self, name: str, target, args: tuple) -> bool:
        # Register before Thread.start(): shutdown must also wait for a worker
        # which exists but has not been scheduled by the OS yet.
        self._begin_source_action()

        def run() -> None:
            try:
                target(*args)
            finally:
                self._end_source_action()

        try:
            threading.Thread(target=run, name=name, daemon=True).start()
            return True
        except Exception:
            self._end_source_action()
            logger.exception("Failed to start worker %s", name)
            return False

    def _journal_record_intent(self, hwnd: int, state: winapi.ParkedWindowState, label: str) -> bool:
        """Persist recovery ownership of a pending park (worker thread only).

        The returned record is kept so the in-memory registry can later be matched
        against the exact durable record it created - that is what keeps the UI
        path from ever having to read the journal back.
        """
        record = record_from_state(hwnd, state, owner=self._executor, label=label)
        if not self.recovery_journal.record_intent(record):
            return None
        return record

    def _journal_commit_park(self, hwnd: int) -> bool:
        return bool(self.recovery_journal.mark_parked(hwnd, self._executor))

    def _journal_drop(self, hwnd: int, recorded_at: float | None = None) -> None:
        """Forget recovery ownership after a verified restore (worker thread only).

        The record is only retired by the run that registered it, or by the
        executor that claimed it, so a *different* process that merely sees the
        window on screen can no longer erase an obligation whose park may still be
        in flight (the delayed-move race).
        """
        self.recovery_journal.clear_unless_claimed(hwnd, self._executor, recorded_at=recorded_at)

    def _journal_drop_async(self, hwnd: int, recorded_at: float | None) -> None:
        """Release a stale record without doing file I/O on the UI thread.

        There is deliberately no synchronous fallback: if a worker cannot be
        started, falling back would put the journal lock - a *cross-process* lock
        with a 15s timeout, behind an in-process lock with no timeout at all -
        straight onto the UI thread.  Keeping the record is the safe answer: the
        next attempt, the shutdown barrier or the guardian still retires it.
        """
        if not self._start_daemon_worker(
            "LookUpWindows-JournalRelease",
            self._journal_drop,
            (hwnd, recorded_at),
        ):
            logger.error(
                "Keeping durable recovery for hwnd=%s: no worker could release it",
                hwnd,
            )

    def _begin_source_action(self) -> None:
        """Mark a blocking foreign-window operation as in flight."""
        with self._source_action_idle:
            self._source_action_inflight += 1

    def _end_source_action(self) -> None:
        with self._source_action_idle:
            if self._source_action_inflight > 0:
                self._source_action_inflight -= 1
            if self._source_action_inflight == 0:
                self._source_action_idle.notify_all()

    def _wait_for_source_actions(self, timeout: float) -> bool:
        """Wait until no blocking foreign-window operation is in flight.

        Shutdown uses this as its barrier: a park or a startup recovery that is
        still moving a window must either finish (and record or clear its
        journal entry) or be left with its durable record intact.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._source_action_idle:
            while self._source_action_inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._source_action_idle.wait(remaining)
            return True

    def _register_recovery(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState,
        label: str,
        recorded_at: float | None = None,
    ) -> None:
        with self._recovery_lock:
            current = self._recovery_registry.get(hwnd)
            if current is None or current.state is not state:
                self._recovery_registry[hwnd] = RecoveryEntry(
                    state=state, label=label, recorded_at=recorded_at
                )
            else:
                if label:
                    current.label = label
                if recorded_at is not None:
                    current.recorded_at = recorded_at
        if (
            not self._shutting_down
            and getattr(self, "panel", None) is not None
            and self.panel.hwnd
        ):
            winui.set_timer(self.panel.hwnd, TIMER_PARKED_WATCH, 100)

    def _drop_recovery(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState | None = None,
        recorded_at: float | None = None,
    ) -> None:
        with self._recovery_lock:
            current = self._recovery_registry.get(hwnd)
            if current is not None and (state is None or current.state is state):
                self._recovery_registry.pop(hwnd, None)
                if recorded_at is None:
                    recorded_at = current.recorded_at
            self._recovery_inflight.discard(hwnd)
        # The durable record has to go as well, but only for the exact record this
        # registry entry describes - and the decision itself belongs to a worker:
        # the journal lock is a cross-process lock with a 15s timeout, and the UI
        # thread must never wait on it (nor on a same-process worker that holds
        # it), let alone act on an I/O failure as if the obligation had ended.
        self._journal_drop_async(hwnd, recorded_at)

    def _begin_recovery(self, hwnd: int, state: winapi.ParkedWindowState) -> bool:
        with self._recovery_lock:
            current = self._recovery_registry.get(hwnd)
            if current is None or current.state is not state or hwnd in self._recovery_inflight:
                return False
            self._recovery_inflight.add(hwnd)
            return True

    def _complete_recovery_attempt(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState,
        ok: bool,
    ) -> bool:
        # One classifier decides everything this method does with the record.
        # "I could not find out whether this is still my window" is not the same
        # answer as "this is somebody else's window": treating an inaccessible
        # process as a reused handle deleted the only durable proof that a live
        # window was parked off-screen.
        verdict = winapi.classify_parked_window(hwnd, state)
        stale = verdict in (winapi.VERIFY_GONE, winapi.VERIFY_REUSED)
        unknown = verdict == winapi.VERIFY_UNKNOWN
        released = False
        recorded_at: float | None = None
        with self._recovery_lock:
            self._recovery_inflight.discard(hwnd)
            current = self._recovery_registry.get(hwnd)
            if current is not None and current.state is state and (ok or stale):
                recorded_at = current.recorded_at
                self._recovery_registry.pop(hwnd, None)
                released = True
            elif current is not None and current.state is state:
                current.attempts += 1
                current.needs_retry = True
                current.retry_at = time.monotonic() + min(5.0, 0.25 * (2 ** min(current.attempts, 4)))
        if released:
            # The window is observably back (or provably not ours any more): the
            # on-disk recovery journal must stop advertising it as parked.
            self._journal_drop_async(hwnd, recorded_at)
        elif unknown:
            logger.debug(
                "Keeping recovery for hwnd=%s: its identity could not be established right now",
                hwnd,
            )
        return stale


    def _queue_cleanup_restore(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState,
        *,
        activate: bool,
        label: str,
        card: CardWnd | None,
        done: threading.Event | None = None,
    ) -> bool:
        self._register_recovery(hwnd, state, label)
        if not self._begin_recovery(hwnd, state):
            if done is not None:
                done.set()
            return False
        started = self._start_daemon_worker(
            "LookUpWindows-CleanupRestore",
            self._cleanup_restore_worker,
            (hwnd, state, activate, label, card, done),
        )
        if not started:
            self._complete_recovery_attempt(hwnd, state, False)
            if card is not None:
                card._source_action_pending = None
                card._invalidate()
            if done is not None:
                done.set()
        return started

    def _recover_abandoned_park(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState,
        label: str,
    ) -> None:
        if self._shutting_down:
            return
        self._queue_cleanup_restore(
            hwnd,
            state,
            activate=False,
            label=label,
            card=None,
        )

    def restore_parked_source(self, card: CardWnd, *, activate: bool) -> bool:
        """Queue a verified restore while retaining recovery state until success."""
        state = card._parked_source
        hwnd = card.src_hwnd
        if state is None:
            if activate and hwnd and winapi.is_window(hwnd):
                # UI dispatch thread: never block on a foreign window.
                return winapi.set_foreground(hwnd, async_restore=True)
            return False
        if not hwnd or not winapi.is_window(hwnd):
            # The handle is gone: that is provable from this thread without asking
            # the target process anything.  Anything less certain is decided by the
            # worker, which is where the durable record may also be retired.
            card._parked_source = None
            card._parked_seen_not_foreground = False
            card._source_action_pending = None
            self._drop_recovery(hwnd, state)
            return False

        card._source_action_pending = "restore"
        self.detector.forget(hwnd)
        card._invalidate()
        return self._queue_cleanup_restore(
            hwnd,
            state,
            activate=activate,
            label=card._title,
            card=card,
        )

    def _cleanup_restore_worker(
        self,
        hwnd: int,
        state: winapi.ParkedWindowState,
        activate: bool,
        label: str,
        card: CardWnd | None,
        done: threading.Event | None,
    ) -> None:
        ok = False
        try:
            ok = winapi.restore_parked_window_sync(hwnd, state)
            if (
                not ok
                and winapi.window_matches_parked_state(hwnd, state)
                and winapi.looks_like_lookup_parked(hwnd)
            ):
                ok = winapi.recover_orphaned_lookup_park(hwnd)
            if ok and activate:
                winapi.set_foreground(hwnd)
            if not ok and winapi.is_window(hwnd):
                logger.warning("Cleanup restore failed: hwnd=%s title=%r", hwnd, label)
        except Exception:
            logger.exception("Cleanup restore crashed: hwnd=%s title=%r", hwnd, label)
        finally:
            stale = self._complete_recovery_attempt(hwnd, state, ok)
            if not self._shutting_down:
                self.defer(self._finish_cleanup_restore, card, hwnd, state, ok, stale, activate)
            if done is not None:
                done.set()

    def _finish_cleanup_restore(
        self,
        card: CardWnd | None,
        hwnd: int,
        state: winapi.ParkedWindowState,
        ok: bool,
        stale: bool,
        activate: bool,
    ) -> None:
        if card is not None and card.hwnd and card.src_hwnd == hwnd and card._parked_source is state:
            card._source_action_pending = None
            if ok or stale:
                card._parked_source = None
                card._parked_seen_not_foreground = False
            if ok:
                card._minimized = False
                card._active = bool(activate)
                card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
                card.set_changed(False)
                card.update_thumb_geometry()
            card._invalidate()
        if not ok and not stale and self.tray is not None:
            self.tray.notify("LookUp Windows", "Не удалось вернуть исходное окно; восстановление будет повторено")

    def _restore_sources_for_shutdown(self, deadline_sec: float = 1.5) -> None:
        # One absolute deadline covers the whole shutdown barrier: first let an
        # in-flight park finish (and record its own recovery), then restore.
        # A park that is still moving a foreign window must not be observed
        # half-committed by the restore pass.
        deadline = time.monotonic() + max(0.0, float(deadline_sec))
        if not self._wait_for_source_actions(max(0.0, deadline - time.monotonic())):
            logger.warning(
                "Shutdown started while %s source action(s) were still in flight",
                self._source_action_inflight,
            )

        # No identity is judged on this thread.  A parked window that is gone, or
        # that provably belongs to somebody else, is recognised by the worker -
        # which is also the only place allowed to retire a durable record.  A
        # window whose identity merely cannot be established keeps its record and
        # is simply handed to the guardian with it.
        for card in self.cards:
            state = card._parked_source
            hwnd = card.src_hwnd
            if state is None or not hwnd:
                continue
            self._register_recovery(hwnd, state, card._title)

        with self._recovery_lock:
            recovery_items = list(self._recovery_registry.items())

        jobs: list[tuple[threading.Event, int, str]] = []
        for hwnd, entry in recovery_items:
            done = threading.Event()
            jobs.append((done, hwnd, entry.label))
            if not self._start_daemon_worker(
                "LookUpWindows-ShutdownRestore",
                self._cleanup_restore_worker,
                (hwnd, entry.state, False, entry.label, None, done),
            ):
                done.set()

        for done, _hwnd, _label in jobs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done.wait(remaining)
        unfinished = [(hwnd, label) for done, hwnd, label in jobs if not done.is_set()]
        if unfinished:
            # Their durable recovery records stay in the journal on purpose:
            # whoever executes them (the guardian below, or the next LookUp
            # start) still owns the obligation.
            logger.warning("Shutdown restore deadline reached for %s source window(s): %r", len(unfinished), unfinished)

    def _handover_unfinished_restores(self) -> bool:
        """Give every unfinished park obligation to a process that outlives us.

        Exiting is not allowed to end the ownership of a window this process
        moved off-screen.  When the shutdown barrier expires with records still
        in the journal, a small guardian process is started and confirmed ready
        *before* the application continues to exit, so the restore is executed by
        something that stays alive after our UI, threads and GIL are gone.

        An unresolved journal is unfinished work in its own right: the document
        nobody could read may still describe a parked window, so it is handed over
        as well - and if no guardian can be confirmed, the application stays alive
        instead of exiting with that work unowned.

        Worker thread only.  Everything in here blocks for as long as the journal
        lock takes, plus up to `RESTORE_HANDOVER_TIMEOUT_SEC` for the guardian
        to answer and `RESTORE_FALLBACK_WAIT_SEC` for the fail-closed wait -
        none of which may be spent on the Tk thread while the panel is still up.
        """
        try:
            # A journal that cannot be read does not mean "nothing to restore":
            # recover what LookUp can still prove, then decide.
            resolution = restoreguard.resolve_journal_damage(self.recovery_journal)
            if resolution.recovered:
                self.defer(self._notify_damaged_journal, list(resolution.recovered))
            # Execution ownership moves with the obligation: a lease this process
            # still holds (from startup recovery) would keep the guardian out
            # until it expired.
            self.recovery_journal.release_all(self._executor)
            snapshot = self.recovery_journal.snapshot()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Shutdown could not read the recovery journal")
            return False
        outstanding = snapshot.outstanding
        if not outstanding and not snapshot.damaged:
            return True
        if snapshot.damaged:
            logger.error(
                "Shutdown is handing over an unresolved recovery journal (%s); the "
                "successor keeps sweeping until the damage is really resolved",
                snapshot.damage or "unknown damage",
            )
        if self._supervisor_has_live_executor():
            logger.warning(
                "Shutdown handed %s unfinished source restore(s) to recovery guardian pid=%s: %r",
                len(outstanding) or 1,
                self._recovery_guardian.pid,
                [(record.hwnd, record.label) for record in outstanding],
            )
            return True
        guardian = restoreguard.spawn_guardian(
            self.recovery_journal.path,
            owner_pid=self._executor.pid,
            owner_created=self._executor.created or 0,
            owner_run_id=self._executor.executor_id,
            timeout=RESTORE_HANDOVER_TIMEOUT_SEC,
        )
        if guardian is not None:
            self._recovery_guardian = guardian
            logger.warning(
                "Shutdown handed %s unfinished source restore(s) to recovery guardian "
                "pid=%s: %r",
                len(outstanding) or 1,
                guardian.pid,
                [(record.hwnd, record.label) for record in outstanding],
            )
            return True
        # Fail closed: without an executor the obligation would only survive in the
        # journal, so keep this process alive instead of walking
        # away from a window the user cannot see.
        logger.error(
            "No recovery guardian could be started while %s source window(s) are still "
            "parked (damage: %s); keeping the application alive to finish them: %r",
            len(outstanding),
            snapshot.damage or "none",
            [(record.hwnd, record.label) for record in outstanding],
        )
        if self.tray is not None:
            self.tray.notify(
                "LookUp Windows",
                "Возврат исходного окна задерживается: приложение пока не может закрыться",
            )
        if restoreguard.wait_for_handover(
            self.recovery_journal, RESTORE_FALLBACK_WAIT_SEC
        ):
            logger.info("Unfinished source restores completed before shutdown")
            return True
        else:
            logger.error("Shutdown is waiting for outstanding source restores or journal access")

        return False

    def _recover_orphan_source_worker(self, card: CardWnd, hwnd: int, action_id: int) -> None:
        ok = False
        try:
            ok = winapi.recover_orphaned_lookup_park(hwnd)
        except Exception:
            logger.exception("Orphan recovery crashed: hwnd=%s", hwnd)
        finally:
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
            card._orphan_restore_attempts = 0
            card._orphan_retry_at = 0.0
            card._minimized = False
            card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
            card.update_thumb_geometry()
            self.detector.forget(hwnd)
            winui.kill_timer(self.panel.hwnd, TIMER_REFRESH_SOON)
            winui.set_timer(self.panel.hwnd, TIMER_REFRESH_SOON, 80)
        elif not ok:
            card._orphan_restore_attempts += 1
            card._orphan_retry_at = time.monotonic() + min(30.0, 1.0 * (2 ** min(card._orphan_restore_attempts, 5)))
            self._notify_restore_failure_throttled(
                hwnd, "Не удалось вернуть окно, оставшееся за экраном"
            )
        card._invalidate()

    def _park_source_worker(self, card: CardWnd, hwnd: int, action_id: int) -> None:
        # The park is counted as in flight for the whole native sequence, so a
        # shutdown cannot start restoring while this window is being moved.
        if self._shutting_down:
            return
        # ``state`` is captured from the moment ownership is durable, never cleared
        # afterwards: an exception in the native sequence below happens *after* the
        # window may already have been moved, and erasing the state there is what
        # left a parked window with nobody restoring it.
        state = None
        intent_recorded = False
        recorded_at: float | None = None
        failed = False

        def before_park(parked: winapi.ParkedWindowState) -> bool:
            nonlocal intent_recorded, recorded_at, state
            record = self._journal_record_intent(hwnd, parked, card._title)
            intent_recorded = bool(record)
            if record is not None:
                recorded_at = record.recorded_at
                # The obligation exists now, so the state is ours to keep: whatever
                # happens next, this process has to restore the window.
                state = parked
                self._ensure_recovery_supervisor()
            return intent_recorded

        try:
            # A park may only start once a confirmed executor exists for it, so a
            # hard kill of this process still leaves somebody who restores the
            # window.  Blocking handshake, worker thread only.
            if not self._await_session_executor():
                logger.error(
                    "Park of hwnd=%s refused: no recovery executor could be confirmed",
                    hwnd,
                )
                self.defer(self._notify_park_refused, card, hwnd, action_id)
                return
            state = winapi.park_window_offscreen_sync(
                hwnd,
                before_park=before_park,
            )
            if state is not None:
                if not self._journal_commit_park(hwnd):
                    # The window may already be off-screen, but the durable intent
                    # could not be promoted.  Keep the intent and immediately ask
                    # the recovery path to return the window; never present this as
                    # a successfully committed park.
                    logger.error(
                        "Park of hwnd=%s moved the window but could not commit the recovery record; "
                        "restoring immediately",
                        hwnd,
                    )
                    if winapi.restore_parked_window_sync(hwnd, state):
                        self._journal_drop(hwnd, recorded_at)
                        state = None
                    else:
                        failed = True
            elif intent_recorded:
                # No park is in effect, so the recorded intent must not linger.
                # A refused intent never authorized deleting an older record.
                self._journal_drop(hwnd, recorded_at)

            # If the card disappeared while the foreign window was being moved,
            # put the application back immediately rather than orphaning it.
            if state is not None and (
                self._shutting_down
                or card.hwnd == 0
                or card.src_hwnd != hwnd
                or card not in self.cards
            ):
                if not winapi.restore_parked_window_sync(hwnd, state):
                    logger.warning("Rollback after abandoned park failed: hwnd=%s", hwnd)
                    if not self._shutting_down:
                        self.defer(self._recover_abandoned_park, hwnd, state, card._title)
                else:
                    self._journal_drop(hwnd, recorded_at)
                return
        except Exception:
            # ``state`` deliberately keeps whatever ``before_park`` captured: the
            # journal already owns an obligation for this window, and a park may
            # well be in effect.  Dropping it here would leave the obligation
            # without an executor for the rest of the session.
            logger.exception("Park worker crashed: hwnd=%s", hwnd)
            failed = True
        if failed and state is not None and intent_recorded and not self._shutting_down:
            # Registered ownership without a completed sequence: recover it now, the
            # same way an abandoned card is handled.
            self.defer(self._recover_abandoned_park, hwnd, state, card._title)
        self.defer(self._finish_park_source, card, hwnd, action_id, state, recorded_at)

    def _await_session_executor(self, timeout: float = SESSION_GUARDIAN_CONFIRM_TIMEOUT_SEC) -> bool:
        """Whether a confirmed executor exists for this journal right now.

        Worker thread only: both the guardian handshake and the journal read block.
        The supervisor keeps trying in the background, so this only has to give the
        park a bounded amount of time before refusing it - it never gives up and
        never parks a window nobody could put back.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        attempt = 0
        while True:
            if self._supervisor_has_live_executor():
                return True
            self._ensure_recovery_supervisor()
            attempt += 1
            if self._start_session_guardian():
                return True
            if self._shutting_down or time.monotonic() >= deadline:
                return False
            logger.error(
                "No recovery guardian could be started (attempt %s); the park waits "
                "instead of moving a window nobody could put back",
                attempt,
            )
            time.sleep(min(2.0 * attempt, 10.0))

    def _notify_park_refused(self, card: CardWnd, hwnd: int, action_id: int) -> None:
        if card.hwnd and card.src_hwnd == hwnd and action_id == card._source_action_id:
            card._source_action_pending = None
            card._invalidate()
        if self.tray is not None:
            self.tray.notify(
                "LookUp Windows",
                "Не удалось скрыть исходное окно: нет исполнителя восстановления",
            )

    def _finish_park_source(
        self,
        card: CardWnd,
        hwnd: int,
        action_id: int,
        state: winapi.ParkedWindowState | None,
        recorded_at: float | None = None,
    ) -> None:
        if card.hwnd == 0 or card.src_hwnd != hwnd or action_id != card._source_action_id:
            if state is not None:
                self._register_recovery(hwnd, state, card._title, recorded_at)
                self._queue_cleanup_restore(
                    hwnd,
                    state,
                    activate=False,
                    label=card._title,
                    card=None,
                )
            return
        card._source_action_pending = None
        if state is None:
            card._invalidate()
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Не удалось скрыть исходное окно")
            return
        card._parked_source = state
        self._register_recovery(hwnd, state, card._title, recorded_at)
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
        ok = False
        try:
            ok = winapi.restore_parked_window_sync(hwnd, state)
            if ok and activate:
                winapi.set_foreground(hwnd)
        except Exception:
            logger.exception("Restore worker crashed: hwnd=%s", hwnd)
        stale = self._complete_recovery_attempt(hwnd, state, ok)
        self.defer(self._finish_restore_source, card, hwnd, action_id, state, ok, stale, activate)

    def _finish_restore_source(
        self,
        card: CardWnd,
        hwnd: int,
        action_id: int,
        state: winapi.ParkedWindowState,
        ok: bool,
        stale: bool,
        activate: bool,
    ) -> None:
        if card.hwnd == 0 or card.src_hwnd != hwnd:
            return
        if action_id != card._source_action_id:
            if ok and card._parked_source is state:
                card._parked_source = None
                card._parked_seen_not_foreground = False
                card._invalidate()
            return
        card._source_action_pending = None
        if not ok:
            if stale and card._parked_source is state:
                card._parked_source = None
                card._parked_seen_not_foreground = False
            card._invalidate()
            if not stale:
                self._notify_restore_failure_throttled(
                    hwnd, "Не удалось вернуть исходное окно; восстановление будет повторено"
                )
            return
        if card._parked_source is state:
            card._parked_source = None
        card._parked_seen_not_foreground = False
        card._minimized = False
        card._active = bool(activate)
        card._placeholder = "" if card.thumb is not None else "Не удалось создать превью (DWM)"
        card.set_changed(False)
        self.detector.forget(hwnd)
        card.update_thumb_geometry()
        card._invalidate()
        with self._recovery_lock:
            recovery_pending = bool(self._recovery_registry)
        if not recovery_pending and not any(item._parked_source is not None for item in self.cards):
            winui.kill_timer(self.panel.hwnd, TIMER_PARKED_WATCH)

    def _queue_interactive_restore(
        self,
        card: CardWnd,
        hwnd: int,
        action_id: int,
        state: winapi.ParkedWindowState,
        activate: bool,
    ) -> bool:
        self._register_recovery(hwnd, state, card._title)
        if not self._begin_recovery(hwnd, state):
            card._source_action_pending = None
            card._invalidate()
            return False
        started = self._start_daemon_worker(
            "LookUpWindows-RestoreSource",
            self._restore_source_worker,
            (card, hwnd, action_id, state, activate),
        )
        if not started:
            self._complete_recovery_attempt(hwnd, state, False)
            card._source_action_pending = None
            card._invalidate()
        return started

    def toggle_card_source(self, card: CardWnd) -> None:
        """PiP click: hide/show the source without blocking LookUp's UI.

        Cross-process placement is executed on a short-lived daemon worker.  The
        worker uses a synchronous, verified Win32 sequence, so maximized/RDP/1C
        windows cannot race SW_RESTORE against the off-screen move as they did
        with two asynchronous requests.
        """
        hwnd = card.src_hwnd
        if self._shutting_down or not hwnd or not winapi.is_window(hwnd) or card._source_action_pending is not None:
            return

        card._source_action_id += 1
        action_id = card._source_action_id
        if card._parked_source is not None:
            state = card._parked_source
            card._source_action_pending = "restore"
            card._invalidate()
            self._queue_interactive_restore(card, hwnd, action_id, state, True)
            return

        if winapi.is_minimized(hwnd):
            # UI dispatch thread: post the restore instead of waiting for a
            # possibly hung target application to process it.
            winapi.set_foreground(hwnd, async_restore=True)
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
        if not self._start_daemon_worker(
            "LookUpWindows-ParkSource",
            self._park_source_worker,
            (card, hwnd, action_id),
        ):
            card._source_action_pending = None
            card._invalidate()
            if self.tray is not None:
                self.tray.notify("LookUp Windows", "Не удалось запустить скрытие исходного окна")

    def activate_card(self, card: CardWnd) -> None:
        """Explicit open action: always restore/activate, never hide."""
        if self._shutting_down or not card.src_hwnd or card._source_action_pending is not None:
            return
        if card._parked_source is not None:
            card._source_action_id += 1
            action_id = card._source_action_id
            state = card._parked_source
            card._source_action_pending = "restore"
            card._invalidate()
            self._queue_interactive_restore(card, card.src_hwnd, action_id, state, True)
        else:
            winapi.set_foreground(card.src_hwnd, async_restore=True)
            card.set_changed(False)
            self.detector.forget(card.src_hwnd)

    def remove_card(self, card: CardWnd) -> None:
        """Удалить окно из конфигурации целиком (не просто выключить PiP)."""
        self.remove_tracked_window(card.tracked)
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
                (MENU_DISABLE_PIP, "Выключить PiP", False),
                (MENU_REMOVE, "Удалить из списка", False),
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
        elif command == MENU_DISABLE_PIP:
            self.disable_card(card)
        elif command == MENU_REMOVE:
            self.remove_card(card)

    def tracked_window_menu(self, tracked: TrackedWindow) -> None:
        if not any(item is tracked for item in self.config.windows):
            return
        command = winui.track_popup_menu(
            [
                (1, "Выключить PiP" if tracked.pip_enabled else "Включить PiP", tracked.pip_enabled),
                None,
                (2, "Удалить из списка", False),
            ],
            self.panel.hwnd,
        )
        if command == 1:
            self.toggle_tracked_pip(tracked)
        elif command == 2:
            self.remove_tracked_window(tracked)

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
        dialog = WindowSelectorDialog(
            self.root,
            self.finder,
            self.config.windows,
            tuple(card.src_hwnd for card in self.cards if card.src_hwnd),
        )
        picked = dialog.show(self.root)
        if not picked:
            return
        added = False
        for process, title, title_hint, class_hint, selected_hwnd in picked:
            safe_process = "" if process in {"?", "<нет доступа>"} else (process or "")
            tracked = TrackedWindow(
                process=safe_process,
                title_contains=title or "",
                pip_enabled=True,
                title_hint=title_hint or "",
                class_hint=class_hint or "",
                # Runtime-only binding: the first card attaches to exactly this
                # window, so several identically titled windows of one process can
                # be tracked separately.  HWNDs are not persisted.
                source_hwnd=int(selected_hwnd or 0) or None,
            )
            if any(existing.same_target(tracked) for existing in self.config.windows):
                continue
            self.config.windows.append(tracked)
            card = self._create_card_for_tracked(tracked)
            if card is None:
                self.config.windows.remove(tracked)
                continue
            added = True
        if added:
            self.panel.sync_layout()
            self.save_config()
            self.apply_topmost()
            self._do_refresh()
    def show_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.config)
        if not dialog.show(self.root):
            return
        if not self.autostart.set_enabled(self.config.autostart):
            self.config.autostart = self.autostart.is_enabled()
        self._sync_change_timer()
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

    def _start_shutdown_worker(self, *, wait: bool = False) -> bool:
        """Make sure the shutdown worker is already waiting for a request.

        The worker is started with the application rather than at exit, because
        starting a thread is the one part of a shutdown that can be done on the UI
        thread - and it is exactly what makes every *other* part possible without
        blocking the UI: the flush, the joins, the restore barrier, the journal
        reads, the guardian handshake and the fail-closed retry all run on it while
        the message pump keeps running.

        Returns whether a worker is available; when it is not, the shutdown must not
        happen inline.
        """
        with self._shutdown_lock:
            if self._shutdown_worker_ready.is_set() or self._shutdown_worker_started.is_set():
                return True
            self._shutdown_worker_started.set()
            if not self._start_daemon_worker(
                "LookUpWindows-Shutdown", self._shutdown_worker, ()
            ):
                self._shutdown_worker_started.clear()
                logger.error(
                    "The shutdown worker could not be started; the application will "
                    "stay alive rather than perform recovery work on the UI thread"
                )
                return False
            if not wait:
                return True
            # Thread.start() only guarantees the thread object exists; the worker sets
            # this once it is actually waiting, so a request is never lost in between.
            if not self._shutdown_worker_ready.wait(timeout=SHUTDOWN_WORKER_READY_TIMEOUT_SEC):
                logger.error(
                    "The shutdown worker did not report ready within %.0fs",
                    SHUTDOWN_WORKER_READY_TIMEOUT_SEC,
                )
                return False
            return True

    def _shutdown_worker(self) -> None:
        self._shutdown_worker_ready.set()
        while not self._shutdown_requested.wait(timeout=1.0):
            if self._defer_closed:
                return
        attempt = 0
        try:
            self._teardown_services()
            handed = self._handover_unfinished_restores()
            while not handed and not self._defer_closed:
                attempt += 1
                logger.error(
                    "Shutdown handover attempt %s did not transfer the outstanding "
                    "restore(s); retrying while the application stays alive",
                    attempt,
                )
                # Waiting here is what keeps the UI alive: the Tk thread is free to
                # paint, to answer input and to drain the deferred queue.
                time.sleep(2.0)
                handed = self._handover_unfinished_restores()
        except Exception:  # pragma: no cover - the shutdown must not be swallowed
            logger.exception("Shutdown failed")
            self._shutdown_worker_ready.clear()
            self._shutdown_worker_started.clear()
            self.defer(self._retry_shutdown)
            return
        self.defer(self._finish_shutdown)

    def _retry_shutdown(self) -> None:
        if not self._start_shutdown_worker(wait=False):
            self._notify_shutdown_unavailable()
            self._request_recovery_supervisor_from_timer()

    def _teardown_services(self) -> None:
        """The blocking part of the exit: flush, joins, the restore barrier."""
        if not self._config_saver.flush(timeout=2.0):
            logger.warning("Config saver did not flush cleanly before shutdown")
        if not self._config_saver.close(timeout=1.0):
            logger.warning("Config saver worker did not stop before shutdown deadline")
        if not self.detector.close(timeout=2.0):
            logger.warning("Change detector did not fully stop during shutdown")
        self._restore_sources_for_shutdown(deadline_sec=SHUTDOWN_RESTORE_DEADLINE_SEC)

    def quit(self) -> None:
        if self._shutting_down:
            # A second quit request (tray menu plus a window close) must not run
            # the teardown again: in particular it must not try to hand the same
            # obligation over a second time and then wait for itself.
            return
        if not self._start_shutdown_worker(wait=False):
            # No worker means no place to do the blocking work.  Doing it here would
            # put journal I/O, a guardian handshake and a retry loop on the Tk thread
            # with live obligations in the journal - the exact boundary this
            # architecture forbids - so the application stays as it is, keeps its
            # recovery ownership, and the user is told why.
            self._shutdown_started = True
            self._notify_shutdown_unavailable()
            self._request_recovery_supervisor_from_timer()
            return
        self._shutdown_started = False
        self._shutting_down = True
        self.save_config()
        for hotkey_id in tuple(self._registered_hotkeys):
            winui.unregister_hotkey(self.panel.hwnd, hotkey_id)
        self._registered_hotkeys.clear()
        for timer in (
            TIMER_REFRESH,
            TIMER_CHANGE,
            TIMER_SAVE_CONFIG,
            TIMER_REFRESH_SOON,
            TIMER_STARTUP,
            TIMER_PARKED_WATCH,
        ):
            winui.kill_timer(self.panel.hwnd, timer)
        self.close_fullscreen()
        # The panel keeps painting and pumping messages until the worker has really
        # finished: paint, input and the deferred queue must keep working while the
        # recovery barrier and the handover are in progress.
        winui.invalidate(self.panel.hwnd)
        if self.tray is not None:
            self.tray.notify("LookUp Windows", "Завершение: возврат исходных окон…")
        self._shutdown_requested.set()

    def _notify_shutdown_unavailable(self) -> None:
        logger.error(
            "Exit refused: the shutdown worker is not available, so the recovery "
            "handover would have to run on the UI thread"
        )
        if self.tray is not None:
            self.tray.notify(
                "LookUp Windows",
                "Не удалось начать завершение: приложение остаётся открытым, чтобы "
                "не потерять возврат исходного окна",
            )

    def _finish_shutdown(self) -> None:
        self._recovery_supervisor_stop.set()
        self._stop_deferred_pump()
        for card in self.cards:
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
    def __init__(
        self,
        master: tk.Misc,
        finder: WindowFinder,
        existing: list[TrackedWindow],
        bound_hwnds: tuple[int, ...] = (),
    ):
        self.result: list[tuple[str, str, str, str, int]] | None = None
        self.finder = finder
        self.existing = existing
        self.bound_hwnds = {int(hwnd) for hwnd in bound_hwnds if hwnd}
        self.windows: list[WindowCandidate] = []
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
        entry.bind("<KeyRelease>", lambda _e: self._filter())

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

    def _claimed_hwnds(self, windows: list[WindowCandidate]) -> set[int]:
        """Return the windows that are already shown by a configured card.

        The dialog used to hide every window matching a tracked entry.  For a
        process-only entry that filter matched each window of the process, so a
        second window of an already tracked application could not be picked at
        all.  Now only the window an entry really resolves to is hidden, which
        keeps "add a second window of the same app" possible.
        """
        claimed = set(self.bound_hwnds)
        for tracked in self.existing:
            if tracked.source_hwnd:
                claimed.add(int(tracked.source_hwnd))
                continue
            resolved = self.finder.find_preferred(tracked, windows)
            if resolved is not None:
                claimed.add(resolved.hwnd)
        return claimed

    def _load(self) -> None:
        self.windows = self.finder.list_windows()
        claimed = self._claimed_hwnds(self.windows)
        self.candidates = [candidate for candidate in self.windows if candidate.hwnd not in claimed]
        self._filter()

    def _filter(self) -> None:
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
            # Counted over every window of the process, including the ones already
            # bound to a card, so a second window of a tracked application still
            # gets a title discriminator.
            process_counts: dict[str, int] = {}
            for candidate in self.windows:
                key = (candidate.process_name or "").casefold()
                process_counts[key] = process_counts.get(key, 0) + 1
            result: list[tuple[str, str, str, str, int]] = []
            for index in indexes:
                if index >= len(self.shown):
                    continue
                candidate = self.shown[index]
                process = candidate.process_name or ""
                # A single window of a process is more robust when tracked by process only.
                # Multiple same-process windows need a title discriminator.
                title = candidate.title if (process in {"?", "<нет доступа>"} or process_counts.get(process.casefold(), 0) > 1) else ""
                result.append((
                    process,
                    title,
                    candidate.title or "",
                    candidate.info.class_name or "",
                    candidate.hwnd,
                ))
            self.result = result
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self, master: tk.Misc) -> list[tuple[str, str, str, str, int]] | None:
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
        for row_index, (label, variable) in enumerate(zip(labels, variables, strict=True)):
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

def _shutdown_logging(timeout: float = 2.0) -> None:
    """Stop the logging worker and flush what is still queued."""
    listener = _log_listener
    if listener is None:
        return
    try:
        listener.stop()
    except Exception:  # pragma: no cover - logging must never break exit
        pass
    _log_queue.put_nowait(None)


def main() -> None:
    # A frozen build is re-executed for the recovery guardian, so this dispatch
    # has to come before anything that assumes a single UI instance.
    if restoreguard.GUARDIAN_ARG in sys.argv[1:]:
        multiprocessing.freeze_support()
        _configure_logging()
        raise SystemExit(restoreguard.run_guardian_from_argv())
    multiprocessing.freeze_support()
    _configure_logging()
    logger.info("Starting %s %s", APP_NAME, APP_VERSION)
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
    try:
        app.run()
    finally:
        # The listener is a daemon thread, so anything still queued would be lost
        # when the process exits; flush it while the file handler is still open.
        _shutdown_logging()


if __name__ == "__main__":
    main()
