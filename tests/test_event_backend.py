"""Exercise event requests, durable receipts, and the private loopback boundary."""
import base64
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from photo_api.event_backend import EventEngine, EventHTTPServer, local_photo_fetch
from photo_api.event_store import SQLiteStore, SqlServerStore
from photo_api import event_protocol as protocol


class EventBackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'event.sqlite3'
        self.config = {
            'event_name': 'Backend Test', 'secret': secrets.token_hex(32),
            'admin_password': {'salt': secrets.token_hex(16), 'hash': secrets.token_hex(32)},
            'staff_password': {'salt': secrets.token_hex(16), 'hash': secrets.token_hex(32)},
        }
        self.environment = patch.dict(os.environ, {'PARTY_OPEN_STAFF': '1',
                                                   'PARTY_COOKIE_SECURE': '1',
                                                   'PARTY_PUBLIC_ORIGIN': 'https://eventdesk.test'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with patch.object(app, 'DB_PATH', self.path):
            app.init_db()
            app.import_employees([('0000123', 'Test Employee', 'Test')], 'test')
        self.store = SQLiteStore(self.path)
        self.engine = EventEngine(self.store, app, self.config, 'https://eventdesk.test')

    def command(self, path='/api/staff-session', method='POST', data=None, headers=(), **fields):
        return {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'method': method,
                'path': path, 'headers': [['Content-Type', 'application/json'],
                                         ['Origin', 'https://eventdesk.test'], *map(list, headers)],
                'body_base64': '' if method == 'GET' else base64.b64encode(json.dumps(data or {}).encode()).decode(),
                'client_ip': '127.0.0.1', 'after_revision': 0, **fields}

    def staff(self):
        result = self.engine.handle(self.command())
        cookie = next(value.split(';')[0] for name, value in result['headers'] if name.lower() == 'set-cookie')
        csrf = json.loads(base64.b64decode(result['body_base64']))['csrf']
        return cookie, csrf

    def test_actual_handler_retains_security_and_duplicate_operation_receipt(self):
        value = self.command()
        first = self.engine.handle(value)
        second = self.engine.handle({**value, 'after_revision': first['revision']})
        self.assertEqual(first['status'], 200)
        self.assertEqual(first['body_base64'], second['body_base64'])
        self.assertEqual(first['headers'], second['headers'])
        self.assertEqual(first['revision'], second['revision'])
        self.assertTrue(second['receipt_found'])
        self.assertEqual(second['changes'], [])
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 1)

    def test_operation_id_reuse_with_different_body_is_conflict(self):
        value = self.command()
        self.engine.handle(value)
        conflict = self.engine.handle({**value, 'body_base64': base64.b64encode(b'{"x":1}').decode()})
        self.assertEqual(conflict['status'], 409)
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 1)

    def test_sql_only_get_and_post_do_not_read_replication(self):
        from photo_api.event_store import EventTransaction
        with patch.object(EventTransaction, 'replication', side_effect=AssertionError('Replica unavailable')) as replica:
            session = self.engine.handle(self.command(include_replica=False))
            cookie = next(value.split(';')[0] for name, value in session['headers'] if name.lower() == 'set-cookie')
            csrf = json.loads(base64.b64decode(session['body_base64']))['csrf']
            checked_in = self.engine.handle(self.command('/api/scan/checkin', data={'code': '0000123'},
                headers=[('Cookie', cookie), ('X-CSRF-Token', csrf)], include_replica=False))
            state = self.engine.handle(self.command('/api/me', method='GET',
                headers=[('Cookie', cookie)], include_replica=False))
            for result in (session, checked_in, state):
                self.assertEqual(result['status'], 200)
                self.assertEqual(result['changes'], [])
                self.assertFalse(result['replica_complete'])
                self.assertFalse(result['snapshot_required'])
            self.assertGreater(checked_in['revision'], session['revision'])
            replica.assert_not_called()
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)

    def test_replica_mode_switch_after_lost_ack_reuses_original_receipt(self):
        value = self.command(include_replica=False)
        first = self.engine.handle(value)
        retry = self.engine.handle({**value, 'include_replica': True})
        default_retry = self.engine.handle({key: item for key, item in value.items() if key != 'include_replica'})
        for result in (retry, default_retry):
            self.assertTrue(result['receipt_found'])
            self.assertEqual(result['body_base64'], first['body_base64'])
            self.assertEqual(result['headers'], first['headers'])
            self.assertEqual(result['revision'], first['revision'])
            self.assertTrue(result['replica_complete'])
            self.assertTrue(result['changes'])
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 1)
            self.assertEqual(tx.receipt(value['operation_id'])['response']['body_base64'], first['body_base64'])

    def test_audit_time_and_ip_changes_do_not_repeat_a_committed_checkin(self):
        cookie, csrf = self.staff()
        value = self.command('/api/scan/checkin', data={'code': '0000123'},
                             headers=[('Cookie', cookie), ('X-CSRF-Token', csrf)],
                             recorded_at=datetime.now(timezone.utc).isoformat())
        first = self.engine.handle(value)
        second = self.engine.handle({**value, 'client_ip': '192.0.2.1',
                                     'recorded_at': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()})
        self.assertEqual(first['status'], 200)
        self.assertEqual(second['body_base64'], first['body_base64'])
        self.assertEqual(second['revision'], first['revision'])

    def replay_command(self, *, named=False):
        recorded = datetime.now(timezone.utc) - timedelta(days=1)
        claims = {'role': 'checkin_staff' if named else 'staff',
                  'username': 'offline-user' if named else 'legacy-staff',
                  'display_name': 'Offline User', 'account_type': 'named' if named else 'legacy',
                  'exp': int(recorded.timestamp()) + 3600,
                  'csrf': secrets.token_urlsafe(24), 'sid': secrets.token_urlsafe(24)}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        signature = hmac.new(bytes.fromhex(self.config['secret']), payload.encode(), hashlib.sha256).hexdigest()
        cookie = 'party_session=' + payload + '.' + signature
        command = self.command('/api/scan/checkin', data={'code': '0000123'},
                               headers=[('Cookie', cookie), ('X-CSRF-Token', claims['csrf'])],
                               recorded_at=recorded.isoformat(), replay=True, queue_proof='0' * 64)
        command['queue_proof'] = protocol.offline_proof(self.config['secret'], protocol.parse_command(command))
        return command

    def test_signed_offline_checkin_replays_expired_session_and_original_time_once(self):
        command = self.replay_command()
        response = self.engine.handle(command)
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.engine.handle(command)['body_base64'], response['body_base64'])
        with self.store.transaction() as tx:
            row = tx.db.execute('SELECT checked_in_at FROM event_checkins').fetchone()
            self.assertEqual(row[0], command['recorded_at'])

    def test_invalid_proof_and_revoked_named_account_do_not_mutate_data(self):
        command = self.replay_command()
        with self.assertRaises(protocol.ProtocolError):
            self.engine.handle({**command, 'queue_proof': 'f' * 64})
        with self.assertRaises(protocol.ProtocolError):
            self.engine.handle(self.replay_command(named=True))
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)

    def test_named_offline_account_must_still_be_active_with_checkin_permission(self):
        with self.store.transaction() as tx, self.engine._bind(tx):
            app.create_user({'username': 'offline-user', 'display_name': 'Offline User',
                             'role': 'checkin_staff', 'password': secrets.token_urlsafe(18)}, 'test.admin')
            tx.finish(None, None, {})
        command = self.replay_command(named=True)
        self.assertEqual(self.engine.handle(command)['status'], 200)
        with self.store.transaction() as tx:
            tx.db.execute("UPDATE users SET active=0 WHERE username='offline-user'")
            tx.finish(None, None, {})
        with self.assertRaises(protocol.ProtocolError):
            self.engine.handle(self.replay_command(named=True))

    def test_replication_gap_requires_snapshot_instead_of_claiming_complete_copy(self):
        self.engine.handle(self.command())
        with self.store.transaction() as tx:
            tx._query('DELETE FROM ' + tx._name('EventChangeJournal'))
        response = self.engine.handle(self.command('/api/me', method='GET'))
        self.assertFalse(response['replica_complete'])
        self.assertTrue(response['snapshot_required'])

    def test_large_atomic_update_rolls_back_before_receipt_and_replication_commit(self):
        value = self.command()
        with patch('photo_api.event_store.MAX_RESPONSE_BYTES', 100), self.assertRaises(protocol.ProtocolError):
            self.engine.handle(value)
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 0)
            self.assertIsNone(tx.receipt(value['operation_id']))

    def test_receipt_and_business_rows_commit_atomically_on_journal_failure(self):
        value = self.command()
        from photo_api.event_store import EventTransaction
        original = EventTransaction._query
        def fail_journal(tx, sql, values=()):
            if sql.startswith('INSERT INTO ') and 'EventChangeJournal' in sql:
                raise RuntimeError('simulated journal failure')
            return original(tx, sql, values)
        with patch.object(EventTransaction, '_query', fail_journal), self.assertRaises(RuntimeError):
            self.engine.handle(value)
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 0)
            self.assertIsNone(tx.receipt(value['operation_id']))

    def test_concurrent_duplicate_requests_create_one_session_and_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        value = self.command()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.engine.handle, [value] * 4))
        self.assertEqual(len({entry['body_base64'] for entry in results}), 1)
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 1)

    def test_cancelling_uncertain_command_fences_late_original(self):
        value = self.command()
        cancelled = self.engine.handle({**value, 'receipt_only': True, 'cancel_if_missing': True})
        late = self.engine.handle(value)
        self.assertTrue(cancelled['cancelled'])
        self.assertEqual(late['status'], 409)
        self.assertTrue(late['cancelled'])
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM staff_sessions').fetchone()[0], 0)

    def test_receipt_lookup_after_commit_returns_original_result_without_rerunning(self):
        value = self.command()
        first = self.engine.handle(value)
        found = self.engine.handle({**value, 'receipt_only': True, 'cancel_if_missing': True})
        self.assertTrue(found['receipt_found'])
        self.assertFalse(found['cancelled'])
        self.assertEqual(found['body_base64'], first['body_base64'])

    def test_application_error_rolls_back_partial_writes(self):
        def broken_dispatch(*_args, **_kwargs):
            with app.db_connection() as db:
                db.execute("INSERT INTO event_log(action,actor,occurred_at) VALUES ('partial','test','now')")
                db.commit()
            return {'status': 400, 'headers': [], 'body': b'{"error":"rejected"}'}
        with patch('photo_api.event_backend.dispatch', broken_dispatch):
            first = self.engine.handle(self.command())
        self.assertEqual(first['status'], 400)
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute("SELECT COUNT(*) FROM event_log WHERE action='partial'").fetchone()[0], 0)

    def test_checkin_and_finite_projector_event_use_existing_business_handlers(self):
        cookie, csrf = self.staff()
        result = self.engine.handle(self.command('/api/scan/checkin', data={'code': '0000123'},
                                    headers=[('Cookie', cookie), ('X-CSRF-Token', csrf)]))
        self.assertEqual(result['status'], 200)
        state = self.engine.handle(self.command('/api/projector/events', method='GET', headers=[('Cookie', cookie)]))
        self.assertEqual(state['status'], 200)
        self.assertIn(b'event: draw-state', base64.b64decode(state['body_base64']))
        self.assertTrue(any(batch['changes'] for batch in result['changes']))

    def test_photo_uses_sql_employee_authorization_and_injected_local_fetch(self):
        from unittest.mock import Mock
        photo = b'\xff\xd8\xfftest\xff\xd9'
        fetch = Mock(return_value=photo)
        self.engine.photo_fetch = fetch
        denied = self.engine.handle(self.command('/api/employee-photo/0000123', method='GET'))
        self.assertEqual(denied['status'], 401)
        fetch.assert_not_called()
        cookie, _csrf = self.staff()
        allowed = self.engine.handle(self.command('/api/employee-photo/0000123', method='GET',
                                                  headers=[('Cookie', cookie)]))
        self.assertEqual(allowed['status'], 200)
        self.assertEqual(base64.b64decode(allowed['body_base64']), photo)
        fetch.assert_called_once_with('0000123')
        missing = self.engine.handle(self.command('/api/employee-photo/9999999', method='GET',
                                                  headers=[('Cookie', cookie)]))
        self.assertEqual(missing['status'], 404)
        self.assertEqual(fetch.call_count, 1)

    def test_local_photo_client_is_bounded_fixed_loopback_and_never_redirects(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        photo = b'\xff\xd8\xfftest\xff\xd9'
        response = Mock(status=200, headers={'Content-Type': 'image/jpeg', 'Content-Length': str(len(photo))})
        response.read1 = io.BytesIO(photo).read1
        connection = Mock(sock=Mock())
        connection.getresponse.return_value = response
        factory = Mock(return_value=connection)
        token = secrets.token_urlsafe(36)
        fetch = local_photo_fetch(app, token, connection_factory=factory)
        self.assertEqual(fetch('0000123'), photo)
        factory.assert_called_once_with('127.0.0.1', 8765, timeout=8)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('GET', '/v1/employees/0000123/photo'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + token)
        connection.close.assert_called_once()
        response.close.assert_called_once()
        with self.assertRaises(app.EmployeePhotoError):
            fetch('../bad')
        for status in (302, 404, 500):
            response.status = status
            with self.subTest(status=status), self.assertRaises(app.EmployeePhotoError) as caught:
                fetch('0000123')
            self.assertEqual(caught.exception.status, 404 if status == 404 else 503)

    def test_waiting_photo_does_not_hold_sql_or_application_lock_for_checkin(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        def fetch(_employee_id):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Photo test deadline expired.')
            return b'\xff\xd8\xfftest\xff\xd9'
        self.engine.photo_fetch = fetch
        cookie, csrf = self.staff()
        with ThreadPoolExecutor(max_workers=2) as pool:
            photo = pool.submit(self.engine.handle, self.command('/api/employee-photo/0000123',
                                method='GET', headers=[('Cookie', cookie)]))
            try:
                self.assertTrue(entered.wait(2))
                checkin = pool.submit(self.engine.handle, self.command('/api/scan/checkin',
                                    data={'code': '0000123'},
                                    headers=[('Cookie', cookie), ('X-CSRF-Token', csrf)]))
                self.assertEqual(checkin.result(timeout=2)['status'], 200)
            finally:
                release.set()
            self.assertEqual(photo.result(timeout=2)['status'], 200)

    def test_snapshot_discovery_pages_and_stale_revision_rejection(self):
        info = self.engine.snapshot({'schema_version': 1})
        self.assertIn('employees', [table['name'] for table in info['tables']])
        page = self.engine.snapshot({'schema_version': 1, 'table': 'employees', 'offset': 0,
                                     'limit': 100, 'revision': info['revision']})
        self.assertEqual(page['rows'][0]['employee_id'], '0000123')
        self.assertIsNone(page['next_offset'])
        self.engine.handle(self.command())
        with self.assertRaises(ValueError):
            self.engine.snapshot({'schema_version': 1, 'table': 'employees', 'revision': info['revision']})

    def test_snapshot_thousand_row_pages_preserve_all_employee_rows_and_cursor(self):
        with self.store.transaction() as tx:
            tx.db.executemany('INSERT INTO employees(employee_id,name,department,qr_token) VALUES(?,?,?,?)',
                [(f'TEST_{number:04}', 'Snapshot test', 'Test', f'snapshot-token-{number}')
                 for number in range(1000)])
        info = self.engine.snapshot({'schema_version': 1})
        request = {'schema_version': 1, 'table': 'employees', 'offset': 0,
                   'limit': 1000, 'revision': info['revision']}
        first = self.engine.snapshot(request)
        self.assertEqual(len(first['rows']), 1000)
        self.assertEqual(first['next_offset'], 1000)
        second = self.engine.snapshot({**request, 'offset': first['next_offset']})
        self.assertEqual(len(second['rows']), 1)
        self.assertIsNone(second['next_offset'])
        ids = [row['id'] for row in first['rows'] + second['rows']]
        self.assertEqual(len(set(ids)), 1001)
        with self.assertRaises(protocol.ProtocolError):
            self.engine.snapshot({**request, 'limit': 1001})

    def test_private_http_requires_bearer_and_fixed_routes_before_engine_access(self):
        token = secrets.token_urlsafe(36)
        server = EventHTTPServer(('127.0.0.1', 0), self.engine, token)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        data = json.dumps(self.command()).encode()
        def request(path, auth=None, payload=data):
            headers = {'Content-Type': 'application/json'}
            if auth is not None:
                headers['Authorization'] = auth
            return urllib.request.urlopen(urllib.request.Request(url + path, data=payload, headers=headers), timeout=3)
        for path, auth, expected in (('/v1/event/request', None, 401),
                                      ('/v1/event/request', 'Bearer wrong', 401),
                                      ('/v1/event/unknown', 'Bearer ' + token, 404)):
            with self.subTest(path=path, auth=auth), self.assertRaises(urllib.error.HTTPError) as caught:
                request(path, auth)
            self.assertEqual(caught.exception.code, expected)
            caught.exception.close()
        with request('/v1/event/request', 'Bearer ' + token) as result:
            self.assertEqual(result.status, 200)
            self.assertEqual(json.load(result)['status'], 200)

        for content_type, payload, expected in (
                ('text/plain', data, 415), ('application/json', b'{"x":NaN}', 400),
                ('application/json', b'{}', 400)):
            headers = {'Content-Type': content_type, 'Authorization': 'Bearer ' + token}
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(url + '/v1/event/request',
                                                              data=payload, headers=headers), timeout=3)
            self.assertEqual(caught.exception.code, expected)
            caught.exception.close()
        with request('/v1/event/snapshot', 'Bearer ' + token,
                     json.dumps({'schema_version': 1}).encode()) as result:
            self.assertIn('tables', json.load(result))
        with urllib.request.urlopen(urllib.request.Request(url + '/healthz',
                headers={'Authorization': 'Bearer ' + token}), timeout=3) as result:
            self.assertEqual(json.load(result)['status'], 'ok')

    def test_non_loopback_listener_is_rejected(self):
        with self.assertRaises(ValueError):
            EventHTTPServer(('0.0.0.0', 0), self.engine, secrets.token_urlsafe(36))

    def test_private_shutdown_requires_authentication_and_exact_empty_body(self):
        from unittest.mock import Mock
        token = secrets.token_urlsafe(36)
        server = EventHTTPServer(('127.0.0.1', 0), self.engine, token)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        stopped = threading.Event()
        with patch.object(server, 'shutdown', side_effect=stopped.set) as shutdown:
            for path, auth, body, expected in (
                    ('/__service/shutdown', None, b'', 401),
                    ('/__service/shutdown', 'Bearer wrong', b'', 401),
                    ('/__service/shutdown?x=1', 'Bearer ' + token, b'', 404),
                    ('/__service/shutdown', 'Bearer ' + token, b'{}', 400)):
                headers = {'Content-Type': 'application/json'}
                if auth is not None:
                    headers['Authorization'] = auth
                request = urllib.request.Request(url + path, data=body, headers=headers)
                with self.subTest(path=path, expected=expected), self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code, expected)
                caught.exception.close()
                shutdown.assert_not_called()
            request = urllib.request.Request(url + '/__service/shutdown', data=b'',
                                              headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response), {'status': 'stopping'})
            self.assertTrue(stopped.wait(1))
            shutdown.assert_called_once()
        self.assertFalse(server.daemon_threads)
        self.assertTrue(server.block_on_close)

    def test_shutdown_stops_accepting_and_waits_for_an_existing_request_to_finish(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import Mock
        token = secrets.token_urlsafe(36)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def slow_request(_payload):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Drain test deadline expired.')
            return {'status': 200}
        engine = Mock()
        engine.handle.side_effect = slow_request
        server = EventHTTPServer(('127.0.0.1', 0), engine, token)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        def request():
            with urllib.request.urlopen(urllib.request.Request(url + '/v1/event/request',
                    data=protocol.dumps(self.command()), headers=headers), timeout=3) as response:
                return response.status
        def close_server():
            server.server_close()
            closed.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(request)
            try:
                self.assertTrue(entered.wait(1))
                with urllib.request.urlopen(urllib.request.Request(url + '/__service/shutdown',
                        data=b'', headers=headers), timeout=3) as response:
                    self.assertEqual(response.status, 202)
                serving.join(timeout=2)
                self.assertFalse(serving.is_alive())
                closer = threading.Thread(target=close_server, daemon=True)
                closer.start()
                self.assertFalse(closed.wait(0.05))
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2), 200)
            self.assertTrue(closed.wait(2))

    def test_startup_reads_prepared_config_only_from_stdin_and_closes_server(self):
        from types import SimpleNamespace
        from photo_api import event_backend
        settings = {
            'ConnectionString': 'Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=Employee_db;',
            'ApiToken': secrets.token_urlsafe(36), 'AppConfig': self.config,
            'PublicOrigin': 'https://eventdesk.test',
        }
        with patch.object(sys, 'argv', ['event_backend', '--config-stdin']), \
                patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(protocol.dumps(settings)))), \
                patch.object(event_backend, 'SqlServerStore') as store, \
                patch.object(event_backend, 'EventHTTPServer') as server:
            event_backend.main()
        store.assert_called_once_with(settings['ConnectionString'], schema='eventdesk')
        store.return_value.initialize.assert_called_once()
        self.assertEqual(server.call_args.args[0], ('127.0.0.1', 8766))
        self.assertEqual(server.call_args.args[2], settings['ApiToken'])
        server.return_value.serve_forever.assert_called_once()
        server.return_value.server_close.assert_called_once()

    def test_startup_accepts_private_dbo_schema_option(self):
        from types import SimpleNamespace
        from photo_api import event_backend
        settings = {
            'ConnectionString': 'Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=Employee_db;',
            'ApiToken': secrets.token_urlsafe(36), 'AppConfig': self.config,
            'PublicOrigin': 'https://eventdesk.test', 'Schema': 'dbo',
        }
        with patch.object(sys, 'argv', ['event_backend', '--config-stdin']), \
                patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(protocol.dumps(settings)))), \
                patch.object(event_backend, 'SqlServerStore') as store, \
                patch.object(event_backend, 'EventHTTPServer'):
            event_backend.main()
        store.assert_called_once_with(settings['ConnectionString'], schema='dbo')

    def test_startup_rejects_ado_net_or_invalid_credentials_before_database_work(self):
        from types import SimpleNamespace
        from photo_api import event_backend
        valid = {
            'ConnectionString': 'Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=Employee_db;',
            'ApiToken': secrets.token_urlsafe(36), 'AppConfig': self.config,
            'PublicOrigin': 'https://eventdesk.test',
        }
        for changes in (
                {'ConnectionString': 'Data Source=localhost;Initial Catalog=Employee_db;Integrated Security=True'},
                {'ApiToken': 'weak'}, {'AppConfig': {}}, {'PublicOrigin': 'http://eventdesk.test'},
                {'Schema': 'dbo;DROP TABLE employees'}, {'Schema': None}):
            with self.subTest(changes=list(changes)), \
                    patch.object(sys, 'argv', ['event_backend', '--config-stdin']), \
                    patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(protocol.dumps({**valid, **changes})))), \
                    patch.object(event_backend, 'SqlServerStore') as store, self.assertRaises(ValueError):
                event_backend.main()
            store.return_value.initialize.assert_not_called()


