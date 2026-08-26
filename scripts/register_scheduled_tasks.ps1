<#
.SYNOPSIS
    Registers the Automation Control jobs and their always-on dependencies in
    Windows Task Scheduler.

.DESCRIPTION
    Four tasks, two kinds.

    Scheduled jobs -- run, do their work, exit:
      * BCSands-ContentAgent-Daily  -- content_seo_agent/daily_run.py, once a day
      * BCSands-ChatInsights-Weekly -- chat_insights/weekly_run.py, once a week

    Always-on services -- start at boot and stay up:
      * BCSands-Dashboard -- the review dashboard (uvicorn)
      * BCSands-Ollama    -- the local model server both jobs depend on

    The services matter more than they look. Ollama ships as a per-user Startup
    shortcut, which only runs while someone is logged in interactively: log off
    the server and the 02:00 job wakes up to no model at all, escalating every
    product straight to Claude (or, with Claude disabled, filling the queue with
    low-confidence rows). The dashboard has the same problem -- started by hand
    in a console, it dies with the console.

    All four run whether or not anyone is logged on. How they authenticate is
    -LogonType:

      S4U       (default) service-for-user: no password stored anywhere. Needs
                the account to hold "Log on as a batch job", and domain policy
                must permit S4U. When it does not, every task fails to start
                with error 2147943726 (0x8007052E, "the user name or password
                is incorrect") and event 104 in the Task Scheduler operational
                log naming LogonUserS4U. Nothing runs and no log is written --
                the failure is entirely inside Task Scheduler.

      Password  stores the account's password with the task. Always works where
                S4U is blocked by policy. The script prompts for it via
                Get-Credential; it is never written to disk by this script and
                never belongs in the repo.

      System    runs as NT AUTHORITY\SYSTEM. No password to store, nothing to
                re-enter when a password changes, and no batch-logon right or
                domain S4U support needed -- the usual answer when S4U is
                refused. SYSTEM needs read/write on the repo (it has it by
                default) and, for Ollama, a models path it can actually reach:
                set OLLAMA_MODELS machine-wide to a LOCAL path, because SYSTEM
                has neither the user's profile nor their mapped drives.

      Interactive
                runs as the user, with no stored password, but ONLY while that
                user is logged on -- a trigger that fires with nobody logged on
                is skipped. Reasonable on a server someone stays signed into
                (it is how the stock Ollama Startup shortcut already behaves),
                and it needs no domain change at all. It does not survive a
                logoff, so it is a fallback, not the goal.

    Re-running is safe: tasks with the same names are replaced. Re-running with
    new times is how you change a schedule.

    -LogonType is sticky. Omit it on a re-run and the script keeps whatever the
    already-registered tasks use, rather than silently reverting them to the S4U
    default -- on a host where S4U is refused, that would turn a working set of
    jobs back into ones that never start, just because someone changed a time.

.PARAMETER DailyAt
    Time of day for the content job. Default 11:00.

.PARAMETER WeeklyAt
    Time of day for the chat job. Default 11:00.

.PARAMETER WeeklyOn
    Day of week for the chat job. Default Wednesday.

.PARAMETER DashboardPort
    Port for the dashboard task. Default 8420, matching the README.

.PARAMETER SkipDashboard
    Don't register the dashboard service (e.g. you run it under IIS or NSSM).

.PARAMETER SkipOllama
    Don't register the Ollama service (e.g. it is already installed as a
    machine-wide service rather than a per-user Startup shortcut).

.PARAMETER LogonType
    How the tasks authenticate: S4U, System, Password, or Interactive. Defaults
    to whatever the existing tasks already use, and to S4U only on a first run.
    See above.

.PARAMETER Credential
    Used with -LogonType Password. Omit it and the script prompts.

.PARAMETER RunAsUser
    Account the tasks run as. Defaults to the current user. The account needs to
    reach Ollama, Odoo and Chatbase, and needs "Log on as a batch job".

.EXAMPLE
    # From an ELEVATED PowerShell, in the repo root:
    .\scripts\register_scheduled_tasks.ps1

.EXAMPLE
    # Change the schedule. Times are HH:mm (24-hour); the logon type is kept.
    # -Skip* leaves the running dashboard and Ollama services alone.
    .\scripts\register_scheduled_tasks.ps1 -DailyAt 01:30 -WeeklyOn Sunday -WeeklyAt 23:00 -SkipDashboard -SkipOllama

.EXAMPLE
    # When S4U is refused and you do not want a stored password.
    .\scripts\register_scheduled_tasks.ps1 -LogonType System

.EXAMPLE
    # When S4U is blocked by domain policy -- prompts for the password once.
    .\scripts\register_scheduled_tasks.ps1 -LogonType Password

