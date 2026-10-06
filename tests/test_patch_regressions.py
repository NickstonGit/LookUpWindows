import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found in {path.name}")


def method_source(path: Path, class_name: str, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    return ast.get_source_segment(source, child) or ""
    raise AssertionError(f"{class_name}.{name} not found in {path.name}")


class PatchRegressionTests(unittest.TestCase):
    def test_source_packaging_uses_shared_contract_and_reproducible_zip_entries(self):
        source = (ROOT / "tools" / "package_source.ps1").read_text(encoding="utf-8")
        contract = (ROOT / "tools" / "source_contract.py").read_text(encoding="utf-8")
        self.assertIn("LookUpWindows_source_stage_", source)
        self.assertIn("Copy-Item -LiteralPath $source -Destination $destination", source)
        self.assertIn("tools\\source_contract.py", source)
        self.assertIn('("src", "*.py")', contract)
        self.assertIn('("tests", "*.py")', contract)
        self.assertIn('"src/change_logic.py"', contract)
        self.assertIn('"src/windowmatch.py"', contract)
        self.assertIn('path.relative_to(stage).as_posix()', source)
        self.assertIn("zipfile.ZipFile", source)
        self.assertIn("date_time=(1980, 1, 1, 0, 0, 0)", source)
        self.assertIn("ZIP_STORED", source)
        self.assertNotIn("Compress-Archive", source)
        self.assertIn("[System.IO.File]::Replace($tempArchive, $OutputPath, $backupArchive, $true)", source)
        self.assertIn("[System.IO.File]::Move($tempArchive, $OutputPath)", source)
        self.assertNotIn("[System.IO.File]::Replace($tempArchive, $OutputPath, $null", source)
        # arch.bat runs this script through Windows PowerShell 5.1, where
        # ConvertFrom-Json hands back a top-level JSON array nested in one array.
        # Without the explicit flattening every path becomes a single string and
        # Join-Path rejects it, so the archive cannot be built at all.
        self.assertNotIn("@($contractJson | ConvertFrom-Json)", source)
        self.assertIn("$contractJson | ConvertFrom-Json", source)
        self.assertIn("foreach ($item in $contractParsed)", source)

    def test_build_scripts_pin_same_pyinstaller_version(self):
        requirements = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        match = re.search(r"pyinstaller==([0-9][0-9.]*)", requirements)
        self.assertIsNotNone(match, "requirements-build.txt must pin an exact pyinstaller version")
        version = match.group(1)
        # PyInstaller < 6.17 cannot collect Tcl/Tk data on Python 3.14, where the
        # Tcl/Tk libraries live in a DLL-embedded zipfs archive, so the frozen app
        # dies at startup with "Tcl data directory ... not found".
        self.assertGreaterEqual(
            tuple(int(part) for part in version.split(".")),
            (6, 17),
            "PyInstaller must be new enough to support Python 3.14 Tcl/Tk zipfs data",
        )
        source = (ROOT / "build-onefile.bat").read_text(encoding="utf-8")
        self.assertIn(
            f"PyInstaller.__version__ == '{version}'",
            source,
            "build-onefile.bat must pin the same PyInstaller version as requirements-build.txt",
        )

    def test_ci_uses_exact_source_archive_name(self):
        source = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")
        self.assertIn('$archive = Get-Item "LookUpWindows-src.zip" -ErrorAction Stop', source)
        self.assertNotIn('Get-ChildItem -File "LookUpWindows-src-*.zip"', source)

    def test_artifact_verifier_checks_runtime_closure_and_imports(self):
        source = (ROOT / "tools" / "verify_source_archive.ps1").read_text(encoding="utf-8")
        self.assertIn('"src\\app.py"', source)
        self.assertIn('"src\\change_logic.py"', source)
        self.assertIn('"src\\windowmatch.py"', source)
        self.assertIn('"tests\\test_patch_regressions.py"', source)
        self.assertIn("python tools/check_source_imports.py", source)
        self.assertIn("python -m compileall -q src", source)
        self.assertIn("python -m pytest -q tests", source)
        self.assertIn("application import smoke test failed", source)
        self.assertIn("ZIP entries use backslashes", source)

    def test_ci_and_release_check_source_import_closure(self):
        for relative in (
            Path(".github/workflows/windows-ci.yml"),
            Path(".github/workflows/release.yml"),
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("python tools/check_source_imports.py", source)

    def test_readme_version_matches_app_version(self):
        config = (SRC / "config.py").read_text(encoding="utf-8")
        match = re.search(r'^APP_VERSION = "([^"]+)"', config, flags=re.MULTILINE)
        self.assertIsNotNone(match)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Версия: **{match.group(1)}**", readme)
        self.assertIn(f"git tag v{match.group(1)}", readme)

    def test_dpi_awareness_falls_back_when_modern_api_returns_failure(self):
        main = function_source(SRC / "winui.py", "enable_dpi_awareness")
        self.assertIn("if setter(ctypes.c_void_p(-4))", main)
        self.assertIn("if setter(2) == 0", main)
        self.assertIn("SetProcessDPIAware", main)
        self.assertLess(
            main.index("user32.SetProcessDpiAwarenessContext"),
            main.index("shcore.SetProcessDpiAwareness"),
        )
        self.assertLess(
            main.index("shcore.SetProcessDpiAwareness"),
            main.index("user32.SetProcessDPIAware"),
        )

        helper = function_source(SRC / "winapi.py", "_enable_capture_dpi_awareness")
        self.assertIn("if setter(ctypes.c_void_p(-4))", helper)
        self.assertIn('ctypes.WinDLL("shcore"', helper)
        self.assertIn("if setter(2) == 0", helper)
        self.assertIn("SetProcessDPIAware", helper)

    def test_autostart_normalizes_explicit_config_and_uses_src_app(self):
        source = (SRC / "config.py").read_text(encoding="utf-8")
        self.assertIn("Path(os.path.expandvars(explicit)).expanduser().resolve()", source)
        self.assertIn('PROJECT_ROOT / "src" / "app.py"', source)

    def test_wndproc_has_exception_boundary(self):
        body = function_source(SRC / "app.py", "wnd_proc")
        self.assertIn("except Exception", body)
        self.assertIn("logger.exception", body)
        self.assertIn("DefWindowProcW", body)

    def test_change_detector_uses_killable_process_timeout(self):
        source = (SRC / "winapi.py").read_text(encoding="utf-8")
        self.assertIn('multiprocessing.get_context("spawn")', source)
        self.assertIn('_emit_status(request, "capture_timeout")', source)
        self.assertIn("process.terminate()", source)
        self.assertIn("max_workers: int = 2", source)
        self.assertIn("capture_timeout: float = 3.0", source)
        self.assertIn("reset_token", source)
        helper = function_source(SRC / "winapi.py", "_capture_process_main")
        self.assertIn("_enable_capture_dpi_awareness()", helper)

    def test_capture_downscale_is_done_by_gdi_to_comparison_grid(self):
        body = function_source(SRC / "winapi.py", "_capture_grid")
        self.assertIn("source_dc = user32.GetDC(hwnd)", body)
        self.assertIn("self.grid_w", body)
        self.assertIn("self.grid_h", body)
        self.assertIn("gdi32.StretchBlt", body)
        self.assertIn("cache_full = width * height <= 3_000_000", body)
        self.assertIn("user32.ReleaseDC(hwnd, source_dc)", body)
        self.assertNotIn("for gy in range", body)
        self.assertNotIn("for gx in range", body)
        self.assertIn("close_capture_resources", (SRC / "winapi.py").read_text(encoding="utf-8"))

    def test_cleanup_restore_is_not_synchronous_on_ui_path(self):
        body = function_source(SRC / "app.py", "restore_parked_source")
        self.assertIn("_queue_cleanup_restore", body)
        self.assertNotIn("restore_parked_window_sync(hwnd, state)", body)

    def test_cleanup_restore_keeps_registry_until_verified(self):
        source = (SRC / "app.py").read_text(encoding="utf-8")
        self.assertIn("self._recovery_registry", source)
        self.assertIn("_complete_recovery_attempt", source)
        self.assertIn("needs_retry = True", source)

    def test_park_failure_rolls_back(self):
        body = function_source(SRC / "winapi.py", "park_window_offscreen_sync")
        self.assertIn("restore_parked_window_sync(hwnd, state)", body)

    def test_parked_state_protects_against_hwnd_reuse(self):
        source = (SRC / "winapi.py").read_text(encoding="utf-8")
        self.assertIn("process_created: int | None", source)
        self.assertIn("def window_matches_parked_state", source)
        self.assertIn("_query_process_identity(pid", source)

    def test_change_detector_has_bounded_join_and_health(self):
        source = (SRC / "winapi.py").read_text(encoding="utf-8")
        self.assertIn("def close(self, timeout: float = 1.5) -> bool", source)
        self.assertIn("worker.join(timeout=", source)
        self.assertIn("def healthy(self) -> bool", source)
        self.assertIn('logger.exception("Change detector supervisor iteration crashed")', source)

    def test_config_save_is_off_ui_thread(self):
        app = function_source(SRC / "app.py", "_save_config_now")
        config = (SRC / "config.py").read_text(encoding="utf-8")
        self.assertIn("self._config_saver.submit(self.config)", app)
        self.assertIn("class AsyncConfigSaver", config)
        self.assertIn("LookUpWindows-ConfigSaver", config)

    def test_deferred_pump_has_idle_backoff(self):
        body = function_source(SRC / "app.py", "_pump_deferred")
        self.assertIn("_defer_idle_passes", body)
        self.assertIn("(30, 60, 120, 200)", body)

    def test_dwm_update_skips_identical_state(self):
        body = function_source(SRC / "dwm.py", "update")
        self.assertIn("state == self._last_applied", body)
        self.assertIn("self._last_applied = state", body)

    def test_expired_access_denied_cache_entry_is_removed(self):
        body = function_source(SRC / "winapi.py", "process_access_denied")
        self.assertIn("_process_access_denied.pop(pid, None)", body)


    def test_tray_class_uses_hwnd_dispatch_and_post_menu_wm_null(self):
        source = (SRC / "trayicon.py").read_text(encoding="utf-8")
        self.assertIn("_instances", source)
        self.assertIn("_WINDOW_PROC = WNDPROC(_dispatch_window_proc)", source)
        self.assertIn("PostMessageW(self.hwnd, WM_NULL", source)
        self.assertIn("WM_NCDESTROY", source)
        self.assertNotIn("WNDPROC(self._wnd_proc)", source)

    def test_icon_restores_selected_bitmap_in_exception_path(self):
        body = function_source(SRC / "icon.py", "_render_bitmap")
        self.assertIn("gdi32.SelectObject(dc, old)", body)
        self.assertIn("raw[3::4]", body)

    def test_dwm_thumbnail_has_safety_finalizer(self):
        source = (SRC / "dwm.py").read_text(encoding="utf-8")
        self.assertIn("weakref.finalize", source)
        self.assertIn("_unregister_thumbnail", source)

    def test_change_timer_is_disabled_with_global_detection(self):
        body = function_source(SRC / "app.py", "_sync_change_timer")
        self.assertIn("kill_timer", body)
        self.assertIn("self.config.change_detection", body)
        self.assertIn("_reset_change_detection_state", body)

    def test_resize_height_is_clamped_to_monitor(self):
        body = function_source(SRC / "app.py", "_on_move")
        self.assertIn("monitor_height", body)
        self.assertIn("min(self.desired_height(width), monitor_height)", body)

    def test_round_region_change_repaints_the_window(self):
        # SetWindowRgn discards the update region Windows queued for the resize,
        # so layered cards kept stale pixels on the newly exposed strip (right
        # border, header buttons) until an unrelated invalidate happened.
        card = method_source(SRC / "app.py", "CardWnd", "_apply_shape")
        self.assertIn("set_round_region", card)
        self.assertIn("self._invalidate()", card)
        self.assertLess(
            card.index("set_round_region"),
            card.index("self._invalidate()"),
            "the invalidate must come after SetWindowRgn, not before",
        )
        big = method_source(SRC / "app.py", "BigPreviewWnd", "_apply_shape")
        self.assertIn("set_round_region", big)
        self.assertIn("winui.invalidate(self.hwnd)", big)


if __name__ == "__main__":
    unittest.main()
