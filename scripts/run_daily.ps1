<#
.SYNOPSIS
    Lancement planifié de jobbot (tâches « jobbot-matin » et « jobbot-soir »).

.DESCRIPTION
    -Mode run      : journée complète  -> jobbot run
    -Mode evening  : fin de journée    -> jobbot run --step inbox --step followup
    La sortie de chaque lancement est gardée dans data\logs\tache-<mode>-<date>.out.log / .err.log
    (le journal détaillé de jobbot reste data\logs\jobbot.log). Les journaux de tâche de plus de 30 jours
    sont supprimés.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Mode run
#>
param(
    [ValidateSet("run", "evening")]
    [string]$Mode = "run"
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$outLog = Join-Path $logDir "tache-$Mode-$stamp.out.log"
$errLog = Join-Path $logDir "tache-$Mode-$stamp.err.log"
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-TaskLog([string]$message) {
    $line = "{0} {1}`r`n" -f (Get-Date -Format "s"), $message
    [System.IO.File]::AppendAllText($outLog, $line, $utf8)
}

Get-ChildItem -Path $logDir -Filter "tache-*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $python)) {
    Write-TaskLog "ERREUR : Python introuvable ($python). Installe d'abord jobbot (voir README.md)."
    exit 1
}

$arguments = @("-m", "jobbot.cli", "run")
if ($Mode -eq "evening") {
    $arguments += @("--step", "inbox", "--step", "followup")
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:JOBBOT_ROOT = $root
Write-TaskLog ("Début : jobbot {0}" -f ($arguments[2..($arguments.Length - 1)] -join " "))
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -NoNewWindow -Wait `
    -PassThru -RedirectStandardOutput "$outLog.tmp" -RedirectStandardError $errLog
if (Test-Path "$outLog.tmp") {
    [System.IO.File]::AppendAllText($outLog, [System.IO.File]::ReadAllText("$outLog.tmp", $utf8), $utf8)
    Remove-Item "$outLog.tmp" -Force
}
$code = $process.ExitCode
Write-TaskLog "Fin (code $code)"
exit $code
