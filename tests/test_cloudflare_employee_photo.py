"""Hosted photo requests authorize in SQL, then fetch outside the transaction."""
import asyncio
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_cloudflare_worker import Headers, Reader, import_worker, request


TOKEN = secrets.token_hex(32)
JPEG = b'\xff\xd8\xffsynthetic-hosted-portrait\xff\xd9'


class HostedEmployeePhotoTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_worker()

    def fixture(self, role='staff', token=TOKEN, photo=None):
        worker = object.__new__(self.module.EventDesk)
        worker.initialized, worker.config = True, {}
        worker.bind_database = Mock()
        worker._photo_inflight = 0
        transaction = {'active': False}

        def transact(callback):
            transaction['active'] = True
            try:
                return callback()
            finally:
                transaction['active'] = False

        storage = SimpleNamespace(sql=SimpleNamespace(exec=Mock(return_value=[{'value': '1'}])),
                                  transactionSync=Mock(side_effect=transact))
        worker.ctx = SimpleNamespace(storage=storage)
        reader = Reader([JPEG])
        upstream = photo or SimpleNamespace(status=200, redirected=False,
            headers=Headers({'Content-Type': 'image/jpeg', 'Content-Length': str(len(JPEG))}),
            body=SimpleNamespace(getReader=lambda: reader))

        async def fetch(incoming):
            self.assertFalse(transaction['active'], 'network fetch must be outside the SQL transaction')
            return upstream

        service = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
        worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example', PHOTO_PILOT=service,
                                     PHOTO_PILOT_ACCESS_TOKEN=token)
        profile = {'role': role, 'sid': 'synthetic-photo-session', 'csrf': 'synthetic'} if role else None
        return worker, service, reader, profile

    async def run_request(self, worker, profile, path='/api/employee-photo/1001519', method='GET'):
        with patch.object(self.module.app.PartyHandler, 'get_user', return_value=profile), \
                patch.object(self.module.app, 'employee_photo_exists',
                             side_effect=lambda employee_id: employee_id in ('1001519', '0001600', '2412577', '0305528'), create=True), \
                patch.object(self.module.app, 'photo_request_allowed', return_value=True):
            return await worker.fetch(request(path, {'Cookie': 'party_session=client-secret',
                'Authorization': 'Bearer client-secret', 'Origin': 'https://untrusted.invalid'}, method=method))

    async def test_valid_photo_uses_fixed_bound_request_and_auth_before_async_io(self):
        worker, service, reader, profile = self.fixture()
        outgoing = await self.run_request(worker, profile)
        self.assertEqual((outgoing.status, outgoing.body), (200, JPEG))
        self.assertEqual(outgoing.headers.get('Content-Type'), 'image/jpeg')
        self.assertEqual(outgoing.headers.get('Vary'), 'Cookie')
        self.assertEqual(outgoing.headers.get('Cache-Control'), 'no-store')
        incoming = service.fetch.await_args.args[0]
        self.assertEqual(incoming.url, 'https://eventdesk-photo-pilot.pages.dev/v1/employees/1001519/photo')
        self.assertEqual(incoming.method, 'GET')
        self.assertEqual(incoming.redirect, 'manual')
        self.assertEqual(incoming.headers.get('Authorization'), 'Bearer ' + TOKEN)
        self.assertIsNone(incoming.headers.get('Cookie'))
        self.assertIsNone(incoming.headers.get('Origin'))
        worker.ctx.storage.transactionSync.assert_called_once()
        reader.releaseLock.assert_called_once()
        self.assertEqual(worker._photo_inflight, 0)

    async def test_no_session_or_wrong_role_never_fetches_or_exposes_pilot(self):
        for role, status in ((None, 401), ('lucky_draw_operator', 403)):
            worker, service, _, profile = self.fixture(role=role)
            outgoing = await self.run_request(worker, profile)
            self.assertEqual(outgoing.status, status)
            service.fetch.assert_not_awaited()
        for role in ('checkin_staff', 'verification_staff', 'admin'):
            worker, _, _, profile = self.fixture(role=role)
            self.assertEqual((await self.run_request(worker, profile)).status, 200)

    async def test_bad_route_query_nonpilot_and_method_never_fetch(self):
        cases = [('/api/employee-photo/1001519?x=1', 'GET', 400),
                 ('/api/employee-photo/1001519?', 'GET', 400),
                 ('/api/employee-photo/%31001519', 'GET', 400),
                 ('/api/employee-photo/1001519/extra', 'GET', 400),
                 ('/api/employee-photo/9999999', 'GET', 404),
                 ('/api/employee-photo/1001519', 'POST', 405)]
        for path, method, status in cases:
            with self.subTest(path=path, method=method):
                worker, service, _, profile = self.fixture()
                outgoing = await self.run_request(worker, profile, path, method)
                self.assertEqual(outgoing.status, status)
                service.fetch.assert_not_awaited()

    async def test_missing_bad_configuration_and_inflight_limit_fail_closed(self):
        for token in ('', 'short', 'bad\nheader' + 'x' * 32, None):
            worker, service, _, profile = self.fixture(token=token)
            self.assertEqual((await self.run_request(worker, profile)).status, 503)
            service.fetch.assert_not_awaited()
        worker, service, _, profile = self.fixture()
        del worker.env.PHOTO_PILOT
        self.assertEqual((await self.run_request(worker, profile)).status, 503)
        service.fetch.assert_not_awaited()
        worker, service, _, profile = self.fixture()
        worker._photo_inflight = 8
        self.assertEqual((await self.run_request(worker, profile)).status, 503)
        service.fetch.assert_not_awaited()

    async def test_rate_limit_stops_before_upstream(self):
        worker, service, _, profile = self.fixture()
        with patch.object(self.module.app.PartyHandler, 'get_user', return_value=profile), \
                patch.object(self.module.app, 'employee_photo_exists', return_value=True, create=True), \
                patch.object(self.module.app, 'photo_request_allowed', return_value=False):
            outgoing = await worker.fetch(request('/api/employee-photo/1001519'))
        self.assertEqual(outgoing.status, 429)
        service.fetch.assert_not_awaited()

    async def test_current_employee_outside_pilot_uses_private_photo_service(self):
        worker, service, _, profile = self.fixture()
        outgoing = await self.run_request(worker, profile, '/api/employee-photo/2412577')
        self.assertEqual((outgoing.status, outgoing.body), (200, JPEG))
        self.assertEqual(service.fetch.await_args.args[0].url,
                         'https://eventdesk-photo-pilot.pages.dev/v1/employees/2412577/photo')

    async def test_current_employee_without_sql_photo_preserves_upstream_notfound(self):
        photo = SimpleNamespace(status=404, redirected=False, headers=Headers(), body=None)
        worker, service, _, profile = self.fixture(photo=photo)
        outgoing = await self.run_request(worker, profile, '/api/employee-photo/0305528')
        self.assertEqual(outgoing.status, 404)
        service.fetch.assert_awaited_once()

    async def test_upstream_status_and_redirects_are_sanitized(self):
        for status, redirected, expected in ((404, False, 404), (401, False, 503),
                (302, False, 503), (500, False, 503), (200, True, 503)):
            photo = SimpleNamespace(status=status, redirected=redirected, headers=Headers(), body=None)
            worker, _, _, profile = self.fixture(photo=photo)
            outgoing = await self.run_request(worker, profile)
            self.assertEqual(outgoing.status, expected)
            self.assertNotIn(TOKEN, str(outgoing.body))
            self.assertNotIn('pages.dev', str(outgoing.body))

    async def test_invalid_jpeg_mime_length_and_body_limits_are_rejected(self):
        cases = [(b'notjpeg', {'Content-Type': 'image/jpeg'}),
                 (JPEG, {'Content-Type': 'text/html'}),
                 (JPEG, {'Content-Type': 'image/jpeg', 'Content-Length': '262145'}),
                 (JPEG, {'Content-Type': 'image/jpeg', 'Content-Length': 'invalid'}),
                 (JPEG, {'Content-Type': 'image/jpeg', 'Content-Length': '999'}),
                 (JPEG, {'Content-Type': 'image/jpeg', 'Content-Encoding': 'gzip'}),
                 (JPEG + b'x' * (256 * 1024), {'Content-Type': 'image/jpeg'}),
                 (JPEG[:-2], {'Content-Type': 'image/jpeg'})]
        for content, headers in cases:
            reader = Reader([content])
            photo = SimpleNamespace(status=200, redirected=False, headers=Headers(headers),
                                    body=SimpleNamespace(getReader=lambda: reader))
            worker, _, _, profile = self.fixture(photo=photo)
            self.assertEqual((await self.run_request(worker, profile)).status, 503)

    async def test_fetch_and_body_deadlines_release_inflight_slot(self):
        for stage in ('fetch', 'body'):
            worker, service, reader, profile = self.fixture()
            async def hang(*_args):
                await asyncio.sleep(30)
            if stage == 'fetch':
                service.fetch.side_effect = hang
            else:
                reader.read = AsyncMock(side_effect=hang)
            controller = SimpleNamespace(signal=object(), abort=Mock())
            with patch.object(self.module, 'PHOTO_TIMEOUT_SECONDS', 0.01), \
                    patch.object(self.module.AbortController, 'new', return_value=controller):
                self.assertEqual((await self.run_request(worker, profile)).status, 503)
            self.assertGreaterEqual(controller.abort.call_count, 2, 'deadline abort and final cleanup both run')
            self.assertEqual(worker._photo_inflight, 0)

    async def test_diagnostic_logs_only_a_fixed_stage_and_exception_type(self):
        worker, _, reader, profile = self.fixture()
        reader.read = AsyncMock(side_effect=RuntimeError(TOKEN + ' private cookie and SQL details'))
        with patch('builtins.print') as log:
            outgoing = await self.run_request(worker, profile)
        self.assertEqual(outgoing.status, 503)
        log.assert_called_once_with('Photo proxy failed:', 'body', 'RuntimeError')
        self.assertNotIn(TOKEN, str(outgoing.body))

    async def test_python_fetch_response_is_unwrapped_for_js_headers_and_bounded_stream(self):
        reader = Reader([JPEG])
        raw = SimpleNamespace(status=200, redirected=False,
            headers=Headers({'Content-Type': 'image/jpeg', 'Content-Length': str(len(JPEG))}),
            body=SimpleNamespace(getReader=lambda: reader))
        photo = SimpleNamespace(status=200, redirected=False, headers={'content-type': 'image/jpeg'}, js_response=raw)
        worker, _, _, profile = self.fixture(photo=photo)
        with patch('builtins.print') as log:
            outgoing = await self.run_request(worker, profile)
        self.assertEqual((outgoing.status, outgoing.body), (200, JPEG))
        reader.releaseLock.assert_called_once()
        log.assert_not_called()


if __name__ == '__main__':
    unittest.main()
