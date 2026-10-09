# Build Card Peek for Windows as a single standalone program, dist\CardPeek-<version>.exe,
# and self-test it as built.
#
#   pip install -r packaging\requirements-windows.txt
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Environment: PYTHON, the Python to build with (default: python).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Check($what) {
    if ($LASTEXITCODE) { throw "$what failed (exit code $LASTEXITCODE)" }
}

$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$version = (& $python -c "import cardpeek; print(cardpeek.__version__)").Trim()
Check "Reading the version"
$exe = "dist\CardPeek-$version.exe"

Write-Host "==> Building $exe"
Remove-Item -Recurse -Force build\windows, $exe -ErrorAction SilentlyContinue
& $python -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build\windows packaging\CardPeek-windows.spec
Check "PyInstaller"

Write-Host "==> Self-test of the built app"
# Piping the output makes PowerShell wait for the app (a GUI program) and hands it a stdout.
& $exe --self-test | Out-Host
Check "The self-test"

Write-Host ("==> {0}: {1:N0} MB" -f $exe, ((Get-Item $exe).Length / 1MB))
