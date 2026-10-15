param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Uninstall','Start','Disable')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Project
)
$ErrorActionPreference = 'Stop'
$taskPrefix = 'RAG-Local-'
if ($Action -eq 'Uninstall') {
    foreach ($taskName in @('Supervisor', 'Backup')) {
        Unregister-ScheduledTask -TaskName ($taskPrefix + $taskName) -Confirm:$false -ErrorAction SilentlyContinue
    }
    exit 0
}
if ($Action -eq 'Start') {
    Enable-ScheduledTask -TaskName ($taskPrefix + 'Supervisor') | Out-Null
    Start-ScheduledTask -TaskName ($taskPrefix + 'Supervisor')
    exit 0
}
if ($Action -eq 'Disable') {
    Disable-ScheduledTask -TaskName ($taskPrefix + 'Supervisor') | Out-Null
    exit 0
}
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$supervisorAction = New-ScheduledTaskAction -Execute $Python -Argument '-m rag.ops supervise' -WorkingDirectory $Project
$backupAction = New-ScheduledTaskAction -Execute $Python -Argument '-m rag.ops backup --due' -WorkingDirectory $Project
$login = New-ScheduledTaskTrigger -AtLogOn -User $account
# A repeated demand check also recovers exits that Task Scheduler records as a
# completed action rather than a launch failure. IgnoreNew keeps one supervisor.
$watchdog = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$daily = New-ScheduledTaskTrigger -Daily -At '03:00'
$supervisorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName ($taskPrefix + 'Supervisor') -Action $supervisorAction -Trigger @($login,$watchdog) `
    -Settings $supervisorSettings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName ($taskPrefix + 'Backup') -Action $backupAction -Trigger $daily `
    -Settings $backupSettings -Principal $principal -Force | Out-Null
Enable-ScheduledTask -TaskName ($taskPrefix + 'Supervisor') | Out-Null
Enable-ScheduledTask -TaskName ($taskPrefix + 'Backup') | Out-Null
