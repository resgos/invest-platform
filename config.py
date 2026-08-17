import os
import time
from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INSTANCE_DIR = os.path.join(_BASE_DIR, 'instance')

# Каталог instance/ нужен ВСЕМ процессам (веб, tg_bot, CLI) до первого обращения
# к SQLite/secret_key — создаём при импорте конфига, а не как побочный эффект.
try:
    os.makedirs(_INSTANCE_DIR, exist_ok=True)
except OSError:
    pass


def _int_env(name, default):
    """int() из env с fallback: опечатка в переменной не должна ронять процесс."""
    raw = os.getenv(name, '')
    try:
        return int(raw) if raw else default
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            f'{name}={raw!r} — не число, использую дефолт {default}')
        return default


def _normalize_db_uri(uri):
    """Относительный sqlite-путь превращаем в абсолютный от корня проекта.

    Иначе Flask-SQLAlchemy резолвит его от instance_path (документированный
    в .env.example `sqlite:///instance/invest.db` превратился бы в
    instance/instance/invest.db), а процессы с разным CWD/способом запуска
    могли бы смотреть в разные файлы.
    """
    prefix = 'sqlite:///'
    if uri and uri.startswith(prefix):
        path = uri[len(prefix):]
        if path and not os.path.isabs(path) and not path.startswith(':memory:'):
            return prefix + os.path.join(_BASE_DIR, path).replace(os.sep, '/')
    return uri


def _persistent_secret_key():
    """SECRET_KEY из env, иначе — из instance/secret_key (генерируется один раз).

    Случайный ключ в памяти ломает сессии при рестарте и между gunicorn-воркерами
    (каждый форк получил бы свой ключ) — поэтому недостающий ключ сохраняем в файл.

    Публикация нового ключа — атомарная: содержимое сначала полностью пишется
    (+fsync) во временный файл в ТОМ ЖЕ каталоге, и только потом «публикуется»
    под именем secret_key через os.link (атомарная семантика «только если ещё
    не существует» — FileExistsError, если нас опередил другой процесс/воркер).
    Поэтому имя secret_key снаружи никогда не наблюдается пустым: либо файла ещё
    нет, либо в нём сразу лежит целый ключ. Раньше между os.open(O_CREAT) и
    записью было окно, в которое параллельный воркер мог увидеть файл пустым,
    удалить его (remove работает и при чужом открытом fd) и создать свой —
    воркеры расходились по SECRET_KEY. Ветка удаления пустого файла ниже
    остаётся только как fallback для файлов-сирот от старых версий/чужого краша
    (сама «пустота» больше не создаётся этим кодом).
    """
    key = os.getenv('SECRET_KEY')
    if key:
        return key
    key_file = os.path.join(_INSTANCE_DIR, 'secret_key')

    def _read_key():
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            return ''

    stored = _read_key()
    if stored:
        return stored

    # Легаси-фоллбек: файл есть, но пуст (сирота от краша до этого фикса).
    # Перепроверяем размер прямо перед удалением — сужает окно гонки, но не
    # закрывает его целиком (TOCTOU), поэтому это запасной путь, а не основная
    # защита — основная защита ниже, публикация файла атомарна и никогда не
    # создаёт наблюдаемое пустое состояние.
    if os.path.exists(key_file):
        try:
            if os.stat(key_file).st_size == 0:
                os.remove(key_file)
        except OSError:
            pass

    new_key = os.urandom(64).hex()
    tmp_path = os.path.join(_INSTANCE_DIR, f'.secret_key.tmp-{os.getpid()}-{new_key[:16]}')
    published = False
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(new_key)
            f.flush()
            os.fsync(f.fileno())
        try:
            # Атомарно публикуем ТОЛЬКО если нас не опередили
            os.link(tmp_path, key_file)
            published = True
        except FileExistsError:
            pass  # кто-то создал key_file первым — прочитаем его ниже
        except OSError:
            # ФС не поддерживает hardlink (cross-device, некоторые сетевые ФС) —
            # атомарная публикация без анти-гонки "только если не существует",
            # но всё ещё без промежуточного пустого состояния
            os.replace(tmp_path, key_file)
            published = True
    except OSError:
        # ФС недоступна на запись вовсе — деградируем на случайный ключ (dev-режим)
        return new_key
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if published:
        return new_key

    # Нас опередили (os.link → FileExistsError) — ждём, пока чужой ключ станет
    # виден. Окно увеличено с 1с (20×0.05) до 5с (100×0.05): на медленном VPS
    # секунды не хватало и воркер молча генерил собственный ключ.
    for _ in range(100):
        stored = _read_key()
        if stored:
            return stored
        time.sleep(0.05)

    import logging
    logging.getLogger(__name__).warning(
        'secret_key: не дождались записи параллельным процессом за 5с — '
        'генерирую собственный ключ (возможен разъезд SECRET_KEY между воркерами)')
    return new_key


class Config:
    SECRET_KEY = _persistent_secret_key()
    # Исторически .env.example документировал DATABASE_URL, а код читал DATABASE_URI —
    # поддерживаем оба имени (URL приоритетнее как документированный).
    # Относительные sqlite-пути нормализуются к корню проекта (см. _normalize_db_uri),
    # дефолт — абсолютный instance/invest.db.
    SQLALCHEMY_DATABASE_URI = _normalize_db_uri(
        os.getenv('DATABASE_URL')
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
    BOT_OUTBOX_INTERVAL = _int_env('BOT_OUTBOX_INTERVAL', 5)
    # Таймаут long-poll getUpdates, сек
    BOT_POLL_TIMEOUT = _int_env('BOT_POLL_TIMEOUT', 25)
    # Публичный URL платформы — для ссылок на сделки в сообщениях бота
    # (пусто = ссылки не добавляются)
    SITE_URL = os.getenv('SITE_URL', '').rstrip('/')

    # Массовая рассылка анонсов сделок инвесторам («Новая сделка для вас»,
    # «Сделка скоро стартует»). ВЫКЛЮЧЕНА по умолчанию: включение бота не должно
    # само по себе начать рассылку всем подключённым. Персональные события по
    # собственным заявкам инвестора приходят независимо от этого флага.
    BOT_DEAL_BROADCAST = os.getenv('BOT_DEAL_BROADCAST', 'false').lower() in ('true', '1', 'yes')

    # Rate limiter storage (Redis для multi-worker prod, иначе in-memory)
    RATE_LIMIT_STORAGE_URI = os.getenv('RATE_LIMIT_STORAGE_URI', 'memory://')

    # Деплой / бэкапы
    DEPLOY_DIR = os.getenv('DEPLOY_DIR', 'gruppa-titan')
