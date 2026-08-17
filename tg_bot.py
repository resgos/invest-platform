"""
Телеграм-бот Группы Титан — отдельный процесс (systemd: gruppa-titan-bot.service).

Доступ СТРОГО по прописанному Telegram ID: админ заполняет поле «Telegram ID»
в карточке пользователя; все остальные получают отказ (с подсказкой, какой ID
сообщить админу). Бот делает две вещи:

  1. Доставляет сохранённые события из outbox (bot_notifications) в личку —
     новые сделки по фильтрам пользователя, скорый старт, статусы заявок.
     Перед отправкой deal-события перепроверяются: если админ успел сузить
     доступ, поставить сделку на паузу или сделка уже фактически стартовала —
     строка отменяется (не доставляется), но не теряется навсегда: следующая
     эмиссия того же события реанимирует её обратно в pending.
  2. Отвечает на команды: /deals /feed /filters /myid /help.

Транспорт — чистый urllib с той же прокси-логикой, что и telegram_notify
(HTTP/SOCKS5, работает из РФ), никаких новых зависимостей.

Запуск:
    python tg_bot.py            # рабочий режим (long polling)
    python tg_bot.py --once     # один проход outbox + один getUpdates (smoke)

ВАЖНО: getUpdates должен звать только один процесс. Не запускайте --once,
пока работает systemd-сервис — оба процесса прочитают одни pending-строки
и пользователи получат дубли.

Токен: TELEGRAM_USER_BOT_TOKEN (по умолчанию = TELEGRAM_BOT_TOKEN).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bot_events import EVENT_DEAL_PUBLISHED, EVENT_DEAL_STARTING
from models import db, User, Deal, BotFilter, BotNotification
from telegram_notify import _get_proxy_opener, CATEGORY_LABELS, RISK_LABELS, fmt_money, esc

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('tg_bot')

MSK = timezone(timedelta(hours=3))  # Europe/Moscow (без DST)

INSTANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
OFFSET_FILE = os.path.join(INSTANCE_DIR, 'bot_offset')

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

TRANSIENT_MAX_ATTEMPTS = 100  # net_error (сеть/5xx) ретраится долго; permanent — сразу failed
ROW_ERROR_MAX_ATTEMPTS = 5    # row_error (проблема конкретной строки, не сеть) — ретраим недолго
OUTBOX_BATCH = 50             # уведомлений за один проход диспетчера
SEND_PACING_SEC = 0.05        # пауза между sendMessage (лимит Telegram ~30 msg/s)

FEED_TEXT_BUDGET = 3900       # лимит Telegram 4096 с запасом под текст /feed целиком
FEED_BLOCK_MAX_CHARS = 900    # обрезка одного аномально длинного блока ленты (см. cmd_feed)


# ──────────────────── Telegram API (urllib + прокси) ────────────────────

class TelegramApiError(RuntimeError):
    """Ошибка Bot API с кодом, description из тела ответа и retry_after."""

    def __init__(self, status, description, retry_after=None):
        super().__init__(f'HTTP {status}: {description}')
        self.status = status
        self.description = description or ''
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str, proxy_url: str = ''):
        if not token:
            raise SystemExit('TELEGRAM_USER_BOT_TOKEN / TELEGRAM_BOT_TOKEN не задан — бот не может стартовать')
        self.base = f'https://api.telegram.org/bot{token}'
        # SOCKS5 монкипатчит socket глобально и возвращает None;
        # HTTP-прокси возвращает opener для each-request использования
        self.opener = _get_proxy_opener(proxy_url)

    def call(self, method: str, payload: dict | None = None, timeout: int = 20):
        """Возвращает result; ошибки API поднимает как TelegramApiError
        с описанием из ТЕЛА ответа (urllib кидает HTTPError на 4xx/5xx до
        разбора JSON — без чтения тела все описания терялись бы)."""
        url = f'{self.base}/{method}'
        data = json.dumps(payload or {}).encode('utf-8')
        req = Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            if self.opener:
                resp = self.opener.open(req, timeout=timeout)
            else:
                resp = urlopen(req, timeout=timeout)
        except HTTPError as e:
            raw = e.read().decode('utf-8', errors='replace')
            try:
                body = json.loads(raw)
                desc = body.get('description', '')
                retry_after = (body.get('parameters') or {}).get('retry_after')
            except ValueError:
                desc, retry_after = raw[:200], None
            raise TelegramApiError(e.code, desc, retry_after) from e
        try:
            body = json.loads(resp.read())
        finally:
            resp.close()
        if not body.get('ok'):
            raise TelegramApiError(200, body.get('description', str(body)))
        return body.get('result')

    def get_updates(self, offset: int, poll_timeout: int):
        return self.call('getUpdates', {
            'offset': offset,
            'timeout': poll_timeout,
            'allowed_updates': ['message', 'callback_query'],
        }, timeout=poll_timeout + 15)

    def send_message(self, chat_id, text, reply_markup=None):
        """Возвращает (status, retry_after).

        status — одно из:
          'ok'        — доставлено;
          'flood'     — 429, ждать retry_after, попытку не считать;
          'permanent' — 400/401/403/404: неисправимая ошибка запроса/токена
                        (бан бота, чат не найден, битая разметка, ОТОЗВАННЫЙ
                        ТОКЕН) — ретрай бессмыслен, сразу failed;
          'row_error' — прочие ошибки API уровня КОНКРЕТНОЙ строки (иной 4xx,
                        либо ok:false в теле 200-ответа — см. call()) — не
                        транспортная проблема, остальным адресатам в батче
                        это не мешает;
          'net_error' — транспортная проблема (сеть недоступна/таймаут/5xx
                        Telegram) — не специфична для строки, есть смысл
                        прервать батч и подождать следующего прохода.
        """
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
            return 'ok', None
        except TelegramApiError as e:
            if e.status == 429:
                logger.warning(f'sendMessage → {chat_id}: flood limit, retry_after={e.retry_after}')
                return 'flood', e.retry_after
            if e.status in (400, 401, 403, 404):
                # заблокировал бота / чат не найден / не смогли распарсить
                # разметку / ТОКЕН ОТОЗВАН (401) — ретрай бессмыслен
                logger.error(f'sendMessage → {chat_id}: перманентная ошибка {e}')
                return 'permanent', None
            if e.status >= 500:
                # Telegram лежит — проблема транспорта, а не этого адресата
                logger.error(f'sendMessage → {chat_id}: сервер Telegram — {e}')
                return 'net_error', None
            # прочие 4xx и ok:false в 200-ответе — ошибка ИМЕННО этой строки
            logger.error(f'sendMessage → {chat_id}: ошибка по строке — {e}')
            return 'row_error', None
        except (URLError, OSError, ValueError) as e:
            logger.error(f'sendMessage → {chat_id}: сеть — {e}')
            return 'net_error', None

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
        except TelegramApiError as e:
            # «message is not modified» — штатно при повторном клике по той же кнопке
            if 'not modified' not in e.description:
                logger.warning(f'editMessageText: {e}')
        except (URLError, OSError, ValueError) as e:
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


# ──────────────────── Персистентный offset getUpdates ────────────────────
# Telegram подтверждает апдейты только следующим getUpdates с бОльшим offset.
# Держим offset в файле: после краша/рестарта не переобрабатываем старые
# команды (toggle-кнопки неидемпотентны — повторный клик перевернул бы фильтр).

def load_offset() -> int:
    try:
        with open(OFFSET_FILE, 'r', encoding='utf-8') as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    try:
        with open(OFFSET_FILE, 'w', encoding='utf-8') as f:
            f.write(str(offset))
    except OSError as e:
        logger.warning(f'offset не сохранён: {e}')


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

def visible_active_deals_query(user: User):
    """Активные сделки, принимающие заявки и видимые пользователю.
    Бизнес-правила — методы Deal (единый источник с веб-роутами)."""
    q = Deal.query.filter(*Deal.accepting_criteria(date.today()))
    if not user.is_admin:
        q = q.filter(Deal.visible_to_user_criteria(user))
    return q.order_by(Deal.created_at.desc())


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
    q = visible_active_deals_query(user)
    total = q.count()
    if not total:
        return ('💼 Сейчас нет доступных вам сделок, принимающих заявки.\n'
                'Как только появится подходящая — пришлём уведомление.')
    shown = q.limit(10).all()
    lines = [f'💼 <b>Доступные сделки ({total})</b>',
             '━━━━━━━━━━━━━━━━━━━━━']
    lines += [_deal_line(d) for d in shown]
    if total > len(shown):
        lines.append(f'… и ещё {total - len(shown)} в каталоге платформы')
    site = (app.config.get('SITE_URL') or '').rstrip('/')
    if site:
        lines.append(f'\n🔗 Каталог: {site}/catalog')
    return '\n'.join(lines)


def _truncate_block(block: str, limit: int) -> str:
    """Обрезает один аномально длинный блок ленты до ~limit символов.

    Режем по границе строки (rfind('\\n')), а не посимвольно: в блоке только
    парные HTML-теги вида <b>...</b>, целиком лежащие в одной строке, поэтому
    отбрасывание ЦЕЛЫХ хвостовых строк не может оставить незакрытый тег.
    Если переноса строки в пределах limit нет (например поле notes — сплошной
    текст без \\n длиннее самого лимита), это крайний случай: режем жёстко по
    символам — тег порвать всё равно нечем, notes всегда чистый текст (esc()
    экранирует, но не добавляет теги)."""
    if len(block) <= limit:
        return block
    cut = block.rfind('\n', 0, limit)
    if cut <= 0:
        cut = limit
    return block[:cut].rstrip() + '\n…'


def cmd_feed(app, user: User) -> str:
    notifs = (BotNotification.query.filter_by(user_id=user.id)
              .filter(BotNotification.status != 'cancelled')
              .order_by(BotNotification.created_at.desc())
              .limit(10).all())
    if not notifs:
        return ('📭 Лента пуста — пока не было событий по вашим фильтрам.\n'
                'Настроить фильтры: /filters')
    header = f'🗂 <b>Последние события ({len(notifs)})</b>'
    blocks = []
    for n in notifs:
        when = ''
        if n.created_at:
            when = n.created_at.replace(tzinfo=timezone.utc).astimezone(MSK).strftime('%d.%m %H:%M')
        blocks.append(f'━━ {when} ━━\n{n.message}')
    # Лимит Telegram 4096: набираем блоки, пока влезает. Аномально длинный
    # ОДИНОЧНЫЙ блок (например отклонение заявки с длинным notes — поле без
    # ограничения длины) режем по символам вместо того, чтобы прервать сбор:
    # раньше, если такой блок оказывался ПЕРВЫМ (самым свежим), лента
    # прерывалась сразу на нём и показывала заголовок без единого события.
    parts = [header]
    size = len(header)
    truncated = False
    for b in blocks:
        if len(b) > FEED_BLOCK_MAX_CHARS:
            b = _truncate_block(b, FEED_BLOCK_MAX_CHARS)
            truncated = True
        if size + len(b) + 2 > FEED_TEXT_BUDGET:
            truncated = True
            break
        parts.append(b)
        size += len(b) + 2
    text = '\n\n'.join(parts)
    if truncated:
        text += '\n\n…(показаны не все события)'
    return text


def cmd_myid(app, user: User) -> str:
    return f'🆔 Ваш Telegram ID: <code>{user.telegram_id}</code>'


# ──────────────────── /filters: текст + клавиатура ────────────────────

RISK_SHORT = RISK_LABELS  # один источник подписей рисков с остальными сообщениями


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
        [{'text': '📈 Доходность:', 'callback_data': 'f|noop|0'}],
        [profit_btn(p) for p in PROFIT_PRESETS],
        [{'text': '💵 Порог входа:', 'callback_data': 'f|noop|0'}],
        [entry_btn(e) for e in ENTRY_PRESETS],
        [{'text': toggle_label, 'callback_data': 'f|toggle|0'},
         {'text': '♻️ Сброс', 'callback_data': 'f|reset|0'}],
    ]}


def _preset_value(value: str, presets: list) -> float | None:
    """callback_data приходит от клиента и может быть подделана — принимаем
    ТОЛЬКО значения из пресетов (никаких float('inf')/'nan'/мусора в БД)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v in presets else None


