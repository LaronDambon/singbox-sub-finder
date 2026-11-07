[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$venvPython = Join-Path (Split-Path -Parent $here) '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host ''
    Write-Host 'Virtual environment .venv not found.' -ForegroundColor Yellow
    Write-Host 'Run the installer first:  .\install.ps1'
    exit 1
}

$cliArgs = @("$here\setup.py")
if ($Yes) { $cliArgs += '--yes' }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

& $venvPython @cliArgs
exit $LASTEXITCODE
