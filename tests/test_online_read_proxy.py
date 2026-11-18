"""Healthy SQL reads avoid the durable coordinator without bypassing its writes."""
import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cloudflare_worker import Headers, Reader, import_worker, request
from cloudflare.event_primary import PrimaryProtocolError, PrimaryUnavailable
from cloudflare.read_proxy import OnlineReadProxy, eligible_read


def reply(envelope, *, status=200, body=b'{}'):
    return {'schema_version': 1, 'operation_id': None, 'after_revision': 0,
            'revision': 10, 'receipt_found': False, 'cancelled': False,
            'changes': [], 'replica_complete': False, 'snapshot_required': False,
            'status': status, 'headers': [['Content-Type', 'application/json']],
            'body_base64': base64.b64encode(body).decode()}


def coordinated(status=200, *, source='sql', backup='current', queue='clear', revision='10'):
    return SimpleNamespace(status=status, body=b'coordinated', headers=Headers({
        'X-EventDesk-Storage': source, 'X-EventDesk-Backup': backup,
        'X-EventDesk-Queue': queue, 'X-EventDesk-Sync': 'synced',
        'X-EventDesk-Backup-Revision': revision}))


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.proxy = OnlineReadProxy(clock=lambda: self.now)
        self.coordinate = AsyncMock(return_value=coordinated())
        self.transport = AsyncMock(side_effect=lambda _kind, value: reply(value))
        self.make_response = lambda value: SimpleNamespace(**value)

    async def read(self, incoming=None):
        return await self.proxy.fetch(incoming or request(), self.transport,
                                      self.coordinate, self.make_response)

    async def test_first_read_coordinates_then_one_hundred_reads_skip_the_object(self):
        await self.read()
        for _ in range(100):
            result = await self.read()
            self.assertEqual(result.status, 200)
        self.assertEqual(self.coordinate.await_count, 1)
        self.assertEqual(self.transport.await_count, 100)

    async def test_sixty_second_refresh_is_request_driven(self):
        await self.read()
        self.now += 59.9
        await self.read()
        self.assertEqual(self.coordinate.await_count, 1)
        self.now += .1
        await self.read()
        self.assertEqual(self.coordinate.await_count, 2)

    async def test_ten_simultaneous_reads_claim_only_one_coordinator_probe(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(_request):
            entered.set()
            await release.wait()
            return coordinated()
        self.coordinate.side_effect = slow
        first = asyncio.create_task(self.read())
        await entered.wait()
        # A long initial seed must not allow another maintenance probe at 60s.
        self.now += 61
        results = await asyncio.gather(*(self.read() for _ in range(9)))
        self.assertEqual(len(results), 9)
        self.assertEqual(self.coordinate.await_count, 1)
        release.set()
        await first

    async def test_sql_outage_uses_original_request_for_existing_sqlite_fallback(self):
        await self.read()
        self.transport.side_effect = PrimaryUnavailable('unavailable')
        self.coordinate.return_value = coordinated(source='sqlite', queue='pending')
        incoming = request('/api/attendance?page=2', {'Cookie': 'private'})
        result = await self.read(incoming)
        self.assertEqual(result.headers.get('X-EventDesk-Storage'), 'sqlite')
        self.coordinate.assert_awaited_with(incoming)

    async def test_malformed_sql_reply_and_configuration_errors_fail_closed(self):
        await self.read()
        self.transport.return_value = {'schema_version': 1}
        self.transport.side_effect = None
        with self.assertRaises(PrimaryProtocolError):
            await self.read()
        self.transport.side_effect = PrimaryProtocolError('bad configuration')
        with self.assertRaises(PrimaryProtocolError):
            await self.read()
        self.assertEqual(self.coordinate.await_count, 1)

    async def test_backend_authorization_failure_is_not_replaced_by_sqlite(self):
        await self.read()
        self.transport.side_effect = lambda _kind, value: reply(value, status=401)
        result = await self.read()
        self.assertEqual(result.status, 401)
        self.assertEqual(self.coordinate.await_count, 1)

    async def test_failed_due_coordinator_never_blocks_healthy_sql(self):
        self.coordinate.side_effect = RuntimeError('durable request quota')
        result = await self.read()
        self.assertEqual(result.status, 200)
        self.assertEqual(self.coordinate.await_count, 1)
        self.assertEqual(self.transport.await_count, 1)

    async def test_failed_sql_after_due_coordinator_has_no_second_object_attempt(self):
        self.coordinate.return_value = coordinated(status=503)
        self.transport.side_effect = PrimaryUnavailable('unavailable')
        result = await self.read()
        self.assertEqual(result.status, 503)
        self.assertEqual(self.coordinate.await_count, 1)

    async def test_forged_internal_headers_cannot_change_read_envelope(self):
        await self.read()
        incoming = request('/api/stats?scope=all', {'Cookie': 'session',
            'Authorization': 'Bearer attacker', 'X-EventDesk-Replay': 'true',
            'X-Deployment-Key': 'private', 'X-EventDesk-Operation-Id': 'd85db106-bc9e-4839-bcbd-8443da74bb4e'})
        await self.read(incoming)
        envelope = self.transport.await_args.args[1]
        self.assertIsNone(envelope['operation_id'])
        self.assertEqual(envelope['after_revision'], 0)
        self.assertFalse(envelope['include_replica'])
        self.assertEqual(envelope['headers'], [['Cookie', 'session']])
        self.assertNotIn('replay', envelope)

    async def test_pending_metadata_stays_pending_and_get_never_resolves_an_operation(self):
        self.coordinate.return_value = coordinated(backup='stale', queue='pending')
        await self.read()
        result = await self.read()
        headers = dict(result.headers)
        self.assertEqual(headers['X-EventDesk-Storage'], 'sql')
        self.assertEqual(headers['X-EventDesk-Sync'], 'pending')
        self.assertEqual(headers['X-EventDesk-Queue'], 'pending')
        self.assertFalse(any('resolved' in key.lower() for key in headers))

    async def test_backup_current_requires_a_mirror_revision_covering_the_sql_head(self):
        await self.read()
        self.assertEqual(dict((await self.read()).headers)['X-EventDesk-Backup'], 'current')
        def advanced(_kind, envelope):
            return {**reply(envelope), 'revision': 11}
        self.transport.side_effect = advanced
        self.assertEqual(dict((await self.read()).headers)['X-EventDesk-Backup'], 'stale')
        self.proxy.observe(coordinated(revision=None))
        self.assertEqual(dict((await self.read()).headers)['X-EventDesk-Backup'], 'stale')

    async def test_cached_clear_metadata_never_claims_a_freshly_drained_queue(self):
        await self.read()
        result = await self.read()
        headers = dict(result.headers)
        self.assertNotIn('X-EventDesk-Sync', headers)
        self.assertNotIn('X-EventDesk-Queue', headers)
        self.assertNotIn('X-EventDesk-Operation-Resolved', headers)

    async def test_known_sqlite_outage_response_avoids_a_second_sql_timeout(self):
        self.coordinate.return_value = coordinated(status=503, source='sqlite')
        result = await self.read()
        self.assertEqual(result.status, 503)
        self.transport.assert_not_awaited()

    async def test_successful_sqlite_probe_also_opens_the_short_outage_circuit(self):
        self.coordinate.return_value = coordinated(source='sqlite')
        for _ in range(3):
            self.assertEqual((await self.read()).status, 200)
        self.transport.assert_not_awaited()
        self.assertEqual(self.coordinate.await_count, 3)
        self.now += 5
        await self.read()
        self.transport.assert_awaited_once()

    async def test_sql_reply_body_is_decoded_only_once_in_the_free_worker(self):
        await self.read()
        with patch('cloudflare.event_primary.base64.b64decode', wraps=base64.b64decode) as decode:
            result = await self.read()
        self.assertEqual(result.body, b'{}')
        self.assertEqual(decode.call_count, 1)

    async def test_short_outage_circuit_skips_repeated_direct_api_timeouts(self):
        await self.read()
        self.transport.side_effect = PrimaryUnavailable('unavailable')
        await self.read()
        for _ in range(10):
            await self.read()
        self.assertEqual(self.transport.await_count, 1)
        self.now += 5
        await self.read()
        self.assertEqual(self.transport.await_count, 2)

    async def test_only_fixed_get_routes_are_eligible(self):
        for path in ('/api/stats', '/api/projector/state', '/api/draw/eligible-ids'):
            self.assertTrue(eligible_read(request(path)), path)
        for path in ('/api/unknown', '/__deployment/status', '/v1/event/snapshot',
                     '/api/employee-photo/1003603', '/api/rewards/image?id=1', '/api/export/report',
                     '/api/employee-photo/1003603?x=1', '/api/employee-photo/../secret'):
            self.assertFalse(eligible_read(request(path)), path)
        self.assertFalse(eligible_read(request('/api/stats', method='POST')))


class DefaultWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = import_worker()
        self.worker = self.module.Default()
        self.backend = SimpleNamespace(fetch=AsyncMock(return_value=coordinated()))
        self.worker.env = SimpleNamespace(PUBLIC_ORIGIN='https://eventdesk.example',
            PHOTO_PILOT=object(), PHOTO_PILOT_ACCESS_TOKEN='test-token-for-service-0123456789',
            STATE=SimpleNamespace(getByName=Mock(return_value=self.backend)))
        self.worker._read_transport = AsyncMock(side_effect=lambda _kind, value: reply(value))

    async def test_read_optimization_still_sends_every_post_to_the_coordinator(self):
        await self.worker.fetch(request())
        await self.worker.fetch(request())
        self.assertEqual(self.backend.fetch.await_count, 1)
        for path in ('/api/scan/checkin', '/api/draw', '/api/login', '/api/lookup'):
            incoming = request(path, method='POST')
            self.assertIs(await self.worker.fetch(incoming), self.backend.fetch.return_value)
        self.assertEqual(self.backend.fetch.await_count, 5)

    async def test_sqlite_and_maintenance_modes_preserve_coordinator_routing(self):
        for settings in ({'EVENT_STORAGE': 'sqlite'}, {'EVENT_MAINTENANCE': '1'}, {'EVENT_DIRECT_READS': '0'}):
            for key, value in settings.items():
                setattr(self.worker.env, key, value)
            await self.worker.fetch(request())
            self.worker._read_transport.assert_not_awaited()
            for key in settings:
                delattr(self.worker.env, key)
        self.assertEqual(self.backend.fetch.await_count, 3)

    async def test_canonical_origin_is_checked_before_any_sql_or_object_call(self):
        incoming = request()
        incoming.url = 'https://attacker.example/api/stats'
        result = await self.worker.fetch(incoming)
        self.assertEqual(result.status, 308)
        self.worker._read_transport.assert_not_awaited()
        self.backend.fetch.assert_not_awaited()

    async def test_python_fetch_response_is_unwrapped_before_observing_backup_headers(self):
        raw = coordinated()
        raw.headers.append('Set-Cookie', 'first=fixture; Secure; HttpOnly')
        raw.headers.append('Set-Cookie', 'second=fixture; Secure; HttpOnly')
        self.backend.fetch.return_value = SimpleNamespace(js_response=raw, status=raw.status,
            headers={name.lower(): value for name, value in raw.headers})
        first = await self.worker.fetch(request())
        self.assertIs(first, raw)
        self.assertEqual(len(first.headers.values('Set-Cookie')), 2)
        second = await self.worker.fetch(request())
        self.assertEqual(second.headers.get('X-EventDesk-Backup'), 'current')
        self.assertEqual(self.backend.fetch.await_count, 1)

    async def test_python_fetch_response_sqlite_status_activates_the_outage_circuit(self):
        raw = coordinated(source='sqlite')
        self.backend.fetch.return_value = SimpleNamespace(js_response=raw, status=raw.status,
            headers={name.lower(): value for name, value in raw.headers})
        for _ in range(3):
            self.assertIs(await self.worker.fetch(request()), raw)
        self.worker._read_transport.assert_not_awaited()


class SharedTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = import_worker()
        self.worker = self.module.Default()
        self.service = SimpleNamespace(fetch=AsyncMock())
        self.worker.env = SimpleNamespace(PHOTO_PILOT=self.service,
            PHOTO_PILOT_ACCESS_TOKEN='Synthetic-event-gateway-token-0123456789')

    def upstream(self, raw=b'{}', *, status=200, headers=None, redirected=False):
        values = {'Content-Type': 'application/json; charset=utf-8', **(headers or {})}
        return SimpleNamespace(status=status, redirected=redirected, headers=Headers(values),
                               body=SimpleNamespace(getReader=lambda: Reader([raw])))

    async def test_direct_transport_uses_fixed_private_route_and_eight_second_deadline(self):
        self.service.fetch.return_value = self.upstream()
        original, deadlines = asyncio.wait_for, []
        async def observed(awaitable, *, timeout):
            deadlines.append(timeout)
            return await original(awaitable, timeout=timeout)
        with patch('cloudflare.event_runtime.asyncio.wait_for', side_effect=observed):
            await self.worker._read_transport('request', {'schema_version': 1})
        self.assertEqual(deadlines, [8])
        sent = self.service.fetch.await_args.args[0]
        self.assertEqual(sent.url, 'https://eventdesk-photo-pilot.pages.dev/v1/event/request')
        self.assertEqual(sent.redirect, 'manual')
        self.assertEqual(sent.headers.get('Authorization'),
                         'Bearer Synthetic-event-gateway-token-0123456789')

    async def test_transport_rejects_redirects_encoding_lengths_and_invalid_json(self):
        for result in (
                self.upstream(status=401), self.upstream(status=302),
                self.upstream(redirected=True), self.upstream(headers={'Content-Type': 'text/html'}),
                self.upstream(headers={'Content-Encoding': 'gzip'}),
                self.upstream(headers={'Content-Length': '25165825'}),
                self.upstream(headers={'Content-Length': '3'}),
                self.upstream(raw=b'{"status":1,"status":2}'),
                self.upstream(raw=b'{"status":NaN}'), self.upstream(raw=b'not-json')):
            with self.subTest(result=result.status, headers=list(result.headers)):
                self.service.fetch.return_value = result
                with self.assertRaises(PrimaryProtocolError):
                    await self.worker._read_transport('request', {'schema_version': 1})

    async def test_only_availability_failure_allows_sqlite_fallback(self):
        for result in (self.upstream(status=500), self.upstream(status=503)):
            self.service.fetch.return_value = result
            with self.assertRaises(PrimaryUnavailable):
                await self.worker._read_transport('request', {'schema_version': 1})

    async def test_oversized_envelope_is_rejected_before_network_access(self):
        with self.assertRaises(PrimaryProtocolError):
            await self.worker._read_transport('request', {'body': 'x' * (12 * 1024 * 1024)})
        self.service.fetch.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
