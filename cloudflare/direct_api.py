"""SQL-first event operations with optional backup and authenticated failover."""
from datetime import datetime, timezone
import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit

from .event_primary import PrimaryProtocolError, PrimaryUnavailable, SQLPrimary, _failure
from .read_proxy import OnlineReadProxy, DIRECT_PATHS
from .replica import intent_hash


POST_PATHS = frozenset('/api/' + name for name in (
    'staff-session', 'login', 'logout', 'lookup', 'scan/checkin', 'draw',
    'projector/state', 'winners/reroll', 'checkin/wristband-lost', 'scan/verify',
))
IMAGE_PATHS = frozenset(('/api/rewards/image', '/api/projector/prize-image'))
FALLBACK_POSTS = frozenset(('/api/staff-session', '/api/login', '/api/lookup', '/api/scan/checkin'))
FALLBACK_STAMP = 'X-EventDesk-Fallback-Stamp'
FALLBACK_PROOF = 'X-EventDesk-Fallback-Proof'
MAX_POST_BYTES = 16 * 1024
MAINTENANCE_SECONDS = 60
QUOTA_COOLDOWN_SECONDS = 300
OUTAGE_SECONDS = 5


def eligible_operation(request):
    if request.method not in ('GET', 'POST'):
        return False
    try:
        parts = urlsplit(request.url)
    except ValueError:
        return False
    if (len(parts.path) + len(parts.query) > 8192 or len(parts.query) > 4096
            or parts.fragment or '\\' in parts.path
            or any(ord(c) < 32 or ord(c) == 127 for c in parts.path + parts.query)):
        return False
    if request.method == 'POST':
        return parts.path in POST_PATHS and not parts.query
    photo = re.fullmatch(r'/api/employee-photo/[0-9]{7}', parts.path)
    return parts.path in DIRECT_PATHS | IMAGE_PATHS or bool(photo and not parts.query)


def _secret_bytes(secret):
    if not isinstance(secret, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', secret):
        raise PrimaryProtocolError('Invalid event signing configuration.')
    return bytes.fromhex(secret)


def sign_fallback_stamp(envelope, secret):
    stamp = envelope.get('recorded_at')
    if envelope.get('method') != 'POST' or envelope.get('path') != '/api/scan/checkin' or not isinstance(stamp, str):
        raise ValueError('Only check-in failover can carry an original timestamp.')
    message = 'eventdesk-direct-fallback-v1\n' + intent_hash(envelope) + '\n' + stamp
    return hmac.new(_secret_bytes(secret), message.encode(), hashlib.sha256).hexdigest()


def apply_fallback_stamp(envelope, headers, secret):
    stamp, proof = headers.get(FALLBACK_STAMP) or None, headers.get(FALLBACK_PROOF) or None
    if stamp is None and proof is None:
        return envelope
    if (not isinstance(stamp, str) or len(stamp) > 64 or not isinstance(proof, str)
            or not re.fullmatch(r'[0-9a-f]{64}', proof)):
        raise ValueError('Invalid check-in failover proof.')
    try:
        recorded = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        age = datetime.now(timezone.utc).timestamp() - recorded.timestamp()
        if recorded.utcoffset() is None or recorded.utcoffset().total_seconds() != 0 or not -5 <= age <= 300:
            raise ValueError()
        value = {**envelope, 'recorded_at': stamp}
        if not hmac.compare_digest(sign_fallback_stamp(value, secret), proof):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Invalid check-in failover proof.') from None
    return value


class DirectAPIProxy(OnlineReadProxy):
    """Only valid SQL replies confirm online operations; backup is independent."""

    def __init__(self, *, clock=time.monotonic):
        super().__init__(clock=clock)

    def _confirmed(self, envelope, value, make_response):
        captured = SQLPrimary._wire(self, envelope, value, capture=True)
        metadata = dict(self.metadata)
        if metadata.get('X-EventDesk-Backup') == 'current' and (
                self.backup_revision is None or self.backup_revision < value['revision']):
            metadata['X-EventDesk-Backup'] = 'stale'
        # A SQL result proves connectivity, never the state of an inaccessible outbox.
        if metadata.get('X-EventDesk-Queue') == 'clear':
            metadata.pop('X-EventDesk-Queue')
        headers = [*captured['headers'], ('X-EventDesk-Storage', 'sql'),
                   ('X-EventDesk-Sync', 'online'), *metadata.items()]
        if envelope['method'] == 'POST':
            headers.append(('X-EventDesk-Operation-Resolved', envelope['operation_id']))
        return make_response({**captured, 'headers': headers})

    def _schedule(self, envelope, revision, schedule, maintain):
        if schedule is None or self.probe_in_flight or self.clock() < self.coordinator_at:
            return
        self.coordinator_at = self.clock() + MAINTENANCE_SECONDS
        self.probe_in_flight = True

        async def run():
            try:
                result = await maintain(envelope)
                self.observe(result)
                if result.status >= 500:
                    self.metadata = {'X-EventDesk-Backup': 'unavailable', 'X-EventDesk-Queue': 'unknown'}
                    self.backup_revision = None
                if result.status >= 500 or self.metadata.get('X-EventDesk-Backup') == 'unavailable':
                    self.coordinator_at = self.clock() + QUOTA_COOLDOWN_SECONDS
            except Exception:
                self.metadata = {'X-EventDesk-Backup': 'unavailable', 'X-EventDesk-Queue': 'unknown'}
                self.backup_revision = None
                self.coordinator_at = self.clock() + QUOTA_COOLDOWN_SECONDS
            finally:
                self.probe_in_flight = False

        task = run()
        try:
            schedule(task)
        except Exception:
            task.close()
            self.probe_in_flight = False
            self.metadata = {'X-EventDesk-Backup': 'unavailable', 'X-EventDesk-Queue': 'unknown'}
            self.backup_revision = None
            self.coordinator_at = self.clock() + QUOTA_COOLDOWN_SECONDS

    async def handle(self, envelope, transport, fallback, make_response, schedule=None, maintain=None):
        if self.clock() < self.unavailable_until:
            return await self._unavailable(envelope, fallback, make_response)
        try:
            value = await transport('request', envelope)
            result = self._confirmed(envelope, value, make_response)
        except PrimaryUnavailable:
            self.unavailable_until = self.clock() + OUTAGE_SECONDS
            return await self._unavailable(envelope, fallback, make_response)
        self.unavailable_until = 0
        if maintain is not None:
            self._schedule(envelope, value['revision'], schedule, maintain)
        return result

    async def _unavailable(self, envelope, fallback, make_response):
        if envelope['method'] == 'GET' or envelope['path'] in FALLBACK_POSTS:
            try:
                result = await fallback(envelope)
                self.observe(result)
                return result
            except Exception:
                return make_response(_failure('SQL and its backup are unavailable. Retry the same action.'))
        return make_response(_failure('The save is not confirmed. Retry the same action when connected.'))
