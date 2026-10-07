"""Runtime readiness/restore smoke for the real LookUp Windows application.

The unit suite is mostly static, so a release needs a check that starts
the *actual* application (frozen onefile artifact or a source run), waits for
real UI readiness, exercises the park/restore path against a foreign window and
then quits cleanly.  It is also the regression gate for the failure modes listed
below, and each scenario states the invariant it enforces:

* ``responsive`` - park, graceful quit: the source is restored *inside* the
  shutting-down process, the journal ends empty, no recovery guardian is needed
  and no helper process survives.
* ``slow``       - park a source that stalls for seconds, quit inside the restore
  deadline: the obligation is handed to the recovery guardian and the source
  must come back **without any restart**.
* ``inflight``   - quit while the park itself is still moving the foreign window:
  the pending intent is executed by the guardian, again without a restart.
* ``hardkill``   - park, terminate the application without warning.  No guardian
  can exist, so the journal itself has to be enough: a fresh start restores.
* ``aged``       - park, hard kill, then age the record by eight days: age must
  not cancel an outstanding obligation.
* ``badjournal`` - start with malformed journal documents: LookUp must start,
  quarantine them and still park/restore normally.
* ``jobfail``    - force the capture-helper job binding to fail: no unbound
  helper may ever be started or used, while the application stays usable.
* ``journalrace``- park a source that stalls, quit inside the restore deadline,
  and let a real recovery guardian inherit it.  A second *process* then parks
  another window and records an obligation in the same journal while the
  guardian is executing its own: no outstanding obligation may be lost, every
  window has to come back and no guardian may be left behind.
* ``journalio``  - deny reads of a live recovery journal: a new park must be
  refused, the older obligation preserved, and both windows recovered later.
* ``guardianfail`` - stall the guardian before readiness: timeout and cleanup
  must be bounded, including the onefile child; a healthy guardian then recovers.
* ``guardianlimit`` - hand a live parked obligation to the frozen guardian with a
  fast retry interval and a target that stays unresponsive for a long time: the
  guardian must keep owning and re-attempting the restore instead of reaching a
  lifetime limit, and it may only leave after the window is verifiably back.
* ``ownerpidreuse`` - plant an obligation whose owner PID belongs to a *live*
  process with a different creation time: the PID must not be mistaken for the
  identity of the dead owner, and recovery must proceed.
* ``badlease``    - plant an obligation whose lease is ``Infinity``/``NaN``: it
  must not be permanently unclaimable, and the window must still come back.
* ``claimaba``    - plant an obligation with a stale claim, let a newer executor
  take it over, and then let the stale holder try to end the obligation: the
  mutation must be rejected and the record must survive.
* ``monitorgap``  - a window that sits inside the virtual desktop's bounding box
  but on no monitor (the gap of an L-shaped layout) must count as *not*
  visible, so recovery cannot report success for an unreachable window.
* ``outerjob``    - run a guardian launch from a process deliberately placed in
  a kill-on-close Windows Job Object that forbids breakaway: the application must
  reject that guardian instead of accepting an executor that can be killed
  together with its owner.

Usage::

    python tools/runtime_smoke.py --mode exe --exe dist/LookUpWindows.exe
    python tools/runtime_smoke.py --mode source
    python tools/runtime_smoke.py --mode exe --exe dist/LookUpWindows.exe --scenario hardkill
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import restoreguard  # noqa: E402  (after sys.path bootstrap)
import screen  # noqa: E402  (after sys.path bootstrap)
import winapi  # noqa: E402  (after sys.path bootstrap)
from ctypes import wintypes  # noqa: E402

APP_TITLE = "LUW runtime smoke target"
GUARDIAN_TOKEN = restoreguard.GUARDIAN_ARG
# How long a stalled target keeps its window procedure busy.
STALL_SEC = 6.0

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
user32 = winapi.user32
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(winapi.wintypes.RECT)]
user32.GetClientRect.restype = ctypes.c_int


def click_card(hwnd: int) -> None:
    """Click the middle of a card window, which toggles its source."""
    rect = winapi.wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise SmokeError(f"could not read card client rect (hwnd={hwnd})")
    x = max(1, (rect.right - rect.left) // 2)
    y = max(1, (rect.bottom - rect.top) // 2)
    position = (y << 16) | (x & 0xFFFF)
    user32.SendMessageW(hwnd, WM_LBUTTONDOWN, 1, position)
    time.sleep(0.05)
    user32.SendMessageW(hwnd, WM_LBUTTONUP, 0, position)
    log(f"clicked card hwnd={hwnd} at client ({x},{y})")


class SmokeError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def wait_for(predicate, timeout: float, interval: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise SmokeError(f"timed out after {timeout:.1f}s waiting for {what}")


def find_window(class_name: str) -> int:
    return int(winapi.user32.FindWindowW(class_name, None) or 0)


def find_window_of(class_name: str, pid: int) -> int:
    """First window of ``class_name`` that belongs to ``pid``.

    Two instances of the same executable register the same window classes, so a
    scenario that runs two of them must never look a window up by class alone:
    it would drive the other instance instead of the one it started.
    """
    wanted = int(pid)
    found = 0

    @winapi.WNDENUMPROC
    def callback(hwnd, _lparam):
        nonlocal found
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        if buffer.value == class_name and int(winapi.get_pid(int(hwnd) or 0)) == wanted:
            found = int(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found


def child_pids(pid: int) -> list[int]:
    """Return the live direct children of ``pid`` (used for capture helpers)."""
    parent = int(pid)
    # The PID is interpolated directly: Windows PowerShell appends trailing
    # arguments to the command text instead of populating $args.
    script = f"$ErrorActionPreference='Stop'; (Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent}').ProcessId"
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SmokeError(f"could not enumerate Windows processes: {exc}") from exc
    if completed.returncode:
        raise SmokeError(f"could not enumerate Windows processes: {completed.stderr.strip()}")
    return [int(line) for line in completed.stdout.split() if line.strip().isdigit()]


def guardian_pids() -> list[int]:
    """Every running recovery guardian, identified by its command line.

    This is the only honest way to observe the handover: the guardian is
    deliberately not a child of any PID we could guess (a frozen build wraps it
    in its own bootloader), so the smoke looks for the marker argument itself.
    """
    script = (
        "$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -Filter "
        "\"Name='LookUpWindows.exe' OR Name='python.exe' OR Name='pythonw.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{GUARDIAN_TOKEN}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SmokeError(f"could not enumerate Windows processes: {exc}") from exc
    if completed.returncode:
        raise SmokeError(f"could not enumerate Windows processes: {completed.stderr.strip()}")
    return [int(line) for line in completed.stdout.split() if line.strip().isdigit()]


class Target:
    """Foreign window in a separate process."""

    def __init__(self, hang: float = 0.0, stall_on: str = "moved", title: str = APP_TITLE):
        self.hang = hang
        self.stall_on = stall_on
        self.title = title
        self.process: subprocess.Popen | None = None
        self.hwnd = 0
        self.tid = 0

    def __enter__(self) -> "Target":
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "smoke_target.py"), "--title", self.title,
             "--hang", str(self.hang), "--stall-on", self.stall_on],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.process.stdout.readline()
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise SmokeError(f"smoke target did not report a window: {line!r} ({exc})") from exc
        if "hwnd" not in payload:
            raise SmokeError(f"smoke target failed: {payload}")
        self.hwnd = int(payload["hwnd"])
        self.tid = int(payload.get("tid") or 0)
        log(
            f"foreign target window hwnd={self.hwnd} tid={self.tid} "
            f"hang={self.hang} stall_on={self.stall_on}"
        )
        return self

    def __exit__(self, *_exc) -> None:
        if self.process is None:
            return
        try:
            winapi.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        finally:
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()


class AppRun:
    """One application process under test."""

    def __init__(self, mode: str, exe: Path | None, settings: Path, extra_env: dict | None = None):
        self.mode = mode
        self.exe = exe
        self.settings = settings
        self.extra_env = dict(extra_env or {})
        self.process: subprocess.Popen | None = None
        self.pid = 0
        # One-file builds run the bootloader plus a child process; the real
        # application (and therefore the capture helpers) lives in the child.
        self.app_pid = 0

    @property
    def owner_pid(self) -> int:
        return self.app_pid or self.pid

    def resolve_app_pid(self, timeout: float = 60.0) -> int:
        """The process that owns the application windows.

        A onefile build runs the bootloader plus exactly one child process, and
        every window belongs to the child.  A scenario that has to address one
        specific instance (two applications, one recovery journal) therefore
        cannot use the launcher PID to look a window up.
        """
        if self.app_pid or self.mode != "exe":
            return self.owner_pid

        def child_app_pid() -> int:
            # onefile owns its windows in a child process.
            # Resolve using the actual panel, never by child list ordering (a
            # capture helper is also a child but owns no management window).
            if find_window_of("WPCtrl", self.pid):
                return self.pid
            for pid in child_pids(self.pid):
                if find_window_of("WPCtrl", pid):
                    return pid
            return 0

        self.app_pid = wait_for(
            child_app_pid,
            timeout,
            interval=0.5,
            what="the process owning the application panel",
        )
        return self.app_pid

    @property
    def journal(self) -> Path:
        return Path(str(self.settings) + ".park.json")

    def start(self) -> "AppRun":
        if self.mode == "exe":
            if self.exe is None or not self.exe.exists():
                raise SmokeError(f"executable not found: {self.exe}")
            command = [str(self.exe), "--background", "--config", str(self.settings)]
        else:
            command = [sys.executable, str(ROOT / "src" / "app.py"), "--background",
                       "--config", str(self.settings)]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        environment = os.environ.copy()
        # The test hooks are the only reason the application ever runs with a
        # fault injected; never inherit them silently into a normal start.
        environment.pop("LOOKUPWINDOWS_TEST_HOOKS", None)
        environment.update(self.extra_env)
        self.process = subprocess.Popen(
            command,
            startupinfo=startupinfo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        self.pid = self.process.pid
        return self

    def wait_panel(self, timeout: float) -> int:
        """Real UI readiness: the native control panel window must exist."""
        pid = self.resolve_app_pid(timeout)
        hwnd = wait_for(
            lambda: find_window_of("WPCtrl", pid),
            timeout,
            what="WPCtrl panel window",
        )
        self.app_pid = winapi.get_pid(hwnd)
        log(f"panel window ready hwnd={hwnd} app pid={self.app_pid}")
        return hwnd

    def wait_card(self, timeout: float) -> int:
        pid = self.resolve_app_pid(timeout)
        hwnd = wait_for(
            lambda: find_window_of("WPCard", pid),
            timeout,
            what="WPCard window",
        )
        log(f"card window ready hwnd={hwnd}")
        return hwnd

    def wait_exit(self, timeout: float) -> int:
        if self.process is None:
            raise SmokeError("process was not started")
        code = self.process.wait(timeout=timeout)
        log(f"application exited with code {code}")
        return code

    def request_quit(self) -> None:
        import winui

        if not winui.request_graceful_quit(timeout_ms=3000):
            raise SmokeError("running instance did not accept the graceful quit request")

    def kill(self) -> None:
        if self.process is None:
            return
        # Kill the application process itself (for one-file builds that is the
        # child of the bootloader) and deliberately without /T: killing the
        # whole tree would hide whether the OS-level lifetime binding
        # (kill-on-close Job Object) removed the capture helpers by itself.
        killed = subprocess.run(
            ["taskkill", "/F", "/PID", str(self.owner_pid)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if killed.returncode and pid_alive(self.owner_pid):
            raise SmokeError(f"could not terminate test application: {killed.stderr.strip()}")
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log("launcher did not exit after the application was killed")
        deadline = time.monotonic() + 15.0
        while find_window_of("WPCtrl", self.owner_pid) and time.monotonic() < deadline:
            time.sleep(0.25)
        log("application terminated without warning (simulated crash/taskkill)")

    def wait_helpers(self, timeout: float) -> list[int]:
        """Capture helpers must actually start: this exercises frozen multiprocessing."""
        return wait_for(
            lambda: self.helper_pids(),
            timeout,
            interval=0.5,
            what="capture helper process(es)",
        )

    def helper_pids(self) -> list[int]:
        guardians = set(guardian_pids())
        return [pid for pid in child_pids(self.owner_pid) if pid not in guardians]

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def app_log_path() -> Path:
    """Where the application writes its rotating log."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) / "LookUpWindows" if local_app_data else ROOT
    return base / "logs" / "lookupwindows.log"