def apply_filter_callback(bf: BotFilter, action: str, value: str) -> tuple[bool, str | None]:
    """Применяет клик по кнопке фильтров. Возвращает (изменилось, alert-текст).
    Все значения валидируются по whitelist — мусор молча игнорируется."""
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
        if value not in ALL_CATEGORIES:
            return False, None
        current = set(bf.categories_list() or ALL_CATEGORIES)
        if value in current:
            if len(current) == 1:
                return False, 'Нельзя снять последнюю категорию — выключите уведомления кнопкой ниже'
            current.discard(value)
        else:
            current.add(value)
        bf.categories = None if current == set(ALL_CATEGORIES) else ','.join(sorted(current))
    elif action == 'risk':
        if value not in ALL_RISKS:
            return False, None
        current = set(bf.risk_list() or ALL_RISKS)
        if value in current:
            if len(current) == 1:
                return False, 'Нельзя снять последний уровень риска — выключите уведомления кнопкой ниже'
            current.discard(value)
        else:
            current.add(value)
        bf.risk_levels = None if current == set(ALL_RISKS) else ','.join(sorted(current))
    elif action == 'profit':
        p = _preset_value(value, PROFIT_PRESETS)
        if p is None:
            return False, None
        bf.min_profit_pct = None if p == 0 else p
    elif action == 'entry':
        e = _preset_value(value, ENTRY_PRESETS)
        if e is None:
            return False, None
        bf.max_entry = None if e == 0 else e
    else:
        return False, None
    db.session.commit()
    return True, None


