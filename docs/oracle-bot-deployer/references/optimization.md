# Оптимизация Oracle Cloud (Ubuntu 1GB RAM)

При установке на новый сервер ОБЯЗАТЕЛЬНО выполнить:

## 1. Лимиты Journald (`/etc/systemd/journald.conf.d/limits.conf`)
```ini
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=50M
MaxRetentionSec=2week
```

## 2. Параметры Swap
- Минимум 4-5 ГБ для стабильности.
- Настройка `swappiness=10`, чтобы не дергать диск лишний раз.

## 3. Лимиты Сервиса (`systemd`)
```ini
# В секции [Service]
MemoryMax=650M
MemoryHigh=520M
CPUQuota=80%
Restart=always
RestartSec=10

# В секции [Install]
WantedBy=multi-user.target
```

## 4. Полезные утилиты
- `earlyoom` — защита от зависания (OOM Killer).
- `htop`, `btop`, `ncdu`, `eza`.
