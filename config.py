import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', os.urandom(64).hex())
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URI', 'sqlite:///invest.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
    }

    # Uploads
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
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

    # Telegram notifications
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    # Прокси для Telegram (в РФ API заблокировано)
    # Форматы: http://user:pass@host:port, socks5://user:pass@host:port
    TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY', '')

    # Rate limiter storage (Redis для multi-worker prod, иначе in-memory)
    RATE_LIMIT_STORAGE_URI = os.getenv('RATE_LIMIT_STORAGE_URI', 'memory://')

    # Деплой / бэкапы
    DEPLOY_DIR = os.getenv('DEPLOY_DIR', 'gruppa-titan')
