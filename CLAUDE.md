# gruppa-titan («Группа Титан») — заметки для агента

Flask-инвест-платформа, репо `resgos/invest-platform`. Подробности о фичах — `README.md`,
деплой — `deploy/README.md`.

## Команды (Windows, локальный клон `Downloads\invest-platform`)

- Тесты: `venv\Scripts\python -m unittest discover -s tests` (158 шт., in-memory SQLite, боевую БД не трогают)
- Локальный запуск: `venv\Scripts\python app.py` → http://localhost:5000
- Миграции: `venv\Scripts\python migrate.py` (идемпотентны, сверяют схему; `--status` — список)

## Где живёт прод

| Что | Где |
|---|---|
| Сайт | https://titan.93.77.165.109.sslip.io (Let's Encrypt, автопродление certbot) |
| Сервер | `resgosss@93.77.165.109`, ssh-алиас `stand-giganetwork` — **общий** сервер (чужие giga-*, olt, arkham) |
| SSH | только через SOCKS: перед `ssh`/`scp` экспортировать `SOCKS5_USER`/`SOCKS5_PASSWD` (иначе banner exchange timeout) |
| Код | `/home/deploy/gruppa-titan` (пользователь `deploy`), venv там же |
| Службы | `gruppa-titan` (gunicorn 127.0.0.1:5000, 3 воркера), `gruppa-titan-bot` (tg_bot.py), redis 127.0.0.1:6379 |
| nginx | сайт `/etc/nginx/sites-available/gruppa-titan`, именованный (без default_server), под certbot |
| БД | SQLite `instance/invest.db`, ночной бэкап cron 03:00 в `backups/` |

Старый прод `111.88.146.209` (titan.111-88-146-209.sslip.io) с 11.2026 не отвечает.

## Выкат

Только через git: push в ветку → на сервере
`sudo REPO_BRANCH=<ветка> bash /home/deploy/gruppa-titan/deploy/update.sh`
(снапшот БД → reset на origin → pip → миграции → рестарт). Первичная установка на общий сервер:
`sudo SHARED_HOST=1 DOMAIN=<домен> bash deploy/install.sh` (не трогает ufw и чужой nginx).

## Грабли

- `ufw enable` на общем сервере закроет порты соседей — только `SHARED_HOST=1`.
- `apt-get install` на сервере через needrestart перезапускает затронутые службы (coturn архэма, cron) — после установки пакетов проверить соседей.
- Telegram API с сервера закрыт → `TELEGRAM_PROXY` нужен. **Использовать `http://…` прокси, не `socks5://`:**
  ветка SOCKS в `telegram_notify._get_proxy_opener` подменяет `socket.socket` глобально в процессе —
  в веб-воркере после первой отправки и подключения к redis пойдут через удалённый прокси.
- За nginx обязателен `TRUSTED_PROXIES=1` в `.env`, иначе лимитер видит всех клиентов как 127.0.0.1.
- `DATABASE_URI` — абсолютный sqlite-путь с 4 слешами, иначе процессы разъезжаются по CWD.
- `update.sh`, обновивший сам себя, применяет новые правки только со следующего запуска.
- Inline `<script>` в шаблонах нельзя (CSP `script-src 'self'`) — JS в `static/`.
- `git show` на Windows показывает CRLF (autocrlf), сами блобы в LF — bash-скрипты на сервере в порядке.
