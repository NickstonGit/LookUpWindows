param(
    [Parameter(Mandatory = $true)]
    [string]$ArchivePath
)

$ErrorActionPreference = "Stop"
$archive = (Resolve-Path $ArchivePath).Path
$temp = Join-Path $env:TEMP ("LookUpWindows_verify_" + [guid]::NewGuid().ToString("N"))
$required = @(
    "src\app.py",
    "src\winapi.py",
    "src\config.py",
    "src\change_logic.py",
    "src\windowmatch.py",
    "src\screen.py",
    "src\recovery.py",
    "src\restoreguard.py",
    "tests\test_config_model.py",
    "tests\test_patch_regressions.py",
    "tests\test_recovery_journal.py",
    "tests\test_restore_guard.py",
    "tests\test_recovery_io_failures.py",
    "tests\test_recovery_ownership.py",
    "tests\test_guardian_handshake.py",
    "config\settings.example.json",
    ".github\workflows\windows-ci.yml",
    ".github\workflows\release.yml",
    "tools\package_source.ps1",
    "tools\check_source_imports.py",
    "tools\check_build_env.py",
    "tools\runtime_smoke.py",
    "tools\smoke_target.py",
    "tools\stop_running_instance.py"
)
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $badEntries = @($zip.Entries | Where-Object { $_.FullName.Contains('\') })
        if ($badEntries.Count -gt 0) {
            $sample = ($badEntries | Select-Object -First 3 | ForEach-Object { $_.FullName }) -join ", "
            throw "Archive structure check failed; ZIP entries use backslashes: $sample"
        }
    } finally {
        $zip.Dispose()
    }

    New-Item -ItemType Directory -Path $temp | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $temp -Force
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $temp $relative) -PathType Leaf)) {
            throw "Archive structure check failed; missing: $relative"
        }
    }
    Push-Location $temp
    try {
        python tools/check_source_imports.py
        if ($LASTEXITCODE -ne 0) { throw "source import-closure check failed" }
        python tools/check_build_env.py --mode dev
        if ($LASTEXITCODE -ne 0) { throw "source build-environment check failed" }
        python -m compileall -q src
        if ($LASTEXITCODE -ne 0) { throw "compileall failed" }
        python -m pytest -q tests
        if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
        if ($env:OS -eq "Windows_NT") {
            python -c "import sys; sys.path.insert(0, 'src'); import config, change_logic, windowmatch, screen, winapi, dwm, winui, trayicon, icon, recovery, restoreguard, app"
            if ($LASTEXITCODE -ne 0) { throw "application import smoke test failed" }
        }
    } finally {
        Pop-Location
    }
    Write-Host "Archive verified: $archive"
} finally {
    if (Test-Path -LiteralPath $temp) {
        $resolvedStage = (Resolve-Path -LiteralPath $temp).Path
        $resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path.TrimEnd('\') + '\'
        if (-not $resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -or
            (Split-Path -Leaf $resolvedStage) -notlike 'LookUpWindows_verify_*') {
            throw "Refusing to remove unexpected verification path: $resolvedStage"
        }
        Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
