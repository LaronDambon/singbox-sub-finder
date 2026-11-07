$ErrorActionPreference = 'Stop'

if ($env:SINGBOX_REPO)   { $repo   = $env:SINGBOX_REPO }   else { $repo   = 'https://github.com/LaronDambon/singbox-sub-finder.git' }
if ($env:SINGBOX_BRANCH) { $branch = $env:SINGBOX_BRANCH } else { $branch = 'main' }

function Say  { param($m) Write-Host $m -ForegroundColor Cyan }
function Warn { param($m) Write-Host $m -ForegroundColor Yellow }
function Die  { param($m) Write-Host $m -ForegroundColor Red; exit 1 }

Say 'Installing singbox-subs-finder'
Write-Host ''

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die 'git not found. Install: https://git-scm.com/download/win'
}

function Test-Python {
    param($Exe, $ExeArgs)
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    # Native stderr must not become a terminating error under Stop: the
    # Microsoft Store stub writes to stderr and exits 9009, and without this
    # guard the whole script dies with an empty "Python" message.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $probe = & $Exe @($ExeArgs + @('-c', 'import sys; print(sys.version_info[0])')) 2>$null
        return ($LASTEXITCODE -eq 0 -and "$probe".Trim() -eq '3')
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

$pythonExe  = $null
$pythonArgs = @()
foreach ($c in @(@{Exe='py'; Args=@('-3')}, @{Exe='python'; Args=@()}, @{Exe='python3'; Args=@()})) {
    if (Test-Python $c.Exe $c.Args) {
        $pythonExe  = $c.Exe
        $pythonArgs = $c.Args
        break
    }
}

if (-not $pythonExe) {
    Die 'Python 3 not found. Install: https://www.python.org/downloads/'
}

if ($env:SINGBOX_PROGRAM_DIR) {
    $dir = $env:SINGBOX_PROGRAM_DIR
} elseif ($env:SINGBOX_DIR) {
    $dir = $env:SINGBOX_DIR
} else {
    if ($env:ProgramFiles) { $base = $env:ProgramFiles } else { $base = 'C:\Program Files' }
    $dir = Join-Path $base 'singbox-subs-finder'
}

if ($env:SINGBOX_DATA_DIR) {
    $data = $env:SINGBOX_DATA_DIR
} else {
    if ($env:LOCALAPPDATA) { $data = Join-Path $env:LOCALAPPDATA 'singbox-subs-finder' }
    else { $data = Join-Path $env:USERPROFILE 'singbox-subs-finder' }
}

$pyVersion = "$(& $pythonExe @($pythonArgs + @('--version')) 2>&1)"

Write-Host "  Python:    $pyVersion"
Write-Host "  git:       $(git --version)"
Write-Host "  program:   $dir"
Write-Host "  data:      $data"
Write-Host ''

if (Test-Path $dir) {
    $existing = Get-ChildItem -Path $dir -Force -ErrorAction SilentlyContinue
    if ($existing -and -not (Test-Path (Join-Path $dir '.git'))) {
        Warn "$dir is not empty and is not a git clone - using the user profile instead"
        if ($env:LOCALAPPDATA) { $dir = Join-Path $env:LOCALAPPDATA 'singbox-subs-finder' }
        else { $dir = Join-Path $env:USERPROFILE 'singbox-subs-finder' }
    }
}

try {
    New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
    $probeFile = Join-Path $dir '.write_probe'
    [System.IO.File]::WriteAllText($probeFile, '')
    Remove-Item $probeFile -Force
} catch {
    Warn "Cannot write to $dir"
    Die 'Run PowerShell as Administrator (required to install into Program Files)'
}

$clone = $true
if (Test-Path (Join-Path $dir '.git')) {
    Say 'Directory already exists - updating'
    $clone = $false
}

# git writes progress to stderr, which becomes a terminating error under Stop.
# Run it with Continue and check the exit code explicitly.
$ErrorActionPreference = 'Continue'

if ($clone) {
    Say 'Downloading repository'
    & git clone --depth 1 --branch $branch $repo $dir 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Die "git clone failed (exit $LASTEXITCODE)"
    }
} else {
    & git -C $dir fetch --depth 1 origin $branch 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Die "git fetch failed (exit $LASTEXITCODE)" }
    & git -C $dir checkout $branch 2>&1 | Out-Null
    & git -C $dir reset --hard "origin/$branch" 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Die "git reset failed (exit $LASTEXITCODE)" }
}

$ErrorActionPreference = 'Stop'

Say 'Running installer'
Write-Host ''

$installer = Join-Path $dir 'singbox-subscribe/install.ps1'
if (-not (Test-Path $installer)) {
    Die ('install.ps1 not found in branch ' + $branch)
}

$env:SINGBOX_PROGRAM_DIR = $dir
$env:SINGBOX_DATA_DIR = $data

& $installer @args
exit $LASTEXITCODE
