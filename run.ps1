# Task Scheduler wrapper: run one scrape and log the REAL exit code.
#
# Not using $ErrorActionPreference = "Stop" / 2>&1 on purpose: in Windows PowerShell 5.1 that turns
# any stderr line from a native command into a terminating error, which (a) masked the scraper's
# own exit code as 99 and (b) would flag a successful run as failed over a harmless warning.
# stdout/stderr go to files via Start-Process; only $proc.ExitCode decides success.
# Usage: run.ps1  ->  weather_scraper.py, log: logs/run.log
#                    run.ps1 -Target publish_data.py -LogName publish.log   -> daily data publish
param([string]$Target = "weather_scraper.py", [string]$LogName = "run.log")
$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root $Target
$logdir = Join-Path $root "logs"
$runlog = Join-Path $logdir $LogName
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

function Write-RunLog([string]$line) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    "$stamp  $line" | Out-File -FilePath $runlog -Append -Encoding utf8
}

# Locate Python at run time instead of hard-coding a versioned Windows Store path.
$python = $null
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) { $python = $py.Source; $pyArgs = @("-3") }
else {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; $pyArgs = @() }
}
if (-not $python) { Write-RunLog "exit=98  python not found (tried py.exe, python.exe)"; exit 98 }

$stem    = [IO.Path]::GetFileNameWithoutExtension($LogName)
$outFile = Join-Path $logdir "$stem.last_stdout.txt"
$errFile = Join-Path $logdir "$stem.last_stderr.txt"
try {
    $proc = Start-Process -FilePath $python -ArgumentList ($pyArgs + @("`"$script`"")) `
        -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $code = $proc.ExitCode
}
catch {
    Write-RunLog "exit=97  could not start python: $_"
    exit 97
}

$out = ((Get-Content $outFile -ErrorAction SilentlyContinue) -join " ").Trim()
$err = ((Get-Content $errFile -ErrorAction SilentlyContinue) -join " ").Trim()
if ($err.Length -gt 300) { $err = $err.Substring(0, 300) + "..." }
$line = "exit=$code  $out"
if ($err) { $line += "  [stderr: $err]" }
Write-RunLog $line
exit $code
