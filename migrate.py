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
from datetime import datetime, timedelta
from contextlib import closing


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
        (version, datetime.utcnow().isoformat()),
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


MIGRATIONS = [
    ('001_investment_dates', m_001_investment_dates),
    ('002_deal_date_start',  m_002_deal_date_start),
    ('003_investment_manual_profit', m_003_investment_manual_profit),
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
