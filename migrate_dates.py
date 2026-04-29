"""
Migration script: Add date_start/date_end to deals table,
migrate existing investment_term_months data,
add 'closed' status support for deals.
"""
import sqlite3
from datetime import datetime, timedelta

DB_PATH = 'instance/invest.db'

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Check if columns exist already
c.execute("PRAGMA table_info(deals)")
columns = [col[1] for col in c.fetchall()]

if 'date_start' not in columns:
    c.execute("ALTER TABLE deals ADD COLUMN date_start DATE")
    print("Added date_start column")

if 'date_end' not in columns:
    c.execute("ALTER TABLE deals ADD COLUMN date_end DATE")
    print("Added date_end column")

# Migrate existing data: use created_at as date_start, computed date_end from investment_term_months
c.execute("SELECT id, created_at, investment_term_months FROM deals WHERE date_start IS NULL")
rows = c.fetchall()
for row in rows:
    deal_id, created_at_str, term_months = row
    if created_at_str:
        try:
            # Handle various datetime formats
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    created_dt = datetime.strptime(created_at_str.split('+')[0].strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                created_dt = datetime.now()
        except Exception:
            created_dt = datetime.now()
    else:
        created_dt = datetime.now()

    date_start = created_dt.date()
    term = term_months if term_months else 12
    date_end = (created_dt + timedelta(days=term * 30)).date()

    c.execute("UPDATE deals SET date_start = ?, date_end = ? WHERE id = ?",
              (date_start.isoformat(), date_end.isoformat(), deal_id))
    print(f"Migrated deal #{deal_id}: {date_start} -> {date_end} ({term} months)")

conn.commit()
conn.close()
print("\nMigration complete!")
