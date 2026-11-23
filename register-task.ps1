# Registers the nightly "Claude Session Backup" task (2:00 AM, runs as you, only while logged on).
# -StartWhenAvailable: if the laptop was asleep/off at 2 AM, it runs at the next opportunity.
$script = Join-Path $PSScriptRoot 'sync-sessions.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 2:00am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'Claude Session Backup' -Force `
    -Description 'Nightly rsync of selected ~/.claude/projects folders to the GCS bucket in sync-config.json' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null

Get-ScheduledTask -TaskName 'Claude Session Backup' | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime | Format-List
