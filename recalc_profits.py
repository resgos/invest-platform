"""
One-time script: пересчёт expected_profit для всех инвестиций по pro-rata.

Уважает флаг expected_profit_manual — если админ задал прибыль вручную, она
не перезатирается. Бессрочные сделки (без date_end) получают ep=0.

Запуск:
    python recalc_profits.py            # обычный режим
    python recalc_profits.py --dry-run  # показать что бы поменялось, не сохранять
    python recalc_profits.py --force    # пересчитать включая manual (опасно!)
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app  # noqa: E402
from models import db, Investment, Deal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='Пересчёт expected_profit')
    parser.add_argument('--dry-run', action='store_true', help='Не сохранять изменения')
    parser.add_argument('--force', action='store_true',
                        help='Пересчитать в том числе manual-инвестиции (по умолчанию — пропускаются)')
    args = parser.parse_args()

    with app.app_context():
        investments = Investment.query.all()
        print(f'Найдено инвестиций: {len(investments)}\n')

        changed = 0
        skipped_manual = 0
        for inv in investments:
            deal = Deal.query.get(inv.deal_id)
            if not deal:
                print(f'  [SKIP] inv #{inv.id} — нет связанной сделки')
                continue
            if inv.expected_profit_manual and not args.force:
                skipped_manual += 1
                continue

            old_ep = inv.expected_profit or 0

            if inv.date_start and inv.date_end:
                actual_days = max((inv.date_end - inv.date_start).days, 1)
                new_ep = inv.amount * (deal.expected_profit_pct / 100) * (actual_days / 365)
            elif inv.date_end and inv.date_start is None:
                # fallback: считаем от invested_at-даты, если она есть
                from datetime import date
                base = inv.invested_at.date() if inv.invested_at else date.today()
                actual_days = max((inv.date_end - base).days, 1)
                new_ep = inv.amount * (deal.expected_profit_pct / 100) * (actual_days / 365)
            else:
                # бессрочная — прибыль остаётся за админом
                new_ep = 0

            new_ep = round(new_ep, 2)
            if abs(new_ep - old_ep) < 0.01:
                continue

            print(f'  inv #{inv.id}: deal="{deal.title}", amount={inv.amount:,.0f}, '
                  f'pct={deal.expected_profit_pct}% → ep: {old_ep:,.2f} → {new_ep:,.2f}')
            if not args.dry_run:
                inv.expected_profit = new_ep
            changed += 1

        if not args.dry_run:
            db.session.commit()

        print(f'\nИзменено: {changed}, пропущено manual: {skipped_manual}'
              f'{"  (DRY RUN — изменения не сохранены)" if args.dry_run else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
