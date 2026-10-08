# Install Intravo Messenger on Windows. Forwards every argument to `ivm install`.
# A pair file passed with --secret-file is read by the program and is not printed.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Env:PYTHONPATH = Join-Path $Root "src"
$Env:PYTHONUTF8 = "1"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m intravo_messenger install @args
    exit $LASTEXITCODE
}
& python -m intravo_messenger install @args
exit $LASTEXITCODE
