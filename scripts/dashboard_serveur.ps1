<#
.SYNOPSIS
    Ouvre le tableau de bord jobbot du serveur dans ton navigateur, par un tunnel SSH.

.DESCRIPTION
    Sur le serveur, le tableau de bord écoute seulement sur 127.0.0.1:8501 (jamais exposé au réseau).
    Ce script ouvre « ssh -N -L 8501:127.0.0.1:8501 <PROJECT_SSH_HOST_ALIAS> » (alias lu dans le .env du projet,
    authentification par clé), puis http://localhost:8501. Ferme cette fenêtre ou Ctrl+C pour couper le tunnel.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dashboard_serveur.ps1
    powershell -ExecutionPolicy Bypass -File scripts\dashboard_serveur.ps1 -Port 8600
#>
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Output "Fichier .env introuvable ($envFile)."
    exit 1
}

$alias = $null
foreach ($line in Get-Content -Path $envFile -Encoding UTF8) {
    if ($line -match '^\s*PROJECT_SSH_HOST_ALIAS\s*=\s*([^#\s]+)') {
        $alias = $Matches[1].Trim('"', "'")
    }
}
if (-not $alias) {
    Write-Output "PROJECT_SSH_HOST_ALIAS absent du .env (alias SSH du compte qui fait tourner jobbot)."
    exit 1
}
if ($alias -notmatch '^[A-Za-z0-9][A-Za-z0-9._@-]*$') {
    Write-Output "PROJECT_SSH_HOST_ALIAS invalide dans le .env."
    exit 1
}

$url = "http://localhost:$Port"
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Output "Le port local $Port est déjà utilisé (tunnel déjà ouvert ?) : ouverture de $url."
    Start-Process $url
    exit 0
}

Write-Output "Tunnel SSH vers le tableau de bord du serveur ($alias)... Ferme cette fenêtre ou Ctrl+C pour arrêter."
$sshArgs = @("-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-L", "${Port}:127.0.0.1:8501", $alias)
$ssh = Start-Process -FilePath "ssh" -ArgumentList $sshArgs -NoNewWindow -PassThru
Start-Sleep -Seconds 3
if ($ssh.HasExited) {
    Write-Output "Tunnel impossible (code $($ssh.ExitCode)) : vérifie « ssh $alias » et le service jobbot-dashboard."
    exit 1
}
Start-Process $url
try {
    Wait-Process -Id $ssh.Id
} finally {
    if (-not $ssh.HasExited) {
        Stop-Process -Id $ssh.Id -Force
    }
}
