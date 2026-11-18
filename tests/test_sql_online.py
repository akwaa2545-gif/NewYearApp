"""SQL availability is independent of the optional Durable Object replica."""
import sqlite3
import unittest
from unittest.mock import patch

from cloudflare.sql_online import SQLOnlinePrimary
from tests import test_event_primary as legacy


class OnlineTests(unittest.IsolatedAsyncioTestCase):
    setUp = legacy.PrimaryTests.setUp
    tearDown = legacy.PrimaryTests.tearDown
    atomic = legacy.PrimaryTests.atomic
    transport = legacy.PrimaryTests.transport
    success = legacy.PrimaryTests.success
    reply = legacy.PrimaryTests.reply
    request = legacy.PrimaryTests.request
    fallback = legacy.PrimaryTests.fallback
    unavailable = legacy.PrimaryTests.unavailable
    headers = legacy.PrimaryTests.headers
    snapshot_reply = legacy.PrimaryTests.snapshot_reply
    divergent_attendance = legacy.PrimaryTests.divergent_attendance

    def online(self, atomic=None):
        self.router = SQLOnlinePrimary(self.transport, atomic or self.atomic, self.fallback,
                                       self.store.tables, self.router.secret.hex())
        return self.router

    async def test_cold_quota_failure_never_blocks_sql_reads_checkins_or_draws(self):
        for error in (RuntimeError('quota exceeded'), sqlite3.IntegrityError('broken replica')):
            attempts = []
            def broken(action):
                attempts.append(action)
                raise error
            self.online(broken)
            for path, method in (('/api/stats', 'GET'), ('/api/scan/checkin', 'POST'), ('/api/draw', 'POST')):
                response = await self.router.handle(self.request(path, method))
                self.assertEqual(response['status'], 200)
                self.assertEqual(self.headers(response)['x-eventdesk-sync'], 'synced')
                self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')
                self.assertEqual(self.headers(response)['x-eventdesk-queue'], 'unknown')
                self.assertNotIn('x-eventdesk-uncertain', self.headers(response))
            self.assertEqual(len(attempts), 1)

    async def test_one_hundred_requests_do_not_reprobe_storage_during_cooldown(self):
        attempts = []
        def broken(action):
            attempts.append(action)
            raise RuntimeError('quota')
        self.online(broken)
        with patch('cloudflare.sql_online.time.monotonic', return_value=100):
            for _ in range(100):
                self.assertEqual((await self.router.handle(self.request()))['status'], 200)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(all(not payload.get('include_replica') for _, payload in self.calls))

    async def test_malformed_sql_envelope_still_fails_closed(self):
        self.online()
        async def malformed(kind, payload):
            return {'schema_version': 1}
        self.transport_impl = malformed
        self.assertEqual((await self.router.handle(self.request()))['status'], 503)
        self.assertEqual(self.fallback_calls, [])

    async def test_offline_checkin_is_durable_and_duplicate_keeps_one_operation(self):
        self.online()
        self.transport_impl = self.unavailable
        request = self.request('/api/scan/checkin', 'POST')
        self.assertEqual((await self.router.handle(request))['status'], 202)
        self.assertEqual((await self.router.handle(request))['status'], 202)
        self.assertEqual(len(self.store.pending()), 1)

    async def test_stale_last_good_copy_still_accepts_atomic_offline_checkin(self):
        self.online()
        await self.router.handle(self.request())
        async def newer(kind, payload):
            return self.reply(payload, revision=2, replica_complete=False)
        self.transport_impl = newer
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'stale')
        self.assertEqual(self.store.state()['revision'], 1)
        self.transport_impl = self.unavailable
        result = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(result['status'], 202)
        self.assertEqual(len(self.store.pending()), 1)

    async def test_one_hundred_unchanged_reads_over_hours_never_revisit_storage(self):
        accesses = []
        def counted(action):
            accesses.append(action.__name__)
            return self.atomic(action)
        self.online(counted)
        clock = [100]
        with patch('cloudflare.sql_online.time.monotonic', side_effect=lambda: clock[0]):
            await self.router.handle(self.request())
            self.assertEqual(accesses, ['inspect'])
            initial = self.db.total_changes
            initial_state = self.store.state()
            for _ in range(100):
                clock[0] += 3600
                await self.router.handle(self.request())
            self.assertEqual(accesses, ['inspect'])
            self.assertEqual(self.db.total_changes, initial)
            self.assertEqual(self.store.state(), initial_state)
            self.assertFalse(any(payload.get('include_replica') for _, payload in self.calls))

    async def test_changed_revision_syncs_once_then_throttles_and_catches_up(self):
        accesses, head = [], [1]
        def counted(action):
            accesses.append(action.__name__)
            return self.atomic(action)
        self.online(counted)
        async def changed(kind, payload):
            changes = [{'revision': revision, 'changes': []}
                       for revision in range(payload['after_revision'] + 1, head[0] + 1)]
            return self.reply(payload, revision=head[0],
                              changes=changes if payload.get('include_replica') else [],
                              replica_complete=payload.get('include_replica', False))
        self.transport_impl = changed
        clock = [100]
        with patch('cloudflare.sql_online.time.monotonic', side_effect=lambda: clock[0]):
            await self.router.handle(self.request())
            head[0] = 2  # An external import is detected by an ordinary GET.
            await self.router.handle(self.request())
            self.assertEqual(self.store.state()['revision'], 2)
            baseline = len(accesses)
            for _ in range(10):
                await self.router.handle(self.request())
            self.assertEqual(len(accesses), baseline)
            head[0] = 3
            clock[0] = 110
            result = await self.router.handle(self.request())
            self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'stale')
            head[0] = 4
            clock[0] = 159
            await self.router.handle(self.request())
            self.assertEqual(len(accesses), baseline)
            clock[0] = 160
            result = await self.router.handle(self.request())
            self.assertEqual(self.store.state()['revision'], 4)
            self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'current')
            maintenance = [payload for _, payload in self.calls if payload.get('include_replica')]
            self.assertEqual(len(maintenance), 2)
            self.assertTrue(all(payload['path'] == '/api/me' for payload in maintenance))
            baseline = len(accesses)
            clock[0] = 4000
            await self.router.handle(self.request())
            self.assertEqual(len(accesses), baseline)

    async def test_cold_coordinator_syncs_external_head_instead_of_claiming_current(self):
        self.online()
        async def changed(kind, payload):
            if payload.get('include_replica'):
                self.assertFalse(self.router.backup_current)
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': []}],
                              replica_complete=payload.get('include_replica', False))
        self.transport_impl = changed
        response = await self.router.handle(self.request())
        self.assertEqual(self.store.state()['revision'], 2)
        self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'current')
        self.assertEqual(sum(bool(payload.get('include_replica')) for _, payload in self.calls), 1)

    async def test_unchanged_sql_post_does_not_touch_known_current_backup(self):
        accesses = []
        def counted(action):
            accesses.append(action.__name__)
            return self.atomic(action)
        self.online(counted)
        await self.router.handle(self.request())
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(accesses, ['inspect'])
        self.assertFalse(any(payload.get('include_replica') for _, payload in self.calls))

    async def test_unseeded_backup_bootstraps_even_without_newer_sql_head(self):
        with self.db:
            self.db.execute('UPDATE _eventdesk_replica_meta SET seeded=0,ready=0')
        self.online()
        async def seed(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, {'employees': [
                    {'id': 1, 'employee_id': '0000001', 'checked_in_at': None}]}, 1)
            return self.reply(payload)
        self.transport_impl = seed
        result = await self.router.handle(self.request())
        self.assertEqual(result['status'], 200)
        self.assertTrue(self.store.state()['ready'])
        self.assertTrue(self.store.state()['seeded'])
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'current')

    async def test_failed_explicit_bootstrap_recovers_at_unchanged_sql_head(self):
        from cloudflare.event_primary import PrimaryUnavailable
        self.online()
        await self.router.handle(self.request())
        self.transport_impl = self.unavailable
        with self.assertRaises(PrimaryUnavailable):
            await self.router.bootstrap()
        self.assertFalse(self.store.state()['ready'])
        self.assertFalse(self.router.backup_current)
        async def recover(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, {'employees': [
                    {'id': 1, 'employee_id': '0000001', 'checked_in_at': None}]}, 1)
            return self.reply(payload)
        self.transport_impl = recover
        result = await self.router.handle(self.request())
        self.assertEqual(result['status'], 200)
        self.assertTrue(self.store.state()['ready'])
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'current')

    async def test_failed_sync_storage_retries_same_sql_head_after_quota_cooldown(self):
        attempts, blocked = [], [True]
        def quota(action):
            attempts.append(action.__name__)
            if action.__name__ == 'apply' and blocked[0]:
                raise RuntimeError('quota during sync')
            return self.atomic(action)
        self.online(quota)
        async def newer(kind, payload):
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': []}],
                              replica_complete=payload.get('include_replica', False))
        self.transport_impl = newer
        clock = [100]
        with patch('cloudflare.sql_online.time.monotonic', side_effect=lambda: clock[0]):
            result = await self.router.handle(self.request())
            self.assertEqual(result['status'], 200)
            self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'unavailable')
            blocked[0] = False
            baseline = len(attempts)
            clock[0] = 399
            await self.router.handle(self.request())
            self.assertEqual(len(attempts), baseline)
            clock[0] = 400
            result = await self.router.handle(self.request())
            self.assertEqual(result['status'], 200)
            self.assertEqual(self.store.state()['revision'], 2)
            self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'current')
            self.assertEqual(sum(bool(payload.get('include_replica')) for _, payload in self.calls), 2)

    async def test_unknown_queue_retries_after_quota_cooldown_even_at_unchanged_head(self):
        queued = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        with self.db:
            self.store.enqueue(queued, 'checkin', {'status': 200})
        attempts, blocked = [], [True]
        def quota(action):
            attempts.append(action.__name__)
            if blocked[0]:
                raise RuntimeError('quota')
            return self.atomic(action)
        self.online(quota)
        async def recover(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, {'employees': [
                    {'id': 1, 'employee_id': '0000001', 'checked_in_at': None}]}, 1)
            return self.reply(payload)
        self.transport_impl = recover
        clock = [100]
        with patch('cloudflare.sql_online.time.monotonic', side_effect=lambda: clock[0]):
            self.assertEqual((await self.router.handle(self.request()))['status'], 200)
            blocked[0] = False
            clock[0] = 399
            await self.router.handle(self.request())
            self.assertEqual(len(attempts), 1)
            clock[0] = 400
            result = await self.router.handle(self.request())
            self.assertEqual(result['status'], 200)
            self.assertEqual(self.store.pending(), [])
            self.assertTrue(self.store.state()['ready'])
            self.assertTrue(any(payload.get('replay') for _, payload in self.calls))

    async def test_failed_maintenance_does_not_replace_confirmed_sql_result(self):
        self.online()
        async def broken(kind, payload):
            if payload.get('include_replica'):
                raise RuntimeError('maintenance failed')
            return self.reply(payload, revision=2, replica_complete=False)
        self.transport_impl = broken
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.headers(result)['x-eventdesk-sync'], 'synced')
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'stale')
        self.assertEqual(self.store.state()['revision'], 1)

    async def test_empty_replica_from_ordinary_sql_request_cannot_mark_backup_current(self):
        self.online()
        await self.router.handle(self.request())
        async def empty(kind, payload):
            return self.reply(payload, revision=2, changes=[], replica_complete=True)
        self.transport_impl = empty
        self.router.maintenance_at = 160
        with patch('cloudflare.sql_online.time.monotonic', return_value=100):
            result = await self.router.handle(self.request())
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'stale')
        self.assertEqual(self.store.state()['revision'], 1)

    async def test_known_queue_ack_storage_failure_keeps_sql_available_and_intent_durable(self):
        request = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        with self.db:
            self.store.enqueue(request, 'checkin', {'status': 200})
        def fails_ack(action):
            if action.__name__ == 'acknowledge':
                raise sqlite3.IntegrityError('local failure')
            return self.atomic(action)
        self.online(fails_ack)
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.headers(result)['x-eventdesk-queue'], 'unknown')
        self.assertEqual(len(self.store.pending()), 1)
        replay = [payload for _, payload in self.calls if payload.get('replay')]
        self.assertEqual(len(replay), 1)
        self.assertTrue(replay[0]['queue_proof'])

    async def test_ten_queued_checkins_drain_before_draw_and_replace_divergent_ids(self):
        snapshot, changes = self.divergent_attendance()
        requests = [self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
                    for _ in range(10)]
        with self.db:
            for request in requests:
                self.store.enqueue(request, 'checkin', {'status': 200})
        self.online()
        async def recover(kind, payload):
            if kind == 'snapshot':
                self.assertFalse(self.store.state()['ready'])
                self.assertEqual(self.store.pending(), [])
                return self.snapshot_reply(payload, snapshot, 2)
            if payload['path'] == '/api/draw':
                self.assertEqual(self.store.pending(), [])
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}],
                              replica_complete=payload.get('include_replica', False))
        self.transport_impl = recover
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 200)
        self.assertEqual([payload['operation_id'] for _, payload in self.calls if payload.get('replay')],
                         [request['operation_id'] for request in requests])
        self.assertEqual(self.db.execute('SELECT * FROM event_checkins').fetchall(), [(3, 1, 'original-time')])
        self.assertTrue(self.store.state()['ready'])

    async def test_offline_fallback_storage_failure_never_reports_saved(self):
        self.online()
        self.transport_impl = self.unavailable
        async def broken(envelope, kind):
            raise RuntimeError('quota')
        self.router.fallback = broken
        result = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(result['status'], 503)
        self.assertNotIn(b'pending_sync', result['body'])
        self.assertEqual(self.store.pending(), [])

    async def test_unsafe_lost_ack_is_not_replayed_automatically(self):
        self.online()
        executions = []
        async def lost(kind, payload):
            executions.append(payload)
            raise PrimaryUnavailable()
        from cloudflare.event_primary import PrimaryUnavailable
        self.transport_impl = lost
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 503)
        self.assertEqual(len(executions), 1)
        self.assertEqual(self.store.pending()[0]['kind'], 'uncertain')
        self.assertFalse(self.store.state()['ready'])

    async def test_real_engine_lost_checkin_ack_retry_uses_native_receipt_during_quota(self):
        from tests import test_event_backend as backend
        fixture = backend.EventBackendTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cookie, csrf = fixture.staff()
        request = fixture.command('/api/scan/checkin', data={'code': '0000123'},
                                  headers=[('Cookie', cookie), ('X-CSRF-Token', csrf)])
        lost = [False]
        async def transport(kind, payload):
            result = fixture.engine.handle(payload)
            if not lost[0]:
                lost[0] = True
                raise PrimaryUnavailable()
            return result
        from cloudflare.event_primary import PrimaryUnavailable
        def quota(action):
            raise RuntimeError('quota')
        self.router = SQLOnlinePrimary(transport, quota, self.fallback, self.store.tables, fixture.config['secret'])
        self.assertEqual((await self.router.handle(request))['status'], 503)
        self.assertEqual((await self.router.handle(request))['status'], 200)
        with fixture.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)
            self.assertIsNotNone(tx.receipt(request['operation_id']))
        async def failed_fallback(envelope, kind):
            raise RuntimeError('quota during fallback')
        lost[0] = False
        self.router = SQLOnlinePrimary(transport, self.atomic, failed_fallback,
                                       self.store.tables, fixture.config['secret'])
        result = await self.router.handle(request)
        self.assertEqual(result['status'], 503)
        self.assertIn(b'Check-in not confirmed', result['body'])
        self.assertNotIn(b'not saved', result['body'])
        self.assertEqual((await self.router.handle(request))['status'], 200)

    async def test_more_than_one_recovery_batch_cannot_hide_later_queued_writes(self):
        with self.db:
            for _ in range(22):
                self.store.enqueue(self.request('/api/scan/checkin', 'POST',
                    recorded_at='2026-11-17T01:00:00+00:00'), 'checkin', {'status': 200})
        self.online()
        async def checked(kind, payload):
            if payload.get('path') == '/api/draw':
                self.assertEqual(self.store.pending(), [])
            return self.reply(payload)
        self.transport_impl = checked
        self.assertEqual((await self.router.handle(self.request('/api/draw', 'POST')))['status'], 503)
        self.assertEqual(len(self.store.pending()), 2)
        self.assertEqual((await self.router.handle(self.request('/api/draw', 'POST')))['status'], 200)
        self.assertEqual(self.store.pending(), [])

    async def test_corrupt_saved_checkin_or_uncertain_payload_cannot_block_sql(self):
        for kind in ('checkin', 'uncertain'):
            with self.db:
                self.db.execute('DELETE FROM _eventdesk_outbox')
                queued = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
                self.store.enqueue(queued, kind, {'status': 200})
                self.db.execute('UPDATE _eventdesk_outbox SET envelope_json=?', (b'{}',))
            self.online()
            response = await self.router.handle(self.request('/api/draw', 'POST'))
            self.assertEqual(response['status'], 200)
            self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')
            self.assertEqual(self.headers(response)['x-eventdesk-queue'], 'unknown')
            self.assertEqual(len(self.store.pending()), 1)

    async def test_offline_lookup_failure_prevents_fallback_access_during_cooldown(self):
        attempts = []
        def lookup_failure(action):
            attempts.append(action.__name__)
            if action.__name__ == '<lambda>':
                raise RuntimeError('storage unavailable')
            return self.atomic(action)
        self.online(lookup_failure)
        self.transport_impl = self.unavailable
        result = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(result['status'], 503)
        self.assertEqual(self.fallback_calls, [])
        self.assertEqual(len(attempts), 2)

    async def test_corrupt_revision_cannot_poison_online_request_cursor(self):
        with self.db:
            self.db.execute('UPDATE _eventdesk_replica_meta SET revision=-1')
        self.online()
        result = await self.router.handle(self.request())
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.calls[0][1]['after_revision'], 0)
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'unavailable')

    async def test_late_photo_proves_backup_stale_without_marking_it_current(self):
        self.online()
        await self.router.handle(self.request())
        async def newer(kind, payload):
            return self.reply(payload, revision=2, replica_complete=False)
        self.transport_impl = newer
        result = await self.router.handle(self.request('/api/employee-photo/0000001'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.headers(result)['x-eventdesk-backup'], 'stale')

    async def test_ten_atomic_offline_checkins_recover_and_refresh_without_waiting_a_minute(self):
        snapshot, changes = self.divergent_attendance()
        self.online()
        self.transport_impl = self.unavailable
        for _ in range(10):
            result = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
            self.assertEqual(result['status'], 202)
        self.assertEqual(len(self.store.pending()), 10)
        async def recover(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, snapshot, 2)
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}],
                              replica_complete=payload.get('include_replica', False))
        self.transport_impl = recover
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.store.pending(), [])
        self.assertTrue(self.store.state()['ready'])
        self.assertEqual(self.db.execute('SELECT * FROM event_checkins').fetchall(), [(3, 1, 'original-time')])

    async def test_authoritative_sql_post_resolves_only_its_own_operation_even_on_rejection(self):
        self.online()
        for status in (200, 409):
            async def response(kind, payload):
                return self.reply(payload, status=status)
            self.transport_impl = response
            request = self.request('/api/draw', 'POST')
            result = await self.router.handle(request)
            self.assertEqual(result['status'], status)
            self.assertEqual(self.headers(result)['x-eventdesk-operation-resolved'], request['operation_id'])

    async def test_unconfirmed_or_read_responses_never_claim_operation_resolved(self):
        self.online()
        result = await self.router.handle(self.request())
        self.assertNotIn('x-eventdesk-operation-resolved', self.headers(result))
        self.transport_impl = self.unavailable
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 503)
        self.assertNotIn('x-eventdesk-operation-resolved', self.headers(result))
        async def malformed(kind, payload):
            return {'schema_version': 1}
        self.transport_impl = malformed
        result = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(result['status'], 503)
        self.assertNotIn('x-eventdesk-operation-resolved', self.headers(result))
