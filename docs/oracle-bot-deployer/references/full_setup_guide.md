# Полный гайд по настройке Oracle Ubuntu (Zero to Hero)

Этот справочник содержит все команды для пошаговой настройки нового сервера.

## 1. Базовая подготовка ОС
```bash
# Обновление и время
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Europe/Kyiv

# Зависимости Python
sudo apt install -y python3-venv python3-pip
```

## 2. Оптимизация RAM (Критично для 1GB)
```bash
# Создание Swap 5GB
sudo fallocate -l 5G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Установка EarlyOOM (защита от зависаний)
sudo apt install -y earlyoom
sudo systemctl enable --now earlyoom
```

## 3. Утилиты и Комфорт
```bash
# Современные CLI инструменты
sudo apt install -y htop ncdu curl wget rsync

# Рекомендуется установить eza, btop, zoxide через их официальные скрипты/репозитории
# (Агент должен помочь пользователю с этим по запросу)
```

## 4. Настройка Логов (Journald)
Создать файл `/etc/systemd/journald.conf.d/limits.conf`:
```ini
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=50M
MaxRetentionSec=2week
```
Затем: `sudo systemctl restart systemd-journald`

## 5. SSH Алиасы (На Windows)
Добавить в `C:\Users\<User>\.ssh\config`:
```text
Host sv
    HostName <IP_СЕРВЕРА>
    User ubuntu
    IdentityFile <ПУТЬ_К_КЛЮЧУ>
```
