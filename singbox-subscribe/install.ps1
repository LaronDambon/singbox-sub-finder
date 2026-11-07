[CmdletBinding()]
param(
    [switch]$Browser,
    [switch]$Upgrade,
    [switch]$RecreateVenv,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $candidates = @(
        @{ Exe = 'py';      Args = @('-3') },
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $probe = & $c.Exe @($c.Args + @('-c', 'import sys; print(sys.version_info[0])')) 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe -eq '3') {
            return @{ Exe = $c.Exe; Args = $c.Args }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host ''
    Write-Host 'Python 3 not found.' -ForegroundColor Red
    Write-Host 'Install it: https://www.python.org/downloads/'
    Write-Host 'During setup check "Add python.exe to PATH".'
    exit 1
}

$cliArgs = @("$here\installer.py", 'install')
if ($Browser)      { $cliArgs += '--browser' }
if ($Upgrade)      { $cliArgs += '--upgrade' }
if ($RecreateVenv) { $cliArgs += '--recreate-venv' }
if ($Quiet)        { $cliArgs += '--quiet' }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

& $python.Exe @($python.Args + $cliArgs)
exit $LASTEXITCODE
