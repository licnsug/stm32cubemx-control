#Requires -Version 5.1
<#
.SYNOPSIS
    Installs the STM32CubeMX Control plugin for Codex, at a location you choose.

.DESCRIPTION
    Copies the plugin to the directory you pick, registers it with Codex
    (personal marketplace entry plus an MCP server table in config.toml), then
    installs and verifies it. Every path it touches is printed at the end,
    along with how to undo.

    The personal marketplace resolves its entries relative to the marketplace
    root, so the plugin must also be visible at <home>\plugins\stm32cubemx-control.
    When you install somewhere else, a directory junction is created there so
    both views point at the same files.

.PARAMETER InstallDir
    Where to install. If omitted you are prompted, with a default offered.

.PARAMETER HomeDir
    The user profile to register against. Defaults to the current user profile.
    Point it somewhere else to test an install without touching your own setup.

.PARAMETER Force
    Answer yes to prompts. Useful for scripts and repeat runs.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -InstallDir D:\tools\stm32cubemx-control -Force
    .\install.ps1 -HomeDir E:\sandbox -InstallDir E:\sandbox\wherever -Force
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$HomeDir,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$PluginName = 'stm32cubemx-control'
$MarketplaceName = 'personal'

function Write-Step($text) { Write-Host ''; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Info($text) { Write-Host "    $text" }
function Write-Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Stop-Install($text) { Write-Host ''; Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

function Write-Utf8NoBom($path, $text) {
    $dir = Split-Path -Parent $path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Find-Python {
    $candidates = @()
    if ($env:CUBEMX_TOOLKIT_PYTHON) { $candidates += $env:CUBEMX_TOOLKIT_PYTHON }
    $candidates += @('python', 'python3', 'py')
    foreach ($candidate in $candidates) {
        try {
            if ($candidate -eq 'py') {
                $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
            }
            else {
                $exe = & $candidate -c "import sys; print(sys.executable)" 2>$null
            }
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $version = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and $version) {
                    $parts = $version.Trim().Split('.')
                    if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 8)) {
                        return $exe.Trim()
                    }
                }
            }
        }
        catch { }
    }
    return $null
}

# ---------------------------------------------------------------- preflight

Write-Step 'Checking the source and your tools'

if (-not $HomeDir) { $HomeDir = $env:USERPROFILE }
$HomeDir = [System.IO.Path]::GetFullPath($HomeDir)
$isOwnProfile = ($HomeDir.TrimEnd('\') -eq ([System.IO.Path]::GetFullPath($env:USERPROFILE)).TrimEnd('\'))

$sourceRoot = Join-Path $PSScriptRoot "plugins\$PluginName"
if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot '.codex-plugin\plugin.json'))) {
    Stop-Install "plugin sources not found at $sourceRoot. Run this script from the repository root."
}
Write-Ok "plugin sources: $sourceRoot"
if (-not $isOwnProfile) { Write-Warn "registering against $HomeDir instead of your own profile" }

$python = Find-Python
if (-not $python) {
    Stop-Install 'Python 3.8 or newer was not found. Install it from python.org, or set CUBEMX_TOOLKIT_PYTHON to the interpreter path.'
}
Write-Ok "python: $python"

$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) { Write-Ok "codex CLI: $($codex.Source)" }
else { Write-Warn 'codex CLI not on PATH; files will be installed but not registered.' }

# ------------------------------------------------------------------- target

Write-Step 'Choosing where to install'

