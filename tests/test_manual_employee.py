"""Manual employee creation uses isolated databases and HTTP sessions."""
import http.cookiejar
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def new_employee(**changes):
    return {'employee_id': '12345', 'name': 'Test Employee', 'department': 'IT',
            'position': 'ENGINEER', 'eligible_for_draw': True, **changes}


class ManualEmployeeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        self.original = app.DB_PATH
        app.DB_PATH = Path(self.temp.name) / 'test.db'
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original
        self.temp.cleanup()

    def test_create_employee_wristband_participant_and_audit_without_checkin(self):
        result = app.add_employee(new_employee(), 'test.admin')
        self.assertEqual(result['state'], 'added')
        self.assertEqual(result['person']['employee_id'], '0012345')
        self.assertEqual(result['person']['extra']['position'], 'ENGINEER')
        self.assertIsNone(result['person']['checked_in_at'])
        self.assertTrue(result['eligible_for_draw'])
        with app.db_connection() as db:
            employee = db.execute('SELECT * FROM employees').fetchone()
            self.assertEqual(app.normalize_token(employee['qr_token']), employee['qr_token'])
            self.assertEqual(app.active_wristband(db, employee['id'])['status'], 'not_issued')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_participants WHERE active=1').fetchone()[0], 1)
            self.assertEqual(app.get_setting('manual_lucky_participant_ids', [], db), ['0012345'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='employee.created'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)

    def test_duplicate_normalized_id_does_not_change_existing_employee(self):
        app.add_employee(new_employee(), 'admin')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            app.add_employee(new_employee(employee_id='0012345', name='Overwrite'), 'admin')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT name FROM employees').fetchone()[0], 'Test Employee')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)

    def test_managers_are_excluded_and_cannot_be_added_through_draw_menu(self):
        for index, position in enumerate(('MANAGER I - IT', 'sr.technical manager - EE', 'Managing Director')):
            result = app.add_employee(new_employee(employee_id=f'M{index}', position=position), 'admin')
            self.assertFalse(result['eligible_for_draw'])
            with self.assertRaisesRegex(ValueError, '[Mm]anager'):
                app.add_lucky_participant(f'M{index}', 'admin')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_participants WHERE active=1').fetchone()[0], 0)

    def test_explicit_nonparticipant_stays_out_but_can_check_in(self):
        result = app.add_employee(new_employee(eligible_for_draw=False), 'admin')
        self.assertFalse(result['eligible_for_draw'])
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees').fetchone()[0]
        self.assertEqual(app.scan_action(token, 'checkin', 'staff')[1], 'done')
        self.assertEqual(app.get_stats()['eligible'], 0)

    def test_validation_rejects_malformed_values_without_writes(self):
        invalid = [None, [], {'employee_id': '123'}, new_employee(employee_id=''),
                   new_employee(employee_id='1 2'), new_employee(employee_id='x' * 51),
                   new_employee(employee_id=123), new_employee(employee_id='a\x00b'),
                   new_employee(name=''), new_employee(name='x' * 201), new_employee(name=[]),
                   new_employee(name='Test\nName'), new_employee(position=''),
                   new_employee(position='x' * 201), new_employee(position={}),
                   new_employee(department=3), new_employee(department='x' * 201),
                   new_employee(eligible_for_draw='false'), new_employee(eligible_for_draw=1)]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                app.add_employee(data, 'admin')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 0)

    def test_limit_and_failure_leave_no_partial_employee(self):
        with patch.object(app, 'MAX_EMPLOYEES', 0), self.assertRaisesRegex(ValueError, 'limit'):
            app.add_employee(new_employee(), 'admin')
        with patch.object(app, 'audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                app.add_employee(new_employee(), 'admin')
        with app.db_connection() as db:
            for table in ('employees', 'wristbands', 'lucky_draw_participants'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_creation_preserves_legacy_draw_eligibility(self):
        app.import_employees([('OLD1', 'Existing Person', 'Ops')], 'admin')
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees').fetchone()[0]
        app.scan_action(token, 'checkin', 'staff')
        self.assertEqual(app.get_stats()['eligible'], 1)
        app.add_employee(new_employee(eligible_for_draw=False), 'admin')
        self.assertEqual(app.get_stats()['eligible'], 1)
        with app.db_connection() as db:
            self.assertEqual(db.execute("SELECT name FROM employees WHERE employee_id='OLD1'").fetchone()[0], 'Existing Person')

    def test_default_draw_choice_optional_department_and_legacy_manager_exclusion(self):
        app.import_employees([('OLD1', 'Existing Manager', 'Ops')], 'admin')
        with app.db_connection() as db:
            db.execute('UPDATE employees SET extra_json=?', (json.dumps({'position': 'MANAGER I - IT'}),))
            old_id = db.execute('SELECT id FROM employees').fetchone()[0]
            db.execute('INSERT INTO lucky_draw_participants (employee_id,active,added_at) VALUES (?,1,?)',
                       (old_id, app.now()))
            app.set_setting(db, 'manual_lucky_participant_ids', ['OLD1'], 'admin')
        result = app.add_employee({'employee_id': 'NEW', 'name': 'New Person', 'position': 'Engineer'}, 'admin')
        self.assertTrue(result['eligible_for_draw'])
        self.assertEqual(result['person']['department'], '')
        with app.db_connection() as db:
            active = {r[0] for r in db.execute('SELECT e.employee_id FROM employees e JOIN lucky_draw_participants p ON p.employee_id=e.id WHERE p.active=1')}
            self.assertEqual(active, {'NEW'})
            self.assertEqual(app.get_setting('manual_lucky_participant_ids', [], db), ['NEW'])

    def test_json_reload_preserves_new_employee_and_eligible_participation(self):
        app.import_employees([('OLD1', 'Original', 'Ops')], 'admin')
        with app.db_connection() as db:
            app.set_setting(db, 'lucky_participants_configured', True, 'admin')
        app.add_employee(new_employee(), 'admin')
        app.add_employee(new_employee(employee_id='NEW2', eligible_for_draw=False), 'admin')
        source = Path(self.temp.name) / 'employees.json'
        source.write_text(json.dumps({'employees': [dict(employee_id='OLD1', name='Original', eligible_for_draw=False)]}), encoding='utf-8')
        app.seed_employees_from_json(source)
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 3)
            active = {r[0] for r in db.execute('SELECT e.employee_id FROM employees e JOIN lucky_draw_participants p ON p.employee_id=e.id WHERE p.active=1')}
            self.assertEqual(active, {'0012345'})

    def test_creation_works_with_hosted_sqlite_adapter(self):
        from cloudflare.sqlite_adapter import SQLiteConnection, TransactionState
        from test_cloudflare_sqlite_adapter import FakeSQL
        sql = FakeSQL()
        self.addCleanup(sql.database.close)
        transaction = TransactionState()
        @contextmanager
        def connection():
            db = SQLiteConnection(sql, transaction_state=transaction)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()
        with patch.object(app, 'db_connection', connection):
            app.init_db()
            result = app.add_employee(new_employee(), 'admin')
            self.assertEqual(result['person']['employee_id'], '0012345')
            self.assertTrue(result['eligible_for_draw'])
        self.assertLessEqual(max(sql.parameter_counts), 100)
        self.assertFalse(transaction.rollback_requested)


class ManualEmployeeAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        cls.original = app.DB_PATH
        app.DB_PATH = Path(cls.temp.name) / 'web.db'
        def credential(password, salt):
            return {'salt': salt, 'hash': app.password_hash(password, salt)}
        config = {'event_name': 'Test', 'secret': '12' * 32,
                  'admin_password': credential('test-admin', 'ab' * 16),
                  'staff_password': credential('test-staff', 'cd' * 16)}
        cls.server = app.make_server('127.0.0.1', 0, config)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB_PATH = cls.original
        cls.temp.cleanup()

    def session(self):
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, data, csrf=None):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        request = Request(self.base + path, data=json.dumps(data).encode(), headers=headers)
        try:
            response = client.open(request)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    def test_admin_only_csrf_creation_duplicate_and_lookup(self):
        self.assertEqual(self.request(self.session(), '/api/admin/employees', new_employee())[0], 401)
        staff = self.session()
        _, profile = self.request(staff, '/api/login', {'password': 'test-staff'})
        self.assertEqual(self.request(staff, '/api/admin/employees', new_employee(), profile['csrf'])[0], 403)
        admin = self.session()
        _, profile = self.request(admin, '/api/login', {'password': 'test-admin'})
        self.assertEqual(self.request(admin, '/api/admin/employees', new_employee())[0], 403)
        code, result = self.request(admin, '/api/admin/employees', new_employee(), profile['csrf'])
        self.assertEqual(code, 201)
        self.assertEqual(result['person']['employee_id'], '0012345')
        self.assertTrue(result['eligible_for_draw'])
        self.assertEqual(self.request(admin, '/api/admin/employees', new_employee(), profile['csrf'])[0], 400)
        code, result = self.request(admin, '/api/lookup', {'employee_id': '12345'}, profile['csrf'])
        self.assertEqual(code, 200)
        self.assertEqual(result['person']['name'], 'Test Employee')
        self.assertIsNone(result['person']['checked_in_at'])


if __name__ == '__main__':
    unittest.main()
