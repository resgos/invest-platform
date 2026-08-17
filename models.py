from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone, date, timedelta
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, 'connect')
def _sqlite_pragmas(dbapi_conn, _record):
    """WAL + busy_timeout для SQLite: к базе одновременно ходят gunicorn-воркеры
    и отдельный процесс телеграм-бота (tg_bot.py) — без WAL писатели блокируют
    читателей и наоборот. Для не-SQLite соединений — no-op."""
    import sqlite3
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.execute('PRAGMA busy_timeout=5000')
    cur.execute('PRAGMA foreign_keys=ON')
    cur.close()


# Человекочитаемые подписи автомобильных характеристик — единый словарь
# для шаблонов (детальная страница, каталог) и телеграм-сообщений
CAR_LABELS = {
    'transmission': {'manual': 'Механика', 'auto': 'Автомат',
                     'robot': 'Робот', 'variator': 'Вариатор'},
    'fuel': {'petrol': 'Бензин', 'diesel': 'Дизель', 'hybrid': 'Гибрид',
             'electric': 'Электро', 'gas': 'Газ (LPG)'},
    'body': {'sedan': 'Седан', 'hatchback': 'Хэтчбек', 'wagon': 'Универсал',
             'suv': 'Внедорожник', 'coupe': 'Купе', 'minivan': 'Минивэн',
             'pickup': 'Пикап', 'liftback': 'Лифтбек', 'cabrio': 'Кабриолет',
             'van': 'Фургон'},
    'drive': {'fwd': 'Передний', 'rwd': 'Задний', 'awd': 'Полный'},
    'wheel': {'left': 'Левый', 'right': 'Правый'},
    'condition': {'not_beaten': 'Не битый', 'beaten': 'Битый'},
    'pts': {'original': 'Оригинал', 'duplicate': 'Дубликат'},
    'owners': {1: '1 владелец', 2: '2 владельца', 3: '3 и более'},
}


# Many-to-many: which investors can see which deals
deal_visibility = db.Table(
    'deal_visibility',
    db.Column('deal_id', db.Integer, db.ForeignKey('deals.id', ondelete='CASCADE'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('assigned_at', db.DateTime, default=lambda: datetime.now(timezone.utc))
)


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(128), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    # Telegram ID для доступа к боту (tg_bot.py). Прописывает админ; бот работает
    # ТОЛЬКО с пользователями, у которых поле заполнено. BigInteger — id Telegram
    # уже перевалили за int32.
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=True, index=True)
    role = db.Column(db.String(20), nullable=False, default='investor', index=True)
    is_active = db.Column(db.Boolean, default=True)
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime, nullable=True)

    investments = db.relationship('Investment', backref='investor', lazy='dynamic')
    transactions = db.relationship('Transaction', backref='user', lazy='dynamic')
    audit_logs = db.relationship('AuditLog', backref='user', lazy='dynamic')

    @property
    def is_admin(self):
        return self.role == 'admin'

    def __repr__(self):
        return f'<User {self.username}>'