$defaultDir = Join-Path $HomeDir "plugins\$PluginName"
if (-not $InstallDir) {
    Write-Info 'Press Enter to accept the default.'
    $answer = Read-Host "Install location [$defaultDir]"
    if ([string]::IsNullOrWhiteSpace($answer)) { $InstallDir = $defaultDir }
    else { $InstallDir = $answer.Trim().Trim('"') }
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

if (Test-Path -LiteralPath $InstallDir) {
    $existing = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0 -and -not $Force) {
        Write-Warn "$InstallDir already exists and is not empty."
        $answer = Read-Host 'Overwrite the existing installation? [y/N]'
        if ($answer -notmatch '^[Yy]') { Stop-Install 'aborted, nothing was changed.' }
    }
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Write-Ok "target: $InstallDir"

# --------------------------------------------------------------------- copy

Write-Step 'Copying files'

& robocopy $sourceRoot $InstallDir /E /R:1 /W:1 /NFL /NDL /NJH /NP /XD __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { Stop-Install "copy failed (robocopy exit code $LASTEXITCODE)." }
$installedScript = Join-Path $InstallDir 'scripts\cubemx.py'
if (-not (Test-Path -LiteralPath $installedScript)) {
    Stop-Install "copy finished but $installedScript is missing."
}
Write-Ok "copied to $InstallDir"

# ------------------------------------------------- marketplace visibility

Write-Step 'Making the plugin visible to the personal marketplace'

$linkPath = Join-Path $HomeDir "plugins\$PluginName"
$linkState = 'not needed'
if ([System.IO.Path]::GetFullPath($linkPath) -ne $InstallDir) {
    if (Test-Path -LiteralPath $linkPath) {
        $item = Get-Item -LiteralPath $linkPath -Force
        $existingTarget = $null
        if ($item.LinkType -and $item.Target) { $existingTarget = @($item.Target)[0] }
        if ($existingTarget) {
            if ([System.IO.Path]::GetFullPath($existingTarget) -eq $InstallDir) {
                $linkState = 'already points here'
            }
            else {
                # Remove the link itself, never the directory it points at.
                [System.IO.Directory]::Delete($linkPath, $false)
                New-Item -ItemType Junction -Path $linkPath -Target $InstallDir | Out-Null
                $linkState = "repointed from $existingTarget"
            }
        }
        else {
            Stop-Install "$linkPath is a real directory, not a link. Install there instead, or move it aside first."
        }
    }
    else {
        try {
            New-Item -ItemType Junction -Path $linkPath -Target $InstallDir | Out-Null
            $linkState = 'junction created'
        }
        catch {
            Stop-Install "could not create the junction at $linkPath ($($_.Exception.Message)). The personal marketplace resolves entries relative to $HomeDir, so install to the default location instead."
        }
    }
}
Write-Ok "$linkPath : $linkState"

# -------------------------------------------------- marketplace registration

Write-Step 'Registering the plugin with Codex'

$marketplacePath = Join-Path $HomeDir ".agents\plugins\marketplace.json"
$entry = [pscustomobject][ordered]@{
    name     = $PluginName
    source   = [pscustomobject][ordered]@{ source = 'local'; path = "./plugins/$PluginName" }
    policy   = [pscustomobject][ordered]@{ installation = 'AVAILABLE'; authentication = 'ON_INSTALL' }
    category = 'Developer Tools'
}

if (Test-Path -LiteralPath $marketplacePath) {
    $payload = [System.IO.File]::ReadAllText($marketplacePath) | ConvertFrom-Json
    if (-not $payload.PSObject.Properties['plugins']) {
        $payload | Add-Member -NotePropertyName plugins -NotePropertyValue @()
    }
    $kept = @($payload.plugins | Where-Object { $_.name -ne $PluginName })
    $payload.plugins = @($kept + $entry)
}
else {
    $payload = [pscustomobject][ordered]@{
        name      = $MarketplaceName
        interface = [pscustomobject][ordered]@{ displayName = 'Personal' }
        plugins   = @($entry)
    }
}
Write-Utf8NoBom $marketplacePath ($payload | ConvertTo-Json -Depth 10)
Write-Ok "marketplace entry: $marketplacePath"

# ----------------------------------------------------- MCP server in config

$configPath = Join-Path $HomeDir '.codex\config.toml'
$mcpScript = Join-Path $InstallDir 'scripts\cubemx_mcp.py'
$block = @(
    "[mcp_servers.$PluginName]"
    "command = '$python'"
    "args = ['$mcpScript']"
    'enabled = true'
)

$lines = @()
if (Test-Path -LiteralPath $configPath) {
    $lines = [System.IO.File]::ReadAllText($configPath) -split "\r?\n"
}

$startIndex = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i].Trim() -eq "[mcp_servers.$PluginName]") { $startIndex = $i; break }
}
if ($startIndex -ge 0) {
    $endIndex = $lines.Count
    for ($j = $startIndex + 1; $j -lt $lines.Count; $j++) {
        if ($lines[$j].TrimStart().StartsWith('[')) { $endIndex = $j; break }
    }
    $merged = @()
    if ($startIndex -gt 0) { $merged += $lines[0..($startIndex - 1)] }
    $merged += $block
    if ($endIndex -lt $lines.Count) { $merged += $lines[$endIndex..($lines.Count - 1)] }
    $lines = $merged
    $mcpAction = 'updated'
}
else {
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne '') { $lines += '' }
    $lines += $block
    $mcpAction = 'added'
}
Write-Utf8NoBom $configPath ($lines -join [Environment]::NewLine)
Write-Ok "MCP server $mcpAction in $configPath"

# ------------------------------------------------------------------ install

Write-Step 'Installing into Codex'

$installedOk = $false
if (-not $isOwnProfile) {
    Write-Warn "skipped: a sandboxed HomeDir cannot drive the real Codex app. Run codex plugin add $PluginName@$MarketplaceName yourself."
}
elseif (-not $codex) {
    Write-Warn "codex is not on PATH. Run it yourself later: codex plugin add $PluginName@$MarketplaceName"
}
else {
    & codex plugin add "$PluginName@$MarketplaceName"
    if ($LASTEXITCODE -eq 0) {
        $installedOk = $true
        Write-Ok 'codex plugin add finished'
    }
    else {
        Write-Warn "codex plugin add failed (exit $LASTEXITCODE). Run it yourself later: codex plugin add $PluginName@$MarketplaceName"
    }
}

# ------------------------------------------------------------------- verify

Write-Step 'Verifying'

Write-Info "running: $python $installedScript env"
& $python $installedScript env
if ($LASTEXITCODE -eq 0) { Write-Ok 'the toolkit runs from the installed location' }
else { Write-Warn 'the toolkit did not run; see the output above' }

# ------------------------------------------------------------------ summary

Write-Step 'Done'
Write-Info "plugin files : $InstallDir"
Write-Info "marketplace  : $marketplacePath"
Write-Info "mcp server   : $configPath  ([mcp_servers.$PluginName])"
if ($installedOk) { Write-Info 'plugin       : installed and enabled' }
Write-Host ''
Write-Host 'Next steps' -ForegroundColor Cyan
Write-Info '1. Restart the Codex app so it picks up the new MCP server.'
Write-Info '2. Start a new conversation; the skill and the cubemx_* tools load there.'
Write-Host ''
Write-Host 'To undo' -ForegroundColor Cyan
Write-Info "  codex plugin remove $PluginName@$MarketplaceName"
Write-Info "  remove the $PluginName entry from $marketplacePath"
Write-Info "  remove the [mcp_servers.$PluginName] table from $configPath"
Write-Info "  delete $InstallDir"
if ($linkState -ne 'not needed') { Write-Info "  delete the junction at $linkPath" }
