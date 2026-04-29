# Деплой «Группы Титан» на Ubuntu VPS

Одна команда — установка с нуля. Развёртывание занимает 5–10 минут. Стек:
**Python + gunicorn + nginx + redis + ufw + cron**.

---

## TL;DR — установка одной командой

```bash
ssh root@<IP_VPS>
curl -fsSL https://raw.githubusercontent.com/resgos/invest-platform/main/deploy/install.sh | sudo bash
```

Скрипт сам:
1. Поставит python3, nginx, redis, ufw, git
2. Создаст пользователя `deploy`
3. Клонирует репозиторий в `/home/deploy/gruppa-titan`
4. Развернёт venv, поставит зависимости, прогонит миграции
5. Запустит gunicorn под systemd
6. Настроит nginx как reverse-proxy
7. Откроет 22/80/443 в файрволе
8. Сгенерирует `SECRET_KEY` и положит шаблон `.env`
9. Настроит cron на бэкап БД и Telegram-уведомления

В конце напечатает URL и пароль.

---

## Шаг 1. Подготовка ДО установки

### 1а. Telegram-бот
1. Создайте бота: напишите [@BotFather](https://t.me/BotFather) → `/newbot` → сохраните **TOKEN**
2. Создайте Telegram-группу для уведомлений, добавьте бота, сделайте админом
3. Узнайте `chat_id` группы:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   В ответе ищите `"chat":{"id":-100xxxxxxxxxx,...}` — для групп он отрицательный

### 1б. Прокси для Telegram (РФ блокирует api.telegram.org)
Купите **индивидуальный IPv4** на [proxy6.net](https://proxy6.net) или аналоге (~50–100 ₽/мес), страна — Нидерланды/Германия/любая кроме РФ.

Подробнее в основном `README.md` репозитория.

---

## Шаг 2. Установка

```bash
ssh root@<IP_VPS>
curl -fsSL https://raw.githubusercontent.com/resgos/invest-platform/main/deploy/install.sh | sudo bash
```

> Если репозиторий приватный — сначала склонируйте вручную:
> ```bash
> ssh root@<IP_VPS>
> cd /tmp
> git clone https://USER:TOKEN@github.com/resgos/invest-platform.git
> sudo bash invest-platform/deploy/install.sh
> ```
> где `TOKEN` — [GitHub Personal Access Token](https://github.com/settings/tokens) с правом `repo`.

## Шаг 3. Заполнить `.env`

```bash
sudo nano /home/deploy/gruppa-titan/.env
```

Минимум, что вписать:
- `ADMIN_PASSWORD` — придумайте сильный пароль (≥ 8, заглавная + строчная + цифра)
- `TELEGRAM_BOT_TOKEN` — токен от BotFather
- `TELEGRAM_CHAT_ID` — id группы (со знаком «−» для групп)
- `TELEGRAM_PROXY` — `http://user:pass@host:port` или `socks5://user:pass@host:port`

Перезапустите сервис:
```bash
sudo systemctl restart gruppa-titan
```

## Шаг 4. Открыть в браузере

```
http://<IP_VPS>/
```

Войти под `admin / <ADMIN_PASSWORD>` и **сразу сменить пароль через профиль**.

---

## Обновление кода (после первого install.sh)

После каждого `git push` в `main`:

```bash
ssh root@<IP_VPS>
sudo bash /home/deploy/gruppa-titan/deploy/update.sh
```

Или однострочником с локалки:
```bash
ssh root@<IP_VPS> "sudo bash /home/deploy/gruppa-titan/deploy/update.sh"
```

`update.sh` сам:
- Сделает снапшот БД (на всякий случай)
- `git fetch + reset --hard origin/main`
- Обновит зависимости
- Прогонит миграции
- Обновит nginx-конфиг и systemd unit, если они менялись
- Перезапустит сервис

Если после обновления что-то сломалось — снапшот лежит в `/home/deploy/gruppa-titan/backups/` с префиксом `pre-update-...`. Восстановить через UI: **Админ → СУБД и Telegram**.

---

## Что именно настраивается

| Компонент | Что делает |
|-----------|------------|
| `git` | Источник кода — клон + pull, без копирования архивов |
| `gunicorn` | 3 worker'а на 127.0.0.1:5000 |
| `nginx` | reverse-proxy на :80, отдаёт static и проксирует /uploads |
| `redis` | хранилище rate-limiter'а (общее для всех worker'ов) |
| `systemd` | автостарт, рестарт при падении, sandboxing |
| `ufw` | firewall: 22, 80, 443 (остальное закрыто) |
| `cron` | ежедневный бэкап БД в 03:00 + уведомления о старте в 09:00 |

## Параметры через ENV

Все три скрипта читают переменные окружения, можно переопределить:

| Переменная | По умолчанию |
|-----------|--------------|
| `REPO_URL` | `https://github.com/resgos/invest-platform.git` |
| `REPO_BRANCH` | `main` |
| `DEPLOY_USER` | `deploy` |
| `APP_DIR` | `/home/$DEPLOY_USER/gruppa-titan` |

Пример — деплой staging-ветки:
```bash
sudo REPO_BRANCH=staging APP_DIR=/home/deploy/gruppa-titan-staging bash install.sh
```

---

## Эксплуатация

```bash
# Статус и логи
sudo systemctl status gruppa-titan
sudo journalctl -u gruppa-titan -f
sudo tail -f /home/deploy/gruppa-titan/logs/error.log

# Текущий коммит на сервере
sudo -u deploy git -C /home/deploy/gruppa-titan rev-parse --short HEAD
sudo -u deploy git -C /home/deploy/gruppa-titan log --oneline -5

# Откатиться на конкретный коммит / тег
ssh root@<IP_VPS>
cd /home/deploy/gruppa-titan
sudo -u deploy git fetch
sudo -u deploy git checkout <SHA или тег>
sudo systemctl restart gruppa-titan

# Бэкапы
ls -lh /home/deploy/gruppa-titan/backups/
```

## HTTPS (когда появится домен)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d gruppa-titan.ru -d www.gruppa-titan.ru
sudo nano /home/deploy/gruppa-titan/.env  # SESSION_COOKIE_SECURE=true
sudo systemctl restart gruppa-titan
```

---

## Безопасность по умолчанию

- `ufw` оставляет открытыми только 22/80/443
- `nginx` скрывает версию (`server_tokens off`) и шлёт security headers
- `gunicorn` под непривилегированным `deploy`
- `systemd` юнит с `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`
- `.env` с правами `600` — никто кроме `deploy` не прочтёт
- Flask: CSP, X-Frame-Options=DENY, HttpOnly+SameSite cookies
- Redis слушает только `127.0.0.1`
- `bcrypt` для паролей, лимит 5 неудачных попыток → блокировка на 15 минут

## Troubleshooting

**`502 Bad Gateway` от nginx:**
```bash
sudo systemctl status gruppa-titan
sudo journalctl -u gruppa-titan -n 50
```

**Telegram не отправляется:**
- Проверьте прокси: `curl --proxy http://user:pass@host:port https://api.telegram.org/bot<TOKEN>/getMe`
- Проверьте, что бот добавлен в группу и сделан админом
- `chat_id` для групп — отрицательный (со знаком «−»)

**Забыли пароль админа:**
```bash
ssh root@<IP_VPS>
cd /home/deploy/gruppa-titan
sudo -u deploy ./venv/bin/python -c "
import bcrypt
from app import app
from models import db, User
with app.app_context():
    u = User.query.filter_by(role='admin').first()
    new_pw = 'НовыйПароль123!'
    u.password_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    u.failed_login_attempts = 0
    u.locked_until = None
    db.session.commit()
    print('Пароль для', u.username, 'сброшен')
"
```
