"""Fully hosted EventDesk: public assets and one authorized event database."""
import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / 'vendor.zip'))

from workers import DurableObject, WorkerEntrypoint
from js import AbortController, Headers, Object, Request as JSRequest, Response, Uint8Array
from pyodide.ffi import to_js

from cloudflare.crypto_compat import install as install_crypto
from cloudflare.handler_adapter import _make_handler, _request_headers, dispatch
from cloudflare.hosted_storage import install as install_storage
from cloudflare.sqlite_adapter import SQLiteConnection, TransactionState
from cloudflare.event_primary import PrimaryProtocolError, PrimaryUnavailable
from cloudflare.event_runtime import HostedSQLRuntime, request_envelope, sql_transport, storage_mode
from cloudflare.event_policy import preflight_mutation
from cloudflare.read_proxy import OnlineReadProxy, eligible_read
from cloudflare.direct_api import (DirectAPIProxy, eligible_operation, MAX_POST_BYTES,
    FALLBACK_STAMP, FALLBACK_PROOF, sign_fallback_stamp, apply_fallback_stamp)

install_crypto()
import app
_uncached_stats = app.get_stats
PHOTO_TIMEOUT_SECONDS = 8
PHOTO_INFLIGHT_LIMIT = 8
_online_reads = OnlineReadProxy()
_online_api = DirectAPIProxy()


