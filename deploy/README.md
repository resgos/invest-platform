# Деплой «Группы Титан» на Ubuntu VPS

Развёртывание занимает 5–10 минут. Скрипт `install.sh` ставит весь стек:
**Python + gunicorn + nginx + redis + ufw + cron**.

---

## Шаг 1. Получить Telegram-токен и chat_id

1. Создайте бота: напишите [@BotFather](https://t.me/BotFather), команда `/newbot`. Сохраните **TOKEN**.
2. Создайте Telegram-группу для уведомлений, добавьте бота, сделайте админом.
3. Узнайте `chat_id` группы. Самый простой способ:
   ```bash
   # Отправьте в группу любое сообщение, потом:
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   В ответе найдите `"chat":{"id":-100xxxxxxxxxx,...}` — это и есть chat_id (для групп он отрицательный).

## Шаг 2. Найти прокси для Telegram (РФ)

Прямой доступ к `api.telegram.org` из РФ заблокирован. Варианты:

- **Аренда зарубежного VPS** ($3–5/мес — Hetzner, Vultr, DO) и настройка SOCKS5 туннеля или squid http-proxy.
- **Платный SOCKS5/HTTP прокси** (например, [proxy6.net](https://proxy6.net), 50–150 ₽/мес за один прокси).
- **Облачный сервис** типа cloudflared / ssh-tunnel.

Формат для `.env`:
```
TELEGRAM_PROXY=http://user:pass@1.2.3.4:8080
# или
TELEGRAM_PROXY=socks5://user:pass@1.2.3.4:1080
```

Если решили без прокси — можно пока оставить пустым: уведомления будут падать, но платформа будет работать. Проверить работу прокси/токена можно через UI: **Админ → СУБД и Telegram → Тест отправки сообщения**.

---

## Шаг 3. Залить код на VPS

С локальной машины:

```bash
# Замените 1.2.3.4 на IP вашего VPS
scp -r /path/to/invest-platform root@1.2.3.4:/root/invest-platform
```

Или, если код в git-репозитории:

```bash
ssh root@1.2.3.4
git clone <ваш-репо> /root/invest-platform
```

## Шаг 4. Запустить install.sh

```bash
ssh root@1.2.3.4
cd /root/invest-platform
sudo bash deploy/install.sh
```

Скрипт пройдёт 9 шагов и в конце напечатает URL и инструкцию.

## Шаг 5. Заполнить `.env`

Скрипт сгенерировал `SECRET_KEY` сам, но вам нужно вписать реальные значения остального:

```bash
sudo nano /home/deploy/gruppa-titan/.env
```

Минимум, что заполнить:
- `ADMIN_PASSWORD` — придумайте сильный пароль (≥ 8 символов, заглавная + строчная + цифра)
- `TELEGRAM_BOT_TOKEN` — токен от BotFather
- `TELEGRAM_CHAT_ID` — id группы (со знаком «-» для групп)
- `TELEGRAM_PROXY` — адрес прокси

После правки:

```bash
sudo systemctl restart gruppa-titan
```

## Шаг 6. Открыть в браузере

```
http://<IP_сервера>/
```

Войти под `admin` / `<ADMIN_PASSWORD>`. **Сразу смените пароль через профиль.**

---

## Что именно настраивается

| Компонент | Что делает |
|-----------|------------|
| `gunicorn` | 3 worker'а на 127.0.0.1:5000 |
| `nginx` | reverse-proxy на :80, отдаёт static и /uploads |
| `redis` | хранилище rate-limiter'а (общее для всех worker'ов) |
| `systemd` | `gruppa-titan.service` — автостарт, рестарт при падении |
| `ufw` | firewall: 22, 80, 443 (остальное закрыто) |
| `cron` | ежедневный бэкап БД в 03:00 + уведомления о старте в 09:00 |

## Тестирование Telegram (после заполнения .env)

```bash
ssh root@1.2.3.4
cd /home/deploy/gruppa-titan
sudo -u deploy ./venv/bin/python -c "
from telegram_notify import test_connection
import os
ok, info = test_connection(os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_PROXY',''))
print('OK' if ok else 'FAIL', '—', info)
"
```

Или зайдите в админке: **Админ → СУБД и Telegram → «Отправить тестовое сообщение»**.

---

## Эксплуатация

```bash
# Статус и логи
sudo systemctl status gruppa-titan
sudo journalctl -u gruppa-titan -f
sudo tail -f /home/deploy/gruppa-titan/logs/error.log

# Перезапуск (после правки .env или кода)
sudo systemctl restart gruppa-titan

# Обновить код (с локалки)
rsync -avz --delete \
  --exclude venv --exclude instance --exclude .env \
  --exclude 'static/uploads' --exclude backups \
  /path/to/invest-platform/ root@1.2.3.4:/home/deploy/gruppa-titan/
ssh root@1.2.3.4 "chown -R deploy:deploy /home/deploy/gruppa-titan && \
  sudo -u deploy /home/deploy/gruppa-titan/venv/bin/pip install -r /home/deploy/gruppa-titan/requirements.txt && \
  sudo -u deploy /home/deploy/gruppa-titan/venv/bin/python /home/deploy/gruppa-titan/migrate.py && \
  systemctl restart gruppa-titan"

# Бэкапы — лежат в /home/deploy/gruppa-titan/backups/
ls -lh /home/deploy/gruppa-titan/backups/

# Восстановление БД — через UI: Админ → СУБД и Telegram → выберите снапшот
```

## HTTPS (когда появится домен)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d gruppa-titan.ru -d www.gruppa-titan.ru
# Certbot сам обновит /etc/nginx/sites-available/gruppa-titan и создаст таймер renewal

# В .env переключите cookie на secure:
sudo nano /home/deploy/gruppa-titan/.env  # SESSION_COOKIE_SECURE=true
sudo systemctl restart gruppa-titan
```

---

## Безопасность по умолчанию

- `ufw` оставляет открытыми только 22/80/443
- `nginx` скрывает версию (`server_tokens off`) и шлёт security headers
- `gunicorn` под непривилегированным `deploy`
- `systemd` юнит с `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`
- `.env` с правами `600` — никто, кроме `deploy`, его не прочтёт
- Flask отдаёт CSP, X-Frame-Options=DENY, HttpOnly+SameSite cookies
- Redis слушает только `127.0.0.1`, наружу не торчит
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
- `chat_id` для групп — отрицательный, не забудьте знак минус

**Сайт открывается, но статика не грузится:**
```bash
sudo nginx -t
sudo tail /var/log/nginx/error.log
ls -la /home/deploy/gruppa-titan/static/
```

**Забыли пароль админа:**
```bash
ssh root@1.2.3.4
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
