"""
Событийная шина телеграм-бота (outbox-паттерн).

Веб-приложение при значимых событиях СОХРАНЯЕТ уведомления в bot_notifications
для каждого подходящего пользователя; отдельный процесс tg_bot.py доставляет их
в личные сообщения. Событие сохраняется пользователю, только если:

  1. у пользователя прописан telegram_id (бот работает только с такими),
  2. сделка ему разрешена к просмотру (Deal.user_can_see),
  3. сделка проходит его фильтры (BotFilter; нет фильтра = подходит всё).

Персональные события по заявкам (подтверждена/отклонена/закрыта) сохраняются
без фильтров — это события самого пользователя.
"""
import html
import logging

from sqlalchemy.exc import IntegrityError

from models import db, User, BotFilter, BotNotification
from telegram_notify import CATEGORY_LABELS, RISK_LABELS

logger = logging.getLogger(__name__)


def _esc(s):
    """HTML-экранирование для parse_mode=HTML — название сделки с «<» или «&»
    иначе роняет sendMessage (400 Bad Request)."""
    return html.escape(str(s or ''), quote=False)

# Типы событий
EVENT_DEAL_PUBLISHED = 'deal_published'
EVENT_DEAL_STARTING = 'deal_starting'
EVENT_INV_CONFIRMED = 'investment_confirmed'
EVENT_INV_REJECTED = 'investment_rejected'
EVENT_INV_CLOSED = 'investment_closed'


def _fmt_money(x):
    return f'{(x or 0):,.0f}'.replace(',', ' ')


# ──────────────────── Матчинг фильтров ────────────────────

def deal_matches_filter(deal, bot_filter):
    """Проходит ли сделка фильтры пользователя. bot_filter=None → подходит всё."""
    if bot_filter is None:
        return True
    if not bot_filter.enabled:
        return False
    cats = bot_filter.categories_list()
    if cats and deal.category not in cats:
        return False
    risks = bot_filter.risk_list()
    if risks and deal.risk_level not in risks:
        return False
    if bot_filter.min_profit_pct is not None and bot_filter.min_profit_pct > 0:
        if (deal.expected_profit_pct or 0) < bot_filter.min_profit_pct:
            return False
    if bot_filter.max_entry is not None and bot_filter.max_entry > 0:
        if (deal.min_investment or 0) > bot_filter.max_entry:
            return False
    return True


def deal_accepts_investors(deal, today):
    """Принимает ли сделка новых инвесторов (та же логика, что в роуте invest)."""
    if deal.status != 'active':
        return False
    if deal.date_end and deal.date_end < today:
        return False
    if deal.date_start and deal.date_start <= today:
        return False
    return True


def eligible_users_for_deal(deal):
    """Пользователи, которым сохранять deal-событие: активные инвесторы
    с прописанным telegram_id, видящие сделку и проходящие свои фильтры."""
    users = User.query.filter(
        User.role == 'investor',
        User.is_active.is_(True),
        User.telegram_id.isnot(None),
    ).all()
    result = []
    for u in users:
        if not deal.user_can_see(u):
            continue
        bf = BotFilter.query.filter_by(user_id=u.id).first()
        if not deal_matches_filter(deal, bf):
            continue
        result.append(u)
    return result


# ──────────────────── Форматирование сообщений ────────────────────

def _deal_link(app, deal):
    site = (app.config.get('SITE_URL') or '').rstrip('/')
    if not site:
        return ''
    return f'\n🔗 {site}/deal/{deal.id}'


def format_deal_published(app, deal):
    cat = CATEGORY_LABELS.get(deal.category, deal.category)
    risk = RISK_LABELS.get(deal.risk_level, deal.risk_level)
    lines = [
        '🆕 <b>Новая сделка для вас</b>',
        '━━━━━━━━━━━━━━━━━━━━━',
        f'📋 <b>{_esc(deal.title)}</b>',
        f'📁 {cat}',
    ]
    if not deal.is_urgent_sale and (deal.expected_profit_pct or 0) > 0:
        lines.append(f'📈 Доходность: <b>+{deal.expected_profit_pct}% годовых</b>')
    lines.append(f'⏱ Срок: {deal.term_display}')
    lines.append(f'⚠️ Риск: {risk}')
    if deal.min_investment:
        lines.append(f'💵 Мин. вход: <b>{_fmt_money(deal.min_investment)} ₽</b>')
    if deal.total_pool:
        lines.append(f'🎯 Пул: <b>{_fmt_money(deal.total_pool)} ₽</b>')
    return '\n'.join(lines) + _deal_link(app, deal)


