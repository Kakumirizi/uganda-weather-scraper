# Wrapper for Windows Task Scheduler. Runs one scrape, logs the outcome.
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\user\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
$script = Join-Path $root "weather_scraper.py"
$runlog = Join-Path $root "logs\run.log"

$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
try {
    $out = & $python $script 2>&1
    $code = $LASTEXITCODE
    "$stamp  exit=$code  $out" | Out-File -FilePath $runlog -Append -Encoding utf8
    exit $code
}
catch {
    "$stamp  exit=99  WRAPPER ERROR: $_" | Out-File -FilePath $runlog -Append -Encoding utf8
    exit 99
}