def log_lines_since(path: Path, since: float) -> list[str]:
    """Log lines written at or after ``since`` (a ``time.time()`` stamp)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = []
    for line in text.splitlines():
        stamp = _log_timestamp(line)
        if stamp is not None and stamp >= since:
            lines.append(line)
    return lines


def _log_timestamp(line: str) -> float | None:
    stamp = line.split(" ", 2)[:2]
    if len(stamp) < 2:
        return None
    try:
        return datetime.strptime(f"{stamp[0]} {stamp[1]}", "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        return None


class GuardianWatcher(threading.Thread):
    """Watch for guardian processes for the whole duration of a shutdown.

    A frozen build can take seconds to leave ``main`` after the handover while a
    blocked worker unwinds, and the guardian may finish inside that window.  A
    watcher that starts *before* the quit request therefore sees a guardian that
    a single check after the exit would miss.
    """

    def __init__(self):
        super().__init__(name="GuardianWatcher", daemon=True)
        self.seen: set[int] = set()
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.seen.update(guardian_pids())
            self._stop_event.wait(0.5)

    def stop(self) -> set[int]:
        self._stop_event.set()
        self.join(timeout=15.0)
        return set(self.seen)


def pid_alive(pid: int) -> bool:
    """True only while the process is still running.

    OpenProcess alone is not enough: a terminated process stays openable until
    the last handle to it is closed, so the exit code has to be inspected.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000 | 0x0400, False, int(pid))  # QUERY_LIMITED|SYNCHRONIZE
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def assert_helpers_gone(helpers: list[int], timeout: float = 15.0) -> None:
    """A clean exit must not leave capture helper processes behind."""
    survivors = [pid for pid in helpers if pid_alive(pid)]
    if not survivors:
        return
    deadline = time.monotonic() + timeout
    while survivors and time.monotonic() < deadline:
        time.sleep(0.25)
        survivors = [pid for pid in survivors if pid_alive(pid)]
    if survivors:
        raise SmokeError(f"capture helper processes survived the application: {survivors}")


