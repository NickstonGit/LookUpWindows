from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading

try:
    import winreg
except ImportError:  # allows config-model tests/static tools outside Windows
    winreg = None
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("lookupwindows")

APP_VERSION = "2026.10.05.1"
APP_AUTHOR = "Nickston"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "LookUpWindows"
DEFAULT_CARD_WIDTH = 280
MAX_PERSISTED_CARD_WIDTH = 16384

# The reader's and the writer's shared size contract.  A writer that does not
# check the serialised result can happily confirm a save whose very next load
# rejects - which means the user loses the change *and* the previous state is
# replaced by a backup of a file nobody can read.  One constant, both sides.
MAX_CONFIG_BYTES = 1024 * 1024

# Settings schema contract.
#
# A settings file is user-editable, so "valid JSON" is not enough to make a
# candidate loadable: wrong container types used to crash startup (and could
# then replace the last known-good backup with defaults on the next save), while
# truthy string flags such as "false" silently flipped every boolean.  Every key
# the application writes is therefore type-checked before a candidate is
# accepted; unknown keys stay tolerated so older builds can read newer files.
_BOOL_SETTING_KEYS = (
    "alwaysOnTop",
    "autostart",
    "autoRefind",
    "restoreMinimized",
    "changeDetection",
    "notifySound",
    "notifyWindowReturn",
    "hotkeysEnabled",
    "firstRunSelector",
)
_FLOAT_SETTING_KEYS = ("opacity", "changeIntervalSec", "changeThreshold")
_INT_SETTING_KEYS = ("ctrlX", "ctrlY")
_BOOL_WINDOW_KEYS = ("collapsed", "clickThrough", "detectChanges", "pipEnabled")
_STR_WINDOW_KEYS = ("process", "titleContains", "titleHint", "classHint")
_INT_WINDOW_KEYS = ("width", "x", "y")
_CROP_KEYS = ("mode", "x", "y", "width", "height")


class ConfigSchemaError(ValueError):
    """Raised when a settings document does not match the expected schema."""


def _is_strict_bool(value) -> bool:
    # bool("false") and bool("0") are both True, which used to turn explicitly
    # disabled cards into enabled ones.  Only real JSON booleans count.
    return isinstance(value, bool)


def _is_strict_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_crop_data(crop, where: str) -> None:
    if not isinstance(crop, dict):
        raise ConfigSchemaError(f"{where} must be an object")
    for key in _CROP_KEYS:
        if key not in crop:
            continue
        value = crop[key]
        if key == "mode":
            if not isinstance(value, str):
                raise ConfigSchemaError(f"{where}.mode must be a string")
        elif not _is_strict_number(value):
            raise ConfigSchemaError(f"{where}.{key} must be a finite number")


def validate_window_data(item, where: str) -> None:
    if not isinstance(item, dict):
        raise ConfigSchemaError(f"{where} must be an object")
    for key in _STR_WINDOW_KEYS:
        if key in item and not isinstance(item[key], str):
            raise ConfigSchemaError(f"{where}.{key} must be a string")
    for key in _INT_WINDOW_KEYS:
        if key not in item or item[key] is None:
            continue
        if not _is_strict_int(item[key]):
            raise ConfigSchemaError(f"{where}.{key} must be an integer or null")
    for key in _BOOL_WINDOW_KEYS:
        if key in item and not _is_strict_bool(item[key]):
            raise ConfigSchemaError(f"{where}.{key} must be a boolean")
    if item.get("crop") is not None:
        validate_crop_data(item["crop"], f"{where}.crop")


