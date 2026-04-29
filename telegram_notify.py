"""
Telegram Bot — уведомления об инвестициях.

Отправляет сообщение в группу Telegram при:
  • Нажатии инвестором кнопки «Инвестировать»
  • Подтверждении/отклонении заявки админом

Настройка:
  1. Создайте бота через @BotFather → скопируйте токен
  2. Добавьте бота в группу, сделайте админом
  3. Получите chat_id группы:
     curl https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Пропишите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env

Для РФ (Telegram заблокирован):
  Установите TELEGRAM_PROXY в .env. Поддерживаются:
    - HTTP-прокси:   http://user:pass@host:port
    - SOCKS5-прокси: socks5://user:pass@host:port (нужен PySocks: pip install pysocks)
  Без прокси бот попробует прямое подключение — если не удастся,
  сообщение будет залогировано, но не потеряно.
"""

import logging
import threading
import json
import os
import ssl
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

# Category labels
CATEGORY_LABELS = {
    'realestate': '🏠 Недвижимость',
    'auto': '🚗 Автомобили',
    'business': '💼 Бизнес',
    'equipment': '⚙️ Оборудование',
    'other': '📦 Другое',
}

RISK_LABELS = {
    'low': '🟢 Низкий',
    'medium': '🟡 Средний',
    'high': '🔴 Высокий',
}


def _get_proxy_opener(proxy_url):
    """Build a urllib opener with proxy support (HTTP/HTTPS/SOCKS5)."""
    if not proxy_url:
        return None

    proxy_url = proxy_url.strip()

    # SOCKS5 proxy — requires PySocks
    if proxy_url.startswith('socks5://') or proxy_url.startswith('socks5h://'):
        try:
            import socks
            import socket

            # socks5h:// = remote DNS resolution через прокси (нужно когда сервер
            # резолвит api.telegram.org в IPv6, а PySocks не поддерживает IPv6).
            # socks5:// = локальный DNS — может вернуть IPv6 → краш PySocks.
            remote_dns = proxy_url.startswith('socks5h://')

            # Parse socks proxy URL
            url_part = proxy_url.split('://', 1)[1]
            auth = None
            host_port = url_part

            if '@' in url_part:
                auth_str, host_port = url_part.rsplit('@', 1)
                parts = auth_str.split(':', 1)
                auth = (parts[0], parts[1] if len(parts) > 1 else '')

            if ':' in host_port:
                host, port = host_port.rsplit(':', 1)
                port = int(port)
            else:
                host = host_port
                port = 1080

            socks.set_default_proxy(
                socks.SOCKS5, host, port,
                rdns=remote_dns,  # remote DNS resolution
                username=auth[0] if auth else None,
                password=auth[1] if auth else None
            )
            socket.socket = socks.socksocket

            # Принудительно резолвим в IPv4 — PySocks 1.7.x не поддерживает IPv6.
            # Без этого getaddrinfo может вернуть IPv6 для api.telegram.org и
            # PySocks упадёт с «PySocks doesn't support IPv6».
            _orig_getaddrinfo = socket.getaddrinfo
            def _ipv4_only_getaddrinfo(host, port, family=0, *args, **kwargs):
                return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
            socket.getaddrinfo = _ipv4_only_getaddrinfo

            logger.info(f'Telegram: SOCKS5-прокси активирован → {host}:{port} (rdns={remote_dns}, IPv4-only)')
            return None  # socks monkeypatches socket globally, no opener needed

        except ImportError:
            logger.error('Telegram: для SOCKS5 нужен пакет PySocks (pip install pysocks)')
            return None

    # HTTP/HTTPS proxy
    proxy_handler = ProxyHandler({
        'http': proxy_url,
        'https': proxy_url,
    })
    opener = build_opener(proxy_handler)
    logger.info(f'Telegram: HTTP-прокси активирован → {proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url}')
    return opener


def _send_telegram_message(bot_token, chat_id, text, parse_mode='HTML', proxy_url=None):
    """Send message via Telegram Bot API (non-blocking, runs in thread)."""
    if not bot_token or not chat_id:
        logger.warning('Telegram: BOT_TOKEN или CHAT_ID не настроены, пропускаем отправку')
        return False

    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload = json.dumps({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': True,
    }).encode('utf-8')

    req = Request(url, data=payload, headers={'Content-Type': 'application/json'})
    opener = _get_proxy_opener(proxy_url)

    try:
        if opener:
            resp = opener.open(req, timeout=15)
        else:
            resp = urlopen(req, timeout=15)

        result = json.loads(resp.read())
        resp.close()

        if not result.get('ok'):
            logger.error(f'Telegram API error: {result}')
            return False
        else:
            logger.info(f'Telegram: сообщение отправлено в {chat_id}')
            return True

    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        logger.error(f'Telegram: HTTP {e.code} — {body}')
    except URLError as e:
        logger.error(f'Telegram: ошибка сети — {e.reason}. '
                     f'{"Проверьте TELEGRAM_PROXY в .env" if not proxy_url else "Проверьте настройки прокси"}')
    except Exception as e:
        logger.error(f'Telegram: непредвиденная ошибка — {e}')

    return False


def send_async(bot_token, chat_id, text, parse_mode='HTML', proxy_url=None):
    """Fire-and-forget: отправка в отдельном потоке, чтобы не блокировать запрос."""
    t = threading.Thread(
        target=_send_telegram_message,
        args=(bot_token, chat_id, text, parse_mode, proxy_url)
    )
    t.daemon = True
    t.start()


