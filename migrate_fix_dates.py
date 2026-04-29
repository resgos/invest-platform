"""
Migration: Move dates from Deal to Investment level.

- Add date_start, date_end columns to investments table
- Keep deals.date_end (used for deal-level end date)
- Keep deals.investment_term_months (used for term in months)
- deals.date_start is no longer used by code but left in DB to avoid data loss
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'instance', 'invest.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check existing columns in investments
    c.execute("PRAGMA table_info(investments)")
    inv_cols = [row[1] for row in c.fetchall()]
    print(f"Current investments columns: {inv_cols}")

    # Add date_start to investments if not exists
    if 'date_start' not in inv_cols:
        c.execute("ALTER TABLE investments ADD COLUMN date_start DATE")
        print("Added date_start to investments")
    else:
        print("date_start already exists in investments")

    # Add date_end to investments if not exists
    if 'date_end' not in inv_cols:
        c.execute("ALTER TABLE investments ADD COLUMN date_end DATE")
        print("Added date_end to investments")
    else:
        print("date_end already exists in investments")

    # Check deals columns
    c.execute("PRAGMA table_info(deals)")
    deal_cols = [row[1] for row in c.fetchall()]
    print(f"Current deals columns: {deal_cols}")

    # For existing active investments, set date_start from invested_at
    c.execute("""
        UPDATE investments
        SET date_start = DATE(invested_at)
        WHERE date_start IS NULL AND invested_at IS NOT NULL AND status IN ('active', 'closed')
    """)
    updated = c.rowcount
    print(f"Set date_start for {updated} existing investments from invested_at")

    # For existing investments, calculate date_end from deal settings
    c.execute("""
        SELECT i.id, i.date_start, d.date_end as deal_date_end, d.investment_term_months
        FROM investments i
        JOIN deals d ON i.deal_id = d.id
        WHERE i.date_end IS NULL AND i.date_start IS NOT NULL
    """)
    rows = c.fetchall()
    for inv_id, inv_start, deal_end, term_months in rows:
        end_date = None
        if deal_end and term_months:
            # Both: use earlier
            from dateutil.relativedelta import relativedelta
            from datetime import date as dt_date
            inv_start_d = dt_date.fromisoformat(inv_start) if isinstance(inv_start, str) else inv_start
            end_b = inv_start_d + relativedelta(months=term_months)
            deal_end_d = dt_date.fromisoformat(deal_end) if isinstance(deal_end, str) else deal_end
            end_date = min(deal_end_d, end_b).isoformat()
        elif deal_end:
            end_date = deal_end
        elif term_months and inv_start:
            from dateutil.relativedelta import relativedelta
            from datetime import date as dt_date
            inv_start_d = dt_date.fromisoformat(inv_start) if isinstance(inv_start, str) else inv_start
            end_date = (inv_start_d + relativedelta(months=term_months)).isoformat()

        if end_date:
            c.execute("UPDATE investments SET date_end = ? WHERE id = ?", (end_date, inv_id))
            print(f"  Investment #{inv_id}: date_end = {end_date}")

    conn.commit()
    conn.close()
    print("\nMigration complete!")


if __name__ == '__main__':
    migrate()
