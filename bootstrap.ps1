$ErrorActionPreference = 'Stop'

if ($env:SINGBOX_REPO)   { $repo   = $env:SINGBOX_REPO }   else { $repo   = 'https://github.com/LaronDambon/singbox-sub-finder.git' }
if ($env:SINGBOX_BRANCH) { $branch = $env:SINGBOX_BRANCH } else { $branch = 'main' }
if ($env:SINGBOX_DIR)    { $dir    = $env:SINGBOX_DIR }    else { $dir    = Join-Path (Get-Location) 'singbox-subs-finder' }

function Say  { param($m) Write-Host $m -ForegroundColor Cyan }
function Warn { param($m) Write-Host $m -ForegroundColor Yellow }
function Die  { param($m) Write-Host $m -ForegroundColor Red; exit 1 }

Say 'Установка singbox-subs-finder'
Write-Host ''

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die 'git не найден. Установите: https://git-scm.com/download/win'
}

$pythonExe  = $null
$pythonArgs = @()
foreach ($c in @(@{Exe='py'; Args=@('-3')}, @{Exe='python'; Args=@()}, @{Exe='python3'; Args=@()})) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    $probe = & $c.Exe @($c.Args + @('-c', 'import sys; print(sys.version_info[0])')) 2>$null
    if ($LASTEXITCODE -eq 0 -and $probe -eq '3') {
        $pythonExe  = $c.Exe
        $pythonArgs = $c.Args
        break
    }
}

if (-not $pythonExe) {
    Die 'Python 3 не найден. Установите: https://www.python.org/downloads/'
}

$pyVersion = & $pythonExe @($pythonArgs + @('--version')) 2>&1
Write-Host "  Python:  $pyVersion"
Write-Host "  git:     $(git --version)"
Write-Host "  каталог: $dir"
Write-Host ''

if (Test-Path (Join-Path $dir '.git')) {
    Say 'Каталог уже есть, обновляю'
    git -C $dir fetch --depth 1 origin $branch
    git -C $dir checkout $branch 2>$null
    $ref = 'origin/' + $branch
    git -C $dir reset --hard $ref
} else {
    Say 'Скачиваю репозиторий'
    git clone --depth 1 --branch $branch $repo $dir
}

Say 'Запускаю установку'
Write-Host ''

$installer = Join-Path $dir 'singbox-subscribe/install.ps1'
if (-not (Test-Path $installer)) {
    Die ('не найден install.ps1 в ветке ' + $branch)
}

& $installer @args
exit $LASTEXITCODE