def format_investment_notification(investor_name, deal_title, amount, deal_category,
                                    deal_profit_pct, deal_term, deal_risk,
                                    investor_phone=None):
    """Форматирует HTML-сообщение об инвестиции для Telegram."""
    cat_label = CATEGORY_LABELS.get(deal_category, deal_category)
    risk_label = RISK_LABELS.get(deal_risk, deal_risk)

    amount_fmt = f'{amount:,.0f}'.replace(',', ' ')
    phone_line = f'\n📞 Телефон: <b>{investor_phone}</b>' if investor_phone else ''

    msg = (
        f'💰 <b>Новая заявка на инвестицию</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'👤 Инвестор: <b>{investor_name}</b>{phone_line}\n'
        f'📋 Сделка: <b>{deal_title}</b>\n'
        f'💵 Сумма: <b>{amount_fmt} ₽</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'📁 Категория: {cat_label}\n'
        f'📈 Доходность: <b>{deal_profit_pct}%</b>\n'
        f'⏱ Срок: {deal_term}\n'
        f'⚠️ Риск: {risk_label}\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'⏳ <b>Ожидает подтверждения администратором</b>'
    )
    return msg


def notify_investment(app, investor_name, deal_title, amount, deal_category,
                      deal_profit_pct, deal_term, deal_risk, investor_phone=None):
    """Основная функция — вызывается из маршрута invest."""
    bot_token = app.config.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = app.config.get('TELEGRAM_CHAT_ID', '')
    proxy_url = app.config.get('TELEGRAM_PROXY', '')

    if not bot_token or not chat_id:
        logger.info('Telegram: токен/chat_id не настроены — уведомление пропущено')
        return

    text = format_investment_notification(
        investor_name, deal_title, amount,
        deal_category, deal_profit_pct, deal_term, deal_risk,
        investor_phone=investor_phone
    )
    send_async(bot_token, chat_id, text, proxy_url=proxy_url)


def format_status_notification(investor_name, deal_title, amount, status, admin_name):
    """Форматирует уведомление об изменении статуса заявки."""
    amount_fmt = f'{amount:,.0f}'.replace(',', ' ')

    if status == 'confirmed':
        icon = '✅'
        status_text = 'ПОДТВЕРЖДЕНА'
    elif status == 'closed':
        icon = '🏁'
        status_text = 'ЗАКРЫТА'
    else:
        icon = '❌'
        status_text = 'ОТКЛОНЕНА'

    msg = (
        f'{icon} <b>Заявка {status_text}</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'👤 Инвестор: <b>{investor_name}</b>\n'
        f'📋 Сделка: <b>{deal_title}</b>\n'
        f'💵 Сумма: <b>{amount_fmt} ₽</b>\n'
        f'👨\u200d💼 Админ: {admin_name}\n'
    )
    return msg


def notify_investment_status(app, investor_name, deal_title, amount, status, admin_name):
    """Уведомляет в Telegram об изменении статуса заявки."""
    bot_token = app.config.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = app.config.get('TELEGRAM_CHAT_ID', '')
    proxy_url = app.config.get('TELEGRAM_PROXY', '')

    if not bot_token or not chat_id:
        return

    text = format_status_notification(investor_name, deal_title, amount, status, admin_name)
    send_async(bot_token, chat_id, text, proxy_url=proxy_url)


def format_upcoming_deal_notification(deal_title, days_until, date_start_str,
                                       deal_profit_pct, deal_category, min_investment, total_pool):
    """Форматирует уведомление о приближающемся старте сделки."""
    cat_label = CATEGORY_LABELS.get(deal_category, deal_category)
    if days_until == 0:
        when = 'Сегодня'
    elif days_until == 1:
        when = 'Завтра'
    else:
        when = f'Через {days_until} дн.'
    return (
        f'🔥 <b>Сделка скоро стартует</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━\n'
        f'📋 <b>{deal_title}</b>\n'
        f'📁 Категория: {cat_label}\n'
        f'📅 Старт: <b>{date_start_str}</b> ({when})\n'
        f'📈 Доходность: <b>+{deal_profit_pct}% годовых</b>\n'
        f'💵 Мин. вход: <b>{min_investment:,.0f} ₽</b>\n'
        f'🎯 Пул: <b>{total_pool:,.0f} ₽</b>\n'
    ).replace(',', ' ')


def notify_upcoming_deal(app, deal_title, days_until, date_start_str,
                         deal_profit_pct, deal_category, min_investment, total_pool):
    """Уведомляет в Telegram о приближающемся старте сделки."""
    bot_token = app.config.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = app.config.get('TELEGRAM_CHAT_ID', '')
    proxy_url = app.config.get('TELEGRAM_PROXY', '')
    if not bot_token or not chat_id:
        return
    text = format_upcoming_deal_notification(
        deal_title, days_until, date_start_str,
        deal_profit_pct, deal_category, min_investment, total_pool
    )
    send_async(bot_token, chat_id, text, proxy_url=proxy_url)


def test_connection(bot_token, proxy_url=None):
    """Тест подключения к Telegram API. Возвращает (ok, info_str)."""
    if not bot_token:
        return False, 'Токен бота не указан'

    url = f'https://api.telegram.org/bot{bot_token}/getMe'
    req = Request(url, headers={'Content-Type': 'application/json'})
    opener = _get_proxy_opener(proxy_url)

    try:
        if opener:
            resp = opener.open(req, timeout=10)
        else:
            resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())
        resp.close()

        if data.get('ok'):
            bot = data['result']
            return True, f"@{bot['username']} ({bot['first_name']})"
        return False, f"API ответил: {data}"

    except URLError as e:
        return False, f"Ошибка сети: {e.reason}. Возможно нужен прокси (TELEGRAM_PROXY в .env)"
    except Exception as e:
        return False, f"Ошибка: {e}"
