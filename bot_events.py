"""
Событийная шина телеграм-бота (outbox-паттерн).

Веб-приложение при значимых событиях СОХРАНЯЕТ уведомления в bot_notifications
для каждого подходящего пользователя; отдельный процесс tg_bot.py доставляет их
в личные сообщения. Событие сохраняется пользователю, только если:

  1. у пользователя прописан telegram_id (бот работает только с такими),
  2. сделка ему разрешена к просмотру (Deal.user_can_see),
  3. сделка проходит его фильтры (BotFilter; нет фильтра = подходит всё).

Персональные события по заявкам (подтверждена/отклонена/закрыта/прибыль)
сохраняются без фильтров — это события самого пользователя.

Дедуп deal-событий: одна строка на (user, deal, event_type). Повторная эмиссия
обновляет текст ещё не доставленных строк и реанимирует failed (например, после
исправления опечатки в telegram_id); уже доставленные повторно не шлются.
"""
import logging
from datetime import date

from sqlalchemy.exc import IntegrityError

from models import db, User, BotFilter, BotNotification, CAR_LABELS
from telegram_notify import CATEGORY_LABELS, RISK_LABELS, fmt_money, esc

logger = logging.getLogger(__name__)

# Типы событий
EVENT_DEAL_PUBLISHED = 'deal_published'
EVENT_DEAL_STARTING = 'deal_starting'   # + суффикс горизонта: deal_starting_3
EVENT_INV_CONFIRMED = 'investment_confirmed'
EVENT_INV_REJECTED = 'investment_rejected'
EVENT_INV_CLOSED = 'investment_closed'
EVENT_PROFIT_PAID = 'profit_paid'


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
        # Срочные продажи не имеют доходности по определению (profit принудительно 0)
        # — порог доходности к ним не применяется, иначе они не доходили бы никому
        if not deal.is_urgent_sale and (deal.expected_profit_pct or 0) < bot_filter.min_profit_pct:
            return False
    if bot_filter.max_entry is not None and bot_filter.max_entry > 0:
        if (deal.min_investment or 0) > bot_filter.max_entry:
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
    if not users:
        return []
    # Один запрос фильтров на всех вместо N+1
    filters_by_user = {
        bf.user_id: bf
        for bf in BotFilter.query.filter(
            BotFilter.user_id.in_([u.id for u in users])).all()
    }
    # Для адресной видимости — один запрос списка допущенных вместо N точечных
    allowed_ids = None
    if deal.visibility != 'all':
        allowed_ids = {u.id for u in deal.visible_to.all()}
    result = []
    for u in users:
        if allowed_ids is not None and u.id not in allowed_ids:
            continue
        if not deal_matches_filter(deal, filters_by_user.get(u.id)):
            continue
        result.append(u)
    return result


# ──────────────────── Форматирование сообщений ────────────────────

def _deal_link(app, deal):
    site = (app.config.get('SITE_URL') or '').rstrip('/')
    if not site:
        return ''
    return f'\n🔗 {site}/deal/{deal.id}'


def _car_summary_line(deal):
    """Короткая строка об авто для сообщений бота: BMW X5, 2022, 45 000 км, автомат."""
    if deal.category != 'auto':
        return None
    parts = []
    name = ' '.join(p for p in (deal.car_brand, deal.car_model) if p)
    if name:
        parts.append(esc(name))
    if deal.car_year:
        parts.append(str(deal.car_year))
    if deal.car_mileage:
        parts.append(f'{fmt_money(deal.car_mileage)} км')
    if deal.car_transmission:
        parts.append(CAR_LABELS['transmission'].get(deal.car_transmission,
                                                    deal.car_transmission).lower())
    if deal.car_drive:
        parts.append(f"привод {CAR_LABELS['drive'].get(deal.car_drive, deal.car_drive).lower()}")
    return f'🚗 {", ".join(parts)}' if parts else None


def format_deal_published(app, deal):
    cat = CATEGORY_LABELS.get(deal.category, deal.category)
    risk = RISK_LABELS.get(deal.risk_level, deal.risk_level)
    lines = [
        '🆕 <b>Новая сделка для вас</b>',
        '━━━━━━━━━━━━━━━━━━━━━',
        f'📋 <b>{esc(deal.title)}</b>',
        f'📁 {cat}',
    ]
    car_line = _car_summary_line(deal)
    if car_line:
        lines.append(car_line)
    if not deal.is_urgent_sale and (deal.expected_profit_pct or 0) > 0:
        lines.append(f'📈 Доходность: <b>+{deal.expected_profit_pct}% годовых</b>')
    lines.append(f'⏱ Срок: {deal.term_display}')
    lines.append(f'⚠️ Риск: {risk}')
    if deal.min_investment:
        lines.append(f'💵 Мин. вход: <b>{fmt_money(deal.min_investment)} ₽</b>')
    if deal.total_pool:
        lines.append(f'🎯 Пул: <b>{fmt_money(deal.total_pool)} ₽</b>')
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
        f'📋 <b>{esc(deal.title)}</b>',
        f'📁 {cat}',
        f'📅 Старт: <b>{deal.date_start.strftime("%d.%m.%Y")}</b> ({when})',
    ]
    if (deal.expected_profit_pct or 0) > 0:
        lines.append(f'📈 Доходность: <b>+{deal.expected_profit_pct}% годовых</b>')
    if deal.min_investment:
        lines.append(f'💵 Мин. вход: <b>{fmt_money(deal.min_investment)} ₽</b>')
    lines.append('⏳ Успейте подать заявку до старта')
    return '\n'.join(lines) + _deal_link(app, deal)


