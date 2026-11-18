"""Private loopback event backend, reached through the existing PhotoPilot API."""
import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from cloudflare.handler_adapter import _make_handler, _request_headers, dispatch
from cloudflare.hosted_storage import install as install_storage
from eventdesk_storage.schema import validate_schema
from .event_protocol import (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, ProtocolError,
                             command_hash, decode_json, dumps, encode_response,
                             offline_proof, parse_command, parse_snapshot)
from .event_store import RevisionConflict, SqlServerStore


_APP_LOCK = threading.RLock()
_CHECKIN_ROLES = frozenset(('admin', 'staff', 'checkin_staff'))


class _ApplicationRejected(Exception):
    def __init__(self, response):
        self.response = response


class _ReplayApp:
    def __init__(self, module, principal):
        self.module = module
        class ReplayHandler(module.PartyHandler):
            def get_user(self):
                return dict(principal)
        self.PartyHandler = ReplayHandler

    def __getattr__(self, name):
        return getattr(self.module, name)


class EventEngine:
    """One atomic SQL transaction owns application work and its operation receipt."""
    def __init__(self, store, app_module, config, public_origin, *, photo_fetch=None):
        self.store, self.app, self.config = store, app_module, validate_app_config(config)
        parsed = urlsplit(public_origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('An HTTPS application origin is required.')
        self.origin = public_origin.rstrip('/')
        self.host = parsed.netloc
        self.photo_fetch = photo_fetch or self._photo_unavailable

    def _photo_unavailable(self, _employee_id):
        raise self.app.EmployeePhotoError()

    @contextmanager
    def _bind(self, tx, recorded_at=None):
        names = ('db_connection', 'load_config', 'get_projector_prize_image',
                 'update_projector_prize_image', 'now', 'fetch_employee_photo')
        previous = {name: getattr(self.app, name) for name in names}
        environment = {'PARTY_OPEN_STAFF': '1', 'PARTY_COOKIE_SECURE': '1', 'PARTY_PUBLIC_ORIGIN': self.origin}
        prior_environment = {name: os.environ.get(name) for name in environment}
        @contextmanager
        def connection():
            yield tx.db
        try:
            self.app.db_connection = connection
            self.app.load_config = lambda: self.config
            self.app.fetch_employee_photo = self.photo_fetch
            if recorded_at is not None:
                self.app.now = lambda: recorded_at
            os.environ.update(environment)
            install_storage(self.app)
            yield
        finally:
            for name, value in previous.items():
                setattr(self.app, name, value)
            for name, value in prior_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def _replay_principal(self, command, tx):
        if not hmac.compare_digest(offline_proof(self.config['secret'], command), command.queue_proof):
            raise ProtocolError('Invalid queued check-in proof.')
        recorded = datetime.fromisoformat(command.recorded_at.replace('Z', '+00:00'))
        age = datetime.now(timezone.utc).timestamp() - recorded.timestamp()
        if not -300 <= age <= 7 * 86400:
            raise ProtocolError('Queued check-in time is outside the recovery window.')
        cookie_header = next((value for name, value in command.headers if name.lower() == 'cookie'), '')
        try:
            cookie = SimpleCookie(cookie_header)
            token = cookie['party_session'].value
            payload, signature = token.split('.')
            expected = hmac.new(bytes.fromhex(self.config['secret']), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            if type(claims.get('exp')) is not int or claims['exp'] < recorded.timestamp() \
                    or claims.get('role') not in _CHECKIN_ROLES \
                    or not all(isinstance(claims.get(name), str) for name in ('csrf', 'sid')):
                raise ValueError()
        except (ValueError, KeyError, UnicodeError, TypeError, CookieError, binascii.Error):
            raise ProtocolError('The queued check-in session is not valid.') from None
        if claims.get('account_type') == 'named':
            row = tx.db.execute('SELECT username,display_name,role,active FROM users '
                                'WHERE username=? COLLATE NOCASE', (claims.get('username'),)).fetchone()
            if not row or not row['active'] or row['role'] not in _CHECKIN_ROLES:
                raise ProtocolError('The queued check-in account is no longer active.')
            claims = {**claims, 'username': row['username'], 'display_name': row['display_name'], 'role': row['role']}
        elif claims.get('account_type') != 'legacy':
            raise ProtocolError('The queued check-in account type is not valid.')
        elif claims.get('username') not in ('legacy-admin', 'legacy-staff'):
            raise ProtocolError('The queued check-in account is not valid.')
        session = tx.db.execute('SELECT ended_at FROM staff_sessions WHERE session_id=?', (claims['sid'],)).fetchone()
        if session and session['ended_at']:
            raise ProtocolError('The queued check-in session has been revoked.')
        return claims

    def _response(self, tx, command, value, *, receipt_found=False, reserve_bytes=0):
        base = {'schema_version': 1, 'operation_id': command.operation_id,
                'after_revision': command.after_revision, 'revision': tx.revision,
                'receipt_found': receipt_found, 'cancelled': False, **value}
        available = MAX_RESPONSE_BYTES - len(dumps(base)) - 512 - reserve_bytes
        if available < 0:
            raise ProtocolError('Application response is too large.')
        if not command.include_replica:
            return {**base, 'changes': [], 'replica_complete': False, 'snapshot_required': False}
        changes, complete, needs_snapshot = tx.replication(command.after_revision, available)
        return {**base, 'changes': changes, 'replica_complete': complete, 'snapshot_required': needs_snapshot}

    def _existing(self, tx, command, digest):
        receipt = tx.receipt(command.operation_id) if command.method == 'POST' else None
        if not receipt:
            return None
        if not hmac.compare_digest(receipt['request_hash'], digest):
            value = encode_response({'status': 409, 'headers': [('Content-Type', 'application/json')],
                                     'body': b'{"error":"Operation ID already used for a different request."}'})
            return self._response(tx, command, value, receipt_found=True)
        response = receipt['response']
        if command.replay and not command.receipt_only:
            try:
                principal = self._replay_principal(command, tx)
            except ProtocolError:
                # A committed receipt still resolves the old operation after
                # session revocation. It never authorizes a new time adjustment.
                return self._response(tx, command, response, receipt_found=True)
            response, changed = self._reconcile_replay(tx, command, response, principal, existing=True)
            if changed or response != receipt['response']:
                revision = tx.finish(None, None, {})
                tx._query('UPDATE ' + tx._name('EventOperationReceipt')
                          + ' SET response_json=?,revision=? WHERE operation_id=?',
                          (dumps(response).decode(), revision, command.operation_id))
        return self._response(tx, command, response, receipt_found=True)

    @staticmethod
    def _utc_time(value):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError('Saved check-in time is not valid.') from None
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ProtocolError('Saved check-in time must be UTC.')
        return stamp

    def _reconcile_replay(self, tx, command, response, principal, *, existing=False):
        """A verified offline scan may predate a separately accepted SQL scan."""
        if response['status'] != 200 or command.path != '/api/scan/checkin':
            return response, False
        payload = json.loads(base64.b64decode(response['body_base64']))
        person = payload.get('person')
        if not isinstance(person, dict) or type(person.get('id')) is not int:
            return response, False
        removed = tx.db.execute('SELECT removed_at FROM attendance_removals WHERE employee_id=?',
                                (person['id'],)).fetchall()
        if any(self._utc_time(row['removed_at']) >= self._utc_time(command.recorded_at) for row in removed):
            if existing:
                return response, False  # Resolve the old receipt, preserving the administrator's later reset.
            raise _ApplicationRejected(encode_response({'status': 409,
                'headers': [('Content-Type', 'application/json')],
                'body': b'{"error":"This queued check-in was removed by an administrator.","code":"attendance_removed"}'}))
        row = tx.db.execute('''SELECT e.checked_in_at AS employee_time,c.checked_in_at AS attendance_time
            FROM employees e JOIN event_checkins c ON c.employee_id=e.id WHERE e.id=?''',
            (person['id'],)).fetchone()
        if row is None:
            return response, False  # A receipt cannot recreate attendance removed by an administrator.
        times = [row['attendance_time'], row['employee_time'], command.recorded_at]
        earliest = min((value for value in times if value is not None), key=self._utc_time)
        changed = row['attendance_time'] != earliest or row['employee_time'] != earliest
        if changed:
            self._update_replay_time(tx, command, principal, person['id'], row['attendance_time'], earliest)
        updated = {**payload, 'person': {**person, 'checked_in_at': earliest}}
        if isinstance(payload.get('checkin'), dict):
            updated['checkin'] = {**payload['checkin'], 'checked_in_at': earliest}
        body = json.dumps(updated, ensure_ascii=False).encode('utf-8')
        headers = [(name, value) for name, value in response['headers'] if name.lower() != 'content-length']
        return encode_response({'status': response['status'], 'headers': headers, 'body': body}), changed

    def _update_replay_time(self, tx, command, principal, employee_id, previous, earliest):
        tx.db.execute('UPDATE event_checkins SET checked_in_at=? WHERE employee_id=?', (earliest, employee_id))
        tx.db.execute('UPDATE employees SET checked_in_at=? WHERE id=?', (earliest, employee_id))
        log = tx.db.execute('''SELECT id FROM event_log WHERE employee_id=? AND action='checkin'
            AND occurred_at=? ORDER BY id DESC LIMIT 1''', (employee_id, previous)).fetchone()
        if log:
            tx.db.execute('UPDATE event_log SET occurred_at=? WHERE id=?', (earliest, log['id']))
        self.app.audit(tx.db, principal, 'attendance.replay_time_reconciled', 'employee', employee_id,
                       {'previous_checked_in_at': previous, 'checked_in_at': earliest,
                        'operation_id': command.operation_id})

    def handle(self, payload):
        command = parse_command(payload)
        if command.method == 'GET' and command.path.startswith('/api/employee-photo/'):
            return self._photo(command)
        digest = command_hash(command)
        with _APP_LOCK:
            try:
                with self.store.transaction() as tx:
                    if command.after_revision > tx.revision:
                        raise RevisionConflict('Replica revision is ahead of SQL.')
                    existing = self._existing(tx, command, digest)
                    if existing is not None:
                        return existing
                    if command.receipt_only:
                        return self._cancel_or_missing(tx, command, digest)
                    principal = self._replay_principal(command, tx) if command.replay else None
                    with self._bind(tx, command.recorded_at if command.replay else None):
                        application = _ReplayApp(self.app, principal) if command.replay else self.app
                        result = dispatch(application, command.method, command.path,
                                          [*command.headers, ('Host', self.host)], command.body,
                                          command.client_ip, self.config)
                    response = encode_response(result)
                    if response['status'] >= 400 or tx.db.rollback_requested:
                        raise _ApplicationRejected(response)
                    if command.replay:
                        response, _ = self._reconcile_replay(tx, command, response, principal)
                    tx.finish(command.operation_id if command.method == 'POST' else None, digest, response)
                    return self._response(tx, command, response)
            except _ApplicationRejected as rejected:
                # The failed business transaction has already rolled back. Keep its
                # error receipt separately so a retry cannot become another action.
                with self.store.transaction() as tx:
                    existing = self._existing(tx, command, digest)
                    if existing is not None:
                        return existing
                    tx.finish(command.operation_id if command.method == 'POST' else None, digest, rejected.response)
                    return self._response(tx, command, rejected.response)

    @staticmethod
    def _captured(handler):
        return {'status': handler.response_status, 'headers': list(handler.response_headers),
                'body': handler.wfile.getvalue()}

    def _photo(self, command):
        """Authorize in SQL, then release both transaction and app lock before I/O."""
        employee_id = command.path.removeprefix('/api/employee-photo/')
        with _APP_LOCK:
            try:
                with self.store.transaction() as tx, self._bind(tx):
                    if command.after_revision > tx.revision:
                        raise RevisionConflict('Replica revision is ahead of SQL.')
                    headers = _request_headers([*command.headers, ('Host', self.host)], 0)
                    handler = _make_handler(self.app, 'GET', command.path, headers, b'',
                                            command.client_ip, self.config)
                    user = handler.authorize(roles=('checkin_staff', 'verification_staff'))
                    if user:
                        if not self.app.photo_request_allowed(user):
                            handler.fail('Try again shortly.', 429)
                        elif not self.app.employee_photo_exists(employee_id):
                            handler.fail('No photo.', 404)
                        else:
                            handler.response_status = 200
                    if handler.response_status != 200:
                        raise _ApplicationRejected(encode_response(self._captured(handler)))
                    tx.finish(None, None, {})
                    result = self._response(tx, command, {},
                                            reserve_bytes=4 * ((self.app.PHOTO_MAX_BYTES + 2) // 3) + 4096)
            except _ApplicationRejected as rejected:
                with self.store.transaction() as tx:
                    return self._response(tx, command, rejected.response)
        try:
            photo = self.photo_fetch(employee_id)
            handler.send_bytes(photo, 'image/jpeg', extra_headers={'Vary': 'Cookie'})
        except self.app.EmployeePhotoError as error:
            handler.fail(str(error), error.status)
        return {**result, **encode_response(self._captured(handler))}

    def _cancel_or_missing(self, tx, command, digest):
        status = 409 if command.cancel_if_missing else 404
        response = encode_response({'status': status, 'headers': [('Content-Type', 'application/json')],
                                    'body': b'{"error":"Previous request did not finish. Try again."}'})
        if command.cancel_if_missing:
            response = {**response, 'cancelled': True}
            tx.finish(command.operation_id, digest, response)
        return self._response(tx, command, response)

    def snapshot(self, payload):
        request = parse_snapshot(payload)
        with _APP_LOCK, self.store.transaction() as tx:
            return tx.snapshot(request)


def validate_app_config(value):
    if not isinstance(value, dict) or not isinstance(value.get('secret'), str) \
            or not re.fullmatch(r'[0-9a-fA-F]{64}', value['secret']):
        raise ValueError('Invalid application configuration.')
    for key in ('admin_password', 'staff_password'):
        entry = value.get(key)
        if not isinstance(entry, dict) or not re.fullmatch(r'[0-9a-fA-F]{32}', str(entry.get('salt', ''))) \
                or not re.fullmatch(r'[0-9a-fA-F]{64}', str(entry.get('hash', ''))):
            raise ValueError('Invalid application credentials.')
    return dict(value)


def validate_api_token(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512 \
            or not re.fullmatch(r'[!-~]+', token) or len(set(token)) < 12:
        raise ValueError('Invalid event API token.')
    return token


def local_photo_fetch(app_module, token, *, connection_factory=HTTPConnection):
    """Photos stay on the same API listener; this client never follows redirects."""
    def fetch(employee_id):
        if not isinstance(employee_id, str) or not re.fullmatch(r'[0-9]{7}', employee_id):
            raise app_module.EmployeePhotoError(400)
        if not app_module.PHOTO_PROXY_SLOTS.acquire(blocking=False):
            raise app_module.EmployeePhotoError()
        connection, response, timer = None, None, None
        deadline = time.monotonic() + 8
        try:
            connection = connection_factory('127.0.0.1', 8765, timeout=8)
            connection.connect()
            # HTTPConnection may detach a closing socket after getresponse().
            # Retain that exact socket for deadline cancellation of a body read.
            channel = connection.sock
            def abort_channel():
                if channel is not None:
                    try:
                        channel.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            timer = threading.Timer(max(0, deadline - time.monotonic()), abort_channel)
            timer.daemon = True
            timer.start()
            connection.request('GET', '/v1/employees/' + employee_id + '/photo',
                               headers={'Authorization': 'Bearer ' + token,
                                        'Accept': 'image/jpeg', 'Accept-Encoding': 'identity',
                                        'Connection': 'close'})
            response = connection.getresponse()
            if response.status == 404:
                raise app_module.EmployeePhotoError(404)
            return app_module._read_employee_photo(response, deadline)
        except app_module.EmployeePhotoError:
            raise
        except Exception:
            raise app_module.EmployeePhotoError() from None
        finally:
            if timer is not None:
                timer.cancel()
            try:
                if response is not None:
                    response.close()
            finally:
                try:
                    if connection is not None:
                        connection.close()
                finally:
                    app_module.PHOTO_PROXY_SLOTS.release()
    return fetch


class EventHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, address, engine, token):
        if address[0] != '127.0.0.1':
            raise ValueError('The event backend must listen on loopback only.')
        self.engine, self.api_token = engine, validate_api_token(token)
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(address, _EventHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _EventHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, *_args):
        pass

    def _reply(self, status, value):
        body = dumps(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        self.close_connection = True

    def _authorized(self):
        headers = self.headers.get_all('Authorization', [])
        supplied = headers[0] if len(headers) == 1 else ''
        expected = 'Bearer ' + self.server.api_token
        if not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
            self._reply(401, {'error': 'Unauthorized.'})
            return False
        return True

    def do_GET(self):
        if self._authorized():
            self._reply(200, {'status': 'ok'}) if self.path == '/healthz' else self._reply(404, {'error': 'Not found.'})

    def do_POST(self):
        self.connection.settimeout(15)
        if not self._authorized():
            return
        if self.path == '/__service/shutdown':
            if self.headers.get_all('Content-Length', []) != ['0'] \
                    or self.headers.get('Transfer-Encoding') is not None:
                return self._reply(400, {'error': 'An empty request body is required.'})
            self._reply(202, {'status': 'stopping'})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path not in ('/v1/event/request', '/v1/event/snapshot'):
            return self._reply(404, {'error': 'Not found.'})
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]) \
                or self.headers.get('Transfer-Encoding') is not None:
            return self._reply(400, {'error': 'Invalid request framing.'})
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            return self._reply(413, {'error': 'Request is too large.'})
        if self.headers.get_content_type() != 'application/json':
            return self._reply(415, {'error': 'JSON is required.'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ProtocolError('Incomplete request.')
            payload = decode_json(raw)
            result = self.server.engine.handle(payload) if self.path.endswith('/request') else self.server.engine.snapshot(payload)
            if len(dumps(result)) > MAX_RESPONSE_BYTES:
                raise ProtocolError('Response is too large.')
            self._reply(200, result)
        except RevisionConflict:
            self._reply(409, {'error': 'Event revision changed. Retry synchronization.'})
        except ProtocolError as error:
            self._reply(400, {'error': str(error)})
        except Exception as error:
            # A class name is sufficient to diagnose availability without leaking
            # SQL statements, credentials, request data, or employee records.
            print('Event backend unavailable:', type(error).__name__, file=sys.stderr)
            self._reply(503, {'error': 'Event service unavailable.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-stdin', action='store_true', required=True)
    parser.parse_args()
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('Configuration is too large.')
    settings = decode_json(raw)
    connection_string = settings.get('ConnectionString')
    if not isinstance(connection_string, str) or not 1 <= len(connection_string) <= 8192 \
            or not re.match(r'\s*DRIVER\s*=', connection_string, re.I):
        raise ValueError('A prepared ODBC connection string is required.')
    token = validate_api_token(settings.get('ApiToken'))
    config = validate_app_config(settings.get('AppConfig'))
    schema = validate_schema(settings.get('Schema', 'eventdesk'))
    import app
    # The trusted launcher converts .NET SQL settings with OdbcConnectionStringBuilder.
    # Its prepared string is never placed in argv, environment variables, or logs.
    store = SqlServerStore(connection_string, schema=schema)
    engine = EventEngine(store, app, config, settings.get('PublicOrigin', ''),
                         photo_fetch=local_photo_fetch(app, token))
    store.initialize()
    server = EventHTTPServer(('127.0.0.1', 8766), engine, token)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print('Event backend could not start:', type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
