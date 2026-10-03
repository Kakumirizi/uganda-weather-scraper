# (Re)creates the Windows Task Scheduler jobs from known-good definitions.
#   powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1                  # scraper, every 15 min
#   powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1 -Publish         # daily data export + push
#   ... [-IntervalMinutes 15] [-PublishTime 13:00]
#
# Why XML instead of `schtasks /Create ... /SC MINUTE`: the shortcut cannot set the options that
# matter for a laptop-hosted collector. The portal only exposes each station's latest value, so a
# missed poll is lost for good. Defaults the shortcut leaves on were silently skipping ~2/3 of the time:
#   DisallowStartIfOnBatteries / StopIfGoingOnBatteries  -> OFF here (run on battery)
#   StartWhenAvailable                                   -> ON  (catch up one run after sleep)
#   ExecutionTimeLimit                                   -> 5 min scrape / 15 min publish (default is 72 h)
# Not enabled: WakeToRun. A sleeping laptop still misses polls; host this on an always-on machine
# if continuous coverage matters.
param([int]$IntervalMinutes = 15, [switch]$Publish, [string]$PublishTime = "13:00", [string]$TaskName = "")

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($Publish) {
    if (-not $TaskName) { $TaskName = "UgandaWeatherPublish" }
    $desc    = "Daily: export weather.db to CSV, commit data/exports + data/stations.csv, push to GitHub ($root)."
    $start   = (Get-Date).Date.AddHours([int]$PublishTime.Split(':')[0]).AddMinutes([int]$PublishTime.Split(':')[1]).ToString("yyyy-MM-ddTHH:mm:ss")
    $trigger = "<CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>"
    $args_   = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run.ps1`" -Target publish_data.py -LogName publish.log"
    $limit   = "PT15M"
    $summary = "daily at $PublishTime"
} else {
    if (-not $TaskName) { $TaskName = "UgandaWeatherScraper" }
    $desc    = "Polls the Adcon LiveData portal (Uganda AWS network) every $IntervalMinutes min into $root\data. Runs on battery, catches up after sleep."
    $start   = (Get-Date).AddMinutes(2).ToString("yyyy-MM-ddTHH:mm:ss")
    $trigger = "<TimeTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><Repetition><Interval>PT${IntervalMinutes}M</Interval></Repetition></TimeTrigger>"
    $args_   = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run.ps1`""
    $limit   = "PT5M"
    $summary = "every $IntervalMinutes min"
}

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$desc</Description>
  </RegistrationInfo>
  <Triggers>
    $trigger
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>$limit</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$args_</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
$s = (Get-ScheduledTask -TaskName $TaskName).Settings
"Registered '$TaskName': $summary | battery-safe=$(-not $s.DisallowStartIfOnBatteries) | catch-up=$($s.StartWhenAvailable) | limit=$($s.ExecutionTimeLimit)"
