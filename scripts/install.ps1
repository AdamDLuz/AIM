# Install AIM on Windows. Forwards every argument to `aim install`.
# A pair file passed with --secret-file is read by the program and is not printed.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Env:PYTHONPATH = Join-Path $Root "src"
$Env:PYTHONUTF8 = "1"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m aim install @args
    exit $LASTEXITCODE
}
& python -m aim install @args
exit $LASTEXITCODE
