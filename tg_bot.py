"""
Телеграм-бот Группы Титан — отдельный процесс (systemd: gruppa-titan-bot.service).

Доступ СТРОГО по прописанному Telegram ID: админ заполняет поле «Telegram ID»
в карточке пользователя; все остальные получают отказ (с подсказкой, какой ID
сообщить админу). Бот делает две вещи:

  1. Доставляет сохранённые события из outbox (bot_notifications) в личку —
     новые сделки по фильтрам пользователя, скорый старт, статусы заявок.
  2. Отвечает на команды: /deals /feed /filters /myid /help.

Транспорт — чистый urllib с той же прокси-логикой, что и telegram_notify
(HTTP/SOCKS5, работает из РФ), никаких новых зависимостей.

Запуск:
    python tg_bot.py            # рабочий режим (long polling)
    python tg_bot.py --once     # один проход outbox + один getUpdates (smoke)

Токен: TELEGRAM_USER_BOT_TOKEN (по умолчанию = TELEGRAM_BOT_TOKEN).
ВАЖНО: getUpdates должен звать только этот процесс — не запускайте два бота
с одним токеном.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import threading
import time
from datetime import date, datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from models import db, User, Deal, BotFilter, BotNotification
from telegram_notify import _get_proxy_opener, CATEGORY_LABELS, RISK_LABELS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('tg_bot')

MSK = timezone(timedelta(hours=3))  # Europe/Moscow (без DST)

ALL_CATEGORIES = ['realestate', 'auto', 'business', 'equipment', 'other']
ALL_RISKS = ['low', 'medium', 'high']
PROFIT_PRESETS = [0, 10, 15, 20]          # 0 = любая
ENTRY_PRESETS = [0, 100_000, 500_000, 1_000_000]  # 0 = любой

CAT_SHORT = {
    'realestate': '🏠 Недвиж.',
    'auto': '🚗 Авто',
    'business': '💼 Бизнес',
    'equipment': '⚙️ Оборуд.',
    'other': '📦 Другое',
}
RISK_SHORT = {'low': '🟢 Низкий', 'medium': '🟡 Средний', 'high': '🔴 Высокий'}

MAX_ATTEMPTS = 5      # попыток доставки уведомления до статуса failed
OUTBOX_BATCH = 50     # уведомлений за один проход диспетчера


def esc(s) -> str:
    return html.escape(str(s or ''), quote=False)


def fmt_money(x) -> str:
    return f'{(x or 0):,.0f}'.replace(',', ' ')


# ──────────────────── Telegram API (urllib + прокси) ────────────────────

class TelegramAPI:
    def __init__(self, token: str, proxy_url: str = ''):
        if not token:
            raise SystemExit('TELEGRAM_USER_BOT_TOKEN / TELEGRAM_BOT_TOKEN не задан — бот не может стартовать')
        self.base = f'https://api.telegram.org/bot{token}'
        # SOCKS5 монкипатчит socket глобально и возвращает None;
        # HTTP-прокси возвращает opener для each-request использования
        self.opener = _get_proxy_opener(proxy_url)

    def call(self, method: str, payload: dict | None = None, timeout: int = 20):
        """Возвращает result из ответа API или бросает исключение."""
        url = f'{self.base}/{method}'
        data = json.dumps(payload or {}).encode('utf-8')
        req = Request(url, data=data, headers={'Content-Type': 'application/json'})
        if self.opener:
            resp = self.opener.open(req, timeout=timeout)
        else:
            resp = urlopen(req, timeout=timeout)
        try:
            body = json.loads(resp.read())
        finally:
            resp.close()
        if not body.get('ok'):
            raise RuntimeError(f'Telegram API {method}: {body}')
        return body.get('result')

    def get_updates(self, offset: int, poll_timeout: int):
        return self.call('getUpdates', {
            'offset': offset,
            'timeout': poll_timeout,
            'allowed_updates': ['message', 'callback_query'],
        }, timeout=poll_timeout + 15)

    def send_message(self, chat_id, text, reply_markup=None) -> bool:
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        try:
            self.call('sendMessage', payload)
            return True
        except (HTTPError, URLError, RuntimeError, OSError) as e:
            logger.error(f'sendMessage → {chat_id}: {e}')
            return False

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        payload = {
            'chat_id': chat_id,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup
        try:
            self.call('editMessageText', payload)
        except (HTTPError, URLError, RuntimeError, OSError) as e:
            # «message is not modified» — штатно при повторном клике
            if 'not modified' not in str(e):
                logger.warning(f'editMessageText: {e}')

    def answer_callback(self, callback_id, text=None, show_alert=False):
        payload = {'callback_query_id': callback_id}
        if text:
            payload['text'] = text
            payload['show_alert'] = show_alert
        try:
            self.call('answerCallbackQuery', payload)
        except Exception as e:
            logger.warning(f'answerCallbackQuery: {e}')

    def set_my_commands(self):
        commands = [
            {'command': 'deals', 'description': 'Доступные сделки'},
            {'command': 'feed', 'description': 'Лента сохранённых событий'},
            {'command': 'filters', 'description': 'Фильтры уведомлений'},
            {'command': 'myid', 'description': 'Мой Telegram ID'},
            {'command': 'help', 'description': 'Справка'},
        ]
        try:
            self.call('setMyCommands', {'commands': commands})
        except Exception as e:
            logger.warning(f'setMyCommands: {e}')


# ──────────────────── Доступ ────────────────────

def resolve_user(telegram_id) -> User | None:
    """Пользователь платформы по Telegram ID. None = доступ запрещён."""
    if not telegram_id:
        return None
    return User.query.filter_by(telegram_id=int(telegram_id), is_active=True).first()


def access_denied_text(telegram_id) -> str:
    return (
        '⛔ <b>Доступ запрещён</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        'Бот доступен только инвесторам «Группы Титан», у которых\n'
        'администратор прописал Telegram ID в профиле.\n\n'
        f'Ваш ID: <code>{telegram_id}</code>\n'
        'Сообщите его администратору для подключения.'
    )


# ──────────────────── Данные ────────────────────

def visible_active_deals(user: User):
    """Активные сделки, принимающие заявки и видимые пользователю."""
    today = date.today()
    q = Deal.query.filter(
        Deal.status == 'active',
        db.or_(Deal.date_end.is_(None), Deal.date_end >= today),
        db.or_(Deal.date_start.is_(None), Deal.date_start > today),
    )
    if not user.is_admin:
        q = q.filter(db.or_(Deal.visibility == 'all',
                            Deal.visible_to.any(id=user.id)))
    return q.order_by(Deal.created_at.desc()).all()


def get_or_create_filter(user: User) -> BotFilter:
    bf = BotFilter.query.filter_by(user_id=user.id).first()
    if not bf:
        bf = BotFilter(user_id=user.id, enabled=True)
        db.session.add(bf)
        db.session.commit()
    return bf


# ──────────────────── Команды ────────────────────

def cmd_start(app, user: User) -> str:
    return (
        f'👋 Здравствуйте, <b>{esc(user.full_name)}</b>!\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        'Это бот закрытой платформы «Группа Титан».\n'
        'Сюда приходят уведомления о новых сделках (по вашим фильтрам),\n'
        'о скором старте сделок и о статусах ваших заявок.\n\n'
        '📌 Команды:\n'
        '/deals — доступные вам сделки\n'
        '/feed — последние события\n'
        '/filters — настроить фильтры уведомлений\n'
        '/myid — ваш Telegram ID\n'
        '/help — справка'
    )


def cmd_help(app, user: User) -> str:
    return (
        'ℹ️ <b>Справка</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        '• <b>/deals</b> — сделки, доступные вам для инвестирования.\n'
        '• <b>/feed</b> — последние сохранённые события (новые сделки, '
        'старты, статусы ваших заявок).\n'
        '• <b>/filters</b> — какие сделки присылать: категории, риск, '
        'мин. доходность, макс. порог входа. Персональные уведомления '
        'по вашим заявкам приходят всегда.\n'
        '• Инвестирование — только на платформе; бот работает в режиме '
        'уведомлений и просмотра.'
    )


def _deal_line(deal: Deal) -> str:
    cat = CAT_SHORT.get(deal.category, deal.category)
    parts = [f'▫️ <b>{esc(deal.title)}</b> — {cat}']
    if not deal.is_urgent_sale and (deal.expected_profit_pct or 0) > 0:
        parts.append(f'+{deal.expected_profit_pct}% год.')
    if deal.min_investment:
        parts.append(f'от {fmt_money(deal.min_investment)} ₽')
    if deal.date_start and deal.date_start > date.today():
        parts.append(f'старт {deal.date_start.strftime("%d.%m")}')
    elif deal.date_end:
        parts.append(f'до {deal.date_end.strftime("%d.%m")}')
    return ', '.join(parts)


def cmd_deals(app, user: User) -> str:
    deals = visible_active_deals(user)
    if not deals:
        return ('💼 Сейчас нет доступных вам сделок, принимающих заявки.\n'
                'Как только появится подходящая — пришлём уведомление.')
    shown = deals[:10]
    lines = [f'💼 <b>Доступные сделки ({len(deals)})</b>',
             '━━━━━━━━━━━━━━━━━━━━━']
    lines += [_deal_line(d) for d in shown]
    if len(deals) > len(shown):
        lines.append(f'… и ещё {len(deals) - len(shown)} в каталоге платформы')
    site = (app.config.get('SITE_URL') or '').rstrip('/')
    if site:
        lines.append(f'\n🔗 Каталог: {site}/catalog')
    return '\n'.join(lines)


def cmd_feed(app, user: User) -> str:
    notifs = (BotNotification.query.filter_by(user_id=user.id)
              .order_by(BotNotification.created_at.desc())
              .limit(10).all())
    if not notifs:
        return ('📭 Лента пуста — пока не было событий по вашим фильтрам.\n'
                'Настроить фильтры: /filters')
    blocks = [f'🗂 <b>Последние события ({len(notifs)})</b>']
    for n in notifs:
        when = ''
        if n.created_at:
            when = n.created_at.replace(tzinfo=timezone.utc).astimezone(MSK).strftime('%d.%m %H:%M')
        blocks.append(f'━━ {when} ━━\n{n.message}')
    text = '\n\n'.join(blocks)
    # Telegram лимит 4096 — обрезаем по границе блока
    if len(text) > 3900:
        cut = text[:3900]
        text = cut[:cut.rfind('━━')] + '\n…(показаны не все события)'
    return text


def cmd_myid(app, user_or_id) -> str:
    tid = user_or_id.telegram_id if isinstance(user_or_id, User) else user_or_id
    return f'🆔 Ваш Telegram ID: <code>{tid}</code>'


# ──────────────────── /filters: текст + клавиатура ────────────────────

def filters_text(bf: BotFilter) -> str:
    cats = bf.categories_list()
    risks = bf.risk_list()
    cats_str = ', '.join(CATEGORY_LABELS.get(c, c) for c in cats) if cats else 'все'
    risks_str = ', '.join(RISK_SHORT.get(r, r) for r in risks) if risks else 'все'
    profit_str = f'от {bf.min_profit_pct:g}%' if bf.min_profit_pct else 'любая'
    entry_str = f'до {fmt_money(bf.max_entry)} ₽' if bf.max_entry else 'любой'
    status = '✅ включены' if bf.enabled else '🔕 выключены'
    return (
        '⚙️ <b>Фильтры уведомлений о сделках</b>\n'
        '━━━━━━━━━━━━━━━━━━━━━\n'
        f'Уведомления: <b>{status}</b>\n'
        f'📁 Категории: <b>{cats_str}</b>\n'
        f'⚠️ Риск: <b>{risks_str}</b>\n'
        f'📈 Доходность: <b>{profit_str}</b>\n'
        f'💵 Порог входа: <b>{entry_str}</b>\n\n'
        'Нажимайте кнопки, чтобы переключить. Персональные уведомления '
        'по вашим заявкам приходят всегда.'
    )


def filters_keyboard(bf: BotFilter) -> dict:
    cats = set(bf.categories_list() or ALL_CATEGORIES)
    risks = set(bf.risk_list() or ALL_RISKS)

    def cat_btn(c):
        mark = '✅' if c in cats else '☐'
        return {'text': f'{mark} {CAT_SHORT[c]}', 'callback_data': f'f|cat|{c}'}

    def risk_btn(r):
        mark = '✅' if r in risks else '☐'
        return {'text': f'{mark} {RISK_SHORT[r]}', 'callback_data': f'f|risk|{r}'}

    def profit_btn(p):
        cur = bf.min_profit_pct or 0
        mark = '● ' if cur == p else ''
        label = 'любая' if p == 0 else f'{p}%+'
        return {'text': f'{mark}{label}', 'callback_data': f'f|profit|{p}'}

    def entry_btn(e):
        cur = bf.max_entry or 0
        mark = '● ' if cur == e else ''
        if e == 0:
            label = 'любой'
        elif e >= 1_000_000:
            label = f'≤{e // 1_000_000} млн'
        else:
            label = f'≤{e // 1000} тыс'
        return {'text': f'{mark}{label}', 'callback_data': f'f|entry|{e}'}

    toggle_label = '🔕 Выключить уведомления' if bf.enabled else '🔔 Включить уведомления'
    return {'inline_keyboard': [
        [cat_btn('realestate'), cat_btn('auto'), cat_btn('business')],
        [cat_btn('equipment'), cat_btn('other')],
        [risk_btn('low'), risk_btn('medium'), risk_btn('high')],
        [{'text': '📈 Доходность:', 'callback_data': 'f|noop|0'}] ,
        [profit_btn(p) for p in PROFIT_PRESETS],
        [{'text': '💵 Порог входа:', 'callback_data': 'f|noop|0'}],
        [entry_btn(e) for e in ENTRY_PRESETS],
        [{'text': toggle_label, 'callback_data': 'f|toggle|0'},
         {'text': '♻️ Сброс', 'callback_data': 'f|reset|0'}],
    ]}


def apply_filter_callback(bf: BotFilter, action: str, value: str) -> tuple[bool, str | None]:
    """Применяет клик по кнопке фильтров. Возвращает (изменилось, alert-текст)."""
    if action == 'noop':
        return False, None
    if action == 'toggle':
        bf.enabled = not bf.enabled
    elif action == 'reset':
        bf.enabled = True
        bf.categories = None
        bf.risk_levels = None
        bf.min_profit_pct = None
        bf.max_entry = None
    elif action == 'cat':
        current = set(bf.categories_list() or ALL_CATEGORIES)
        if value in current:
            if len(current) == 1:
                return False, 'Нельзя снять последнюю категорию — выключите уведомления кнопкой ниже'
            current.discard(value)
        else:
            current.add(value)
        bf.categories = None if current == set(ALL_CATEGORIES) else ','.join(sorted(current))
    elif action == 'risk':
        current = set(bf.risk_list() or ALL_RISKS)
        if value in current:
            if len(current) == 1:
                return False, 'Нельзя снять последний уровень риска — выключите уведомления кнопкой ниже'
            current.discard(value)
        else:
            current.add(value)
        bf.risk_levels = None if current == set(ALL_RISKS) else ','.join(sorted(current))
    elif action == 'profit':
        p = float(value)
        bf.min_profit_pct = None if p == 0 else p
    elif action == 'entry':
        e = float(value)
        bf.max_entry = None if e == 0 else e
    else:
        return False, None
    db.session.commit()
    return True, None


# ──────────────────── Outbox-диспетчер ────────────────────

def dispatch_outbox(api: TelegramAPI, app) -> tuple[int, int]:
    """Доставляет pending-уведомления. Возвращает (отправлено, ошибок)."""
    sent = failed = 0
    with app.app_context():
        pending = (BotNotification.query
                   .filter_by(status='pending')
                   .order_by(BotNotification.created_at.asc())
                   .limit(OUTBOX_BATCH).all())
        for n in pending:
            user = db.session.get(User, n.user_id)
            if not user or not user.telegram_id or not user.is_active:
                # Адресат недоступен (ID убрали / юзер заблокирован) — не ретраим
                n.status = 'failed'
                n.attempts += 1
                db.session.commit()
                failed += 1
                continue
            ok = api.send_message(user.telegram_id, n.message)
            n.attempts += 1
            if ok:
                n.status = 'sent'
                n.sent_at = datetime.now(timezone.utc)
                sent += 1
            else:
                if n.attempts >= MAX_ATTEMPTS:
                    n.status = 'failed'
                failed += 1
            db.session.commit()
    if sent or failed:
        logger.info(f'outbox: отправлено {sent}, ошибок {failed}')
    return sent, failed


def outbox_loop(api: TelegramAPI, app, interval: int, stop: threading.Event):
    while not stop.is_set():
        try:
            dispatch_outbox(api, app)
        except Exception as e:
            logger.error(f'outbox loop: {e}')
        stop.wait(interval)


# ──────────────────── Обработка апдейтов ────────────────────

COMMANDS = {
    '/start': cmd_start,
    '/help': cmd_help,
    '/deals': cmd_deals,
    '/feed': cmd_feed,
}


def process_message(api: TelegramAPI, app, msg: dict):
    chat = msg.get('chat') or {}
    chat_id = chat.get('id')
    if not chat_id or chat.get('type') != 'private':
        return  # работаем только в личке
    text = (msg.get('text') or '').strip()
    from_id = (msg.get('from') or {}).get('id') or chat_id

    user = resolve_user(from_id)
    if user is None:
        # /myid отвечаем и неизвестным — иначе им нечего сообщить админу
        api.send_message(chat_id, access_denied_text(from_id))
        logger.info(f'access denied: telegram_id={from_id}')
        return

    cmd = text.split('@', 1)[0].split(' ', 1)[0].lower() if text.startswith('/') else ''
    if cmd == '/filters':
        bf = get_or_create_filter(user)
        api.send_message(chat_id, filters_text(bf), reply_markup=filters_keyboard(bf))
    elif cmd == '/myid':
        api.send_message(chat_id, cmd_myid(app, user))
    elif cmd in COMMANDS:
        api.send_message(chat_id, COMMANDS[cmd](app, user))
    else:
        api.send_message(chat_id, 'Не понимаю эту команду. Список команд: /help')


def process_callback(api: TelegramAPI, app, cb: dict):
    cb_id = cb.get('id')
    from_id = (cb.get('from') or {}).get('id')
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')
    data = cb.get('data') or ''

    user = resolve_user(from_id)
    if user is None:
        api.answer_callback(cb_id, 'Доступ запрещён', show_alert=True)
        return

    parts = data.split('|')
    if len(parts) == 3 and parts[0] == 'f':
        _, action, value = parts
        bf = get_or_create_filter(user)
        changed, alert = apply_filter_callback(bf, action, value)
        api.answer_callback(cb_id, alert, show_alert=bool(alert))
        if changed and chat_id and message_id:
            api.edit_message(chat_id, message_id, filters_text(bf),
                             reply_markup=filters_keyboard(bf))
    else:
        api.answer_callback(cb_id)


def process_update(api: TelegramAPI, app, upd: dict):
    with app.app_context():
        if 'message' in upd:
            process_message(api, app, upd['message'])
        elif 'callback_query' in upd:
            process_callback(api, app, upd['callback_query'])


# ──────────────────── Main ────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description='Телеграм-бот Группы Титан')
    parser.add_argument('--once', action='store_true',
                        help='Один проход outbox + один getUpdates и выход (smoke-режим)')
    args = parser.parse_args()

    # Импорт здесь: app.py при импорте создаёт таблицы (db.create_all) —
    # бот можно стартовать до первого запуска веб-приложения
    from app import app

    token = app.config.get('TELEGRAM_USER_BOT_TOKEN') or app.config.get('TELEGRAM_BOT_TOKEN')
    proxy = app.config.get('TELEGRAM_PROXY', '')
    poll_timeout = int(app.config.get('BOT_POLL_TIMEOUT', 25))
    outbox_interval = int(app.config.get('BOT_OUTBOX_INTERVAL', 5))

    api = TelegramAPI(token, proxy)

    if args.once:
        sent, failed = dispatch_outbox(api, app)
        try:
            updates = api.get_updates(offset=0, poll_timeout=0)
            for upd in updates:
                process_update(api, app, upd)
            print(f'--once: outbox sent={sent} failed={failed}, updates={len(updates)}')
        except Exception as e:
            print(f'--once: outbox sent={sent} failed={failed}, getUpdates error: {e}')
            return 1
        return 0

    try:
        me = api.call('getMe')
        logger.info(f'Бот запущен: @{me.get("username")} ({me.get("first_name")})')
    except Exception as e:
        logger.error(f'getMe не прошёл — проверьте токен/прокси: {e}')
        return 1

    api.set_my_commands()

    stop = threading.Event()
    t = threading.Thread(target=outbox_loop, args=(api, app, outbox_interval, stop),
                         daemon=True, name='outbox')
    t.start()

    offset = 0
    try:
        while True:
            try:
                updates = api.get_updates(offset, poll_timeout)
            except (HTTPError, URLError, OSError, RuntimeError) as e:
                if '409' in str(e) or 'Conflict' in str(e):
                    logger.error('getUpdates 409: другой процесс уже поллит этого бота. Пауза 30с.')
                    time.sleep(30)
                else:
                    logger.warning(f'getUpdates: {e}; пауза 5с')
                    time.sleep(5)
                continue
            for upd in updates:
                offset = max(offset, upd.get('update_id', 0) + 1)
                try:
                    process_update(api, app, upd)
                except Exception as e:
                    logger.exception(f'Ошибка обработки апдейта: {e}')
    except KeyboardInterrupt:
        logger.info('Остановка по Ctrl+C')
    finally:
        stop.set()
    return 0


if __name__ == '__main__':
    sys.exit(main())