# ──────────────────── Outbox-диспетчер ────────────────────

def _deal_event_still_relevant(n: BotNotification, user: User) -> bool:
    """Перепроверка deal-события на момент ДОСТАВКИ: сделка всё ещё активна
    (или, для «скоро старт»/«опубликована», всё ещё ПРИНИМАЕТ заявки) и видима
    адресату. Иначе (админ сузил видимость / поставил паузу, пока строка ждала
    в очереди) — событие отменяется, а не доставляется.

    Для deal_published и deal_starting_* (текст обещает «успейте подать заявку
    до старта») голой проверки status=='active' недостаточно: сделка остаётся
    active И ПОСЛЕ фактического старта (date_start наступил), а бот, полежавший
    в очереди через эту границу, прислал бы «успевайте» по уже стартовавшей
    сделке — и роут invest её всё равно отверг бы. Используем то же бизнес-
    правило, что и сам приём заявок — deal.accepts_investors()."""
    if n.investment_id is not None or n.deal_id is None:
        return True  # персональные события всегда актуальны
    deal = db.session.get(Deal, n.deal_id)
    if not deal:
        return False
    if n.event_type == EVENT_DEAL_PUBLISHED or n.event_type.startswith(EVENT_DEAL_STARTING):
        if not deal.accepts_investors(date.today()):
            return False
    elif deal.status != 'active':
        return False
    return deal.user_can_see(user)