@unittest.skipUnless(os.environ.get('EVENTDESK_SQLSERVER_NATIVE_TESTS') == '1',
                     'Enable the isolated local SQL Server integration tests explicitly.')
class NativeEventBackendTests(EventBackendTests):
    @classmethod
    def setUpClass(cls):
        import pyodbc
        cls.database = 'EventDeskMigrationTest_backend_' + uuid.uuid4().hex[:12]
        cls.master = ('DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\\SQLEXPRESS;'
                      'DATABASE=master;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes')
        with pyodbc.connect(cls.master, autocommit=True) as raw:
            raw.execute('CREATE DATABASE [' + cls.database + ']')
        cls.sql_connection = cls.master.replace('DATABASE=master;', 'DATABASE=' + cls.database + ';')
        SqlServerStore(cls.sql_connection).initialize()

    @classmethod
    def tearDownClass(cls):
        import pyodbc
        if not re.fullmatch(r'EventDeskMigrationTest_backend_[0-9a-f]{12}', cls.database):
            raise RuntimeError('Refusing to remove an unexpected test database.')
        with pyodbc.connect(cls.master, autocommit=True) as raw:
            raw.execute('ALTER DATABASE [' + cls.database + '] SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
            raw.execute('DROP DATABASE [' + cls.database + ']')

    def setUp(self):
        import pyodbc
        from eventdesk_storage import TABLES
        super().setUp()
        with pyodbc.connect(self.sql_connection, autocommit=True) as raw:
            for table in TABLES:
                raw.execute('ALTER TABLE [eventdesk].[' + table + '] NOCHECK CONSTRAINT ALL')
            for table in TABLES:
                raw.execute('DELETE FROM [eventdesk].[' + table + ']')
            for table in TABLES:
                raw.execute('ALTER TABLE [eventdesk].[' + table + '] WITH CHECK CHECK CONSTRAINT ALL')
            for table in ('EventOperationReceipt', 'EventChangeJournal'):
                raw.execute('DELETE FROM [eventdesk].[' + table + ']')
            raw.execute('UPDATE [eventdesk].[EventRevision] SET revision=0 WHERE singleton=1')
        self.store = SqlServerStore(self.sql_connection)
        self.engine = EventEngine(self.store, app, self.config, 'https://eventdesk.test')
        with self.store.transaction() as tx, self.engine._bind(tx):
            app.init_db()
            app.import_employees([('0000123', 'Test Employee', 'Test')], 'test')
            tx.finish(None, None, {})


if __name__ == '__main__':
    unittest.main()