.NOTES
    Check afterwards with:   Get-ScheduledTask -TaskName 'BCSands-*'
    Run one immediately:     Start-ScheduledTask -TaskName 'BCSands-ChatInsights-Weekly'
    Inspect last result:     Get-ScheduledTaskInfo -TaskName 'BCSands-ChatInsights-Weekly'
    Remove:                  Unregister-ScheduledTask -TaskName 'BCSands-*' -Confirm:$false

    LastTaskResult 0 means success, 267011 "not run yet", 267009 "currently
    running" (normal for the two services).
    Anything else: read the matching log in logs\ -- and if there is no log at
    all, the task never started, so look in Task Scheduler's own log instead:

      Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 200 |
        Where-Object { $_.Message -like '*BCSands*' } |
        Select-Object TimeCreated, Id, Message | Format-List
#>
[CmdletBinding()]
param(
    [string]$DailyAt = "11:00",
    [string]$WeeklyAt = "11:00",
    [ValidateSet("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")]
    [string]$WeeklyOn = "Wednesday",
    [int]$DashboardPort = 8420,
    [ValidateSet("S4U","System","Password","Interactive")]
    [string]$LogonType,
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$SkipDashboard,
    [switch]$SkipOllama,
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = "Stop"

# Registering a task with an S4U principal needs elevation. Fail here with a
# clear message rather than part-way through with an access-denied.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated PowerShell (Run as Administrator)."
}

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

# Changing a schedule means re-running this script, and a re-run must not
# quietly change how the tasks log on. Unless -LogonType was passed explicitly,
# inherit it from the tasks already registered.
if (-not $PSBoundParameters.ContainsKey("LogonType")) {
    $existing = Get-ScheduledTask -TaskName "BCSands-*" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($existing) {
        $LogonType = switch ($existing.Principal.LogonType) {
            "ServiceAccount" { "System" }
            "Password"       { "Password" }
            "Interactive"    { "Interactive" }
            "InteractiveOrPassword" { "Password" }
            default          { "S4U" }
        }
        Write-Host "Keeping the logon type the existing tasks use: $LogonType"
        Write-Host "(pass -LogonType explicitly to change it)"
    } else {
        $LogonType = "S4U"
    }
}

# How every task authenticates. Password mode passes -User/-Password straight to
# Register-ScheduledTask instead of a principal object, which is the only way
# Task Scheduler will store a password.
$identityArgs = @{}
switch ($LogonType) {
    "S4U" {
        $identityArgs["Principal"] = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType S4U -RunLevel Limited
    }
    "System" {
        $identityArgs["Principal"] = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        # SYSTEM has no user profile of its own to speak of and no mapped
        # drives. Anything the jobs read by absolute local path is fine; a
        # models path under a user profile or on a drive letter is not.
        if (-not $SkipOllama) {
            $machineModels = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Machine")
            if (-not $machineModels) {
                Write-Warning "OLLAMA_MODELS is not set machine-wide. As SYSTEM, Ollama will not find the models in the user profile. Set it first, e.g.:"
                Write-Warning "  [Environment]::SetEnvironmentVariable('OLLAMA_MODELS','C:\Users\$env:USERNAME\.ollama\models','Machine')"
            }
        }
    }
    "Interactive" {
        $identityArgs["Principal"] = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Limited
        Write-Warning "Interactive: these tasks run ONLY while $RunAsUser is logged on. A trigger that fires with nobody logged on is skipped, and the at-boot services will not start until someone signs in."
    }
    "Password" {
        if (-not $Credential) {
            $Credential = Get-Credential -UserName $RunAsUser -Message "Password for the account the jobs run as"
        }
        $identityArgs["User"] = $Credential.UserName
        $identityArgs["Password"] = $Credential.GetNetworkCredential().Password
    }
}
Write-Host "Logon type: $LogonType"
Write-Host ""

function New-Action {
    <# Runs one command with stdout and stderr appended to a log file -- a
       scheduled task's console output is otherwise lost, which makes a silent
       failure very hard to notice.

       Via cmd.exe rather than powershell.exe, for two reasons learned the hard
       way. Windows PowerShell wraps a native program's stderr in ErrorRecords,
       so python's logging output (which goes to stderr) made powershell.exe
       exit 1 even on a completely successful run -- LastTaskResult said failure
       while the job had just emailed the report. And PowerShell's redirection
       writes UTF-16, so the logs came out full of null bytes. cmd passes the
       child's exit code through untouched and writes plain text. #>
    param([string]$Exe, [string]$Arguments, [string]$LogFile)

    # cmd /c needs the whole command line wrapped in one more pair of quotes
    # when the program path itself is quoted.
    $line = "/c `"`"$Exe`" $Arguments >> `"$LogFile`" 2>&1`""
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument $line -WorkingDirectory $RepoRoot
}

function Register-Job {
    <# A job that runs, finishes and exits. #>
    param(
        [string]$Name,
        [string]$Module,
        [string]$Description,
        $Trigger,
        [string]$LogFile
    )

    $action = New-Action -Exe $Python -Arguments "-m $Module" -LogFile $LogFile

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Description $Description -Force @identityArgs | Out-Null

    Write-Host "  registered job     : $Name"
}