class Deal(db.Model):
    """
    Deal (объявление/предложение).

    Поля срока:
      - date_start (Date, optional) — фиксированная дата старта сделки.
        Если указана, инвестиции начинают действовать с этой даты, а не с даты
        подтверждения. До этой даты сделка считается «предстоящей» и помечается
        горящим маркером в каталоге, если стартует в ближайшие 7 дней.
      - date_end (Date, optional) — конкретная дата окончания приёма инвестиций.
        Если указана, все инвестиции действуют до этой даты.
      - investment_term_months (Integer, optional) — срок инвестиции в месяцах.
        Если указан, каждая инвестиция действует N месяцев от даты старта.
    """
    __tablename__ = 'deals'

    id = db.Column(db.Integer, primary_key=True)
    deal_type = db.Column(db.String(20), nullable=False, default='investment', index=True)  # investment | urgent_sale
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False, index=True)
    subcategory = db.Column(db.String(100))

    price = db.Column(db.Float, nullable=False)  # Сумма займа (ранее — стоимость актива)
    market_value = db.Column(db.Float, nullable=True)  # Рыночная стоимость объекта (для расчёта КЗ)
    expected_profit_pct = db.Column(db.Float, default=0)
    investment_term_months = db.Column(db.Integer, nullable=True)  # optional term in months
    investment_term_days = db.Column(db.Integer, nullable=True)    # optional term in days
    date_start = db.Column(db.Date, nullable=True, index=True)  # optional fixed start date
    date_end = db.Column(db.Date, nullable=True)  # optional fixed end date
    min_investment = db.Column(db.Float, default=0)
    risk_level = db.Column(db.String(20), default='medium', index=True)
    total_pool = db.Column(db.Float, default=0)
    collected_amount = db.Column(db.Float, default=0)

    contact_info = db.Column(db.Text)
    images = db.Column(db.Text)  # comma-separated filenames
    status = db.Column(db.String(20), default='active', index=True)
    visibility = db.Column(db.String(20), default='selected')  # 'all' or 'selected'

    # Real estate fields
    property_type = db.Column(db.String(50))
    area = db.Column(db.Float)
    rooms = db.Column(db.Integer)
    location = db.Column(db.String(200))
    floor = db.Column(db.Integer)
    total_floors = db.Column(db.Integer)

    # Auto fields (набор характеристик по образцу Авто.ру/Дром)
    car_brand = db.Column(db.String(50))
    car_model = db.Column(db.String(50))
    car_year = db.Column(db.Integer)
    car_power = db.Column(db.Integer)          # л.с.
    car_mileage = db.Column(db.Integer)        # км
    car_transmission = db.Column(db.String(30))  # manual|auto|robot|variator
    car_fuel = db.Column(db.String(30))          # petrol|diesel|hybrid|electric|gas
    car_body = db.Column(db.String(30))          # sedan|hatchback|wagon|suv|coupe|minivan|pickup|liftback|cabrio|van
    car_engine_volume = db.Column(db.Float)      # л
    car_drive = db.Column(db.String(10))         # fwd|rwd|awd
    car_wheel = db.Column(db.String(10))         # left|right
    car_owners = db.Column(db.Integer)           # владельцев по ПТС: 1|2|3 (3 = «3 и более»)
    car_condition = db.Column(db.String(20))     # not_beaten|beaten
    car_pts = db.Column(db.String(12))           # original|duplicate
    car_color = db.Column(db.String(30))
    car_vin = db.Column(db.String(20))

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    creator = db.relationship('User', backref='created_deals')
    investments = db.relationship('Investment', backref='deal', lazy='dynamic')
    visible_to = db.relationship('User', secondary=deal_visibility, backref='visible_deals', lazy='dynamic')

    @property
    def is_urgent_sale(self):
        return self.deal_type == 'urgent_sale'

    @property
    def loan_ratio(self):
        """Коэффициент займа — % от рыночной стоимости. None если рыночная не указана."""
        if not self.market_value or self.market_value <= 0:
            return None
        return round(self.price / self.market_value * 100, 1)

    @property
    def loan_ratio_class(self):
        """CSS-класс для отображения КЗ: зеленый до 50%, жёлтый 50-70%, красный выше."""
        ratio = self.loan_ratio
        if ratio is None:
            return ''
        if ratio <= 50:
            return 'text-profit'  # зелёный
        if ratio <= 70:
            return 'text-warning'  # жёлтый
        return 'text-danger'  # красный

    @property
    def pool_pct(self):
        if self.total_pool and self.total_pool > 0:
            return min(round(self.collected_amount / self.total_pool * 100, 1), 100)
        return 0

    @property
    def remaining(self):
        return max(self.total_pool - self.collected_amount, 0)

    HOT_THRESHOLD_DAYS = 7  # сделка помечается «горящей», если стартует в ближайшие N дней

    @property
    def term_display(self):
        """Human-readable term info for the deal (announcement level)."""
        parts = []
        if self.date_start and self.date_end:
            parts.append(f"{self.date_start.strftime('%d.%m.%Y')} — {self.date_end.strftime('%d.%m.%Y')}")
        elif self.date_start:
            parts.append(f"с {self.date_start.strftime('%d.%m.%Y')}")
        elif self.date_end:
            parts.append(f"до {self.date_end.strftime('%d.%m.%Y')}")
        if self.investment_term_months:
            parts.append(f"{self.investment_term_months} мес.")
        if self.investment_term_days:
            parts.append(f"{self.investment_term_days} дн.")
        if parts:
            return ' / '.join(parts)
        return 'Бессрочно'

    @property
    def has_started(self):
        """Стартовала ли сделка (если задана date_start)."""
        if self.date_start:
            return self.date_start <= date.today()
        return True  # без фиксированного старта — считаем активной с момента создания

    @property
    def days_until_start(self):
        """Дней до старта (None — если уже стартовала или дата не задана)."""
        if self.date_start and self.date_start > date.today():
            return (self.date_start - date.today()).days
        return None

    @property
    def is_starting_soon(self):
        """Сделка скоро стартует — помечаем горящей в каталоге."""
        d = self.days_until_start
        return d is not None and d <= self.HOT_THRESHOLD_DAYS

    @property
    def term_months_for_calc(self):
        """Term in months for calculator. Prefers investment_term_months, then days, then date_end."""
        if self.investment_term_months:
            return self.investment_term_months
        if self.investment_term_days:
            return max(round(self.investment_term_days / 30), 1)
        if self.date_end:
            delta = (self.date_end - date.today()).days
            return max(round(delta / 30), 1)
        return 12  # fallback

    @property
    def term_days_for_calc(self):
        """Term in days for calculator. Use date_start as base if it's in the future."""
        base = self.date_start if (self.date_start and self.date_start > date.today()) else date.today()
        if self.investment_term_days:
            return self.investment_term_days
        if self.investment_term_months:
            return self.investment_term_months * 30
        if self.date_end:
            delta = (self.date_end - base).days
            return max(delta, 1)
        return 365  # fallback

    @property
    def is_expired(self):
        """Check if deal end date has passed."""
        if self.date_end:
            return self.date_end < date.today()
        return False

    @property
    def days_remaining(self):
        """Days until deal ends (only if date_end is set)."""
        if self.date_end:
            delta = (self.date_end - date.today()).days
            return max(delta, 0)
        return None

    def user_can_see(self, user):
        if user and user.is_admin:
            return True
        if self.visibility == 'all':
            return True
        if user and self.visible_to.filter_by(id=user.id).first():
            return True
        return False

    def accepts_investors(self, today=None):
        """Единое бизнес-правило «сделка принимает новых инвесторов»:
        активна, срок приёма не истёк, фиксированный старт ещё не наступил.
        Используется роутом invest, шиной бота и каталогом (SQL-версия ниже) —
        менять политику ТОЛЬКО здесь."""
        today = today or date.today()
        if self.status != 'active':
            return False
        if self.date_end and self.date_end < today:
            return False
        if self.date_start and self.date_start <= today:
            return False
        return True

    @classmethod
    def accepting_criteria(cls, today=None):
        """SQL-эквивалент accepts_investors для query.filter(*...)."""
        today = today or date.today()
        return [
            cls.status == 'active',
            db.or_(cls.date_end.is_(None), cls.date_end >= today),
            db.or_(cls.date_start.is_(None), cls.date_start > today),
        ]

    @classmethod
    def visible_to_user_criteria(cls, user):
        """SQL-предикат видимости сделок пользователю (админ видит всё —
        вызывающий код не применяет предикат для админа)."""
        return db.or_(cls.visibility == 'all', cls.visible_to.any(id=user.id))


