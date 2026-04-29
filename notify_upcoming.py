"""
CLI-скрипт: рассылает в Telegram уведомления о сделках, стартующих в ближайшие дни.

Запускается в cron раз в сутки, например утром:
    0 9 * * * cd /home/deploy/gruppa-titan && /home/deploy/gruppa-titan/venv/bin/python notify_upcoming.py

По умолчанию шлёт уведомления о сделках со стартом через 3 дня (-d/--days). Чтобы
не дублировать в течение дня, скрипт записывает отметку в файл .notify_state.json
рядом с собой и пропускает уже разосланные.

Использование:
    python notify_upcoming.py            # по дефолту: за 3 дня до старта
    python notify_upcoming.py --days 1   # за 1 день до старта (накануне)
    python notify_upcoming.py --days 0   # в день старта
    python notify_upcoming.py --force    # игнорировать .notify_state.json
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import date, timedelta


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.notify_state.json')


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f'Не удалось сохранить состояние: {e}', file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description='Telegram-уведомления о предстоящем старте сделок')
    parser.add_argument('--days', '-d', type=int, default=3,
                        help='За сколько дней до старта оповещать (по умолчанию 3)')
    parser.add_argument('--force', action='store_true',
                        help='Игнорировать дедуп — отправить в любом случае')
    parser.add_argument('--dry-run', action='store_true',
                        help='Не отправлять, только показать что бы отправилось')
    args = parser.parse_args()

    # Импортируем приложение лениво — чтобы --help работал без зависимостей
    from app import app
    from models import Deal
    from telegram_notify import notify_upcoming_deal

    target_date = date.today() + timedelta(days=args.days)
    state = load_state() if not args.force else {}
    state_key = f'days_{args.days}'
    already_notified = set(state.get(state_key, {}).get(target_date.isoformat(), []))

    sent_ids: list[int] = []
    skipped_ids: list[int] = []

    with app.app_context():
        deals = Deal.query.filter(
            Deal.status == 'active',
            Deal.date_start == target_date
        ).all()

        if not deals:
            print(f'Сделок на {target_date.isoformat()} (через {args.days} дн.) не найдено.')
            return 0

        for deal in deals:
            if deal.id in already_notified:
                skipped_ids.append(deal.id)
                continue
            if args.dry_run:
                print(f'  [dry-run] #{deal.id} {deal.title} → старт {deal.date_start.strftime("%d.%m.%Y")}')
                continue
            try:
                notify_upcoming_deal(
                    app=app,
                    deal_title=deal.title,
                    days_until=args.days,
                    date_start_str=deal.date_start.strftime('%d.%m.%Y'),
                    deal_profit_pct=deal.expected_profit_pct or 0,
                    deal_category=deal.category,
                    min_investment=deal.min_investment or 0,
                    total_pool=deal.total_pool or 0,
                )
                sent_ids.append(deal.id)
                print(f'  ✓ #{deal.id} {deal.title} — отправлено')
            except Exception as e:
                print(f'  ✗ #{deal.id} {deal.title} — ошибка: {e}', file=sys.stderr)

    if not args.dry_run and not args.force and sent_ids:
        state.setdefault(state_key, {})[target_date.isoformat()] = list(already_notified | set(sent_ids))
        # Чистим старые ключи (>30 дней)
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        for k in list(state.get(state_key, {}).keys()):
            if k < cutoff:
                state[state_key].pop(k, None)
        save_state(state)

    print(f'\nИтого: отправлено {len(sent_ids)}, пропущено (уже разосланы): {len(skipped_ids)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
