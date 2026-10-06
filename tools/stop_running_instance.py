"""Stop a running LookUp Windows instance through its normal shutdown path.

Force-killing the application can leave a parked foreign window off-screen,
because the process dies before its restore pass can finish.  Build scripts use
this helper instead: it asks the running instance to quit, waits for it to
actually disappear, and fails loudly when it does not.

Exit codes: 0 = no instance was running or it exited cleanly, 1 = an instance
is still running.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import winui  # noqa: E402


def main() -> int:
    timeout = 20.0
    panel = int(winui.user32.FindWindowW("WPCtrl", None) or 0)
    if not panel:
        print("No running LookUp Windows instance found.")
        return 0
    print("Requesting graceful shutdown of the running instance...")
    if not winui.request_graceful_quit(timeout_ms=3000):
        print("The running instance did not accept the shutdown request.", file=sys.stderr)
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not int(winui.user32.FindWindowW("WPCtrl", None) or 0):
            print("Running instance exited cleanly.")
            return 0
        time.sleep(0.25)
    print(
        "The running instance did not exit within "
        f"{timeout:.0f}s. Close it from the tray and try again: forcing a kill "
        "can leave a parked source window off-screen.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())