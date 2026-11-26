"""Online event operations remain usable without the optional coordinator."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cloudflare_worker import Headers, Reader, import_worker, request
from cloudflare.event_primary import PrimaryProtocolError, PrimaryUnavailable
from cloudflare.direct_api import (DirectAPIProxy, eligible_operation,
    sign_fallback_stamp, apply_fallback_stamp, FALLBACK_STAMP, FALLBACK_PROOF)
from photo_api.event_protocol import parse_command


SECRET = '01' * 32
PROJECTOR_MANAGEMENT_POSTS = (
    '/api/rewards/reorder', '/api/winners/remove', '/api/winners/history/clear',
)
BULK_HISTORY_PATH = '/api/winners/history/clear'


def envelope(path='/api/scan/checkin', method='POST'):
    return {'schema_version': 1, 'method': method, 'path': path,
        'operation_id': str(uuid.uuid4()) if method == 'POST' else None,
        'headers': [['Cookie', 'party_session=fixture']], 'body_base64':
        base64.b64encode(b'{"employee_id":"0000123"}').decode() if method == 'POST' else '',
        'client_ip': '127.0.0.1', 'after_revision': 0, 'include_replica': False,
        **({'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            if path == '/api/scan/checkin' else {})}


def reply(command, *, status=200):
    return {'schema_version': 1, 'operation_id': command['operation_id'],
        'after_revision': 0, 'revision': 10, 'changes': [], 'replica_complete': False,
        'snapshot_required': False, 'receipt_found': False, 'cancelled': False,
        'status': status, 'headers': [['Content-Type', 'application/json']],
        'body_base64': base64.b64encode(b'{"ok":true}').decode()}


class DirectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.proxy = DirectAPIProxy(clock=lambda: self.now)
        self.transport = AsyncMock(side_effect=lambda _kind, value: reply(value))
        self.fallback = AsyncMock(return_value=SimpleNamespace(status=202, headers=Headers(), body=b'{}'))
        self.maintain = AsyncMock(return_value=SimpleNamespace(status=200, headers=Headers({
            'X-EventDesk-Backup': 'current', 'X-EventDesk-Backup-Revision': '10',
            'X-EventDesk-Queue': 'clear'})))
        self.tasks = []
        self.addAsyncCleanup(self.drain)

    async def drain(self):
        tasks, self.tasks = self.tasks, []
        if tasks:
            await asyncio.gather(*tasks)

    async def call(self, command=None):
        return await self.proxy.handle(command or envelope(), self.transport,
            self.fallback, lambda value: SimpleNamespace(**value), self.tasks.append, self.maintain)

    async def test_all_core_operations_succeed_before_any_optional_object_work(self):
        for path in ('staff-session', 'login', 'logout', 'lookup', 'scan/checkin', 'draw', 'projector/state'):
            command = envelope('/api/' + path)
            result = await self.call(command)
            self.assertEqual(result.status, 200)
            headers = dict(result.headers)
            self.assertEqual(headers['X-EventDesk-Storage'], 'sql')
            self.assertEqual(headers['X-EventDesk-Sync'], 'online')
            self.assertEqual(headers['X-EventDesk-Operation-Resolved'], command['operation_id'])
        self.maintain.assert_not_awaited()
        self.fallback.assert_not_awaited()

    async def test_lookup_never_waits_for_backup_or_sends_mutation_to_it(self):
        self.maintain.side_effect = RuntimeError('Durable Object quota')
        command = envelope('/api/lookup')
        result = await self.call(command)
        self.assertEqual(result.status, 200)
        self.maintain.assert_not_awaited()
        await self.drain()
        self.assertEqual(result.status, 200)
        self.fallback.assert_not_awaited()
        self.maintain.assert_awaited_once()

    async def test_backup_failure_starts_cooldown_and_cannot_override_sql(self):
        self.maintain.side_effect = RuntimeError('quota')
        await self.call()
        await self.drain()
        self.now += 61
        result = await self.call()
        self.assertEqual(result.status, 200)
        self.assertEqual(self.tasks, [])
        self.assertEqual(dict(result.headers)['X-EventDesk-Backup'], 'unavailable')
        self.now += 300
        await self.call()
        self.assertEqual(len(self.tasks), 1)

    async def test_backup_http_quota_error_is_reported_without_erasing_sql_success(self):
        self.maintain.return_value = SimpleNamespace(status=503, headers=Headers())
        await self.call()
        await self.drain()
        result = await self.call()
        self.assertEqual(result.status, 200)
        self.assertEqual(dict(result.headers)['X-EventDesk-Backup'], 'unavailable')
        self.assertEqual(self.tasks, [])

    async def test_one_probe_in_flight_across_ten_devices_and_long_sync(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(_value):
            entered.set()
            await release.wait()
            return self.maintain.return_value
        self.maintain.side_effect = slow
        await self.call()
        first = asyncio.create_task(self.tasks.pop())
        await entered.wait()
        self.now += 61
        results = await asyncio.gather(*(self.call() for _ in range(10)))
        self.assertTrue(all(value.status == 200 for value in results))
        self.assertEqual(self.tasks, [])
        release.set()
        await first

    async def test_checkin_transport_failure_preserves_exact_envelope_for_fallback(self):
        command = envelope()
        self.transport.side_effect = PrimaryUnavailable('lost response')
        result = await self.call(command)
        self.assertEqual(result.status, 202)
        self.fallback.assert_awaited_once_with(command)

    async def test_known_outage_avoids_repeated_sql_timeouts_until_short_retry_lease(self):
        self.transport.side_effect = PrimaryUnavailable('down')
        for _ in range(10):
            self.assertEqual((await self.call(envelope('/api/lookup'))).status, 202)
        self.assertEqual(self.transport.await_count, 1)
        self.assertEqual(self.fallback.await_count, 10)
        self.now += 5
        await self.call(envelope('/api/lookup'))
        self.assertEqual(self.transport.await_count, 2)

    async def test_draw_transport_failure_is_uncertain_and_never_uses_offline_draw(self):
        self.transport.side_effect = PrimaryUnavailable('lost draw acknowledgement')
        result = await self.call(envelope('/api/draw'))
        self.assertEqual(result.status, 503)
        self.assertNotIn('X-EventDesk-Operation-Resolved', dict(result.headers))
        self.fallback.assert_not_awaited()

    async def test_projector_management_never_writes_to_the_backup_during_sql_outage(self):
        self.transport.side_effect = PrimaryUnavailable('SQL is unavailable')
        for path in PROJECTOR_MANAGEMENT_POSTS:
            with self.subTest(path=path):
                response = await self.call(envelope(path))
                self.assertEqual(response.status, 503)
                self.assertNotIn('X-EventDesk-Operation-Resolved', dict(response.headers))
        self.fallback.assert_not_awaited()

    async def test_read_and_session_outage_can_use_existing_fallback(self):
        self.transport.side_effect = PrimaryUnavailable('unavailable')
        for path, method in (('/api/me', 'GET'), ('/api/lookup', 'POST'), ('/api/staff-session', 'POST')):
            command = envelope(path, method)
            await self.call(command)
            self.fallback.assert_awaited_with(command)

    async def test_auth_and_protocol_rejections_never_fall_back(self):
        self.transport.side_effect = lambda _kind, value: reply(value, status=403)
        self.assertEqual((await self.call()).status, 403)
        self.fallback.assert_not_awaited()
        self.transport.side_effect = PrimaryProtocolError('invalid protocol')
        with self.assertRaises(PrimaryProtocolError):
            await self.call()
        self.fallback.assert_not_awaited()

    async def test_mismatched_operation_response_never_confirms_mutation(self):
        def mismatched(_kind, value):
            return {**reply(value), 'operation_id': str(uuid.uuid4())}
        self.transport.side_effect = mismatched
        with self.assertRaises(PrimaryProtocolError):
            await self.call()
        self.fallback.assert_not_awaited()

    async def test_exact_retries_retain_uuid_body_headers_and_no_internal_flags(self):
        command = envelope('/api/draw')
        await self.call(command)
        await self.call(command)
        self.assertEqual(self.transport.await_args_list[0].args[1], command)
        self.assertEqual(self.transport.await_args_list[1].args[1], command)
        self.assertFalse({'replay', 'receipt_only', 'cancel_if_missing'} & set(command))

    async def test_sql_success_never_claims_old_queue_cleared(self):
        result = await self.call()
        headers = dict(result.headers)
        self.assertNotEqual(headers.get('X-EventDesk-Queue'), 'clear')
        self.assertNotEqual(headers.get('X-EventDesk-Sync'), 'synced')

    async def test_failed_background_registration_cannot_fail_confirmed_checkin(self):
        def broken(_task):
            raise RuntimeError('runtime ended')
        result = await self.proxy.handle(envelope(), self.transport, self.fallback,
            lambda value: SimpleNamespace(**value), broken, self.maintain)
        self.assertEqual(result.status, 200)


class StampTests(unittest.TestCase):
    def test_proof_keeps_original_checkin_time_and_binds_full_envelope(self):
        value = envelope()
        proof = sign_fallback_stamp(value, SECRET)
        headers = Headers({FALLBACK_STAMP: value['recorded_at'], FALLBACK_PROOF: proof})
        later = {**value, 'recorded_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
        self.assertEqual(apply_fallback_stamp(later, headers, SECRET)['recorded_at'], value['recorded_at'])
        for changed in ({'body_base64': 'e30='}, {'operation_id': str(uuid.uuid4())},
                        {'headers': [['Cookie', 'another-session']]}, {'path': '/api/draw'}):
            with self.assertRaises(ValueError):
                apply_fallback_stamp({**later, **changed}, headers, SECRET)

    def test_bad_or_expired_or_future_stamp_is_rejected(self):
        value = envelope()
        for offset in (-301, 6):
            bad = {**value, 'recorded_at': (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()}
            with self.assertRaises(ValueError):
                apply_fallback_stamp(value, Headers({FALLBACK_STAMP: bad['recorded_at'],
                    FALLBACK_PROOF: sign_fallback_stamp(bad, SECRET)}), SECRET)
        for supplied in ({FALLBACK_STAMP: value['recorded_at']}, {FALLBACK_PROOF: '0' * 64},
                         {FALLBACK_STAMP: value['recorded_at'], FALLBACK_PROOF: '0' * 64}):
            with self.assertRaises(ValueError):
                apply_fallback_stamp(value, Headers(supplied), SECRET)

    def test_normal_request_has_no_override_and_allowlist_is_closed(self):
        value = envelope()
        self.assertEqual(apply_fallback_stamp(value, Headers(), SECRET), value)
        for path in ('/api/scan/checkin', '/api/draw', '/api/lookup', '/api/staff-session'):
            self.assertTrue(eligible_operation(request(path, method='POST')))
        for path in ('/api/import', '/api/settings', '/api/unknown', '/v1/event/request'):
            self.assertFalse(eligible_operation(request(path, method='POST')))
        self.assertTrue(eligible_operation(request('/api/employee-photo/0000123')))
        self.assertFalse(eligible_operation(request('/api/employee-photo/0000123?x=1')))


class WorkerDirectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = import_worker()
        self.worker = self.module.Default()
        self.config = {'secret': SECRET}
        self.backend = SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError('quota')))
        self.worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example',
            APP_CONFIG=json.dumps(self.config), PHOTO_PILOT=object(),
            STATE=SimpleNamespace(getByName=Mock(return_value=self.backend)))
        self.worker._direct_transport = AsyncMock(side_effect=lambda _kind, value: reply(value))
        self.tasks = []
        self.worker.ctx = SimpleNamespace(waitUntil=self.tasks.append)
        self.addAsyncCleanup(self.drain)
        self.cookie, claims = self.module.app.sign_session('staff', self.config)
        self.csrf = claims['csrf']

    async def drain(self):
        tasks, self.tasks = self.tasks, []
        if tasks:
            await asyncio.gather(*tasks)

    def incoming(self, path='/api/scan/checkin', *, method='POST', data=b'{"code":"0000123"}',
                 role='staff', **extra):
        cookie, csrf = self.cookie, self.csrf
        if role != 'staff':
            cookie, claims = self.module.app.sign_session(role, self.config)
            csrf = claims['csrf']
        headers = {'Cookie': 'party_session=' + cookie, 'X-CSRF-Token': csrf,
            'Origin': 'https://eventdesk.example', 'Content-Type': 'application/json',
            'X-EventDesk-Operation-Id': str(uuid.uuid4()), **extra}
        return request(path, headers, method=method,
            body=SimpleNamespace(getReader=lambda: Reader([data])) if method == 'POST' else None)

    async def test_core_sql_flow_with_quota_disabled_coordinator_never_awaits_it(self):
        self.worker.env.EVENT_COORDINATOR_DISABLED = '1'
        for path in ('staff-session', 'login', 'lookup', 'scan/checkin', 'projector/state', 'draw', 'logout'):
            result = await self.worker.fetch(self.incoming('/api/' + path))
            self.assertEqual(result.status, 200, path)
            self.assertEqual(result.headers.get('X-EventDesk-Sync'), 'online')
        self.backend.fetch.assert_not_awaited()
        self.assertEqual(len(self.tasks), 1)
        await self.drain()
        self.backend.fetch.assert_not_awaited()

    async def test_projector_management_survives_quota_and_contract_validation(self):
        def private_api(_kind, value):
            parse_command(value)
            return reply(value)

        self.worker.env.EVENT_COORDINATOR_DISABLED = '1'
        self.worker._direct_transport.side_effect = private_api
        for path in PROJECTOR_MANAGEMENT_POSTS:
            with self.subTest(path=path):
                role = 'admin' if path == BULK_HISTORY_PATH else 'staff'
                result = await self.worker.fetch(self.incoming(path, data=b'{}', role=role))
                self.assertEqual(result.status, 200)
                self.assertEqual(result.headers.get('X-EventDesk-Storage'), 'sql')
        self.assertEqual(self.worker._direct_transport.await_count, len(PROJECTOR_MANAGEMENT_POSTS))
        await self.drain()
        self.backend.fetch.assert_not_awaited()

    async def test_projector_management_checks_roles_and_csrf_before_sql(self):
        for path in PROJECTOR_MANAGEMENT_POSTS:
            with self.subTest(path=path):
                incoming = self.incoming(path, role='checkin_staff')
                self.assertEqual((await self.worker.fetch(incoming)).status, 403)
                role = 'admin' if path == BULK_HISTORY_PATH else 'staff'
                self.assertEqual((await self.worker.fetch(
                    self.incoming(path, role=role, **{'X-CSRF-Token': 'wrong'}))).status, 403)
        self.worker._direct_transport.assert_not_awaited()
        self.backend.fetch.assert_not_awaited()

    async def test_bulk_history_rejects_staff_before_sql_even_with_backup_disabled(self):
        self.worker.env.EVENT_COORDINATOR_DISABLED = '1'
        for role in ('staff', 'lucky_draw_operator'):
            incoming = self.incoming(BULK_HISTORY_PATH, role=role)
            self.assertEqual((await self.worker.fetch(incoming)).status, 403)
        self.worker._direct_transport.assert_not_awaited()
        self.backend.fetch.assert_not_awaited()

    async def test_exact_optional_backup_confirmation_is_never_required_by_core_work(self):
        self.worker.env.EVENT_COORDINATOR_DISABLED = '1'
        result = await self.worker.fetch(self.incoming('/api/me?backup=1', method='GET'))
        self.assertEqual(result.status, 503)
        self.worker._direct_transport.assert_not_awaited()
        for path in ('/api/me?backup=0', '/api/me?backup=1&other=1', '/api/stats?backup=1'):
            self.assertEqual((await self.worker.fetch(self.incoming(path, method='GET'))).status, 200)
        self.assertEqual(self.worker._direct_transport.await_count, 3)
        self.backend.fetch.assert_not_awaited()
        self.assertEqual(len(self.tasks), 1)
        await self.drain()
        self.backend.fetch.assert_not_awaited()

    async def test_cold_lookup_and_get_do_not_call_coordinator_before_response(self):
        for incoming in (self.incoming('/api/lookup'), self.incoming('/api/me', method='GET'),
                         self.incoming('/api/employee-photo/0000123', method='GET')):
            self.assertEqual((await self.worker.fetch(incoming)).status, 200)
        self.backend.fetch.assert_not_awaited()

    async def test_preflight_rejects_csrf_before_consuming_body_or_calling_sql(self):
        read = Reader([b'{}'])
        incoming = self.incoming(**{'X-CSRF-Token': 'wrong'})
        incoming.body = SimpleNamespace(getReader=lambda: read)
        self.assertEqual((await self.worker.fetch(incoming)).status, 403)
        self.assertEqual(read.read_count, 0)
        self.worker._direct_transport.assert_not_awaited()
        self.backend.fetch.assert_not_awaited()

    async def test_public_login_still_checks_same_origin_and_config(self):
        self.assertEqual((await self.worker.fetch(self.incoming('/api/login', Origin='https://evil.example'))).status, 403)
        for config in ('null', '{}', 'broken-json'):
            self.worker.env.APP_CONFIG = config
            self.assertEqual((await self.worker.fetch(self.incoming('/api/login'))).status, 503)
        self.worker._direct_transport.assert_not_awaited()

    async def test_body_limit_and_uuid_missing_fail_before_sql(self):
        for path in ('/api/scan/checkin', *PROJECTOR_MANAGEMENT_POSTS):
            with self.subTest(path=path):
                role = 'admin' if path == BULK_HISTORY_PATH else 'staff'
                incoming = self.incoming(path, data=b'x' * (16384 + 1), role=role)
                self.assertEqual((await self.worker.fetch(incoming)).status, 413)
                missing_id = self.incoming(path, role=role, **{'X-EventDesk-Operation-Id': ''})
                self.assertEqual((await self.worker.fetch(missing_id)).status, 400)
        self.worker._direct_transport.assert_not_awaited()
        self.backend.fetch.assert_not_awaited()

    async def test_fallback_reconstructs_consumed_bytes_and_server_signed_time(self):
        self.worker._direct_transport.side_effect = PrimaryUnavailable('down')
        self.backend.fetch.side_effect = None
        self.backend.fetch.return_value = SimpleNamespace(status=202, headers=Headers(), body=b'{}')
        incoming = self.incoming(**{FALLBACK_STAMP: 'attacker', FALLBACK_PROOF: '0' * 64})
        result = await self.worker.fetch(incoming)
        self.assertEqual(result.status, 202)
        direct = self.worker._direct_transport.await_args.args[1]
        rebuilt = self.backend.fetch.await_args.args[0]
        self.assertEqual(rebuilt.body, base64.b64decode(direct['body_base64']))
        self.assertEqual(rebuilt.headers.get('X-EventDesk-Operation-Id'), direct['operation_id'])
        self.assertEqual(rebuilt.headers.get(FALLBACK_STAMP), direct['recorded_at'])
        self.assertEqual(rebuilt.headers.get(FALLBACK_PROOF), sign_fallback_stamp(direct, SECRET))

    async def test_success_schedules_only_get_maintenance_never_a_second_post(self):
        incoming = self.incoming('/api/draw')
        result = await self.worker.fetch(incoming)
        self.assertEqual(result.status, 200)
        self.backend.fetch.assert_not_awaited()
        self.backend.fetch.side_effect = None
        self.backend.fetch.return_value = SimpleNamespace(status=200, headers=Headers())
        await self.drain()
        maintenance = self.backend.fetch.await_args.args[0]
        self.assertEqual(maintenance.method, 'GET')
        self.assertEqual(maintenance.url, 'https://eventdesk.example/api/me')
        self.assertFalse(hasattr(maintenance, 'body'))
        self.assertIsNone(maintenance.headers.get('X-EventDesk-Operation-Id'))

    async def test_sqlite_mode_maintenance_and_optout_preserve_old_path(self):
        self.backend.fetch.side_effect = None
        self.backend.fetch.return_value = SimpleNamespace(status=503, headers=Headers())
        for field, value in (('EVENT_STORAGE', 'sqlite'), ('EVENT_MAINTENANCE', '1'), ('EVENT_DIRECT_API', '0')):
            setattr(self.worker.env, field, value)
            result = await self.worker.fetch(self.incoming('/api/draw'))
            self.assertEqual(result.status, 503)
            delattr(self.worker.env, field)
        self.worker._direct_transport.assert_not_awaited()

    async def test_projector_management_direct_optout_forwards_original_request(self):
        self.worker.env.EVENT_DIRECT_API = '0'
        self.backend.fetch.side_effect = None
        self.backend.fetch.return_value = SimpleNamespace(status=200, headers=Headers())
        for path in PROJECTOR_MANAGEMENT_POSTS:
            with self.subTest(path=path):
                role = 'admin' if path == BULK_HISTORY_PATH else 'staff'
                incoming = self.incoming(path, role=role)
                self.assertEqual((await self.worker.fetch(incoming)).status, 200)
                self.backend.fetch.assert_awaited_with(incoming)
        self.worker._direct_transport.assert_not_awaited()

    async def test_forged_private_proof_is_not_forwarded_on_normal_coordinator_path(self):
        result = await self.worker.fetch(self.incoming('/api/settings', **{FALLBACK_PROOF: '0' * 64}))
        self.assertEqual(result.status, 400)
        self.backend.fetch.assert_not_awaited()

    async def test_coordinator_validates_original_time_before_sql_or_offline_dispatch(self):
        edge_command = envelope()
        edge_command['headers'] = [['Cookie', 'party_session=' + self.cookie],
            ['Origin', 'https://eventdesk.example'], ['X-CSRF-Token', self.csrf],
            ['Content-Type', 'application/json']]
        incoming = self.worker._coordinator_request(edge_command, config=self.config)
        data = incoming.body
        incoming.body = SimpleNamespace(getReader=lambda: Reader([data]))
        durable = object.__new__(self.module.EventDesk)
        durable.config, durable.env = self.config, self.worker.env
        primary = SimpleNamespace(handle=AsyncMock(), handle_unavailable_checkin=AsyncMock(
            return_value={'status': 202, 'body': b'{}', 'headers': []}))
        durable._get_sql_primary = Mock(return_value=primary)
        result = await durable.fetch(incoming)
        self.assertEqual(result.status, 202)
        self.assertEqual(primary.handle_unavailable_checkin.await_args.args[0]['recorded_at'], edge_command['recorded_at'])
        primary.handle.assert_not_awaited()
        incoming.headers.set(FALLBACK_PROOF, '0' * 64)
        primary.handle.reset_mock()
        primary.handle_unavailable_checkin.reset_mock()
        self.assertEqual((await durable.fetch(incoming)).status, 400)
        primary.handle.assert_not_awaited()
        primary.handle_unavailable_checkin.assert_not_awaited()
