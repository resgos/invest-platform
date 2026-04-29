"""One-time script: recalculate expected_profit for all investments using pro-rata formula."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from models import db, Investment, Deal

app = create_app()

with app.app_context():
    investments = Investment.query.all()
    print(f"Found {len(investments)} investments to recalculate\n")

    for inv in investments:
        deal = Deal.query.get(inv.deal_id)
        if not deal:
            print(f"  [SKIP] inv #{inv.id} — no deal found")
            continue

        old_ep = inv.expected_profit

        if inv.date_start and inv.date_end:
            actual_days = max((inv.date_end - inv.date_start).days, 1)
        elif inv.date_end:
            # fallback: use today as start
            from datetime import date
            actual_days = max((inv.date_end - date.today()).days, 1)
        else:
            actual_days = 365  # open-ended

        new_ep = inv.amount * (deal.expected_profit_pct / 100) * (actual_days / 365)
        inv.expected_profit = round(new_ep, 2)

        print(f"  inv #{inv.id}: deal='{deal.title}', amount={inv.amount:,.0f}, pct={deal.expected_profit_pct}%, "
              f"days={actual_days}, old_ep={old_ep:,.2f}, new_ep={new_ep:,.2f}")

    db.session.commit()
    print("\nDone. All expected_profit values recalculated.")
