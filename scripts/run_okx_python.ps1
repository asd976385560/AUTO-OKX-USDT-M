# OKX Python wrapper
# Keeps Python runtime environment local to OKX jobs without changing global env.
# Usage:
#   pwsh -NoProfile -File E:\OKX\scripts\run_okx_python.ps1 E:\OKX\scripts\collect_data.py --profile live --db-root E:\OKX\db

$ErrorActionPreference = 'Stop'

if ($args.Count -lt 1) {
    Write-Error "Usage: run_okx_python.ps1 <python-script-or-args> [args...]"
    exit 64
}

$pythonHome = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314'
$okxSitePackages = 'E:\OKX\Lib\site-packages'
$okxRoot = 'E:\OKX'

if (-not (Test-Path (Join-Path $pythonHome 'Lib'))) {
    Write-Error "Python home is invalid: $pythonHome"
    exit 65
}

if (-not (Test-Path $okxSitePackages)) {
    Write-Error "OKX site-packages not found: $okxSitePackages"
    exit 66
}

$env:PYTHONHOME = $pythonHome
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $okxSitePackages
} elseif (($env:PYTHONPATH -split ';') -notcontains $okxSitePackages) {
    $env:PYTHONPATH = "$okxSitePackages;$env:PYTHONPATH"
}

if (Test-Path $okxRoot) {
    Set-Location $okxRoot
}

& py -3.14 @args
exit $LASTEXITCODE
