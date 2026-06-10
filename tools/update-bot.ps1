<#
.SYNOPSIS
    Updates an existing Conan Exiles Shop Bot install from GitHub master.

.DESCRIPTION
    Fetches the latest version of every Python source file in the bot/
    directory via the GitHub Contents API (which always returns live
    content, unlike raw.githubusercontent.com which caches for ~5 min),
    clears Python bytecode caches, and prints a diff summary.

    Does NOT touch your .env file, MariaDB data, or anything outside
    bot/, README.md, and .env.example.

.PARAMETER Repo
    Owner/repo to pull from. Defaults to GhostConan/Conan-Exiles-Shop-Improved-version.

.PARAMETER Ref
    Branch, tag or commit SHA. Defaults to master.

.EXAMPLE
    .\tools\update-bot.ps1
    # Pull every changed file from master into the current install.

.EXAMPLE
    .\tools\update-bot.ps1 -Ref v1.2.0
    # Pin to a specific tag.

.NOTES
    Run from the root of your bot install (the folder that contains
    start-bot.bat and the bot/ directory). Stop the bot before running
    so .pyc files can be cleaned safely.
#>
[CmdletBinding()]
param(
    [string]$Repo = "GhostConan/Conan-Exiles-Shop-Improved-version",
    [string]$Ref  = "master"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path "bot")) {
    throw "bot/ directory not found. Run this script from the root of your install."
}

$apiBase = "https://api.github.com/repos/$Repo/contents"
$headers = @{ "User-Agent" = "conan-shop-updater" }

function Get-RemoteTree {
    param([string]$Path)
    Write-Host "  scanning $Path ..." -ForegroundColor DarkGray
    $url = "$apiBase/$Path`?ref=$Ref"
    $items = Invoke-RestMethod -Uri $url -Headers $headers
    foreach ($item in $items) {
        if ($item.type -eq "file") {
            $item
        } elseif ($item.type -eq "dir") {
            Get-RemoteTree -Path $item.path
        }
    }
}

function Update-File {
    param([Parameter(Mandatory)] $RemoteItem)
    $localPath = $RemoteItem.path -replace '/', '\'
    $localDir  = Split-Path $localPath -Parent
    if ($localDir -and -not (Test-Path $localDir)) {
        New-Item -ItemType Directory -Force -Path $localDir | Out-Null
    }
    $resp = Invoke-RestMethod -Uri $RemoteItem.url -Headers $headers
    $bytes = [Convert]::FromBase64String($resp.content)

    $changed = $true
    if (Test-Path $localPath) {
        $existing = [IO.File]::ReadAllBytes($localPath)
        if ($existing.Length -eq $bytes.Length) {
            $changed = -not ([Linq.Enumerable]::SequenceEqual($existing, $bytes))
        }
    }

    if ($changed) {
        [IO.File]::WriteAllBytes($localPath, $bytes)
        Write-Host "  updated $localPath" -ForegroundColor Green
        return 1
    } else {
        return 0
    }
}

Write-Host ""
Write-Host "Conan Shop Bot updater" -ForegroundColor Cyan
Write-Host "  repo: $Repo" -ForegroundColor Cyan
Write-Host "  ref:  $Ref" -ForegroundColor Cyan
Write-Host ""

$pathsToSync = @("bot")
$rootFiles   = @("README.md", ".env.example", "requirements.txt", "setup_db.py",
                 "watchdog.py", "Dockerfile", "docker-compose.yml")

$changed = 0

Write-Host "Walking remote bot/ tree..." -ForegroundColor Yellow
foreach ($p in $pathsToSync) {
    foreach ($item in Get-RemoteTree -Path $p) {
        $changed += Update-File -RemoteItem $item
    }
}

Write-Host ""
Write-Host "Syncing root files..." -ForegroundColor Yellow
foreach ($f in $rootFiles) {
    try {
        $resp = Invoke-RestMethod -Uri "$apiBase/$f`?ref=$Ref" -Headers $headers
        $changed += Update-File -RemoteItem $resp
    } catch {
        Write-Host "  skipped $f (not in repo)" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Clearing Python bytecode cache..." -ForegroundColor Yellow
Get-ChildItem -Path "bot" -Recurse -Include "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($changed -eq 0) {
    Write-Host "Already up to date." -ForegroundColor Green
} else {
    Write-Host "$changed file(s) updated." -ForegroundColor Green
    Write-Host "Restart the bot with: .\start-bot.bat" -ForegroundColor Cyan
}
