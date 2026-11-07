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
    [switch]$Browser,
    [switch]$Upgrade,
    [switch]$RecreateVenv,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- поиск Python -----------------------------------------------------------
# py -3 предпочтительнее: он находит последнюю установленную версию и не
# зависит от того, что лежит в PATH под именем python (часто это заглушка
# из Microsoft Store, которая молча открывает магазин).
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
    Write-Host 'Python 3 не найден.' -ForegroundColor Red
    Write-Host 'Установите его: https://www.python.org/downloads/'
    Write-Host 'При установке отметьте галочку "Add python.exe to PATH".'
    exit 1
}

$cliArgs = @("$here\installer.py", 'install')
if ($Browser)      { $cliArgs += '--browser' }
if ($Upgrade)      { $cliArgs += '--upgrade' }
if ($RecreateVenv) { $cliArgs += '--recreate-venv' }
if ($Quiet)        { $cliArgs += '--quiet' }

# PYTHONIOENCODING: без него PowerShell 5.1 срывается при чтении русского
# вывода дочернего процесса (Exception in thread Thread-N _readerthread).
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

& $python.Exe @($python.Args + $cliArgs)
exit $LASTEXITCODE
