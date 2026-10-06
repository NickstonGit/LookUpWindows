param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $root "LookUpWindows-src.zip"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $root $OutputPath
}

# The public file set has one source of truth shared with release_manifest.py.
# This prevents the ZIP and the attested source fingerprint from drifting apart.
# Windows PowerShell 5.1 returns a top-level JSON array as a single nested array
# from ConvertFrom-Json (only PowerShell 7 unwraps it), so the result is
# flattened explicitly: arch.bat runs this script through "powershell".
$contractJson = python (Join-Path $root "tools\source_contract.py") --root $root --json
if ($LASTEXITCODE -ne 0) {
    throw "Source contract validation failed"
}
$contractParsed = $contractJson | ConvertFrom-Json
$files = @()
foreach ($item in $contractParsed) {
    if ($item -is [System.Array]) {
        $files += $item
    } else {
        $files += $item
    }
}
$files = @($files | ForEach-Object { [string]$_ })
if ($files.Count -eq 0) {
    throw "Source contract returned no files"
}

python (Join-Path $root "tools\check_source_imports.py")
if ($LASTEXITCODE -ne 0) {
    throw "Source import-closure check failed"
}

$stage = Join-Path $env:TEMP ("LookUpWindows_source_stage_" + [guid]::NewGuid().ToString("N"))
$outputDir = Split-Path -Parent $OutputPath
if (-not $outputDir) { $outputDir = $root }
$tempArchive = Join-Path $outputDir (".LookUpWindows-src-" + [guid]::NewGuid().ToString("N") + ".zip")
$backupArchive = Join-Path $outputDir (".LookUpWindows-src-" + [guid]::NewGuid().ToString("N") + ".bak")

try {
    # Validate and stage first. Existing known-good archives are untouched until
    # the new archive has been created and opened successfully.
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    New-Item -ItemType Directory -Path $stage | Out-Null
    foreach ($relative in $files) {
        $source = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Required source file is missing: $relative"
        }
        $destination = Join-Path $stage $relative
        $destinationDir = Split-Path -Parent $destination
        New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }

    # The built-in PowerShell archive cmdlet can emit backslashes in ZIP entry names. Use
    # Python's zipfile writer so the archive follows the ZIP convention and is
    # extractable by standard tools on Windows, Linux and macOS.
    $zipWriter = @'
import sys
import zipfile
from pathlib import Path

stage = Path(sys.argv[1])
archive = Path(sys.argv[2])
paths = sorted(
    (path for path in stage.rglob("*") if path.is_file()),
    key=lambda path: path.relative_to(stage).as_posix().casefold(),
)
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
    for path in paths:
        name = path.relative_to(stage).as_posix()
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = (0o100644 << 16)
        info.compress_type = zipfile.ZIP_STORED
        handle.writestr(info, path.read_bytes())
'@
    $zipWriter | python - "$stage" "$tempArchive"
    if ($LASTEXITCODE -ne 0) {
        throw "ZIP creation failed"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($tempArchive)
    try {
        $badEntries = @($zip.Entries | Where-Object { $_.FullName.Contains('\') })
        if ($badEntries.Count -gt 0) {
            throw "Archive verification failed; ZIP entries contain backslashes"
        }
        $entryNames = @($zip.Entries | ForEach-Object { $_.FullName.Replace('/', '\') })
        foreach ($required in @(
            "src\app.py",
            "src\winapi.py",
            "src\config.py",
            "src\change_logic.py",
            "src\windowmatch.py",
            "src\screen.py",
            "src\recovery.py",
            "src\restoreguard.py",
            "tools\check_source_imports.py",
            "tools\check_build_env.py",
            "tools\runtime_smoke.py"
        )) {
            if ($entryNames -notcontains $required) {
                throw "Archive verification failed; missing: $required"
            }
        }
    } finally {
        $zip.Dispose()
    }

    if (Test-Path -LiteralPath $OutputPath -PathType Leaf) {
        # Source and destination live in the same directory/volume, so File.Replace
        # gives us an atomic swap and preserves the previous known-good archive if
        # staging or ZIP verification fails before this point.
        # A real backup path is mandatory: Windows PowerShell coerces $null to an
        # empty string for .NET string parameters, and File.Replace then fails with
        # "The path is not of a legal form" instead of accepting a null backup.
        [System.IO.File]::Replace($tempArchive, $OutputPath, $backupArchive, $true)
    } else {
        [System.IO.File]::Move($tempArchive, $OutputPath)
    }
    Write-Host "Done: $OutputPath"
} finally {
    if (Test-Path -LiteralPath $stage) {
        $resolvedStage = (Resolve-Path -LiteralPath $stage).Path
        $resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path.TrimEnd('\') + '\'
        if (-not $resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -or
            (Split-Path -Leaf $resolvedStage) -notlike 'LookUpWindows_source_stage_*') {
            throw "Refusing to remove unexpected staging path: $resolvedStage"
        }
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tempArchive) {
        Remove-Item -LiteralPath $tempArchive -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $backupArchive) {
        Remove-Item -LiteralPath $backupArchive -Force -ErrorAction SilentlyContinue
    }
}