def write_settings(path: Path, *, change_detection: bool = True) -> None:
    from config import AppConfig, TrackedWindow

    config = AppConfig(
        hotkeys_enabled=False,
        first_run_selector=False,
        change_detection=change_detection,
        notify_sound=False,
        notify_window_return=False,
        restore_minimized=False,
        windows=[
            TrackedWindow(
                process=Path(sys.executable).name,
                title_contains=APP_TITLE,
                detect_changes=change_detection,
            )
        ],
    )
    path.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_park(
    card_hwnd: int,
    source_hwnd: int,
    timeout: float = 30.0,
    journal: Path | None = None,
) -> None:
    """Click the card until the source is parked.

    A card that has not bound its source yet ignores the click, so the click is
    repeated while the source is still visibly on screen.  The source is never
    clicked twice while it is parked, which would toggle it back.

    When ``journal`` is given, a click is only repeated while *no* recovery
    record for the source exists: the record is written before the move, so it
    is proof that a park is already in flight.  A target that stalls for a long
    time makes that distinction essential, because clicking every second would
    otherwise toggle the park off again before it ever lands.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    while time.monotonic() < deadline:
        if winapi.looks_like_lookup_parked(source_hwnd):
            log(f"source parked after {attempts} click(s)")
            return
        if journal is None or not journal_has_hwnd(journal, source_hwnd):
            click_card(card_hwnd)
            attempts += 1
        time.sleep(1.0)
    raise SmokeError(f"source was not parked within {timeout:.0f}s ({attempts} clicks)")


def recover_on_restart(
    args: argparse.Namespace,
    exe: Path | None,
    settings: Path,
    target: "Target",
    hang: float,
) -> None:
    """A fresh start must restore the stranded window and drain the journal.

    The surviving guardian may already be executing the durable obligation.
    The new application must coordinate with it and preserve any unfinished work.
    """
    second = AppRun(args.mode, exe, settings).start()
    try:
        second.wait_panel(args.timeout)
        assert_restored(target.hwnd, max(args.timeout, hang + 20.0))
        journal = second.journal
        wait_for(
            lambda: not journal_records(journal),
            30.0,
            what="recovery journal to drain",
        )
        second.request_quit()
        if second.wait_exit(args.timeout) != 0:
            raise SmokeError("restarted instance did not quit cleanly")
    finally:
        if second.alive():
            second.kill()


def require_guardian_handover(
    journal: Path,
    hwnd: int,
    *,
    quit_started: float,
    watcher: GuardianWatcher | None = None,
    timeout: float,
    expect_guardian: bool = True,
) -> None:
    """The source must come back *without a restart*, and the journal must empty.

    This is the invariant the previous failure mode broke: LookUp used to exit
    with a foreign window still parked and no live component willing to execute
    the restore.  The proof is threefold:

    * the shutting-down process recorded that it handed the obligation over;
    * a guardian process outside that process actually ran;
    * the window came back and the journal emptied without any restart.
    """
    log_file = app_log_path()
    handed = wait_for(
        lambda: [line for line in log_lines_since(log_file, quit_started - 1.0)
                 if "recovery guardian" in line],
        min(30.0, timeout),
        interval=0.5,
        what="the application to hand its unfinished restore over to the guardian",
    )
    log(f"handover confirmed: {handed[-1]}")
    seen = set(watcher.stop()) if watcher is not None else set()
    if expect_guardian:
        deadline = time.monotonic() + min(20.0, timeout)
        while not seen and time.monotonic() < deadline:
            seen.update(guardian_pids())
            time.sleep(0.5)
        if not seen:
            raise SmokeError(
                "no recovery guardian process was observed while an outstanding park "
                "obligation had to be executed"
            )
        log(f"recovery guardian pids: {sorted(seen)}")
    assert_restored(hwnd, timeout)
    wait_for(
        lambda: not journal_records(journal),
        timeout,
        what="the guardian to drain the recovery journal",
    )
    wait_for(
        lambda: not guardian_pids(),
        30.0,
        interval=0.5,
        what="the recovery guardian to exit",
    )


def require_no_guardian(timeout: float) -> None:
    """The fast path must not leave a guardian behind for nothing."""
    deadline = time.monotonic() + max(2.0, timeout)
    while time.monotonic() < deadline:
        pids = guardian_pids()
        if not pids:
            return
        time.sleep(0.5)
    raise SmokeError(f"a recovery guardian is still running although nothing was pending: {pids}")


def journal_has_hwnd(path: Path, hwnd: int) -> bool:
    return any(int(item.get("hwnd") or 0) == int(hwnd) for item in journal_records(path))


def assert_journal_retains_obligations(journal: Path, hwnds) -> None:
    """No still-parked window may lose its durable record.

    This is the invariant a concurrent writer can break: the application and a
    recovery guardian share one journal, so an obligation may only be absent
    when the window is verifiably back on a monitor (or provably gone).
    """
    for hwnd in hwnds:
        hwnd = int(hwnd)
        if journal_has_hwnd(journal, hwnd):
            continue
        if not winapi.is_window(hwnd) or winapi.is_effectively_onscreen(hwnd):
            continue
        raise SmokeError(
            f"window {hwnd} is still parked but the recovery journal lost its record"
        )


def require_guardian_working(
    journal: Path,
    hwnd: int,
    *,
    quit_started: float,
    watcher: GuardianWatcher | None = None,
    timeout: float,
) -> None:
    """Prove a guardian took the obligation and is still executing it.

    Unlike :func:`require_guardian_handover` this deliberately does *not* wait
    for the journal to drain: the point is that a second writer appears while
    the guardian is still working.
    """
    log_file = app_log_path()
    handed = wait_for(
        lambda: [line for line in log_lines_since(log_file, quit_started - 1.0)
                 if "recovery guardian" in line],
        min(30.0, timeout),
        interval=0.5,
        what="the application to hand its unfinished restore over to the guardian",
    )
    log(f"handover confirmed: {handed[-1]}")
    seen = set(watcher.stop()) if watcher is not None else set()
    deadline = time.monotonic() + min(20.0, timeout)
    while not seen and time.monotonic() < deadline:
        seen.update(guardian_pids())
        time.sleep(0.5)
    if not seen:
        raise SmokeError(
            "no recovery guardian process was observed while an outstanding park "
            "obligation had to be executed"
        )
    log(f"recovery guardian working: {sorted(seen)}")
    # The obligation is still durably outstanding: a record only disappears after
    # a *verified* restore, so what is checked here is that the handover really
    # left work behind and that nothing was lost on the way.
    assert_journal_retains_obligations(journal, [hwnd])


def age_journal_records(journal: Path, seconds: float) -> int:
    """Backdate every record, simulating an obligation that has been pending for weeks."""
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeError(f"recovery journal could not be read for ageing: {exc}") from exc
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise SmokeError("recovery journal holds no records to age")
    stamp = time.time() - seconds
    cleared_claim = {
        "claimPid": 0,
        "claimUntil": 0.0,
        "claimExecutor": "",
        "claimCreated": None,
        "claimToken": "",
        "claimGeneration": 0,
    }
    for record in records:
        if isinstance(record, dict):
            record["recordedAt"] = stamp
            # Age the whole claim, not just its expiry: the fencing fields have to
            # disappear with it, or a stalled executor would still be able to
            # present the identity it was granted before.
            record.update(cleared_claim)
    journal.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(records)


def broken_journal_documents() -> list[str]:
    """Damage the journal the way a partial write or a wrong-version file would."""
    return [
        json.dumps({"version": "1", "records": []}),
        json.dumps({"version": 1, "records": 123}),
        json.dumps([1, 2, 3]),
        "{not json",
    ]


def rewrite_records(journal: Path, mutate) -> dict:
    """Apply ``mutate`` to the newest document and write it back atomically."""
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeError(f"recovery journal could not be read: {exc}") from exc
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise SmokeError("recovery journal holds no records to rewrite")
    for record in records:
        mutate(record)
    journal.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def journal_record(journal: Path, hwnd: int) -> dict:
    for record in journal_records(journal):
        if int(record.get("hwnd") or 0) == int(hwnd):
            return record
    raise SmokeError(f"recovery journal holds no record for hwnd={hwnd}")


def journal_claim(journal: Path, hwnd: int) -> dict:
    """The *live* claim stored for ``hwnd``, or ``{}`` when there is none.

    An expired lease is not a claim: the whole point of the ABA gate is that the
    stale holder is no longer the owner, so waiting for "some claim fields" would
    be satisfied by the very lease that is being superseded.
    """
    record = journal_record(journal, hwnd)
    if not record.get("claimExecutor") or not record.get("claimToken"):
        return {}
    try:
        until = float(record.get("claimUntil") or 0.0)
    except (TypeError, ValueError):
        return {}
    return record if until > time.time() else {}


def plant_owner_identity(journal: Path, hwnd: int, *, owner_pid: int, owner_created: int) -> None:
    """Rewrite the owner identity of one record."""

    def mutate(record: dict) -> None:
        if int(record.get("hwnd") or 0) != int(hwnd):
            return
        record["ownerPid"] = int(owner_pid)
        record["ownerCreated"] = int(owner_created)
        record["ownerRunId"] = "planted-run-id"

    rewrite_records(journal, mutate)


def plant_claim(journal: Path, hwnd: int, *, claim_until: str) -> None:
    """Plant a lease whose expiry is written as a bare JSON non-finite literal.

    The value has to reach the file as ``Infinity``/``NaN`` text, which
    ``json.dumps`` will not produce for a Python float, so the document is edited
    as text - which is exactly how a hand-edited or corrupted file looks.
    """
    text = journal.read_text(encoding="utf-8")
    document = json.loads(text)
    record = next(
        item for item in document["records"] if int(item.get("hwnd") or 0) == int(hwnd)
    )
    record["claimPid"] = 999999
    record["claimExecutor"] = "planted-executor"
    record["claimGeneration"] = 1
    record["claimToken"] = "planted-token"
    # Replace the whole document with one that carries the literal.
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    marker = '"claimUntil": '
    at = payload.index(marker) + len(marker)
    end = payload.index(",", at)
    journal.write_text(payload[:at] + claim_until + payload[end:], encoding="utf-8")
    if claim_until not in journal.read_text(encoding="utf-8"):
        raise SmokeError(f"could not plant claimUntil={claim_until}")


def plant_stale_claim(journal: Path, hwnd: int):
    """Give one record an expired claim and return it as a stale ``Claim``.

    This is the state a stalled executor still believes in: it holds generation N
    and a token, but the lease has expired and any other executor may take the
    record over and move to generation N+1.
    """
    from recovery import Claim, ExecutorIdentity

    identity = ExecutorIdentity(executor_id="stale-executor", pid=999999, created=1, label="stale")
    token = "stale-token"
    generation = 3

    def mutate(record: dict) -> None:
        if int(record.get("hwnd") or 0) != int(hwnd):
            return
        record["claimPid"] = 999999
        record["claimExecutor"] = identity.executor_id
        record["claimToken"] = token
        record["claimGeneration"] = generation
        record["claimUntil"] = time.time() - 60.0

    rewrite_records(journal, mutate)
    return Claim(hwnd=int(hwnd), executor=identity, generation=generation, token=token)


def trigger_park(run: "AppRun", card_hwnd: int, source_hwnd: int) -> bool:
    """Click the card and report whether a park intent has been recorded.

    The recovery record is written before the foreign window is moved, so it is
    the observable proof that a park is in flight; the click itself is repeated
    while no intent exists (a card that has not bound its source yet ignores it).
    """
    click_card(card_hwnd)
    return bool(journal_records(run.journal))


def assert_parked(hwnd: int, timeout: float) -> None:
    wait_for(lambda: winapi.looks_like_lookup_parked(hwnd), timeout, what="source to be parked")


def assert_restored(hwnd: int, timeout: float) -> None:
    wait_for(
        lambda: winapi.is_effectively_onscreen(hwnd),
        timeout,
        what="source window to be visible on a monitor",
    )
    if winapi.looks_like_lookup_parked(hwnd):
        raise SmokeError("source window is still parked after recovery")


def journal_records(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("records")
    return records if isinstance(records, list) else []


def stop_stray_instances() -> None:
    """No other LookUp instance may be running.

    The application is single-instance and its panel window is found by class
    name, so a leftover process from an earlier run would be measured instead
    of the artifact under test.
    """
    if not find_window("WPCtrl"):
        return
    log("a LookUp Windows instance is already running; stopping it first")
    import winui

    if not winui.request_graceful_quit(timeout_ms=3000):
        raise SmokeError(
            "another LookUp Windows instance is running and did not accept a shutdown "
            "request; stop it manually before running the smoke"
        )
    deadline = time.monotonic() + 30.0
    while find_window("WPCtrl") and time.monotonic() < deadline:
        time.sleep(0.25)
    if find_window("WPCtrl"):
        raise SmokeError("a leftover LookUp Windows instance is still running")


def stop_stray_guardians() -> None:
    """Do not interrupt an executor that may be restoring a user's window."""
    pids = guardian_pids()
    if pids:
        raise SmokeError(f"recovery guardian(s) still working: {pids}; wait before testing")