def format_investment_event(app, inv, event_type, amount_paid=None):
    deal = inv.deal
    amount = fmt_money(inv.amount)
    if event_type == EVENT_INV_CONFIRMED:
        head = '✅ <b>Ваша заявка подтверждена</b>'
        tail = f'⏱ Срок: {inv.term_display}'
    elif event_type == EVENT_INV_REJECTED:
        head = '❌ <b>Ваша заявка отклонена</b>'
        tail = esc((inv.notes or '').strip())
    elif event_type == EVENT_PROFIT_PAID:
        head = '💰 <b>Начислена прибыль</b>'
        tail = f'➕ Сумма начисления: <b>{fmt_money(amount_paid)} ₽</b>'
    else:  # EVENT_INV_CLOSED
        head = '🏁 <b>Ваша инвестиция закрыта</b>'
        tail = f'💰 Прибыль: <b>{fmt_money(inv.actual_profit)} ₽</b>'
    lines = [
        head,
        '━━━━━━━━━━━━━━━━━━━━━',
        f'📋 Сделка: <b>{esc(deal.title)}</b>',
        f'💵 Сумма: <b>{amount} ₽</b>',
    ]
    if tail:
        lines.append(tail)
    return '\n'.join(lines) + _deal_link(app, deal)


# ──────────────────── Сохранение (outbox) ────────────────────

def _upsert_deal_notification(user_id, event_type, message, deal_id):
    """Deal-событие с дедупом. Возвращает True, если добавлена НОВАЯ строка.

    Существующая строка: sent → не трогаем (не спамим повторно);
    pending → обновляем текст (правки сделки доезжают до недоставленных);
    failed → реанимируем в pending (например, админ исправил telegram_id).
    """
    existing = BotNotification.query.filter_by(
        user_id=user_id, deal_id=deal_id, event_type=event_type,
        investment_id=None,
    ).first()
    if existing:
        if existing.status == 'pending':
            existing.message = message
        elif existing.status == 'failed':
            existing.status = 'pending'
            existing.attempts = 0
            existing.message = message
        return False
    db.session.add(BotNotification(
        user_id=user_id, deal_id=deal_id, investment_id=None,
        event_type=event_type, message=message, status='pending',
    ))
    return True


def _emit_deal_event(app, deal, event_type, message):
    """Общий фан-аут deal-события: один текст, батч-выборка получателей,
    один commit; гонка уникального индекса — rollback и точечный повтор."""
    users = eligible_users_for_deal(deal)
    saved = 0
    try:
        for u in users:
            if _upsert_deal_notification(u.id, event_type, message, deal.id):
                saved += 1
        db.session.commit()
    except IntegrityError:
        # Параллельный воркер вставил дубль — откатываемся и повторяем по одному
        db.session.rollback()
        saved = 0
        for u in users:
            try:
                if _upsert_deal_notification(u.id, event_type, message, deal.id):
                    saved += 1
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
    if saved:
        logger.info(f'bot_events: {event_type} #{deal.id} — сохранено {saved} уведомлений')
    return saved


def emit_deal_published(app, deal):
    """Событие «опубликована сделка» всем подходящим пользователям.

    Вызывается при создании сделки И при расширении видимости (edit_deal /
    edit_visibility / реактивация) — дедуп гарантирует, что каждый пользователь
    получит событие по сделке один раз; недоставленным текст обновляется.
    """
    if not deal.accepts_investors(date.today()):
        return 0
    return _emit_deal_event(app, deal, EVENT_DEAL_PUBLISHED,
                            format_deal_published(app, deal))


def emit_deal_starting(app, deal, days_until):
    """Событие «сделка скоро стартует» (из notify_upcoming.py, per-user).

    Горизонт входит в event_type (deal_starting_3, deal_starting_1, ...) —
    напоминания за 3 дня и накануне НЕ схлопываются дедупом."""
    event_type = f'{EVENT_DEAL_STARTING}_{days_until}'
    return _emit_deal_event(app, deal, event_type,
                            format_deal_starting(app, deal, days_until))


def emit_investment_event(app, inv, event_type, amount_paid=None):
    """Персональное событие по заявке/инвестиции — фильтры не применяются,
    нужен только прописанный telegram_id."""
    user = db.session.get(User, inv.user_id)
    if not user or not user.telegram_id or not user.is_active:
        return False
    msg = format_investment_event(app, inv, event_type, amount_paid=amount_paid)
    db.session.add(BotNotification(
        user_id=user.id, deal_id=inv.deal_id, investment_id=inv.id,
        event_type=event_type, message=msg, status='pending',
    ))
    try:
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False
