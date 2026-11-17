"""Bounded, versioned messages for the private EventDesk application API."""
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit
import uuid


MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_HEADER_BYTES = 16384
GET_PATHS = frozenset('/api/' + name for name in (
    'me', 'stats', 'attendance', 'employees', 'wristbands', 'wristbands/qr',
    'import/history', 'draw/config', 'draw/eligible-ids', 'rewards', 'rewards/image',
    'projector/state', 'projector/prize-image', 'projector/events', 'winners',
    'settings', 'users', 'admin/draw-history', 'admin/activity', 'verifications',
    'export/attendance', 'export/winners', 'export/qr', 'export/report',
))
POST_PATHS = frozenset('/api/' + name for name in (
    'staff-session', 'login', 'logout', 'lookup', 'scan/checkin',
    'checkin/wristband-lost', 'scan/verify', 'draw/participants', 'rewards/draft',
    'rewards/draft/delete', 'rewards/publish', 'draw', 'projector/state',
    'projector/prize-image', 'winners/reroll', 'wristbands/generate', 'wristbands/action',
    'prizes', 'settings', 'users', 'users/update', 'admin/employees',
    'admin/attendance/remove', 'admin/draw-history/remove', 'import/inspect',
    'import/preview', 'import/commit', 'import',
))
REQUEST_HEADERS = frozenset(('cookie', 'origin', 'x-csrf-token', 'content-type',
                             'sec-fetch-site', 'last-event-id'))
RESPONSE_HEADERS = frozenset((
    'content-type', 'content-length', 'content-disposition', 'set-cookie',
    'cache-control', 'pragma', 'expires', 'vary', 'allow', 'retry-after',
    'x-content-type-options', 'x-frame-options', 'referrer-policy',
    'strict-transport-security', 'permissions-policy', 'content-security-policy',
))


class ProtocolError(ValueError):
    """A request cannot be safely represented by this protocol version."""


@dataclass(frozen=True)
class Command:
    operation_id: str | None
    method: str
    path: str
    headers: tuple
    body: bytes
    client_ip: str
    after_revision: int
    recorded_at: str | None = None
    replay: bool = False
    queue_proof: str | None = None
    receipt_only: bool = False
    cancel_if_missing: bool = False


@dataclass(frozen=True)
class SnapshotRequest:
    table: str | None = None
    offset: int = 0
    limit: int = 100
    revision: int | None = None


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError('Invalid integer.')
    return value


def _object(value, allowed, required):
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise ProtocolError('Invalid message fields.')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ProtocolError('Unsupported message version.')


def _pairs(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ProtocolError('Invalid request headers.')
    result, names, size = [], set(), 0
    for pair in value:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ProtocolError('Invalid request header.')
        name, text = pair
        if not isinstance(name, str) or not isinstance(text, str):
            raise ProtocolError('Invalid request header.')
        lower = name.lower()
        if lower not in REQUEST_HEADERS or lower in names or any(c in text for c in '\r\n\0'):
            raise ProtocolError('Invalid request header.')
        try:
            size += len(name.encode('utf-8')) + len(text.encode('utf-8'))
        except UnicodeError:
            raise ProtocolError('Invalid request header encoding.') from None
        if size > MAX_HEADER_BYTES:
            raise ProtocolError('Request headers are too large.')
        names.add(lower)
        result.append((name, text))
    return tuple(result)


def parse_command(value):
    allowed = {'schema_version', 'operation_id', 'method', 'path', 'headers',
               'body_base64', 'client_ip', 'after_revision', 'recorded_at',
               'replay', 'queue_proof', 'receipt_only', 'cancel_if_missing'}
    _object(value, allowed, {'schema_version', 'method', 'path', 'headers',
                             'body_base64', 'client_ip', 'after_revision'})
    method, path = value['method'], value['path']
    if method not in ('GET', 'POST') or not isinstance(path, str) or len(path) > 8192:
        raise ProtocolError('Invalid application route.')
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or '\\' in path or '#' in path:
        raise ProtocolError('Invalid application route.')
    try:
        parts = urlsplit(path)
    except ValueError:
        raise ProtocolError('Invalid application route.') from None
    permitted = GET_PATHS if method == 'GET' else POST_PATHS
    photo = method == 'GET' and re.fullmatch(r'/api/employee-photo/[0-9]{7}', parts.path)
    if parts.scheme or parts.netloc or (parts.path not in permitted and not photo) or len(parts.query) > 4096:
        raise ProtocolError('Invalid application route.')
    if photo and '?' in path:
        raise ProtocolError('Photo requests cannot contain a query.')
    operation_id = value.get('operation_id')
    if operation_id is not None:
        try:
            parsed = uuid.UUID(operation_id)
        except (TypeError, ValueError, AttributeError):
            raise ProtocolError('Invalid operation ID.') from None
        if parsed.version != 4 or str(parsed) != operation_id:
            raise ProtocolError('Invalid operation ID.')
    elif method == 'POST':
        raise ProtocolError('An operation ID is required.')
    encoded = value['body_base64']
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BODY_BYTES + 2) // 3):
        raise ProtocolError('Application request is too large.')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ProtocolError('Invalid request encoding.') from None
    if len(body) > MAX_BODY_BYTES or (method == 'GET' and body):
        raise ProtocolError('Invalid application request body.')
    try:
        client_ip = str(ipaddress.ip_address(value['client_ip']))
    except (TypeError, ValueError):
        raise ProtocolError('Invalid client address.') from None
    flags = [value.get(name, False) for name in ('replay', 'receipt_only', 'cancel_if_missing')]
    if any(type(flag) is not bool for flag in flags):
        raise ProtocolError('Invalid request mode.')
    replay, receipt_only, cancel_if_missing = flags
    recorded_at, proof = value.get('recorded_at'), value.get('queue_proof')
    if recorded_at is not None:
        try:
            stamp = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
        except (TypeError, ValueError, AttributeError):
            raise ProtocolError('Invalid recorded time.') from None
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ProtocolError('Recorded time must be UTC.')
        if method != 'POST' or parts.path != '/api/scan/checkin':
            raise ProtocolError('Recorded time is only available for check-ins.')
    if proof is not None and (not isinstance(proof, str) or not re.fullmatch(r'[0-9a-f]{64}', proof)):
        raise ProtocolError('Invalid queue proof.')
    if replay and (recorded_at is None or proof is None):
        raise ProtocolError('A queued check-in proof is required.')
    if proof is not None and not replay:
        raise ProtocolError('Queue proof requires replay mode.')
    if receipt_only and method != 'POST' or cancel_if_missing and not receipt_only:
        raise ProtocolError('Invalid receipt lookup mode.')
    return Command(operation_id, method, path, _pairs(value['headers']), body,
                   client_ip, _integer(value['after_revision']), recorded_at,
                   replay, proof, receipt_only, cancel_if_missing)


