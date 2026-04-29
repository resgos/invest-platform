"""
Тесты ключевой логики дат для Группы Титан.

Проверяют:
  • Deal.is_starting_soon / has_started / days_until_start
  • Deal.HOT_THRESHOLD_DAYS
  • term_display / term_days_for_calc

Запуск:
    python -m pytest tests/ -v
    или: python -m unittest tests.test_date_logic
"""
from datetime import date, timedelta
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Deal  # noqa: E402


def make_deal(**kwargs):
    """Создаёт Deal без БД (для тестирования pure-property логики)."""
    deal = Deal()
    deal.title = kwargs.pop('title', 'Test')
    deal.deal_type = 'investment'
    deal.category = 'realestate'
    deal.description = '...'
    deal.price = 1_000_000
    deal.expected_profit_pct = kwargs.pop('expected_profit_pct', 12)
    deal.investment_term_months = kwargs.pop('investment_term_months', None)
    deal.investment_term_days = kwargs.pop('investment_term_days', None)
    deal.date_start = kwargs.pop('date_start', None)
    deal.date_end = kwargs.pop('date_end', None)
    deal.collected_amount = 0
    deal.total_pool = 0
    deal.min_investment = 0
    deal.risk_level = 'medium'
    deal.status = 'active'
    deal.visibility = 'all'
    return deal


class HotDealTests(unittest.TestCase):

    def test_no_date_start_means_not_hot(self):
        deal = make_deal()
        self.assertFalse(deal.is_starting_soon)
        self.assertIsNone(deal.days_until_start)
        self.assertTrue(deal.has_started, 'Без date_start считаем стартовавшей')

    def test_start_in_3_days_is_hot(self):
        deal = make_deal(date_start=date.today() + timedelta(days=3))
        self.assertTrue(deal.is_starting_soon)
        self.assertEqual(deal.days_until_start, 3)
        self.assertFalse(deal.has_started)

    def test_start_today_is_started_not_hot(self):
        # Старт сегодня: has_started=True (date_start <= today), is_starting_soon=False (нужно date_start > today)
        deal = make_deal(date_start=date.today())
        self.assertTrue(deal.has_started)
        self.assertIsNone(deal.days_until_start)
        self.assertFalse(deal.is_starting_soon)

    def test_start_tomorrow_is_hot(self):
        deal = make_deal(date_start=date.today() + timedelta(days=1))
        self.assertTrue(deal.is_starting_soon)
        self.assertEqual(deal.days_until_start, 1)

    def test_start_at_exact_threshold_is_hot(self):
        deal = make_deal(date_start=date.today() + timedelta(days=Deal.HOT_THRESHOLD_DAYS))
        self.assertTrue(deal.is_starting_soon)
        self.assertEqual(deal.days_until_start, Deal.HOT_THRESHOLD_DAYS)

    def test_start_beyond_threshold_not_hot(self):
        deal = make_deal(date_start=date.today() + timedelta(days=Deal.HOT_THRESHOLD_DAYS + 1))
        self.assertFalse(deal.is_starting_soon)
        self.assertEqual(deal.days_until_start, Deal.HOT_THRESHOLD_DAYS + 1)
        self.assertFalse(deal.has_started)

    def test_start_in_past_is_started_not_hot(self):
        deal = make_deal(date_start=date.today() - timedelta(days=5))
        self.assertTrue(deal.has_started)
        self.assertIsNone(deal.days_until_start)
        self.assertFalse(deal.is_starting_soon)


class TermDisplayTests(unittest.TestCase):

    def test_open_ended(self):
        deal = make_deal()
        self.assertEqual(deal.term_display, 'Бессрочно')

    def test_only_term_months(self):
        deal = make_deal(investment_term_months=12)
        self.assertEqual(deal.term_display, '12 мес.')

    def test_only_date_end(self):
        d = date(2026, 12, 31)
        deal = make_deal(date_end=d)
        self.assertEqual(deal.term_display, 'до 31.12.2026')

    def test_start_and_end_range(self):
        deal = make_deal(date_start=date(2026, 5, 1), date_end=date(2027, 5, 1))
        self.assertEqual(deal.term_display, '01.05.2026 — 01.05.2027')

    def test_start_only(self):
        deal = make_deal(date_start=date(2026, 8, 15))
        self.assertEqual(deal.term_display, 'с 15.08.2026')

    def test_combined(self):
        deal = make_deal(date_start=date(2026, 1, 1), date_end=date(2027, 1, 1),
                         investment_term_months=12)
        self.assertIn('01.01.2026 — 01.01.2027', deal.term_display)
        self.assertIn('12 мес.', deal.term_display)


class TermDaysForCalcTests(unittest.TestCase):

    def test_term_days_priority(self):
        deal = make_deal(investment_term_days=90, investment_term_months=12)
        self.assertEqual(deal.term_days_for_calc, 90)

    def test_term_months_to_days(self):
        deal = make_deal(investment_term_months=12)
        self.assertEqual(deal.term_days_for_calc, 12 * 30)

    def test_open_ended_fallback(self):
        deal = make_deal()
        self.assertEqual(deal.term_days_for_calc, 365)

    def test_date_end_with_future_start(self):
        # Если задан и date_start (в будущем), и date_end — отсчёт от даты старта
        future_start = date.today() + timedelta(days=10)
        end = future_start + timedelta(days=180)
        deal = make_deal(date_start=future_start, date_end=end)
        self.assertEqual(deal.term_days_for_calc, 180)


class HasStartedTests(unittest.TestCase):
    """Проверка семантики: после date_start сделка уходит из каталога,
    has_started=True означает «приём заявок завершён»."""

    def test_no_date_start_open_for_investments(self):
        # Без фикс. старта — всегда «открыта» (has_started=True по нашему flag,
        # но это ОК потому что поведение в каталоге зависит от date_start IS NULL)
        deal = make_deal()
        self.assertTrue(deal.has_started)

    def test_future_start_not_started(self):
        deal = make_deal(date_start=date.today() + timedelta(days=5))
        self.assertFalse(deal.has_started)

    def test_today_start_is_started(self):
        deal = make_deal(date_start=date.today())
        self.assertTrue(deal.has_started, 'В день старта приём заявок завершён')

    def test_past_start_is_started(self):
        deal = make_deal(date_start=date.today() - timedelta(days=3))
        self.assertTrue(deal.has_started)


class IsExpiredTests(unittest.TestCase):

    def test_no_date_end_not_expired(self):
        self.assertFalse(make_deal().is_expired)

    def test_past_date_end_expired(self):
        deal = make_deal(date_end=date.today() - timedelta(days=1))
        self.assertTrue(deal.is_expired)

    def test_future_date_end_not_expired(self):
        deal = make_deal(date_end=date.today() + timedelta(days=1))
        self.assertFalse(deal.is_expired)

    def test_today_date_end_not_expired(self):
        deal = make_deal(date_end=date.today())
        self.assertFalse(deal.is_expired)


if __name__ == '__main__':
    unittest.main()
