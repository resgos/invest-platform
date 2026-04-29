#!/usr/bin/env bash
# ============================================================
#  Обновление кода после первого install.sh.
#  Запускать на сервере под root: sudo bash deploy/update.sh
#  Подразумевается, что свежий код уже залит rsync'ом
#  в /home/deploy/gruppa-titan-new (или поверх).
# ============================================================
set -euo pipefail

DEPLOY_USER="deploy"
APP_DIR="/home/${DEPLOY_USER}/gruppa-titan"

if [[ $EUID -ne 0 ]]; then
    echo "Запустите под root: sudo bash deploy/update.sh"
    exit 1
fi

# Снапшот БД перед обновлением
echo "[*] Снапшот БД..."
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/db_backup.py" create -l "pre-update-$(date +%Y%m%d-%H%M%S)" || \
    echo "[!] Снапшот не создан (продолжаем)"

echo "[*] Обновление зависимостей..."
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet redis pysocks

echo "[*] Миграции БД..."
sudo -u "$DEPLOY_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' migrate.py"

echo "[*] Перезапуск сервиса..."
systemctl restart gruppa-titan
sleep 2
if systemctl is-active --quiet gruppa-titan; then
    echo "[+] Обновление завершено, сервис работает"
else
    echo "[x] Сервис не стартовал — откатитесь по бэкапу из админки"
    journalctl -u gruppa-titan -n 30
    exit 1
fi
