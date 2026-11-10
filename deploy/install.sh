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
#
#  Общий сервер (рядом живут чужие сайты/службы) + свой HTTPS-поддомен:
#    sudo SHARED_HOST=1 DOMAIN=titan.example.com bash install.sh
#  SHARED_HOST=1 — не трогать ufw и чужой nginx default-сайт;
#  DOMAIN        — именованный server-блок вместо default_server + сертификат
#                  Let's Encrypt (certbot --nginx, редирект http→https).
# ============================================================
set -euo pipefail

# ── Параметры (можно переопределить через env) ──
REPO_URL="${REPO_URL:-https://github.com/resgos/invest-platform.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/home/${DEPLOY_USER}/gruppa-titan}"
DOMAIN="${DOMAIN:-}"
SHARED_HOST="${SHARED_HOST:-0}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

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
echo "  Домен:    ${DOMAIN:-— (default_server, без HTTPS)}"
echo "  Общий сервер: $([[ "$SHARED_HOST" == "1" ]] && echo да || echo нет)"
echo "══════════════════════════════════════════════════════════"
echo

if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    err "Недопустимое имя домена: $DOMAIN"
    exit 1
fi

# ── 1. Системные пакеты ──
info "Шаг 1/9: установка системных пакетов..."
export DEBIAN_FRONTEND=noninteractive
EXTRA_PKGS=()
if [[ "$SHARED_HOST" != "1" ]]; then EXTRA_PKGS+=(ufw); fi
if [[ -n "$DOMAIN" ]]; then EXTRA_PKGS+=(certbot python3-certbot-nginx); fi
apt-get update -qq
apt-get install -y -qq \
    git \
    python3 python3-venv python3-pip python3-dev \
    build-essential libssl-dev libffi-dev \
    nginx redis-server \
    sqlite3 \
    cron \
    ca-certificates curl \
    "${EXTRA_PKGS[@]}" >/dev/null
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
    # Админ создаётся при ПЕРВОМ старте сервиса с паролем из .env — шаблонный
    # пароль из публичного репозитория стал бы боевым. Генерируем случайный
    # (буквы+цифры: проходит политику «заглавная+строчная+цифра», безопасен для sed/.env).
    ADMIN_PW=$(python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print('Ti'+''.join(secrets.choice(a) for _ in range(18))+'7')")
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PW}|" "$APP_DIR/.env"
    if [[ -n "$DOMAIN" ]]; then
        sed -i "s|^SITE_URL=.*|SITE_URL=https://${DOMAIN}|" "$APP_DIR/.env"
    fi
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    warn ".env создан: SECRET_KEY и пароль админа сгенерированы (ADMIN_PASSWORD в .env)."
    warn "Telegram (по желанию): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY"
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
cp "$APP_DIR/deploy/gruppa-titan-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable redis-server >/dev/null 2>&1 || true
systemctl restart redis-server
systemctl enable gruppa-titan >/dev/null 2>&1
# Не прерываем установку, если сервис не стартовал —
# тогда хотя бы nginx и cron настроятся, и юзер сможет довести до ума через .env
systemctl restart gruppa-titan || true
sleep 2
if systemctl is-active --quiet gruppa-titan; then
    ok "Сервис gruppa-titan запущен"
    # На чистой установке шаг 6 нашёл пустоту (БД создаёт первый старт приложения
    # через db.create_all) — прогоняем миграции ещё раз: они идемпотентны и
    # пометят себя применёнными, чтобы будущие update.sh не начинали с 001.
    sudo -u "$DEPLOY_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' migrate.py" >/dev/null || \
        warn "Повторный прогон миграций вернул non-zero: sudo -u $DEPLOY_USER $APP_DIR/venv/bin/python $APP_DIR/migrate.py"
else
    warn "Сервис не стартовал — продолжаем установку. Проверьте после правки .env:"
    warn "    sudo journalctl -u gruppa-titan -n 50"
fi

# Телеграм-бот (отдельный процесс; без токена в .env не стартует — это норма)
systemctl enable gruppa-titan-bot >/dev/null 2>&1 || true
systemctl restart gruppa-titan-bot || true
sleep 2
if systemctl is-active --quiet gruppa-titan-bot; then
    ok "Телеграм-бот gruppa-titan-bot запущен"
