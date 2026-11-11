[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $Config,

    [string] $Python,

    [switch] $Start
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$configFile = (Resolve-Path -LiteralPath $Config).Path
if (-not $Python) {
    $Python = Join-Path $repo '.venv\Scripts\python.exe'
}
$pythonFile = (Resolve-Path -LiteralPath $Python).Path
$profileConfig = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
$project = $profileConfig.mcp_servers.project_files
if ($project.cwd -notmatch '^[A-Za-z]:[\\/]' -or
    -not (Test-Path -LiteralPath $project.cwd -PathType Container)) {
    throw 'Configure mcp_servers.project_files.cwd as an existing absolute project path first.'
}

$serverTask = 'Strata Local Web'
$openerTask = 'Strata Project File Opener'
foreach ($taskName in @($serverTask, $openerTask)) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        throw "The Windows task '$taskName' already exists."
    }
}
if ($Start -and (Get-NetTCPConnection -LocalPort 8080, 8081 -State Listen -ErrorAction SilentlyContinue)) {
    throw 'Stop the processes already listening on ports 8080 and 8081 before starting the tasks.'
}

function Quote-TaskValue([string] $Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

$logDir = Join-Path $env:LOCALAPPDATA 'Strata\ProjectWeb'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$shellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$serverCommand = '& {0} -u {1} --engine strata --config {2} --host 127.0.0.1 --port 8080 --lazy >> {3} 2>> {4}; exit $LASTEXITCODE' -f `
    (Quote-TaskValue $pythonFile), (Quote-TaskValue (Join-Path $repo 'serve\server.py')), (Quote-TaskValue $configFile), `
    (Quote-TaskValue (Join-Path $logDir 'server.out.log')), (Quote-TaskValue (Join-Path $logDir 'server.err.log'))
$openerCommand = '& {0} -u {1} {2} >> {3} 2>> {4}; exit $LASTEXITCODE' -f `
    (Quote-TaskValue $pythonFile), (Quote-TaskValue (Join-Path $PSScriptRoot 'project_file_opener.py')), (Quote-TaskValue $configFile), `
    (Quote-TaskValue (Join-Path $logDir 'opener.out.log')), (Quote-TaskValue (Join-Path $logDir 'opener.err.log'))

foreach ($task in @(
    @{ Name = $serverTask; Command = $serverCommand; Description = 'Run the local Strata web server.' }
    @{ Name = $openerTask; Command = $openerCommand; Description = 'Open existing project files from the local Strata page.' }
)) {
    $action = New-ScheduledTaskAction -Execute $shellPath `
        -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -Command "' + $task.Command + '"') `
        -WorkingDirectory $repo
    Register-ScheduledTask -TaskName $task.Name -Action $action -Principal $principal -Trigger $trigger `
        -Settings $settings -Description $task.Description | Out-Null
}
if ($Start) {
    Start-ScheduledTask -TaskName $serverTask
    Start-ScheduledTask -TaskName $openerTask
}
