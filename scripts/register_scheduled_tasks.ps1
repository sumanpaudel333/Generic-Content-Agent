<#
.SYNOPSIS
    Registers the Automation Control scheduled jobs in Windows Task Scheduler.

.DESCRIPTION
    Two jobs:
      * BCSands-ContentAgent-Daily  -- content_seo_agent/daily_run.py, once a day
      * BCSands-ChatInsights-Weekly -- chat_insights/weekly_run.py, once a week

    Neither job has ever been scheduled automatically; both were only ever run by
    hand. Run this once (as Administrator) to put them on a schedule.

    Re-running is safe: existing tasks with the same names are replaced.

.PARAMETER DailyAt
    Time of day for the content job. Default 02:00.

.PARAMETER WeeklyAt
    Time of day for the chat job. Default 03:00 -- deliberately after the daily
    one so the two never contend for this host's two CPU cores.

.PARAMETER WeeklyOn
    Day of week for the chat job. Default Monday, so the report covers the week
    just finished.

.PARAMETER RunAsUser
    Account the tasks run as. Defaults to the current user. The account needs to
    be able to reach Ollama, Odoo and Chatbase, and needs "Log on as a batch job".

.EXAMPLE
    # From an elevated PowerShell, in the repo root:
    .\scripts\register_scheduled_tasks.ps1

.EXAMPLE
    .\scripts\register_scheduled_tasks.ps1 -DailyAt 01:30 -WeeklyOn Sunday -WeeklyAt 23:00

.NOTES
    Check afterwards with:   Get-ScheduledTask -TaskName 'BCSands-*'
    Run one immediately:     Start-ScheduledTask -TaskName 'BCSands-ChatInsights-Weekly'
    Inspect last result:     Get-ScheduledTaskInfo -TaskName 'BCSands-ChatInsights-Weekly'
    Remove:                  Unregister-ScheduledTask -TaskName 'BCSands-*' -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$DailyAt = "02:00",
    [string]$WeeklyAt = "03:00",
    [ValidateSet("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")]
    [string]$WeeklyOn = "Monday",
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = "Stop"

# Repo root is the parent of this script's folder.
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot "venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Python not found at $Python. Create the venv first (python -m venv venv) and install requirements."
}

$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

Write-Host "Repo root : $RepoRoot"
Write-Host "Python    : $Python"
Write-Host "Run as    : $RunAsUser"
Write-Host ""

function Register-Job {
    param(
        [string]$Name,
        [string]$Module,
        [string]$Description,
        $Trigger,
        [string]$LogFile
    )

    # Redirect stdout/stderr to a log file -- a scheduled task's console output is
    # otherwise lost, which makes a silent failure very hard to notice.
    $cmd = "& '$Python' -m $Module *>> '$LogFile'"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$cmd`"" `
        -WorkingDirectory $RepoRoot

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    $principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType S4U -RunLevel Limited

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Principal $principal -Description $Description -Force | Out-Null

    Write-Host "  registered: $Name"
}

Write-Host "Registering tasks..."

Register-Job -Name "BCSands-ContentAgent-Daily" `
    -Module "content_seo_agent.daily_run" `
    -Description "Drafts product descriptions for products that need them, into the review queue." `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $DailyAt) `
    -LogFile (Join-Path $LogDir "scheduled_daily_run.log")

Register-Job -Name "BCSands-ChatInsights-Weekly" `
    -Module "chat_insights.weekly_run" `
    -Description "Analyses the week's Chatbase conversations and emails the manager a report." `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyOn -At $WeeklyAt) `
    -LogFile (Join-Path $LogDir "scheduled_weekly_chat.log")

Write-Host ""
Write-Host "Done. Current state:"
Get-ScheduledTask -TaskName "BCSands-*" |
    Select-Object TaskName, State, @{N='NextRun';E={(Get-ScheduledTaskInfo $_.TaskName).NextRunTime}} |
    Format-Table -AutoSize

Write-Host "Logs will be written to $LogDir."
Write-Host "Test one now with:  Start-ScheduledTask -TaskName 'BCSands-ChatInsights-Weekly'"
