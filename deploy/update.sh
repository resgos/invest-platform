#!/usr/bin/env bash
# ============================================================
#  Обновление кода: git pull + миграции + рестарт.
#  Запускать на сервере под root: sudo bash deploy/update.sh
#
#  Переопределить параметры можно через переменные окружения:
#    sudo APP_DIR=/home/deploy/investplatform \
#         SERVICE_NAME=investplatform \
#         NGINX_SITE=investplatform \
#         bash deploy/update.sh
# ============================================================
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/home/${DEPLOY_USER}/gruppa-titan}"
SERVICE_NAME="${SERVICE_NAME:-gruppa-titan}"
NGINX_SITE="${NGINX_SITE:-${SERVICE_NAME}}"
SERVICE_FILE="${SERVICE_FILE:-${SERVICE_NAME}.service}"
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

if ! systemctl list-unit-files | grep -q "^${SERVICE_FILE}"; then
    err "systemd-сервис ${SERVICE_FILE} не найден."
    err "Передайте корректное имя через SERVICE_NAME=<имя> bash $0"
    exit 1
fi

# 0. Фикс git dubious ownership (на случай разных владельцев)
git config --global --add safe.directory "$APP_DIR" || true

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
    # Всё равно перезапустим сервис, если попросили (полезно для применения env)
    if [[ "${FORCE_RESTART:-0}" == "1" ]]; then
        info "FORCE_RESTART=1 — перезапускаем сервис принудительно"
        systemctl restart "$SERVICE_NAME"
    fi
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
if [[ -f "$APP_DIR/migrate.py" ]]; then
    sudo -u "$DEPLOY_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' migrate.py"
else
    warn "migrate.py не найден — пропускаем"
fi

# 5. Перезапуск + обновление nginx-конфига если изменился
NGINX_TARGET="/etc/nginx/sites-available/${NGINX_SITE}"
if [[ -f "$APP_DIR/deploy/nginx.conf" ]] && [[ -f "$NGINX_TARGET" ]] && ! cmp -s "$APP_DIR/deploy/nginx.conf" "$NGINX_TARGET"; then
    info "nginx.conf изменился — обновляем"
    cp "$APP_DIR/deploy/nginx.conf" "$NGINX_TARGET"
    nginx -t && systemctl reload nginx
fi

SERVICE_TARGET="/etc/systemd/system/${SERVICE_FILE}"
SERVICE_SOURCE="$APP_DIR/deploy/${SERVICE_FILE}"
if [[ -f "$SERVICE_SOURCE" ]] && [[ -f "$SERVICE_TARGET" ]] && ! cmp -s "$SERVICE_SOURCE" "$SERVICE_TARGET"; then
    info "systemd unit изменился — обновляем"
    cp "$SERVICE_SOURCE" "$SERVICE_TARGET"
    systemctl daemon-reload
fi

info "Перезапуск сервиса ${SERVICE_NAME}..."
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Обновление завершено: $OLD_COMMIT → $NEW_COMMIT, сервис работает"
else
    err "Сервис не стартовал — откатитесь по бэкапу из админки"
    journalctl -u "$SERVICE_NAME" -n 30
    exit 1
fi
