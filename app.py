import os
import secrets
from datetime import datetime, timezone, timedelta, date
from functools import wraps
from dateutil.relativedelta import relativedelta

import bcrypt
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, send_from_directory, session, jsonify, abort)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename

from config import Config
from models import db, User, Deal, Investment, Transaction, AuditLog, deal_visibility
from forms import (LoginForm, CreateUserForm, EditUserForm, DealForm,
                   ExistingDealForm, ChangePasswordForm)
from telegram_notify import notify_investment, notify_investment_status, test_connection as tg_test_connection
from db_backup import (create_snapshot, list_snapshots, restore_snapshot,
                       delete_snapshot, get_db_info)


# ──────────────────── App Factory ────────────────────
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Extensions
    db.init_app(app)
    csrf = CSRFProtect(app)
    login_manager = LoginManager(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Требуется авторизация'
    login_manager.login_message_category = 'error'

    # Storage URI: memory:// для dev, redis://... для multi-worker prod (через ENV RATE_LIMIT_STORAGE_URI)
    rate_storage = app.config.get('RATE_LIMIT_STORAGE_URI', 'memory://')
    try:
        limiter = Limiter(
            app=app,
            key_func=get_remote_address,
            default_limits=["200 per minute"],
            storage_uri=rate_storage,
        )
    except Exception as e:
        # Если Redis недоступен — деградируем на in-memory с предупреждением, не падаем
        app.logger.warning(f'Limiter storage {rate_storage!r} недоступен ({e}), fallback на memory://')
        limiter = Limiter(
            app=app,
            key_func=get_remote_address,
            default_limits=["200 per minute"],
            storage_uri='memory://',
        )

    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}

    def allowed_file(filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

    # ──── Security headers ────
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        # CSP — узкий профиль: разрешаем только нужные CDN (Bootstrap, FontAwesome, Google Fonts)
        # 'unsafe-inline' для style — пока в шаблонах есть inline-style (планируется вынос в CSS-классы)
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com data:; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    # ──── Session rotation ────
    @app.before_request
    def rotate_session():
        session.permanent = True
        if 'created_at' not in session:
            session['created_at'] = datetime.now(timezone.utc).isoformat()

    # ──── Login Manager ────
    @login_manager.user_loader
    def load_user(user_id):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        return db.session.get(User, uid)

    # ──── Helpers ────
    def hash_password(password):
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def check_password(password, hashed):
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

    def admin_required(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            if not current_user.is_admin:
                abort(403)
            return f(*args, **kwargs)
        return decorated

    def log_action(action, target_type=None, target_id=None, details=None):
        entry = AuditLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip_address=request.remote_addr,
            user_agent=str(request.user_agent)[:300]
        )
        db.session.add(entry)
        db.session.commit()

    def safe_float(val, default=0):
        try:
            return float(str(val).replace(' ', '').replace(',', ''))
        except (ValueError, TypeError):
            return default

    def safe_int(val, default=0):
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def effective_start_date(deal, fallback):
        """Реальная дата начала инвестиции: фиксированный старт сделки (если задан и в будущем),
        иначе — fallback (обычно сегодня)."""
        if deal.date_start and deal.date_start > fallback:
            return deal.date_start
        return fallback

    def calc_investment_end_date(deal, start_date):
        """Calculate investment end date based on Deal's settings.
        Срок отсчитывается от effective_start_date (фикс. старт сделки приоритетнее).
        """
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

    # ──── Create upload dir ────
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # ──── DB init ────
    # Под gunicorn 3 воркера форкаются параллельно и каждый выполняет этот блок —
    # без try/except это даёт UNIQUE-конфликт по email/username при первом запуске.
    with app.app_context():
        db.create_all()
        try:
            admin = User.query.filter_by(username=app.config['ADMIN_USERNAME']).first()
            if not admin:
                admin = User(
                    username=app.config['ADMIN_USERNAME'],
                    email=app.config['ADMIN_EMAIL'],
                    password_hash=hash_password(app.config['ADMIN_PASSWORD']),
                    full_name='Администратор',
                    role='admin'
                )
                db.session.add(admin)
                db.session.commit()
        except Exception as e:
            # Другой воркер уже создал админа — безопасно игнорируем
            db.session.rollback()
            app.logger.info(f'Admin init: пропущено ({e.__class__.__name__})')

    # ════════════════════════════════════════════
    #  AUTH ROUTES
    # ════════════════════════════════════════════
    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit("10 per minute")
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('admin_dashboard') if current_user.is_admin else url_for('dashboard'))

        form = LoginForm()
        if form.validate_on_submit():
            user = User.query.filter_by(username=form.username.data.strip()).first()

            # Check lockout
            if user and user.locked_until and user.locked_until > datetime.now(timezone.utc):
                remaining = (user.locked_until - datetime.now(timezone.utc)).seconds // 60 + 1
                flash(f'Аккаунт заблокирован. Попробуйте через {remaining} мин.', 'error')
                log_action('login_blocked', 'user', user.id if user else None)
                return render_template('login.html', form=form)

            if user and user.is_active and check_password(form.password.data, user.password_hash):
                # Success
                user.failed_login_attempts = 0
                user.locked_until = None
                user.last_login = datetime.now(timezone.utc)
                db.session.commit()

                # Session fixation protection
                session.clear()
                login_user(user)
                session['_fresh_token'] = secrets.token_hex(16)

                log_action('login_success', 'user', user.id)
                flash(f'Добро пожаловать, {user.full_name}', 'success')
                return redirect(url_for('admin_dashboard') if user.is_admin else url_for('dashboard'))
            else:
                # Failed login
                if user:
                    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                    if user.failed_login_attempts >= app.config['MAX_LOGIN_ATTEMPTS']:
                        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=app.config['LOCKOUT_MINUTES'])
                        flash(f'Слишком много попыток. Аккаунт заблокирован на {app.config["LOCKOUT_MINUTES"]} мин.', 'error')
                    db.session.commit()
                    log_action('login_failed', 'user', user.id)
                else:
                    log_action('login_failed', details=f'username={form.username.data}')
                flash('Неверные учётные данные', 'error')

        return render_template('login.html', form=form)

    @app.route('/logout')
    @login_required
    def logout():
        log_action('logout', 'user', current_user.id)
        logout_user()
        session.clear()
        flash('Вы вышли из системы', 'info')
        return redirect(url_for('index'))

    @app.route('/change-password', methods=['GET', 'POST'])
    @login_required
    def change_password():
        form = ChangePasswordForm()
        if form.validate_on_submit():
            if not check_password(form.current_password.data, current_user.password_hash):
                flash('Неверный текущий пароль', 'error')
                return render_template('change_password.html', form=form)
            current_user.password_hash = hash_password(form.new_password.data)
            db.session.commit()
            log_action('password_changed', 'user', current_user.id)
            flash('Пароль успешно изменён', 'success')
            return redirect(url_for('dashboard'))
        return render_template('change_password.html', form=form)

    # ════════════════════════════════════════════
    #  PUBLIC ROUTES
    # ════════════════════════════════════════════
    @app.route('/')
    def index():
        user = current_user if current_user.is_authenticated else None

        # Stats - только активные сделки, ещё не стартовавшие (или без фикс. старта),
        # и только видимые пользователю
        today = date.today()
        hot_horizon = today + timedelta(days=Deal.HOT_THRESHOLD_DAYS)
        not_started_filter = db.or_(Deal.date_start.is_(None), Deal.date_start > today)
        not_expired_filter = db.or_(Deal.date_end.is_(None), Deal.date_end >= today)

        if user and user.is_admin:
            deals_q = Deal.query.filter(Deal.status == 'active', not_started_filter, not_expired_filter)
        elif user:
            deals_q = Deal.query.filter(
                Deal.status == 'active', not_started_filter, not_expired_filter,
                db.or_(Deal.visibility == 'all', Deal.visible_to.any(id=user.id))
            )
        else:
            deals_q = Deal.query.filter(
                Deal.status == 'active', not_started_filter, not_expired_filter,
                Deal.visibility == 'all'
            )

        total_ads = deals_q.count()
        total_invested = db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter_by(status='active').scalar()
        total_investors = User.query.filter_by(role='investor', is_active=True).count()
        avg_profit = deals_q.with_entities(db.func.coalesce(db.func.avg(Deal.expected_profit_pct), 0)).scalar()
        hot_count = deals_q.filter(
            Deal.date_start.isnot(None),
            Deal.date_start > today,
            Deal.date_start <= hot_horizon
        ).count()
        # Горящие — наверху, потом по дате создания
        is_hot_expr = db.case(
            (db.and_(Deal.date_start.isnot(None),
                     Deal.date_start > today,
                     Deal.date_start <= hot_horizon), 0),
            else_=1
        )
        recent_deals = deals_q.order_by(is_hot_expr, Deal.created_at.desc()).limit(6).all()

        return render_template('index.html',
                               total_ads=total_ads, total_invested=total_invested,
                               total_investors=total_investors, avg_profit=avg_profit,
                               hot_count=hot_count,
                               recent_deals=recent_deals)

    @app.route('/catalog')
    @login_required
    def catalog():
        cat = request.args.get('category', 'all')
        risk = request.args.get('risk', 'all')
        sort = request.args.get('sort', 'newest')
        tab = request.args.get('tab', 'active')  # active, closed, all
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')

        if current_user.is_admin:
            q = Deal.query
        else:
            q = Deal.query.filter(
                db.or_(
                    Deal.visibility == 'all',
                    Deal.visible_to.any(id=current_user.id)
                )
            )

        # Tab filter. Сделка «активна» в каталоге, пока ещё не стартовала
        # (либо у неё нет фиксированного старта). После date_start уходит в архив.
        today = date.today()
        hot_horizon = today + timedelta(days=Deal.HOT_THRESHOLD_DAYS)
        if tab == 'active':
            q = q.filter(
                Deal.status == 'active',
                db.or_(Deal.date_end.is_(None), Deal.date_end >= today),
                db.or_(Deal.date_start.is_(None), Deal.date_start > today)
            )
        elif tab == 'hot':
            q = q.filter(
                Deal.status == 'active',
                Deal.date_start.isnot(None),
                Deal.date_start > today,
                Deal.date_start <= hot_horizon
            )
        elif tab == 'closed':
            q = q.filter(
                db.or_(
                    Deal.status.in_(['closed', 'paused']),
                    db.and_(Deal.date_end.isnot(None), Deal.date_end < today),
                    db.and_(Deal.date_start.isnot(None), Deal.date_start <= today)
                )
            )
        # tab == 'all' — no status filter

        # Date period filter — filter by deal's created_at and date_end
        if date_from:
            try:
                df = datetime.strptime(date_from, '%Y-%m-%d').date()
                q = q.filter(db.or_(Deal.date_end.is_(None), Deal.date_end >= df))
            except ValueError:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, '%Y-%m-%d').date()
                q = q.filter(Deal.created_at <= datetime.combine(dt, datetime.max.time()))
            except ValueError:
                pass

        if cat != 'all':
            q = q.filter_by(category=cat)
        if risk != 'all':
            q = q.filter_by(risk_level=risk)

        if sort == 'profit_desc':
            q = q.order_by(Deal.expected_profit_pct.desc())
        elif sort == 'price_asc':
            q = q.order_by(Deal.price.asc())
        elif sort == 'price_desc':
            q = q.order_by(Deal.price.desc())
        elif sort == 'end_date':
            q = q.order_by(Deal.date_end.asc())
        elif sort == 'start_date':
            # NULL → в конец, затем по возрастанию даты старта
            q = q.order_by(Deal.date_start.is_(None).asc(), Deal.date_start.asc())
        elif tab == 'hot':
            q = q.order_by(Deal.date_start.asc())
        else:
            # Default: горящие сделки наверху, затем по дате создания
            is_hot_expr = db.case(
                (db.and_(Deal.date_start.isnot(None),
                         Deal.date_start > today,
                         Deal.date_start <= hot_horizon), 0),
                else_=1
            )
            q = q.order_by(is_hot_expr, Deal.created_at.desc())

        deals = q.all()

        # Counts for tabs
        if current_user.is_admin:
            base_q = Deal.query
        else:
            base_q = Deal.query.filter(
                db.or_(Deal.visibility == 'all', Deal.visible_to.any(id=current_user.id))
            )
        active_count = base_q.filter(
            Deal.status == 'active',
            db.or_(Deal.date_end.is_(None), Deal.date_end >= today),
            db.or_(Deal.date_start.is_(None), Deal.date_start > today)
        ).count()
        hot_count = base_q.filter(
            Deal.status == 'active',
            Deal.date_start.isnot(None),
            Deal.date_start > today,
            Deal.date_start <= hot_horizon
        ).count()
        closed_count = base_q.filter(
            db.or_(
                Deal.status.in_(['closed', 'paused']),
                db.and_(Deal.date_end.isnot(None), Deal.date_end < today),
                db.and_(Deal.date_start.isnot(None), Deal.date_start <= today)
            )
        ).count()
        all_count = base_q.count()

        return render_template('catalog.html', ads=deals, category=cat, risk=risk, sort=sort,
                               tab=tab, date_from=date_from, date_to=date_to,
                               active_count=active_count, hot_count=hot_count,
                               closed_count=closed_count, all_count=all_count)

    @app.route('/deal/<int:deal_id>')
    @login_required
    def deal_detail(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        if not deal.user_can_see(current_user):
            abort(403)
        investors_count = deal.investments.filter_by(status='active').with_entities(
            db.func.count(db.distinct(Investment.user_id))
        ).scalar()
        return render_template('ad_detail.html', ad=deal, investors_count=investors_count)

    # ════════════════════════════════════════════
    #  INVESTOR DASHBOARD
    # ════════════════════════════════════════════
    @app.route('/dashboard')
    @login_required
    def dashboard():
        user = current_user
        inv_tab = request.args.get('inv_tab', 'all')  # all, active, pending, closed
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')

        q = Investment.query.filter_by(user_id=user.id).join(Deal)

        # Tab filter
        if inv_tab == 'active':
            q = q.filter(Investment.status == 'active')
        elif inv_tab == 'pending':
            q = q.filter(Investment.status == 'pending')
        elif inv_tab == 'closed':
            q = q.filter(Investment.status.in_(['closed', 'rejected']))

        # Date filter
        if date_from:
            try:
                df = datetime.strptime(date_from, '%Y-%m-%d')
                q = q.filter(Investment.invested_at >= df)
            except ValueError:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                q = q.filter(Investment.invested_at < dt)
            except ValueError:
                pass

        investments = q.order_by(Investment.invested_at.desc()).all()

        # Stats queries — apply same date filter as investments
        def apply_date_filter(sq):
            if date_from:
                try:
                    df = datetime.strptime(date_from, '%Y-%m-%d')
                    sq = sq.filter(Investment.invested_at >= df)
                except ValueError:
                    pass
            if date_to:
                try:
                    dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                    sq = sq.filter(Investment.invested_at < dt)
                except ValueError:
                    pass
            return sq

        sq_invested = db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter_by(
            user_id=user.id, status='active')
        total_invested = apply_date_filter(sq_invested).scalar()

        sq_profit = db.session.query(db.func.coalesce(db.func.sum(Investment.expected_profit), 0)).filter_by(
            user_id=user.id, status='active')
        total_profit = apply_date_filter(sq_profit).scalar()

        sq_actual = db.session.query(db.func.coalesce(db.func.sum(Investment.actual_profit), 0)).filter(
            Investment.user_id == user.id, Investment.status.in_(['active', 'closed']))
        total_actual_profit = apply_date_filter(sq_actual).scalar()

        active_count = Investment.query.filter_by(user_id=user.id, status='active').count()
        pending_count = Investment.query.filter_by(user_id=user.id, status='pending').count()
        closed_count = Investment.query.filter_by(user_id=user.id, status='closed').count()
        rejected_count = Investment.query.filter_by(user_id=user.id, status='rejected').count()

        txns = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).limit(20).all()

        return render_template('dashboard.html',
                               user=user, investments=investments,
                               total_invested=total_invested, total_profit=total_profit,
                               total_actual_profit=total_actual_profit,
                               active_deals=active_count, pending_deals=pending_count,
                               closed_deals=closed_count, rejected_deals=rejected_count,
                               transactions=txns, inv_tab=inv_tab,
                               date_from=date_from, date_to=date_to)

    @app.route('/invest/<int:deal_id>', methods=['POST'])
    @login_required
    @limiter.limit("5 per minute")
    def invest(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        if not deal.user_can_see(current_user):
            abort(403)
        if deal.status != 'active':
            flash('Сделка не активна', 'error')
            return redirect(url_for('catalog'))
        if deal.is_expired:
            flash('Срок инвестирования по сделке истёк', 'error')
            return redirect(url_for('deal_detail', deal_id=deal.id))
        # Сделка с фиксированным стартом принимает заявки только ДО даты старта.
        # После — она уже в работе, новых инвесторов не подключаем.
        if deal.date_start and deal.date_start <= date.today():
            flash(
                f'Приём заявок завершён {deal.date_start.strftime("%d.%m.%Y")} '
                f'— сделка уже в работе.',
                'error'
            )
            return redirect(url_for('deal_detail', deal_id=deal.id))

        amount = safe_float(request.form.get('amount', 0))
        if amount <= 0:
            flash('Введите корректную сумму инвестиции', 'error')
            return redirect(url_for('deal_detail', deal_id=deal.id))
        if amount < deal.min_investment:
            flash(f'Минимальная сумма: {deal.min_investment:,.0f} руб.', 'error')
            return redirect(url_for('deal_detail', deal_id=deal.id))

        # Учитываем pending-заявки в остатке: иначе несколько инвесторов могут «перебронировать» пул
        pending_amount = db.session.query(
            db.func.coalesce(db.func.sum(Investment.amount), 0)
        ).filter(
            Investment.deal_id == deal.id,
            Investment.status == 'pending'
        ).scalar() or 0
        available = max(deal.remaining - pending_amount, 0)
        if amount > available:
            flash(f'Доступно с учётом ожидающих заявок: {available:,.0f} руб.', 'error')
            return redirect(url_for('deal_detail', deal_id=deal.id))

        today_date = date.today()
        inv_start_date = effective_start_date(deal, today_date)
        inv_end_date = calc_investment_end_date(deal, today_date)

        # For urgent_sale deals, no profit calculation
        if deal.is_urgent_sale:
            ep = 0
        elif inv_end_date:
            # Pro-rata profit: annual rate * (actual_days / 365)
            actual_days = max((inv_end_date - inv_start_date).days, 1)
            ep = amount * (deal.expected_profit_pct / 100) * (actual_days / 365)
        else:
            # Бессрочная сделка без срока — прибыль рассчитывается админом вручную при закрытии
            ep = 0

        inv = Investment(
            user_id=current_user.id,
            deal_id=deal.id,
            amount=amount,
            expected_profit=ep,
            status='pending',
            date_start=inv_start_date,
            date_end=inv_end_date
        )
        db.session.add(inv)
        # НЕ увеличиваем collected_amount до подтверждения админом
        db.session.flush()

        txn = Transaction(user_id=current_user.id, investment_id=inv.id,
                          type='investment', amount=amount,
                          description=f'Заявка на инвестицию в "{deal.title}" (ожидает подтверждения)')
        db.session.add(txn)
        db.session.commit()

        log_action('investment_requested', 'investment', inv.id,
                   f'amount={amount}, deal={deal.title}, status=pending')

        # Telegram notification to admin group
        notify_investment(
            app=app,
            investor_name=current_user.full_name,
            deal_title=deal.title,
            amount=amount,
            deal_category=deal.category,
            deal_profit_pct=deal.expected_profit_pct,
            deal_term=deal.term_display,
            deal_risk=deal.risk_level,
            investor_phone=current_user.phone
        )

        flash('Ваша заявка принята и находится в обработке. С вами скоро свяжется администратор.', 'info')
        return redirect(url_for('dashboard'))

    # ════════════════════════════════════════════
    #  ADMIN ROUTES
    # ════════════════════════════════════════════

    # ──── Dashboard ────
    @app.route('/admin')
    @admin_required
    def admin_dashboard():
        deal_tab = request.args.get('deal_tab', 'active')  # active, closed, all
        date_from = request.args.get('date_from', '')
        date_to = request.args.get('date_to', '')

        today = date.today()

        # Date filter helper for admin stats
        def admin_date_filter(sq):
            if date_from:
                try:
                    df = datetime.strptime(date_from, '%Y-%m-%d')
                    sq = sq.filter(Investment.invested_at >= df)
                except ValueError:
                    pass
            if date_to:
                try:
                    dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                    sq = sq.filter(Investment.invested_at < dt)
                except ValueError:
                    pass
            return sq

        # Stats (all respect date filter)
        total_ads = Deal.query.count()
        active_ads = Deal.query.filter(
            Deal.status == 'active',
            db.or_(Deal.date_end.is_(None), Deal.date_end >= today)
        ).count()
        closed_ads = Deal.query.filter(
            db.or_(
                Deal.status.in_(['closed', 'paused']),
                db.and_(Deal.date_end.isnot(None), Deal.date_end < today)
            )
        ).count()

        sq_inv = db.session.query(db.func.coalesce(db.func.sum(Investment.amount), 0)).filter(
            Investment.status == 'active')
        total_investments = admin_date_filter(sq_inv).scalar()

        sq_ap = db.session.query(db.func.coalesce(db.func.sum(Investment.actual_profit), 0)).filter(
            Investment.status.in_(['active', 'closed']))
        total_actual_profit = admin_date_filter(sq_ap).scalar()

        sq_ep = db.session.query(db.func.coalesce(db.func.sum(Investment.expected_profit), 0)).filter(
            Investment.status.in_(['active', 'closed']))
        total_expected_profit = admin_date_filter(sq_ep).scalar()

        total_users = User.query.filter_by(role='investor').count()
        pending_count = Investment.query.filter_by(status='pending').count()

        # period_invested is now same as total_investments since stats are date-filtered
        period_invested = total_investments

        # Deals query with tab filter
        dq = Deal.query
        if deal_tab == 'active':
            dq = dq.filter(
                Deal.status == 'active',
                db.or_(Deal.date_end.is_(None), Deal.date_end >= today)
            )
        elif deal_tab == 'closed':
            dq = dq.filter(
                db.or_(
                    Deal.status.in_(['closed', 'paused']),
                    db.and_(Deal.date_end.isnot(None), Deal.date_end < today)
                )
            )

        # Date filters for deals — only use date_end and created_at (no date_start on Deal)
        if date_from:
            try:
                df = datetime.strptime(date_from, '%Y-%m-%d').date()
                dq = dq.filter(db.or_(Deal.date_end.is_(None), Deal.date_end >= df))
            except ValueError:
                pass
        if date_to:
            try:
                dt_val = datetime.strptime(date_to, '%Y-%m-%d').date()
                dq = dq.filter(Deal.created_at <= datetime.combine(dt_val, datetime.max.time()))
            except ValueError:
                pass

        deals = dq.order_by(Deal.created_at.desc()).all()
        deals_data = []
        for d in deals:
            dd = d
            dd.assigned_users = d.visible_to.all() if d.visibility == 'selected' else []
            deals_data.append(dd)

        # Investments query with date filter
        inv_q = db.session.query(Investment, User, Deal).join(
            User, Investment.user_id == User.id
        ).join(
            Deal, Investment.deal_id == Deal.id
        )
        if date_from:
            try:
                df = datetime.strptime(date_from, '%Y-%m-%d')
                inv_q = inv_q.filter(Investment.invested_at >= df)
            except ValueError:
                pass
        if date_to:
            try:
                dt = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                inv_q = inv_q.filter(Investment.invested_at < dt)
            except ValueError:
                pass
        recent_investments = inv_q.order_by(Investment.invested_at.desc()).limit(30).all()

        investors = User.query.filter_by(role='investor').order_by(User.full_name).all()

        return render_template('admin_dashboard.html',
                               total_ads=total_ads, active_ads=active_ads,
                               closed_ads=closed_ads,
                               total_investments=total_investments, total_users=total_users,
                               total_actual_profit=total_actual_profit,
                               total_expected_profit=total_expected_profit,
                               pending_count=pending_count,
                               period_invested=period_invested,
                               ads=deals_data, recent_investments=recent_investments,
                               investors=investors,
                               deal_tab=deal_tab, date_from=date_from, date_to=date_to)

    # ──── User Management ────
    @app.route('/admin/users')
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.desc()).all()
        return render_template('admin_users.html', users=users)

    @app.route('/admin/users/create', methods=['GET', 'POST'])
    @admin_required
    def admin_create_user():
        form = CreateUserForm()
        if form.validate_on_submit():
            if User.query.filter((User.username == form.username.data) | (User.email == form.email.data)).first():
                flash('Пользователь с таким логином или email уже существует', 'error')
                return render_template('admin_create_user.html', form=form)

            user = User(
                username=form.username.data.strip(),
                email=form.email.data.strip().lower(),
                full_name=form.full_name.data.strip(),
                phone=form.phone.data.strip() if form.phone.data else None,
                password_hash=hash_password(form.password.data),
                role=form.role.data
            )
            db.session.add(user)
            db.session.commit()

            log_action('user_created', 'user', user.id, f'role={user.role}')
            flash(f'Пользователь {user.full_name} создан. Логин: {user.username}', 'success')
            return redirect(url_for('admin_users'))
        return render_template('admin_create_user.html', form=form)

    @app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
    @admin_required
    def admin_edit_user(user_id):
        user = User.query.get_or_404(user_id)
        form = EditUserForm(obj=user)
        if form.validate_on_submit():
            # Check email uniqueness
            existing = User.query.filter(User.email == form.email.data, User.id != user.id).first()
            if existing:
                flash('Email уже используется', 'error')
                return render_template('admin_edit_user.html', form=form, user=user)

            user.full_name = form.full_name.data.strip()
            user.email = form.email.data.strip().lower()
            user.phone = form.phone.data.strip() if form.phone.data else None
            user.is_active = form.is_active.data

            if form.new_password.data:
                user.password_hash = hash_password(form.new_password.data)

            db.session.commit()
            log_action('user_updated', 'user', user.id)
            flash('Пользователь обновлён', 'success')
            return redirect(url_for('admin_users'))
        return render_template('admin_edit_user.html', form=form, user=user)

    @app.route('/admin/users/<int:user_id>/toggle')
    @admin_required
    def admin_toggle_user(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('Нельзя деактивировать самого себя', 'error')
            return redirect(url_for('admin_users'))
        user.is_active = not user.is_active
        if not user.is_active:
            user.failed_login_attempts = 0
            user.locked_until = None
        db.session.commit()
        log_action('user_toggled', 'user', user.id, f'is_active={user.is_active}')
        flash(f'Пользователь {"активирован" if user.is_active else "деактивирован"}', 'success')
        return redirect(url_for('admin_users'))

    # ──── Deal Management ────
    @app.route('/admin/deals/create', methods=['GET', 'POST'])
    @admin_required
    def create_deal():
        form = DealForm()
        investors = User.query.filter_by(role='investor', is_active=True).order_by(User.full_name).all()

        if form.validate_on_submit():
            # Handle file uploads
            filenames = []
            if 'files' in request.files:
                for file in request.files.getlist('files'):
                    if file and file.filename and allowed_file(file.filename):
                        fn = secure_filename(file.filename)
                        fn = datetime.now().strftime('%Y%m%d_%H%M%S_') + secrets.token_hex(4) + '_' + fn
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                        filenames.append(fn)

            is_urgent = form.deal_type.data == 'urgent_sale'

            deal = Deal(
                deal_type=form.deal_type.data,
                title=form.title.data.strip(),
                description=form.description.data.strip(),
                category=form.category.data,
                subcategory=form.subcategory.data.strip() if form.subcategory.data else None,
                price=form.price.data,
                market_value=form.market_value.data if form.market_value.data else None,
                expected_profit_pct=0 if is_urgent else (form.expected_profit_pct.data or 0),
                date_start=form.date_start.data if form.date_start.data else None,
                date_end=form.date_end.data if form.date_end.data else None,
                investment_term_months=None if is_urgent else (form.investment_term_months.data if form.investment_term_months.data else None),
                investment_term_days=None if is_urgent else (form.investment_term_days.data if form.investment_term_days.data else None),
                min_investment=0 if is_urgent else (form.min_investment.data or 0),
                risk_level='low' if is_urgent else form.risk_level.data,
                total_pool=form.price.data if is_urgent else (form.total_pool.data or 0),
                contact_info=form.contact_info.data.strip() if form.contact_info.data else None,
                visibility=form.visibility.data,
                images=','.join(filenames) if filenames else None,
                property_type=form.property_type.data or None,
                area=safe_float(form.area.data) if form.area.data else None,
                rooms=safe_int(form.rooms.data) if form.rooms.data else None,
                location=form.location.data or None,
                floor=safe_int(form.floor.data) if form.floor.data else None,
                total_floors=safe_int(form.total_floors.data) if form.total_floors.data else None,
                car_brand=form.car_brand.data or None,
                car_model=form.car_model.data or None,
                car_year=safe_int(form.car_year.data) if form.car_year.data else None,
                car_power=safe_int(form.car_power.data) if form.car_power.data else None,
                car_mileage=safe_int(form.car_mileage.data) if form.car_mileage.data else None,
                car_transmission=form.car_transmission.data or None,
                car_fuel=form.car_fuel.data or None,
                created_by=current_user.id,
                status='active'
            )
            db.session.add(deal)
            db.session.flush()

            # Assign visibility
            selected_ids = request.form.getlist('selected_investors')
            if form.visibility.data == 'selected' and selected_ids:
                for sid in selected_ids:
                    u = User.query.get(int(sid))
                    if u:
                        deal.visible_to.append(u)

            db.session.commit()
            log_action('deal_created', 'deal', deal.id, f'title={deal.title}')
            flash('Предложение создано', 'success')
            return redirect(url_for('admin_dashboard'))

        return render_template('create_ad.html', form=form, investors=investors)

    @app.route('/admin/deals/<int:deal_id>/edit', methods=['GET', 'POST'])
    @admin_required
    def edit_deal(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        form = DealForm(obj=deal)
        investors = User.query.filter_by(role='investor', is_active=True).order_by(User.full_name).all()
        assigned_ids = [u.id for u in deal.visible_to.all()]

        # Current images for display
        current_images = deal.images.split(',') if deal.images else []

        if form.validate_on_submit():
            is_urgent = form.deal_type.data == 'urgent_sale'
            deal.deal_type = form.deal_type.data
            deal.title = form.title.data.strip()
            deal.description = form.description.data.strip()
            deal.category = form.category.data
            deal.subcategory = form.subcategory.data.strip() if form.subcategory.data else None
            deal.price = form.price.data
            deal.market_value = form.market_value.data if form.market_value.data else None
            deal.expected_profit_pct = 0 if is_urgent else (form.expected_profit_pct.data or 0)
            deal.date_start = form.date_start.data if form.date_start.data else None
            deal.date_end = form.date_end.data if form.date_end.data else None
            deal.investment_term_months = None if is_urgent else (form.investment_term_months.data if form.investment_term_months.data else None)
            deal.investment_term_days = None if is_urgent else (form.investment_term_days.data if form.investment_term_days.data else None)
            deal.min_investment = 0 if is_urgent else (form.min_investment.data or 0)
            deal.risk_level = 'low' if is_urgent else form.risk_level.data
            deal.total_pool = form.price.data if is_urgent else (form.total_pool.data or 0)
            deal.contact_info = form.contact_info.data.strip() if form.contact_info.data else None
            deal.visibility = form.visibility.data

            deal.property_type = form.property_type.data or None
            deal.area = safe_float(form.area.data) if form.area.data else None
            deal.rooms = safe_int(form.rooms.data) if form.rooms.data else None
            deal.location = form.location.data or None
            deal.floor = safe_int(form.floor.data) if form.floor.data else None
            deal.total_floors = safe_int(form.total_floors.data) if form.total_floors.data else None
            deal.car_brand = form.car_brand.data or None
            deal.car_model = form.car_model.data or None
            deal.car_year = safe_int(form.car_year.data) if form.car_year.data else None
            deal.car_power = safe_int(form.car_power.data) if form.car_power.data else None
            deal.car_mileage = safe_int(form.car_mileage.data) if form.car_mileage.data else None
            deal.car_transmission = form.car_transmission.data or None
            deal.car_fuel = form.car_fuel.data or None

            # Handle image deletion
            delete_imgs = request.form.getlist('delete_images')
            remaining_imgs = [img for img in current_images if img not in delete_imgs]
            # Delete physical files
            for img in delete_imgs:
                img_path = os.path.join(app.config['UPLOAD_FOLDER'], img)
                if os.path.exists(img_path):
                    try:
                        os.remove(img_path)
                    except OSError:
                        pass

            # Handle new file uploads
            new_filenames = []
            if 'files' in request.files:
                for file in request.files.getlist('files'):
                    if file and file.filename and allowed_file(file.filename):
                        fn = secure_filename(file.filename)
                        fn = datetime.now().strftime('%Y%m%d_%H%M%S_') + secrets.token_hex(4) + '_' + fn
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
                        new_filenames.append(fn)

            all_imgs = remaining_imgs + new_filenames
            deal.images = ','.join(all_imgs) if all_imgs else None

            # Update visibility assignments
            deal.visible_to = []
            selected_ids = request.form.getlist('selected_investors')
            if form.visibility.data == 'selected' and selected_ids:
                for sid in selected_ids:
                    u = User.query.get(int(sid))
                    if u:
                        deal.visible_to.append(u)

            db.session.commit()
            log_action('deal_updated', 'deal', deal.id)
            flash('Предложение обновлено', 'success')
            return redirect(url_for('admin_dashboard'))

        return render_template('edit_ad.html', form=form, ad=deal, investors=investors,
                               assigned_ids=assigned_ids, current_images=current_images)

    @app.route('/admin/deals/<int:deal_id>/visibility', methods=['GET', 'POST'])
    @admin_required
    def edit_visibility(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        investors = User.query.filter_by(role='investor', is_active=True).order_by(User.full_name).all()
        assigned_ids = [u.id for u in deal.visible_to.all()]

        if request.method == 'POST':
            vis = request.form.get('visibility', 'selected')
            deal.visibility = vis
            deal.visible_to = []
            if vis == 'selected':
                for sid in request.form.getlist('selected_investors'):
                    u = User.query.get(int(sid))
                    if u:
                        deal.visible_to.append(u)
            db.session.commit()
            log_action('visibility_updated', 'deal', deal.id, f'visibility={vis}')
            flash('Видимость обновлена', 'success')
            return redirect(url_for('admin_dashboard'))

        return render_template('edit_visibility.html', ad=deal, investors=investors, assigned_ids=assigned_ids)

    @app.route('/admin/deals/<int:deal_id>/toggle')
    @admin_required
    def toggle_deal(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        deal.status = 'paused' if deal.status == 'active' else 'active'
        db.session.commit()
        log_action('deal_toggled', 'deal', deal.id, f'status={deal.status}')
        flash('Статус обновлён', 'success')
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/deals/<int:deal_id>/delete', methods=['POST'])
    @admin_required
    def delete_deal(deal_id):
        deal = Deal.query.get_or_404(deal_id)
        if deal.investments.count() > 0:
            flash('Нельзя удалить сделку с инвестициями. Поставьте на паузу.', 'error')
            return redirect(url_for('admin_dashboard'))
        title = deal.title
        db.session.delete(deal)
        db.session.commit()
        log_action('deal_deleted', 'deal', deal_id, f'title={title}')
        flash('Предложение удалено', 'warning')
        return redirect(url_for('admin_dashboard'))

    # ──── Existing Deals (admin records past investments) ────
    @app.route('/admin/investments/create', methods=['GET', 'POST'])
    @admin_required
    def create_existing_investment():
        form = ExistingDealForm()
        form.deal_id.choices = [(d.id, d.title) for d in Deal.query.order_by(Deal.title).all()]
        form.user_id.choices = [(u.id, f'{u.full_name} ({u.username})') for u in
                                User.query.filter_by(role='investor', is_active=True).order_by(User.full_name).all()]

        if form.validate_on_submit():
            deal = Deal.query.get(form.deal_id.data)
            user = User.query.get(form.user_id.data)
            if not deal or not user:
                flash('Сделка или инвестор не найдены', 'error')
                return render_template('admin_create_investment.html', form=form)

            # Set investment dates
            inv_start = form.inv_date_start.data if form.inv_date_start.data else date.today()
            inv_end = form.inv_date_end.data if form.inv_date_end.data else calc_investment_end_date(deal, inv_start)

            # Если админ задал прибыль вручную — фиксируем флагом manual, чтобы pro-rata не перезатёрла
            manual_ep = bool(form.expected_profit.data and form.expected_profit.data > 0)
            ep = form.expected_profit.data
            if not manual_ep:
                if inv_end:
                    actual_days = max((inv_end - inv_start).days, 1)
                    ep = form.amount.data * (deal.expected_profit_pct / 100) * (actual_days / 365)
                else:
                    ep = 0

            inv = Investment(
                user_id=user.id,
                deal_id=deal.id,
                amount=form.amount.data,
                expected_profit=ep,
                expected_profit_manual=manual_ep,
                actual_profit=form.actual_profit.data if form.actual_profit.data else 0,
                status=form.status.data,
                notes=form.notes.data.strip() if form.notes.data else None,
                date_start=inv_start,
                date_end=inv_end
            )
            db.session.add(inv)
            deal.collected_amount += form.amount.data
            db.session.flush()

            txn = Transaction(
                user_id=user.id,
                investment_id=inv.id,
                type='investment',
                amount=form.amount.data,
                description=f'Существующая сделка: "{deal.title}"'
            )
            db.session.add(txn)

            # Auto-assign deal visibility
            if not deal.visible_to.filter_by(id=user.id).first():
                deal.visible_to.append(user)

            db.session.commit()
            log_action('existing_investment_created', 'investment', inv.id,
                       f'user={user.username}, deal={deal.title}, amount={form.amount.data}')
            flash(f'Сделка для {user.full_name} зарегистрирована', 'success')
            return redirect(url_for('admin_dashboard'))

        return render_template('admin_create_investment.html', form=form)

    # ──── Investment Confirmation ────
    @app.route('/admin/investments/pending')
    @admin_required
    def admin_pending_investments():
        pending = db.session.query(Investment, User, Deal).join(
            User, Investment.user_id == User.id
        ).join(
            Deal, Investment.deal_id == Deal.id
        ).filter(
            Investment.status == 'pending'
        ).order_by(Investment.invested_at.desc()).all()
        return render_template('admin_pending.html', pending=pending)

    @app.route('/admin/investments/<int:inv_id>/confirm', methods=['POST'])
    @admin_required
    def confirm_investment(inv_id):
        inv = Investment.query.get_or_404(inv_id)
        if inv.status != 'pending':
            flash('Эта заявка уже обработана', 'error')
            return redirect(url_for('admin_pending_investments'))

        deal = Deal.query.get(inv.deal_id)
        investor = User.query.get(inv.user_id)

        inv.status = 'active'
        deal.collected_amount += inv.amount

        # Set investment dates at confirmation time
        confirm_date = date.today()
        inv.date_start = effective_start_date(deal, confirm_date)
        inv.date_end = calc_investment_end_date(deal, confirm_date)

        # Recalculate expected profit pro-rata by days (если не задана вручную)
        if not inv.expected_profit_manual:
            if inv.date_end:
                actual_days = max((inv.date_end - inv.date_start).days, 1)
                inv.expected_profit = inv.amount * (deal.expected_profit_pct / 100) * (actual_days / 365)
            else:
                inv.expected_profit = 0

        # Update the transaction description
        txn = Transaction.query.filter_by(investment_id=inv.id, type='investment').first()
        if txn:
            txn.description = f'Инвестиция в "{deal.title}" (подтверждена)'

        db.session.commit()

        log_action('investment_confirmed', 'investment', inv.id,
                   f'investor={investor.username}, deal={deal.title}, amount={inv.amount}')

        # Telegram notification about confirmation
        notify_investment_status(
            app=app,
            investor_name=investor.full_name,
            deal_title=deal.title,
            amount=inv.amount,
            status='confirmed',
            admin_name=current_user.full_name
        )

        flash(f'Инвестиция {inv.amount:,.0f} руб. от {investor.full_name} подтверждена', 'success')
        return redirect(url_for('admin_pending_investments'))

    @app.route('/admin/investments/<int:inv_id>/reject', methods=['POST'])
    @admin_required
    def reject_investment(inv_id):
        inv = Investment.query.get_or_404(inv_id)
        if inv.status != 'pending':
            flash('Эта заявка уже обработана', 'error')
            return redirect(url_for('admin_pending_investments'))

        deal = Deal.query.get(inv.deal_id)
        investor = User.query.get(inv.user_id)
        reason = request.form.get('reason', '').strip()

        inv.status = 'rejected'
        inv.notes = f'Отклонено: {reason}' if reason else 'Отклонено администратором'

        # Update the transaction description
        txn = Transaction.query.filter_by(investment_id=inv.id, type='investment').first()
        if txn:
            txn.description = f'Заявка на инвестицию в "{deal.title}" (отклонена)'

        db.session.commit()

        log_action('investment_rejected', 'investment', inv.id,
                   f'investor={investor.username}, deal={deal.title}, amount={inv.amount}, reason={reason}')

        # Telegram notification about rejection
        notify_investment_status(
            app=app,
            investor_name=investor.full_name,
            deal_title=deal.title,
            amount=inv.amount,
            status='rejected',
            admin_name=current_user.full_name
        )

        flash(f'Заявка от {investor.full_name} отклонена', 'warning')
        return redirect(url_for('admin_pending_investments'))

    # ──── Update actual profit ────
    @app.route('/admin/investments/<int:inv_id>/profit', methods=['POST'])
    @admin_required
    def update_actual_profit(inv_id):
        inv = Investment.query.get_or_404(inv_id)
        new_profit = safe_float(request.form.get('actual_profit', 0))
        old_profit = inv.actual_profit or 0
        inv.actual_profit = new_profit
        db.session.commit()

        # Create transaction record for profit change
        if new_profit > old_profit:
            diff = new_profit - old_profit
            txn = Transaction(
                user_id=inv.user_id,
                investment_id=inv.id,
                type='profit',
                amount=diff,
                description=f'Прибыль по сделке "{inv.deal.title}" (+{diff:,.0f} руб.)'
            )
            db.session.add(txn)
            db.session.commit()

        log_action('profit_updated', 'investment', inv.id,
                   f'actual_profit={new_profit}, old={old_profit}')
        flash(f'Полученная прибыль обновлена: {new_profit:,.0f} руб.', 'success')
        return redirect(request.referrer or url_for('admin_dashboard'))

    # ──── Close investment (early or on schedule) ────
    @app.route('/admin/investments/<int:inv_id>/close', methods=['POST'])
    @admin_required
    def close_investment(inv_id):
        inv = Investment.query.get_or_404(inv_id)
        if inv.status not in ('active', 'pending'):
            flash('Можно закрыть только активную или ожидающую инвестицию', 'warning')
            return redirect(request.referrer or url_for('admin_dashboard'))

        actual_profit_str = request.form.get('actual_profit', '').strip()
        # Если прибыль не указана явно — берём текущую actual_profit или pro-rata от ставки
        if actual_profit_str:
            actual_profit = safe_float(actual_profit_str)
        else:
            actual_profit = inv.actual_profit or 0

        if actual_profit < 0:
            flash('Прибыль не может быть отрицательной', 'danger')
            return redirect(request.referrer or url_for('admin_dashboard'))

        old_profit = inv.actual_profit or 0
        old_status = inv.status
        now_dt = datetime.now(timezone.utc)
        today = date.today()

        inv.actual_profit = actual_profit
        inv.status = 'closed'
        inv.closed_at = now_dt
        # Фиксируем фактическую дату окончания — именно сегодня, если закрыли раньше срока
        inv.date_end = today

        # Транзакция прибыли (доначисление разницы, если явно подвинули)
        if actual_profit > old_profit:
            diff = actual_profit - old_profit
            db.session.add(Transaction(
                user_id=inv.user_id,
                investment_id=inv.id,
                type='profit',
                amount=diff,
                description=f'Прибыль по сделке "{inv.deal.title}" (закрытие, +{diff:,.0f} руб.)'
            ))
        # Транзакция возврата тела
        db.session.add(Transaction(
            user_id=inv.user_id,
            investment_id=inv.id,
            type='return',
            amount=inv.amount,
            description=f'Возврат тела инвестиции по сделке "{inv.deal.title}"'
        ))

        db.session.commit()
        log_action('investment_closed', 'investment', inv.id,
                   f'old_status={old_status}, actual_profit={actual_profit}, '
                   f'closed_at={today.isoformat()}')

        # TG-уведомление
        try:
            notify_investment_status(
                app,
                investor_name=inv.user.full_name or inv.user.username,
                deal_title=inv.deal.title,
                amount=inv.amount,
                status='closed',
                admin_name=current_user.full_name,
            )
        except Exception:
            pass

        flash(f'Инвестиция закрыта. Прибыль: {actual_profit:,.0f} ₽', 'success')
        return redirect(request.referrer or url_for('admin_dashboard'))

    # ──── Audit Log ────
    @app.route('/admin/audit')
    @admin_required
    def admin_audit():
        page = request.args.get('page', 1, type=int)
        logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50)
        return render_template('admin_audit.html', logs=logs)

    # ──── Database Backups ────
    @app.route('/admin/backups')
    @admin_required
    def admin_backups():
        snapshots = list_snapshots()
        db_info = get_db_info()
        # Test TG connection
        tg_ok, tg_info = tg_test_connection(
            app.config.get('TELEGRAM_BOT_TOKEN', ''),
            app.config.get('TELEGRAM_PROXY', '')
        )
        return render_template('admin_backups.html',
                               snapshots=snapshots, db_info=db_info,
                               tg_ok=tg_ok, tg_info=tg_info)

    @app.route('/admin/backups/create', methods=['POST'])
    @admin_required
    def admin_create_backup():
        try:
            label = request.form.get('label', '').strip()
            fn, path, size = create_snapshot(label=label)
            log_action('backup_created', 'database', details=f'file={fn}')
            flash(f'Снапшот создан: {fn}', 'success')
        except Exception as e:
            flash(f'Ошибка при создании снапшота: {e}', 'error')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/<filename>/restore', methods=['POST'])
    @admin_required
    def admin_restore_backup(filename):
        try:
            pre, restored = restore_snapshot(filename)
            log_action('backup_restored', 'database', details=f'restored={restored}, pre_backup={pre}')
            flash(f'БД восстановлена из {restored}. Предыдущее состояние: {pre}', 'success')
        except Exception as e:
            flash(f'Ошибка при восстановлении: {e}', 'error')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/<filename>/delete', methods=['POST'])
    @admin_required
    def admin_delete_backup(filename):
        if delete_snapshot(filename):
            log_action('backup_deleted', 'database', details=f'file={filename}')
            flash(f'Снапшот {filename} удалён', 'warning')
        else:
            flash('Снапшот не найден', 'error')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/<filename>/download')
    @admin_required
    def admin_download_backup(filename):
        from flask import send_from_directory as sfd
        backup_dir = os.path.join(app.root_path, 'backups')
        fn = secure_filename(filename)
        if fn != filename:
            abort(404)
        return sfd(backup_dir, fn, as_attachment=True)

    @app.route('/admin/telegram/test', methods=['POST'])
    @admin_required
    def admin_test_telegram():
        from telegram_notify import _send_telegram_message
        bot_token = app.config.get('TELEGRAM_BOT_TOKEN', '')
        chat_id = app.config.get('TELEGRAM_CHAT_ID', '')
        proxy_url = app.config.get('TELEGRAM_PROXY', '')

        text = (
            '🔧 <b>Тест подключения · Группа Титан</b>\n'
            '━━━━━━━━━━━━━━━━━━━━━\n'
            f'👤 Отправил: {current_user.full_name}\n'
            f'🕐 {datetime.now().strftime("%d.%m.%Y %H:%M:%S")}\n'
            '✅ Telegram-уведомления работают!'
        )

        ok = _send_telegram_message(bot_token, chat_id, text, proxy_url=proxy_url)
        if ok:
            flash('Тестовое сообщение отправлено в Telegram', 'success')
        else:
            flash('Ошибка отправки. Проверьте токен, chat_id и прокси (TELEGRAM_PROXY).', 'error')

        return redirect(url_for('admin_backups'))

    # ════════════════════════════════════════════
    #  API
    # ════════════════════════════════════════════
    @app.route('/api/calculate', methods=['POST'])
    @csrf.exempt
    @limiter.limit("30 per minute")
    def calculate_profit():
        data = request.get_json()
        if not data:
            return jsonify(error='Invalid request'), 400
        a = safe_float(data.get('amount', 0))
        pp = safe_float(data.get('profit_pct', 0))
        t_days = safe_int(data.get('term_days', 0))
        t_months = safe_int(data.get('term', 0))
        # Determine total days for pro-rata calc
        if t_days > 0:
            days = t_days
        elif t_months > 0:
            days = t_months * 30
        else:
            days = 365
        # Pro-rata: annual rate * days / 365
        tp = a * (pp / 100) * (days / 365)
        months = max(round(days / 30), 1)
        mp = tp / months if months > 0 else 0
        dp = tp / days if days > 0 else 0
        return jsonify(total_profit=round(tp, 2), monthly_profit=round(mp, 2),
                       daily_profit=round(dp, 2),
                       total_return=round(a + tp, 2), roi=round(pp, 2))

    @app.route('/uploads/<filename>')
    @login_required
    def uploaded_file(filename):
        # Sanitize filename to prevent directory traversal
        fn = secure_filename(filename)
        if fn != filename or '/' in filename or '\\' in filename:
            abort(404)

        # Админ имеет доступ ко всему
        if not current_user.is_admin:
            # Файл должен принадлежать сделке, видимой пользователю
            owning_deal = Deal.query.filter(Deal.images.like(f'%{fn}%')).first()
            if not owning_deal or not owning_deal.user_can_see(current_user):
                abort(403)
            # Дополнительная точная проверка (LIKE может ложно срабатывать на подстроках)
            if fn not in (owning_deal.images or '').split(','):
                abort(403)

        return send_from_directory(app.config['UPLOAD_FOLDER'], fn)

    # ──── Error handlers ────
    @app.errorhandler(403)
    def forbidden(e):
        return render_template('error.html', code=403, message='Доступ запрещён'), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template('error.html', code=404, message='Страница не найдена'), 404

    @app.errorhandler(429)
    def too_many_requests(e):
        return render_template('error.html', code=429, message='Слишком много запросов. Попробуйте позже.'), 429

    @app.errorhandler(500)
    def server_error(e):
        return render_template('error.html', code=500, message='Внутренняя ошибка сервера'), 500

    return app


app = create_app()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
