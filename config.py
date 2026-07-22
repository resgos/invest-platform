import os
from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _persistent_secret_key():
    """SECRET_KEY из env, иначе — из instance/secret_key (генерируется один раз).

    Случайный ключ в памяти ломает сессии при рестарте и между gunicorn-воркерами
    (каждый форк получил бы свой ключ) — поэтому недостающий ключ сохраняем в файл.
    """
    key = os.getenv('SECRET_KEY')
    if key:
        return key
    instance_dir = os.path.join(_BASE_DIR, 'instance')
    key_file = os.path.join(instance_dir, 'secret_key')
    try:
        if os.path.exists(key_file):
            with open(key_file, 'r', encoding='utf-8') as f:
                stored = f.read().strip()
            if stored:
                return stored
        os.makedirs(instance_dir, exist_ok=True)
        new_key = os.urandom(64).hex()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(new_key)
        return new_key
    except FileExistsError:
        # Параллельный воркер успел первым — читаем его ключ
        with open(key_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        # ФС недоступна на запись — деградируем на случайный ключ (dev-режим)
        return os.urandom(64).hex()


class Config:
    SECRET_KEY = _persistent_secret_key()
    # Исторически .env.example документировал DATABASE_URL, а код читал DATABASE_URI —
    # поддерживаем оба имени (URL приоритетнее как документированный).
    # Дефолт — АБСОЛЮТНЫЙ путь: относительный sqlite-путь разъезжается между
    # процессами (веб и tg_bot) при разном CWD/способе запуска.
    SQLALCHEMY_DATABASE_URI = (os.getenv('DATABASE_URL')
                               or os.getenv('DATABASE_URI')
                               or 'sqlite:///' + os.path.join(_BASE_DIR, 'instance', 'invest.db').replace(os.sep, '/'))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }

    # Uploads
    UPLOAD_FOLDER = os.path.join(_BASE_DIR, 'static', 'uploads')
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB

    # Session security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() in ('true', '1', 'yes')
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Security
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600

    # Rate limiting
    MAX_LOGIN_ATTEMPTS = int(os.getenv('MAX_LOGIN_ATTEMPTS', '5'))
    LOCKOUT_MINUTES = int(os.getenv('LOCKOUT_MINUTES', '15'))

    # Admin defaults
    ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'Admin@Secure2026!')
    ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', 'admin@gruppa-titan.ru')

    # Telegram notifications (группа админов)
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    # Прокси для Telegram (в РФ API заблокировано)
    # Форматы: http://user:pass@host:port, socks5://user:pass@host:port
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY', '')

    # Telegram-бот для инвесторов (отдельный процесс tg_bot.py).
    # По умолчанию использует тот же токен, что и уведомления в группу;
    # можно завести отдельного бота через TELEGRAM_USER_BOT_TOKEN.
    TELEGRAM_USER_BOT_TOKEN = os.getenv('TELEGRAM_USER_BOT_TOKEN', '') or os.getenv('TELEGRAM_BOT_TOKEN', '')
    # Период проверки outbox-уведомлений ботом, сек
    BOT_OUTBOX_INTERVAL = int(os.getenv('BOT_OUTBOX_INTERVAL', '5'))
    # Таймаут long-poll getUpdates, сек
    BOT_POLL_TIMEOUT = int(os.getenv('BOT_POLL_TIMEOUT', '25'))
    # Публичный URL платформы — для ссылок на сделки в сообщениях бота
    # (пусто = ссылки не добавляются)
    SITE_URL = os.getenv('SITE_URL', '').rstrip('/')

    # Rate limiter storage (Redis для multi-worker prod, иначе in-memory)
    RATE_LIMIT_STORAGE_URI = os.getenv('RATE_LIMIT_STORAGE_URI', 'memory://')

    # Деплой / бэкапы
    DEPLOY_DIR = os.getenv('DEPLOY_DIR', 'gruppa-titan')
