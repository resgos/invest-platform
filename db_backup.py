"""
Снапшоты (бэкапы) SQLite базы данных InvestPlatform.

Использование:
  - Из кода:     from db_backup import create_snapshot, list_snapshots, restore_snapshot
  - CLI:         python db_backup.py create
                 python db_backup.py list
                 python db_backup.py restore <filename>
  - Из админки:  /admin/backups

Снапшоты хранятся в BACKUP_DIR (по умолчанию ./backups/).
Формат имени: invest_YYYYMMDD_HHMMSS.db

Подключение к БД:
  SQLite — файловая СУБД, БД хранится в instance/invest.db
  Для просмотра/редактирования:
    - CLI:    sqlite3 instance/invest.db
    - GUI:    DB Browser for SQLite (https://sqlitebrowser.org/)
    - Python: import sqlite3; conn = sqlite3.connect('instance/invest.db')
"""

import os
import shutil
import sqlite3
import glob
from datetime import datetime, timezone

# Default paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'invest.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

# Max backups to keep (oldest auto-deleted)
MAX_BACKUPS = 30


def ensure_backup_dir():
    """Create backup directory if missing."""
    os.makedirs(BACKUP_DIR, exist_ok=True)


def create_snapshot(db_path=None, backup_dir=None, label=''):
    """
    Создаёт снапшот (горячий бэкап) SQLite базы.

    Использует sqlite3 .backup API — безопасно для активной БД
    (не нужно останавливать сервер).

    Returns: (filename, full_path, size_bytes)
    """
    db_path = db_path or DB_PATH
    backup_dir = backup_dir or BACKUP_DIR
    ensure_backup_dir()

    if not os.path.exists(db_path):
        raise FileNotFoundError(f'БД не найдена: {db_path}')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    suffix = f'_{label}' if label else ''
    filename = f'invest_{timestamp}{suffix}.db'
    backup_path = os.path.join(backup_dir, filename)

    # Use SQLite .backup() for hot backup (safe with active connections)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    size = os.path.getsize(backup_path)

    # Auto-cleanup: remove oldest if over limit
    _cleanup_old_backups(backup_dir)

    return filename, backup_path, size


def list_snapshots(backup_dir=None):
    """
    Возвращает список снапшотов отсортированных по дате (новые первые).

    Returns: list of dicts: {filename, path, size, created_at}
    """
    backup_dir = backup_dir or BACKUP_DIR
    ensure_backup_dir()

    files = glob.glob(os.path.join(backup_dir, 'invest_*.db'))
    snapshots = []

    for fp in files:
        stat = os.stat(fp)
        snapshots.append({
            'filename': os.path.basename(fp),
            'path': fp,
            'size': stat.st_size,
            'size_human': _human_size(stat.st_size),
            'created_at': datetime.fromtimestamp(stat.st_mtime),
        })

    snapshots.sort(key=lambda x: x['created_at'], reverse=True)
    return snapshots


def restore_snapshot(filename, db_path=None, backup_dir=None):
    """
    Восстанавливает БД из снапшота.

    ВНИМАНИЕ: перезаписывает текущую БД!
    Перед восстановлением автоматически создаётся бэкап текущей БД (pre_restore_*).

    Returns: (pre_restore_filename, restored_from)
    """
    db_path = db_path or DB_PATH
    backup_dir = backup_dir or BACKUP_DIR

    backup_path = os.path.join(backup_dir, filename)
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f'Снапшот не найден: {filename}')

    # Pre-restore backup
    pre_filename, _, _ = create_snapshot(db_path, backup_dir, label='pre_restore')

    # Restore using SQLite backup API
    src = sqlite3.connect(backup_path)
    dst = sqlite3.connect(db_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    return pre_filename, filename


def delete_snapshot(filename, backup_dir=None):
    """Удаляет снапшот."""
    backup_dir = backup_dir or BACKUP_DIR
    path = os.path.join(backup_dir, filename)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def get_db_info(db_path=None):
    """
    Возвращает информацию о текущей БД:
    таблицы, количество записей, размер файла.
    """
    db_path = db_path or DB_PATH
    if not os.path.exists(db_path):
        return None

    info = {
        'path': db_path,
        'size': os.path.getsize(db_path),
        'size_human': _human_size(os.path.getsize(db_path)),
        'tables': [],
    }

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        for table in tables:
            cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
            count = cursor.fetchone()[0]

            # Get columns
            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = [{'name': row[1], 'type': row[2], 'pk': bool(row[5])} for row in cursor.fetchall()]

            info['tables'].append({
                'name': table,
                'rows': count,
                'columns': columns,
            })

        conn.close()
    except Exception as e:
        info['error'] = str(e)

    return info


def _cleanup_old_backups(backup_dir):
    """Remove oldest backups if count exceeds MAX_BACKUPS."""
    files = glob.glob(os.path.join(backup_dir, 'invest_*.db'))
    if len(files) <= MAX_BACKUPS:
        return

    files_with_time = [(f, os.path.getmtime(f)) for f in files]
    files_with_time.sort(key=lambda x: x[1])

    to_remove = len(files) - MAX_BACKUPS
    for fp, _ in files_with_time[:to_remove]:
        try:
            os.remove(fp)
        except OSError:
            pass


def _human_size(size_bytes):
    """Convert bytes to human-readable size."""
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if abs(size_bytes) < 1024:
            return f'{size_bytes:.1f} {unit}'
        size_bytes /= 1024
    return f'{size_bytes:.1f} ТБ'


# ─── CLI ───
if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print('Использование:')
        print('  python db_backup.py create          — создать снапшот')
        print('  python db_backup.py list             — список снапшотов')
        print('  python db_backup.py restore <file>   — восстановить из снапшота')
        print('  python db_backup.py info             — информация о БД')
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'create':
        fn, path, size = create_snapshot()
        print(f'✅ Снапшот создан: {fn} ({_human_size(size)})')

    elif cmd == 'list':
        snaps = list_snapshots()
        if not snaps:
            print('Нет снапшотов')
        else:
            print(f'Всего снапшотов: {len(snaps)}\n')
            for s in snaps:
                print(f'  {s["filename"]:40s}  {s["size_human"]:>10s}  {s["created_at"].strftime("%d.%m.%Y %H:%M:%S")}')

    elif cmd == 'restore':
        if len(sys.argv) < 3:
            print('Укажите имя файла: python db_backup.py restore invest_20260324_120000.db')
            sys.exit(1)
        fn = sys.argv[2]
        pre, restored = restore_snapshot(fn)
        print(f'✅ БД восстановлена из {restored}')
        print(f'   Предыдущее состояние сохранено: {pre}')

    elif cmd == 'info':
        info = get_db_info()
        if not info:
            print('БД не найдена')
            sys.exit(1)
        print(f'Путь: {info["path"]}')
        print(f'Размер: {info["size_human"]}')
        print(f'\nТаблицы:')
        for t in info['tables']:
            print(f'  {t["name"]:30s}  {t["rows"]:>8d} записей  ({len(t["columns"])} колонок)')
            for c in t['columns']:
                pk = ' [PK]' if c['pk'] else ''
                print(f'    {c["name"]:25s}  {c["type"]}{pk}')
    else:
        print(f'Неизвестная команда: {cmd}')
        sys.exit(1)
