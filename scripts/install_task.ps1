<#
.SYNOPSIS
    Installe (ou supprime) les tâches planifiées Windows de jobbot.

.DESCRIPTION
    jobbot-matin : chaque jour à -Matin (09:30)  -> scripts\run_daily.ps1 -Mode run
    jobbot-soir  : chaque jour à -Soir  (18:00)  -> scripts\run_daily.ps1 -Mode evening (boîte mail + relances)
    Les tâches tournent sous ton compte, session ouverte (le navigateur de LinkedIn / HelloWork doit pouvoir
    s'afficher), et démarrent dès que possible si le PC était éteint à l'heure prévue.
    Équivalent : « jobbot schedule install » / « jobbot schedule remove ».

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Matin 08:45 -Soir 18:30
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Supprimer
#>
param(
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$Matin = "09:30",
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$Soir = "18:00",
    [switch]$Supprimer
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_daily.ps1"
$tasks = @(
    @{ Name = "jobbot-matin"; Time = $Matin; Mode = "run"; Description = "jobbot : collecte, tri, CV/lettres, candidatures, relances" },
    @{ Name = "jobbot-soir"; Time = $Soir; Mode = "evening"; Description = "jobbot : lecture des réponses et relances" }
)

$failed = 0
foreach ($task in $tasks) {
    try {
        if ($Supprimer) {
            if (Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false
                Write-Output "$($task.Name) : supprimée"
            } else {
                Write-Output "$($task.Name) : absente"
            }
            continue
        }
        $argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Mode $($task.Mode)"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -Daily -At $task.Time
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $trigger -Settings $settings `
            -Description $task.Description -Force | Out-Null
        Write-Output "$($task.Name) : installée (chaque jour à $($task.Time))"
    } catch {
        $failed++
        Write-Output "$($task.Name) : échec ($($_.Exception.Message))"
    }
}
exit $failed
