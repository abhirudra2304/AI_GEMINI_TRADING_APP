# One-time setup: registers the 14:30 decision scan and the 20:15 nightly data job as
# Mon-Fri scheduled tasks that run whether or not you are signed in (same as Prewarm).
# Re-running it replaces the two tasks; it does not touch Prewarm or EOD.
$ErrorActionPreference = "Stop"
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$defs = @(
    @{ Name = "TradingApp_Decision"; Time = "14:30"; Script = "run_decision.ps1"; StartWhenAvailable = $false
       Desc = "Decision scan (momentum --force-refresh) + BTST/SWING/GRIND brief before the 15:15 closing auction" },
    @{ Name = "TradingApp_Nightly"; Time = "20:15"; Script = "run_nightly.ps1"; StartWhenAvailable = $true
       Desc = "NSE bhavcopy update, official-close overlay, outcome scoring" }
)
foreach ($d in $defs) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\AI_GEMINI_TRADING_APP\scripts\$($d.Script)"
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $d.Time
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    $settings.StartWhenAvailable = $d.StartWhenAvailable
    Register-ScheduledTask -TaskName $d.Name -Description $d.Desc -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    $info = Get-ScheduledTask -TaskName $d.Name | Get-ScheduledTaskInfo
    Write-Output "$($d.Name) registered, next run $($info.NextRunTime)"
}
