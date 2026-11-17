"""The hosted fallback must save attendance and its replay intent atomically."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import qrcode
from cloudflare.replica import ReplicaError, ReplicaStore
from test_cloudflare_sqlite_adapter import FakeSQL
from test_cloudflare_worker import Headers, Reader, import_worker, request


class WorkerSQL(FakeSQL):
    """Add the Workers cursor's iterable surface to the real SQLite fixture."""
    def exec(self, statement, *parameters):
        source = super().exec(statement, *parameters)
        class Result:
            columnNames = source.columnNames
            def raw(self):
                return source.raw()
            def __iter__(self):
                return iter(source.rows)
        return Result()


class TransactionStorage:
    """A real transaction boundary; callbacks cannot hand work to a later await."""
    def __init__(self):
        self.sql = WorkerSQL()
        self.active = False
        self.commits = 0
        self.rollbacks = 0

    def transactionSync(self, callback):
        if self.active:
            raise AssertionError('Unexpected nested hosted transaction.')
        self.sql.database.execute('BEGIN IMMEDIATE')
        self.active = True
        try:
            result = callback()
            if inspect.isawaitable(result):
                if hasattr(result, 'close'):
                    result.close()
                raise TypeError('A transaction callback must be synchronous.')
            self.sql.database.commit()
            self.commits += 1
            return result
        except BaseException:
            self.sql.database.rollback()
            self.rollbacks += 1
            raise
        finally:
            self.active = False


class HostedFallbackTransactionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_worker()

    def setUp(self):
        self.storage = TransactionStorage()
        self.addCleanup(self.storage.sql.database.close)
        self.sql = self.storage.sql.database
        self.config = {
            'event_name': 'Fallback Transaction Test', 'secret': secrets.token_hex(32),
            'admin_password': {'salt': secrets.token_hex(16), 'hash': secrets.token_hex(32)},
            'staff_password': {'salt': secrets.token_hex(16), 'hash': secrets.token_hex(32)},
        }
        # bind_database changes these globals in the real Worker. Restore every
        # affected function/environment value before another test can use app.
        for name in ('db_connection', 'get_stats', 'load_config',
                     'get_projector_prize_image', 'update_projector_prize_image'):
            fixture = patch.object(app, name, getattr(app, name))
            fixture.start()
            self.addCleanup(fixture.stop)
        environment = patch.dict(os.environ, {
            'PARTY_OPEN_STAFF': '1', 'PARTY_COOKIE_SECURE': '1',
            'PARTY_PUBLIC_ORIGIN': 'https://eventdesk.example'})
        environment.start()
        self.addCleanup(environment.stop)
        qr_factory = patch.object(qrcode.QRCode, 'make_image', qrcode.QRCode.make_image)
        qr_factory.start()
        self.addCleanup(qr_factory.stop)

        @contextmanager
        def connection():
            self.sql.row_factory = sqlite3.Row
            yield self.sql

        app.db_connection = connection
        app.init_db()
        app.import_employees([('0000123', 'Test Employee', 'Test Department')], 'test')
        self.sql.execute('CREATE TABLE _hosted_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        self.sql.execute("INSERT INTO _hosted_metadata VALUES('migration_complete','1')")
        self.worker = object.__new__(self.module.EventDesk)
        self.worker.ctx = SimpleNamespace(storage=self.storage)
        self.worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example')
        self.worker.config = self.config
        self.worker.initialized = False
        self.worker._stats_cache = None
        self.worker._stats_until = 0
        self.worker._photo_inflight = 0

        def js_request(url, options):
            incoming = dict(options)
            body = incoming.pop('body', None)
            if body is not None:
                payload = bytes(body)
                body = SimpleNamespace(getReader=lambda: Reader([payload]))
            return SimpleNamespace(url=url, body=body, **incoming)
        js = patch.object(self.module, 'JSRequest', SimpleNamespace(new=js_request))
        js.start()
        self.addCleanup(js.stop)
        self.runtime = self.worker._sql_runtime()
        self.runtime.atomic(lambda store: store.state())

    def row_count(self, table):
        allowed = {'event_checkins', '_eventdesk_outbox', 'event_log', 'staff_sessions'}
        if table not in allowed:
            raise AssertionError('Unexpected test table.')
        return self.sql.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    async def staff_session(self):
        incoming = request('/api/staff-session', method='POST',
            headers={'Content-Type': 'application/json', 'Origin': 'https://eventdesk.example'},
            body=SimpleNamespace(getReader=lambda: Reader([b'{}'])))
        response = await self.worker._fetch_sqlite(incoming, capture=True)
        self.assertEqual(response['status'], 200)
        cookie = next(value.split(';')[0] for name, value in response['headers']
                      if name.lower() == 'set-cookie')
        return cookie, json.loads(response['body'])['csrf']

    def checkin_envelope(self, cookie, csrf, **overrides):
        body = json.dumps({'employee_id': '0000123', 'wristband_reason': 'unreadable'}).encode()
        return {
            'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'method': 'POST',
            'path': '/api/scan/checkin', 'client_ip': '127.0.0.1', 'after_revision': 0,
            'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'headers': [['Content-Type', 'application/json'], ['Origin', 'https://eventdesk.example'],
                        ['Cookie', cookie], ['X-CSRF-Token', csrf]],
            'body_base64': base64.b64encode(body).decode(), **overrides,
        }

    async def test_successful_offline_checkin_and_outbox_commit_in_the_same_transaction(self):
        cookie, csrf = await self.staff_session()
        envelope = self.checkin_envelope(cookie, csrf)
        enqueue = ReplicaStore.enqueue
        observed = []
        def check_transaction(store, *args):
            observed.append((self.storage.active, self.sql.in_transaction))
            return enqueue(store, *args)
        with patch.object(ReplicaStore, 'enqueue', check_transaction):
            response = await self.worker._fallback_event(envelope, 'checkin')
        self.assertEqual(response['status'], 200)
        self.assertEqual(observed, [(True, True)])
        self.assertEqual(self.row_count('event_checkins'), 1)
        self.assertEqual(self.row_count('_eventdesk_outbox'), 1)
        saved = self.runtime.atomic(lambda store: store.find(envelope['operation_id']))
        self.assertEqual(saved['kind'], 'checkin')
        self.assertEqual(saved['envelope'], envelope)
        self.assertEqual(base64.b64decode(saved['response']['body_base64']), response['body'])
        self.assertFalse(self.sql.in_transaction)

    async def test_queue_write_failure_rolls_back_attendance_and_returns_failure(self):
        cookie, csrf = await self.staff_session()
        envelope = self.checkin_envelope(cookie, csrf)
        before_log = self.row_count('event_log')
        rollbacks = self.storage.rollbacks
        with patch.object(ReplicaStore, 'enqueue', side_effect=RuntimeError('Simulated queue storage failure.')):
            response = await self.worker._fallback_event(envelope, 'checkin')
        self.assertEqual(response['status'], 500)
        self.assertIn('error', json.loads(response['body']))
        self.assertEqual(self.row_count('event_checkins'), 0)
        self.assertEqual(self.row_count('_eventdesk_outbox'), 0)
        self.assertEqual(self.row_count('event_log'), before_log)
        self.assertIsNone(self.sql.execute('SELECT checked_in_at FROM employees').fetchone()[0])
        self.assertGreater(self.storage.rollbacks, rollbacks)

    async def test_conflicting_duplicate_uuid_rolls_back_the_new_local_action(self):
        cookie, csrf = await self.staff_session()
        envelope = self.checkin_envelope(cookie, csrf)
        previous = {**envelope, 'body_base64': base64.b64encode(json.dumps({
            'employee_id': '0000123', 'wristband_reason': 'lost'}).encode()).decode()}
        self.runtime.atomic(lambda store: store.enqueue(previous, 'checkin',
                            {'status': 200, 'headers': [], 'body_base64': 'e30='}))
        response = await self.worker._fallback_event(envelope, 'checkin')
        self.assertEqual(response['status'], 500)
        self.assertEqual(self.row_count('event_checkins'), 0)
        self.assertEqual(self.row_count('_eventdesk_outbox'), 1)
        self.assertIsNone(self.sql.execute('SELECT checked_in_at FROM employees').fetchone()[0])
        saved = self.runtime.atomic(lambda store: store.find(envelope['operation_id']))
        self.assertEqual(saved['envelope'], previous)

    async def test_failed_csrf_never_saves_attendance_or_an_outbox_intent(self):
        cookie, _csrf = await self.staff_session()
        envelope = self.checkin_envelope(cookie, 'invalid-csrf')
        with patch.object(ReplicaStore, 'enqueue', side_effect=AssertionError('Unauthorized requests cannot queue.')):
            response = await self.worker._fallback_event(envelope, 'checkin')
        self.assertEqual(response['status'], 403)
        self.assertEqual(self.row_count('event_checkins'), 0)
        self.assertEqual(self.row_count('_eventdesk_outbox'), 0)

    def test_runtime_atomic_persists_metadata_and_rolls_back_exceptional_changes(self):
        def update(store):
            self.assertTrue(self.storage.active)
            self.assertTrue(self.sql.in_transaction)
            store.db.execute('UPDATE _eventdesk_replica_meta SET revision=7 WHERE singleton=1')
            return store.state()['revision']
        self.assertEqual(self.runtime.atomic(update), 7)
        self.assertEqual(self.runtime.atomic(lambda store: store.state()['revision']), 7)
        def rejected(store):
            store.db.execute('UPDATE _eventdesk_replica_meta SET revision=9 WHERE singleton=1')
            raise RuntimeError('A transaction cannot partially persist.')
        with self.assertRaises(RuntimeError):
            self.runtime.atomic(rejected)
        self.assertEqual(self.runtime.atomic(lambda store: store.state()['revision']), 7)
        self.assertFalse(self.storage.active)

    def test_runtime_atomic_rejects_a_callback_that_returns_awaitable_work(self):
        async def delayed(store):
            store.db.execute('UPDATE _eventdesk_replica_meta SET revision=99 WHERE singleton=1')
        with self.assertRaises(TypeError):
            result = self.runtime.atomic(delayed)
            if inspect.isawaitable(result):
                result.close()
        self.assertEqual(self.runtime.atomic(lambda store: store.state()['revision']), 0)


if __name__ == '__main__':
    unittest.main()