def validate_settings_data(data) -> None:
    """Raise :class:`ConfigSchemaError` when ``data`` is not a usable settings document."""
    if not isinstance(data, dict):
        raise ConfigSchemaError("settings root must be a JSON object")
    for key in _BOOL_SETTING_KEYS:
        if key in data and not _is_strict_bool(data[key]):
            raise ConfigSchemaError(f"{key} must be a boolean")
    for key in _FLOAT_SETTING_KEYS:
        if key in data and not _is_strict_number(data[key]):
            raise ConfigSchemaError(f"{key} must be a finite number")
    for key in _INT_SETTING_KEYS:
        if key in data and data[key] is not None and not _is_strict_int(data[key]):
            raise ConfigSchemaError(f"{key} must be an integer or null")
    windows = data.get("windows")
    if windows is not None:
        if not isinstance(windows, list):
            raise ConfigSchemaError("windows must be a list")
        for index, item in enumerate(windows):
            validate_window_data(item, f"windows[{index}]")
    profiles = data.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, dict):
            raise ConfigSchemaError("profiles must be an object")
        for name, items in profiles.items():
            if not isinstance(name, str):
                raise ConfigSchemaError("profile names must be strings")
            if not isinstance(items, list):
                raise ConfigSchemaError(f"profiles[{name!r}] must be a list")
            for index, item in enumerate(items):
                validate_window_data(item, f"profiles[{name!r}][{index}]")


def settings_schema_problem(data) -> str | None:
    """Return a human readable reason why ``data`` is unusable, or ``None``."""
    try:
        validate_settings_data(data)
    except ConfigSchemaError as exc:
        return str(exc)
    return None


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
    if explicit is not None:
        # "--config" without a value is a user mistake, not a request for the
        # default: silently ignoring it would start the app against the wrong
        # settings file.
        if not explicit.strip():
            raise SystemExit("--config requires a path to a settings file")
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
        values = (self.x, self.y, self.width, self.height)
        try:
            finite = all(math.isfinite(float(value)) for value in values)
        except (OverflowError, TypeError, ValueError):
            return False
        if not finite:
            return False
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
        if source_width <= 0 or source_height <= 0 or not self.is_valid():
            return None
        if self.is_relative():
            x = int(round(self.x * source_width))
            y = int(round(self.y * source_height))
            right = int(round((self.x + self.width) * source_width))
            bottom = int(round((self.y + self.height) * source_height))
            return CropRect(x=x, y=y, width=max(1, right - x), height=max(1, bottom - y))
        return CropRect(x=int(round(self.x)), y=int(round(self.y)), width=int(round(self.width)), height=int(round(self.height)))

    def as_rect(self) -> tuple[int, int, int, int]:
        if not self.is_valid():
            raise ValueError("invalid crop rectangle")
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
        if not self.is_valid():
            raise ValueError("invalid crop rectangle")
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
        except (OverflowError, TypeError, ValueError):
            return None
        return crop if crop.is_valid() else None


