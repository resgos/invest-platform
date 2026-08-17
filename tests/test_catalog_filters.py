"""
Тесты каталога сделок (БАГ 5, 6, 11a):
  - авто-фильтры терпимы к пробелам-разрядам/NBSP ("100 000") и отличают
    пустое поле от введённого 0 (parse_int_filter, не safe_int);
  - выпадашка марок учитывает видимость сделок (Deal.visible_to_user_criteria);
  - таб-ссылки каталога сохраняют все активные фильтры, а не только
    category/risk/sort.

Поднимают реальное Flask-приложение (create_app() из app.py, включая роут
/catalog) на in-memory SQLite. Config.SQLALCHEMY_DATABASE_URI патчится
на 'sqlite://' ДО импорта app.py — иначе create_app() при импорте отработал
бы на боевом instance/invest.db (app.py делает `app = create_app()` на
уровне модуля). Стиль/фикстуры — по образцу tests/test_bot_logic.py.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ВАЖНО: патчим ДО первого `import app` в процессе (unittest discover грузит
# все test_*.py в одном процессе; на момент этого файла ни один другой тест
# app.py/config.py не импортирует — но патчим атрибут класса, а не только
# env var, чтобы не зависеть от порядка запуска файлов).
import config as _config_module
_config_module.Config.SQLALCHEMY_DATABASE_URI = 'sqlite://'
_config_module.Config.WTF_CSRF_ENABLED = False

from models import db, User, Deal
from app import app as flask_app


def mk_user(username, role='investor', active=True):
    u = User(username=username, email=f'{username}@t.ru', password_hash='x',
             full_name=username.title(), role=role, is_active=active)
    db.session.add(u)
    db.session.commit()
    return u


def mk_deal(title='Сделка', category='auto', visibility='all', **kwargs):
    defaults = dict(description='desc', risk_level='medium',
                     expected_profit_pct=15.0, min_investment=0,
                     price=1_000_000, total_pool=1_000_000, status='active')
    defaults.update(kwargs)
    d = Deal(title=title, category=category, visibility=visibility, **defaults)
    db.session.add(d)
    db.session.commit()
    return d


def login_as(client, user):
    """Flask-Login session hack: минуем форму /login (пароль/CSRF не нужны)."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.app = flask_app
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


# ──────────────────── БАГ 5: пробельные разряды + car_mileage_to=0 ────────────────────

class MileageFilterTests(CatalogTestCase):
    def test_mileage_with_spaces_filters(self):
        u = mk_user('inv_mileage1')
        login_as(self.client, u)
        low = mk_deal(title='Ниссан низкий пробег', car_brand='Nissan', car_mileage=50_000)
        high = mk_deal(title='Ниссан большой пробег', car_brand='Nissan', car_mileage=250_000)

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'all', 'car_mileage_to': '100 000',
        })
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(low.title, html)
        self.assertNotIn(high.title, html)

    def test_mileage_with_nbsp_filters(self):
        u = mk_user('inv_mileage2')
        login_as(self.client, u)
        low = mk_deal(title='Тойота низкий пробег', car_brand='Toyota', car_mileage=10_000)
        high = mk_deal(title='Тойота высокий пробег', car_brand='Toyota', car_mileage=300_000)

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'all', 'car_mileage_to': '100\xa0000',
        })
        html = resp.get_data(as_text=True)
        self.assertIn(low.title, html)
        self.assertNotIn(high.title, html)

    def test_mileage_zero_finds_only_zero_mileage(self):
        u = mk_user('inv_mileage3')
        login_as(self.client, u)
        new_car = mk_deal(title='Новое авто без пробега', car_brand='Kia', car_mileage=0)
        used_car = mk_deal(title='Авто с пробегом', car_brand='Kia', car_mileage=5_000)

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'all', 'car_mileage_to': '0',
        })
        html = resp.get_data(as_text=True)
        self.assertIn(new_car.title, html)
        self.assertNotIn(used_car.title, html)

    def test_mileage_negative_ignored_not_500(self):
        # Отрицательное значение — фильтр молча не применяется (как и раньше),
        # роут не должен падать
        u = mk_user('inv_mileage4')
        login_as(self.client, u)
        a = mk_deal(title='Авто А пробег', car_brand='Lada', car_mileage=1_000)
        b = mk_deal(title='Авто Б пробег', car_brand='Lada', car_mileage=200_000)

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'all', 'car_mileage_to': '-500',
        })
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(a.title, html)
        self.assertIn(b.title, html)

    def test_mileage_empty_means_no_filter(self):
        u = mk_user('inv_mileage5')
        login_as(self.client, u)
        a = mk_deal(title='Компакт пробег', car_brand='Hyundai', car_mileage=1_000)
        b = mk_deal(title='Кроссовер пробег', car_brand='Hyundai', car_mileage=200_000)

        resp = self.client.get('/catalog', query_string={'category': 'auto', 'tab': 'all'})
        html = resp.get_data(as_text=True)
        self.assertIn(a.title, html)
        self.assertIn(b.title, html)


# ──────────────────── БАГ 6: видимость марок в выпадашке ────────────────────

