# InvestPlatform

Закрытая инвестиционная платформа для админов и приглашённых инвесторов. Flask + SQLAlchemy + Bootstrap, тёмная тема.

## Возможности

- Админ-панель: управление пользователями, сделками, инвестициями
- Два типа сделок: **инвестиционные** (с доходностью и пулом) и **срочная продажа** (без прибыльности)
- Гибкий срок инвестиции: до даты, в месяцах или в днях
- Калькулятор пропорциональной прибыли (`amount × pct/100 × days/365`)
- Дашборд с фильтрацией по периоду
- Загрузка фото сделок (множественная)
- Telegram-уведомления о новых заявках (с поддержкой прокси для РФ)
- Резервное копирование SQLite (горячий бэкап)
- bcrypt + CSRF + rate limiting

## Стек

- Python 3.12, Flask, SQLAlchemy, Flask-Login, Flask-WTF, Flask-Limiter
- SQLite (production-ready через горячий бэкап `sqlite3.backup()`)
- Bootstrap 5 + Font Awesome
- Gunicorn + Nginx (для деплоя)

## Быстрый старт (локально)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # заполнить SECRET_KEY и Telegram-токены
python app.py
```

Открой http://localhost:5000

**Админ по умолчанию:** создаётся при первом запуске из переменных окружения `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

## Конфигурация (.env)

```
SECRET_KEY=<длинный_случайный_ключ>
DATABASE_URL=sqlite:///instance/invest.db
TELEGRAM_BOT_TOKEN=<токен_бота>
TELEGRAM_CHAT_ID=<chat_id_группы>
TELEGRAM_PROXY=  # оставить пустым или socks5://user:pass@host:port
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<надёжный_пароль>
```

Для прокси (если бот заблокирован): `pip install pysocks`.

## Деплой на VPS

См. подробный гайд `deploy_guide.pdf`. Краткая схема:

1. Ubuntu 22.04+, Nginx, Python 3.12
2. `gunicorn` через systemd
3. Nginx reverse proxy на 127.0.0.1:5000
4. Раздача `/static/` и `/uploads/` напрямую через nginx
5. Daily DB backup через cron (`db_backup.py`)

## Структура

```
.
├── app.py                  # Маршруты Flask
├── models.py               # SQLAlchemy модели (User, Deal, Investment, AuditLog)
├── forms.py                # WTForms формы с валидацией
├── config.py               # Конфигурация
├── telegram_notify.py      # Telegram-интеграция
├── db_backup.py            # Бэкап БД
├── templates/              # Jinja2 шаблоны
├── static/
│   ├── css/style.css
│   ├── js/app.js
│   └── uploads/            # Загруженные фото (gitignored)
└── instance/invest.db      # SQLite (gitignored)
```

## Лицензия

Private — все права защищены.