function Register-Service {
    <# A process that starts at boot and is meant to stay up. Differs from a job
       in three ways that matter: no execution time limit (six hours would kill
       the dashboard mid-week), restart on failure, and StartWhenAvailable so a
       missed boot trigger still fires. #>
    param(
        [string]$Name,
        [string]$Exe,
        [string]$Arguments,
        [string]$LogFile,
        [string]$Description
    )

    $action = New-Action -Exe $Exe -Arguments $Arguments -LogFile $LogFile

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -RestartCount 5 `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

    Register-ScheduledTask -TaskName $Name -Action $action `
        -Trigger (New-ScheduledTaskTrigger -AtStartup) `
        -Settings $settings -Description $Description -Force @identityArgs | Out-Null

    Write-Host "  registered service : $Name"
}

Write-Host "Registering tasks..."

# --- the two batch jobs ----------------------------------------------------

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

# --- the things those jobs depend on ---------------------------------------

if (-not $SkipDashboard) {
    Register-Service -Name "BCSands-Dashboard" `
        -Exe $Python `
        -Arguments "-m uvicorn dashboard.app:app --host 0.0.0.0 --port $DashboardPort" `
        -LogFile (Join-Path $LogDir "dashboard.log") `
        -Description "Review dashboard on port $DashboardPort. Started at boot so it survives logoff."
}

if (-not $SkipOllama) {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) {
        $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    }
    if (Test-Path $ollama) {
        # OLLAMA_MODELS decides where the server looks for models, and a
        # scheduled task is a non-interactive logon: mapped drive letters do not
        # exist there, whoever the task runs as. A models path on H:\ resolves
        # in an interactive session and nowhere else, so the server would come
        # up serving nothing and every product would escalate.
        $modelsPath = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Machine")
        if (-not $modelsPath) {
            $modelsPath = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
        }
        if ($modelsPath) {
            $isUnc = $modelsPath.StartsWith("\\")
            $driveLetter = if ($modelsPath -match '^([A-Za-z]):') { $Matches[1] } else { $null }
            $isMapped = $driveLetter -and
                (Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue).DisplayRoot
            if ($isMapped) {
                Write-Warning "OLLAMA_MODELS is $modelsPath, on mapped drive ${driveLetter}: ($((Get-PSDrive -Name $driveLetter).DisplayRoot)). A scheduled task cannot see mapped drives -- Ollama will start with no models. Point it at a local path or the UNC path instead:"
                Write-Warning "  [Environment]::SetEnvironmentVariable('OLLAMA_MODELS','C:\OllamaModels','Machine')"
            } elseif ($isUnc) {
                Write-Warning "OLLAMA_MODELS is a UNC path ($modelsPath). It will only work if the task's account can reach it non-interactively -- SYSTEM cannot. A local path is safer."
            } elseif (-not (Test-Path $modelsPath)) {
                Write-Warning "OLLAMA_MODELS is $modelsPath, which does not exist. Ollama will start with no models at all."
            }
        }

        Register-Service -Name "BCSands-Ollama" `
            -Exe $ollama `
            -Arguments "serve" `
            -LogFile (Join-Path $LogDir "ollama.log") `
            -Description "Local model server. Both jobs depend on it; the stock Startup shortcut only runs while someone is logged in."
        Write-Host ""
        Write-Host "NOTE: Ollama also has a per-user Startup shortcut. Leaving it is harmless -- the"
        Write-Host "      second instance finds port 11434 taken and exits -- but you can remove it:"
        Write-Host "      Remove-Item '$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk'"
    } else {
        Write-Warning "ollama.exe not found -- skipping the Ollama service. Install Ollama, or pass -SkipOllama to silence this."
    }
}

Write-Host ""
Write-Host "Done. Current state:"
Get-ScheduledTask -TaskName "BCSands-*" |
    Select-Object TaskName, State, @{N='NextRun';E={(Get-ScheduledTaskInfo $_.TaskName).NextRunTime}} |
    Format-Table -AutoSize

Write-Host "Logs will be written to $LogDir."
Write-Host ""
Write-Host "The two services only start on boot. Start them now without rebooting:"
Write-Host "  Start-ScheduledTask -TaskName 'BCSands-Ollama'"
Write-Host "  Start-ScheduledTask -TaskName 'BCSands-Dashboard'"
Write-Host ""
Write-Host "If a task stays at LastTaskResult 267011 with no log file, it never started."
Write-Host "Check Task Scheduler's own log -- 0x8007052E in event 104 means the logon was"
Write-Host "refused, not that the job failed. Re-register with -LogonType System in that case"
Write-Host "(no password, nothing that expires), or -LogonType Password if the jobs must run"
Write-Host "as a specific user."
Write-Host ""
Write-Host "Then prove the whole chain works end to end:"
Write-Host "  & '$Python' -m chat_insights.weekly_run --dry-run"
Write-Host "  Start-ScheduledTask -TaskName 'BCSands-ChatInsights-Weekly'"