def _as_strict_bool(value, fallback: bool) -> bool:
    """Return ``value`` only when it is a real JSON boolean, else ``fallback``."""
    return value if isinstance(value, bool) else fallback


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
    pip_enabled: bool = True
    # Soft identity hints used only to disambiguate auto-refind.  They are not
    # strict filters, because normal applications change their window titles.
    title_hint: str = ""
    class_hint: str = ""
    # Runtime-only binding selected from the window selector.
    # Not persisted: HWNDs are recreated after reboot.
    source_hwnd: int | None = None

    def to_dict(self) -> dict:
        data = {
            "process": self.process,
            "titleContains": self.title_contains,
            "width": int(self.width),
            "collapsed": bool(self.collapsed),
            "clickThrough": bool(self.click_through),
            "detectChanges": bool(self.detect_changes),
            "pipEnabled": bool(self.pip_enabled),
        }
        if self.title_hint:
            data["titleHint"] = self.title_hint
        if self.class_hint:
            data["classHint"] = self.class_hint
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
            collapsed=_as_strict_bool(data.get("collapsed"), False),
            click_through=_as_strict_bool(data.get("clickThrough"), False),
            detect_changes=_as_strict_bool(data.get("detectChanges"), True),
            pip_enabled=_as_strict_bool(data.get("pipEnabled"), True),
            title_hint=str(data.get("titleHint", "") or "").strip(),
            class_hint=str(data.get("classHint", "") or "").strip(),
        )

    def clone(self) -> "TrackedWindow":
        cloned = TrackedWindow.from_dict(self.to_dict())
        if cloned is None:
            return TrackedWindow(process=self.process, title_contains=self.title_contains)
        # The live window binding is runtime state, so it survives cloning even
        # though it is never written to the settings file.
        cloned.source_hwnd = self.source_hwnd
        return cloned

    def display_name(self) -> str:
        return self.title_contains or self.process or "Окно"

    def same_target(self, other: "TrackedWindow") -> bool:
        if (
            self.process.lower() != other.process.lower()
            or self.title_contains.lower() != other.title_contains.lower()
        ):
            return False
        # Identical process + title filters are only the same target when both
        # entries point at the same window.  One process can expose several
        # windows with an identical title (the same project opened twice), and
        # each of them needs its own PiP card.  Entries without a live binding
        # keep the conservative legacy dedup.
        if not self.source_hwnd or not other.source_hwnd:
            return True
        return self.source_hwnd == other.source_hwnd


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
        def finite(value, fallback: float) -> float:
            try:
                parsed = float(value)
            except (OverflowError, TypeError, ValueError):
                return fallback
            return parsed if math.isfinite(parsed) else fallback

        self.opacity = max(0.3, min(1.0, finite(self.opacity, 0.95)))
        self.change_interval_sec = max(1.0, min(60.0, finite(self.change_interval_sec, 3.0)))
        self.change_threshold = max(0.01, min(0.9, finite(self.change_threshold, 0.08)))
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
            return value if isinstance(value, bool) else fallback

        def as_float(value, fallback: float) -> float:
            try:
                parsed = float(value)
            except (OverflowError, TypeError, ValueError):
                return fallback
            return parsed if math.isfinite(parsed) else fallback

        def as_int(value, fallback: int) -> int:
            try:
                return int(value)
            except (OverflowError, TypeError, ValueError):
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
        # A schema-valid document always has a list here, but from_dict() is also
        # reachable from tests and legacy callers, so a wrong container type must
        # degrade to defaults instead of raising TypeError during startup.
        raw_windows = data.get("windows")
        if isinstance(raw_windows, dict):
            raw_windows = list(raw_windows.values())
        elif not isinstance(raw_windows, (list, tuple)):
            raw_windows = []
        for item in raw_windows:
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

    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    def quarantined_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".invalid")

    @staticmethod
    def _read_document(candidate: Path):
        """Read and schema-check one candidate file.

        Returns ``None`` when the file is missing, too large, syntactically
        broken or schema-invalid.  A JSON-valid but schema-invalid document is
        treated exactly like corruption: it must never become the live
        configuration and must never become the backup.
        """
        try:
            if candidate.stat().st_size > MAX_CONFIG_BYTES:
                logger.warning(
                    "Ignoring settings file %s: it is larger than %s bytes",
                    candidate,
                    MAX_CONFIG_BYTES,
                )
                return None
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle, parse_constant=ConfigService._reject_json_constant)
        except (OSError, OverflowError, ValueError, json.JSONDecodeError, RecursionError):
            return None
        problem = settings_schema_problem(data)
        if problem is not None:
            logger.warning("Ignoring unusable settings file %s: %s", candidate, problem)
            return None
        return data

    def _quarantine(self, candidate: Path) -> None:
        """Move a schema-invalid primary aside so it cannot be reloaded later."""
        try:
            if not candidate.exists():
                return
            os.replace(candidate, self.quarantined_path())
            logger.warning(
                "Quarantined unusable settings file %s as %s",
                candidate,
                self.quarantined_path(),
            )
        except OSError:
            logger.warning("Could not quarantine unusable settings file %s", candidate)

    def load(self) -> AppConfig:
        candidates = [self.path, self.backup_path()]
        legacy = app_dir() / "config" / "settings.json"
        # An explicit destination is isolated from the source tree's local
        # settings. Migration applies only to the default installed location.
        if self.path == default_settings_path() and not _runtime_option("--config") and legacy not in candidates:
            candidates.append(legacy)
        for candidate in candidates:
            data = self._read_document(candidate)
            if data is None:
                if candidate == self.path:
                    # Keep the last known-good backup intact, but do not leave a
                    # broken primary in place to be re-read or re-backed-up.
                    self._quarantine(candidate)
                continue
            config = AppConfig.from_dict(data)
            if candidate != self.path:
                self.save(config)
            return config
        return AppConfig()

    def save(self, config: AppConfig) -> bool:
        return self.save_dict(config.to_dict())

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError(f"non-finite JSON constant is not allowed: {value}")

    def _replace_backup_atomically(self, source: Path) -> None:
        """Replace the known-good backup without ever truncating it in place."""
        backup = self.backup_path()
        backup.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(backup.parent), suffix=".bak.tmp")
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp_name, backup)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save_dict(self, data: dict) -> bool:
        problem = settings_schema_problem(data)
        if problem is not None:
            logger.warning("Refusing to persist invalid settings payload: %s", problem)
            return False
        # Serialise first and measure the result.  A schema-valid snapshot can
        # still exceed what the reader accepts (many profiles, long filter
        # strings), and writing it anyway turns a confirmed save into a file the
        # next start quarantines - taking the change *and* the previous state
        # with it.  The bytes are handed to the temp file unchanged, so what is
        # measured is exactly what lands on disk.
        try:
            encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.warning("Refusing to persist settings that cannot be encoded: %s", exc)
            return False
        if len(encoded) > MAX_CONFIG_BYTES:
            logger.error(
                "Refusing to persist settings: the serialised document is %s bytes, "
                "more than the %s bytes a settings file may contain; the previous "
                "settings were kept",
                len(encoded),
                MAX_CONFIG_BYTES,
            )
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists() and self._read_document(self.path) is not None:
                    # Only a schema-valid primary may become the backup; an
                    # invalid one would destroy the last known-good copy.
                    self._replace_backup_atomically(self.path)
                os.replace(tmp_name, self.path)
            except (OSError, ValueError):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except (OSError, ValueError):
            return False


