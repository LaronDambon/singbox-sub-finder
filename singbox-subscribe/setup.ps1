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
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Мастеру нужен интерпретатор ИЗ VENV: он импортирует config/config.py, а
# зависимости (PyYAML, ruamel.yaml) стоят именно туда. Системный Python их
# не увидит.
$venvPython = Join-Path (Split-Path -Parent $here) '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host ''
    Write-Host 'Окружение .venv не найдено.' -ForegroundColor Yellow
    Write-Host 'Сначала выполните установку:  .\install.ps1'
    exit 1
}

$cliArgs = @("$here\setup.py")
if ($Yes) { $cliArgs += '--yes' }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

& $venvPython @cliArgs
exit $LASTEXITCODE