def scenario_responsive(args, exe, settings, target, first, card) -> None:
    """Everything must be finished by the shutting-down process itself."""
    helpers = first.wait_helpers(args.helper_timeout)
    log(f"capture helpers running: {helpers}")
    ensure_park(card, target.hwnd, args.timeout)
    first.request_quit()
    code = first.wait_exit(args.timeout)
    if code != 0:
        raise SmokeError(f"clean quit returned exit code {code}")
    assert_restored(target.hwnd, args.timeout)
    if journal_records(first.journal):
        raise SmokeError("recovery journal still lists a restored window")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


def scenario_slow(args, exe, settings, target, first, card) -> None:
    """Quit while the target is still stalled: no restart may be required."""
    ensure_park(card, target.hwnd, args.timeout)
    watcher = GuardianWatcher()
    watcher.start()
    quit_started = time.time()
    first.request_quit()
    code = first.wait_exit(args.timeout)
    if code != 0:
        raise SmokeError(f"clean quit returned exit code {code}")
    log("quit completed while the source was still unresponsive")
    require_guardian_handover(
        first.journal,
        target.hwnd,
        quit_started=quit_started,
        watcher=watcher,
        timeout=args.guardian_timeout,
    )


def scenario_inflight(args, exe, settings, target, first, card) -> None:
    """Quit while the park is still moving the window: a pending intent must not strand it."""
    wait_for(
        lambda: trigger_park(first, card, target.hwnd),
        args.timeout,
        interval=1.0,
        what="park intent (recovery record) for the in-flight park",
    )
    records = journal_records(first.journal)
    log(f"park intent recorded before quitting: {len(records)} record(s)")
    if not records:
        raise SmokeError("in-flight park left no recovery record")
    watcher = GuardianWatcher()
    watcher.start()
    quit_started = time.time()
    first.request_quit()
    code = first.wait_exit(args.timeout)
    if code != 0:
        raise SmokeError("clean quit during an in-flight park failed")
    log("quit completed while the park was in flight")
    # The delayed move may still land after this process is gone, which is
    # exactly why the guardian has to keep watching instead of assuming the
    # park never happened.
    require_guardian_handover(
        first.journal,
        target.hwnd,
        quit_started=quit_started,
        watcher=watcher,
        timeout=args.guardian_timeout,
    )


def scenario_hardkill(args, exe, settings, target, first, card) -> None:
    """A hard kill must leave an executor, not only a record.

    The invariant this gates is "a live park obligation has a live executor": the
    record in the journal is durable, but a record nobody executes only postpones
    the loss until the next launch.  So the window has to come back *without* a
    new application process, executed by the guardian that was started before the
    park was allowed, while the capture helpers - which must never outlive the
    owner - are gone.
    """
    helpers = first.wait_helpers(args.helper_timeout)
    ensure_park(card, target.hwnd, args.timeout)
    if not journal_has_hwnd(first.journal, target.hwnd):
        raise SmokeError("park was not durably recorded before the hard kill")
    if not guardian_pids():
        raise SmokeError("park was permitted without a live external guardian")
    first.kill()
    survivors = [pid for pid in helpers if pid_alive(pid)]
    if survivors:
        raise SmokeError(f"capture helpers survived a hard owner kill: {survivors}")
    log("no capture helper survived the hard kill")
    # No new UI process: the guardian that owns this journal has to do it.
    assert_restored(target.hwnd, args.guardian_timeout)
    wait_for(
        lambda: not journal_records(first.journal),
        30.0,
        what="the guardian to drain the journal after a hard kill",
    )
    log("the survivor guardian restored the window without a restart")
    # A fresh start still has to work, and must not have anything left to do.
    second = AppRun(args.mode, exe, settings).start()
    try:
        second.wait_panel(args.timeout)
        if journal_records(second.journal):
            raise SmokeError("the restarted instance still found an outstanding park")
        second.request_quit()
        if second.wait_exit(args.timeout) != 0:
            raise SmokeError("restarted instance did not quit cleanly")
    finally:
        if second.alive():
            second.kill()


def scenario_aged(args, exe, settings, target, first, card) -> None:
    """An obligation that has been pending for days must still be executed."""
    ensure_park(card, target.hwnd, args.timeout)
    aged = age_journal_records(first.journal, 8 * 24 * 3600)
    if not aged:
        raise SmokeError("no live recovery record could be aged")
    log(f"aged {aged} recovery record(s) by eight days")
    first.kill()
    recover_on_restart(args, exe, settings, target, args.hang)


def scenario_badjournal(args, exe, settings, target, _first=None, _card=None) -> None:
    """A damaged journal may never keep LookUp from starting or from parking.

    Every malformed shape is planted *before* a start, because the failure this
    gates is a startup loop: the document is only read during startup recovery.

    The second half of the gate is the one that matters for a lost window: a real
    window that LookUp parked off-screen, whose only recovery record is then
    destroyed.  Quarantine keeps the file, but it cannot execute anything, so the
    next start has to find that window by its parking signature and bring it back
    on its own - with an empty set of cards, because nothing in the configuration
    refers to it any more.
    """
    documents = broken_journal_documents()
    for index, document in enumerate(documents):
        first = AppRun(args.mode, exe, settings)
        first.journal.write_text(document, encoding="utf-8")
        log(f"starting with a damaged recovery journal #{index + 1}: {document[:48]}")
        first.start()
        try:
            first.wait_panel(args.timeout)
            first.wait_card(args.timeout)
            if not list(first.journal.parent.glob(first.journal.name + ".invalid*")):
                raise SmokeError("the damaged recovery journal was not quarantined for diagnostics")
            if index == 0:
                # The rest of the park/restore cycle must still work normally.
                helpers = first.wait_helpers(args.helper_timeout)
                ensure_park(first.wait_card(args.timeout), target.hwnd, args.timeout)
                if not journal_records(first.journal):
                    raise SmokeError("a parked window was not recorded after a damaged journal")
                first.request_quit()
                if first.wait_exit(args.timeout) != 0:
                    raise SmokeError("clean quit after a damaged journal did not return 0")
                assert_restored(target.hwnd, args.timeout)
                if journal_records(first.journal):
                    raise SmokeError("recovery journal still lists a restored window")
                assert_helpers_gone(helpers)
            else:
                first.request_quit()
                if first.wait_exit(args.timeout) != 0:
                    raise SmokeError("clean quit after a damaged journal did not return 0")
        finally:
            if first.alive():
                first.kill()
    _gate_orphan_recovery(args, exe, settings)


def _gate_orphan_recovery(args, exe, settings) -> None:
    """A window whose only recovery record was destroyed still comes back.

    The window is parked exactly the way LookUp parks it, then the only durable
    proof of the obligation is destroyed.  Nothing in the configuration refers to
    that window at all, so no card and no shutdown barrier can bring it back: the
    only thing that can find it is LookUp's own parking signature.
    """
    with Target(title="LUW untracked damaged-journal victim") as victim:
        journal = settings.with_suffix(settings.suffix + ".park.json")
        if spawn_journal_writer(journal, victim.hwnd) != "PARKED":
            raise SmokeError("could not park the victim the way LookUp parks")
        if not journal_has_hwnd(journal, victim.hwnd):
            raise SmokeError("the victim was not recorded as parked")
        if not winapi.looks_like_lookup_parked(victim.hwnd):
            raise SmokeError("the victim is not in the LookUp parking position")
        log(f"destroying the only recovery record of hwnd={victim.hwnd}")
        journal.write_text("{ this is not json", encoding="utf-8")

        restarted = AppRun(args.mode, exe, settings)
        restarted.start()
        try:
            restarted.wait_panel(args.timeout)
            restarted.wait_card(args.timeout)
            assert_restored(victim.hwnd, args.guardian_timeout)
            if journal_records(restarted.journal):
                raise SmokeError("the damaged journal still advertises an obligation")
            if not list(restarted.journal.parent.glob(restarted.journal.name + ".invalid*")):
                raise SmokeError("the damaged journal was not quarantined for diagnostics")
            restarted.request_quit()
            if restarted.wait_exit(args.timeout) != 0:
                raise SmokeError("clean quit after orphan recovery did not return 0")
        finally:
            if restarted.alive():
                restarted.kill()
    log("a window only the destroyed journal knew about was recovered from the parking position")


def scenario_jobfail(args, exe, settings, target, first, card) -> None:
    """A helper that cannot be bound must never be started, let alone used."""
    ensure_park(card, target.hwnd, args.timeout)
    deadline = time.monotonic() + 8.0
    observed: list[int] = []
    while time.monotonic() < deadline:
        observed.extend(first.helper_pids())
        if observed:
            break
        time.sleep(0.5)
    if observed:
        raise SmokeError(
            "capture helper processes were started even though the kill-on-close job "
            f"refused them: {observed}"
        )
    log("no capture helper was started while the job binding was failing")
    first.request_quit()
    code = first.wait_exit(args.timeout)
    if code != 0:
        raise SmokeError(f"clean quit with a degraded detector returned exit code {code}")
    assert_restored(target.hwnd, args.timeout)
    if journal_records(first.journal):
        raise SmokeError("recovery journal still lists a restored window")