class Investment(db.Model):
    """
    Investment — конкретная инвестиция пользователя в Deal.

    Поля срока:
      - date_start (Date) — дата начала (авто: день подтверждения или день создания).
      - date_end (Date, optional) — дата окончания (рассчитывается при подтверждении).
    """
    __tablename__ = 'investments'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    deal_id = db.Column(db.Integer, db.ForeignKey('deals.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    expected_profit = db.Column(db.Float, default=0)
    actual_profit = db.Column(db.Float, default=0)  # фактически полученная прибыль
    status = db.Column(db.String(20), default='active')
    invested_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text)

    # Investment-level dates
    date_start = db.Column(db.Date, nullable=True)
    date_end = db.Column(db.Date, nullable=True)

    # Если admin вручную задал ожидаемую прибыль — pro-rata пересчёт её не перезаписывает
    expected_profit_manual = db.Column(db.Boolean, default=False, nullable=False)

    transactions = db.relationship('Transaction', backref='investment', lazy='dynamic')

    @property
    def term_display(self):
        """Human-readable term for this specific investment."""
        if self.date_start and self.date_end:
            return f"{self.date_start.strftime('%d.%m.%Y')} — {self.date_end.strftime('%d.%m.%Y')}"
        if self.date_start:
            return f"с {self.date_start.strftime('%d.%m.%Y')}"
        if self.date_end:
            return f"до {self.date_end.strftime('%d.%m.%Y')}"
        return '—'

    @property
    def term_months(self):
        """Calculate term in months for this investment."""
        if self.date_start and self.date_end:
            delta = (self.date_end - self.date_start).days
            return max(round(delta / 30), 1)
        return None

    @property
    def days_remaining(self):
        """Days remaining until this investment ends."""
        if self.date_end:
            delta = (self.date_end - date.today()).days
            return max(delta, 0)
        return None

    @property
    def is_expired(self):
        """Check if this investment's end date has passed."""
        if self.date_end:
            return self.date_end < date.today()
        return False

    @property
    def profit_progress_pct(self):
        """Percentage of expected profit that has been received."""
        if self.expected_profit and self.expected_profit > 0:
            return min(round(self.actual_profit / self.expected_profit * 100, 1), 100)
        return 0

    @property
    def remaining_profit(self):
        """Expected profit minus actual profit received."""
        return max(self.expected_profit - self.actual_profit, 0)

    @property
    def days_passed(self):
        """Сколько дней прошло с начала инвестиции до сегодня."""
        if not self.date_start:
            if self.invested_at:
                return max((date.today() - self.invested_at.date()).days, 0)
            return 0
        return max((date.today() - self.date_start).days, 0)

    @property
    def pro_rata_profit_today(self):
        """Прибыль pro-rata за фактически прошедшие дни (от start до сегодня).
        Это сумма, которую начислит close_investment в режиме «По расчёту».
        """
        if not self.deal:
            return 0
        pct = self.deal.expected_profit_pct or 0
        if pct == 0:
            return 0
        days = max(self.days_passed, 1)  # минимум 1 день, чтобы не было 0₽ при закрытии в день старта
        return round(self.amount * (pct / 100) * (days / 365), 2)


class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    investment_id = db.Column(db.Integer, db.ForeignKey('investments.id'), nullable=True)
    type = db.Column(db.String(30), nullable=False)  # investment, profit, withdrawal
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class BotFilter(db.Model):
    """
    Фильтры телеграм-уведомлений пользователя (одна строка на пользователя).

    Пустая строка/NULL в categories/risk_levels = «все». Событие по сделке
    сохраняется пользователю, только если сделка проходит фильтры И видима ему
    (Deal.user_can_see). Настраивается самим пользователем в боте (/filters).
    """
    __tablename__ = 'bot_filters'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, unique=True, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    categories = db.Column(db.String(200), nullable=True)   # CSV: realestate,auto,...
    risk_levels = db.Column(db.String(60), nullable=True)   # CSV: low,medium,high
    min_profit_pct = db.Column(db.Float, nullable=True)     # мин. доходность, NULL = любая
    max_entry = db.Column(db.Float, nullable=True)          # макс. порог входа (min_investment), NULL = любой
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', backref=db.backref('bot_filter', uselist=False))

    def categories_list(self):
        return [c for c in (self.categories or '').split(',') if c]

    def risk_list(self):
        return [r for r in (self.risk_levels or '').split(',') if r]


class BotNotification(db.Model):
    """
    Сохранённое событие для пользователя (outbox телеграм-бота).

    Веб-приложение при событии (новая сделка, смена статуса заявки, скорый старт)
    сохраняет строку; отдельный процесс tg_bot.py доставляет pending-строки в личку
    и помечает sent. События хранятся и после отправки — это лента /feed в боте.
    """
    __tablename__ = 'bot_notifications'
    __table_args__ = (
        # Дедуп deal-уровневых событий (deal_published/deal_starting): одно событие
        # типа на пользователя и сделку. Partial-индекс — персональные события по
        # инвестициям (investment_id NOT NULL) не ограничиваем: у пользователя может
        # быть несколько инвестиций в одну сделку.
        db.Index('uq_bot_notif_user_deal_event', 'user_id', 'deal_id', 'event_type',
                 unique=True,
                 sqlite_where=db.text('investment_id IS NULL'),
                 postgresql_where=db.text('investment_id IS NULL')),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    deal_id = db.Column(db.Integer, db.ForeignKey('deals.id', ondelete='CASCADE'),
                        nullable=True, index=True)
    investment_id = db.Column(db.Integer, db.ForeignKey('investments.id'), nullable=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    # deal_published | deal_starting_<N> | investment_confirmed | investment_rejected
    # | investment_closed | profit_paid
    message = db.Column(db.Text, nullable=False)
    # pending → sent | failed (перманентная ошибка/исчерпаны ретраи)
    #         | cancelled (сделка скрыта/на паузе к моменту доставки)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    sent_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('bot_notifications', lazy='dynamic'))
    deal = db.relationship('Deal')


class AuditLog(db.Model):
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    target_type = db.Column(db.String(50))  # user, deal, investment
    target_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