else
    warn "Бот не стартовал (нужен TELEGRAM_BOT_TOKEN/TELEGRAM_USER_BOT_TOKEN в .env)."
    warn "После правки .env: sudo systemctl restart gruppa-titan-bot"
fi

# ── 8. Nginx ──
info "Шаг 8/9: Nginx..."
NGINX_TARGET=/etc/nginx/sites-available/gruppa-titan
if [[ -f "$NGINX_TARGET" ]] && grep -q "managed by Certbot" "$NGINX_TARGET"; then
    # Повторный запуск: конфиг уже с SSL от certbot — перезапись снесла бы 443
    ok "Конфиг nginx под управлением certbot (HTTPS) — не перезаписываю"
else
    NGINX_TMP="$(mktemp)"
    bash "$APP_DIR/deploy/render-nginx.sh" "$DOMAIN" > "$NGINX_TMP"
    [[ -f "$NGINX_TARGET" ]] && cp "$NGINX_TARGET" "${NGINX_TARGET}.bak.$(date +%Y%m%d-%H%M%S)"
    install -m 644 "$NGINX_TMP" "$NGINX_TARGET"
    rm -f "$NGINX_TMP"
fi
ln -sf "$NGINX_TARGET" /etc/nginx/sites-enabled/
if [[ "$SHARED_HOST" != "1" && -z "$DOMAIN" ]]; then
    # Выделенный VPS: наш блок — default_server, штатный default-сайт мешает
    rm -f /etc/nginx/sites-enabled/default
fi
if ! nginx -t; then
    err "nginx -t не прошёл — снимаю наш сайт, чтобы не уронить nginx целиком"
    rm -f /etc/nginx/sites-enabled/gruppa-titan
    exit 1
fi
systemctl reload nginx
ok "Nginx настроен"

# ── 8б. HTTPS (Let's Encrypt) ──
if [[ -n "$DOMAIN" ]]; then
    info "Шаг 8б: сертификат Let's Encrypt для ${DOMAIN}..."
    if grep -q "managed by Certbot" "$NGINX_TARGET"; then
        ok "Сертификат уже подключён"
    else
        CERTBOT_ACCOUNT=(--register-unsafely-without-email)
        [[ -n "$CERTBOT_EMAIL" ]] && CERTBOT_ACCOUNT=(-m "$CERTBOT_EMAIL")
        if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect \
                "${CERTBOT_ACCOUNT[@]}"; then
            ok "HTTPS включён: https://${DOMAIN}"
        else
            warn "certbot не выпустил сертификат — сайт пока только по http://${DOMAIN}"
            warn "  Повторить: sudo certbot --nginx -d ${DOMAIN} --redirect"
        fi
    fi
    if grep -q "managed by Certbot" "$NGINX_TARGET"; then
        # Cookie сессии — только по HTTPS (до сертификата логин по http бы ломался)
        sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=true|" "$APP_DIR/.env"
        systemctl restart gruppa-titan || true
    fi
fi

# ── 9. Firewall + cron ──
info "Шаг 9/9: ufw + cron..."
if [[ "$SHARED_HOST" == "1" ]]; then
    # На общем сервере включение ufw закрыло бы порты соседних служб
    warn "SHARED_HOST=1 — файрвол не трогаю (порты 80/443 открывает облако/хозяин сервера)"
else
    ufw --force enable >/dev/null 2>&1 || true
    ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow 80/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
fi

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
if [[ -n "$DOMAIN" ]]; then
    if grep -q "managed by Certbot" "$NGINX_TARGET"; then SITE="https://${DOMAIN}/"; else SITE="http://${DOMAIN}/"; fi
else
    IP=$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    SITE="http://${IP}/"
fi
echo
echo "════════════════════════════════════════════════════════════"
ok "Установка завершена!"
echo "════════════════════════════════════════════════════════════"
echo
echo "  Открыть:    ${SITE}"
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
echo "   1. Пароль админа:  sudo grep ^ADMIN_PASSWORD $APP_DIR/.env"
echo "      (после первого входа смените его в профиле)"
echo "   2. Telegram-токены и прокси (по желанию):"
echo "        sudo nano $APP_DIR/.env && sudo systemctl restart gruppa-titan gruppa-titan-bot"
if [[ -z "$DOMAIN" ]]; then
echo "   3. Когда появится домен — переустановите с HTTPS:"
echo "        sudo DOMAIN=ВАШ_ДОМЕН bash $APP_DIR/deploy/install.sh"
fi
echo
