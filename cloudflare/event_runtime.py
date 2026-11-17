"""Workers-specific transport and transaction boundaries for SQL-primary mode."""
import asyncio
import base64
from datetime import datetime, timezone
import ipaddress
import inspect
import json
import re
import uuid

from .event_primary import PrimaryProtocolError, PrimaryUnavailable, SQLPrimary
from .replica import ReplicaStore
from .sqlite_adapter import SQLiteConnection, TransactionState
from eventdesk_storage.schema import TABLES


MAX_ENVELOPE_BYTES = 12 * 1024 * 1024
MAX_REPLY_BYTES = 24 * 1024 * 1024
FORWARDED_HEADERS = ('Content-Type', 'Cookie', 'Origin', 'X-CSRF-Token', 'Sec-Fetch-Site', 'Last-Event-ID')


def storage_mode(env):
    mode = getattr(env, 'EVENT_STORAGE', 'sql')
    if mode not in ('sql', 'sqlite'):
        raise PrimaryProtocolError('Invalid event storage configuration.')
    return mode


def request_envelope(request, body):
    from urllib.parse import urlsplit
    method = request.method
    if method not in ('GET', 'POST'):
        raise ValueError('Method not allowed.')
    supplied = request.headers.get('X-EventDesk-Operation-Id') or None
    if method == 'POST' and not supplied:
        raise ValueError('Refresh the page before saving changes.')
    if supplied:
        try:
            identity = uuid.UUID(supplied)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Invalid request ID.') from None
        if identity.version != 4 or str(identity) != supplied:
            raise ValueError('Invalid request ID.')
    path = urlsplit(request.url)
    address = request.headers.get('CF-Connecting-IP') or '127.0.0.1'
    try:
        address = str(ipaddress.ip_address(address))
    except ValueError:
        raise ValueError('Invalid client address.') from None
    envelope = {'schema_version': 1, 'operation_id': supplied, 'method': method,
                'path': path.path + ('?' + path.query if path.query else ''),
                'headers': [[name, value] for name in FORWARDED_HEADERS
                            if isinstance(value := request.headers.get(name), str)],
                'body_base64': base64.b64encode(body).decode('ascii'), 'client_ip': address,
                'after_revision': 0}
    if method == 'POST' and path.path == '/api/scan/checkin':
        envelope['recorded_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return envelope


def _json_pairs(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise PrimaryProtocolError('Duplicate SQL response field.')
        value[name] = item
    return value


class HostedSQLRuntime:
    def __init__(self, owner, js, read_body):
        self.owner, self.js, self.read_body = owner, js, read_body
        config = getattr(owner, 'config', None)
        if (not isinstance(config, dict) or not isinstance(config.get('secret'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{64}', config['secret'])):
            raise PrimaryProtocolError('Invalid event signing configuration.')
        self.primary = SQLPrimary(self.transport, self.atomic, self.fallback, TABLES,
                                  config['secret'], diagnostic=lambda category: print(category))
        self.replica_initialized = False

    async def transport(self, kind, payload):
        if kind not in ('request', 'snapshot'):
            raise PrimaryProtocolError('Invalid SQL service route.')
        env = self.owner.env
        service, token = getattr(env, 'PHOTO_PILOT', None), getattr(env, 'PHOTO_PILOT_ACCESS_TOKEN', None)
        if (service is None or not isinstance(token, str) or not 32 <= len(token) <= 512
                or not re.fullmatch(r'[!-~]+', token) or len(set(token)) < 12):
            raise PrimaryProtocolError('SQL service is not configured.')
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_ENVELOPE_BYTES:
            raise PrimaryProtocolError('SQL request is too large.')
        headers = self.js.Headers.new()
        for name, value in (('Authorization', 'Bearer ' + token), ('Content-Type', 'application/json'),
                            ('Accept-Encoding', 'identity')):
            headers.set(name, value)
        controller = self.js.AbortController.new()
        loop = asyncio.get_running_loop()
        timer = loop.call_later(32, controller.abort)
        async def fetch():
            incoming = self.js.Request.new('https://eventdesk-photo-pilot.pages.dev/v1/event/' + kind,
                self.js.to_js({'method': 'POST', 'headers': headers, 'body': raw,
                               'redirect': 'manual', 'signal': controller.signal}, dict_converter=self.js.Object.fromEntries))
            fetched = await service.fetch(incoming)
            result = getattr(fetched, 'js_response', fetched)
            if result.status in (400, 401, 403, 404, 405, 409, 413, 415):
                raise PrimaryProtocolError('SQL service rejected its configuration or request.')
            if result.status >= 500:
                raise PrimaryUnavailable('SQL service is unavailable.')
            content_type = result.headers.get('Content-Type') or ''
            encoding = result.headers.get('Content-Encoding') or 'identity'
            if result.status != 200:
                raise PrimaryProtocolError('Invalid SQL service status.')
            if getattr(result, 'redirected', False):
                raise PrimaryProtocolError('Unexpected SQL service redirect.')
            if not re.fullmatch(r'application/json(?:;\s*charset=utf-8)?', content_type, re.I):
                raise PrimaryProtocolError('Invalid SQL service media type.')
            if encoding not in (None, '', 'identity'):
                raise PrimaryProtocolError('Invalid SQL service encoding.')
            length = result.headers.get('Content-Length') or None
            if length is not None and (not re.fullmatch(r'[0-9]{1,9}', length) or int(length) > MAX_REPLY_BYTES):
                raise PrimaryProtocolError('SQL service response is too large.')
            try:
                data = await self.read_body(result, MAX_REPLY_BYTES)
                if length is not None and len(data) != int(length):
                    raise PrimaryProtocolError('Incomplete SQL service response.')
                return json.loads(data, object_pairs_hook=_json_pairs,
                                  parse_constant=lambda _text: (_ for _ in ()).throw(PrimaryProtocolError('Invalid SQL JSON.')))
            except (ValueError, UnicodeError) as error:
                raise PrimaryProtocolError('Invalid SQL service JSON.') from error
        try:
            return await asyncio.wait_for(fetch(), timeout=32)
        except (PrimaryUnavailable, PrimaryProtocolError):
            raise
        except (TimeoutError, OSError) as error:
            raise PrimaryUnavailable('SQL service is unavailable.') from error
        except Exception as error:
            # JS fetch exceptions contain private network details; keep them local.
            raise PrimaryUnavailable('SQL service is unavailable.') from error
        finally:
            timer.cancel()
            controller.abort()

    def atomic(self, action):
        owner = self.owner
        # This runs only to prepare/access the fallback; no await crosses it.
        # SQL-primary reads remain available if a free-tier backup limit fails.
        if not self.replica_initialized:
            owner.initialize()
        state, captured = TransactionState(), {}
        def run():
            db = SQLiteConnection(owner.ctx.storage.sql, state,
                bind_value=lambda value: self.js.to_js(value) if isinstance(value, bytes) else value)
            store = ReplicaStore(db, TABLES)
            if not self.replica_initialized:
                store.initialize()
                db.execute('''CREATE TABLE IF NOT EXISTS _eventdesk_projector_images
                    (image_id TEXT PRIMARY KEY,content_type TEXT NOT NULL,image BLOB NOT NULL)''')
            value = action(store)
            if inspect.isawaitable(value):
                closer = getattr(value, 'close', None) or getattr(value, 'cancel', None)
                if closer is not None:
                    closer()
                raise TypeError('Fallback transactions must remain synchronous.')
            captured['value'] = value
            if state.rollback_requested:
                raise RuntimeError('Fallback transaction rolled back.')
        owner.ctx.storage.transactionSync(run)
        self.replica_initialized = True
        return captured['value']

    async def fallback(self, envelope, queue_kind):
        return await self.owner._fallback_event(envelope, queue_kind)
