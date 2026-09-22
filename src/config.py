from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import winreg
except ImportError:  # allows config-model tests/static tools outside Windows
    winreg = None
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"

APP_VERSION = "2026.09.22.10"
APP_AUTHOR = "Nickston"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "LookUpWindows"
DEFAULT_CARD_WIDTH = 280
MAX_PERSISTED_CARD_WIDTH = 16384


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def _runtime_option(name: str) -> str | None:
    for index, arg in enumerate(sys.argv[1:]):
        if arg == name:
            absolute_index = index + 1
            if absolute_index + 1 < len(sys.argv):
                return sys.argv[absolute_index + 1]
            return ""
        prefix = name + "="
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def portable_mode() -> bool:
    return "--portable" in sys.argv[1:] or os.environ.get("LOOKUPWINDOWS_PORTABLE") == "1"


def background_mode() -> bool:
    return "--background" in sys.argv[1:]


def default_settings_path() -> Path:
    explicit = _runtime_option("--config")
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser().resolve()
    if portable_mode() or not getattr(sys, "frozen", False):
        return app_dir() / "config" / "settings.json"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LookUpWindows" / "settings.json"
    return app_dir() / "config" / "settings.json"


@dataclass
class CropRect:
    """Crop definition.

    absolute: x/y/width/height are source pixels.
    relative: x/y/width/height are normalized values in 0..1.
    """

    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    mode: str = "absolute"

    def is_relative(self) -> bool:
        return self.mode == "relative"

    def is_valid(self) -> bool:
        if self.width <= 0 or self.height <= 0:
            return False
        if self.is_relative():
            return (
                0 <= self.x < 1
                and 0 <= self.y < 1
                and self.x + self.width <= 1.000001
                and self.y + self.height <= 1.000001
            )
        return self.x >= 0 and self.y >= 0

    def resolved(self, source_width: int, source_height: int) -> "CropRect | None":
        if source_width <= 0 or source_height <= 0:
            return None
        if self.is_relative():
            x = int(round(self.x * source_width))
            y = int(round(self.y * source_height))
            right = int(round((self.x + self.width) * source_width))
            bottom = int(round((self.y + self.height) * source_height))
            return CropRect(x=x, y=y, width=max(1, right - x), height=max(1, bottom - y))
        return CropRect(x=int(round(self.x)), y=int(round(self.y)), width=int(round(self.width)), height=int(round(self.height)))

    def as_rect(self) -> tuple[int, int, int, int]:
        return (
            int(round(self.x)),
            int(round(self.y)),
            int(round(self.x + self.width)),
            int(round(self.y + self.height)),
        )

    def clamped(self, source_width: int, source_height: int) -> "CropRect | None":
        resolved = self.resolved(source_width, source_height)
        if resolved is None:
            return None
        x = max(0, min(int(resolved.x), source_width - 1))
        y = max(0, min(int(resolved.y), source_height - 1))
        right = max(x + 1, min(source_width, int(resolved.x) + int(resolved.width)))
        bottom = max(y + 1, min(source_height, int(resolved.y) + int(resolved.height)))
        crop = CropRect(x=x, y=y, width=right - x, height=bottom - y)
        return crop if crop.is_valid() else None

    def to_dict(self) -> dict:
        if self.is_relative():
            return {
                "mode": "relative",
                "x": round(float(self.x), 6),
                "y": round(float(self.y), 6),
                "width": round(float(self.width), 6),
                "height": round(float(self.height), 6),
            }
        return {
            "mode": "absolute",
            "x": int(round(self.x)),
            "y": int(round(self.y)),
            "width": int(round(self.width)),
            "height": int(round(self.height)),
        }

    @classmethod
    def from_dict(cls, data) -> "CropRect | None":
        if not isinstance(data, dict):
            return None
        mode = str(data.get("mode", "absolute")).strip().lower()
        if mode not in {"absolute", "relative"}:
            mode = "absolute"
        try:
            crop = cls(
                x=float(data.get("x", 0)),
                y=float(data.get("y", 0)),
                width=float(data.get("width", 0)),
                height=float(data.get("height", 0)),
                mode=mode,
            )
        except (TypeError, ValueError):
            return None
        return crop if crop.is_valid() else None


