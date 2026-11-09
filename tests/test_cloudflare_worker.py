"""Exercise hosted routing and response boundaries without a Workers runtime."""
import base64
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
import zlib
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qrcode


class Headers:
    def __init__(self, pairs=()):
        self.pairs = list(pairs.items() if hasattr(pairs, 'items') else pairs)

    @classmethod
    def new(cls):
        return cls()

    def set(self, name, value):
        self.pairs = [(key, item) for key, item in self.pairs if key.lower() != name.lower()]
        self.append(name, value)

    def append(self, name, value):
        self.pairs.append((name, value))

    def get(self, name):
        return next((value for key, value in self.pairs if key.lower() == name.lower()), None)

    def values(self, name):
        return [value for key, value in self.pairs if key.lower() == name.lower()]

    def __iter__(self):
        return iter(self.pairs)


class Response:
    @staticmethod
    def new(body, options):
        return SimpleNamespace(body=body, status=options.get('status', 200), headers=options['headers'])


def import_worker():
    workers, js, pyodide, ffi = [ModuleType(name) for name in ('workers', 'js', 'pyodide', 'pyodide.ffi')]
    workers.DurableObject = workers.WorkerEntrypoint = type('RuntimeBase', (), {})
    js.Headers, js.Response = Headers, Response
    js.Request = SimpleNamespace(new=lambda url, options: SimpleNamespace(url=url, **options))
    js.AbortController = SimpleNamespace(new=lambda: SimpleNamespace(signal=object(), abort=Mock()))
    js.Object = SimpleNamespace(fromEntries=dict)
    js.Uint8Array = SimpleNamespace(new=lambda value: value)
    ffi.to_js = lambda value, **_options: value
    pyodide.ffi = ffi
    modules = {'workers': workers, 'js': js, 'pyodide': pyodide, 'pyodide.ffi': ffi}
    spec = importlib.util.spec_from_file_location('_worker_under_test', Path(__file__).resolve().parents[1] / 'cloudflare' / 'worker.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules), patch.object(sys, 'path', sys.path[:]), patch.object(qrcode.QRCode, 'make_image', qrcode.QRCode.make_image):
        spec.loader.exec_module(module)
    return module


def request(path='/api/stats', headers=(), method='GET', body=None):
    return SimpleNamespace(url='https://eventdesk.example' + path, headers=Headers(headers), method=method, body=body)


class Reader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.read_count = 0
        self.cancel = AsyncMock()
        self.releaseLock = Mock()

    async def read(self):
        self.read_count += 1
        data = next(self.chunks, None)
        if data is None:
            return SimpleNamespace(done=True)
        return SimpleNamespace(done=False, value=SimpleNamespace(byteLength=len(data), to_py=lambda: data))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = import_worker()

    def environment(self, **overrides):
        backend = SimpleNamespace(fetch=AsyncMock(return_value=Response.new(b'backend', {'status': 200, 'headers': Headers()})))
        state = SimpleNamespace(getByName=Mock(return_value=backend))
        values = {'DEPLOYMENT_KEY': 'd' * 32,
                  'PUBLIC_ORIGIN': 'https://eventdesk.example', 'STATE': state, **overrides}
        return SimpleNamespace(**values), backend

    def test_static_manifest_accepts_compressed_and_original_deployment_formats(self):
        manifest = {'fonts/fc-pride.otf': {'size': 48172, 'type': 'font/otf', 'keys': ['font:0']}}
        original = json.dumps(manifest)
        compressed = 'zlib:' + base64.b64encode(zlib.compress(original.encode())).decode()
        self.assertEqual(self.worker.parse_static_manifest(original), manifest)
        self.assertEqual(self.worker.parse_static_manifest(compressed), manifest)

    async def test_decoder_worker_keeps_strict_script_policy_for_get_and_head(self):
        manifest = {name: {'size': 1, 'type': 'text/javascript', 'keys': ['chunk']}
                    for name in ['app.js', 'qr-detail-worker.js']}
        assets = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(byteLength=1)))
        instance = self.worker.Default()
        instance.env = SimpleNamespace(STATIC_MANIFEST=json.dumps(manifest), ASSETS=assets)
        for method in ['GET', 'HEAD']:
            for name in manifest:
                with self.subTest(method=method, name=name):
                    with patch.object(self.worker, 'Uint8Array',
                                      SimpleNamespace(new=lambda _size: SimpleNamespace(length=1, set=lambda *_args: None))):
                        result = await instance.asset(request('/static/' + name, method=method), '/static/' + name)
                    policies = result.headers.values('Content-Security-Policy')
                    self.assertEqual(len(policies), 1)
                    self.assertNotIn("'wasm-unsafe-eval'", policies[0])
                    self.assertNotIn("'unsafe-eval'", policies[0])

    async def test_body_reader_handles_no_body_and_releases_completed_stream(self):
        self.assertEqual(await self.worker.read_body(request(), 8 * 1024 * 1024), b'')
        reader = Reader([b'first', b'second'])
        body = SimpleNamespace(getReader=lambda: reader)
        self.assertEqual(await self.worker.read_body(request(body=body), 8 * 1024 * 1024), b'firstsecond')
        reader.cancel.assert_not_awaited()
        reader.releaseLock.assert_called_once()

    async def test_eight_megabyte_limit_accepts_exact_size_and_cancels_overflow(self):
        limit = 8 * 1024 * 1024
        reader = Reader([b'a' * (limit // 2), b'b' * (limit // 2)])
        result = await self.worker.read_body(request(body=SimpleNamespace(getReader=lambda: reader)), limit)
        self.assertEqual(len(result), limit)
        reader.releaseLock.assert_called_once()
        overflow = Reader([b'x' * limit, b'y', b'not read'])
        with self.assertRaisesRegex(ValueError, 'too large'):
            await self.worker.read_body(request(body=SimpleNamespace(getReader=lambda: overflow)), limit)
        overflow.cancel.assert_awaited_once()
        overflow.releaseLock.assert_called_once()
        self.assertEqual(overflow.read_count, 2)

    async def test_body_read_failure_still_releases_lock(self):
        reader = SimpleNamespace(read=AsyncMock(side_effect=RuntimeError('stream failed')), releaseLock=Mock())
        with self.assertRaisesRegex(RuntimeError, 'stream failed'):
            await self.worker.read_body(request(body=SimpleNamespace(getReader=lambda: reader)), 8 * 1024 * 1024)
        reader.releaseLock.assert_called_once()

    async def test_old_or_malformed_authorization_headers_do_not_gate_public_requests(self):
        values = ['', 'Bearer abc', 'Basic invalid!', 'Basic ' + 'x' * 2049,
                  'Basic ZXZlbnRzdGFmZjpyZW1vdGUtcGFzc3dvcmQ=', 'Basic \U0001f511']
        for value in values:
            with self.subTest(value=value):
                env, backend = self.environment(REMOTE_PASSWORD='remote-password')
                worker = self.worker.Default()
                worker.env = env
                incoming = request(headers={'Authorization': value})
                result = await worker.fetch(incoming)
                self.assertIs(result, backend.fetch.return_value)
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
                backend.fetch.assert_awaited_once_with(incoming)

    def test_deployment_key_requires_a_configured_secret_of_at_least_32_characters(self):
        for key in (None, '', 'short', 123):
            with self.subTest(key=key):
                self.assertFalse(self.worker.deployment_authorized(request(), SimpleNamespace(DEPLOYMENT_KEY=key)))
        self.assertFalse(self.worker.deployment_authorized(request(), SimpleNamespace()))
        env, _ = self.environment()
        self.assertFalse(self.worker.deployment_authorized(request(), env))
        self.assertTrue(self.worker.deployment_authorized(request(headers={'X-Deployment-Key': 'd' * 32}), env))
        self.assertFalse(self.worker.deployment_authorized(request(headers={'X-Deployment-Key': 'wrong'}), env))

    def test_deployment_key_rejects_non_ascii_input_without_raising(self):
        env, _ = self.environment()
        self.assertFalse(self.worker.deployment_authorized(request(headers={'X-Deployment-Key': '\U0001f511' * 32}), env))

    async def test_anonymous_homepage_and_static_files_load_without_browser_login(self):
        env, backend = self.environment()
        worker = self.worker.Default()
        worker.env = env
        worker.asset = AsyncMock(return_value=Response.new(b'public asset', {'headers': Headers()}))
        for path in ('/', '/static/app.js', '/projector'):
            with self.subTest(path=path):
                incoming = request(path)
                result = await worker.fetch(incoming)
                self.assertIs(result, worker.asset.return_value)
                self.assertEqual(result.status, 200)
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
                worker.asset.assert_awaited_with(incoming, path)
        self.assertEqual(worker.asset.await_count, 3)
        env.STATE.getByName.assert_not_called()
        backend.fetch.assert_not_awaited()

    async def test_deployment_route_still_needs_its_secret_after_public_entry_is_enabled(self):
        env, backend = self.environment()
        worker = self.worker.Default()
        worker.env = env
        result = await worker.fetch(request('/__deployment/health', {'Authorization': 'Basic ZXZlbnRzdGFmZjpyZW1vdGUtcGFzc3dvcmQ='}))
        self.assertEqual(result.status, 404)
        backend.fetch.assert_not_awaited()
        valid = request('/__deployment/health', {'X-Deployment-Key': env.DEPLOYMENT_KEY})
        self.assertIs(await worker.fetch(valid), backend.fetch.return_value)
        backend.fetch.assert_awaited_once_with(valid)

    async def test_anonymous_api_request_forwards_unchanged_to_existing_application_authentication(self):
        env, backend = self.environment()
        worker = self.worker.Default()
        worker.env = env
        incoming = request('/api/lookup', method='POST')
        self.assertIs(await worker.fetch(incoming), backend.fetch.return_value)
        env.STATE.getByName.assert_called_once_with('main')
        backend.fetch.assert_awaited_once_with(incoming)

    async def test_empty_or_missing_remote_password_does_not_block_public_entry(self):
        for overrides in ({}, {'REMOTE_PASSWORD': None}, {'REMOTE_PASSWORD': ''}):
            with self.subTest(overrides=overrides):
                env, backend = self.environment(**overrides)
                worker = self.worker.Default()
                worker.env = env
                result = await worker.fetch(request())
                self.assertIs(result, backend.fetch.return_value)
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
                backend.fetch.assert_awaited_once()

    async def test_deployment_routes_remain_hidden_when_deployment_secret_is_removed(self):
        env, backend = self.environment()
        del env.DEPLOYMENT_KEY
        worker = self.worker.Default()
        worker.env = env
        for path in ('/__deployment/health', '/__deployment/import', '/__deployment/complete'):
            with self.subTest(path=path):
                result = await worker.fetch(request(path, {'X-Deployment-Key': 'd' * 32}))
                self.assertEqual(result.status, 404)
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
        backend.fetch.assert_not_awaited()

    async def test_http_redirects_to_canonical_https_before_authentication_or_forwarding(self):
        env, backend = self.environment(PUBLIC_ORIGIN='https://secure-eventdesk.example')
        worker = self.worker.Default()
        worker.env = env
        worker.asset = AsyncMock()
        for headers in ({}, {'Authorization': 'Basic old-credentials'}):
            with self.subTest(authenticated=bool(headers)):
                incoming = request('/api/stats?view=summary', headers)
                incoming.url = incoming.url.replace('https://', 'http://')
                result = await worker.fetch(incoming)
                self.assertEqual(result.status, 308)
                self.assertEqual(result.headers.get('Location'), 'https://secure-eventdesk.example/api/stats?view=summary')
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
        worker.asset.assert_not_awaited()
        env.STATE.getByName.assert_not_called()
        backend.fetch.assert_not_awaited()

    async def test_legacy_https_worker_hostname_redirects_to_pages_before_authentication(self):
        env, backend = self.environment(PUBLIC_ORIGIN='https://eventdesk.pages.dev')
        worker = self.worker.Default()
        worker.env = env
        worker.asset = AsyncMock()
        for headers in ({}, {'Authorization': 'Basic old-credentials'}):
            with self.subTest(authenticated=bool(headers)):
                incoming = request('/api/stats?view=summary', headers)
                incoming.url = 'https://legacy-worker.account.workers.dev/api/stats?view=summary'
                result = await worker.fetch(incoming)
                self.assertEqual(result.status, 308)
                self.assertEqual(result.headers.get('Location'), 'https://eventdesk.pages.dev/api/stats?view=summary')
                self.assertIsNone(result.headers.get('WWW-Authenticate'))
        worker.asset.assert_not_awaited()
        env.STATE.getByName.assert_not_called()
        backend.fetch.assert_not_awaited()

    def test_response_preserves_status_challenge_and_multiple_session_cookies(self):
        result = self.worker.response(b'login required', 401, [('WWW-Authenticate', 'Basic realm="staff"'), ('Set-Cookie', 'first=1; HttpOnly'), ('Set-Cookie', 'second=2; Secure')])
        self.assertEqual(result.status, 401)
        self.assertEqual(result.headers.get('WWW-Authenticate'), 'Basic realm="staff"')
        self.assertEqual(result.headers.values('Set-Cookie'), ['first=1; HttpOnly', 'second=2; Secure'])
        self.assertEqual(result.headers.get('Cache-Control'), 'no-store')
        self.assertRegex(result.headers.get('Strict-Transport-Security'), r'max-age=\d+')

    async def test_durable_request_rollback_preserves_handler_auth_error_and_cookies(self):
        worker = object.__new__(self.worker.EventDesk)
        worker.initialized, worker.config = True, {}
        worker.bind_database = Mock()
        storage = SimpleNamespace(sql=SimpleNamespace(exec=Mock(return_value=[{'value': '1'}])), transactionSync=Mock(side_effect=lambda callback: callback()))
        worker.ctx = SimpleNamespace(storage=storage)
        worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example')
        result = {'body': b'not authorized', 'status': 401, 'headers': [('WWW-Authenticate', 'Basic realm="staff"'), ('Set-Cookie', 'one=1'), ('Set-Cookie', 'two=2')]}
        with patch.object(self.worker, 'dispatch', return_value=result), \
                patch.object(self.worker.app, 'get_stats', self.worker.app.get_stats):
            outgoing = await worker.fetch(request())
        self.assertEqual(outgoing.status, 401)
        self.assertEqual(outgoing.headers.get('WWW-Authenticate'), 'Basic realm="staff"')
        self.assertEqual(outgoing.headers.values('Set-Cookie'), ['one=1', 'two=2'])
        storage.transactionSync.assert_called_once()

    def test_event_stats_are_shared_until_the_30_second_cache_window_expires(self):
        worker = object.__new__(self.worker.EventDesk)
        worker._stats_cache, worker._stats_until = None, 0
        first, refreshed = {'total_employees': 1709, 'checked_in': 1}, {'total_employees': 1709, 'checked_in': 2}
        clock = Mock(return_value=100)
        with patch.object(self.worker, 'time', SimpleNamespace(time=clock), create=True), patch.object(self.worker, '_uncached_stats', side_effect=[first, refreshed], create=True) as query:
            self.assertEqual(worker.cached_stats(), first)
            clock.return_value = 129.999
            self.assertEqual(worker.cached_stats(), first)
            query.assert_called_once_with()
            clock.return_value = 130
            self.assertEqual(worker.cached_stats(), refreshed)
            self.assertEqual(query.call_count, 2)

    def test_initial_stats_failure_is_not_cached(self):
        worker = object.__new__(self.worker.EventDesk)
        worker._stats_cache, worker._stats_until = None, 0
        recovered = {'total_employees': 1709, 'checked_in': 4}
        clock = Mock(return_value=100)
        with patch.object(self.worker, 'time', SimpleNamespace(time=clock), create=True), patch.object(self.worker, '_uncached_stats', side_effect=[RuntimeError('database unavailable'), recovered], create=True) as query:
            with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
                worker.cached_stats()
            clock.return_value = 101
            self.assertEqual(worker.cached_stats(), recovered)
            clock.return_value = 102
            self.assertEqual(worker.cached_stats(), recovered)
            self.assertEqual(query.call_count, 2)

    def test_failed_stats_refresh_does_not_extend_the_expired_cache(self):
        worker = object.__new__(self.worker.EventDesk)
        worker._stats_cache, worker._stats_until = {'checked_in': 1}, 100
        recovered = {'checked_in': 2}
        clock = Mock(return_value=100)
        with patch.object(self.worker, 'time', SimpleNamespace(time=clock), create=True), patch.object(self.worker, '_uncached_stats', side_effect=[RuntimeError('refresh failed'), recovered], create=True) as query:
            with self.assertRaisesRegex(RuntimeError, 'refresh failed'):
                worker.cached_stats()
            clock.return_value = 101
            self.assertEqual(worker.cached_stats(), recovered)
            self.assertEqual(query.call_count, 2)


if __name__ == '__main__':
    unittest.main()
