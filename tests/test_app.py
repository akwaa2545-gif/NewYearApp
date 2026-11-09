"""Run: python -m unittest discover -s tests -v"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import base64
import io
import json
import os
import secrets
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
import http.cookiejar
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from scripts.convert_excel_to_json import convert


def workbook_bytes(data_headers, data_rows, lucky_headers=None, lucky_rows=None):
    """Build an in-memory workbook without assuming equal worksheet lengths."""
    import openpyxl

    book = openpyxl.Workbook()
    data_sheet = book.active
    data_sheet.title = 'DATA'
    data_sheet.append(data_headers)
    for row in data_rows:
        data_sheet.append(row)
    if lucky_headers is not None:
        lucky_sheet = book.create_sheet('lucky wheel')
        lucky_sheet.append(lucky_headers)
        for row in lucky_rows or []:
            lucky_sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_config():
    def credential(password, salt):
        return {'salt': salt, 'hash': app.password_hash(password, salt)}
    return {'event_name':'Test Event','secret':'01' * 32,
            'admin_password': credential('verystrongadminpass', 'ab' * 16),
            'staff_password': credential('safestaffpass', 'cd' * 16)}


class BusinessRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        self.original = app.DB_PATH
        app.DB_PATH = Path(self.temp.name) / 'test.db'
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original
        self.temp.cleanup()

    def test_attendance_removal_requires_confirmation_and_preserves_history(self):
        app.import_employees([('E901', 'Test One', 'IT'), ('E902', 'Test Two', 'IT')], 'test')
        with app.db_connection() as db:
            tokens = [row['qr_token'] for row in db.execute('SELECT qr_token FROM employees ORDER BY employee_id')]
        for token in tokens:
            app.scan_action(token, 'checkin', 'test')
        app.scan_action(tokens[0], 'verify', 'test')
        self.assertIsNotNone(app.draw_winner('Test prize', 'test'))
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            app.remove_attendance({'scope':'all', 'reason':'test', 'confirmation':'wrong', 'expected_count':2}, 'admin')
        with self.assertRaisesRegex(ValueError, 'Attendance changed'):
            app.remove_attendance({'scope':'all', 'reason':'test', 'confirmation':'RESET ALL', 'expected_count':1}, 'admin')
        result = app.remove_attendance({'scope':'one', 'employee_id':'E901', 'reason':'Correction',
                                        'confirmation':'REMOVE', 'expected_count':2}, 'admin')
        self.assertEqual(result['removed'], 1)
        result = app.remove_attendance({'scope':'all', 'reason':'Reset rehearsal',
                                        'confirmation':'RESET ALL', 'expected_count':1}, 'admin')
        self.assertEqual(result['removed'], 1)
        app.init_db()
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees WHERE checked_in_at IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attendance_removals').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='attendance.removed'").fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM identity_verification_history').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0], 1)

    def test_remove_draw_history_preserves_archive_and_restores_eligibility(self):
        app.import_employees([('E903', 'Test Winner', 'IT')], 'test')
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees').fetchone()[0]
        app.scan_action(token, 'checkin', 'test')
        self.assertIsNotNone(app.draw_winner('Test prize', 'test'))
        payload = {'reason':'Rehearsal', 'confirmation':'RESET DRAW', 'expected_count':1}
        with self.assertRaisesRegex(ValueError, 'progress'):
            app.remove_draw_history(payload, 'admin')
        with app.db_connection() as db:
            db.execute("UPDATE lucky_draw_presentation SET phase='winner_announced'")
        with self.assertRaisesRegex(ValueError, 'confirm'):
            app.remove_draw_history({**payload, 'confirmation':'wrong'}, 'admin')
        with self.assertRaisesRegex(ValueError, 'changed'):
            app.remove_draw_history({**payload, 'expected_count':0}, 'admin')
        self.assertEqual(app.remove_draw_history(payload, 'admin')['removed'], 1)
        app.init_db()
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM winners').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT phase FROM lucky_draw_presentation").fetchone()[0], 'waiting')
            archive = json.loads(db.execute('SELECT history_json FROM draw_history_removals').fetchone()[0])
            self.assertEqual(len(archive['winners']), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='draw.history_removed'").fetchone()[0], 1)
        self.assertEqual(app.get_stats()['eligible'], 1)
        self.assertIsNotNone(app.draw_winner('Second prize', 'test'))

    def test_wristband_checkin_controls_draw_eligibility(self):
        app.import_employees([('E501','Lost Wristband','IT'),('E502','Unreadable QR','IT'),('E503','Other','IT')], 'test')
        with app.db_connection() as db:
            tokens = {row['employee_id']:row['qr_token'] for row in db.execute('SELECT employee_id,qr_token FROM employees')}
        self.assertEqual(app.scan_action(tokens['E501'], 'checkin', 'test', 'lost')[1], 'done')
        self.assertEqual(app.scan_action(tokens['E502'], 'checkin', 'test', 'unreadable')[1], 'done')
        self.assertEqual(app.scan_action(tokens['E503'], 'checkin', 'test', 'other', 'Test reason')[1], 'done')
        self.assertEqual(app.scan_action(tokens['E501'], 'checkin', 'test', 'unreadable')[1], 'already_done')
        self.assertEqual(app.get_stats()['checked_in'], 3)
        self.assertEqual(app.get_stats()['eligible'], 1)
        winner = app.draw_winner('Test prize', 'test')
        self.assertEqual(winner['employee_id'], 'E502')
        app.remove_attendance({'scope':'one','employee_id':'E501','reason':'test','confirmation':'REMOVE','expected_count':3}, 'admin')
        with app.db_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM checkin_wristband_exceptions WHERE reason='lost'").fetchone()[0], 0)
            archived = json.loads(db.execute('SELECT checkin_json FROM attendance_removals').fetchone()[0])
            self.assertEqual(archived['wristband_reason'], 'lost')

    def test_projector_reward_list_hides_current_winner_until_reveal(self):
        app.import_employees([('E801','Winner One','IT'),('E802','Winner Two','IT'),('E803','Winner Three','IT')], 'test')
        with app.db_connection() as db:
            tokens = [row['qr_token'] for row in db.execute('SELECT qr_token FROM employees ORDER BY id')]
        for token in tokens:
            app.scan_action(token, 'checkin', 'test')
        prize = app.save_prize({'name':'Headphones', 'quantity':2}, 'admin')
        app.save_prize({'name':'Tablet', 'quantity':1}, 'admin')
        state = app.get_projector_state()
        self.assertEqual([(p['name'],p['quantity'],p['winners']) for p in state['prizes']],
                         [('Headphones',2,[]),('Tablet',1,[])])
        first, result = app.draw_configured_prize(prize['id'], 'staff')
        self.assertEqual(result, 'drawn')
        self.assertEqual(app.get_projector_state()['prizes'][0]['winners'], [])
        with self.assertRaisesRegex(ValueError, 'current draw'):
            app.save_prize({'name':'Extra reward', 'quantity':1}, 'admin')
        self.assertEqual(app.draw_configured_prize(prize['id'], 'other staff', prevent_overlap=True)[1], 'draw_in_progress')
        with app.db_connection() as db:
            db.execute("UPDATE lucky_draw_presentation SET reveal_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(app.get_projector_state()['prizes'][0]['winners'], [{'name':first['name'], 'employee_id':first['employee_id'], 'department':'IT'}])
        second, result = app.draw_configured_prize(prize['id'], 'staff')
        self.assertEqual(result, 'drawn')
        self.assertEqual([w['name'] for w in app.get_projector_state()['prizes'][0]['winners']], [first['name']])
        with app.db_connection() as db:
            db.execute("UPDATE lucky_draw_presentation SET reveal_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual([w['name'] for w in app.get_projector_state()['prizes'][0]['winners']], [first['name'], second['name']])
        self.assertEqual(app.draw_configured_prize(prize['id'], 'staff')[1], 'prize_exhausted')

    def test_reward_drafts_publish_atomically_and_preserve_images(self):
        image = 'data:image/png;base64,iVBORw0KGgo='
        first = app.save_reward_draft({'name':'Phone','quantity':2,'data_url':image})['id']
        second = app.save_reward_draft({'name':'Tablet','quantity':1})['id']
        self.assertEqual(app.get_projector_state()['prizes'], [])
        with self.assertRaisesRegex(ValueError, 'changed'):
            app.publish_reward_drafts({'ids':[first]}, 'staff')
        self.assertEqual(app.get_projector_state()['prizes'], [])
        app.publish_reward_drafts({'ids':[first,second]}, 'staff')
        prizes = app.get_projector_state()['prizes']
        self.assertEqual([p['name'] for p in prizes], ['Phone','Tablet'])
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reward_drafts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT data FROM reward_images').fetchone()[0], b'\x89PNG\r\n\x1a\n')
        draft = app.save_reward_draft({'source_id':prizes[0]['id'],'name':'Phone','quantity':3})['id']
        app.publish_reward_drafts({'ids':[draft]}, 'staff')
        self.assertEqual(app.get_projector_state()['prizes'][0]['quantity'], 3)
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reward_images').fetchone()[0], 1)

    def test_scale_1700_and_reimport_retains_token(self):
        rows = [(f'E{i:05}',f'Employee {i}',f'Department {i % 15}') for i in range(1700)]
        result = app.import_employees(rows, 'test-admin')
        self.assertEqual(result['added'], 1700)
        self.assertEqual(app.get_stats()['total'], 1700)
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees WHERE employee_id=?', ('E00001',)).fetchone()[0]
        app.scan_action(token, 'checkin', 'staff')
        result2 = app.import_employees([('E00001','New Name','Production')], 'test-admin')
        self.assertEqual(result2['updated'], 1)
        with app.db_connection() as db:
            row = db.execute('SELECT * FROM employees WHERE employee_id=?', ('E00001',)).fetchone()
        self.assertEqual(row['qr_token'], token)
        self.assertIsNotNone(row['checked_in_at'])
        self.assertEqual(row['name'], 'New Name')

    def test_1700_concurrency_safe_and_unique_draw(self):
        rows = [(f'E{i:05}',f'Employee {i}', 'QA') for i in range(1700)]
        app.import_employees(rows, 'admin')
        with app.db_connection() as db:
            tokens = [r['qr_token'] for r in db.execute('SELECT qr_token FROM employees ORDER BY id LIMIT 36')]
        self.assertEqual(app.scan_action(tokens[0], 'verify', 'staff')[1], 'checkin_required')
        with ThreadPoolExecutor(max_workers=12) as pool:
            states = list(pool.map(lambda _: app.scan_action(tokens[0], 'checkin', 'staff')[1], range(30)))
        self.assertEqual(states.count('done'), 1)
        self.assertEqual(states.count('already_done'), 29)
        self.assertEqual(app.get_stats()['checked_in'], 1)
        for token in tokens:
            app.scan_action(token, 'checkin', 'staff')
            app.scan_action(token, 'verify', 'staff')
        winners = [app.draw_winner('Lucky Gift', 'admin') for _ in range(len(tokens))]
        self.assertEqual(len({winner['employee_id'] for winner in winners}), len(tokens))
        self.assertIsNone(app.draw_winner('Extra Gift', 'admin'))
        stats = app.get_stats()
        self.assertEqual(stats['winners'], 36)
        self.assertEqual(stats['eligible'], 0)
        with app.db_connection() as db:
            action_count = db.execute('SELECT COUNT(*) FROM event_log WHERE action=? AND employee_id=(SELECT id FROM employees WHERE qr_token=?)', ('checkin',tokens[0])).fetchone()[0]
        self.assertEqual(action_count, 1)

    def test_csv_import_validation(self):
        rows = app.get_import_rows('sample.csv', b'employee_id,name,department\nA001,Person A,Engineering\n')
        self.assertEqual(rows, [('A001','Person A','Engineering')])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            app.get_import_rows('sample.csv', b'employee_id,name\nA001,A\nA001,B\n')
        self.assertEqual(app.csv_safe('=2+2'), "'=2+2")
        with self.assertRaisesRegex(ValueError, 'QR code'):
            app.normalize_token('short')

    def test_qr_zip_generation(self):
        app.import_employees([('E001','A Person','QC'),('E002','B Person','HR')], 'admin')
        raw = app.qr_zip_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as zp:
            self.assertIn('manifest.csv', zp.namelist())
            self.assertIn('labels.html', zp.namelist())
            self.assertEqual(len([n for n in zp.namelist() if n.endswith('.png')]), 2)
            self.assertIn(b'E001',zp.read('manifest.csv'))
            self.assertIn(b'E001',zp.read('labels.html'))

    def test_excel_import(self):
        import openpyxl
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(['Employee ID','Full Name','Dept'])
        sheet.append(['E002','Test Person','Plant A'])
        buffer = io.BytesIO(); book.save(buffer)
        self.assertEqual(app.get_import_rows('people.xlsx', buffer.getvalue()), [('E002','Test Person','Plant A')])

    def test_json_seed_loads_staff_source_and_preserves_qr_token(self):
        source = Path(self.temp.name) / 'employees.json'
        source.write_text(json.dumps({'source_file': 'employees.xlsx', 'employees': [{
            'employee_id': '001234', 'name': 'Test Person', 'department': 'IT',
            'first_name': 'Test', 'last_name': 'Person',
            'position': 'Engineer', 'route': '1', 'eligible_for_draw': True,
            'qr_token': 'a' * 24,
        }]}), encoding='utf-8')
        result = app.seed_employees_from_json(source)
        self.assertEqual(result['added'], 1)
        with app.db_connection() as db:
            employee = db.execute('SELECT * FROM employees').fetchone()
            participant = db.execute('SELECT active FROM lucky_draw_participants').fetchone()
        self.assertEqual(employee['employee_id'], '0001234')
        self.assertEqual(employee['qr_token'], 'a' * 24)
        self.assertEqual(json.loads(employee['extra_json'])['position'], 'Engineer')
        self.assertEqual(json.loads(employee['extra_json'])['first_name'], 'Test')
        self.assertEqual(participant['active'], 1)


class ConverterTests(unittest.TestCase):
    def test_two_sheets_reconcile_lucky_participants_by_employee_id(self):
        import openpyxl
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'employees.xlsx'
            output = Path(folder) / 'employees.json'
            book = openpyxl.Workbook()
            data = book.active
            data.title = 'DATA'
            data.append(['No', 'Employee ID', 'Title', 'First', 'Last', 'Full Name',
                         'Position', 'Department', 'Route'])
            data.append([1, '001', '', 'One', 'Employee', 'Employee One', 'Engineer', 'IT', 1])
            data.append([2, '002', '', 'Two', 'Employee', 'Employee Two', 'Operator', 'Production', 2])
            lucky = book.create_sheet('Lucky Wheel')
            lucky.append(['No', 'Employee ID', 'Title', 'First', 'Last', 'Full Name',
                          'Position', 'Department'])
            lucky.append([1, '002', '', '', '', 'Employee Two', 'Operator', 'Production'])
            book.save(source)

            payload = convert(source, output)
            eligibility = {row['employee_id']: row['eligible_for_draw']
                           for row in payload['employees']}
            self.assertEqual(eligibility, {'0000001': False, '0000002': True})
            self.assertEqual(payload['draw_participant_count'], 1)
            self.assertEqual(payload['reconciliation']['matched'], 1)
            self.assertEqual(payload['reconciliation']['data_only'], 1)
            self.assertEqual(payload['reconciliation']['lucky_only'], 0)
            self.assertEqual(payload['employees'][0]['first_name'], 'One')


class UpgradeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        self.original = app.DB_PATH
        app.DB_PATH = Path(self.temp.name) / 'upgrade.db'
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original
        self.temp.cleanup()

    def active_token(self, employee_id):
        with app.db_connection() as db:
            row = db.execute('''SELECT w.token FROM wristbands w
                JOIN employees e ON e.id=w.employee_id
                WHERE e.employee_id=? AND w.status IN ('not_issued','issued')
                ORDER BY w.id DESC LIMIT 1''', (employee_id,)).fetchone()
        self.assertIsNotNone(row, employee_id)
        return row['token']

    def inspect_and_preview(self, raw, *, data_mapping, lucky_type='none',
                            lucky_mapping=None):
        inspected = app.inspect_import('company-party.xlsx', raw, 'admin.user')
        options = {
            'data_sheet': 'DATA',
            'data_mapping': data_mapping,
            'lucky_type': lucky_type,
        }
        if lucky_type != 'none':
            options.update({
                'lucky_sheet': 'lucky wheel',
                'lucky_mapping': lucky_mapping or {},
            })
        return inspected, app.build_import_preview(
            inspected['upload_id'], options, 'admin.user')

    def configured_draw(self, prize_id, actor='draw.operator'):
        if hasattr(app, 'draw_configured_prize'):
            return app.draw_configured_prize(prize_id, actor)
        with app.db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                winner, state = app._draw_with_db(
                    db, None, actor, prize_id=prize_id, enforce_quantity=True)
                if winner is None:
                    db.rollback()
                else:
                    db.commit()
                return winner, state
            except Exception:
                db.rollback()
                raise

    def test_additive_migration_backfills_legacy_records_idempotently(self):
        legacy_path = Path(self.temp.name) / 'legacy.db'
        app.DB_PATH = legacy_path
        stamp = '2026-10-01T12:00:00+00:00'
        token = secrets.token_hex(16)
        # sqlite3.Connection's context manager commits or rolls back but does
        # not close the handle. Explicit closing keeps Windows from retaining
        # a lock on the temporary legacy database during tearDown.
        with closing(sqlite3.connect(legacy_path)) as db:
            db.executescript('''
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY,
                    employee_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    department TEXT NOT NULL DEFAULT '',
                    qr_token TEXT NOT NULL UNIQUE,
                    checked_in_at TEXT,
                    verified_at TEXT
                );
                CREATE TABLE winners (
                    id INTEGER PRIMARY KEY,
                    employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(id),
                    prize TEXT NOT NULL,
                    drawn_at TEXT NOT NULL,
                    drawn_by TEXT NOT NULL
                );
                CREATE TABLE event_log (
                    id INTEGER PRIMARY KEY,
                    employee_id INTEGER REFERENCES employees(id),
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
            ''')
            db.execute('''INSERT INTO employees
                (employee_id,name,department,qr_token,checked_in_at,verified_at)
                VALUES (?,?,?,?,?,?)''',
                ('00123', 'Legacy Person', 'Operations', token, stamp, stamp))
            db.execute('''INSERT INTO winners(employee_id,prize,drawn_at,drawn_by)
                          VALUES (1,'Legacy Prize',?, 'legacy-admin')''', (stamp,))
            db.commit()
        app.init_db()
        app.init_db()
        with app.db_connection() as db:
            self.assertIn('extra_json', app._table_columns(db, 'employees'))
            self.assertIn('status', app._table_columns(db, 'winners'))
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM migrations WHERE version=1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM migrations WHERE version=2').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT employee_id FROM employees').fetchone()[0], '0000123')
            wristband = db.execute('SELECT token,status FROM wristbands').fetchone()
            self.assertEqual((wristband['token'], wristband['status']), (token, 'issued'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM identity_verification_history').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM prizes').fetchone()[0], 1)

    def test_unequal_participant_sheet_reconciles_by_identifier(self):
        raw = workbook_bytes(
            ['Payroll Code', 'Employee Display', 'Cost Centre'],
            [
                ['0001', 'Alice One', 'Engineering'],
                ['0002', 'Bob Two', 'Finance'],
                ['0003', 'Cara Three', 'Quality'],
                ['0004', 'Dan Four', 'Operations'],
            ],
            ['Draw Identifier'],
            [['0003'], ['0001'], ['9999']],
        )
        inspected, preview = self.inspect_and_preview(
            raw,
            data_mapping={
                'employee_id': 'Payroll Code',
                'name': 'Employee Display',
                'department': 'Cost Centre',
            },
            lucky_type='participants',
            lucky_mapping={'employee_id': 'Draw Identifier'},
        )
        self.assertEqual(
            {sheet['name']: sheet['row_count'] for sheet in inspected['sheets']},
            {'DATA': 4, 'lucky wheel': 3},
        )
        self.assertEqual(preview['errors'], [])
        self.assertEqual(
            {item['employee_id'] for item in preview['reconciliation']['matched']},
            {'0000001', '0000003'},
        )
        self.assertEqual(
            {item['employee_id'] for item in preview['reconciliation']['unmatched']},
            {'0009999'},
        )
        self.assertEqual(
            {item['employee_id'] for item in preview['reconciliation']['master_only']},
            {'0000002', '0000004'},
        )
        result = app.commit_import(inspected['upload_id'], 'admin.user')
        self.assertEqual((result['added'], result['count']), (4, 4))
        repeated = app.commit_import(inspected['upload_id'], 'admin.user')
        self.assertTrue(repeated['already_committed'])
        with app.db_connection() as db:
            employees = {row['employee_id']: row['name'] for row in db.execute(
                'SELECT employee_id,name FROM employees')}
            participants = {row['employee_id'] for row in db.execute('''
                SELECT e.employee_id FROM lucky_draw_participants p
                JOIN employees e ON e.id=p.employee_id WHERE p.active=1''')}
            self.assertEqual(employees['0000001'], 'Alice One')
            self.assertEqual(employees['0000003'], 'Cara Three')
            self.assertNotIn('0009999', employees)
            self.assertEqual(participants, {'0000001', '0000003'})
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM wristbands WHERE status='not_issued'").fetchone()[0], 4)

    def test_lucky_sheet_may_have_more_rows_than_data(self):
        raw = workbook_bytes(
            ['Code', 'Person'],
            [['0100', 'First Person'], ['0200', 'Second Person']],
            ['Eligible Code'],
            [['0200'], ['0100'], ['0300']],
        )
        _, preview = self.inspect_and_preview(
            raw,
            data_mapping={'employee_id': 'Code', 'name': 'Person'},
            lucky_type='participants',
            lucky_mapping={'employee_id': 'Eligible Code'},
        )
        self.assertEqual(preview['summary']['data_rows'], 2)
        self.assertEqual(preview['summary']['participant_rows'], 3)
        self.assertEqual(preview['summary']['matched_participants'], 2)
        self.assertEqual(preview['summary']['unmatched_participants'], 1)

    def test_import_preview_reports_row_errors_and_blocks_commit(self):
        raw = workbook_bytes(
            ['Badge Number', 'Display Name', 'Team'],
            [
                ['A001', 'Valid Person', 'Ops'],
                ['A001', 'Duplicate Person', 'Ops'],
                ['', 'Missing Identifier', 'Ops'],
                ['B002', '', 'Ops'],
                ['X' * 61, 'Identifier Too Long', 'Ops'],
            ],
            ['Lucky Badge'],
            [['A001'], ['A001'], [''], ['NOT-IN-MASTER']],
        )
        inspected, preview = self.inspect_and_preview(
            raw,
            data_mapping={
                'employee_id': 'Badge Number',
                'name': 'Display Name',
                'department': 'Team',
            },
            lucky_type='participants',
            lucky_mapping={'employee_id': 'Lucky Badge'},
        )
        error_keys = {(error['sheet'], error['row'], error['code'])
                      for error in preview['errors']}
        self.assertIn(('DATA', 3, 'duplicate'), error_keys)
        self.assertIn(('DATA', 4, 'required'), error_keys)
        self.assertIn(('DATA', 5, 'required'), error_keys)
        self.assertIn(('DATA', 6, 'too_long'), error_keys)
        self.assertIn(('lucky wheel', 3, 'duplicate'), error_keys)
        self.assertIn(('lucky wheel', 4, 'required'), error_keys)
        self.assertEqual(
            [item['employee_id'] for item in preview['reconciliation']['unmatched']],
            ['NOT-IN-MASTER'],
        )
        with self.assertRaisesRegex(ValueError, 'validation errors'):
            app.commit_import(inspected['upload_id'], 'admin.user')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 0)

    def test_import_commit_preserves_wristband_and_event_history(self):
        app.import_employees([('00123', 'Original Name', 'Assembly')], 'admin.user')
        issued = app.change_wristband('00123', 'issue', 'issuer.user')
        token = app.normalize_token(issued['qr_value'])
        app.scan_action(token, 'checkin', 'checkin.user')
        app.record_verification(token, 'verified', None, 'verification.user')
        winner = app.draw_winner('Existing Prize', 'admin.user')
        self.assertEqual(winner['employee_id'], '0000123')
        raw = workbook_bytes(
            ['Employee Key', 'Preferred Name', 'Department'],
            [['00123', 'Updated Name', 'Engineering']],
        )
        inspected, preview = self.inspect_and_preview(
            raw,
            data_mapping={
                'employee_id': 'Employee Key',
                'name': 'Preferred Name',
                'department': 'Department',
            },
        )
        self.assertEqual(preview['summary']['employees_to_update'], 1)
        app.commit_import(inspected['upload_id'], 'admin.user')
        with app.db_connection() as db:
            employee = db.execute(
                'SELECT * FROM employees WHERE employee_id=?', ('0000123',)).fetchone()
            self.assertEqual(employee['name'], 'Updated Name')
            self.assertEqual(employee['qr_token'], token)
            self.assertIsNotNone(employee['checked_in_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wristbands').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertEqual(db.execute(
                'SELECT COUNT(*) FROM identity_verification_history').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0], 1)

    def test_wristband_issue_replace_and_revoke_are_independent_of_attendance(self):
        app.import_employees([
            ('E001', 'Issued Person', 'Ops'),
            ('E002', 'Revoked Person', 'Ops'),
        ], 'admin.user')
        first = app.change_wristband('E001', 'issue', 'issuer.user')
        first_token = app.normalize_token(first['qr_value'])
        with app.db_connection() as db:
            employee = db.execute(
                'SELECT checked_in_at FROM employees WHERE employee_id=?', ('E001',)).fetchone()
            self.assertIsNone(employee['checked_in_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)
        replacement = app.change_wristband(
            'E001', 'replace', 'issuer.user', 'Damaged during distribution')
        replacement_token = app.normalize_token(replacement['qr_value'])
        self.assertNotEqual(first_token, replacement_token)
        _, old_state = app.scan_action(first_token, 'checkin', 'checkin.user')
        self.assertEqual(old_state, 'replaced')
        _, new_state = app.scan_action(replacement_token, 'checkin', 'checkin.user')
        self.assertEqual(new_state, 'done')
        second = app.change_wristband('E002', 'issue', 'issuer.user')
        revoked_token = app.normalize_token(second['qr_value'])
        app.change_wristband('E002', 'revoke', 'issuer.user', 'Employee cannot attend')
        _, revoked_state = app.scan_action(revoked_token, 'checkin', 'checkin.user')
        self.assertEqual(revoked_state, 'revoked')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM wristbands WHERE status='replaced'").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM wristbands WHERE status='revoked'").fetchone()[0], 1)

    def test_concurrent_checkin_creates_one_attendance_record(self):
        app.import_employees([('E100', 'Concurrent Person', 'QA')], 'admin.user')
        issued = app.change_wristband('E100', 'issue', 'issuer.user')
        token = app.normalize_token(issued['qr_value'])
        with ThreadPoolExecutor(max_workers=16) as pool:
            states = list(pool.map(
                lambda _: app.scan_action(token, 'checkin', 'entrance-a')[1], range(40)))
        self.assertEqual(states.count('done'), 1)
        self.assertEqual(states.count('already_done'), 39)
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM event_log WHERE action='checkin'").fetchone()[0], 1)

    def test_repeat_and_failed_verification_never_mutate_attendance(self):
        app.import_employees([('E200', 'Verify Person', 'Finance')], 'admin.user')
        issued = app.change_wristband('E200', 'issue', 'issuer.user')
        token = app.normalize_token(issued['qr_value'])
        _, before_checkin = app.record_verification(
            token, 'verified', None, 'verification.user')
        self.assertEqual(before_checkin['result'], 'checkin_required')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM identity_verification_history').fetchone()[0], 0)
            self.assertIsNone(db.execute(
                'SELECT checked_in_at FROM employees WHERE employee_id=?', ('E200',)).fetchone()[0])
        app.scan_action(token, 'checkin', 'checkin.user')
        with app.db_connection() as db:
            checkin_time = db.execute(
                'SELECT checked_in_at FROM event_checkins WHERE employee_id=(SELECT id FROM employees WHERE employee_id=?)',
                ('E200',)).fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'reason'):
            app.record_verification(token, 'identity_mismatch', None, 'verification.user')
        app.record_verification(
            token, 'identity_mismatch', 'Badge photo does not match', 'verification.user')
        app.record_verification(token, 'verified', None, 'verification.user')
        app.record_verification(token, 'verified', None, 'verification.user')
        with app.db_connection() as db:
            results = [row['result'] for row in db.execute(
                'SELECT result FROM identity_verification_history ORDER BY id')]
            self.assertEqual(results, ['identity_mismatch', 'verified', 'verified'])
            self.assertEqual(db.execute(
                'SELECT checked_in_at FROM event_checkins').fetchone()[0], checkin_time)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)

    def test_participant_eligibility_prize_quantity_and_concurrent_draw(self):
        app.import_employees([
            ('E301', 'Participant One', 'Ops'),
            ('E302', 'Participant Two', 'Ops'),
            ('E303', 'Participant Three', 'Ops'),
            ('E304', 'Not A Participant', 'Ops'),
        ], 'admin.user')
        with app.db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            stamp = app.now()
            for employee_id in ('E301', 'E302', 'E303'):
                internal_id = db.execute(
                    'SELECT id FROM employees WHERE employee_id=?', (employee_id,)).fetchone()[0]
                db.execute('''INSERT INTO lucky_draw_participants
                    (employee_id,active,added_at) VALUES (?,1,?)''', (internal_id, stamp))
            app.set_setting(db, 'lucky_participants_configured', True, 'admin.user')
            db.commit()
        for employee_id in ('E301', 'E302', 'E303', 'E304'):
            issued = app.change_wristband(employee_id, 'issue', 'issuer.user')
            app.scan_action(app.normalize_token(issued['qr_value']), 'checkin', 'checkin.user')
        prize = app.save_prize({'name': 'Headphones', 'quantity': 2}, 'admin.user')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.configured_draw(prize['id']), range(8)))
        winners = [winner for winner, _ in results if winner is not None]
        self.assertEqual(len(winners), 2)
        self.assertEqual(len({winner['employee_id'] for winner in winners}), 2)
        self.assertLessEqual(
            {winner['employee_id'] for winner in winners}, {'E301', 'E302', 'E303'})
        self.assertNotIn('E304', {winner['employee_id'] for winner in winners})
        self.assertTrue(all(
            state in ('drawn', 'prize_exhausted', 'no_eligible') for _, state in results))
        with app.db_connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'",
                (prize['id'],)).fetchone()[0], 2)
            self.assertEqual(db.execute(
                'SELECT COUNT(DISTINCT employee_id) FROM lucky_draw_winners').fetchone()[0], 2)

    def test_reroll_requires_reason_and_preserves_audit_history(self):
        app.import_employees([
            ('E401', 'Reroll One', 'Ops'),
            ('E402', 'Reroll Two', 'Ops'),
            ('E403', 'Reroll Three', 'Ops'),
        ], 'admin.user')
        for employee_id in ('E401', 'E402', 'E403'):
            issued = app.change_wristband(employee_id, 'issue', 'issuer.user')
            app.scan_action(app.normalize_token(issued['qr_value']), 'checkin', 'checkin.user')
        prize = app.save_prize({'name': 'Tablet', 'quantity': 1}, 'admin.user')
        original, state = self.configured_draw(prize['id'])
        self.assertEqual(state, 'drawn')
        with self.assertRaisesRegex(ValueError, 'reason'):
            app.reroll_winner(original['winner_id'], '', 'admin.user')
        rerolled = app.reroll_winner(
            original['winner_id'], 'Winner left before collection', 'admin.user')
        self.assertNotEqual(
            rerolled['winner']['employee_id'], original['employee_id'])
        with app.db_connection() as db:
            old = db.execute(
                'SELECT status,reroll_reason FROM lucky_draw_winners WHERE id=?',
                (original['winner_id'],)).fetchone()
            self.assertEqual((old['status'], old['reroll_reason']),
                             ('rerolled', 'Winner left before collection'))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'",
                (prize['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='draw.winner_rerolled'").fetchone()[0], 1)

    def test_all_xlsx_reports_keep_employee_ids_as_text(self):
        app.import_employees([('00123', 'Leading Zero Person', 'Finance')], 'admin.user')
        raw = app.xlsx_report_bytes('all')
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), data_only=False)
        self.assertEqual(set(workbook.sheetnames), {
            'Master Employees', 'Wristbands', 'Attendance', 'Not Checked In',
            'Verifications', 'Reconciliation', 'Draw Eligibility', 'Winners',
            'Event Summary',
        })
        identifier = workbook['Master Employees']['A2']
        self.assertEqual(identifier.value, '0000123')
        self.assertEqual(identifier.data_type, 's')
        self.assertEqual(identifier.number_format, '@')
        with self.assertRaisesRegex(ValueError, 'Unknown report'):
            app.xlsx_report_bytes('not-a-report')


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        cls.old = app.DB_PATH
        app.DB_PATH = Path(cls.temp.name) / 'web.db'
        cls.server = app.make_server('127.0.0.1', 0, test_config())
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB_PATH = cls.old
        cls.temp.cleanup()

    def session(self):
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, data=None, csrf=None, content_type='application/json'):
        headers = {}
        if csrf: headers['X-CSRF-Token'] = csrf
        if data is not None:
            headers['Content-Type'] = content_type
            if isinstance(data, dict): data = json.dumps(data).encode()
        req = Request(self.base + path, data=data, headers=headers)
        try:
            result = client.open(req)
            return result.status, json.loads(result.read()), result.headers
        except HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_direct_staff_access_keeps_admin_protected(self):
        client = self.session()
        with patch.dict(os.environ, {'PARTY_OPEN_STAFF':'1'}):
            status, staff, _ = self.request(client, '/api/staff-session', {})
            self.assertEqual((status, staff['role']), (200, 'staff'))
            self.assertEqual(self.request(client, '/api/admin/activity')[0], 403)
            self.assertEqual(self.request(client, '/api/scan/checkin', {'employee_id':'1234567'})[0], 403)
            self.assertEqual(self.request(client, '/api/login', {'password':'safestaffpass', 'admin_only':True})[0], 401)
            status, admin, _ = self.request(client, '/api/login', {'password':'verystrongadminpass', 'admin_only':True})
            self.assertEqual((status, admin['role']), (200, 'admin'))
            self.assertEqual(self.request(client, '/api/admin/activity')[0], 200)
            req = Request(self.base + '/api/staff-session', data=b'{}', headers={'Content-Type':'application/json', 'Origin':'https://untrusted.invalid'})
            with self.assertRaises(HTTPError) as denied:
                self.session().open(req)
            self.assertEqual(denied.exception.code, 403)
        with patch.dict(os.environ, {'PARTY_OPEN_STAFF':'0'}):
            self.assertEqual(self.request(self.session(), '/api/staff-session', {})[0], 403)

    def test_shared_passwords_select_role(self):
        client = self.session()
        status, profile, _ = self.request(client, '/api/login', {'password':'verystrongadminpass'})
        self.assertEqual((status, profile['role']), (200, 'admin'))
        status, profile, _ = self.request(self.session(), '/api/login', {'password':'safestaffpass'})
        self.assertEqual((status, profile['role']), (200, 'staff'))

    def test_wheel_preview_only_exposes_eligible_ids_to_draw_staff(self):
        app.import_employees([('PREVIEW001','Preview One','IT'),('PREVIEW002','Preview Two','IT')], 'test')
        with app.db_connection() as db:
            app.set_setting(db, 'projector_show_employee_id', True, 'test')
            app.set_setting(db, 'lucky_participants_configured', False, 'test')
            app.set_setting(db, 'require_verification_for_draw', False, 'test')
            tokens = {r['employee_id']:r['qr_token'] for r in db.execute("SELECT employee_id,qr_token FROM employees WHERE employee_id LIKE 'PREVIEW%'")}
        app.scan_action(tokens['PREVIEW001'], 'checkin', 'test')
        self.assertEqual(self.request(self.session(), '/api/draw/eligible-ids')[0], 401)
        staff = self.session()
        self.request(staff, '/api/login', {'password':'safestaffpass'})
        status, result, _ = self.request(staff, '/api/draw/eligible-ids')
        self.assertEqual(status, 200)
        self.assertIn('PREVIEW001', result['items'])
        self.assertNotIn('PREVIEW002', result['items'])

    def test_manual_id_reason_and_numeric_qr(self):
        app.import_employees([('2612501','QR Employee','IT'),('2612502','Manual Employee','IT'),
                              ('2612503','Not Checked In','IT')], 'test')
        with app.db_connection() as db:
            app.set_setting(db, 'projector_show_employee_id', True, 'test')
            app.set_setting(db, 'lucky_participants_configured', False, 'test')
            app.set_setting(db, 'require_verification_for_draw', False, 'test')
        client = self.session()
        status, login, _ = self.request(client, '/api/login', {'password':'safestaffpass'})
        csrf = login['csrf']
        status, result, _ = self.request(client, '/api/lookup', {'code':'2612501'}, csrf)
        self.assertEqual(result['lookup'], {'code':'2612501'})
        self.assertEqual(self.request(client, '/api/scan/checkin', result['lookup'], csrf)[0], 200)
        self.assertEqual(self.request(client, '/api/scan/checkin', {'employee_id':'2612502'}, csrf)[0], 400)
        self.assertEqual(self.request(client, '/api/scan/checkin', {'employee_id':'2612502', 'wristband_reason':'other'}, csrf)[0], 400)
        self.assertEqual(self.request(client, '/api/scan/checkin', {'employee_id':'2612502', 'wristband_reason':'lost'}, csrf)[0], 200)
        with app.db_connection() as db:
            row = db.execute("SELECT x.reason FROM checkin_wristband_exceptions x JOIN employees e ON e.id=x.employee_id WHERE e.employee_id='2612502'").fetchone()
            self.assertEqual(row['reason'], 'lost')
        status, eligible, _ = self.request(client, '/api/draw/eligible-ids')
        self.assertEqual(status, 200)
        self.assertNotIn('2612502', eligible['items'])
        self.assertIn('2612501', eligible['items'])
        self.assertEqual(self.request(client, '/api/checkin/wristband-lost',
                                      {'employee_id':'2612503'}, csrf)[0], 409)
        status, marked, _ = self.request(client, '/api/checkin/wristband-lost',
                                         {'employee_id':'2612501'}, csrf)
        self.assertEqual((status, marked['state']), (200, 'updated'))
        self.assertEqual(marked['person']['checkin_wristband_reason'], 'lost')
        self.assertEqual(self.request(client, '/api/checkin/wristband-lost',
                                      {'employee_id':'2612501'}, csrf)[1]['state'], 'already_lost')
        with app.db_connection() as db:
            self.assertEqual(db.execute('''SELECT COUNT(*) FROM event_checkins c
                JOIN employees e ON e.id=c.employee_id
                WHERE e.employee_id IN ('2612501','2612502')''').fetchone()[0], 2)
        status, eligible, _ = self.request(client, '/api/draw/eligible-ids')
        self.assertEqual(status, 200)
        self.assertNotIn('2612501', eligible['items'])

    def test_staff_attendance_history_filters(self):
        app.import_employees([('HIST001','QR Guest','IT'), ('HIST002','Unreadable Guest','IT'),
                              ('HIST003','Lost Guest','HR'), ('HIST004','Waiting Guest','HR')], 'test')
        with app.db_connection() as db:
            tokens = {row['employee_id']: row['qr_token'] for row in db.execute(
                "SELECT employee_id,qr_token FROM employees WHERE employee_id LIKE 'HIST%'")}
        app.scan_action(tokens['HIST001'], 'checkin', 'test')
        app.scan_action(tokens['HIST002'], 'checkin', 'test', wristband_reason='unreadable')
        app.scan_action(tokens['HIST003'], 'checkin', 'test')
        app.mark_checked_in_wristband_lost('HIST003', 'test')
        self.assertEqual(self.request(self.session(), '/api/attendance?employee_id=HIST')[0], 401)
        staff = self.session()
        self.request(staff, '/api/login', {'password':'safestaffpass'})
        def ids(status):
            code, result, _ = self.request(staff, f'/api/attendance?employee_id=HIST&status={status}')
            self.assertEqual(code, 200)
            return result
        self.assertEqual(ids('all')['total'], 4)
        self.assertEqual({row['employee_id'] for row in ids('checked')['items']},
                         {'HIST001','HIST002','HIST003'})
        self.assertEqual([row['employee_id'] for row in ids('pending')['items']], ['HIST004'])
        self.assertEqual([row['employee_id'] for row in ids('lost')['items']], ['HIST003'])
        self.assertEqual([row['employee_id'] for row in ids('unreadable')['items']], ['HIST002'])
        self.assertEqual([row['employee_id'] for row in self.request(
            staff, '/api/attendance?employee_id=HIST004')[1]['items']], ['HIST004'])

    def test_admin_activity_and_attendance_permissions(self):
        admin, staff = self.session(), self.session()
        _, admin_login, _ = self.request(admin, '/api/login', {'password':'verystrongadminpass'})
        _, staff_login, _ = self.request(staff, '/api/login', {'password':'safestaffpass'})
        status, activity, _ = self.request(admin, '/api/admin/activity')
        self.assertEqual(status, 200)
        self.assertTrue({'legacy-admin', 'legacy-staff'}.issubset({row['username'] for row in activity['items']}))
        status, _, _ = self.request(staff, '/api/admin/activity')
        self.assertEqual(status, 403)
        self.assertEqual(self.request(staff, '/api/admin/draw-history')[0], 403)
        self.assertEqual(self.request(staff, '/api/admin/draw-history/remove', {'reason':'test','confirmation':'RESET DRAW','expected_count':0}, staff_login['csrf'])[0], 403)
        self.assertEqual(self.request(admin, '/api/admin/draw-history/remove', {'reason':'test','confirmation':'RESET DRAW','expected_count':0})[0], 403)
        payload = {'scope':'all', 'reason':'test', 'confirmation':'RESET ALL', 'expected_count':0}
        status, _, _ = self.request(staff, '/api/admin/attendance/remove', payload, staff_login['csrf'])
        self.assertEqual(status, 403)
        status, _, _ = self.request(admin, '/api/admin/attendance/remove', payload)
        self.assertEqual(status, 403)
        status, _, _ = self.request(staff, '/api/logout', {}, staff_login['csrf'])
        self.assertEqual(status, 200)
        status, activity, _ = self.request(admin, '/api/admin/activity')
        self.assertNotIn('legacy-staff', {row['username'] for row in activity['items']})

    def test_auth_import_checkin_verify_draw(self):
        admin = self.session()
        staff = self.session()
        status, _, _ = self.request(admin, '/api/stats')
        self.assertEqual(status, 401)
        status, login, _ = self.request(admin, '/api/login', {'password':'verystrongadminpass'})
        self.assertEqual(status, 200)
        admin_csrf = login['csrf']
        status, staff_login, _ = self.request(staff, '/api/login', {'password':'safestaffpass'})
        self.assertEqual(status, 200)
        staff_csrf = staff_login['csrf']

        prize_png = b'\x89PNG\r\n\x1a\nprize-picture-test'
        status, image_result, _ = self.request(staff, '/api/projector/prize-image', {
            'filename': 'grand-prize.png',
            'data_url': 'data:image/png;base64,' + base64.b64encode(prize_png).decode('ascii'),
        }, staff_csrf)
        self.assertEqual(status, 200, image_result)
        self.assertTrue(image_result['state']['prize_image_url'].startswith(
            '/api/projector/prize-image?v='))
        self.assertEqual(image_result['state']['prize_image_name'], 'grand-prize.png')
        image_response = staff.open(self.base + image_result['state']['prize_image_url'])
        self.assertEqual(image_response.headers.get_content_type(), 'image/png')
        self.assertEqual(image_response.read(), prize_png)
        csv_bytes = b'employee_id,name,department\nE100,Foo Bar,Production\nE101,Foo Two,QC\n'
        boundary = 'boundaryfortest'
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="staff.csv"\r\n'
                   'Content-Type: text/csv\r\n\r\n').encode() + csv_bytes + f'\r\n--{boundary}--\r\n'.encode()
        status, result, _ = self.request(staff, '/api/import', payload, staff_csrf,
                                         'multipart/form-data; boundary=' + boundary)
        self.assertEqual(status, 403)
        status, result, _ = self.request(admin, '/api/import', payload, admin_csrf,
                                         'multipart/form-data; boundary=' + boundary)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['added'], 2)
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees WHERE employee_id=?', ('E100',)).fetchone()[0]
        code = app.TOKEN_PREFIX + token
        status, result, _ = self.request(
            staff, '/api/lookup', {'employee_id': 'E100'}, staff_csrf)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['person']['employee_id'], 'E100')
        status, result, _ = self.request(staff, '/api/lookup', {'code': code}, staff_csrf)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['person']['employee_id'], 'E100')
        status, result, _ = self.request(staff, '/api/scan/verify', {'code':code,'confirmed_match':True}, staff_csrf)
        self.assertEqual(status, 409)
        status, result, _ = self.request(staff, '/api/scan/checkin', {'code':code}, staff_csrf)
        self.assertEqual(result['state'], 'done')
        status, result, _ = self.request(staff, '/api/scan/checkin', {'code':code}, staff_csrf)
        self.assertEqual(result['state'], 'already_done')
        status, result, _ = self.request(staff, '/api/scan/verify', {'code':code,'confirmed_match':False}, staff_csrf)
        self.assertEqual(status, 400)
        status, result, _ = self.request(staff, '/api/scan/verify', {'code':code,'confirmed_match':True}, staff_csrf)
        self.assertEqual(result['state'], 'done')
        status, result, _ = self.request(
            staff, '/api/draw/participants', {'employee_id':'E100'}, staff_csrf)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['person']['eligible_now'])
        status, result, _ = self.request(
            staff, '/api/draw', {'prize':'Bike', 'round_name':'Opening Round'}, staff_csrf)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['winner']['employee_id'], 'E100')
        self.assertEqual(result['winner']['round_name'], 'Opening Round')
        status, result, _ = self.request(admin, '/api/draw', {'prize':'Bike 2'}, admin_csrf)
        self.assertEqual(status, 409)
        status, result, _ = self.request(admin, '/api/stats')
        self.assertEqual(result['winners'],1)
        status, result, _ = self.request(staff, '/api/employees')
        self.assertEqual(status,403)
        status, result, _ = self.request(admin, '/api/employees?q=Foo')
        self.assertEqual(result['total'],2)
        self.assertEqual(len(result['items']),2)
        status, result, _ = self.request(
            admin, '/api/employees?department=Production&wristband=not_issued&status=checkedin&sort=name')
        self.assertEqual(status, 200, result)
        self.assertEqual([item['employee_id'] for item in result['items']], ['E100'])
        self.assertIn('Production', result['departments'])
        self.assertEqual(result['items'][0]['latest_verification_result'], 'verified')
        qr_response = admin.open(self.base + '/api/wristbands/qr?employee_id=E100')
        self.assertEqual(qr_response.headers.get_content_type(), 'image/png')
        self.assertTrue(qr_response.read().startswith(b'\x89PNG\r\n\x1a\n'))
        status, result, _ = self.request(admin, '/api/lookup', {'code':code})
        self.assertEqual(status, 403)

    def test_named_roles_are_scoped_and_disabled_sessions_are_rejected(self):
        with app.db_connection() as db:
            has_employee = db.execute(
                'SELECT 1 FROM employees WHERE employee_id=?', ('E100',)).fetchone()
        if not has_employee:
            app.import_employees([('E100', 'Foo Bar', 'Production')], 'legacy-admin')
        accounts = (
            ('door.one', 'Door One', 'checkin_staff', 'DoorPassword123!'),
            ('verify.one', 'Verify One', 'verification_staff', 'VerifyPassword123!'),
            ('draw.one', 'Draw One', 'lucky_draw_operator', 'DrawPassword123!'),
        )
        for username, display_name, role, password in accounts:
            app.create_user({
                'username': username,
                'display_name': display_name,
                'role': role,
                'password': password,
            }, 'legacy-admin')
        with self.assertRaisesRegex(ValueError, 'reserved'):
            app.create_user({
                'username': 'legacy-shadow',
                'display_name': 'Reserved Name',
                'role': 'admin',
                'password': 'ReservedPassword123!',
            }, 'legacy-admin')

        clients = {}
        for username, _, role, password in accounts:
            client = self.session()
            status, login, _ = self.request(
                client, '/api/login', {'username': username, 'password': password})
            self.assertEqual(status, 200, login)
            self.assertEqual(login['role'], role)
            clients[role] = (client, login['csrf'])

        checkin, checkin_csrf = clients['checkin_staff']
        status, _, _ = self.request(checkin, '/api/scan/checkin',
                                    {'employee_id': 'E100', 'wristband_reason':'unreadable'}, checkin_csrf)
        self.assertEqual(status, 200)
        status, _, _ = self.request(checkin, '/api/draw/config')
        self.assertEqual(status, 403)

        verifier, verifier_csrf = clients['verification_staff']
        status, _, _ = self.request(verifier, '/api/scan/checkin',
                                    {'employee_id': 'E100'}, verifier_csrf)
        self.assertEqual(status, 403)
        status, result, _ = self.request(verifier, '/api/scan/verify', {
            'employee_id': 'E100',
            'result': 'identity_mismatch',
            'reason': 'Badge photograph does not match',
        }, verifier_csrf)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['verification']['result'], 'identity_mismatch')

        operator, operator_csrf = clients['lucky_draw_operator']
        status, _, _ = self.request(operator, '/api/draw/config')
        self.assertEqual(status, 200)
        status, _, _ = self.request(operator, '/api/settings', {}, operator_csrf)
        self.assertEqual(status, 403)
        status, _, _ = self.request(operator, '/api/users')
        self.assertEqual(status, 403)

        app.update_user({'username': 'door.one', 'active': False}, 'legacy-admin')
        status, _, _ = self.request(checkin, '/api/stats')
        self.assertEqual(status, 401)
        admin = self.session()
        self.request(admin, '/api/login', {'password': 'verystrongadminpass'})
        status, activity, _ = self.request(admin, '/api/admin/activity')
        self.assertEqual(status, 200)
        self.assertNotIn('door.one', {row['username'] for row in activity['items']})

    def test_numeric_company_badge_qr_uses_employee_lookup_fallback(self):
        app.import_employees([
            ('1234567', 'Badge Test', 'Operations'),
            ('505505', 'Leading Zero Badge', 'Operations'),
        ], 'test-admin')
        staff = self.session()
        status, login, _ = self.request(
            staff, '/api/login', {'password': 'safestaffpass'})
        self.assertEqual(status, 200)
        status, result, _ = self.request(
            staff, '/api/lookup', {'code': '1234567'}, login['csrf'])
        self.assertEqual(status, 200, result)
        self.assertEqual(result['person']['employee_id'], '1234567')
        self.assertEqual(result['lookup'], {'code': '1234567'})
        self.assertEqual(result['lookup_source'], 'id_badge')
        status, leading_zero, _ = self.request(
            staff, '/api/lookup', {'code': '0505505'}, login['csrf'])
        self.assertEqual(status, 200, leading_zero)
        self.assertEqual(leading_zero['person']['employee_id'], '0505505')
        self.assertEqual(leading_zero['lookup'], {'code': '0505505'})
        with app.db_connection() as db:
            employee = db.execute(
                'SELECT checked_in_at FROM employees WHERE employee_id=?',
                ('1234567',)).fetchone()
        self.assertIsNone(employee['checked_in_at'])

    def test_projector_state_is_authorized_recoverable_and_privacy_aware(self):
        with app.db_connection() as db:
            app.set_projector_state(db, 'waiting', 'test')
        anonymous = self.session()
        status, _, _ = self.request(anonymous, '/api/projector/state')
        self.assertEqual(status, 401)

        page = anonymous.open(self.base + '/projector')
        markup = page.read().decode('utf-8')
        self.assertIn('id="presentation"', markup)
        self.assertNotIn('app-header', markup)
        self.assertIn('id="winnerSoundPlayer"', markup)
        self.assertIn('UUjJD9Px5vI', markup)
        self.assertIn('frame-src https://www.youtube-nocookie.com',
                      page.headers.get('Content-Security-Policy', ''))

        admin = self.session()
        status, admin_login, _ = self.request(
            admin, '/api/login', {'password': 'verystrongadminpass'})
        self.assertEqual(status, 200)
        admin_csrf = admin_login['csrf']
        staff = self.session()
        status, staff_login, _ = self.request(
            staff, '/api/login', {'password': 'safestaffpass'})
        self.assertEqual(status, 200)
        staff_csrf = staff_login['csrf']

        status, prepared, _ = self.request(staff, '/api/projector/state', {
            'phase': 'preparing', 'prize': 'Projector Test Prize',
            'round_name': 'Projector Test Round'}, staff_csrf)
        self.assertEqual(status, 200, prepared)
        self.assertEqual(prepared['phase'], 'preparing')

        app.import_employees([('P100', 'Projector Winner', 'Events')], 'test-admin')
        status, result, _ = self.request(
            staff, '/api/scan/checkin', {'employee_id': 'P100', 'wristband_reason':'unreadable'}, staff_csrf)
        self.assertEqual(status, 200, result)
        status, result, _ = self.request(
            staff, '/api/draw/participants', {'employee_id': 'P100'}, staff_csrf)
        self.assertEqual(status, 200, result)
        status, draw, _ = self.request(staff, '/api/draw', {
            'prize': 'Projector Test Prize', 'round_name': 'Projector Test Round'}, staff_csrf)
        self.assertEqual(status, 200, draw)
        self.assertEqual(draw['winner']['employee_id'], 'P100')

        status, spinning, _ = self.request(staff, '/api/projector/state')
        self.assertEqual(status, 200, spinning)
        self.assertIn(spinning['phase'], ('spinning', 'winner_selected'))
        self.assertIsNone(spinning['winner'])
        status, busy, _ = self.request(staff, '/api/projector/state', {'phase':'preparing','prize':'Another'}, staff_csrf)
        self.assertEqual((status, busy['code']), (409, 'draw_in_progress'))

        with app.db_connection() as db:
            db.execute("UPDATE lucky_draw_presentation SET reveal_at='2000-01-01T00:00:00+00:00'")
        status, announced, _ = self.request(staff, '/api/projector/state')
        self.assertEqual(status, 200, announced)
        self.assertEqual(announced['phase'], 'winner_announced')
        self.assertEqual(announced['winner']['employee_id'], 'P100')

        status, settings, _ = self.request(admin, '/api/settings', {
            'projector_title': 'Annual Celebration',
            'projector_theme': 'indigo',
            'projector_show_employee_id': False,
        }, admin_csrf)
        self.assertEqual(status, 200, settings)
        status, private_state, _ = self.request(staff, '/api/projector/state')
        self.assertEqual(status, 200, private_state)
        self.assertEqual(private_state['title'], 'Annual Celebration')
        self.assertEqual(private_state['theme'], 'indigo')
        self.assertNotIn('employee_id', private_state['winner'])

        status, ready, _ = self.request(
            staff, '/api/projector/state', {'phase': 'ready_for_next'}, staff_csrf)
        self.assertEqual(status, 200, ready)
        self.assertEqual(ready['phase'], 'ready_for_next')
        self.assertIsNone(ready['winner'])

        status, cleared, _ = self.request(
            staff, '/api/projector/prize-image', {'clear': True}, staff_csrf)
        self.assertEqual(status, 200, cleared)
        self.assertIsNone(cleared['state']['prize_image_url'])


if __name__ == '__main__':
    unittest.main()