SECURITY_HEADERS = {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
    'Strict-Transport-Security': 'max-age=31536000',
    'Permissions-Policy': 'camera=(self), microphone=(), autoplay=(self "https://www.youtube-nocookie.com")',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; worker-src 'self' blob:; frame-src https://www.youtube-nocookie.com; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}


def response(body=b'', status=200, headers=()):
    output_headers = Headers.new()
    for name, value in SECURITY_HEADERS.items():
        output_headers.set(name, value)
    for name, value in headers:
        output_headers.append(name, str(value))
    content = None if status in (204, 304) else to_js(body) if isinstance(body, bytes) else body
    return Response.new(content, to_js({'status': status, 'headers': output_headers}, dict_converter=Object.fromEntries))


def json_response(payload, status=200):
    return response(json.dumps(payload, ensure_ascii=False), status, [('Content-Type', 'application/json; charset=utf-8')])


def deployment_authorized(request, env):
    key = getattr(env, 'DEPLOYMENT_KEY', None)
    return isinstance(key, str) and len(key) >= 32 and hmac.compare_digest((request.headers.get('X-Deployment-Key') or '').encode('utf-8'), key.encode('utf-8'))


async def read_body(request, limit):
    if not request.body:
        return b''
    reader = request.body.getReader()
    chunks, size = [], 0
    try:
        while True:
            part = await reader.read()
            if part.done:
                return b''.join(chunks)
            size += part.value.byteLength
            if size > limit:
                await reader.cancel()
                raise ValueError('Request is too large.')
            chunks.append(bytes(part.value.to_py()))
    finally:
        reader.releaseLock()


def request_path(request):
    from urllib.parse import urlsplit
    url = urlsplit(request.url)
    return url.path + ('?' + url.query if url.query else '')


def parse_static_manifest(value):
    if value.startswith('zlib:'):
        import zlib
        value = zlib.decompress(base64.b64decode(value[5:], validate=True)).decode('utf-8')
    return json.loads(value)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        from urllib.parse import urlsplit
        canonical = urlsplit(self.env.PUBLIC_ORIGIN)
        incoming = urlsplit(request.url)
        if incoming.scheme != 'https' or incoming.netloc != canonical.netloc:
            return response(status=308, headers=[('Location', self.env.PUBLIC_ORIGIN + request_path(request))])
        path = request_path(request).split('?', 1)[0]
        deployment = path.startswith('/__deployment/')
        if deployment:
            if not deployment_authorized(request, self.env):
                return response('Not found.', 404)
        if not deployment and request.method in ('GET', 'HEAD') and not path.startswith('/api/'):
            return await self.asset(request, path)
        try:
            mode = storage_mode(self.env)
            # Explicit, optional recovery confirmation is separate from online work.
            if request.method == 'GET' and request_path(request) == '/api/me?backup=1':
                return await self._coordinate(request)
            direct = (not deployment and mode == 'sql' and eligible_operation(request)
                      and getattr(self.env, 'EVENT_MAINTENANCE', '0') != '1'
                      and getattr(self.env, 'EVENT_DIRECT_API', '1') == '1'
                      and getattr(self.env, 'PHOTO_PILOT', None) is not None
                      and getattr(self.env, 'APP_CONFIG', None) is not None)
            if direct:
                return await self._direct_operation(request)
            optimized = (not deployment and mode == 'sql' and eligible_read(request)
                         and getattr(self.env, 'EVENT_MAINTENANCE', '0') != '1'
                         and getattr(self.env, 'EVENT_DIRECT_READS', '1') == '1'
                         and getattr(self.env, 'PHOTO_PILOT', None) is not None)
            if optimized:
                return await _online_reads.fetch(request, self._read_transport,
                    self._coordinate, lambda value: response(value['body'], value['status'], value['headers']))
            result = await self._coordinate(request)
            if request.method == 'POST':
                _online_reads.observe(result)
            return result
        except PrimaryProtocolError:
            return json_response({'error': 'The SQL connection needs attention.'}, 503)
        except PrimaryUnavailable:
            return json_response({'error': 'The Cloudflare coordinator is unavailable.'}, 503)
        except ValueError as error:
            return json_response({'error': str(error)}, 400)

    async def _coordinate(self, request):
        # Only _fallback_operation may create the signed internal failover headers.
        if request.headers.get(FALLBACK_STAMP) or request.headers.get(FALLBACK_PROOF):
            return json_response({'error': 'Invalid internal request headers.'}, 400)
        return await self._fetch_coordinator(request)

    async def _fetch_coordinator(self, request):
        if getattr(self.env, 'EVENT_COORDINATOR_DISABLED', '0') == '1':
            raise PrimaryUnavailable('The optional coordinator is unavailable.')
        fetched = await self.env.STATE.getByName('main').fetch(request)
        # Python fetch wraps Headers as a case-sensitive mapping. Observe the
        # underlying JS response, retaining case-insensitive headers and cookies.
        return getattr(fetched, 'js_response', fetched)

    async def _direct_operation(self, request):
        try:
            config = json.loads(self.env.APP_CONFIG)
            if not isinstance(config, dict) or not re.fullmatch(r'[0-9a-fA-F]{64}', str(config.get('secret', ''))):
                raise ValueError()
        except (ValueError, TypeError):
            raise PrimaryProtocolError('Invalid event signing configuration.') from None
        envelope = request_envelope(request, b'')
        if request.method == 'GET':
            envelope['operation_id'] = None
        elif request.headers.get('Origin') and request.headers.get('Origin') != self.env.PUBLIC_ORIGIN.rstrip('/'):
            return json_response({'error': 'This request came from another website.'}, 403)
        denied = preflight_mutation(envelope, config, self.env.PUBLIC_ORIGIN, app.parse_session)
        if denied:
            return json_response({'error': denied[1]}, denied[0])
        if request.method == 'POST':
            try:
                body = await read_body(request, MAX_POST_BYTES)
            except ValueError:
                return json_response({'error': 'Request is too large.'}, 413)
            envelope['body_base64'] = base64.b64encode(body).decode('ascii')
        envelope['include_replica'] = False
        schedule = getattr(getattr(self, 'ctx', None), 'waitUntil', None)
        return await _online_api.handle(envelope, self._direct_transport,
            lambda value: self._fallback_operation(value, config),
            lambda value: response(value['body'], value['status'], value['headers']),
            schedule, self._maintain_backup)

    async def _direct_transport(self, kind, payload):
        from types import SimpleNamespace
        js = SimpleNamespace(Headers=Headers, Request=JSRequest, AbortController=AbortController,
                             Object=Object, to_js=to_js)
        mutation = payload['method'] == 'POST' and payload['path'] not in ('/api/lookup', '/api/scan/checkin')
        return await sql_transport(self.env, js, read_body, kind, payload, timeout=32 if mutation else 8)

    def _coordinator_request(self, envelope, *, maintenance=False, config=None):
        headers = Headers.new()
        for name, value in envelope['headers']:
            headers.set(name, value)
        headers.set('CF-Connecting-IP', envelope['client_ip'])
        method, path = ('GET', '/api/me') if maintenance else (envelope['method'], envelope['path'])
        options = {'method': method, 'headers': headers}
        if not maintenance and method == 'POST':
            headers.set('X-EventDesk-Operation-Id', envelope['operation_id'])
            options['body'] = to_js(base64.b64decode(envelope['body_base64'], validate=True))
            if path == '/api/scan/checkin':
                headers.set(FALLBACK_STAMP, envelope['recorded_at'])
                headers.set(FALLBACK_PROOF, sign_fallback_stamp(envelope, config['secret']))
        return JSRequest.new(self.env.PUBLIC_ORIGIN + path,
                             to_js(options, dict_converter=Object.fromEntries))

    async def _fallback_operation(self, envelope, config):
        return await self._fetch_coordinator(self._coordinator_request(envelope, config=config))

    async def _maintain_backup(self, envelope):
        return await self._coordinate(self._coordinator_request(envelope, maintenance=True))

    async def _read_transport(self, kind, payload):
        from types import SimpleNamespace
        js = SimpleNamespace(Headers=Headers, Request=JSRequest, AbortController=AbortController,
                             Object=Object, to_js=to_js)
        return await sql_transport(self.env, js, read_body, kind, payload, timeout=8)

    async def asset(self, request, path):
        key = 'index.html' if path == '/' else 'projector.html' if path in ('/projector', '/projector/') else path.removeprefix('/static/') if path.startswith('/static/') else None
        manifest = parse_static_manifest(self.env.STATIC_MANIFEST)
        item = manifest.get(key)
        if not item:
            return response('Not found.', 404)
        headers = Headers.new()
        for name, value in {**SECURITY_HEADERS, 'Content-Type': item['type']}.items():
            headers.set(name, value)
        if request.method == 'HEAD':
            return Response.new(None, to_js({'headers': headers}, dict_converter=Object.fromEntries))
        parts = await asyncio.gather(*(self.env.ASSETS.get(name, 'arrayBuffer') for name in item['keys']))
        if any(not hasattr(part, 'byteLength') for part in parts):
            return response('Site files are temporarily unavailable.', 503)
        content = Uint8Array.new(item['size'])
        offset = 0
        for part in parts:
            data = Uint8Array.new(part)
            content.set(data, offset)
            offset += data.length
        return Response.new(content, to_js({'headers': headers}, dict_converter=Object.fromEntries))


class EventDesk(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.initialized = False
        self.config = json.loads(env.APP_CONFIG)
        self._stats_cache = None
        self._stats_until = 0
        self._projector_cache = None
        self._projector_until = 0
        self._photo_inflight = 0

    def authorize_employee_photo(self, request: object, path: str) -> dict:
        """Use the same handler/session checks as other staff APIs, without I/O."""
        state = TransactionState()
        self.bind_database(state)
        cookie = request.headers.get('Cookie')
        headers = _request_headers([('Cookie', cookie)] if isinstance(cookie, str) else [], 0)
        handler = _make_handler(app, request.method, path, headers, b'',
                                request.headers.get('CF-Connecting-IP') or 'unknown', self.config)
        user = handler.authorize(roles=('checkin_staff', 'verification_staff'))
        employee_id = path.removeprefix('/api/employee-photo/')
        if user:
            if request.method != 'GET':
                handler.respond({'error': 'Method not allowed.'}, 405, {'Allow': 'GET'})
            elif '?' in request.url or not re.fullmatch(r'[0-9]{7}', employee_id):
                handler.fail('Invalid employee ID.', 400)
            elif not app.photo_request_allowed(user):
                handler.fail('Try again shortly.', 429)
            elif not app.employee_photo_exists(employee_id):
                handler.fail('No photo.', 404)
            else:
                if state.rollback_requested:
                    raise RuntimeError('Photo authorization rolled back.')
                return {'employee_id': employee_id}
        return {'status': handler.response_status, 'headers': handler.response_headers,
                'body': handler.wfile.getvalue()}

    async def fetch_employee_photo(self, employee_id: str) -> object:
        """Fetch through the private pilot binding after the SQL transaction ends."""
        service = getattr(self.env, 'PHOTO_PILOT', None)
        token = getattr(self.env, 'PHOTO_PILOT_ACCESS_TOKEN', None)
        if (not callable(getattr(service, 'fetch', None)) or not isinstance(token, str)
                or not 32 <= len(token) <= 512 or any(not 33 <= ord(char) <= 126 for char in token)
                or self._photo_inflight >= PHOTO_INFLIGHT_LIMIT):
            return json_response({'error': 'Photo unavailable.'}, 503)
        self._photo_inflight += 1
        controller, deadline_timer = None, None
        stage = 'controller'

        async def fetch_bounded():
            nonlocal stage
            stage = 'request'
            headers = Headers.new()
            for name, value in {'Authorization': 'Bearer ' + token, 'Accept': 'image/jpeg',
                                'Accept-Encoding': 'identity', 'User-Agent': 'EventDeskPhotoProxy/1.0'}.items():
                headers.set(name, value)
            incoming = JSRequest.new('https://eventdesk-photo-pilot.pages.dev/v1/employees/' + employee_id + '/photo',
                                     to_js({'method': 'GET', 'headers': headers, 'redirect': 'manual',
                                            'signal': controller.signal}, dict_converter=Object.fromEntries))
            stage = 'fetch'
            fetched = await service.fetch(incoming)
            # Python bindings return FetchResponse; use its documented raw JS
            # response for case-insensitive Headers and the bounded stream reader.
            upstream = getattr(fetched, 'js_response', fetched)
            stage = 'status'
            if upstream.status != 200 or upstream.redirected:
                return json_response({'error': 'No photo.' if upstream.status == 404 else 'Photo unavailable.'},
                                     404 if upstream.status == 404 and not upstream.redirected else 503)
            stage = 'content_type'
            if upstream.headers.get('Content-Type') != 'image/jpeg':
                raise ValueError('Invalid photo response.')
            stage = 'content_encoding'
            if (upstream.headers.get('Content-Encoding') or 'identity').lower() != 'identity':
                raise ValueError('Invalid photo response.')
            stage = 'content_length'
            length = upstream.headers.get('Content-Length')
            if length is not None and (not re.fullmatch(r'[0-9]{1,9}', length)
                                        or not 3 <= int(length) <= app.PHOTO_MAX_BYTES):
                raise ValueError('Invalid photo length.')
            stage = 'body'
            data = await read_body(upstream, app.PHOTO_MAX_BYTES)
            stage = 'body_length'
            if length is not None and len(data) != int(length):
                raise ValueError('Invalid photo data.')
            stage = 'jpeg'
            if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):
                raise ValueError('Invalid photo data.')
            stage = 'response'
            return response(data, headers=[('Content-Type', 'image/jpeg'), ('Vary', 'Cookie')])

        try:
            controller = AbortController.new()
            # Abort JS I/O independently of asyncio's cancellation acknowledgement.
            stage = 'deadline'
            deadline_timer = asyncio.get_running_loop().call_later(PHOTO_TIMEOUT_SECONDS, controller.abort)
            return await asyncio.wait_for(fetch_bounded(), timeout=PHOTO_TIMEOUT_SECONDS)
        except Exception as error:
            # Upstream messages and credentials never cross the public API boundary.
            print('Photo proxy failed:', stage, type(error).__name__)
            return json_response({'error': 'Photo unavailable.'}, 503)
        finally:
            try:
                if deadline_timer is not None:
                    deadline_timer.cancel()
                if controller is not None:
                    controller.abort()
            finally:
                self._photo_inflight -= 1

    async def employee_photo(self, request: object, path: str) -> object:
        ready = list(self.ctx.storage.sql.exec("SELECT value FROM _hosted_metadata WHERE key='migration_complete'"))
        if not ready:
            return json_response({'error': 'The employee migration is still in progress.'}, 503)
        authorized = {}
        try:
            self.ctx.storage.transactionSync(lambda: authorized.update(self.authorize_employee_photo(request, path)))
        except Exception:
            return json_response({'error': 'Photo unavailable.'}, 503)
        if 'employee_id' not in authorized:
            return response(authorized['body'], authorized['status'], authorized['headers'])
        return await self.fetch_employee_photo(authorized['employee_id'])

    def cached_stats(self):
        now = time.time()
        if self._stats_cache is not None and now < self._stats_until:
            return self._stats_cache
        stats = _uncached_stats()
        self._stats_cache, self._stats_until = stats, now + 30
        return stats

    def cached_projector_state(self):
        now = time.monotonic()
        if self._projector_cache is not None and now < self._projector_until:
            return self._projector_cache
        state = app.get_projector_state()
        self._projector_cache = state
        # The reveal phase changes with time; otherwise writes invalidate this cache.
        self._projector_until = now + (0.5 if state['phase'] in ('spinning', 'winner_selected') else 60)
        return state

    def bind_database(self, state):
        @contextmanager
        def connection():
            db = SQLiteConnection(self.ctx.storage.sql, state, bind_value=lambda value: to_js(value) if isinstance(value, bytes) else value)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()
        app.db_connection = connection
        app.get_stats = _uncached_stats
        app.load_config = lambda: self.config
        os.environ['PARTY_OPEN_STAFF'] = '1'
        os.environ['PARTY_COOKIE_SECURE'] = '1'
        os.environ['PARTY_PUBLIC_ORIGIN'] = self.env.PUBLIC_ORIGIN
        install_storage(app)

    def initialize(self):
        if self.initialized:
            return
        from cloudflare.qr_image import install as install_qr
        install_qr()
        def setup():
            state = TransactionState()
            self.bind_database(state)
            app.init_db()
            self.ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS _hosted_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            if state.rollback_requested:
                raise RuntimeError('Schema migration was rolled back.')
        self.ctx.storage.transactionSync(setup)
        self.initialized = True

    def _sql_runtime(self):
        runtime = getattr(self, '_event_runtime', None)
        if runtime is None:
            from types import SimpleNamespace
            runtime = HostedSQLRuntime(self, SimpleNamespace(Headers=Headers, Request=JSRequest,
                AbortController=AbortController, Object=Object, to_js=to_js), read_body)
            self._event_runtime = runtime
        return runtime

    def _get_sql_primary(self):
        return self._sql_runtime().primary

    async def _event_transport(self, kind, payload):
        return await self._sql_runtime().transport(kind, payload)

    async def fetch(self, request):
        try:
            if (getattr(self.env, 'EVENT_MAINTENANCE', '0') == '1'
                    and request_path(request).split('?', 1)[0].startswith('/api/')):
                return response('{"error":"Moving event data. Please try again shortly."}', 503,
                    [('Content-Type', 'application/json; charset=utf-8'), ('Retry-After', '10')])
            mode = storage_mode(self.env)
            if mode == 'sqlite' or request_path(request).startswith('/__deployment/'):
                return await self._fetch_sqlite(request)
            envelope = request_envelope(request, b'')
            denied = preflight_mutation(envelope, self.config, self.env.PUBLIC_ORIGIN, app.parse_session)
            if denied:
                return json_response({'error': denied[1]}, denied[0])
            try:
                body = await read_body(request, app.MAX_BODY)
            except ValueError:
                return json_response({'error': 'Request is too large.'}, 413)
            envelope['body_base64'] = base64.b64encode(body).decode('ascii')
            secret = self.config.get('secret') if isinstance(self.config, dict) else None
            envelope = apply_fallback_stamp(envelope, request.headers, secret)
            primary = self._get_sql_primary()
            if request.headers.get(FALLBACK_STAMP):
                result = await primary.handle_unavailable_checkin(envelope)
            else:
                result = await primary.handle(envelope)
            return response(result['body'], result['status'], result['headers'])
        except ValueError as error:
            if isinstance(error, PrimaryProtocolError):
                return json_response({'error': 'The SQL connection needs attention.'}, 503)
            return json_response({'error': str(error)}, 400)

    async def _fallback_event(self, envelope, queue_kind):
        path = envelope['path'].split('?', 1)[0]
        if path.startswith('/api/employee-photo/'):
            return {'status': 503, 'headers': [('Content-Type', 'application/json')],
                    'body': b'{"error":"Photo unavailable."}'}
        headers = Headers.new()
        for name, value in envelope['headers']:
            headers.append(name, value)
        headers.set('CF-Connecting-IP', envelope['client_ip'])
        options = {'method': envelope['method'], 'headers': headers}
        if envelope['method'] == 'POST':
            options['body'] = to_js(base64.b64decode(envelope['body_base64'], validate=True))
        incoming = JSRequest.new(self.env.PUBLIC_ORIGIN + envelope['path'],
                                 to_js(options, dict_converter=Object.fromEntries))
        return await self._fetch_sqlite(incoming, capture=True,
            queued_request=envelope if queue_kind == 'checkin' else None)

    async def _fetch_sqlite(self, request, *, capture=False, queued_request=None):
        def rejected(message, status):
            if capture:
                return {'status': status, 'headers': [('Content-Type', 'application/json; charset=utf-8')],
                        'body': json.dumps({'error': message}).encode()}
            return json_response({'error': message}, status)
        try:
            self.initialize()
        except Exception as error:
            if deployment_authorized(request, self.env):
                import traceback
                return json_response({'error': str(error), 'trace': traceback.format_exc()[-6000:]}, 500)
            return rejected('Server initialization failed.', 500)
        path = request_path(request)
        if path.split('?', 1)[0].startswith('/api/employee-photo/'):
            return await self.employee_photo(request, path)
        try:
            body = await read_body(request, app.MAX_BODY)
        except ValueError:
            return rejected('Request is too large.', 413)
        if path.startswith('/__deployment/'):
            if not deployment_authorized(request, self.env):
                return response('Not found.', 404)
            return self.deployment(path.split('?', 1)[0], request.method, body)
        ready = list(self.ctx.storage.sql.exec("SELECT value FROM _hosted_metadata WHERE key='migration_complete'"))
        if not ready:
            return rejected('The employee migration is still in progress.', 503)
        captured = {}
        def handle():
            state = TransactionState()
            self.bind_database(state)
            if request.method == 'GET' and path.split('?', 1)[0] == '/api/stats':
                app.get_stats = self.cached_stats
            names = ('Content-Type', 'Cookie', 'Origin', 'X-CSRF-Token', 'Host', 'Sec-Fetch-Site', 'Last-Event-ID')
            headers = [(name, value) for name in names if isinstance(value := request.headers.get(name), str)]
            if not any(name.lower() == 'host' for name, _ in headers):
                from urllib.parse import urlsplit
                headers.append(('Host', urlsplit(request.url).netloc))
            recorded_at = None
            if queued_request is not None:
                recorded_at = queued_request.get('recorded_at')
                if request.method != 'POST' or path.split('?', 1)[0] != '/api/scan/checkin' \
                        or not isinstance(recorded_at, str):
                    raise ValueError('Queued time is only available for check-ins.')
                stamp = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
                if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                    raise ValueError('Queued time must be UTC.')
            previous_now = app.now
            try:
                if recorded_at is not None:
                    # SQLite and SQL replay must use the same original check-in time.
                    app.now = lambda: recorded_at
                result = dispatch(app, request.method, path, headers, body,
                                  request.headers.get('CF-Connecting-IP') or 'unknown', self.config,
                                  projector_state_provider=self.cached_projector_state)
            finally:
                app.now = previous_now
            captured.update(result)
            if state.rollback_requested or result['status'] >= 400:
                raise RuntimeError('Request rolled back.')
            if queued_request is not None:
                from cloudflare.replica import ReplicaStore
                from eventdesk_storage.schema import TABLES
                replica_db = SQLiteConnection(self.ctx.storage.sql, state,
                    bind_value=lambda value: to_js(value) if isinstance(value, bytes) else value)
                wire = {'status': result['status'], 'headers': result['headers'],
                        'body_base64': base64.b64encode(result['body']).decode('ascii')}
                ReplicaStore(replica_db, TABLES).enqueue(queued_request, 'checkin', wire)
        try:
            self.ctx.storage.transactionSync(handle)
        except Exception:
            if not captured or captured['status'] < 400:
                return rejected('Server error. Please try again.', 500)
        if request.method == 'POST' and captured['status'] < 400:
            self._projector_cache = None
        if capture:
            return captured
        return response(captured['body'], captured['status'], captured['headers'])

    def deployment(self, path, method, body):
        state = TransactionState()
        self.bind_database(state)
        with app.db_connection() as db:
            if path == '/__deployment/runtime' and method == 'GET':
                import importlib
                import traceback
                packages = {}
                for name in ('openpyxl', 'et_xmlfile', 'qrcode'):
                    try:
                        importlib.import_module(name)
                        packages[name] = 'available'
                    except Exception:
                        packages[name] = traceback.format_exc()[-5000:]
                return json_response(packages)
            if path == '/__deployment/health' and method == 'GET':
                tables = ['employees', 'event_checkins', 'lucky_draw_participants', 'lucky_draw_winners', 'wristbands']
                return json_response({'counts': {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in tables},
                                      'complete': bool(db.execute("SELECT 1 FROM _hosted_metadata WHERE key='migration_complete'").fetchone())})
        if method != 'POST' or path not in ('/__deployment/import', '/__deployment/complete'):
            return response('Not found.', 404)
        payload = json.loads(body)
        def migrate():
            state = TransactionState()
            self.bind_database(state)
            with app.db_connection() as db:
                if db.execute("SELECT 1 FROM _hosted_metadata WHERE key='migration_complete'").fetchone():
                    raise ValueError('Migration is already complete.')
                if path.endswith('/complete'):
                    required = {'employees', 'event_checkins', 'lucky_draw_participants', 'lucky_draw_winners', 'wristbands'}
                    if set(payload.get('counts', {})) != required:
                        raise ValueError('All migration counts are required.')
                    for table, expected in payload['counts'].items():
                        if table not in ('employees', 'event_checkins', 'lucky_draw_participants', 'lucky_draw_winners', 'wristbands'):
                            raise ValueError('Invalid verification table.')
                        actual = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                        if actual != expected:
                            raise ValueError('Migration count mismatch for ' + table)
                    if db.execute('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Migration has broken references.')
                    db.execute("INSERT INTO _hosted_metadata VALUES ('migration_complete','1')")
                else:
                    table, columns, rows = payload['table'], payload['columns'], payload['rows']
                    if not re.fullmatch(r'[a-z_]+', table) or table.startswith('_'):
                        raise ValueError('Invalid migration table.')
                    actual = {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}
                    if not columns or any(column not in actual for column in columns) or len(rows) > 100:
                        raise ValueError('Invalid migration columns or batch size.')
                    names = ','.join('"' + column + '"' for column in columns)
                    updates = ','.join('"' + column + '"=excluded."' + column + '"' for column in columns)
                    sql = 'INSERT INTO ' + table + ' (' + names + ') VALUES (' + ','.join('?' for _ in columns) + ') ON CONFLICT DO UPDATE SET ' + updates
                    for row in rows:
                        values = [base64.b64decode(value['$bytes'], validate=True) if isinstance(value, dict) and '$bytes' in value else value for value in row]
                        db.execute(sql, values)
                if state.rollback_requested:
                    raise RuntimeError('Import rolled back.')
        try:
            self.ctx.storage.transactionSync(migrate)
        except Exception as error:
            return json_response({'error': str(error)}, 400)
        return json_response({'ok': True})
