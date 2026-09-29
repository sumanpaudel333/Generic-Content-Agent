<#
.SYNOPSIS
    What the scheduled jobs are doing: when each last ran, how it went, and when
    it runs next.

.DESCRIPTION
    Answers "did the job run?" without opening Task Scheduler or an RDP session.

    Read it in this order:

      Schedule  what the task is actually set to -- not what you remember
                setting it to, and not what the README says
      LastRun   when it last STARTED (a manual run counts, see -History)
      Result    0 is success. 267009 means "currently running", 267011 means
                "has never run". Anything else is the exit code of the job.
      NextRun   the next scheduled fire. Blank for at-boot services.

    A task whose Result is 0 and whose NextRun is a date you did not expect has
    not failed -- it is scheduled for a different day than you think.

.PARAMETER History
    Also list recent launches from the Task Scheduler event log, marking each as
    "ON SCHEDULE" or "on demand". This is the only way to tell an automatic run
    from someone pressing Run: both update LastRunTime identically.

.PARAMETER Name
    Task name filter. Defaults to BCSands-*.

.EXAMPLE
    .\scripts\job_status.ps1

.EXAMPLE
    .\scripts\job_status.ps1 -History
#>
[CmdletBinding()]
param(
    [string]$Name = "BCSands-*",
    [switch]$History
)

$dayBits = @(
    @{ Bit = 1;  Name = 'Sun' }, @{ Bit = 2;  Name = 'Mon' }, @{ Bit = 4;  Name = 'Tue' }
    @{ Bit = 8;  Name = 'Wed' }, @{ Bit = 16; Name = 'Thu' }, @{ Bit = 32; Name = 'Fri' }
    @{ Bit = 64; Name = 'Sat' }
)

function Format-Schedule {
    param($Task)
    $parts = foreach ($trigger in $Task.Triggers) {
        $at = ""
        if ($trigger.StartBoundary) {
            $at = " at " + (Get-Date ([datetime]$trigger.StartBoundary) -Format 'HH:mm')
        }
        switch -Wildcard ($trigger.CimClass.CimClassName) {
            '*WeeklyTrigger' {
                $days = foreach ($day in $dayBits) {
                    if ($trigger.DaysOfWeek -band $day.Bit) { $day.Name }
                }
                "Weekly $($days -join ',')$at"
            }
            '*DailyTrigger'   { "Daily$at" }
            '*BootTrigger'    { "At boot" }
            '*LogonTrigger'   { "At logon" }
            '*TimeTrigger'    { "Once$at" }
            default           { $trigger.CimClass.CimClassName -replace '^MSFT_Task', '' }
        }
        if (-not $trigger.Enabled) { "(trigger disabled)" }
    }
    if ($parts) { $parts -join '; ' } else { 'no trigger' }
}

$tasks = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Warning "No scheduled tasks match '$Name'. Register them with scripts\register_scheduled_tasks.ps1 from an elevated PowerShell."
    return
}

$tasks | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    $note = switch ($info.LastTaskResult) {
        0      { '' }
        267009 { 'running now' }
        267011 { 'has never run' }
        267014 { 'last run was terminated' }
        default { "exit code $($info.LastTaskResult)" }
    }
    [PSCustomObject]@{
        Task     = $_.TaskName
        State    = $_.State
        Schedule = Format-Schedule -Task $_
        LastRun  = $info.LastRunTime
        Result   = $info.LastTaskResult
        Note     = $note
        NextRun  = $info.NextRunTime
    }
} | Sort-Object Task | Format-Table -AutoSize | Out-Host

if ($History) {
    Write-Host "`nRecent launches (107 = fired on schedule, 110 = started by hand):`n"
    $events = Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' `
                            -MaxEvents 3000 -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -in 107, 110 -and $_.Message -like '*BCSands*' }

    if (-not $events) {
        Write-Host "  Nothing in the event log. Either the jobs have not run recently, or the"
        Write-Host "  TaskScheduler/Operational log is disabled -- enable it with:"
        Write-Host "    wevtutil set-log Microsoft-Windows-TaskScheduler/Operational /enabled:true"
        return
    }

    $events | Sort-Object TimeCreated | ForEach-Object {
        $taskName = if ($_.Message -match '\\(BCSands-[\w-]+)') { $Matches[1] } else { '?' }
        [PSCustomObject]@{
            When = $_.TimeCreated
            Task = $taskName
            How  = if ($_.Id -eq 107) { 'ON SCHEDULE' } else { 'on demand (manual)' }
        }
    } | Format-Table -AutoSize | Out-Host
}
