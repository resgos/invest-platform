"""
Тесты телеграм-бота: матчинг фильтров, эмиссия событий (outbox),
авторизация по telegram_id, переключение фильтров кнопками.

Работают на изолированном Flask-приложении с in-memory SQLite —
боевая БД и .env не трогаются.
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import db, User, Deal, Investment, BotFilter, BotNotification
import bot_events
from bot_events import (deal_matches_filter, emit_deal_published,
                        emit_deal_starting, emit_investment_event,
                        EVENT_DEAL_PUBLISHED, EVENT_INV_CONFIRMED)
import tg_bot
from tg_bot import (resolve_user, apply_filter_callback,
                    visible_active_deals_query, _deal_event_still_relevant)


def make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'  # in-memory
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SITE_URL'] = 'https://titan.example'
    db.init_app(app)
    return app


def mk_user(username, tg=None, role='investor', active=True):
    u = User(username=username, email=f'{username}@t.ru', password_hash='x',
             full_name=username.title(), role=role, is_active=active,
             telegram_id=tg)
    db.session.add(u)
    db.session.commit()
    return u


def mk_deal(title='Сделка', category='realestate', risk='medium', profit=15.0,
            min_inv=100_000, visibility='all', status='active',
            date_start=None, date_end=None, deal_type='investment'):
    d = Deal(title=title, description='desc', category=category,
             risk_level=risk, expected_profit_pct=profit,
             min_investment=min_inv, price=1_000_000, total_pool=1_000_000,
             visibility=visibility, status=status, deal_type=deal_type,
             date_start=date_start, date_end=date_end)
    db.session.add(d)
    db.session.commit()
    return d


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


class FilterMatchTests(BotTestCase):
    # deal_matches_filter — чистая функция: BotFilter не персистится,
    # пользователь для матчинга не нужен

    def test_no_filter_matches_everything(self):
        d = mk_deal()
        self.assertTrue(deal_matches_filter(d, None))

    def test_disabled_filter_rejects(self):
        bf = BotFilter(enabled=False)
        d = mk_deal()
        self.assertFalse(deal_matches_filter(d, bf))

    def test_category_filter(self):
        bf = BotFilter(enabled=True, categories='auto,business')
        self.assertFalse(deal_matches_filter(mk_deal(category='realestate'), bf))
        self.assertTrue(deal_matches_filter(mk_deal(category='auto'), bf))

    def test_risk_filter(self):
        bf = BotFilter(enabled=True, risk_levels='low')
        self.assertFalse(deal_matches_filter(mk_deal(risk='high'), bf))
        self.assertTrue(deal_matches_filter(mk_deal(risk='low'), bf))

    def test_min_profit_filter(self):
        bf = BotFilter(enabled=True, min_profit_pct=15)
        self.assertFalse(deal_matches_filter(mk_deal(profit=10), bf))
        self.assertTrue(deal_matches_filter(mk_deal(profit=20), bf))
        self.assertTrue(deal_matches_filter(mk_deal(profit=15), bf))

    def test_urgent_sale_bypasses_min_profit(self):
        # У срочных продаж доходность принудительно 0 — порог доходности
        # не должен их резать (иначе они не доходили бы никому с пресетом)
        bf = BotFilter(enabled=True, min_profit_pct=15)
        urgent = mk_deal(profit=0, deal_type='urgent_sale')
        self.assertTrue(deal_matches_filter(urgent, bf))

    def test_max_entry_filter(self):
        bf = BotFilter(enabled=True, max_entry=500_000)
        self.assertFalse(deal_matches_filter(mk_deal(min_inv=700_000), bf))
        self.assertTrue(deal_matches_filter(mk_deal(min_inv=300_000), bf))

    def test_empty_csv_means_all(self):
        bf = BotFilter(enabled=True, categories='', risk_levels='')
        self.assertTrue(deal_matches_filter(mk_deal(category='other', risk='high'), bf))


class EmitDealPublishedTests(BotTestCase):
    def test_saves_only_for_eligible_users(self):
        with_tg = mk_user('tg_user', tg=111)
        without_tg = mk_user('no_tg')
        inactive = mk_user('sleeper', tg=222, active=False)
        admin = mk_user('boss', tg=333, role='admin')
        d = mk_deal(visibility='all')

        saved = emit_deal_published(self.app, d)

        self.assertEqual(saved, 1)
        rows = BotNotification.query.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, with_tg.id)
        self.assertEqual(rows[0].event_type, EVENT_DEAL_PUBLISHED)
        self.assertEqual(rows[0].status, 'pending')
        self.assertIn('Новая сделка', rows[0].message)
        self.assertIn('https://titan.example/deal/', rows[0].message)

    def test_respects_selected_visibility(self):
        chosen = mk_user('chosen', tg=111)
        outsider = mk_user('outsider', tg=222)
        d = mk_deal(visibility='selected')
        d.visible_to.append(chosen)
        db.session.commit()

        saved = emit_deal_published(self.app, d)

        self.assertEqual(saved, 1)
        self.assertEqual(BotNotification.query.first().user_id, chosen.id)

    def test_respects_user_filters(self):
        want_auto = mk_user('auto_fan', tg=111)
        db.session.add(BotFilter(user_id=want_auto.id, enabled=True, categories='auto'))
        anyone = mk_user('anyone', tg=222)
        db.session.commit()
        d = mk_deal(category='realestate', visibility='all')

        saved = emit_deal_published(self.app, d)

        self.assertEqual(saved, 1)
        self.assertEqual(BotNotification.query.first().user_id, anyone.id)

    def test_dedup_on_second_emit(self):
        mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        self.assertEqual(emit_deal_published(self.app, d), 1)
        # Повторная эмиссия (редактирование сделки) — дублей нет
        self.assertEqual(emit_deal_published(self.app, d), 0)
        self.assertEqual(BotNotification.query.count(), 1)

    def test_emit_after_visibility_expansion(self):
        first = mk_user('first', tg=111)
        second = mk_user('second', tg=222)
        d = mk_deal(visibility='selected')
        d.visible_to.append(first)
        db.session.commit()
        self.assertEqual(emit_deal_published(self.app, d), 1)

        d.visible_to.append(second)
        db.session.commit()
        # Событие получает только новый пользователь
        self.assertEqual(emit_deal_published(self.app, d), 1)
        users = {n.user_id for n in BotNotification.query.all()}
        self.assertEqual(users, {first.id, second.id})

    def test_no_events_for_paused_or_started_deals(self):
        mk_user('u1', tg=111)
        paused = mk_deal(status='paused')
        self.assertEqual(emit_deal_published(self.app, paused), 0)
        started = mk_deal(date_start=date.today() - timedelta(days=1))
        self.assertEqual(emit_deal_published(self.app, started), 0)

    def test_failed_notification_requeued_on_reemit(self):
        # Опечатка в telegram_id → доставка failed → админ исправил → повторная
        # эмиссия обязана реанимировать строку, а не молча дедупнуть
        mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        row = BotNotification.query.first()
        row.status = 'failed'
        row.attempts = 5
        db.session.commit()

        self.assertEqual(emit_deal_published(self.app, d), 0)  # новых строк нет
        row = BotNotification.query.first()
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.attempts, 0)

    def test_pending_message_refreshed_on_deal_edit(self):
        # Правка сделки до доставки — недоставленная строка получает свежий текст
        mk_user('u1', tg=111)
        d = mk_deal(title='Старое название', visibility='all')
        emit_deal_published(self.app, d)
        d.title = 'Новое название'
        db.session.commit()
        emit_deal_published(self.app, d)

        rows = BotNotification.query.all()
        self.assertEqual(len(rows), 1)
        self.assertIn('Новое название', rows[0].message)

    def test_deal_starting_horizons_not_collapsed(self):
        # Напоминания «за 3 дня» и «завтра» — разные события, дедуп не схлопывает
        mk_user('u1', tg=111)
        d = mk_deal(visibility='all', date_start=date.today() + timedelta(days=3))
        self.assertEqual(emit_deal_starting(self.app, d, 3), 1)
        self.assertEqual(emit_deal_starting(self.app, d, 1), 1)
        # повтор того же горизонта — дубль не создаётся
        self.assertEqual(emit_deal_starting(self.app, d, 3), 0)
        types = sorted(n.event_type for n in BotNotification.query.all())
        self.assertEqual(types, ['deal_starting_1', 'deal_starting_3'])


class EmitInvestmentEventTests(BotTestCase):
    def test_personal_event_saved(self):
        u = mk_user('inv', tg=111)
        d = mk_deal()
        inv = Investment(user_id=u.id, deal_id=d.id, amount=250_000,
                         expected_profit=10_000, status='active')
        db.session.add(inv)
        db.session.commit()

        ok = emit_investment_event(self.app, inv, EVENT_INV_CONFIRMED)
        self.assertTrue(ok)
        row = BotNotification.query.first()
        self.assertEqual(row.investment_id, inv.id)
        self.assertIn('подтверждена', row.message)

    def test_skipped_without_telegram_id(self):
        u = mk_user('inv')
        d = mk_deal()
        inv = Investment(user_id=u.id, deal_id=d.id, amount=100, status='active')
        db.session.add(inv)
        db.session.commit()
        self.assertFalse(emit_investment_event(self.app, inv, EVENT_INV_CONFIRMED))
        self.assertEqual(BotNotification.query.count(), 0)

    def test_filters_do_not_apply_to_personal_events(self):
        u = mk_user('inv', tg=111)
        db.session.add(BotFilter(user_id=u.id, enabled=False))  # уведомления выключены
        d = mk_deal()
        inv = Investment(user_id=u.id, deal_id=d.id, amount=100, status='active')
        db.session.add(inv)
        db.session.commit()
        self.assertTrue(emit_investment_event(self.app, inv, EVENT_INV_CONFIRMED))


class BotAuthTests(BotTestCase):
    def test_known_telegram_id_resolves(self):
        u = mk_user('u1', tg=555)
        self.assertEqual(resolve_user(555).id, u.id)

    def test_unknown_id_denied(self):
        mk_user('u1', tg=555)
        self.assertIsNone(resolve_user(999))

    def test_user_without_tg_never_resolves(self):
        mk_user('u1')
        self.assertIsNone(resolve_user(None))

    def test_inactive_user_denied(self):
        mk_user('u1', tg=555, active=False)
        self.assertIsNone(resolve_user(555))

    def test_denied_text_contains_id(self):
        self.assertIn('999', tg_bot.access_denied_text(999))


class FilterCallbackTests(BotTestCase):
    def _bf(self):
        u = mk_user('u1', tg=111)
        bf = BotFilter(user_id=u.id, enabled=True)
        db.session.add(bf)
        db.session.commit()
        return bf

    def test_toggle_category_off_and_on(self):
        bf = self._bf()
        changed, alert = apply_filter_callback(bf, 'cat', 'auto')
        self.assertTrue(changed)
        self.assertIsNone(alert)
        self.assertNotIn('auto', bf.categories_list())
        # осталось 4 категории
        self.assertEqual(len(bf.categories_list()), 4)
        changed, _ = apply_filter_callback(bf, 'cat', 'auto')
        self.assertTrue(changed)
        # все категории → снова «все» (NULL)
        self.assertIsNone(bf.categories)

    def test_cannot_remove_last_category(self):
        bf = self._bf()
        for c in ['realestate', 'auto', 'business', 'equipment']:
            apply_filter_callback(bf, 'cat', c)
        self.assertEqual(bf.categories_list(), ['other'])
        changed, alert = apply_filter_callback(bf, 'cat', 'other')
        self.assertFalse(changed)
        self.assertIsNotNone(alert)

    def test_profit_and_entry_presets(self):
        bf = self._bf()
        apply_filter_callback(bf, 'profit', '15')
        self.assertEqual(bf.min_profit_pct, 15)
        apply_filter_callback(bf, 'profit', '0')
        self.assertIsNone(bf.min_profit_pct)
        apply_filter_callback(bf, 'entry', '500000')
        self.assertEqual(bf.max_entry, 500_000)

    def test_forged_callback_values_ignored(self):
        # callback_data подделываем как клиент: inf/nan/мусор не должны
        # ни падать, ни персиститься в фильтры
        bf = self._bf()
        for action, value in [('profit', 'inf'), ('profit', 'nan'),
                              ('profit', 'abc'), ('profit', '99'),
                              ('entry', '1e400'), ('entry', 'garbage'),
                              ('cat', 'FORGED'), ('risk', 'FORGED'),
                              ('unknown', 'x')]:
            changed, alert = apply_filter_callback(bf, action, value)
            self.assertFalse(changed, f'{action}|{value} не должен применяться')
        self.assertIsNone(bf.min_profit_pct)
        self.assertIsNone(bf.max_entry)
        self.assertIsNone(bf.categories)
        self.assertIsNone(bf.risk_levels)

    def test_reset(self):
        bf = self._bf()
        apply_filter_callback(bf, 'cat', 'auto')
        apply_filter_callback(bf, 'profit', '20')
        apply_filter_callback(bf, 'toggle', '0')
        self.assertFalse(bf.enabled)
        apply_filter_callback(bf, 'reset', '0')
        self.assertTrue(bf.enabled)
        self.assertIsNone(bf.categories)
        self.assertIsNone(bf.min_profit_pct)


class VisibleDealsTests(BotTestCase):
    def test_visible_deals_for_bot(self):
        u = mk_user('u1', tg=111)
        mk_deal(title='Открытая', visibility='all')
        mk_deal(title='Чужая', visibility='selected')
        mk_deal(title='Истёкшая', visibility='all',
                date_end=date.today() - timedelta(days=1))
        mk_deal(title='Ушедшая', visibility='all', date_start=date.today())
        deals = visible_active_deals_query(u).all()
        self.assertEqual([d.title for d in deals], ['Открытая'])

    def test_admin_sees_all(self):
        admin = mk_user('boss', tg=999, role='admin')
        mk_deal(title='Открытая', visibility='all')
        mk_deal(title='Адресная', visibility='selected')
        self.assertEqual(visible_active_deals_query(admin).count(), 2)


class DeliveryRecheckTests(BotTestCase):
    """Перепроверка deal-события на момент доставки (защита от утечки)."""

    def _notif(self, user, deal):
        emit_deal_published(self.app, deal)
        return BotNotification.query.filter_by(user_id=user.id, deal_id=deal.id).first()

    def test_paused_deal_event_not_relevant(self):
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        n = self._notif(u, d)
        self.assertTrue(_deal_event_still_relevant(n, u))
        d.status = 'paused'
        db.session.commit()
        self.assertFalse(_deal_event_still_relevant(n, u))

    def test_visibility_narrowed_event_not_relevant(self):
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        n = self._notif(u, d)
        # Админ сужает видимость до «выбранным», пользователь в список не входит
        d.visibility = 'selected'
        db.session.commit()
        self.assertFalse(_deal_event_still_relevant(n, u))
        # А включённый в список — по-прежнему получает
        d.visible_to.append(u)
        db.session.commit()
        self.assertTrue(_deal_event_still_relevant(n, u))

    def test_personal_events_always_relevant(self):
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        inv = Investment(user_id=u.id, deal_id=d.id, amount=100, status='active')
        db.session.add(inv)
        db.session.commit()
        emit_investment_event(self.app, inv, EVENT_INV_CONFIRMED)
        n = BotNotification.query.filter_by(investment_id=inv.id).first()
        d.status = 'paused'
        db.session.commit()
        self.assertTrue(_deal_event_still_relevant(n, u))


if __name__ == '__main__':
    unittest.main()
