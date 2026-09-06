<#
.SYNOPSIS
  Scan the git working tree for unstaged and untracked files, then bundle
  them into a timestamped zip.

.DESCRIPTION
  Uses `git status -s --untracked-files=all` to collect two kinds of changes:
    1) ??  Untracked
    2) ' M' Modified but not staged

  The collected files are zipped into <NamePrefix>_yyyyMMdd_HHmmss.zip
  (default name: patch_yyyyMMdd_HHmmss.zip) using .NET's
  System.IO.Compression.ZipFile so the script has zero external tool
  dependencies and works identically on Windows PowerShell 5.1 and 7+.

  The script will:
    * Verify the current directory is inside a git repository and `cd` to the repo root.
    * Exit with code 0 (no empty zip produced) when there is nothing to bundle.
    * Create the output directory if it does not exist.

  NOTE on encoding:
  This file is intentionally ASCII-only for log / error messages so that
  Windows PowerShell 5.1 (which assumes the system ANSI codepage, e.g. GBK
  on zh-CN) can parse it without mojibake. The Chinese comments below are
  kept inside `#` comment blocks; PowerShell does not tokenize them as code,
  but if your editor saves the file as UTF-8 without BOM, even those may
  appear garbled in `Get-Content`. The script body itself never depends on
  any non-ASCII string, so it runs identically on PowerShell 5.1 and 7+.

.PARAMETER OutputDir
  Output directory for the zip. Defaults to the current directory.

.PARAMETER NamePrefix
  Filename prefix for the zip. Defaults to `patch`.

.EXAMPLE
  .\pack_changes.ps1
  # Produces patch_20260904_153012.zip in the current directory.

.EXAMPLE
  .\pack_changes.ps1 -OutputDir dist -NamePrefix snapshot
  # Produces dist/snapshot_20260904_153012.zip.
#>
[CmdletBinding()]
param(
    [string]$OutputDir = ".",
    [string]$NamePrefix = "patch"
)

$ErrorActionPreference = "Stop"

# 1) Must be inside a git repo; cd to the repo root so the script can be
#    invoked from any subdirectory and still produce correct relative paths.
$gitRoot = git rev-parse --show-toplevel 2>$null
if (-not $gitRoot) {
    Write-Host "[ERROR] Not inside a git repository." -ForegroundColor Red
    exit 1
}
Set-Location $gitRoot

# 2) Scan file status
Write-Host "Scanning file status..." -ForegroundColor Cyan
$rawStatus = git status -s --untracked-files=all

# 3) Filter target files:
#    ??  -> Untracked
#     M -> Modified but not staged (note: first column is a space)
$targetFiles = $rawStatus | Where-Object {
    $_ -match '^\?\?' -or $_ -match '^ M'
} | ForEach-Object {
    # Strip the leading 3 chars ("?? " or " M ") to get the raw path
    $path = $_.Substring(3).Trim()
    if ($path) { $path }
}

if (-not $targetFiles -or $targetFiles.Count -eq 0) {
    Write-Host "No unstaged or untracked files to bundle." -ForegroundColor Yellow
    exit 0
}

Write-Host "Found $($targetFiles.Count) file(s):" -ForegroundColor Green
$targetFiles | ForEach-Object { Write-Host "  - $_" }

# 4) Ensure the output directory exists
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
$resolvedOutputDir = (Resolve-Path $OutputDir).Path

# 5) Build a timestamped filename
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$zipBaseName = "${NamePrefix}_$timestamp.zip"
$zipPath = Join-Path $resolvedOutputDir $zipBaseName

Write-Host ""
Write-Host "Bundling into $zipBaseName ..." -ForegroundColor Cyan

# 6) Bundle as zip via .NET BCL. This avoids Windows PowerShell 5.1's
#    well-known argv splat bugs when piping a string[] to native exes
#    (e.g. `&` / Start-Process -ArgumentList not expanding the array, and
#    .NET Framework 4.x lacking ProcessStartInfo.ArgumentList). The BCL
#    ZipFile is part of the framework, so no external tar / 7z is needed.
try {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $fileList = [string[]]$targetFiles
    $archive = [System.IO.Compression.ZipFile]::Open(
        $zipPath,
        [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($rel in $fileList) {
            $abs = Join-Path $gitRoot $rel
            if (-not (Test-Path -LiteralPath $abs)) {
                throw "Source file not found: $rel"
            }
            # zip spec: forward slashes inside the archive entry name
            $entryName = $rel -replace '\\', '/'
            $entry = $archive.CreateEntry(
                $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal)
            $in  = [System.IO.File]::OpenRead($abs)
            $out = $entry.Open()
            try { $in.CopyTo($out) } finally { $in.Dispose(); $out.Dispose() }
        }
    } finally {
        $archive.Dispose()
    }
} catch {
    Write-Host "[ERROR] Bundling failed: $_" -ForegroundColor Red
    exit 1
}

if (Test-Path $zipPath) {
    Write-Host "Done. Saved to: $zipPath" -ForegroundColor Green
} else {
    Write-Host "[ERROR] Bundling finished but the zip file is missing." -ForegroundColor Red
    exit 1
}
