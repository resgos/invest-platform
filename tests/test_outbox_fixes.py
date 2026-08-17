"""
Тесты 4 точечных фиксов outbox-конвейера телеграм-бота (tg_bot.py, bot_events.py).

  БАГ 1 — cancelled была вечной могилой: _upsert_deal_notification теперь
          реанимирует cancelled так же, как failed; sent по-прежнему не трогаем.
  БАГ 2 — 401 (и прочие ошибки токена/запроса) считались 'transient' и
          ретраились ×100 с прерыванием ВСЕГО батча (head-of-line blocking).
          Теперь: permanent (400/401/403/404) — сразу failed; row_error
          (ошибка конкретной строки) — не прерывает батч; net_error (сеть/5xx)
          — по-прежнему прерывает батч.
  БАГ 3 — cmd_feed прерывал сбор на первом невлезающем ЦЕЛОМ блоке; если им
          оказывался самый свежий (например отклонение заявки с длинным
          notes), лента показывала заголовок и ноль событий. Теперь длинный
          одиночный блок обрезается по символам, а сбор остальных продолжается.
  БАГ 4 — _deal_event_still_relevant для deal_starting_*/deal_published
          проверяла только status=='active', что не ловит уже СТАРТОВАВШУЮ
          (но всё ещё active) сделку. Теперь используется accepts_investors().

Фикстуры и стиль — как в tests/test_bot_logic.py (изолированный Flask +
in-memory SQLite, боевая БД не трогается).
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from models import db, User, Deal, Investment, BotFilter, BotNotification
import bot_events
from bot_events import (emit_deal_published, emit_deal_starting, emit_investment_event,
                        EVENT_DEAL_PUBLISHED, EVENT_INV_REJECTED)
import tg_bot
from tg_bot import (dispatch_outbox, cmd_feed, _deal_event_still_relevant,
                    _truncate_block, TelegramAPI, TelegramApiError,
                    ROW_ERROR_MAX_ATTEMPTS)


def make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'  # in-memory
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['BOT_DEAL_BROADCAST'] = True  # тесты проверяют саму рассылку
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


# ════════════════════ БАГ 1: cancelled — не вечная могила ════════════════════

class CancelledReanimationTests(BotTestCase):
    def test_cancelled_row_reanimated_on_reemit(self):
        # Строка была отменена диспетчером (сделку временно поставили на
        # паузу/сузили видимость), затем админ вернул всё как было — повторная
        # эмиссия ОБЯЗАНА реанимировать строку, иначе пользователь не получит
        # уведомление никогда (уникальный индекс не даст вставить новую).
        mk_user('u1', tg=111)
        d = mk_deal(title='Старое название', visibility='all')
        emit_deal_published(self.app, d)
        row = BotNotification.query.first()
        row.status = 'cancelled'
        row.attempts = 7
        db.session.commit()

        d.title = 'Новое название'
        db.session.commit()
        saved = emit_deal_published(self.app, d)

        self.assertEqual(saved, 0)  # реанимация существующей строки, не новая запись
        row = BotNotification.query.first()
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.attempts, 0)
        self.assertIn('Новое название', row.message)

    def test_sent_row_not_reanimated(self):
        # sent трогать нельзя — иначе уже доставленное уведомление уйдёт повторно
        mk_user('u1', tg=111)
        d = mk_deal(title='Название', visibility='all')
        emit_deal_published(self.app, d)
        row = BotNotification.query.first()
        row.status = 'sent'
        row.attempts = 1
        old_message = row.message
        db.session.commit()

        d.title = 'Изменённое название'
        db.session.commit()
        saved = emit_deal_published(self.app, d)

        self.assertEqual(saved, 0)
        row = BotNotification.query.first()
        self.assertEqual(row.status, 'sent')
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.message, old_message)  # текст НЕ обновлён

    def test_failed_still_reanimated(self):
        # Регресс: старое поведение (реанимация failed) не должно было сломаться
        mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        row = BotNotification.query.first()
        row.status = 'failed'
        row.attempts = 3
        db.session.commit()

        self.assertEqual(emit_deal_published(self.app, d), 0)
        row = BotNotification.query.first()
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.attempts, 0)


# ═══════════════ БАГ 2: классификация ошибок / head-of-line blocking ═════════

class SendMessageClassificationTests(unittest.TestCase):
    """TelegramAPI.send_message без сети: подменяем .call() (единственное
    место, которое реально бьёт по urllib) и проверяем классификацию статуса."""

    def _api_raising(self, status, description, retry_after=None):
        api = TelegramAPI('fake-token')

        def fake_call(method, payload=None, timeout=20):
            raise TelegramApiError(status, description, retry_after)

        api.call = fake_call
        return api

    def test_401_is_permanent(self):
        # Основа бага 2: отозванный токен раньше считался 'transient' и
        # ретраился ×100, блокируя всю очередь
        api = self._api_raising(401, 'Unauthorized')
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'permanent')

    def test_400_403_404_are_permanent(self):
        for code in (400, 403, 404):
            api = self._api_raising(code, 'nope')
            status, _ = api.send_message(123, 'hi')
            self.assertEqual(status, 'permanent', f'{code} должен быть permanent')

    def test_429_is_flood(self):
        api = self._api_raising(429, 'Too Many Requests', retry_after=7)
        status, retry_after = api.send_message(123, 'hi')
        self.assertEqual(status, 'flood')
        self.assertEqual(retry_after, 7)

    def test_5xx_is_net_error(self):
        # Сервер Telegram лежит — транспортная проблема, не адресата
        api = self._api_raising(500, 'Internal Server Error')
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'net_error')

    def test_other_4xx_is_row_error(self):
        # Ошибка, специфичная конкретному запросу/строке — не токену и не сети
        api = self._api_raising(409, 'Conflict')
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'row_error')

    def test_ok_false_200_is_row_error(self):
        # call() поднимает TelegramApiError(200, ...) при ok:false в теле ответа
        api = self._api_raising(200, 'Bad Request: chat not found')
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'row_error')

    def test_url_error_is_net_error(self):
        api = TelegramAPI('fake-token')

        def fake_call(method, payload=None, timeout=20):
            raise URLError('unreachable')

        api.call = fake_call
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'net_error')

    def test_os_error_is_net_error(self):
        api = TelegramAPI('fake-token')

        def fake_call(method, payload=None, timeout=20):
            raise OSError('timed out')

        api.call = fake_call
        status, _ = api.send_message(123, 'hi')
        self.assertEqual(status, 'net_error')


class FakeApi:
    """Подмена TelegramAPI для dispatch_outbox: без сети, ответ send_message
    задаётся заранее по chat_id (task требует именно фейковый объект,
    а не реальный TelegramAPI, для тестов диспетчера)."""

    def __init__(self, by_chat=None, default=('ok', None)):
        self.by_chat = by_chat or {}
        self.default = default
        self.sent_to = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_to.append(chat_id)
        return self.by_chat.get(chat_id, self.default)


class DispatchOutboxErrorHandlingTests(BotTestCase):
    """dispatch_outbox: одна битая строка не блокирует остальных (фикс
    head-of-line blocking), а сетевая ошибка по-прежнему прерывает батч."""

    def test_row_error_does_not_block_rest_of_batch(self):
        mk_user('u1', tg=111)
        u2 = mk_user('u2', tg=222)  # получит row_error
        mk_user('u3', tg=333)
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        self.assertEqual(BotNotification.query.filter_by(status='pending').count(), 3)

        api = FakeApi(by_chat={222: ('row_error', None)})
        sent, failed = dispatch_outbox(api, self.app)
        db.session.expire_all()  # dispatch_outbox коммитит через СВОЙ nested app_context

        # Обе исправные строки доставлены в ТОМ ЖЕ проходе — батч не прервался
        self.assertEqual(sent, 2)
        self.assertEqual(failed, 0)
        self.assertEqual(set(api.sent_to), {111, 222, 333})
        row2 = BotNotification.query.filter_by(user_id=u2.id).first()
        self.assertEqual(row2.status, 'pending')  # лимит попыток ещё не исчерпан
        self.assertEqual(row2.attempts, 1)
        self.assertEqual(BotNotification.query.filter_by(status='sent').count(), 2)

    def test_row_error_marks_failed_after_attempt_limit(self):
        mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        row = BotNotification.query.first()
        row.attempts = ROW_ERROR_MAX_ATTEMPTS - 1
        db.session.commit()

        api = FakeApi(by_chat={111: ('row_error', None)})
        sent, failed = dispatch_outbox(api, self.app)
        db.session.expire_all()

        self.assertEqual(sent, 0)
        self.assertEqual(failed, 1)
        row = BotNotification.query.first()
        self.assertEqual(row.status, 'failed')

    def test_net_error_breaks_batch(self):
        u1 = mk_user('u1', tg=111)  # получит net_error
        u2 = mk_user('u2', tg=222)  # НЕ должна быть тронута — батч прерван раньше
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        # Порядок обработки — created_at ASC; фиксируем явно, чтобы тест не
        # зависел от порядка вставки/tie-break конкретной СУБД
        row1 = BotNotification.query.filter_by(user_id=u1.id).first()
        row2 = BotNotification.query.filter_by(user_id=u2.id).first()
        base = datetime.now(timezone.utc)
        row1.created_at = base
        row2.created_at = base + timedelta(seconds=1)
        db.session.commit()

        api = FakeApi(by_chat={111: ('net_error', None)})
        sent, failed = dispatch_outbox(api, self.app)
        db.session.expire_all()

        self.assertEqual(sent, 0)
        self.assertEqual(failed, 0)
        self.assertEqual(api.sent_to, [111])  # до второй строки не дошли — батч прерван
        row1 = BotNotification.query.filter_by(user_id=u1.id).first()
        row2 = BotNotification.query.filter_by(user_id=u2.id).first()
        self.assertEqual(row1.status, 'pending')
        self.assertEqual(row1.attempts, 1)
        self.assertEqual(row2.status, 'pending')
        self.assertEqual(row2.attempts, 0)  # не тронута вовсе


# ════════════════════ БАГ 3: /feed не должен быть пустым ═════════════════════

class TruncateBlockTests(unittest.TestCase):
    """Юнит-тесты хелпера _truncate_block в отрыве от cmd_feed."""

    def test_short_block_unchanged(self):
        self.assertEqual(_truncate_block('short text', 900), 'short text')

    def test_cuts_at_line_boundary_not_mid_tag(self):
        block = 'line1\nline2\n' + ('x' * 2000)
        out = _truncate_block(block, 900)
        self.assertTrue(out.startswith('line1\nline2'))
        self.assertTrue(out.endswith('\n…'))
        self.assertNotIn('xxxxxxxxxx', out)  # хвост реально отрезан

    def test_hard_cut_when_no_newline_in_range(self):
        block = 'x' * 5000  # ни одного переноса строки — крайний случай
        out = _truncate_block(block, 900)
        self.assertTrue(out.endswith('…'))
        self.assertLessEqual(len(out), 902)


class CmdFeedTruncationTests(BotTestCase):
    def test_huge_first_block_does_not_empty_the_feed(self):
        # Реальный сценарий бага: отклонение заявки с длинным notes (5000
        # символов, поле без ограничения длины) — самое свежее (единственное)
        # событие в ленте
        u = mk_user('u1', tg=111)
        d = mk_deal()
        inv = Investment(user_id=u.id, deal_id=d.id, amount=250_000, status='active',
                         notes='Слишком большой риск. ' * 230)  # > 5000 символов, без \n
        db.session.add(inv)
        db.session.commit()
        self.assertGreater(len(inv.notes), 5000)
        emit_investment_event(self.app, inv, EVENT_INV_REJECTED)

        text = cmd_feed(self.app, u)

        self.assertIn('Последние события', text)
        self.assertIn('Ваша заявка отклонена', text)  # событие реально показано, не только заголовок
        self.assertLess(len(text), 4096)  # укладывается в лимит Telegram

    def test_huge_block_followed_by_normal_blocks_all_visible(self):
        u = mk_user('u1', tg=111)
        d = mk_deal()
        now = datetime.now(timezone.utc)
        huge = BotNotification(
            user_id=u.id, deal_id=d.id, event_type='investment_rejected',
            message='❌ <b>Огромное событие</b>\n' + ('Ж' * 5000), status='sent',
            created_at=now,  # самое свежее — первым в ленте (сортировка desc)
        )
        small = BotNotification(
            user_id=u.id, deal_id=d.id, event_type=EVENT_DEAL_PUBLISHED,
            message='🆕 <b>Маленькое событие</b>', status='sent',
            created_at=now - timedelta(seconds=1),
        )
        db.session.add_all([huge, small])
        db.session.commit()

        text = cmd_feed(self.app, u)

        self.assertIn('Огромное событие', text)
        self.assertIn('Маленькое событие', text)  # второй блок не потерян
        self.assertLess(len(text), 4096)

    def test_empty_feed_message_unaffected(self):
        u = mk_user('u1', tg=111)
        text = cmd_feed(self.app, u)
        self.assertIn('Лента пуста', text)


# ═══════════════ БАГ 4: «скоро старт» после фактического старта ══════════════

class DealStartingRelevanceTests(BotTestCase):
    def test_starting_notification_for_started_deal_is_cancelled(self):
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all', date_start=date.today() + timedelta(days=3))
        emit_deal_starting(self.app, d, 3)
        n = BotNotification.query.filter_by(user_id=u.id).first()
        self.assertTrue(_deal_event_still_relevant(n, u))

        # Сделка фактически стартовала, пока строка ждала в очереди диспетчера
        d.date_start = date.today()
        db.session.commit()
        self.assertFalse(_deal_event_still_relevant(n, u))

    def test_starting_notification_for_not_yet_started_deal_is_relevant(self):
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all', date_start=date.today() + timedelta(days=3))
        emit_deal_starting(self.app, d, 3)
        n = BotNotification.query.filter_by(user_id=u.id).first()
        self.assertTrue(_deal_event_still_relevant(n, u))

    def test_deal_published_notification_for_started_deal_is_cancelled(self):
        # deal_published тоже обещает «успейте подать заявку» — тот же фикс
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all')
        emit_deal_published(self.app, d)
        n = BotNotification.query.filter_by(user_id=u.id, event_type=EVENT_DEAL_PUBLISHED).first()
        self.assertTrue(_deal_event_still_relevant(n, u))

        d.date_start = date.today()  # стартовала прямо сегодня
        db.session.commit()
        self.assertFalse(_deal_event_still_relevant(n, u))

    def test_paused_deal_still_cancels_starting_event(self):
        # Регресс: старая причина отмены (пауза) не должна была сломаться
        # фиксом — accepts_investors тоже проверяет status=='active' внутри
        u = mk_user('u1', tg=111)
        d = mk_deal(visibility='all', date_start=date.today() + timedelta(days=3))
        emit_deal_starting(self.app, d, 3)
        n = BotNotification.query.filter_by(user_id=u.id).first()
        d.status = 'paused'
        db.session.commit()
        self.assertFalse(_deal_event_still_relevant(n, u))


if __name__ == '__main__':
    unittest.main()
