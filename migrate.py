"""
Миграции схемы БД (Группа Титан).

Идемпотентные шаги, выполняются по порядку. Каждый шаг проверяет фактическое
состояние схемы перед изменением — повторный запуск безопасен.

Применённые версии хранятся в таблице `schema_migrations(version, applied_at)`.

Использование:
    python migrate.py            # применить все недостающие миграции
    python migrate.py --status   # показать список применённых
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from contextlib import closing

# Windows-консоль (cp1251/cp866) падает на '✓' — печатаем в UTF-8 с заменой
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'invest.db')


def col_exists(c: sqlite3.Cursor, table: str, column: str) -> bool:
    c.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in c.fetchall())


def table_exists(c: sqlite3.Cursor, table: str) -> bool:
    c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return c.fetchone() is not None


def ensure_migrations_table(c: sqlite3.Cursor) -> None:
    c.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)


def already_applied(c: sqlite3.Cursor, version: str) -> bool:
    c.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
    return c.fetchone() is not None


def mark_applied(c: sqlite3.Cursor, version: str) -> None:
    c.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat()),
    )


# ─────────────────────────── Migrations ───────────────────────────

def m_001_investment_dates(c: sqlite3.Cursor) -> None:
    """Добавляет date_start/date_end в investments и заполняет их для существующих."""
    if not table_exists(c, 'investments'):
        return  # БД свежая, db.create_all() создаст всё

    if not col_exists(c, 'investments', 'date_start'):
        c.execute("ALTER TABLE investments ADD COLUMN date_start DATE")
    if not col_exists(c, 'investments', 'date_end'):
        c.execute("ALTER TABLE investments ADD COLUMN date_end DATE")

    # Заполнить date_start для уже существующих инвестиций из invested_at
    c.execute("""
        UPDATE investments
        SET date_start = DATE(invested_at)
        WHERE date_start IS NULL AND invested_at IS NOT NULL AND status IN ('active','closed')
    """)

    # date_end по настройкам Deal (если можно вычислить)
    c.execute("""
        SELECT i.id, i.date_start, d.date_end, d.investment_term_months
        FROM investments i JOIN deals d ON d.id = i.deal_id
        WHERE i.date_end IS NULL AND i.date_start IS NOT NULL
    """)
    for inv_id, inv_start, deal_end, term_months in c.fetchall():
        if not inv_start:
            continue
        try:
            start = datetime.fromisoformat(inv_start).date() if isinstance(inv_start, str) else inv_start
        except ValueError:
            continue
        candidates = []
        if deal_end:
            try:
                candidates.append(
                    datetime.fromisoformat(deal_end).date() if isinstance(deal_end, str) else deal_end
                )
            except ValueError:
                pass
        if term_months:
            candidates.append(start + timedelta(days=int(term_months) * 30))
        if candidates:
            c.execute(
                "UPDATE investments SET date_end = ? WHERE id = ?",
                (min(candidates).isoformat(), inv_id),
            )


def m_002_deal_date_start(c: sqlite3.Cursor) -> None:
    """Добавляет фиксированную дату старта сделки + индекс."""
    if not table_exists(c, 'deals'):
        return
    if not col_exists(c, 'deals', 'date_start'):
        c.execute("ALTER TABLE deals ADD COLUMN date_start DATE")
    c.execute("CREATE INDEX IF NOT EXISTS ix_deals_date_start ON deals (date_start)")


def m_003_investment_manual_profit(c: sqlite3.Cursor) -> None:
    """Флаг ручной установки expected_profit — чтобы не перезатирался pro-rata формулой."""
    if not table_exists(c, 'investments'):
        return
    if not col_exists(c, 'investments', 'expected_profit_manual'):
        c.execute(
            "ALTER TABLE investments ADD COLUMN expected_profit_manual BOOLEAN DEFAULT 0"
        )


def m_004_deal_market_value(c: sqlite3.Cursor) -> None:
    """Рыночная стоимость объекта (для расчёта КЗ — коэф. займа)."""
    if not table_exists(c, 'deals'):
        return
    if not col_exists(c, 'deals', 'market_value'):
        c.execute("ALTER TABLE deals ADD COLUMN market_value FLOAT")


def m_005_telegram_bot(c: sqlite3.Cursor) -> None:
    """Телеграм-бот: users.telegram_id + уникальный индекс.

    Таблицы bot_filters/bot_notifications создаёт db.create_all() при старте
    приложения (для новых таблиц ALTER не нужен) — здесь только колонка в users.
    """
    if not table_exists(c, 'users'):
        return
    if not col_exists(c, 'users', 'telegram_id'):
        c.execute("ALTER TABLE users ADD COLUMN telegram_id BIGINT")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_id "
        "ON users (telegram_id) WHERE telegram_id IS NOT NULL"
    )


def m_006_car_details(c: sqlite3.Cursor) -> None:
    """Расширенные характеристики авто (по образцу Авто.ру/Дром): кузов, объём,
    привод, руль, владельцы, состояние, ПТС, цвет, VIN."""
    if not table_exists(c, 'deals'):
        return
    columns = [
        ('car_body', 'VARCHAR(30)'),
        ('car_engine_volume', 'FLOAT'),
        ('car_drive', 'VARCHAR(10)'),
        ('car_wheel', 'VARCHAR(10)'),
        ('car_owners', 'INTEGER'),
        ('car_condition', 'VARCHAR(20)'),
        ('car_pts', 'VARCHAR(12)'),
        ('car_color', 'VARCHAR(30)'),
        ('car_vin', 'VARCHAR(20)'),
    ]
    for name, ddl_type in columns:
        if not col_exists(c, 'deals', name):
            c.execute(f"ALTER TABLE deals ADD COLUMN {name} {ddl_type}")


def m_007_deal_starting_horizon(c: sqlite3.Cursor) -> None:
    """Переводит исторический event_type='deal_starting' в формат с горизонтом.

    До этого коммита ключ дедупа bot_notifications для «сделка скоро стартует»
    был единым 'deal_starting' для всех горизонтов; теперь горизонт входит в
    event_type ('deal_starting_3', 'deal_starting_1', ...), см. EVENT_DEAL_STARTING
    и emit_deal_starting() в bot_events.py. Без этой миграции первый же прогон
    notify_upcoming.py по прежнему горизонту не найдёт строку 'deal_starting_3'
    (дедуп ищет по новому имени), создаст новую и пользователь получит повторное
    «Сделка скоро стартует» по уже отработанной сделке.

    Горизонт исторических строк нигде не сохранялся (event_type был один на все
    горизонты) — восстановить его точно нельзя. Выбор: помечаем историю дефолтным
    горизонтом крона notify_upcoming.py (--days по умолчанию 3, см. notify_upcoming.py)
    — это тот горизонт, с которым cron реально запускается в проде чаще всего,
    так что для подавляющего большинства строк переименование окажется точным,
    а не просто "любым правдоподобным". Для меньшинства, где исторически
    использовался другой --days, дубля всё равно не будет: emit_deal_starting
    с ЭТИМ горизонтом создаст СВОЙ event_type (deal_starting_1 и т.п.) — по
    определению не то же самое напоминание, что 'сделка стартует через 3 дня',
    так что дедуп по горизонту 3 их не касается и не обязан их ловить.

    Страховка от дублей: если для пары (user_id, deal_id) уже существует строка
    'deal_starting_3' (redeploy успел прогнать notify_upcoming.py ДО этой
    миграции и создал новую строку рядом со старой) — простое переименование
    задело бы уникальный индекс uq_bot_notif_user_deal_event (user_id, deal_id,
    event_type; partial WHERE investment_id IS NULL). В этом случае старая
    'deal_starting'-строка избыточна (новая 'deal_starting_3' уже отражает
    актуальное состояние отправки) и просто удаляется, а не оставляется
    недостижимым хвостом под именем, которое код больше никогда не создаст
    и не продедупит.
    """
    if not table_exists(c, 'bot_notifications'):
        return  # свежая БД — db.create_all() создаст таблицу уже в новом формате
    if not col_exists(c, 'bot_notifications', 'event_type'):
        return  # неожиданная схема — нечего мигрировать

    c.execute("SELECT id FROM bot_notifications WHERE event_type = 'deal_starting'")
    legacy_ids = [row[0] for row in c.fetchall()]
    for row_id in legacy_ids:
        try:
            c.execute(
                "UPDATE bot_notifications SET event_type = 'deal_starting_3' WHERE id = ?",
                (row_id,),
            )
        except sqlite3.IntegrityError:
            # Конфликт с уже существующей 'deal_starting_3' для той же пары
            # (user_id, deal_id) — старая строка избыточна, удаляем её.
            # SQLite откатывает только упавший statement, не всю транзакцию,
            # поэтому продолжать цикл после этой ошибки безопасно.
            c.execute("DELETE FROM bot_notifications WHERE id = ?", (row_id,))


MIGRATIONS = [
    ('001_investment_dates', m_001_investment_dates),
    ('002_deal_date_start',  m_002_deal_date_start),
    ('003_investment_manual_profit', m_003_investment_manual_profit),
    ('004_deal_market_value', m_004_deal_market_value),
    ('005_telegram_bot', m_005_telegram_bot),
    ('006_car_details', m_006_car_details),
    ('007_deal_starting_horizon', m_007_deal_starting_horizon),
]


# ─────────────────────────── Runner ───────────────────────────

def run(status_only: bool = False) -> int:
    if not os.path.exists(DB_PATH):
        print(f'БД не найдена: {DB_PATH}. Запустите app.py — db.create_all() создаст таблицы по моделям.')
        return 0

    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        ensure_migrations_table(c)

        if status_only:
            c.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            rows = c.fetchall()
            if not rows:
                print('Нет применённых миграций.')
            else:
                for v, t in rows:
                    print(f'  ✓ {v}  ({t})')
            return 0

        applied_now = []
        for version, fn in MIGRATIONS:
            if already_applied(c, version):
                print(f'  • {version} — уже применена')
                continue
            try:
                fn(c)
                mark_applied(c, version)
                conn.commit()
                applied_now.append(version)
                print(f'  ✓ {version} — применена')
            except Exception as e:
                conn.rollback()
                print(f'  ✗ {version} — ошибка: {e}', file=sys.stderr)
                return 1

        if not applied_now:
            print('Все миграции актуальны.')
        else:
            print(f'\nПрименено миграций: {len(applied_now)}')
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='БД-миграции Группы Титан')
    parser.add_argument('--status', action='store_true', help='Показать применённые миграции')
    args = parser.parse_args()
    sys.exit(run(status_only=args.status))
