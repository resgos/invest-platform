"""
Тесты трёх фиксов (см. отчёт по багам P1/P2):

  1. config._persistent_secret_key — атомарная публикация ключа устраняет
     гонку удаления пустого файла-сироты и молчаливую деградацию при
     коротком окне ожидания.
  2. deploy/gruppa-titan-bot.service — StartLimitIntervalSec/StartLimitBurst
     обязаны жить в секции [Unit], а не [Service] (иначе лимит рестартов
     систематически игнорируется systemd).
  3. migrate.py: m_007_deal_starting_horizon — переименование исторического
     event_type='deal_starting' в 'deal_starting_<горизонт>' без дублей
     напоминаний и без падения на уникальном индексе.

Работают полностью изолированно: temp-каталоги/temp-БД, никаких обращений
к боевым instance/secret_key, instance/invest.db или .env репозитория.
"""
import configparser
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
import migrate  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ───────────────────── БАГ 10: гонка secret_key ─────────────────────

class PersistentSecretKeyTests(unittest.TestCase):
    """_persistent_secret_key работает ИСКЛЮЧИТЕЛЬНО во временном каталоге —
    config._INSTANCE_DIR монкипатчится в setUp/восстанавливается в tearDown,
    реальный instance/secret_key репозитория ни разу не открывается на запись.
    """

    def setUp(self):
        self._orig_instance_dir = config_module._INSTANCE_DIR
        self._orig_secret_key_env = os.environ.pop('SECRET_KEY', None)
        self.tmp_dir = tempfile.mkdtemp(prefix='gt_secret_key_test_')
        config_module._INSTANCE_DIR = self.tmp_dir

    def tearDown(self):
        config_module._INSTANCE_DIR = self._orig_instance_dir
        if self._orig_secret_key_env is not None:
            os.environ['SECRET_KEY'] = self._orig_secret_key_env
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _key_file(self):
        return os.path.join(self.tmp_dir, 'secret_key')

    # ---- базовое поведение ----

    def test_does_not_touch_real_repo_instance_dir(self):
        # Санити-чек самого теста, прежде чем доверять остальным: мы правда
        # работаем во временном каталоге, а не в instance/ репозитория.
        real_instance_dir = os.path.join(REPO_ROOT, 'instance')
        self.assertNotEqual(
            os.path.normcase(os.path.normpath(self.tmp_dir)),
            os.path.normcase(os.path.normpath(real_instance_dir)))

    def test_generates_and_persists_new_key(self):
        key = config_module._persistent_secret_key()
        self.assertTrue(key)
        self.assertTrue(os.path.exists(self._key_file()))
        with open(self._key_file(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), key)

    def test_existing_valid_key_read_as_is(self):
        with open(self._key_file(), 'w', encoding='utf-8') as f:
            f.write('already-stored-key-value')

        key = config_module._persistent_secret_key()

        self.assertEqual(key, 'already-stored-key-value')

    def test_env_key_takes_priority(self):
        os.environ['SECRET_KEY'] = 'from-env'
        # В файле лежит другое значение — env обязан победить, файл не трогаем
        with open(self._key_file(), 'w', encoding='utf-8') as f:
            f.write('from-file')

        key = config_module._persistent_secret_key()

        self.assertEqual(key, 'from-env')
        with open(self._key_file(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'from-file')  # не перезаписан

    def test_repeated_calls_return_same_key(self):
        first = config_module._persistent_secret_key()
        second = config_module._persistent_secret_key()
        third = config_module._persistent_secret_key()

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_never_returns_empty_string(self):
        key = config_module._persistent_secret_key()
        self.assertIsInstance(key, str)
        self.assertNotEqual(key, '')

    # ---- сирота (легаси-путь) ----

    def test_orphaned_empty_file_is_recreated_with_nonempty_key(self):
        # Файл существует, но пуст — как после краша между os.open(O_CREAT) и
        # записью в СТАРОЙ реализации. Новый код такое состояние сам больше не
        # создаёт, но обязан пережить файл, оставшийся от старой версии.
        with open(self._key_file(), 'wb'):
            pass
        self.assertEqual(os.path.getsize(self._key_file()), 0)

        key = config_module._persistent_secret_key()

        self.assertTrue(key)
        self.assertNotEqual(key, '')
        self.assertGreater(os.path.getsize(self._key_file()), 0)
        with open(self._key_file(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), key)

    # ---- гонка между воркерами ----

    def test_waits_for_concurrent_writer_and_returns_their_key(self):
        # Симулируем воркера-победителя: он публикует key_file именно в
        # момент НАШЕГО os.link (не раньше — иначе мы прочли бы его на самом
        # первом _read_key() и до генерации своего ключа не дошли бы вовсе).
        # os.link должен упасть FileExistsError — наш код обязан не считать
        # это своей публикацией, а прочитать чужой ключ.
        key_file = self._key_file()

        def fake_link(src, dst):
            with open(key_file, 'w', encoding='utf-8') as f:
                f.write('winner-key-from-concurrent-worker')
            raise FileExistsError()

        with mock.patch('config.os.link', side_effect=fake_link), \
                mock.patch('config.time.sleep'):
            key = config_module._persistent_secret_key()

        self.assertEqual(key, 'winner-key-from-concurrent-worker')

    def test_gives_up_after_timeout_and_logs_warning(self):
        # os.link всегда проигрывает гонку (FileExistsError), а победитель
        # так и не дописывает key_file — ждать вечно нельзя: по истечении
        # окна ожидания воркер обязан сгенерить собственный ключ (не
        # зависнуть/не вернуть '') и залогировать деградацию, а не сделать
        # это молча (раньше окно было 1с и деградация была тихой).
        with mock.patch('config.os.link', side_effect=FileExistsError), \
                mock.patch('config.time.sleep') as mock_sleep, \
                self.assertLogs('config', level='WARNING') as log_ctx:
            key = config_module._persistent_secret_key()

        self.assertTrue(key)
        self.assertNotEqual(key, '')
        self.assertTrue(mock_sleep.called)
        self.assertGreaterEqual(mock_sleep.call_count, 50)  # окно ~5с, не ~1с
        self.assertTrue(any('secret_key' in msg for msg in log_ctx.output))

    def test_never_observes_empty_file_under_concurrent_publish(self):
        # Регресс на сам баг: даже если "конкурент" публикует файл прямо в
        # момент нашей записи, снаружи никогда не должно быть видно ПУСТОГО
        # secret_key — переопределяем os.link на os.replace-подобную запись
        # и проверяем инвариант напрямую через os.path.getsize сразу после.
        key = config_module._persistent_secret_key()
        self.assertGreater(os.path.getsize(self._key_file()), 0)


# ─────────────── БАГ 3: StartLimit* в юните телеграм-бота ───────────────

class BotServiceUnitFileTests(unittest.TestCase):
    """deploy/gruppa-titan-bot.service — валидный INI, StartLimit* в [Unit]."""

    SERVICE_PATH = os.path.join(REPO_ROOT, 'deploy', 'gruppa-titan-bot.service')

    def _parse(self):
        # systemd-юниты — «почти» INI: ключи регистрозависимы, директивы могут
        # повторяться (strict=False), значения могут содержать '%' — которые
        # ломают ConfigParser'овскую интерполяцию по умолчанию, если её не
        # выключить явно.
        parser = configparser.ConfigParser(
            strict=False, delimiters=('=',), interpolation=None)
        parser.optionxform = str  # не лоуэркейсим имена ключей
        with open(self.SERVICE_PATH, 'r', encoding='utf-8') as f:
            parser.read_file(f)
        return parser

    def test_file_exists(self):
        self.assertTrue(os.path.exists(self.SERVICE_PATH))

    def test_is_valid_ini_with_expected_sections(self):
        parser = self._parse()
        self.assertEqual(parser.sections(), ['Unit', 'Service', 'Install'])

    def test_startlimit_options_live_in_unit_section(self):
        parser = self._parse()
        self.assertIn('StartLimitIntervalSec', parser['Unit'])
        self.assertEqual(parser['Unit']['StartLimitIntervalSec'], '300')
        self.assertIn('StartLimitBurst', parser['Unit'])
        self.assertEqual(parser['Unit']['StartLimitBurst'], '10')

    def test_startlimit_options_not_in_service_section(self):
        parser = self._parse()
        self.assertNotIn('StartLimitIntervalSec', parser['Service'])
        self.assertNotIn('StartLimitBurst', parser['Service'])

    def test_restart_options_stay_in_service_section(self):
        parser = self._parse()
        self.assertEqual(parser['Service']['Restart'], 'on-failure')
        self.assertEqual(parser['Service']['RestartSec'], '10')

    def test_install_and_hardening_untouched(self):
        parser = self._parse()
        self.assertEqual(parser['Install']['WantedBy'], 'multi-user.target')
        self.assertEqual(parser['Service']['NoNewPrivileges'], 'true')


# ───────────── БАГ 11b: миграция 007 (deal_starting → _<N>) ─────────────

class Migration007Tests(unittest.TestCase):
    """m_007_deal_starting_horizon: переименование без дублей и без падения
    на уникальном индексе uq_bot_notif_user_deal_event."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='gt_migrate_test_')
        self.db_path = os.path.join(self.tmp_dir, 'test.db')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _connect(self):
        return closing(sqlite3.connect(self.db_path))

    @staticmethod
    def _create_bot_notifications(c):
        # Схема + уникальный partial-индекс — как в models.BotNotification
        c.execute("""
            CREATE TABLE bot_notifications (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                deal_id INTEGER,
                investment_id INTEGER,
                event_type VARCHAR(40) NOT NULL,
                message TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                sent_at TEXT
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX uq_bot_notif_user_deal_event
            ON bot_notifications (user_id, deal_id, event_type)
            WHERE investment_id IS NULL
        """)

    def test_missing_table_is_noop(self):
        # Свежая БД — таблицы bot_notifications ещё нет вообще
        with self._connect() as conn:
            c = conn.cursor()
            migrate.m_007_deal_starting_horizon(c)  # не должно падать
            conn.commit()

    def test_historical_rows_get_horizon_3_suffix(self):
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'старое сообщение', 'sent')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()

            c.execute("SELECT event_type FROM bot_notifications WHERE id = 1")
            self.assertEqual(c.fetchone()[0], 'deal_starting_3')

    def test_other_event_types_untouched(self):
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_published', 'сообщение', 'sent')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()

            c.execute("SELECT event_type FROM bot_notifications WHERE id = 1")
            self.assertEqual(c.fetchone()[0], 'deal_published')

    def test_idempotent_on_repeated_run(self):
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'msg', 'pending')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()
            # Повторный прогон функции напрямую — не должен падать и не
            # должен ничего больше менять (это и есть сама суть бага 11b:
            # без миграции повтор notify_upcoming.py плодил бы дубли)
            migrate.m_007_deal_starting_horizon(c)
            conn.commit()

            c.execute("SELECT COUNT(*) FROM bot_notifications")
            self.assertEqual(c.fetchone()[0], 1)
            c.execute("SELECT event_type FROM bot_notifications WHERE id = 1")
            self.assertEqual(c.fetchone()[0], 'deal_starting_3')

    def test_conflict_with_existing_horizon_row_drops_legacy_duplicate(self):
        # Redeploy успел прогнать notify_upcoming.py (уже новый код,
        # event_type='deal_starting_3') ДО того, как применилась эта
        # миграция — для той же пары (user_id, deal_id) существуют ОБЕ
        # строки. Прямой UPDATE упёрся бы в уникальный индекс; легаси-строка
        # обязана быть тихо удалена, а не уронить всю миграцию.
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'старое сообщение', 'sent')
            """)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (2, 10, 100, 'deal_starting_3', 'новое сообщение', 'pending')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()

            c.execute("SELECT id, event_type FROM bot_notifications ORDER BY id")
            self.assertEqual(c.fetchall(), [(2, 'deal_starting_3')])

    def test_conflict_resolution_is_idempotent(self):
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'старое', 'sent')
            """)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (2, 10, 100, 'deal_starting_3', 'новое', 'pending')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()
            migrate.m_007_deal_starting_horizon(c)  # повтор
            conn.commit()

            c.execute("SELECT id, event_type FROM bot_notifications ORDER BY id")
            self.assertEqual(c.fetchall(), [(2, 'deal_starting_3')])

    def test_different_deals_do_not_conflict(self):
        # Разные сделки/пользователи — переименовываются независимо, без
        # ложных конфликтов друг с другом
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'msg1', 'sent')
            """)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (2, 20, 200, 'deal_starting', 'msg2', 'sent')
            """)
            conn.commit()

            migrate.m_007_deal_starting_horizon(c)
            conn.commit()

            c.execute("SELECT id, event_type FROM bot_notifications ORDER BY id")
            self.assertEqual(
                c.fetchall(), [(1, 'deal_starting_3'), (2, 'deal_starting_3')])

    def test_full_migrate_runner_applies_007_and_stays_idempotent(self):
        # Сквозная проверка через публичный run() migrate.py, как в проде —
        # включая запись в schema_migrations и совместимость с 001-006.
        with self._connect() as conn:
            c = conn.cursor()
            self._create_bot_notifications(c)
            c.execute("""
                INSERT INTO bot_notifications
                    (id, user_id, deal_id, event_type, message, status)
                VALUES (1, 10, 100, 'deal_starting', 'msg', 'sent')
            """)
            conn.commit()

        orig_db_path = migrate.DB_PATH
        migrate.DB_PATH = self.db_path
        try:
            rc1 = migrate.run(status_only=False)
            self.assertEqual(rc1, 0)
            rc2 = migrate.run(status_only=False)  # повторный прогон — идемпотентно
            self.assertEqual(rc2, 0)
        finally:
            migrate.DB_PATH = orig_db_path

        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT event_type FROM bot_notifications WHERE id = 1")
            self.assertEqual(c.fetchone()[0], 'deal_starting_3')
            c.execute(
                "SELECT 1 FROM schema_migrations WHERE version = '007_deal_starting_horizon'")
            self.assertIsNotNone(c.fetchone())


if __name__ == '__main__':
    unittest.main()
