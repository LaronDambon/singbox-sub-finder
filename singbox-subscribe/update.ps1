<#
.SYNOPSIS
    ОБЁРТКА. Вся логика — в installer.py рядом с этим файлом.

.DESCRIPTION
    Файл намеренно тонкий: находит Python, передаёт аргументы и зовёт
    модуль. Так установка, обновление и мастер на Windows и Linux ведут
    себя одинаково, и шаг достаточно починить в одном месте.
#>

[CmdletBinding()]
param(
    [switch]$Force,
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
    Write-Host 'Python 3 не найден: https://www.python.org/downloads/' -ForegroundColor Red
    exit 1
}

$cliArgs = @("$here\installer.py", 'update')
if ($Force) { $cliArgs += '--force' }
if ($Quiet) { $cliArgs += '--quiet' }

# PYTHONIOENCODING: без него PowerShell 5.1 срывается при чтении русского
# вывода дочернего процесса (Exception in thread Thread-N _readerthread).
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

& $python.Exe @($python.Args + $cliArgs)
exit $LASTEXITCODE
