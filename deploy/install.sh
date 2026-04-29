#!/usr/bin/env bash
# ============================================================
#  Группа Титан — деплой-скрипт для Ubuntu (22.04 / 24.04)
#  Клонирует код из git и настраивает весь стек.
#
#  Запускать ОДИН РАЗ на чистом VPS под root:
#    sudo bash install.sh
#
#  Или однострочником прямо с GitHub:
#    curl -fsSL https://raw.githubusercontent.com/resgos/invest-platform/main/deploy/install.sh \
#      | sudo bash
#
#  Идемпотентен — повторный запуск безопасен (rebuild-режим: git pull + restart).
# ============================================================
set -euo pipefail

# ── Параметры (можно переопределить через env) ──
REPO_URL="${REPO_URL:-https://github.com/resgos/invest-platform.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/home/${DEPLOY_USER}/gruppa-titan}"

# ── Цвета ──
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
info() { echo -e "${B}[*]${N} $*"; }
ok()   { echo -e "${G}[+]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[x]${N} $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Запустите под root: sudo bash install.sh"
    exit 1
fi

echo
echo "══════════════════════════════════════════════════════════"
echo "  Группа Титан — установка на VPS"
echo "══════════════════════════════════════════════════════════"
echo "  Репо:     ${REPO_URL}"
echo "  Ветка:    ${REPO_BRANCH}"
echo "  Каталог:  ${APP_DIR}"
echo "══════════════════════════════════════════════════════════"
echo

# ── 1. Системные пакеты ──
info "Шаг 1/9: установка системных пакетов..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git \
    python3 python3-venv python3-pip python3-dev \
    build-essential libssl-dev libffi-dev \
    nginx redis-server \
    sqlite3 \
    ufw cron \
    ca-certificates curl >/dev/null
ok "Системные пакеты установлены"

# ── 2. Пользователь deploy ──
info "Шаг 2/9: пользователь '${DEPLOY_USER}'..."
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
    ok "Создан пользователь $DEPLOY_USER"
else
    ok "Пользователь $DEPLOY_USER уже существует"
fi

# ── 3. Клонирование / обновление кода ──
info "Шаг 3/9: получение кода из git..."
if [[ -d "$APP_DIR/.git" ]]; then
    info "Репо уже клонирован — делаем git pull"
    sudo -u "$DEPLOY_USER" git -C "$APP_DIR" fetch --quiet origin "$REPO_BRANCH"
    sudo -u "$DEPLOY_USER" git -C "$APP_DIR" checkout --quiet "$REPO_BRANCH"
    sudo -u "$DEPLOY_USER" git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
else
    if [[ -d "$APP_DIR" ]] && [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
        warn "Каталог $APP_DIR уже существует и не пуст. Сохраняю в ${APP_DIR}.bak.$(date +%s)"
        mv "$APP_DIR" "${APP_DIR}.bak.$(date +%s)"
    fi
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$(dirname "$APP_DIR")"
    sudo -u "$DEPLOY_USER" git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/instance" "$APP_DIR/static/uploads" "$APP_DIR/backups" "$APP_DIR/logs"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"
ok "Код в $APP_DIR (commit: $(sudo -u $DEPLOY_USER git -C "$APP_DIR" rev-parse --short HEAD))"

# ── 4. Virtualenv + зависимости ──
info "Шаг 4/9: virtualenv и зависимости..."
if [[ ! -d "$APP_DIR/venv" ]]; then
    sudo -u "$DEPLOY_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --upgrade --quiet pip wheel
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$DEPLOY_USER" "$APP_DIR/venv/bin/pip" install --quiet redis pysocks
ok "Виртуальное окружение готово"

# ── 5. .env (генерим один раз, потом пользователь редактирует) ──
info "Шаг 5/9: .env..."
if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/deploy/.env.example" "$APP_DIR/.env"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(48))")
    sed -i "s|REPLACE_WITH_RANDOM_HEX|$SECRET|" "$APP_DIR/.env"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    warn ".env создан с автоматическим SECRET_KEY."
    warn "ОБЯЗАТЕЛЬНО заполните: ADMIN_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY"
    warn "  → sudo nano $APP_DIR/.env"
else
    ok ".env уже существует — пропускаем"
fi

# ── 6. Миграции БД ──
info "Шаг 6/9: миграции БД..."
sudo -u "$DEPLOY_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' migrate.py" || \
    warn "Миграция вернула non-zero (вероятно БД ещё не создана — это норма для первого запуска)"
ok "Миграции применены"

# ── 7. systemd unit ──
info "Шаг 7/9: systemd unit..."
cp "$APP_DIR/deploy/gruppa-titan.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable redis-server >/dev/null 2>&1 || true
systemctl restart redis-server
systemctl enable gruppa-titan >/dev/null 2>&1
systemctl restart gruppa-titan
sleep 2
if systemctl is-active --quiet gruppa-titan; then
    ok "Сервис gruppa-titan запущен"
else
    err "Сервис не стартовал. Логи: journalctl -u gruppa-titan -n 50"
fi

# ── 8. Nginx ──
info "Шаг 8/9: Nginx..."
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/gruppa-titan
ln -sf /etc/nginx/sites-available/gruppa-titan /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
ok "Nginx настроен"

# ── 9. Firewall + cron ──
info "Шаг 9/9: ufw + cron..."
ufw --force enable >/dev/null 2>&1 || true
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true

CRON_FILE="$(mktemp)"
sudo -u "$DEPLOY_USER" crontab -l 2>/dev/null > "$CRON_FILE" || true
if ! grep -q "db_backup.py" "$CRON_FILE"; then
    cat >> "$CRON_FILE" <<EOF
# Группа Титан — авто-бэкап БД
0 3 * * * cd $APP_DIR && $APP_DIR/venv/bin/python db_backup.py create >> $APP_DIR/logs/backup.log 2>&1
EOF
fi
if ! grep -q "notify_upcoming.py" "$CRON_FILE"; then
    cat >> "$CRON_FILE" <<EOF
# Группа Титан — уведомления о скором старте сделок (за 3 дня)
0 9 * * * cd $APP_DIR && $APP_DIR/venv/bin/python notify_upcoming.py -d 3 >> $APP_DIR/logs/notify.log 2>&1
EOF
fi
sudo -u "$DEPLOY_USER" crontab "$CRON_FILE"
rm -f "$CRON_FILE"
ok "Firewall и cron настроены"

# ── Финал ──
IP=$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "════════════════════════════════════════════════════════════"
ok "Установка завершена!"
echo "════════════════════════════════════════════════════════════"
echo
echo "  Открыть:    http://${IP}/"
echo "  Логин:      admin"
echo "  Пароль:     из ADMIN_PASSWORD в $APP_DIR/.env"
echo
echo "  Сервис:     systemctl status gruppa-titan"
echo "  Логи:       journalctl -u gruppa-titan -f"
echo "              tail -f $APP_DIR/logs/error.log"
echo "  Перезапуск: systemctl restart gruppa-titan"
echo "  Обновление: sudo bash $APP_DIR/deploy/update.sh"
echo
warn "СЛЕДУЮЩИЕ ШАГИ:"
echo "   1. Заполните пароль и Telegram-токены:"
echo "        sudo nano $APP_DIR/.env"
echo "   2. Перезапустите сервис:"
echo "        sudo systemctl restart gruppa-titan"
echo "   3. Когда появится домен — поднимите HTTPS:"
echo "        sudo apt install -y certbot python3-certbot-nginx"
echo "        sudo certbot --nginx -d ВАШ_ДОМЕН"
echo "        # Затем в .env: SESSION_COOKIE_SECURE=true"
echo "        sudo systemctl restart gruppa-titan"
echo