def parse_snapshot(value):
    _object(value, {'schema_version', 'table', 'offset', 'limit', 'revision'}, {'schema_version'})
    if 'table' not in value:
        if len(value) != 1:
            raise ProtocolError('Invalid snapshot discovery.')
        return SnapshotRequest()
    table = value['table']
    if not isinstance(table, str) or not re.fullmatch(r'[a-z_][a-z0-9_]{0,80}', table):
        raise ProtocolError('Invalid snapshot table.')
    return SnapshotRequest(table, _integer(value.get('offset', 0)),
                           _integer(value.get('limit', 100), 1, 1000),
                           _integer(value.get('revision')))


def command_hash(command):
    """Cursor/IP are transport context; original authentication remains bound."""
    value = {'method': command.method, 'path': command.path,
             'headers': sorted((name.lower(), text) for name, text in command.headers),
             'body_base64': base64.b64encode(command.body).decode('ascii')}
    return hashlib.sha256(dumps(value)).hexdigest()


def offline_proof(secret, command):
    value = {'operation_id': command.operation_id, 'method': command.method,
             'path': command.path, 'body_base64': base64.b64encode(command.body).decode('ascii'),
             'cookie': next((text for name, text in command.headers if name.lower() == 'cookie'), ''),
             'recorded_at': command.recorded_at}
    return hmac.new(bytes.fromhex(secret), b'eventdesk-offline-checkin-v1\n' + dumps(value),
                    hashlib.sha256).hexdigest()


def decode_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError('Request is too large.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError('Duplicate JSON field.')
            result[key] = value
        return result
    def constant(_value):
        raise ProtocolError('Invalid JSON value.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('Invalid JSON message.') from None
    if not isinstance(value, dict):
        raise ProtocolError('A JSON object is required.')
    return value


def dumps(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'), sort_keys=True).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError('Invalid response value.') from None


def encode_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$bytes': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ProtocolError('Unsupported database value.')


def decode_value(value):
    if isinstance(value, dict):
        if set(value) == {'$bytes'}:
            try:
                return base64.b64decode(value['$bytes'], validate=True)
            except (ValueError, TypeError, binascii.Error):
                raise ProtocolError('Invalid binary database value.') from None
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def encode_response(value):
    status = _integer(value.get('status'), 100, 599)
    headers = []
    for name, text in value.get('headers', ()):
        if not isinstance(name, str) or not isinstance(text, str) or any(c in text for c in '\r\n\0'):
            raise ProtocolError('Invalid response header.')
        if name.lower() in RESPONSE_HEADERS:
            headers.append([name, text])
    body = value.get('body', b'')
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES * 3 // 4:
        raise ProtocolError('Application response is too large.')
    return {'status': status, 'headers': headers,
            'body_base64': base64.b64encode(body).decode('ascii')}
