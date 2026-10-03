# (Re)creates the Windows Task Scheduler job from a known-good definition.
#   powershell -ExecutionPolicy Bypass -File scheduler\install_task.ps1 [-IntervalMinutes 15]
#
# Why XML instead of `schtasks /Create ... /SC MINUTE`: the shortcut cannot set the options that
# matter for a laptop-hosted collector. The portal only exposes each station's latest value, so a
# missed poll is lost for good. Defaults the shortcut leaves on were silently skipping ~2/3 of the time:
#   DisallowStartIfOnBatteries / StopIfGoingOnBatteries  -> OFF here (run on battery)
#   StartWhenAvailable                                   -> ON  (catch up one run after sleep)
#   ExecutionTimeLimit                                   -> 5 min (default is 72 h)
# Not enabled: WakeToRun. A sleeping laptop still misses polls; host this on an always-on machine
# if continuous coverage matters.
param([int]$IntervalMinutes = 15, [string]$TaskName = "UgandaWeatherScraper")

$root  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$start = (Get-Date).AddMinutes(2).ToString("yyyy-MM-ddTHH:mm:ss")
$user  = "$env:USERDOMAIN\$env:USERNAME"

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Polls the Adcon LiveData portal (Uganda AWS network) every $IntervalMinutes min into $root\data. Runs on battery, catches up after sleep.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT${IntervalMinutes}M</Interval></Repetition>
    </TimeTrigger>
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
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$root\run.ps1"</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
$s = (Get-ScheduledTask -TaskName $TaskName).Settings
"Registered '$TaskName': every $IntervalMinutes min | battery-safe=$(-not $s.DisallowStartIfOnBatteries) | catch-up=$($s.StartWhenAvailable) | limit=$($s.ExecutionTimeLimit)"