def dispatch_outbox(api: TelegramAPI, app) -> tuple[int, int]:
    """Доставляет pending-уведомления. Возвращает (отправлено, ошибок).

    Классификация сбоев (см. TelegramAPI.send_message):
      permanent (400/401/403/404) — неисправимая ошибка запроса/токена
        (в т.ч. отозванный токен) — сразу failed, идём дальше;
      flood (429) — ждём retry_after и НЕ сжигаем попытку, следующий проход
        дособерёт остаток батча;
      row_error — ошибка API по КОНКРЕТНОЙ строке (не сеть) — инкремент
        attempts, failed при исчерпании ROW_ERROR_MAX_ATTEMPTS, ИДЁМ ДАЛЬШЕ:
        одна битая строка не должна блокировать доставку остальным (было:
        любая не-4xx-ошибка ретраилась ×100 попыток с break батча — head-of-
        line blocking всей очереди на строке одного адресата);
      net_error (сеть недоступна/таймаут/5xx Telegram) — не специфична для
        строки, остаёмся pending и прерываем батч (сеть лежит — нет смысла
        молотить остальных)."""
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
            if not _deal_event_still_relevant(n, user):
                n.status = 'cancelled'
                db.session.commit()
                continue
            status, retry_after = api.send_message(user.telegram_id, n.message)
            if status == 'ok':
                n.attempts += 1
                n.status = 'sent'
                n.sent_at = datetime.now(timezone.utc)
                db.session.commit()
                sent += 1
                time.sleep(SEND_PACING_SEC)
            elif status == 'permanent':
                n.attempts += 1
                n.status = 'failed'
                db.session.commit()
                failed += 1
            elif status == 'flood':
                # Попытку не сжигаем: лимит Telegram, а не проблема строки
                wait = min(float(retry_after or 3), 60)
                logger.info(f'outbox: flood-пауза {wait}с, остаток батча — следующим проходом')
                time.sleep(wait)
                break
            elif status == 'row_error':
                # Проблема этой строки, не сети — остальным адресатам не мешает
                n.attempts += 1
                if n.attempts >= ROW_ERROR_MAX_ATTEMPTS:
                    n.status = 'failed'
                    failed += 1
                db.session.commit()
                # НЕ break: переходим к следующей строке батча
            else:  # net_error
                n.attempts += 1
                if n.attempts >= TRANSIENT_MAX_ATTEMPTS:
                    n.status = 'failed'
                    failed += 1
                db.session.commit()
                break  # сеть лежит — не жжём попытки остальных строк
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
    '/myid': cmd_myid,
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
        api.send_message(chat_id, access_denied_text(from_id))
        logger.info(f'access denied: telegram_id={from_id}')
        return

    cmd = text.split('@', 1)[0].split(' ', 1)[0].lower() if text.startswith('/') else ''
    if cmd == '/filters':
        bf = get_or_create_filter(user)
        api.send_message(chat_id, filters_text(bf), reply_markup=filters_keyboard(bf))
    elif cmd in COMMANDS:
        api.send_message(chat_id, COMMANDS[cmd](app, user))
    else:
        api.send_message(chat_id, 'Не понимаю эту команду. Список команд: /help')


