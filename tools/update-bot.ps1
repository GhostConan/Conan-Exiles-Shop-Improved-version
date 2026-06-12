<#
.SYNOPSIS
    Updates an existing Conan Exiles Shop Bot install from GitHub master.

.DESCRIPTION
    Downloads the repo as a single zipball (one HTTP request — avoids the
    anonymous GitHub API 60/h rate limit), extracts it to a temp folder,
    then copies bot/, tools/, README.md, .env.example and a few other root
    files over the existing install. Clears Python's __pycache__ so the
    next start picks up the new code.

    Does NOT touch your .env file, MariaDB data, or anything outside the
    repo tree.

.PARAMETER Repo
    Owner/repo to pull from. Defaults to GhostConan/Conan-Exiles-Shop-Improved-version.

.PARAMETER Ref
    Branch, tag or commit SHA. Defaults to master.

.PARAMETER Token
    Optional GitHub Personal Access Token. Authenticated requests get
    5000/h rate limit instead of 60/h — useful if you update very often.

.EXAMPLE
    .\tools\update-bot.ps1

.EXAMPLE
    .\tools\update-bot.ps1 -Ref v1.2.0

.EXAMPLE
    .\tools\update-bot.ps1 -Token ghp_xxx

.NOTES
    Run from the root of your bot install (the folder that contains
    start-bot.bat and bot/). Stop the bot first so .pyc files can be cleaned.
#>
[CmdletBinding()]
param(
    [string]$Repo  = "GhostConan/Conan-Exiles-Shop-Improved-version",
    [string]$Ref   = "master",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path "bot")) {
    throw "bot/ directory not found. Run this script from the root of your install."
}

$installRoot = (Get-Location).Path
$headers = @{ "User-Agent" = "conan-shop-updater" }
if ($Token) {
    $headers["Authorization"] = "Bearer $Token"
}

$tmpRoot = Join-Path $env:TEMP ("conan-shop-update-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpRoot | Out-Null
$zipPath = Join-Path $tmpRoot "repo.zip"

Write-Host ""
Write-Host "Conan Shop Bot updater" -ForegroundColor Cyan
Write-Host "  repo: $Repo" -ForegroundColor Cyan
Write-Host "  ref:  $Ref" -ForegroundColor Cyan
Write-Host ""

# 1. Download the whole repo as one zip
$zipUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Ref"
Write-Host "Downloading $zipUrl ..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri $zipUrl -Headers $headers -OutFile $zipPath
} catch {
    # Fall back to API zipball endpoint (works for tags/SHAs too)
    Write-Host "  codeload failed, trying API zipball..." -ForegroundColor DarkGray
    $apiZip = "https://api.github.com/repos/$Repo/zipball/$Ref"
    Invoke-WebRequest -Uri $apiZip -Headers $headers -OutFile $zipPath
}

# 2. Extract
Write-Host "Extracting..." -ForegroundColor Yellow
Expand-Archive -Path $zipPath -DestinationPath $tmpRoot -Force
$extracted = Get-ChildItem -Path $tmpRoot -Directory | Where-Object { $_.Name -ne $tmpRoot } | Select-Object -First 1
if (-not $extracted) {
    throw "Could not find extracted repo root inside $tmpRoot"
}
$srcRoot = $extracted.FullName

# 3. Mirror bot/ and tools/, plus selected root files
$changed = 0
function Copy-IfChanged {
    param([string]$Src, [string]$Dest)
    $bytes = [IO.File]::ReadAllBytes($Src)
    $write = $true
    if (Test-Path $Dest) {
        $existing = [IO.File]::ReadAllBytes($Dest)
        if ($existing.Length -eq $bytes.Length -and [Linq.Enumerable]::SequenceEqual($existing, $bytes)) {
            $write = $false
        }
    }
    if ($write) {
        $destDir = Split-Path $Dest -Parent
        if ($destDir -and -not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        }
        [IO.File]::WriteAllBytes($Dest, $bytes)
        $rel = $Dest.Substring($installRoot.Length).TrimStart('\','/')
        Write-Host "  updated $rel" -ForegroundColor Green
        return 1
    }
    return 0
}

Write-Host "Syncing bot/ and tools/ trees..." -ForegroundColor Yellow
foreach ($sub in @("bot", "tools")) {
    $srcSub = Join-Path $srcRoot $sub
    if (-not (Test-Path $srcSub)) { continue }
    Get-ChildItem -Path $srcSub -Recurse -File | ForEach-Object {
        $rel  = $_.FullName.Substring($srcRoot.Length).TrimStart('\','/')
        $dest = Join-Path $installRoot $rel
        $changed += Copy-IfChanged -Src $_.FullName -Dest $dest
    }
}

Write-Host "Syncing selected root files..." -ForegroundColor Yellow
$rootFiles = @("README.md", ".env.example", "requirements.txt", "setup_db.py",
               "watchdog.py", "Dockerfile", "docker-compose.yml", "conan-shop.service")
foreach ($f in $rootFiles) {
    $src = Join-Path $srcRoot $f
    if (Test-Path $src) {
        $dest = Join-Path $installRoot $f
        $changed += Copy-IfChanged -Src $src -Dest $dest
    }
}

# 4. Clear bytecode cache
Write-Host "Clearing Python bytecode cache..." -ForegroundColor Yellow
Get-ChildItem -Path (Join-Path $installRoot "bot") -Recurse -Include "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 5. Clean up the temp zip + extracted tree
Remove-Item -Path $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($changed -eq 0) {
    Write-Host "Already up to date." -ForegroundColor Green
} else {
    Write-Host "$changed file(s) updated." -ForegroundColor Green
    Write-Host "Restart the bot with: .\start-bot.bat" -ForegroundColor Cyan
}
