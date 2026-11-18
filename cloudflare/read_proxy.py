"""Small online SQL reads bypass the durable coordinator; writes never do.

Only replica metadata is cached. Browser data and sessions are always authorized
and read by SQL. The request-driven lease is claimed before any network await.
"""
import re
import time
from urllib.parse import urlsplit

from .event_primary import PrimaryUnavailable, SQLPrimary, _failure
from .event_runtime import request_envelope


# Images and exports can spend too much CPU decoding on a Free-plan Worker.
# They keep the original coordinator path until separately measured.
DIRECT_PATHS = frozenset('/api/' + name for name in (
    'me', 'stats', 'projector/state', 'draw/config', 'draw/eligible-ids',
    'winners', 'rewards',
))
COORDINATOR_SECONDS = 60
OUTAGE_COOLDOWN_SECONDS = 5


def eligible_read(request):
    if request.method != 'GET':
        return False
    try:
        parts = urlsplit(request.url)
    except ValueError:
        return False
    return (parts.path in DIRECT_PATHS and len(parts.path) + len(parts.query) <= 8192
            and len(parts.query) <= 4096 and not parts.fragment
            and not any(ord(char) < 32 or ord(char) == 127 for char in parts.path + parts.query)
            and '\\' not in parts.path)


class OnlineReadProxy:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.coordinator_at = 0
        self.probe_in_flight = False
        self.unavailable_until = 0
        self.metadata = {}
        self.backup_revision = None

    def observe(self, result):
        """Keep only bounded, non-personal coordinator status fields."""
        headers = result.headers
        values = {}
        for name, allowed in (
                ('X-EventDesk-Backup', ('current', 'stale', 'unavailable')),
                ('X-EventDesk-Queue', ('clear', 'pending', 'unknown'))):
            value = headers.get(name)
            if value in allowed:
                values[name] = value
        revision = headers.get('X-EventDesk-Backup-Revision')
        self.backup_revision = (int(revision) if isinstance(revision, str)
                                and re.fullmatch(r'[0-9]{1,16}', revision)
                                and int(revision) <= 2**53 - 1 else None)
        self.metadata = values

    def _online(self, envelope, value, make_response):
        captured = SQLPrimary._wire(self, envelope, value, capture=True)
        metadata = dict(self.metadata)
        if metadata.get('X-EventDesk-Backup') == 'current' and (
                self.backup_revision is None or self.backup_revision < value['revision']):
            metadata['X-EventDesk-Backup'] = 'stale'
        # A cached clear queue cannot prove a previous offline change recovered.
        # Only a coordinator response may clear the browser's offline state.
        pending = metadata.pop('X-EventDesk-Queue', None) == 'pending'
        headers = [*captured['headers'], ('X-EventDesk-Storage', 'sql'), *metadata.items()]
        if pending:
            headers.extend((('X-EventDesk-Sync', 'pending'), ('X-EventDesk-Queue', 'pending')))
        return make_response({**captured, 'headers': headers})

    async def fetch(self, request, transport, coordinate, make_response):
        now = self.clock()
        if now < self.unavailable_until:
            result = await coordinate(request)
            self.observe(result)
            return result
        attempted, previous = False, None
        if now >= self.coordinator_at and not self.probe_in_flight:
            self.coordinator_at = now + COORDINATOR_SECONDS
            self.probe_in_flight = True
            attempted = True
            try:
                previous = await coordinate(request)
                self.observe(previous)
                if previous.headers.get('X-EventDesk-Storage') == 'sqlite':
                    self.unavailable_until = self.clock() + OUTAGE_COOLDOWN_SECONDS
                    return previous
                if previous.status < 500:
                    return previous
            except Exception:
                # A Durable Object quota cannot block an otherwise healthy read.
                self.metadata = {'X-EventDesk-Backup': 'unavailable', 'X-EventDesk-Queue': 'unknown'}
                self.backup_revision = None
            finally:
                self.probe_in_flight = False
        envelope = {**request_envelope(request, b''), 'operation_id': None,
                    'include_replica': False}
        try:
            value = await transport('request', envelope)
            return self._online(envelope, value, make_response)
        except PrimaryUnavailable:
            self.unavailable_until = self.clock() + OUTAGE_COOLDOWN_SECONDS
            if attempted:
                return previous if previous is not None else make_response(_failure('SQL is unavailable. Try again shortly.'))
            result = await coordinate(request)
            self.observe(result)
            return result
        # Protocol/configuration errors must not fall back to old authorization.
