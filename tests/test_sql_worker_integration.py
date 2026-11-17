"""SQL-primary routing must not touch old business tables on healthy requests."""
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_cloudflare_worker import Headers, Reader, import_worker, request


class JavaScriptNull:
    def __bool__(self):
        return False


class NullableHeaders(Headers):
    def get(self, name):
        value = super().get(name)
        return JavaScriptNull() if value is None else value


class SQLWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_worker()

    def worker(self, **environment):
        worker = object.__new__(self.module.EventDesk)
        worker.config = {'secret': '01' * 32}
        worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example', **environment)
        worker.initialize = Mock(side_effect=AssertionError('A healthy SQL request must not initialize business tables.'))
        return worker

    async def test_default_sql_routes_to_primary_without_old_sqlite_dispatch(self):
        worker = self.worker()
        primary = SimpleNamespace(handle=AsyncMock(return_value={'status': 200, 'headers': [], 'body': b'{"role":"staff"}'}))
        worker._get_sql_primary = Mock(return_value=primary)
        response = await worker.fetch(request('/api/me', headers={'Cookie': 'party_session=synthetic'}))
        self.assertEqual(response.status, 200)
        worker.initialize.assert_not_called()
        self.assertEqual(primary.handle.await_args.args[0]['path'], '/api/me')

    async def test_mutations_keep_browser_uuid_and_ignore_untrusted_internal_headers(self):
        worker = self.worker()
        identity = 'c8a94140-e19e-4c9f-8a23-dfc6f8a8ac04'
        primary = SimpleNamespace(handle=AsyncMock(return_value={'status': 202, 'headers': [], 'body': b'{}'}))
        worker._get_sql_primary = Mock(return_value=primary)
        token, profile = self.module.app.sign_session('staff', worker.config)
        incoming = request('/api/scan/checkin', headers={
            'X-EventDesk-Operation-Id': identity, 'X-EventDesk-Replay': 'true',
            'X-Deployment-Key': 'untrusted', 'Authorization': 'Bearer browser-value',
            'Cookie': 'party_session=' + token, 'X-CSRF-Token': profile['csrf'], 'Content-Type': 'application/json'}, method='POST',
            body=SimpleNamespace(getReader=lambda: Reader([b'{"employee_id":"0000123"}'])))
        await worker.fetch(incoming)
        envelope = primary.handle.await_args.args[0]
        self.assertEqual(envelope['operation_id'], identity)
        self.assertEqual(base64.b64decode(envelope['body_base64']), b'{"employee_id":"0000123"}')
        self.assertFalse({'replay', 'queue_proof'}.intersection(envelope))
        names = {name.lower() for name, _ in envelope['headers']}
        self.assertNotIn('authorization', names)
        self.assertNotIn('x-deployment-key', names)
        self.assertIn('recorded_at', envelope)

    async def test_missing_mutation_id_requests_refresh_before_any_network(self):
        worker = self.worker()
        worker._get_sql_primary = Mock()
        response = await worker.fetch(request('/api/draw', method='POST'))
        self.assertEqual(response.status, 400)
        worker._get_sql_primary.assert_not_called()

    async def test_sql_transport_preserves_config_rejection_as_protocol_failure(self):
        worker = self.worker(PHOTO_PILOT_ACCESS_TOKEN='Synthetic-event-gateway-token-0123456789',
            PHOTO_PILOT=SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=401,
                headers=Headers(), body=None))))
        with self.assertRaises(self.module.PrimaryProtocolError):
            await worker._event_transport('request', {'schema_version': 1})

    async def test_sql_transport_is_fixed_bounded_and_outside_sqlite_transaction(self):
        expected = {'schema_version': 1, 'status': 200}
        raw = json.dumps(expected).encode()
        service = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(status=200, redirected=False,
            headers=Headers({'Content-Type': 'application/json; charset=utf-8', 'Content-Length': str(len(raw))}),
            body=SimpleNamespace(getReader=lambda: Reader([raw])))))
        worker = self.worker(PHOTO_PILOT_ACCESS_TOKEN='Synthetic-event-gateway-token-0123456789', PHOTO_PILOT=service)
        self.assertEqual(await worker._event_transport('request', {'schema_version': 1}), expected)
        sent = service.fetch.await_args.args[0]
        self.assertEqual(sent.url, 'https://eventdesk-photo-pilot.pages.dev/v1/event/request')
        self.assertEqual(sent.method, 'POST')
        self.assertEqual(sent.headers.get('Authorization'), 'Bearer Synthetic-event-gateway-token-0123456789')
        worker.initialize.assert_not_called()

    async def test_explicit_sqlite_mode_retains_original_handler(self):
        worker = self.worker(EVENT_STORAGE='sqlite')
        worker._fetch_sqlite = AsyncMock(return_value='original SQLite response')
        self.assertEqual(await worker.fetch(request('/api/me')), 'original SQLite response')
        worker._fetch_sqlite.assert_awaited_once()

    async def test_sql_transport_unwraps_python_workers_fetch_response(self):
        expected = {'schema_version': 1, 'status': 200}
        raw = json.dumps(expected).encode()
        upstream = SimpleNamespace(status=200, redirected=False,
            headers=Headers({'Content-Type': 'application/json; charset=utf-8',
                             'Content-Length': str(len(raw))}),
            body=SimpleNamespace(getReader=lambda: Reader([raw])))
        wrapped = SimpleNamespace(status=200,
            headers={'content-type': 'application/json; charset=utf-8'},
            js_response=upstream)
        worker = self.worker(PHOTO_PILOT_ACCESS_TOKEN='Synthetic-event-gateway-token-0123456789',
            PHOTO_PILOT=SimpleNamespace(fetch=AsyncMock(return_value=wrapped)))
        self.assertEqual(await worker._event_transport('request', {'schema_version': 1}), expected)

    async def test_sql_transport_accepts_javascript_null_for_optional_headers(self):
        expected = {'schema_version': 1, 'status': 200}
        raw = json.dumps(expected).encode()
        upstream = SimpleNamespace(status=200, redirected=False,
            headers=NullableHeaders({'Content-Type': 'application/json; charset=utf-8'}),
            body=SimpleNamespace(getReader=lambda: Reader([raw])))
        worker = self.worker(PHOTO_PILOT_ACCESS_TOKEN='Synthetic-event-gateway-token-0123456789',
            PHOTO_PILOT=SimpleNamespace(fetch=AsyncMock(return_value=upstream)))
        self.assertEqual(await worker._event_transport('request', {'schema_version': 1}), expected)

    async def test_missing_javascript_operation_header_becomes_python_none(self):
        worker = self.worker()
        primary = SimpleNamespace(handle=AsyncMock(return_value={'status': 200, 'headers': [], 'body': b'{}'}))
        worker._get_sql_primary = Mock(return_value=primary)
        incoming = request('/api/me')
        incoming.headers = NullableHeaders()
        self.assertEqual((await worker.fetch(incoming)).status, 200)
        self.assertIsNone(primary.handle.await_args.args[0]['operation_id'])

    async def test_invalid_private_server_config_fails_closed_as_server_error(self):
        for config in ({}, {'secret': 'not-hex'}):
            worker = self.worker()
            worker.config = config
            response = await worker.fetch(request('/api/me'))
            self.assertEqual(response.status, 503)
            body = response.body.decode() if isinstance(response.body, bytes) else response.body
            self.assertNotIn('not-hex', body)

    async def test_unauthorized_large_mutation_is_rejected_before_reading_or_tracking(self):
        for role, path, expected in ((None, '/api/rewards/draft', 401),
                                     ('checkin_staff', '/api/rewards/draft', 403),
                                     ('staff', '/api/admin/employees', 403)):
            worker = self.worker()
            worker._get_sql_primary = Mock()
            headers = {'X-EventDesk-Operation-Id': 'c8a94140-e19e-4c9f-8a23-dfc6f8a8ac04'}
            if role:
                token, profile = self.module.app.sign_session(role, worker.config)
                headers.update({'Cookie': 'party_session=' + token, 'X-CSRF-Token': profile['csrf']})
            reader = Mock()
            response = await worker.fetch(request(path, headers=headers, method='POST',
                body=SimpleNamespace(getReader=reader)))
            self.assertEqual(response.status, expected)
            reader.assert_not_called()
            worker._get_sql_primary.assert_not_called()

    async def test_migration_freeze_stops_all_api_work_before_either_database(self):
        worker = self.worker(EVENT_STORAGE='sqlite', EVENT_MAINTENANCE='1')
        worker._get_sql_primary = Mock()
        worker._fetch_sqlite = AsyncMock()
        response = await worker.fetch(request('/api/me'))
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers.get('Retry-After'), '10')
        worker._fetch_sqlite.assert_not_called()
        worker._get_sql_primary.assert_not_called()


if __name__ == '__main__':
    unittest.main()