class AsyncConfigSaver:
    """Single-writer, coalescing config persistence worker.

    The UI thread only creates an immutable dict snapshot.  File reads, JSON
    encoding, fsync, backup copy and atomic replace all run on this worker, so a
    slow/filtered/network-backed settings path cannot stall native UI dispatch.
    """

    def __init__(self, service: ConfigService, on_result=None):
        self._service = service
        self._on_result = on_result
        self._condition = threading.Condition()
        self._pending: dict | None = None
        self._saving = False
        self._closed = False
        self._last_result = True
        self._worker = threading.Thread(
            target=self._run,
            name="LookUpWindows-ConfigSaver",
            daemon=True,
        )
        self._worker.start()

    def submit(self, config: AppConfig) -> bool:
        snapshot = config.to_dict()
        with self._condition:
            if self._closed:
                return False
            # Latest state wins.  This intentionally coalesces drag/resize bursts.
            self._pending = snapshot
            self._condition.notify()
        return True

    def flush(self, timeout: float | None = None) -> bool:
        with self._condition:
            done = self._condition.wait_for(
                lambda: self._pending is None and not self._saving,
                timeout=None if timeout is None else max(0.0, float(timeout)),
            )
            return bool(done and self._last_result)

    def close(self, timeout: float = 2.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._worker is not threading.current_thread():
            self._worker.join(timeout=max(0.0, float(timeout)))
        return not self._worker.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._pending is None and self._closed:
                    return
                data = self._pending
                self._pending = None
                self._saving = True

            try:
                ok = self._service.save_dict(data or {})
            except Exception:
                logger.exception("Unexpected settings persistence failure")
                ok = False

            with self._condition:
                self._saving = False
                self._last_result = ok
                self._condition.notify_all()

            callback = self._on_result
            if callback is not None:
                try:
                    callback(ok)
                except Exception:
                    # Persistence must never die because a UI notification hook
                    # disappeared during shutdown.
                    pass


class Autostart:
    def _runtime_args(self) -> list[str]:
        args: list[str] = ["--background"]
        if portable_mode():
            args.append("--portable")
        explicit = _runtime_option("--config")
        if explicit:
            resolved = Path(os.path.expandvars(explicit)).expanduser().resolve()
            args.extend(["--config", str(resolved)])
        return args

    def run_command(self) -> str:
        if getattr(sys, "frozen", False):
            current = Path(sys.executable).resolve()
            stable = current.with_name("LookUpWindows.exe")
            executable = stable if stable.exists() else current
            return subprocess.list2cmdline([str(executable), *self._runtime_args()])
        script = PROJECT_ROOT / "src" / "app.py"
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
