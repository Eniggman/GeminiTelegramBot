# =============================================================
# setup_server.ps1 - Pervichnaya nastroyka servera (zapustit ODIN RAZ)
# Kopirует vse fayly bota, sozdaet venv, stavit zavisimosti, nastraivaet systemd
# =============================================================

Set-Location $PSScriptRoot

# --- Nastroyki podklyucheniya ---
$Server    = "ubuntu@<ВАШ_IP_АДРЕС>"
$KeyPath   = "<ПУТЬ_К_ВАШЕМУ_SSH_КЛЮЧУ>"
$RemoteDir = "/home/ubuntu/FolderBot"
$SshOpts   = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=no")

function Invoke-SSH($cmd) {
    ssh @SshOpts $Server $cmd
    return $LASTEXITCODE
}

function Invoke-SCP($src, $dst) {
    scp @SshOpts $src "${Server}:${dst}"
    return $LASTEXITCODE
}

function Check-Error($step, $msg) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "OSHIBKA na shage $step : $msg" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  PERVICHNAYA NASTROYKA SERVERA"       -ForegroundColor Cyan
Write-Host "  Server: $Server"                     -ForegroundColor Cyan
Write-Host "  Papka:  $RemoteDir"                  -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# --- Shag 1: Proverit SSH ---
Write-Host "[1/6] Proverka SSH-soyedineniya..." -ForegroundColor Yellow
Invoke-SSH "echo 'SSH OK'"
Check-Error 1 "Ne udalos podklyuchitsya po SSH."
Write-Host "   OK SSH rabotaet" -ForegroundColor Green
Write-Host ""

# --- Shag 2: Sozdat papku FolderBot ---
Write-Host "[2/6] Sozdanie papki $RemoteDir ..." -ForegroundColor Yellow
Invoke-SSH "mkdir -p $RemoteDir/docs"
Check-Error 2 "Ne udalos sozdat papku na servere."
Write-Host "   OK Papka sozdana" -ForegroundColor Green
Write-Host ""

# --- Shag 3: Kopirovat fayly ---
Write-Host "[3/6] Kopirovanie faylov bota..." -ForegroundColor Yellow

Write-Host "   -> googlebot.py" -ForegroundColor Gray
Invoke-SCP "googlebot.py" "$RemoteDir/"
Check-Error 3 "googlebot.py"

Write-Host "   -> .env" -ForegroundColor Gray
Invoke-SCP ".env" "$RemoteDir/"
Check-Error 3 ".env"

Write-Host "   -> docs/requirements.txt" -ForegroundColor Gray
Invoke-SCP "docs/requirements.txt" "$RemoteDir/docs/"
Check-Error 3 "requirements.txt"

Write-Host "   -> docs/image.png" -ForegroundColor Gray
Invoke-SCP "docs/image.png" "$RemoteDir/docs/"
Check-Error 3 "image.png"

Write-Host "   -> docs/black.jpg" -ForegroundColor Gray
Invoke-SCP "docs/black.jpg" "$RemoteDir/docs/"
Check-Error 3 "black.jpg"

Write-Host "   OK Vse fayly skopirovany" -ForegroundColor Green
Write-Host ""

# --- Shag 4: Sozdat venv i ustanovit zavisimosti ---
Write-Host "[4/6] Sozdanie Python-okruzheniya i ustanovka zavisimostey..." -ForegroundColor Yellow
Write-Host "   (eto mozhet zanyat 2-4 minuty)" -ForegroundColor Gray

Invoke-SSH "cd $RemoteDir && python3 -m venv venv"
Check-Error 4 "Ne udalos sozdat venv."

Invoke-SSH "cd $RemoteDir && ./venv/bin/python -m pip install --upgrade pip --quiet"
Check-Error 4 "Ne udalos obnovit pip."

Invoke-SSH "cd $RemoteDir && ./venv/bin/pip install -r docs/requirements.txt"
Check-Error 4 "Ne udalos ustanovit zavisimosti."

Write-Host "   OK Zavisimosti ustanovleny" -ForegroundColor Green
Write-Host ""

# --- Shag 5: Sozdat systemd servis ---
Write-Host "[5/6] Sozdanie systemd-servisa googlebot..." -ForegroundColor Yellow

# Pishom servis cherez printf na servere
$svcContent = "[Unit]`nDescription=Telegram Gemini Bot`nAfter=network-online.target`nWants=network-online.target`n`n[Service]`nType=simple`nUser=ubuntu`nWorkingDirectory=$RemoteDir`nEnvironment=PYTHONUNBUFFERED=1`nExecStart=$RemoteDir/venv/bin/python $RemoteDir/googlebot.py`nRestart=on-failure`nRestartSec=10`nMemoryMax=650M`nMemoryHigh=520M`nCPUQuota=80%`nTasksMax=80`nStandardOutput=journal`nStandardError=journal`n`n[Install]`nWantedBy=multi-user.target"

Invoke-SSH "printf '$svcContent' | sudo tee /etc/systemd/system/googlebot.service > /dev/null"
Check-Error 5 "Ne udalos zapisat googlebot.service."

Invoke-SSH "sudo systemctl daemon-reload"
Check-Error 5 "daemon-reload."

Invoke-SSH "sudo systemctl enable googlebot"
Check-Error 5 "enable googlebot."

Invoke-SSH "sudo systemctl start googlebot"
Check-Error 5 "start googlebot."

Write-Host "   OK Servis sozdan i zapushchen" -ForegroundColor Green
Write-Host ""

# --- Shag 6: Proverit status ---
Write-Host "[6/6] Proverka statusa..." -ForegroundColor Yellow
Start-Sleep -Seconds 2
Invoke-SSH "systemctl status googlebot --no-pager -l"
Write-Host ""
Write-Host "--- Poslednie logi: ---" -ForegroundColor Cyan
Invoke-SSH "journalctl -u googlebot -n 20 --no-pager"

Write-Host ""
Write-Host "======================================" -ForegroundColor Green
Write-Host "  NASTROYKA ZAVERSHENA!"               -ForegroundColor Green
Write-Host "  Bot zapushchen i dobavlen v avtozapusk." -ForegroundColor Green
Write-Host "  Dlya obnovleniy ispolzuy: deploy.ps1"   -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Green
