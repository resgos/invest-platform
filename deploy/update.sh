#!/usr/bin/env bash
# ============================================================
#  Обновление кода: git pull + миграции + рестарт.
#  Запускать на сервере под root: sudo bash deploy/update.sh
#  (или: sudo bash /home/deploy/gruppa-titan/deploy/update.sh)
# ============================================================
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/home/${DEPLOY_USER}/gruppa-titan}"
REPO_BRANCH="${REPO_BRANCH:-main}"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
info() { echo -e "${B}[*]${N} $*"; }
ok()   { echo -e "${G}[+]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[x]${N} $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Запустите под root: sudo bash deploy/update.sh"
    exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
    err "$APP_DIR не git-репозиторий. Сначала запустите install.sh"
    exit 1
fi

# 1. Снапшот БД на всякий случай
info "Снапшот БД..."
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/db_backup.py" create -l "pre-update-$(date +%Y%m%d-%H%M%S)" || \
    warn "Снапшот не создан (продолжаем)"

# 2. git pull
info "git fetch + reset..."
OLD_COMMIT=$(sudo -u "$DEPLOY_USER" git -C "$APP_DIR" rev-parse --short HEAD)
sudo -u "$DEPLOY_USER" git -C "$APP_DIR" fetch --quiet origin "$REPO_BRANCH"
sudo -u "$DEPLOY_USER" git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
NEW_COMMIT=$(sudo -u "$DEPLOY_USER" git -C "$APP_DIR" rev-parse --short HEAD)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    ok "Уже на последнем коммите ($NEW_COMMIT) — нечего обновлять"
    exit 0
fi
ok "$OLD_COMMIT → $NEW_COMMIT"

# 3. Зависимости
info "Обновление зависимостей..."
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --upgrade --quiet pip
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet redis pysocks

# 4. Миграции
info "Миграции БД..."
sudo -u "$DEPLOY_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' migrate.py"

# 5. Перезапуск + обновление nginx-конфига если изменился
if ! cmp -s "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/gruppa-titan; then
    info "nginx.conf изменился — обновляем"
    cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/gruppa-titan
    nginx -t && systemctl reload nginx
fi
if ! cmp -s "$APP_DIR/deploy/gruppa-titan.service" /etc/systemd/system/gruppa-titan.service; then
    info "systemd unit изменился — обновляем"
    cp "$APP_DIR/deploy/gruppa-titan.service" /etc/systemd/system/
    systemctl daemon-reload
fi

info "Перезапуск сервиса..."
systemctl restart gruppa-titan
sleep 2
if systemctl is-active --quiet gruppa-titan; then
    ok "Обновление завершено: $OLD_COMMIT → $NEW_COMMIT, сервис работает"
else
    err "Сервис не стартовал — откатитесь по бэкапу из админки"
    journalctl -u gruppa-titan -n 30
    exit 1
fi