def process_callback(api: TelegramAPI, app, cb: dict):
    """Любой исход обязан ответить на callback — иначе у пользователя
    крутится вечный спиннер (клик при этом уже подтверждён offset'ом)."""
    cb_id = cb.get('id')
    try:
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
    except Exception:
        logger.exception('Ошибка обработки callback')
        try:
            db.session.rollback()
        except Exception:
            pass
        api.answer_callback(cb_id, 'Ошибка, попробуйте ещё раз', show_alert=True)


def process_update(api: TelegramAPI, app, upd: dict):
    with app.app_context():
        if 'message' in upd:
            process_message(api, app, upd['message'])
        elif 'callback_query' in upd:
            process_callback(api, app, upd['callback_query'])


# ──────────────────── Main ────────────────────

def _ack_updates(api: TelegramAPI, offset: int):
    """Подтверждает обработанные апдейты (getUpdates с новым offset)."""
    try:
        api.get_updates(offset=offset, poll_timeout=0)
    except Exception as e:
        logger.warning(f'ack updates: {e}')


def main() -> int:
    parser = argparse.ArgumentParser(description='Телеграм-бот Группы Титан')
    parser.add_argument('--once', action='store_true',
                        help='Один проход outbox + один getUpdates и выход (smoke-режим). '
                             'НЕ запускать при работающем сервисе — будут дубли доставки.')
    args = parser.parse_args()

    # Импорт здесь: app.py при импорте создаёт таблицы (db.create_all) —
    # бот можно стартовать до первого запуска веб-приложения
    from app import app

    token = app.config.get('TELEGRAM_USER_BOT_TOKEN') or app.config.get('TELEGRAM_BOT_TOKEN')
    proxy = app.config.get('TELEGRAM_PROXY', '')
    poll_timeout = int(app.config.get('BOT_POLL_TIMEOUT', 25))
    outbox_interval = int(app.config.get('BOT_OUTBOX_INTERVAL', 5))

    if not token:
        # Чистый выход (не краш): systemd с Restart=on-failure не будет
        # рестартить юнит каждые 10 секунд, пока токен не заполнен
        logger.error('Токен не задан (TELEGRAM_USER_BOT_TOKEN/TELEGRAM_BOT_TOKEN) — бот не запущен')
        return 0

    api = TelegramAPI(token, proxy)

    if args.once:
        sent, failed = dispatch_outbox(api, app)
        try:
            offset = load_offset()
            updates = api.get_updates(offset=offset, poll_timeout=0)
            for upd in updates:
                offset = max(offset, upd.get('update_id', 0) + 1)
                process_update(api, app, upd)
            if updates:
                _ack_updates(api, offset)  # без подтверждения апдейты пришли бы повторно
                save_offset(offset)
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

    # Меню команд выставляем только у ВЫДЕЛЕННОГО бота: если токен общий с
    # ботом групповых уведомлений, не перетираем его команды интерфейсом инвесторов
    if app.config.get('TELEGRAM_USER_BOT_TOKEN') != app.config.get('TELEGRAM_BOT_TOKEN'):
        api.set_my_commands()
    else:
        logger.info('Общий токен с групповым ботом — setMyCommands пропущен '
                    '(заведите отдельного бота через TELEGRAM_USER_BOT_TOKEN)')

    stop = threading.Event()
    t = threading.Thread(target=outbox_loop, args=(api, app, outbox_interval, stop),
                         daemon=True, name='outbox')
    t.start()

    offset = load_offset()
    try:
        while True:
            try:
                updates = api.get_updates(offset, poll_timeout)
            except TelegramApiError as e:
                if e.status == 409:
                    logger.error('getUpdates 409: другой процесс уже поллит этого бота. Пауза 30с.')
                    time.sleep(30)
                else:
                    logger.warning(f'getUpdates: {e}; пауза 5с')
                    time.sleep(5)
                continue
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # Сюда попадают и JSONDecodeError (прокси отдал HTML со статусом 200),
                # и IncompleteRead (оборванный long-poll) — краш-луп недопустим
                logger.warning(f'getUpdates: {e.__class__.__name__}: {e}; пауза 5с')
                time.sleep(5)
                continue
            for upd in updates:
                offset = max(offset, upd.get('update_id', 0) + 1)
                try:
                    process_update(api, app, upd)
                except Exception as e:
                    logger.exception(f'Ошибка обработки апдейта: {e}')
            if updates:
                save_offset(offset)
    except KeyboardInterrupt:
        logger.info('Остановка по Ctrl+C')
    finally:
        stop.set()
    return 0


if __name__ == '__main__':
    sys.exit(main())