class BrandDropdownVisibilityTests(CatalogTestCase):
    def test_hidden_deal_brand_not_in_dropdown(self):
        outsider = mk_user('outsider1')
        insider = mk_user('insider1')
        mk_deal(title='Открытая сделка', category='auto',
                visibility='all', car_brand='Toyota')
        hidden = mk_deal(title='Закрытая сделка', category='auto',
                          visibility='selected', car_brand='SecretBrand')
        hidden.visible_to.append(insider)
        db.session.commit()

        login_as(self.client, outsider)
        resp = self.client.get('/catalog', query_string={'category': 'auto', 'tab': 'all'})
        html = resp.get_data(as_text=True)

        # Марка видимой сделки в select есть, марка скрытой — нет ни в каком виде
        self.assertIn('>Toyota<', html)
        self.assertNotIn('SecretBrand', html)

    def test_user_with_access_sees_the_brand(self):
        insider = mk_user('insider2')
        hidden = mk_deal(title='Адресная сделка', category='auto',
                          visibility='selected', car_brand='OnlyForInsider')
        hidden.visible_to.append(insider)
        db.session.commit()

        login_as(self.client, insider)
        resp = self.client.get('/catalog', query_string={'category': 'auto', 'tab': 'all'})
        html = resp.get_data(as_text=True)
        self.assertIn('OnlyForInsider', html)

    def test_admin_sees_all_brands(self):
        admin = mk_user('admin1', role='admin')
        mk_deal(title='Скрытая от инвесторов', category='auto',
                visibility='selected', car_brand='AdminOnlyVisible')

        login_as(self.client, admin)
        resp = self.client.get('/catalog', query_string={'category': 'auto', 'tab': 'all'})
        html = resp.get_data(as_text=True)
        self.assertIn('AdminOnlyVisible', html)


# ──────────────────── БАГ 11a: таб-ссылки сохраняют фильтры ────────────────────

class TabLinksPreserveFiltersTests(CatalogTestCase):
    @staticmethod
    def _catalog_links(html):
        return re.findall(r'href="(/catalog\?[^"]*)"', html)

    def test_tab_links_preserve_price_and_brand(self):
        u = mk_user('inv_tabs1')
        login_as(self.client, u)
        mk_deal(title='БМВ сделка', category='auto', car_brand='BMW')

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'active',
            'price_from': '500000', 'car_brand': 'BMW',
        })
        html = resp.get_data(as_text=True)
        links = self._catalog_links(html)
        self.assertGreaterEqual(len(links), 4)

        for tab_marker in ('tab=active', 'tab=hot', 'tab=closed', 'tab=all'):
            matching = [l for l in links if tab_marker in l]
            self.assertTrue(matching, f'ссылка с {tab_marker} не найдена среди {links}')
            for link in matching:
                self.assertIn('price_from=500000', link)
                self.assertIn('car_brand=BMW', link)

    def test_tab_links_preserve_zero_mileage_and_dates(self):
        # Регрессия на "0 or None": car_mileage_to='0' — непустая строка,
        # должна сохраниться в ссылке, а не потеряться как "пусто"
        u = mk_user('inv_tabs2')
        login_as(self.client, u)

        resp = self.client.get('/catalog', query_string={
            'category': 'auto', 'tab': 'all',
            'car_mileage_to': '0',
            'date_from': '2026-01-01', 'date_to': '2026-12-31',
        })
        html = resp.get_data(as_text=True)
        links = self._catalog_links(html)
        self.assertGreaterEqual(len(links), 4)
        for link in links:
            self.assertIn('car_mileage_to=0', link)
            self.assertIn('date_from=2026-01-01', link)
            self.assertIn('date_to=2026-12-31', link)


# ──────────────────── Чистая функция parse_int_filter (копия из app.py) ────────────────────
# safe_int/parse_int_filter — closures внутри create_app(), их нельзя
# импортировать напрямую (тот же приём, что и в test_safe_float.py). Копия
# ниже тестирует алгоритм разбора изолированно; HTTP-тесты выше проверяют,
# что роут /catalog действительно им пользуется.

def _safe_float(val, default=0):
    try:
        s = str(val).strip().replace(' ', '').replace('\xa0', '')
        if '.' in s:
            s = s.replace(',', '')
        elif s.count(',') == 1:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
        return float(s)
    except (ValueError, TypeError):
        return default


def _parse_int_filter(raw):
    s = str(raw or '').strip()
    if not s:
        return None
    val = _safe_float(s, default=None)
    return int(val) if val is not None else None


class ParseIntFilterTests(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(_parse_int_filter('100000'), 100000)

    def test_spaces_thousands(self):
        self.assertEqual(_parse_int_filter('100 000'), 100000)

    def test_nbsp_thousands(self):
        self.assertEqual(_parse_int_filter('100\xa0000'), 100000)

    def test_empty_is_none(self):
        self.assertIsNone(_parse_int_filter(''))
        self.assertIsNone(_parse_int_filter('   '))
        self.assertIsNone(_parse_int_filter(None))

    def test_zero_is_not_none(self):
        # Ключевое отличие от safe_int(default=0): "0" — валидное значение,
        # неотличимое там от "поле не заполнено"; здесь — отличимо
        self.assertEqual(_parse_int_filter('0'), 0)
        self.assertIsNotNone(_parse_int_filter('0'))

    def test_garbage_is_none(self):
        self.assertIsNone(_parse_int_filter('abc'))

    def test_negative_parsed_caller_decides(self):
        # Парсер не отбрасывает отрицательные сам — guard в роуте (>= 0 / > 0)
        # решает, применять ли фильтр
        self.assertEqual(_parse_int_filter('-5'), -5)


if __name__ == '__main__':
    unittest.main()