def format_deal_starting(app, deal, days_until):
    if days_until == 0:
        when = 'Сегодня'
    elif days_until == 1:
        when = 'Завтра'
    else:
        when = f'Через {days_until} дн.'
    cat = CATEGORY_LABELS.get(deal.category, deal.category)
    lines = [
        '🔥 <b>Сделка скоро стартует</b>',
        '━━━━━━━━━━━━━━━━━━━━━',
        f'📋 <b>{_esc(deal.title)}</b>',
        f'📁 {cat}',
        f'📅 Старт: <b>{deal.date_start.strftime("%d.%m.%Y")}</b> ({when})',
    ]
    if (deal.expected_profit_pct or 0) > 0:
        lines.append(f'📈 Доходность: <b>+{deal.expected_profit_pct}% годовых</b>')
    if deal.min_investment:
        lines.append(f'💵 Мин. вход: <b>{_fmt_money(deal.min_investment)} ₽</b>')
    lines.append('⏳ Успейте подать заявку до старта')
    return '\n'.join(lines) + _deal_link(app, deal)


def format_investment_event(app, inv, event_type):
    deal = inv.deal
    amount = _fmt_money(inv.amount)
    if event_type == EVENT_INV_CONFIRMED:
        head = '✅ <b>Ваша заявка подтверждена</b>'
        tail = f'⏱ Срок: {inv.term_display}'
    elif event_type == EVENT_INV_REJECTED:
        head = '❌ <b>Ваша заявка отклонена</b>'
        tail = _esc((inv.notes or '').strip())
    else:  # EVENT_INV_CLOSED
        head = '🏁 <b>Ваша инвестиция закрыта</b>'
        tail = f'💰 Прибыль: <b>{_fmt_money(inv.actual_profit)} ₽</b>'
    lines = [
        head,
        '━━━━━━━━━━━━━━━━━━━━━',
        f'📋 Сделка: <b>{_esc(deal.title)}</b>',
        f'💵 Сумма: <b>{amount} ₽</b>',
    ]
    if tail:
        lines.append(tail)
    return '\n'.join(lines) + _deal_link(app, deal)


# ──────────────────── Сохранение (outbox) ────────────────────

def _save_notification(user_id, event_type, message, deal_id=None, investment_id=None):
    """Сохраняет уведомление; дубль deal-события молча пропускается
    (partial-unique индекс uq_bot_notif_user_deal_event)."""
    if investment_id is None and deal_id is not None:
        exists = BotNotification.query.filter_by(
            user_id=user_id, deal_id=deal_id, event_type=event_type,
            investment_id=None,
        ).first()
        if exists:
            return False
    notif = BotNotification(
        user_id=user_id, deal_id=deal_id, investment_id=investment_id,
        event_type=event_type, message=message, status='pending',
    )
    db.session.add(notif)
    try:
        db.session.commit()
        return True
    except IntegrityError:
        # Гонка параллельных воркеров — дубль уже сохранён
        db.session.rollback()
        return False


def emit_deal_published(app, deal):
    """Событие «опубликована сделка» всем подходящим пользователям.

    Вызывается при создании сделки И при расширении видимости (edit_deal /
    edit_visibility) — дедуп гарантирует, что каждый пользователь получит
    событие по сделке один раз.
    """
    from datetime import date as _date
    if not deal_accepts_investors(deal, _date.today()):
        return 0
    saved = 0
    for u in eligible_users_for_deal(deal):
        msg = format_deal_published(app, deal)
        if _save_notification(u.id, EVENT_DEAL_PUBLISHED, msg, deal_id=deal.id):
            saved += 1
    if saved:
        logger.info(f'bot_events: deal_published #{deal.id} — сохранено {saved} уведомлений')
    return saved


def emit_deal_starting(app, deal, days_until):
    """Событие «сделка скоро стартует» (из notify_upcoming.py, per-user)."""
    saved = 0
    for u in eligible_users_for_deal(deal):
        msg = format_deal_starting(app, deal, days_until)
        if _save_notification(u.id, EVENT_DEAL_STARTING, msg, deal_id=deal.id):
            saved += 1
    if saved:
        logger.info(f'bot_events: deal_starting #{deal.id} — сохранено {saved} уведомлений')
    return saved


def emit_investment_event(app, inv, event_type):
    """Персональное событие по заявке/инвестиции — фильтры не применяются,
    нужен только прописанный telegram_id."""
    user = db.session.get(User, inv.user_id)
    if not user or not user.telegram_id or not user.is_active:
        return False
    msg = format_investment_event(app, inv, event_type)
    return _save_notification(user.id, event_type, msg,
                              deal_id=inv.deal_id, investment_id=inv.id)
