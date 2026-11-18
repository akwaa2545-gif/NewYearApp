"""Scan lookup stays current while using one indexed business query normally."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import app
from cloudflare.handler_adapter import dispatch


class _LookupApp:
    class PartyHandler(app.PartyHandler):
        def get_user(self):
            return {'role': 'staff', 'username': 'lookup-test', 'csrf': 'lookup-csrf'}

    def __getattr__(self, name):
        return getattr(app, name)


class LookupFastPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'lookup.sqlite3'
        self.path_patch = patch.object(app, 'DB_PATH', self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        app.init_db()
        app.import_employees([('1000001', 'Test One', 'IT'),
                              ('1000002', 'Test Two', 'Operations')], 'test')
        with app.db_connection() as db:
            self.people = {row['employee_id']: dict(row)
                           for row in db.execute('SELECT * FROM employees')}
        self.statements = []
        self.original_connection = app.db_connection

        @contextmanager
        def traced_connection():
            with self.original_connection() as db:
                db.set_trace_callback(self.statements.append)
                yield db

        self.connection_patch = patch.object(app, 'db_connection', traced_connection)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def lookup(self, data):
        self.statements.clear()
        with patch.dict(app.os.environ, {'PARTY_PUBLIC_ORIGIN': 'https://lookup.test'}):
            result = dispatch(_LookupApp(), 'POST', '/api/lookup',
                              [('Content-Type', 'application/json'),
                               ('X-CSRF-Token', 'lookup-csrf'),
                               ('Origin', 'https://lookup.test')],
                              json.dumps(data).encode(), config={'secret': '01' * 32})
        return result['status'], json.loads(result['body'])

    def business_selects(self):
        return [sql for sql in self.statements if sql.lstrip().upper().startswith('SELECT')
                and re.search(r'\b(?:FROM|JOIN)\s+(?:employees|wristbands|checkin_wristband_exceptions)\b',
                              sql, re.IGNORECASE)]

    def exception(self, employee_id='1000001', reason='lost'):
        with self.original_connection() as db:
            db.execute('''INSERT INTO checkin_wristband_exceptions
                (employee_id,reason,detail,recorded_by,recorded_at) VALUES (?,?,?,?,?)''',
                       (self.people[employee_id]['id'], reason, '', 'test', app.now()))

    def test_exact_numeric_badge_is_one_query_and_preserves_response_shape(self):
        self.exception()
        status, result = self.lookup({'code': '1000001'})
        self.assertEqual(status, 200)
        self.assertEqual(result['lookup'], {'code': '1000001'})
        self.assertEqual(result['lookup_source'], 'id_badge')
        self.assertEqual(result['person']['checkin_wristband_reason'], 'lost')
        self.assertEqual(set(result['person']), {'id', 'employee_id', 'name', 'department',
                         'checked_in_at', 'verified_at', 'extra', 'checkin_wristband_reason'})
        self.assertEqual(set(result['wristband']), {'id', 'status'})
        self.assertEqual(len(self.business_selects()), 1)

    def test_manual_exact_id_and_active_wristband_status_are_one_query(self):
        with self.original_connection() as db:
            db.execute("UPDATE wristbands SET status='issued' WHERE employee_id=?",
                       (self.people['1000001']['id'],))
        status, result = self.lookup({'employee_id': '1000001'})
        self.assertEqual((status, result['lookup_source']), (200, 'employee_id'))
        self.assertEqual(result['wristband']['status'], 'issued')
        self.assertEqual(result['lookup'], {'employee_id': '1000001'})
        self.assertIsNone(result['person']['checkin_wristband_reason'])
        self.assertEqual(len(self.business_selects()), 1)

    def test_active_token_includes_exception_in_one_query(self):
        self.exception(reason='unreadable')
        status, result = self.lookup({'code': app.TOKEN_PREFIX + self.people['1000001']['qr_token']})
        self.assertEqual((status, result['lookup_source']), (200, 'wristband'))
        self.assertEqual(result['person']['checkin_wristband_reason'], 'unreadable')
        self.assertEqual(result['person']['wristband_id'], result['wristband']['id'])
        self.assertEqual(len(self.business_selects()), 1)

    def test_employee_without_active_wristband_is_still_found(self):
        with self.original_connection() as db:
            db.execute("UPDATE wristbands SET status='revoked' WHERE employee_id=?",
                       (self.people['1000001']['id'],))
        status, result = self.lookup({'employee_id': '1000001'})
        self.assertEqual(status, 200)
        self.assertIsNone(result['wristband'])
        self.assertEqual(result['person']['employee_id'], '1000001')
        self.assertEqual(len(self.business_selects()), 1)

    def test_inactive_and_unknown_tokens_keep_existing_errors(self):
        with self.original_connection() as db:
            db.execute("UPDATE wristbands SET status='revoked' WHERE employee_id=?",
                       (self.people['1000001']['id'],))
        status, result = self.lookup({'code': self.people['1000001']['qr_token']})
        self.assertEqual((status, result['code']), (409, 'revoked_wristband'))
        self.assertEqual(result['person']['wristband_status'], 'revoked')
        status, result = self.lookup({'code': 'unrecognized-token-value-for-test'})
        self.assertEqual((status, result['code']), (404, 'unknown_token'))

    def test_unique_zero_trimmed_fallback_returns_current_scan_details(self):
        self.exception()
        with self.original_connection() as db:
            db.execute('UPDATE employees SET employee_id=? WHERE id=?',
                       ('123', self.people['1000001']['id']))
        status, result = self.lookup({'employee_id': '00123'})
        self.assertEqual(status, 200)
        self.assertEqual(result['person']['employee_id'], '123')
        self.assertEqual(result['person']['checkin_wristband_reason'], 'lost')
        self.assertIsNotNone(result['wristband'])
        self.assertEqual(len(self.business_selects()), 2)

    def test_exact_match_wins_and_ambiguous_normalized_match_is_rejected(self):
        with self.original_connection() as db:
            db.execute('UPDATE employees SET employee_id=? WHERE id=?',
                       ('123', self.people['1000001']['id']))
            db.execute('UPDATE employees SET employee_id=? WHERE id=?',
                       ('0123', self.people['1000002']['id']))
        status, result = self.lookup({'employee_id': '0123'})
        self.assertEqual(status, 200)
        self.assertEqual(result['person']['id'], self.people['1000002']['id'])
        status, result = self.lookup({'employee_id': '00123'})
        self.assertEqual((status, result['code']), (404, 'employee_not_found'))

    def test_another_connection_checkin_and_exception_are_visible_without_cache(self):
        status, first = self.lookup({'employee_id': '1000001'})
        self.assertEqual(status, 200)
        self.assertIsNone(first['person']['checked_in_at'])

        def update_from_other_phone():
            with self.original_connection() as db:
                db.execute('UPDATE employees SET checked_in_at=? WHERE id=?',
                           ('2026-11-18T02:00:00+00:00', self.people['1000001']['id']))
                db.execute('''INSERT INTO checkin_wristband_exceptions
                    (employee_id,reason,detail,recorded_by,recorded_at) VALUES (?,?,?,?,?)''',
                           (self.people['1000001']['id'], 'lost', '', 'other-phone', app.now()))

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(update_from_other_phone).result()
        status, latest = self.lookup({'employee_id': '1000001'})
        self.assertEqual(status, 200)
        self.assertEqual(latest['person']['checked_in_at'], '2026-11-18T02:00:00+00:00')
        self.assertEqual(latest['person']['checkin_wristband_reason'], 'lost')
        self.assertEqual(len(self.business_selects()), 1)

    def test_legacy_token_compatibility_still_creates_and_returns_wristband(self):
        self.exception()
        employee = self.people['1000001']
        with self.original_connection() as db:
            db.execute('DELETE FROM wristband_distribution_history WHERE employee_id=?', (employee['id'],))
            db.execute('DELETE FROM wristbands WHERE employee_id=?', (employee['id'],))
        status, result = self.lookup({'code': employee['qr_token']})
        self.assertEqual(status, 200)
        self.assertEqual(result['wristband']['status'], 'issued')
        self.assertEqual(result['person']['checkin_wristband_reason'], 'lost')
        with self.original_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wristbands WHERE employee_id=?',
                                       (employee['id'],)).fetchone()[0], 1)

    def test_lookup_does_not_wait_for_employee_photo(self):
        with patch.object(app, 'fetch_employee_photo', side_effect=AssertionError('Photo blocked lookup')):
            status, _ = self.lookup({'employee_id': '1000001'})
        self.assertEqual(status, 200)

    def test_default_employee_helper_keeps_its_original_columns(self):
        with self.original_connection() as db:
            row = app.find_employee_by_external_id(db, '1000001')
        self.assertEqual(set(row.keys()), set(self.people['1000001']))


@unittest.skipUnless(importlib.util.find_spec('sqlglot') and importlib.util.find_spec('pyodbc'),
                     'The optional native SQL driver dependencies are not installed.')
class NativeLookupCompilationTests(unittest.TestCase):
    def test_native_driver_compiles_one_parameterized_join_with_named_columns(self):
        from eventdesk_storage import SqlServerConnection

        columns = ('id', 'employee_id', 'name', 'department', 'qr_token', 'checked_in_at',
                   'verified_at', 'extra_json', 'lookup_wristband_id',
                   'lookup_wristband_status', 'checkin_wristband_reason')
        values = (7, '1000001', 'Test One', 'IT', 'valid-token-value-for-test', None,
                  None, '{}', 11, 'issued', 'lost')
        cursor = Mock()
        cursor.description = [(name,) for name in columns]
        cursor.fetchall.return_value = [values]
        raw = Mock(autocommit=False)
        raw.cursor.return_value = cursor
        db = SqlServerConnection(raw_connection=raw, schema='dbo', managed=True)
        row = app.find_employee_by_external_id(db, '1000001', scan_details=True)
        self.assertEqual(row['lookup_wristband_id'], 11)
        self.assertEqual(row['checkin_wristband_reason'], 'lost')
        self.assertEqual(tuple(app.employee_data(row)['extra']), ())
        self.assertEqual(cursor.execute.call_count, 1)
        sql, parameters = cursor.execute.call_args.args
        self.assertIn('LEFT JOIN [dbo].[wristbands]', sql)
        self.assertIn('LEFT JOIN [dbo].[checkin_wristband_exceptions]', sql)
        self.assertEqual(parameters, ('1000001',))
        self.assertNotIn('1000001', sql)


if __name__ == '__main__':
    unittest.main()