def spawn_journal_writer(
    journal: Path, hwnd: int, timeout: float = 120.0, *, hold_owner: bool = False
) -> str | subprocess.Popen:
    """Park ``hwnd`` from a separate process, exactly the way the application does.

    The application is deliberately *not* the second writer here: a frozen LookUp
    needs seconds just to reach its UI, which is longer than a guardian needs to
    discharge one obligation, so waiting for a second application would measure
    boot time instead of the journal.  This process performs the two operations
    that matter - record the intent, then move the window - against the same
    document the application and the guardian are writing.
    """
    script = "\n".join(
        (
            "import sys",
            f"sys.path.insert(0, {str(ROOT / 'src')!r})",
            "from pathlib import Path",
            "import winapi",
            "from recovery import RecoveryJournal, record_from_state",
            f"journal = RecoveryJournal(Path({str(journal)!r}))",
            f"hwnd = {int(hwnd)}",
            "state = winapi.park_window_offscreen_sync(",
            "    hwnd,",
            "    before_park=lambda parked: journal.record_intent(record_from_state(hwnd, parked)),",
            ")",
            "if state is not None:",
            "    journal.mark_parked(hwnd)",
            "print('PARKED' if state is not None else 'ABANDONED', flush=True)",
            "sys.stdin.readline()" if hold_owner else "pass",
        )
    )
    if hold_owner:
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if process.stdout.readline().strip() != "PARKED":
            process.kill()
            process.wait(timeout=10)
            raise SmokeError("the live second writer could not park its source")
        return process
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    outcome = (completed.stdout or "").strip()
    return outcome or (completed.stderr or "").strip() or f"exit code {completed.returncode}"


def scenario_journalrace(args, exe, settings, target, first, card) -> None:
    """Concurrent writers must not erase each of their outstanding obligations.

    The recovery journal is shared state, and in production it has more than one
    writer even though LookUp itself runs as a single instance: the guardian that
    outlives a shutting-down process keeps executing obligations while something
    else records new ones in the same document.

    This scenario builds exactly that.  Two *processes* write the same journal -
    the application parks its own source, and a second writer process parks
    another window - and the application then quits inside the restore deadline,
    so a real guardian inherits both obligations.  Nothing may be lost, both
    windows have to come back, and no guardian may be left behind.
    """
    first.wait_helpers(args.helper_timeout)
    with ExitStack() as stack:
        # The second writer's window exists up front: its park happens while the
        # application is still running, so both documents are written by
        # different processes at the same time.
        race_target = stack.enter_context(Target(title=f"{APP_TITLE} race"))
        writer = spawn_journal_writer(first.journal, race_target.hwnd, hold_owner=True)
        def stop_writer():
            if writer.poll() is None:
                writer.kill()
            writer.wait(timeout=10)
        stack.callback(stop_writer)
        log(f"second writer parked hwnd={race_target.hwnd} while the application was running")

        # Now the application parks its own (unresponsive) source into the same
        # journal.  Both records have to survive: a writer that only merges its
        # own change would drop the other process's obligation.
        ensure_park(card, target.hwnd, args.timeout, journal=first.journal)
        outstanding = {int(item.get("hwnd") or 0) for item in journal_records(first.journal)}
        missing = {target.hwnd, race_target.hwnd} - outstanding
        if missing:
            raise SmokeError(
                f"an obligation written by another process was erased: {sorted(missing)}"
            )
        log(f"both writers are in the journal: {sorted(outstanding)}")

        # Quit inside the restore deadline: a real guardian has to execute what
        # both processes left behind.
        watcher = GuardianWatcher()
        watcher.start()
        quit_started = time.time()
        writer.communicate("done\n", timeout=10)
        first.request_quit()
        code = first.wait_exit(args.timeout)
        if code != 0:
            raise SmokeError(f"clean quit during the journal race returned exit code {code}")
        require_guardian_working(
            first.journal,
            target.hwnd,
            quit_started=quit_started,
            watcher=watcher,
            timeout=args.guardian_timeout,
        )
        # The invariant under test: a record may only be gone once its window is
        # verifiably back.  Whatever each writer finished on its own, nothing that
        # is still parked may lose its obligation.
        assert_journal_retains_obligations(first.journal, [target.hwnd, race_target.hwnd])

        # A fresh start has to finish whatever the guardian left behind: it
        # inherits every obligation nobody owns any more.
        second_run = AppRun(args.mode, exe, settings).start()
        try:
            second_run.wait_panel(args.timeout)
            second_run.request_quit()
            if second_run.wait_exit(args.timeout) != 0:
                raise SmokeError("the second application did not quit cleanly")
        finally:
            if second_run.alive():
                second_run.kill()

        # This has to happen while the foreign windows still exist: a destroyed
        # window is not a restored one.
        assert_restored(race_target.hwnd, args.guardian_timeout)
        assert_restored(target.hwnd, args.guardian_timeout)
        wait_for(
            lambda: not journal_records(first.journal),
            args.guardian_timeout,
            what="the journal to drain after the overlapping writers finished",
        )
    require_no_guardian(30.0)


