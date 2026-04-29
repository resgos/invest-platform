"""
Тест для calc_investment_end_date / effective_start_date.

Эти функции — ядро денежной логики (срок и pro-rata прибыль), поэтому
покрываем основные сценарии.

Запуск:
    python -m unittest tests.test_calc_end_date
"""
from datetime import date, timedelta
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dateutil.relativedelta import relativedelta  # noqa: E402


# ── Repro локальных функций из app.py (без app context для unit-тестов) ──

def effective_start_date(deal, fallback):
    if deal.date_start and deal.date_start > fallback:
        return deal.date_start
    return fallback


def calc_investment_end_date(deal, start_date):
    base = effective_start_date(deal, start_date) if start_date else None
    candidates = []
    if deal.date_end:
        candidates.append(deal.date_end)
    if deal.investment_term_months and base:
        candidates.append(base + relativedelta(months=deal.investment_term_months))
    if deal.investment_term_days and base:
        candidates.append(base + timedelta(days=deal.investment_term_days))
    if candidates:
        return min(candidates)
    return None


@dataclass
class FakeDeal:
    date_start: date | None = None
    date_end: date | None = None
    investment_term_months: int | None = None
    investment_term_days: int | None = None


class EffectiveStartDateTests(unittest.TestCase):

    def test_no_date_start_returns_fallback(self):
        d = FakeDeal()
        self.assertEqual(effective_start_date(d, date(2026, 4, 29)), date(2026, 4, 29))

    def test_past_date_start_returns_fallback(self):
        d = FakeDeal(date_start=date(2026, 1, 1))
        self.assertEqual(effective_start_date(d, date(2026, 4, 29)), date(2026, 4, 29))

    def test_future_date_start_used(self):
        d = FakeDeal(date_start=date(2026, 5, 10))
        self.assertEqual(effective_start_date(d, date(2026, 4, 29)), date(2026, 5, 10))

    def test_same_day_date_start_uses_fallback(self):
        d = FakeDeal(date_start=date(2026, 4, 29))
        self.assertEqual(effective_start_date(d, date(2026, 4, 29)), date(2026, 4, 29))


class CalcInvestmentEndDateTests(unittest.TestCase):

    def test_no_term_returns_none(self):
        d = FakeDeal()
        self.assertIsNone(calc_investment_end_date(d, date(2026, 4, 29)))

    def test_only_date_end(self):
        d = FakeDeal(date_end=date(2027, 1, 1))
        self.assertEqual(calc_investment_end_date(d, date(2026, 4, 29)), date(2027, 1, 1))

    def test_only_term_months(self):
        d = FakeDeal(investment_term_months=6)
        self.assertEqual(
            calc_investment_end_date(d, date(2026, 4, 29)),
            date(2026, 10, 29),
        )

    def test_only_term_days(self):
        d = FakeDeal(investment_term_days=90)
        self.assertEqual(
            calc_investment_end_date(d, date(2026, 4, 29)),
            date(2026, 4, 29) + timedelta(days=90),
        )

    def test_term_months_anchored_to_future_start(self):
        """Если дата старта в будущем — срок отсчитывается от неё, не от 'сегодня'."""
        d = FakeDeal(date_start=date(2026, 5, 10), investment_term_months=6)
        self.assertEqual(
            calc_investment_end_date(d, date(2026, 4, 29)),
            date(2026, 11, 10),
        )

    def test_term_days_anchored_to_future_start(self):
        d = FakeDeal(date_start=date(2026, 5, 10), investment_term_days=30)
        self.assertEqual(
            calc_investment_end_date(d, date(2026, 4, 29)),
            date(2026, 6, 9),
        )

    def test_takes_earliest_of_multiple(self):
        d = FakeDeal(
            investment_term_months=12,                # +1 год = 2027-04-29
            date_end=date(2026, 9, 1),                # раньше
        )
        self.assertEqual(
            calc_investment_end_date(d, date(2026, 4, 29)),
            date(2026, 9, 1),
        )

    def test_ignores_term_when_no_start_date(self):
        d = FakeDeal(investment_term_months=12)
        self.assertIsNone(calc_investment_end_date(d, None))


if __name__ == '__main__':
    unittest.main()