@dataclass
class TrackedWindow:
    process: str = ""
    title_contains: str = ""
    crop: CropRect | None = None
    x: int | None = None
    y: int | None = None
    width: int = DEFAULT_CARD_WIDTH
    collapsed: bool = False
    click_through: bool = False
    detect_changes: bool = True

    def to_dict(self) -> dict:
        data = {
            "process": self.process,
            "titleContains": self.title_contains,
            "width": int(self.width),
            "collapsed": bool(self.collapsed),
            "clickThrough": bool(self.click_through),
            "detectChanges": bool(self.detect_changes),
        }
        if self.crop and self.crop.is_valid():
            data["crop"] = self.crop.to_dict()
        if self.x is not None:
            data["x"] = self.x
        if self.y is not None:
            data["y"] = self.y
        return data

    @classmethod
    def from_dict(cls, data) -> "TrackedWindow | None":
        if not isinstance(data, dict):
            return None
        process = str(data.get("process", "")).strip()
        title_contains = str(data.get("titleContains", "")).strip()
        if not process and not title_contains:
            return None
        crop = CropRect.from_dict(data.get("crop")) if data.get("crop") else None
        try:
            x = int(data["x"]) if data.get("x") is not None else None
            y = int(data["y"]) if data.get("y") is not None else None
            width = int(data.get("width", DEFAULT_CARD_WIDTH))
        except (TypeError, ValueError):
            x = y = None
            width = DEFAULT_CARD_WIDTH
        width = max(180, min(MAX_PERSISTED_CARD_WIDTH, width))
        return cls(
            process=process,
            title_contains=title_contains,
            crop=crop,
            x=x,
            y=y,
            width=width,
            collapsed=bool(data.get("collapsed", False)),
            click_through=bool(data.get("clickThrough", False)),
            detect_changes=bool(data.get("detectChanges", True)),
        )

    def clone(self) -> "TrackedWindow":
        cloned = TrackedWindow.from_dict(self.to_dict())
        return cloned if cloned is not None else TrackedWindow(process=self.process, title_contains=self.title_contains)

    def display_name(self) -> str:
        return self.title_contains or self.process or "Окно"

    def same_target(self, other: "TrackedWindow") -> bool:
        return (
            self.process.lower() == other.process.lower()
            and self.title_contains.lower() == other.title_contains.lower()
        )