def scenario_journalio(args, exe, settings, target, first, card) -> None:
    """A failed journal read cannot authorize a park or erase an older obligation."""
    helpers = first.wait_helpers(args.helper_timeout)
    with ExitStack() as stack:
        victim = stack.enter_context(Target(title="LUW untracked journal I/O victim"))
        writer = spawn_journal_writer(first.journal, victim.hwnd, hold_owner=True)
        def stop_writer():
            if writer.poll() is None:
                writer.kill()
            writer.wait(timeout=10)
        stack.callback(stop_writer)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        # Deny other readers while permitting writes/delete. The application
        # must refuse the park because it cannot load the newest journal.
        handle = kernel32.CreateFileW(str(first.journal), 0x80000000, 6, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise SmokeError("could not inject the native journal sharing violation")
        started = time.time()
        try:
            def refused():
                lines = log_lines_since(app_log_path(), started - 1)
                if any(f"Park of hwnd={target.hwnd} aborted" in line
                       or f"Park of hwnd={target.hwnd} refused" in line for line in lines):
                    return True
                click_card(card)
                return False
            wait_for(refused, args.timeout, interval=1, what="fail-closed park after unreadable journal")
            if not winapi.is_effectively_onscreen(target.hwnd):
                raise SmokeError("new source moved despite an unreadable recovery journal")
            if not winapi.looks_like_lookup_parked(victim.hwnd):
                raise SmokeError("the old obligation's window was unexpectedly moved")
        finally:
            kernel32.CloseHandle(handle)
        records = journal_records(first.journal)
        if {int(record["hwnd"]) for record in records} != {victim.hwnd}:
            raise SmokeError("failed journal read lost or invented an obligation")
        ensure_park(card, target.hwnd, args.timeout)
        outstanding = {int(record["hwnd"]) for record in journal_records(first.journal)}
        if outstanding != {victim.hwnd, target.hwnd}:
            raise SmokeError("retry did not retain both journal obligations")
        writer.communicate("done\n", timeout=10)
        first.request_quit()
        if first.wait_exit(args.timeout) != 0:
            raise SmokeError("quit after journal I/O recovery failed")
        assert_restored(victim.hwnd, args.guardian_timeout)
        assert_restored(target.hwnd, args.guardian_timeout)
        wait_for(lambda: not journal_records(first.journal), args.guardian_timeout, what="I/O journal drain")
        assert_helpers_gone(helpers)
        require_no_guardian(30)


def _guardian_startup_retry(args, exe, journal, hwnd) -> None:
    """Exercise a live silent guardian, launcher cleanup and a subsequent retry."""
    if spawn_journal_writer(journal, hwnd) != "PARKED":
        raise SmokeError("could not establish guardian recovery obligation")
    old_frozen = getattr(sys, "frozen", None)
    old_executable = sys.executable
    old_hooks = os.environ.get("LOOKUPWINDOWS_TEST_HOOKS")
    guardian = None
    reached_guardian = threading.Event()
    stop_watching = threading.Event()
    # The executor mutex is named after the journal, so the probe has to ask for
    # *this* document's name: a session-wide name could be taken by another
    # configuration's guardian and would prove nothing about this one.
    mutex = restoreguard.guardian_mutex_name(journal)

    def watch_startup():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        while not stop_watching.wait(.02):
            # Open only: a CreateMutex probe would itself prevent the guardian
            # from acquiring its singleton. This proves the frozen Python child
            # reached run_guardian, rather than timing out during extraction.
            handle = kernel32.OpenMutexW(0x00100000, False, mutex)
            if handle:
                kernel32.CloseHandle(handle)
                reached_guardian.set()

    watcher = threading.Thread(target=watch_startup, name="Smoke-GuardianStartup", daemon=True)
    try:
        if args.mode == "exe":
            sys.frozen = True
            sys.executable = str(exe)
        os.environ["LOOKUPWINDOWS_TEST_HOOKS"] = "guardian_ready_stall"
        watcher.start()
        started = time.monotonic()
        failed = restoreguard.spawn_guardian(journal, timeout=8)
        elapsed = time.monotonic() - started
        stop_watching.set()
        watcher.join(timeout=1)
        if failed is not None or elapsed > 19:
            raise SmokeError(f"guardian startup timeout was not bounded: {elapsed:.2f}s")
        if not reached_guardian.is_set():
            raise SmokeError("guardian failure test did not reach the live frozen Python child")
        require_no_guardian(args.guardian_exit_timeout)
        if not winapi.looks_like_lookup_parked(hwnd) or not journal_has_hwnd(journal, hwnd):
            raise SmokeError("failed guardian startup discharged the recovery obligation")
        log(f"silent guardian startup rejected and tree cleaned in {elapsed:.2f}s")
        os.environ.pop("LOOKUPWINDOWS_TEST_HOOKS", None)
        guardian = restoreguard.spawn_guardian(journal, timeout=20)
        if guardian is None:
            raise SmokeError("healthy guardian did not start after failed startup cleanup")
    finally:
        stop_watching.set()
        if watcher.ident is not None:
            watcher.join(timeout=1)
        sys.executable = old_executable
        if old_frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = old_frozen
        if old_hooks is None:
            os.environ.pop("LOOKUPWINDOWS_TEST_HOOKS", None)
        else:
            os.environ["LOOKUPWINDOWS_TEST_HOOKS"] = old_hooks
    assert_restored(hwnd, args.guardian_timeout)
    wait_for(lambda: not journal_records(journal), args.guardian_timeout, what="guardian retry journal drain")
    if guardian.wait(timeout=30) != 0:
        raise SmokeError("retry guardian did not exit cleanly")
    require_no_guardian(args.guardian_exit_timeout)


def scenario_guardianfail(args, exe, settings, target, first, card) -> None:
    helpers = first.wait_helpers(args.helper_timeout)
    # Use an untracked window: the running app's orphan watcher must not
    # independently restore the victim while its guardian is being tested.
    with Target(title="LUW untracked guardian startup victim") as victim:
        _guardian_startup_retry(args, exe, first.journal, victim.hwnd)
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after guardian startup retry")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


# --------------------------------------------------------------------------- #
# Recovery invariants that used to lose a window
# --------------------------------------------------------------------------- #

def spawn_frozen_guardian(
    journal: Path,
    *,
    exe: Path | None = None,
    hooks: str = "",
    timeout: float = 60.0,
):
    """Start the guardian exactly the way the application would start it.

    With ``exe`` the guardian is the *frozen artifact itself* (the smoke pretends
    to be a frozen process for the duration of the call), which is the only way to
    prove that the published EXE can still spawn and own the recovery executor.
    ``hooks`` are the documented test hooks the executor understands; they are set
    only for this call, so the application under test is never affected.
    """
    old_hooks = os.environ.get("LOOKUPWINDOWS_TEST_HOOKS")
    old_frozen = getattr(sys, "frozen", None)
    old_executable = sys.executable
    if hooks:
        os.environ["LOOKUPWINDOWS_TEST_HOOKS"] = hooks
    if exe is not None:
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = str(exe)
    try:
        return restoreguard.spawn_guardian(journal, timeout=timeout)
    finally:
        if exe is not None:
            sys.executable = old_executable
            if old_frozen is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = old_frozen
        if old_hooks is None:
            os.environ.pop("LOOKUPWINDOWS_TEST_HOOKS", None)
        else:
            os.environ["LOOKUPWINDOWS_TEST_HOOKS"] = old_hooks


def freeze_target(tid: int) -> int:
    """Suspend the target's window thread and return a resume count.

    A suspended thread never pumps messages, so every cross-process window call to
    this target blocks indefinitely - which is the one failure a real restore can
    never get past, and therefore the only honest way to keep a parked obligation
    outstanding for longer than any hard lifetime.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.SuspendThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenThread(0x0002, False, int(tid))  # THREAD_SUSPEND_RESUME
    if not handle:
        raise SmokeError(f"could not open the target thread {tid}: {ctypes.get_last_error()}")
    previous = kernel32.SuspendThread(handle)
    kernel32.CloseHandle(handle)
    if previous == 0xFFFFFFFF:
        raise SmokeError(f"could not suspend the target thread {tid}")
    return int(previous)


def thaw_target(tid: int, count: int) -> None:
    """Resume a target suspended by :func:`freeze_target`."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenThread(0x0002, False, int(tid))
    if not handle:
        raise SmokeError(f"could not reopen the target thread {tid}")
    try:
        for _ in range(max(1, int(count))):
            kernel32.ResumeThread(handle)
    finally:
        kernel32.CloseHandle(handle)


def scenario_guardianlimit(args, exe, settings, target, first, card) -> None:
    """A live obligation must never reach a lifetime limit.

    The target is frozen outright (its window thread is suspended), so no restore
    attempt can complete and no window operation against it returns.  That is the
    scenario the old executor lost: after its hard lifetime it logged an error and
    exited, and the window stayed off-screen until the user started LookUp again.

    What is proved here: the executor keeps the obligation owned for far longer
    than any lifetime used to be (it keeps renewing the lease), it never lets the
    record disappear without a verified restore, and it exits only once the window
    is verifiably back on a monitor.
    """
    helpers = first.wait_helpers(args.helper_timeout)
    hold_sec = max(20.0, args.hang * 3)
    with Target(title="LUW untracked guardian limit victim") as victim:
        if spawn_journal_writer(first.journal, victim.hwnd) != "PARKED":
            raise SmokeError("could not establish the obligation under test")
        assert_parked(victim.hwnd, args.timeout)
        if not victim.tid:
            raise SmokeError("the smoke target did not report its window thread")
        suspend_count = freeze_target(victim.tid)
        log(f"froze the target window thread {victim.tid} (suspend count {suspend_count})")
        watcher = GuardianWatcher()
        watcher.start()
        guardian = spawn_frozen_guardian(
            first.journal, exe=exe, hooks="guardian_fast_backoff", timeout=args.guardian_timeout
        )
        if guardian is None:
            thaw_target(victim.tid, suspend_count)
            raise SmokeError("the recovery guardian refused to start")
        log(f"guardian pid={guardian.pid} owns a parked window inside a frozen process")
        try:
            wait_for(
                lambda: bool(journal_claim(first.journal, victim.hwnd)),
                args.guardian_timeout,
                interval=0.2,
                what="the guardian to claim the obligation",
            )
            claim_before = float(journal_claim(first.journal, victim.hwnd)["claimUntil"])
            watched = 0.0
            while watched < hold_sec:
                time.sleep(0.5)
                watched += 0.5
                if guardian.poll() is not None:
                    raise SmokeError(
                        f"the recovery guardian abandoned a live obligation after {watched:.1f}s"
                    )
                if not journal_has_hwnd(first.journal, victim.hwnd):
                    raise SmokeError(
                        "the obligation was discharged while the target could not answer a "
                        f"single call: hwnd={victim.hwnd}"
                    )
                if not winapi.looks_like_lookup_parked(victim.hwnd):
                    raise SmokeError(
                        "the parked window moved while its process was frozen: "
                        f"rect={winapi.get_window_rect(victim.hwnd)}"
                    )
            claim_after = float(journal_claim(first.journal, victim.hwnd).get("claimUntil") or 0.0)
            if claim_after <= claim_before:
                raise SmokeError(
                    "the executor stopped renewing its claim on a live obligation "
                    f"({claim_before:.0f} -> {claim_after:.0f})"
                )
            log(
                f"guardian kept and renewed the obligation for {watched:.1f}s of a frozen "
                f"process (claim {claim_before:.0f} -> {claim_after:.0f})"
            )
        finally:
            thaw_target(victim.tid, suspend_count)
        log("target thawed; the pending restore must now complete")
        try:
            assert_restored(victim.hwnd, args.guardian_timeout)
            wait_for(
                lambda: not journal_records(first.journal),
                args.guardian_timeout,
                what="the journal to drain after the target answered",
            )
            if guardian.wait(timeout=60) != 0:
                raise SmokeError("the guardian did not exit cleanly once nothing was outstanding")
        finally:
            if guardian.poll() is None:
                guardian.kill()
        seen = watcher.stop()
        log(f"recovery guardian pids seen: {sorted(seen)}")
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after the guardian lifetime gate")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


def scenario_ownerpidreuse(args, exe, settings, target, first, card) -> None:
    """A recycled owner PID must not freeze recovery.

    The planted record names a PID that is *alive right now* - this process - but
    with a creation time from a different process.  An implementation that asks
    only "is this PID running?" would consider the dead owner alive and never
    restore the window.
    """
    helpers = first.wait_helpers(args.helper_timeout)
    with Target(title="LUW untracked pid reuse victim") as victim:
        if spawn_journal_writer(first.journal, victim.hwnd) != "PARKED":
            raise SmokeError("could not establish the obligation under test")
        assert_parked(victim.hwnd, args.timeout)
        plant_owner_identity(first.journal, victim.hwnd, owner_pid=os.getpid(), owner_created=1)
        log(f"planted ownerPid={os.getpid()} (alive, foreign creation time) for hwnd={victim.hwnd}")
        guardian = spawn_frozen_guardian(first.journal, exe=exe, timeout=args.guardian_timeout)
        if guardian is None:
            raise SmokeError("the recovery guardian refused to start")
        try:
            assert_restored(victim.hwnd, args.guardian_timeout)
            wait_for(
                lambda: not journal_records(first.journal),
                args.guardian_timeout,
                what="the journal to drain despite the reused owner PID",
            )
            if guardian.wait(timeout=60) != 0:
                raise SmokeError("the guardian did not exit cleanly")
        finally:
            if guardian.poll() is None:
                guardian.kill()
        log("recovery proceeded although the recorded owner PID is alive")
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after the owner PID reuse gate")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


def scenario_badlease(args, exe, settings, target, first, card) -> None:
    """A non-finite lease must not make a record permanently unclaimable."""
    helpers = first.wait_helpers(args.helper_timeout)
    with Target(title="LUW untracked bad lease victim") as victim:
        for literal in ("Infinity", "NaN", "-Infinity"):
            # Each round starts from a fresh obligation: a discharged journal
            # removes its own file, which is itself part of what is being checked.
            if spawn_journal_writer(first.journal, victim.hwnd) != "PARKED":
                raise SmokeError("could not establish the obligation under test")
            assert_parked(victim.hwnd, args.timeout)
            plant_claim(first.journal, victim.hwnd, claim_until=literal)
            log(f"planted claimUntil={literal} for hwnd={victim.hwnd}")
            guardian = spawn_frozen_guardian(first.journal, exe=exe, timeout=args.guardian_timeout)
            if guardian is None:
                raise SmokeError("the recovery guardian refused to start")
            try:
                assert_restored(victim.hwnd, args.guardian_timeout)
                wait_for(
                    lambda: not journal_records(first.journal),
                    args.guardian_timeout,
                    what=f"the journal to drain with claimUntil={literal}",
                )
                if guardian.wait(timeout=60) != 0:
                    raise SmokeError("the guardian did not exit cleanly")
            finally:
                if guardian.poll() is None:
                    guardian.kill()
        log("every poisoned lease was recoverable")
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after the bad lease gate")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


def scenario_claimaba(args, exe, settings, target, first, card) -> None:
    """A stalled executor must not be able to end a newer executor's claim."""
    from recovery import RecoveryJournal

    helpers = first.wait_helpers(args.helper_timeout)
    with Target(title="LUW untracked claim ABA victim", hang=max(8.0, args.hang)) as victim:
        if spawn_journal_writer(first.journal, victim.hwnd) != "PARKED":
            raise SmokeError("could not establish the obligation under test")
        assert_parked(victim.hwnd, args.timeout)
        stale = plant_stale_claim(first.journal, victim.hwnd)
        log(f"planted a stale claim (generation {stale.generation}) for hwnd={victim.hwnd}")
        guardian = spawn_frozen_guardian(first.journal, exe=exe, timeout=args.guardian_timeout)
        if guardian is None:
            raise SmokeError("the recovery guardian refused to start")
        try:
            # While the guardian owns the record, the stalled executor wakes up and
            # tries to finish the job it started.
            wait_for(
                lambda: bool(journal_claim(first.journal, victim.hwnd)),
                args.guardian_timeout,
                interval=0.2,
                what="the guardian to claim the record",
            )
            live = journal_claim(first.journal, victim.hwnd)
            if int(live.get("claimGeneration") or 0) <= stale.generation:
                raise SmokeError(
                    f"the newer executor did not fence the stale claim: generation "
                    f"{live.get('claimGeneration')} <= {stale.generation}"
                )
            journal = RecoveryJournal(first.journal)
            if journal.clear_claimed(stale):
                raise SmokeError("a stale executor was allowed to clear a newer claim")
            if journal.release(stale):
                raise SmokeError("a stale executor released a lease it no longer holds")
            if not journal_has_hwnd(first.journal, victim.hwnd):
                raise SmokeError("the obligation disappeared while the guardian was executing it")
            assert_restored(victim.hwnd, args.guardian_timeout)
            wait_for(
                lambda: not journal_records(first.journal),
                args.guardian_timeout,
                what="the journal to drain after the stale attempts were rejected",
            )
            if guardian.wait(timeout=60) != 0:
                raise SmokeError("the guardian did not exit cleanly")
        finally:
            if guardian.poll() is None:
                guardian.kill()
        log("the newer claim survived every mutation from the stalled executor")
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after the claim ABA gate")
    assert_helpers_gone(helpers)
    require_no_guardian(args.guardian_exit_timeout)


def scenario_monitorgap(args, exe, settings, target, first, card) -> None:
    """Accessibility is per monitor, not per virtual bounding box.

    A window in the gap of an L-shaped layout intersects the virtual screen's
    bounding box while being on no display at all.  Recovery uses exactly this
    predicate to decide that a window is verifiably back, so bounding-box
    arithmetic would let an unreachable window pass as restored.
    """
    monitors = winapi.display_monitor_rects()
    log(f"reported monitors: {[tuple(rect) for rect in monitors]}")
    for rect in monitors:
        if not winapi.is_visible_on_monitors(rect, monitors):
            raise SmokeError(f"a monitor's own rectangle is not reported as visible: {rect}")
    left = (0, 0, 1920, 1080)
    upper_right = (2560, 0, 3840, 1080)
    gap = (2100, 500, 2500, 700)
    bounding = screen.bounding_rect((left, upper_right))
    if screen.intersection(gap, bounding) is None:
        raise SmokeError("the synthetic layout does not contain the gap this gate needs")
    if winapi.is_visible_on_monitors(gap, monitors):
        raise SmokeError("a window in the gap between monitors counts as visible")
    if winapi.is_effectively_onscreen(0):
        raise SmokeError("a non-existent window reports itself as on screen")
    parked = winapi.visible_window_size(int(target.hwnd))
    log(f"synthetic gap window rejected; live target visible size: {parked}")
    # The real application has to keep working with the new predicate.
    ensure_park(first.wait_card(args.timeout), target.hwnd, args.timeout)
    first.request_quit()
    if first.wait_exit(args.timeout) != 0:
        raise SmokeError("main did not quit after the monitor gap gate")
    assert_restored(target.hwnd, args.timeout)
    if journal_records(first.journal):
        raise SmokeError("recovery journal still lists a restored window")
    require_no_guardian(args.guardian_exit_timeout)


def scenario_parkmark(args, exe, settings, target, _first=None, _card=None) -> None:
    """Recover a marked window after its writer exits and its signature changes."""
    with Target(title="LUW untracked mark victim") as victim, Target(title="LUW unrelated offscreen") as other:
        journal = settings.with_suffix(settings.suffix + ".park.json")
        if spawn_journal_writer(journal, victim.hwnd) != "PARKED":
            raise SmokeError("could not stamp and park the victim")
        operation = winapi.park_operation_id(victim.hwnd)
        if not operation:
            raise SmokeError("mark was not readable after its writer exited")
        # Equivalent evidence to changing virtual origin: the old rectangle no
        # longer matches the current parking signature. No display settings change.
        for hwnd in (victim.hwnd, other.hwnd):
            user32.SetWindowPos(hwnd, None, -20000, -18000, 600, 400, winapi.SWP_NOZORDER)
        other_rect = winapi.get_window_rect(other.hwnd)
        if winapi.looks_like_lookup_parked(victim.hwnd):
            raise SmokeError("test geometry still matches the current parking signature")
        journal.write_text("{ damaged recovery journal", encoding="utf-8")
        app = AppRun(args.mode, exe, settings).start()
        try:
            app.wait_panel(args.timeout)
            assert_restored(victim.hwnd, args.guardian_timeout)
            if winapi.get_window_rect(other.hwnd) != other_rect:
                raise SmokeError("damage sweep moved an unrelated offscreen window")
            app.request_quit()
            if app.wait_exit(args.timeout) != 0:
                raise SmokeError("quit after mark recovery failed")
        finally:
            if app.alive():
                app.kill()
    log("process-independent mark recovered only its own victim without a geometry signature")


def scenario_outerjob(args, exe, settings, target) -> None:
    """A guardian trapped in an enclosing Job Object must be rejected.

    The helper is first placed in a controlled Job Object that deliberately does
    not grant BREAKAWAY_OK. From there it calls the real guardian launcher, and
    for ``--mode exe`` the child it tries to launch is the exact frozen onefile
    artifact. Accepting that child would recreate the production failure: a
    launcher closing/terminating the outer job could kill LookUp and its recovery
    executor together.

    The helper also reports the job context it was launched under, so the gate
    proves it exercised the hostile-job refusal rather than passing because
    something unrelated happened to fail.
    """
    go = settings.parent / "outerjob.go"
    journal = Path(str(settings) + ".park.json")
    helper_code = r'''import json
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
go = Path(sys.argv[2])
journal = Path(sys.argv[3])
mode = sys.argv[4]
exe = sys.argv[5]
sys.path.insert(0, str(root / "src"))
import restoreguard

while not go.exists():
    time.sleep(0.01)
context = restoreguard.enclosing_job()
if mode == "exe":
    command = [exe, restoreguard.GUARDIAN_ARG, str(journal), "0", "0", ""]
else:
    command = [
        sys.executable, str(root / "src" / "restoreguard.py"),
        restoreguard.GUARDIAN_ARG, str(journal), "0", "0", "",
    ]
process = restoreguard._spawn(
    command, timeout=8.0, expect=restoreguard.GUARDIAN_READY_TOKEN
)
print(json.dumps({"accepted": process is not None,
                  "job_context": context.kind,
                  "pid": int(process.pid) if process is not None else 0}), flush=True)
if process is not None:
    restoreguard._link_for(process).close()
    try:
        process.wait(timeout=10.0)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
'''
    outer = restoreguard._GuardianLaunchJob()
    if not outer.available:
        raise SmokeError("could not create the controlled outer Job Object")
    helper = subprocess.Popen(
        [sys.executable, "-u", "-c", helper_code, str(ROOT), str(go), str(journal),
         args.mode, str(exe or "")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        if not outer.adopt(helper):
            raise SmokeError("could not place the outer-job helper in its controlled Job Object")
        go.touch()
        try:
            stdout, stderr = helper.communicate(timeout=30.0)
        except subprocess.TimeoutExpired as exc:
            raise SmokeError("outer-job guardian launch did not finish") from exc
        if helper.returncode != 0:
            raise SmokeError(
                f"outer-job helper failed rc={helper.returncode}: {stderr.strip()}"
            )
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise SmokeError("outer-job helper produced no result")
        try:
            result = json.loads(lines[-1])
        except ValueError as exc:
            raise SmokeError(f"outer-job helper result was not JSON: {stdout!r}") from exc
        if result.get("accepted"):
            raise SmokeError(
                "a recovery guardian trapped in an enclosing Job Object was accepted "
                f"as independent (pid={result.get('pid')})"
            )
        if result.get("job_context") != restoreguard.GUARDIAN_CONTEXT_HOSTILE:
            raise SmokeError(
                "the outer-job helper did not run under a kill-on-close Job Object "
                f"(reported {result.get('job_context')!r}), so it proved nothing about "
                "the hostile-job refusal"
            )
        log("guardian launch correctly failed closed inside a no-breakaway outer Job Object")
    finally:
        if helper.poll() is None:
            try:
                helper.kill()
                helper.wait(timeout=10.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        outer.close()


SCENARIOS = {
    "responsive": scenario_responsive,
    "slow": scenario_slow,
    "inflight": scenario_inflight,
    "hardkill": scenario_hardkill,
    "aged": scenario_aged,
    "badjournal": scenario_badjournal,
    "jobfail": scenario_jobfail,
    "journalrace": scenario_journalrace,
    "journalio": scenario_journalio,
    "guardianfail": scenario_guardianfail,
    "guardianlimit": scenario_guardianlimit,
    "ownerpidreuse": scenario_ownerpidreuse,
    "badlease": scenario_badlease,
    "claimaba": scenario_claimaba,
    "monitorgap": scenario_monitorgap,
    "parkmark": scenario_parkmark,
    "outerjob": scenario_outerjob,
}
# Scenarios that start and stop their own application runs instead of driving
# the one the driver started for them.  Part of the registry's contract, so the
# two cannot drift apart.
OWNS_APP_RUN = {"badjournal", "parkmark", "outerjob"}
# Scenarios whose park is committed before the kill/exit.
NEEDS_PARK = {"responsive", "slow", "hardkill", "aged", "jobfail"}
# Scenarios that need a target that stalls inside its window procedure.
NEEDS_STALL = {"slow", "inflight", "journalrace"}


def run(args: argparse.Namespace) -> int:
    exe = Path(args.exe).resolve() if args.exe else None
    if args.scenario not in SCENARIOS:
        raise SmokeError(f"unknown scenario: {args.scenario}")
    stop_stray_instances()
    stop_stray_guardians()
    with tempfile.TemporaryDirectory(prefix="lookup-smoke-") as tmp:
        tmpdir = Path(tmp)
        settings = tmpdir / "settings.json"
        hang = args.hang if args.scenario in NEEDS_STALL else 0.0
        # The target stalls while it handles WM_WINDOWPOSCHANGED, i.e. after the
        # new position has been applied.  That is the only stall a caller can
        # actually be blocked by on both paths: the park move itself (``moved``)
        # and the restore move (same message).  Stalling WM_SHOWWINDOW instead
        # would not block a placement call at all and would silently make the
        # restore path untestable.
        stall_on = "moved"
        extra_env = (
            {"LOOKUPWINDOWS_TEST_HOOKS": "job_adopt_fail"}
            if args.scenario == "jobfail"
            else None
        )
        with Target(hang=hang, stall_on=stall_on) as target:
            write_settings(settings)
            handler = SCENARIOS[args.scenario]
            if args.scenario in OWNS_APP_RUN:
                handler(args, exe, settings, target)
                return run_tail(args)
            first = AppRun(args.mode, exe, settings, extra_env=extra_env).start()
            try:
                first.wait_panel(args.timeout)
                handler(args, exe, settings, target, first, first.wait_card(args.timeout))
            finally:
                if first.alive():
                    first.kill()
            return run_tail(args)


def wait_for_executor_exit(args) -> None:
    """The end of every scenario: nothing may be left running behind it.

    A guardian is *expected* to outlive the process that started it - that is what
    makes a hard kill recoverable - but it must end by itself once its partner is
    gone, so the scenario waits for that instead of failing on the first sighting.
    """
    wait_for(
        lambda: not guardian_pids(),
        float(getattr(args, "guardian_exit_timeout", 45.0)),
        interval=0.5,
        what="the recovery guardian to exit with its partner",
    )


def run_tail(args) -> int:
    """Wait for the tail of a scenario that reached it, and report that it passed.

    ``PASS`` belongs to the scenario, not to this wait: a caller that is already
    handling a failure uses :func:`wait_for_executor_exit` instead, so a failed
    scenario can never print a pass for the cleanup that followed it.
    """
    wait_for_executor_exit(args)
    log("PASS")
    return 0


def run_all(args) -> int:
    """Run every registered scenario, in registry order.

    This is the interface a release gate uses.  It exists so that adding a scenario
    to :data:`SCENARIOS` *is* adding a release gate: a hand-written list in the
    workflow is a list that quietly stops being the truth.
    """
    failures: list[str] = []
    for name in SCENARIOS:
        log(f"=== scenario {name} ===")
        started = time.time()
        scenario_args = argparse.Namespace(**vars(args))
        scenario_args.scenario = name
        try:
            run(scenario_args)
        except (SmokeError, subprocess.TimeoutExpired, OSError) as exc:
            failures.append(f"{name}: {exc}")
            log(f"--- scenario {name} FAILED: {exc}")
            # A failed scenario has already closed its targets and owner. Let its
            # guardian finish before a subsequent scenario checks for survivors.
            # This is cleanup, not a result: reporting it as a pass would contradict
            # the failure recorded right above.
            try:
                wait_for_executor_exit(args)
            except SmokeError as cleanup_error:
                failures.append(f"{name} cleanup: {cleanup_error}")
            continue
        log(f"--- scenario {name} passed in {time.time() - started:.0f}s")
    if failures:
        raise SmokeError(
            "runtime scenarios failed: " + "; ".join(failures)
        )
    log(f"all {len(SCENARIOS)} runtime scenarios passed")
    return 0


def list_scenarios(as_json: bool) -> int:
    names = list(SCENARIOS)
    if as_json:
        print(json.dumps({
            "scenarios": [
                {
                    "name": name,
                    "ownsAppRun": name in OWNS_APP_RUN,
                    "needsPark": name in NEEDS_PARK,
                    "needsStall": name in NEEDS_STALL,
                }
                for name in names
            ]
        }))
    else:
        for name in names:
            print(name)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("source", "exe"), default="source")
    parser.add_argument("--exe", default="", help="frozen artifact to test (mode=exe)")
    parser.add_argument(
        "--scenario",
        choices=(*SCENARIOS, "badjournal"),
        default="responsive",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="run every scenario of the SCENARIOS registry, in order",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="print the registry and exit (add --json for machine-readable output)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable --list-scenarios")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--guardian-timeout", type=float, default=90.0)
    parser.add_argument("--guardian-exit-timeout", type=float, default=45.0)
    parser.add_argument("--helper-timeout", type=float, default=15.0)
    parser.add_argument("--hang", type=float, default=STALL_SEC, help="target stall in slow scenarios")
    args = parser.parse_args()
    if args.list_scenarios:
        return list_scenarios(args.json)
    os.environ.setdefault("LOOKUPWINDOWS_SMOKE", "1")
    try:
        return run_all(args) if args.all else run(args)
    except SmokeError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    mp_freeze = getattr(sys.modules.get("multiprocessing"), "freeze_support", None)
    if mp_freeze is not None:
        mp_freeze()
    raise SystemExit(main())