@dataclass
class AppConfig:
    opacity: float = 0.95
    always_on_top: bool = True
    autostart: bool = False
    auto_refind: bool = True
    restore_minimized: bool = True
    ctrl_x: int | None = None
    ctrl_y: int | None = None
    change_detection: bool = False
    change_interval_sec: float = 3.0
    change_threshold: float = 0.08
    notify_sound: bool = True
    notify_window_return: bool = True
    hotkeys_enabled: bool = True
    first_run_selector: bool = True
    windows: list[TrackedWindow] = field(default_factory=list)
    profiles: dict[str, list[TrackedWindow]] = field(default_factory=dict)

    def clamped(self) -> "AppConfig":
        self.opacity = max(0.3, min(1.0, float(self.opacity)))
        self.change_interval_sec = max(1.0, min(60.0, float(self.change_interval_sec)))
        self.change_threshold = max(0.01, min(0.9, float(self.change_threshold)))
        return self

    def to_dict(self) -> dict:
        return {
            "opacity": self.opacity,
            "alwaysOnTop": self.always_on_top,
            "autostart": self.autostart,
            "autoRefind": self.auto_refind,
            "restoreMinimized": self.restore_minimized,
            "ctrlX": self.ctrl_x,
            "ctrlY": self.ctrl_y,
            "changeDetection": self.change_detection,
            "changeIntervalSec": self.change_interval_sec,
            "changeThreshold": self.change_threshold,
            "notifySound": self.notify_sound,
            "notifyWindowReturn": self.notify_window_return,
            "hotkeysEnabled": self.hotkeys_enabled,
            "firstRunSelector": self.first_run_selector,
            "windows": [window.to_dict() for window in self.windows],
            "profiles": {
                name: [window.to_dict() for window in windows]
                for name, windows in sorted(self.profiles.items(), key=lambda item: item[0].casefold())
            },
        }

    @classmethod
    def from_dict(cls, data) -> "AppConfig":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg

        def as_bool(value, fallback: bool) -> bool:
            return bool(value) if isinstance(value, bool) else fallback

        def as_float(value, fallback: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        def as_int(value, fallback: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        cfg.opacity = as_float(data.get("opacity"), cfg.opacity)
        cfg.always_on_top = as_bool(data.get("alwaysOnTop"), cfg.always_on_top)
        cfg.autostart = as_bool(data.get("autostart"), cfg.autostart)
        cfg.auto_refind = as_bool(data.get("autoRefind"), cfg.auto_refind)
        cfg.restore_minimized = as_bool(data.get("restoreMinimized"), cfg.restore_minimized)
        if data.get("ctrlX") is not None:
            cfg.ctrl_x = as_int(data.get("ctrlX"), 0)
        if data.get("ctrlY") is not None:
            cfg.ctrl_y = as_int(data.get("ctrlY"), 0)
        cfg.change_detection = as_bool(data.get("changeDetection"), cfg.change_detection)
        cfg.change_interval_sec = as_float(data.get("changeIntervalSec"), cfg.change_interval_sec)
        cfg.change_threshold = as_float(data.get("changeThreshold"), cfg.change_threshold)
        cfg.notify_sound = as_bool(data.get("notifySound"), cfg.notify_sound)
        cfg.notify_window_return = as_bool(data.get("notifyWindowReturn"), cfg.notify_window_return)
        cfg.hotkeys_enabled = as_bool(data.get("hotkeysEnabled"), cfg.hotkeys_enabled)
        cfg.first_run_selector = as_bool(data.get("firstRunSelector"), cfg.first_run_selector)

        cfg.windows = []
        for item in data.get("windows") or []:
            tracked = TrackedWindow.from_dict(item)
            if tracked is not None:
                cfg.windows.append(tracked)

        profiles: dict[str, list[TrackedWindow]] = {}
        raw_profiles = data.get("profiles")
        if isinstance(raw_profiles, dict):
            for raw_name, items in raw_profiles.items():
                name = str(raw_name).strip()
                if not name or not isinstance(items, list):
                    continue
                profile_windows: list[TrackedWindow] = []
                for item in items:
                    tracked = TrackedWindow.from_dict(item)
                    if tracked is not None:
                        profile_windows.append(tracked)
                profiles[name] = profile_windows
        cfg.profiles = profiles
        return cfg.clamped()


class ConfigService:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_settings_path()

    def load(self) -> AppConfig:
        candidates = [self.path, self.path.with_suffix(self.path.suffix + ".bak")]
        legacy = app_dir() / "config" / "settings.json"
        if legacy not in candidates:
            candidates.append(legacy)
        for candidate in candidates:
            try:
                if candidate.stat().st_size > 1024 * 1024:
                    continue
                with open(candidate, "r", encoding="utf-8") as handle:
                    config = AppConfig.from_dict(json.load(handle))
                if candidate != self.path:
                    self.save(config)
                return config
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return AppConfig()

    def save(self, config: AppConfig) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists():
                    try:
                        with open(self.path, "r", encoding="utf-8") as current:
                            json.load(current)
                        shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
                os.replace(tmp_name, self.path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except OSError:
            return False


class Autostart:
    def _runtime_args(self) -> list[str]:
        args: list[str] = ["--background"]
        if portable_mode():
            args.append("--portable")
        explicit = _runtime_option("--config")
        if explicit:
            args.extend(["--config", explicit])
        return args

    def run_command(self) -> str:
        if getattr(sys, "frozen", False):
            current = Path(sys.executable).resolve()
            stable = current.with_name("LookUpWindows.exe")
            executable = stable if stable.exists() else current
            return subprocess.list2cmdline([str(executable), *self._runtime_args()])
        script = PROJECT_ROOT / "app.py"
        exe = Path(sys.executable)
        pythonw = exe.parent / "pythonw.exe"
        runner = pythonw if pythonw.exists() else exe
        return subprocess.list2cmdline([str(runner), str(script), *self._runtime_args()])

    def current_value(self) -> str | None:
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, AUTOSTART_VALUE)
                return str(value)
        except OSError:
            return None

    def is_enabled(self) -> bool:
        return self.current_value() is not None

    def is_current(self) -> bool:
        value = self.current_value()
        return value is not None and value.casefold() == self.run_command().casefold()

    def set_enabled(self, enabled: bool) -> bool:
        if winreg is None:
            return False
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, self.run_command())
                else:
                    try:
                        winreg.DeleteValue(key, AUTOSTART_VALUE)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False
