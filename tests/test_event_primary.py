"""Exercise SQL-first routing and outage recovery without network credentials."""
import base64
import asyncio
import copy
import hashlib
import json
import secrets
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from cloudflare.event_primary import PrimaryProtocolError, PrimaryUnavailable, SQLPrimary
from cloudflare.replica import ReplicaStore


TABLES = {'employees': {'columns': ('id', 'employee_id', 'checked_in_at'),
    'primary_key': ('id',), 'foreign_keys': [], 'column_info': []}}


def encode(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


class PrimaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE employees(id INTEGER PRIMARY KEY, employee_id TEXT, checked_in_at TEXT)')
        self.store = ReplicaStore(self.db, TABLES)
        self.store.initialize()
        self.store.replace_snapshot({'employees': [{'id': 1, 'employee_id': '0000001', 'checked_in_at': None}]}, 1)
        self.db.commit()
        self.calls, self.fallback_calls = [], []
        self.transport_impl = self.success
        self.router = SQLPrimary(self.transport, self.atomic, self.fallback, TABLES, secrets.token_hex(32))

    def tearDown(self):
        self.db.close()

    def atomic(self, action):
        with self.db:
            return action(self.store)

    async def transport(self, kind, payload):
        self.calls.append((kind, copy.deepcopy(payload)))
        return await self.transport_impl(kind, payload)

    async def success(self, kind, payload):
        return self.reply(payload)

    def reply(self, payload, *, status=200, value=None, revision=1, changes=(), **extra):
        return {'schema_version': 1, 'operation_id': payload.get('operation_id'),
            'after_revision': payload.get('after_revision', 1), 'revision': revision,
            'status': status, 'headers': [['Content-Type', 'application/json']],
            'body_base64': encode(value or {'ok': True}), 'changes': list(changes),
            'replica_complete': True, 'snapshot_required': False, **extra}

    def request(self, path='/api/stats', method='GET', **extra):
        return {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'method': method,
            'path': path, 'headers': [['Cookie', 'synthetic-cookie']],
            'body_base64': '' if method == 'GET' else encode({'employee_id': '0000001'}),
            'client_ip': '192.0.2.1', 'after_revision': 0, **extra}

    async def fallback(self, envelope, queue_kind):
        self.fallback_calls.append((copy.deepcopy(envelope), queue_kind))
        value = {'person': {'employee_id': '0000001'}, 'state': 'checked_in'}
        result = {'status': 200, 'headers': [('Content-Type', 'application/json')],
                  'body': json.dumps(value).encode()}
        if queue_kind:
            with self.db:
                self.db.execute("UPDATE employees SET checked_in_at='offline' WHERE id=1")
                self.store.enqueue(envelope, queue_kind,
                    {'status': result['status'], 'headers': result['headers'], 'body_base64': encode(value)})
        return result

    async def unavailable(self, _kind, _payload):
        raise PrimaryUnavailable()

    def headers(self, response):
        return {key.lower(): value for key, value in response['headers']}

    async def test_healthy_request_uses_sql_and_never_runs_fallback_business_queries(self):
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.fallback_calls, [])
        self.assertEqual(self.calls[0][1]['after_revision'], 1)
        self.assertEqual(self.headers(response)['x-eventdesk-storage'], 'sql')
        self.assertEqual(self.headers(response)['x-eventdesk-uncertain'], 'clear')

    async def test_protocol_failure_has_an_opaque_support_code(self):
        async def invalid(_kind, _payload):
            raise PrimaryProtocolError('Synthetic upstream protocol failure.')
        self.transport_impl = invalid
        result = await self.router.handle(self.request())
        expected = hashlib.sha256(b'Synthetic upstream protocol failure.').hexdigest()[:12]
        self.assertEqual(result['status'], 503)
        self.assertEqual(self.headers(result)['x-eventdesk-error-code'], expected)
        self.assertNotIn(b'Synthetic upstream', result['body'])

    async def test_network_outage_reads_last_good_copy_but_pauses_draws(self):
        self.transport_impl = self.unavailable
        read = await self.router.handle(self.request())
        draw = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(read['status'], 200)
        self.assertEqual(self.headers(read)['x-eventdesk-storage'], 'sqlite')
        self.assertEqual(draw['status'], 503)
        self.assertEqual(len(self.fallback_calls), 1)
        self.assertEqual(self.store.pending()[0]['kind'], 'uncertain')

    async def test_offline_checkin_is_atomic_pending_and_duplicate_uuid_keeps_its_metadata(self):
        self.transport_impl = self.unavailable
        request = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        first = await self.router.handle(request)
        second = await self.router.handle(request | {'recorded_at': '2026-11-17T01:01:00+00:00', 'client_ip': '192.0.2.2'})
        self.assertEqual(first['status'], 202)
        self.assertTrue(json.loads(first['body'])['pending_sync'])
        self.assertEqual(second['status'], 202)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.pending()[0]['envelope']['recorded_at'], request['recorded_at'])
        self.assertEqual(len(self.fallback_calls), 1)

    async def test_changed_business_payload_does_not_reuse_pending_operation(self):
        self.transport_impl = self.unavailable
        request = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        await self.router.handle(request)
        response = await self.router.handle(request | {'body_base64': encode({'employee_id': '0000002'})})
        self.assertEqual(response['status'], 409)
        self.assertEqual(len(self.fallback_calls), 1)

    async def test_unknown_draw_is_resolved_by_receipt_instead_of_reexecuted(self):
        self.transport_impl = self.unavailable
        request = self.request('/api/draw', 'POST')
        await self.router.handle(request)
        async def lookup(kind, payload):
            if payload.get('receipt_only'):
                return self.reply(payload, value={'winner': 'original'}, receipt_found=True)
            return self.reply(payload)
        self.transport_impl = lookup
        response = await self.router.handle(self.request())
        receipt = self.calls[1][1]
        self.assertEqual(receipt['operation_id'], request['operation_id'])
        self.assertTrue(receipt['receipt_only'])
        self.assertTrue(receipt['cancel_if_missing'])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.headers(response)['x-eventdesk-uncertain'], 'clear')

    async def test_unknown_command_remains_blocked_when_receipt_cannot_be_checked(self):
        self.transport_impl = self.unavailable
        await self.router.handle(self.request('/api/draw', 'POST'))
        response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertTrue(self.calls[-1][1]['receipt_only'])

    async def test_checkin_replay_preserves_original_order_and_proof_before_mirror(self):
        self.transport_impl = self.unavailable
        request = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        await self.router.handle(request)
        async def recover(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, {'employees': [
                    {'id': 1, 'employee_id': '0000001', 'checked_in_at': 'sql-confirmed'}]}, 2)
            if payload.get('replay'):
                self.assertEqual(self.db.execute('SELECT checked_in_at FROM employees').fetchone()[0], 'offline')
                self.assertTrue(payload['queue_proof'])
                change = {'table': 'employees', 'key': {'id': 1}, 'action': 'upsert',
                          'row': {'id': 1, 'employee_id': '0000001', 'checked_in_at': 'sql-confirmed'}}
                return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': [change]}])
            return self.reply(payload, revision=2)
        self.transport_impl = recover
        response = await self.router.handle(self.request())
        replay = self.calls[1][1]
        self.assertEqual(replay['operation_id'], request['operation_id'])
        self.assertTrue(replay['replay'])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.db.execute('SELECT checked_in_at FROM employees').fetchone()[0], 'sql-confirmed')
        self.assertEqual(response['status'], 200)

    def snapshot_reply(self, payload, tables, revision):
        if 'table' not in payload:
            return {'schema_version': 1, 'revision': revision, 'tables': [
                {'name': name, 'columns': list(self.store.tables[name]['columns']),
                 'primary_key': list(self.store.tables[name]['primary_key']), 'row_count': len(rows)}
                for name, rows in tables.items()]}
        name = payload['table']
        return {'schema_version': 1, 'revision': revision, 'table': name,
                'rows': tables[name], 'next_offset': None}

    def divergent_attendance(self):
        self.db.execute('CREATE TABLE event_checkins(id INTEGER PRIMARY KEY, employee_id INTEGER UNIQUE '
                        'REFERENCES employees(id), checked_in_at TEXT)')
        tables = {**TABLES, 'event_checkins': {
            'columns': ('id', 'employee_id', 'checked_in_at'), 'primary_key': ('id',),
            'foreign_keys': [(0, 0, 'employees', 'employee_id', 'id', 'NO ACTION', 'NO ACTION', 'NONE')],
            'column_info': []}}
        self.store = ReplicaStore(self.db, tables)
        self.router = SQLPrimary(self.transport, self.atomic, self.fallback, tables, secrets.token_hex(32))
        with self.db:
            self.db.execute("INSERT INTO event_checkins VALUES(2,1,'offline')")
            self.db.execute("UPDATE employees SET checked_in_at='offline'")
        snapshot = {'employees': [{'id': 1, 'employee_id': '0000001', 'checked_in_at': 'original-time'}],
                    'event_checkins': [{'id': 3, 'employee_id': 1, 'checked_in_at': 'original-time'}]}
        changes = [{'table': table, 'key': {'id': row['id']}, 'action': 'upsert', 'row': row}
                   for table, rows in snapshot.items() for row in rows]
        return snapshot, changes

    async def test_replayed_checkin_replaces_speculative_identity_rows_from_sql_snapshot(self):
        snapshot, changes = self.divergent_attendance()
        queued = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        with self.db:
            self.store.enqueue(queued, 'checkin', {'status': 200})
        replays = []
        async def recover(kind, payload):
            if kind == 'snapshot':
                self.assertEqual(self.store.pending(), [])
                self.assertFalse(self.store.state()['ready'])
                return self.snapshot_reply(payload, snapshot, 2)
            if payload.get('replay'):
                replays.append(payload['operation_id'])
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}])
        self.transport_impl = recover
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(replays, [queued['operation_id']])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.db.execute('SELECT * FROM event_checkins').fetchall(), [(3, 1, 'original-time')])
        self.assertEqual(self.store.state()['revision'], 2)
        self.assertTrue(self.store.state()['ready'])

    async def test_existing_empty_outbox_identity_collision_heals_from_a_verified_sql_snapshot(self):
        snapshot, changes = self.divergent_attendance()
        async def healthy(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, snapshot, 2)
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}])
        self.transport_impl = healthy
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.db.execute('SELECT * FROM event_checkins').fetchall(), [(3, 1, 'original-time')])
        self.assertTrue(self.store.state()['ready'])
        self.assertEqual(self.store.state()['revision'], 2)

    async def test_failed_collision_invalidation_never_labels_the_backup_as_synced(self):
        snapshot, changes = self.divergent_attendance()
        async def healthy(kind, payload):
            if kind == 'snapshot':
                return self.snapshot_reply(payload, snapshot, 2)
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}])
        self.transport_impl = healthy
        original_atomic = self.atomic
        def failed_invalidation(action):
            if action.__name__ == 'invalidate':
                raise RuntimeError('Transient backup write failure')
            return original_atomic(action)
        self.router.atomic = failed_invalidation
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.headers(response).get('x-eventdesk-backup'), 'unavailable')
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))
        self.assertEqual(self.store.state()['revision'], 1)
        self.assertEqual([kind for kind, _payload in self.calls], ['request'])
        self.router.atomic = self.atomic
        retry = await self.router.handle(self.request())
        self.assertEqual(retry['status'], 200)
        self.assertTrue(self.store.state()['ready'])
        self.assertEqual(self.store.state()['revision'], 2)

    async def test_failed_recovery_snapshot_preserves_sql_result_and_retries_after_coordinator_restart(self):
        snapshot, changes = self.divergent_attendance()
        queued = self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00')
        with self.db:
            self.store.enqueue(queued, 'checkin', {'status': 200})
        replay_count = 0
        fail_snapshot = True
        async def recovering(kind, payload):
            nonlocal replay_count
            if kind == 'snapshot':
                if fail_snapshot:
                    raise PrimaryUnavailable('Snapshot temporarily unavailable')
                return self.snapshot_reply(payload, snapshot, 2)
            replay_count += int(payload.get('replay', False))
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}])
        self.transport_impl = recovering
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')
        self.assertFalse(self.store.state()['ready'])
        self.assertEqual(self.store.pending(), [])
        self.transport_impl = self.unavailable
        paused = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(paused['status'], 503)
        self.assertEqual(self.fallback_calls, [])
        # The pending uncertain attempt is cancelled without a business replay.
        self.router = SQLPrimary(self.transport, self.atomic, self.fallback,
                                 self.store.tables, secrets.token_hex(32))
        fail_snapshot = False
        async def resolved(kind, payload):
            if payload.get('receipt_only'):
                return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': changes}],
                                  receipt_found=False, cancelled=True, status=409)
            return await recovering(kind, payload)
        self.transport_impl = resolved
        retry = await self.router.handle(self.request())
        self.assertEqual(retry['status'], 200)
        self.assertEqual(replay_count, 1)
        self.assertTrue(self.store.state()['ready'])
        self.assertEqual(self.db.execute('SELECT * FROM event_checkins').fetchall(), [(3, 1, 'original-time')])

    async def test_replay_rejection_keeps_intent_for_operator_and_blocks_new_writes(self):
        self.transport_impl = self.unavailable
        await self.router.handle(self.request('/api/scan/checkin', 'POST', recorded_at='2026-11-17T01:00:00+00:00'))
        async def rejected(kind, payload):
            return self.reply(payload, status=401, value={'error': 'Account inactive'})
        self.transport_impl = rejected
        response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.store.pending()[0]['state'], 'conflict')

    async def test_protocol_and_authorization_errors_do_not_run_offline_mutations(self):
        async def invalid(kind, payload):
            return self.reply(payload) | {'operation_id': 'some-other-request'}
        self.transport_impl = invalid
        response = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.fallback_calls, [])
        async def unauthorized(_kind, _payload):
            raise PrimaryProtocolError('Upstream authorization failed.')
        self.transport_impl = unauthorized
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.fallback_calls, [])

    async def test_known_sql_application_error_does_not_trigger_fallback(self):
        async def failed(kind, payload):
            return self.reply(payload, status=500, value={'error': 'Application failed'})
        self.transport_impl = failed
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 500)
        self.assertEqual(self.fallback_calls, [])

    async def test_sql_stays_available_when_only_backup_storage_is_unavailable(self):
        def broken(_action):
            raise RuntimeError('Backup read quota')
        self.router.atomic = broken
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))
        self.assertEqual(self.fallback_calls, [])

    async def test_invalid_revision_gap_never_overwrites_saved_copy(self):
        async def gap(kind, payload):
            change = {'table': 'employees', 'key': {'id': 1}, 'action': 'delete', 'row': None}
            return self.reply(payload, revision=3, changes=[{'revision': 3, 'changes': [change]}])
        self.transport_impl = gap
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.store.state()['revision'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)

    async def test_consistent_paged_bootstrap_activates_only_after_all_tables(self):
        async def snapshot(kind, payload):
            self.assertEqual(kind, 'snapshot')
            if 'table' not in payload:
                return {'schema_version': 1, 'revision': 4, 'tables': [{'name': 'employees',
                    'columns': list(TABLES['employees']['columns']), 'primary_key': ['id'], 'row_count': 1}]}
            self.assertEqual(self.store.state()['revision'], 1)
            return {'schema_version': 1, 'revision': 4, 'table': 'employees', 'rows': [
                {'id': 2, 'employee_id': '0000002', 'checked_in_at': None}], 'next_offset': None}
        self.transport_impl = snapshot
        await self.router.bootstrap()
        self.assertEqual(self.store.state()['revision'], 4)
        self.assertEqual(self.db.execute('SELECT id FROM employees').fetchone()[0], 2)

    async def test_changed_snapshot_revision_keeps_original_copy(self):
        async def snapshot(kind, payload):
            if 'table' not in payload:
                return {'schema_version': 1, 'revision': 4, 'tables': [{'name': 'employees',
                    'columns': list(TABLES['employees']['columns']), 'primary_key': ['id'], 'row_count': 1}]}
            return {'schema_version': 1, 'revision': 5, 'table': 'employees', 'rows': [], 'next_offset': None}
        self.transport_impl = snapshot
        with self.assertRaises(PrimaryProtocolError):
            await self.router.bootstrap()
        self.assertEqual(self.store.state()['revision'], 1)

    async def test_large_snapshot_uses_bounded_thousand_row_pages_without_early_activation(self):
        rows = [{'id': number, 'employee_id': f'{number:07}', 'checked_in_at': None}
                for number in range(1, 1710)]
        pages = []
        async def snapshot(kind, payload):
            self.assertEqual(kind, 'snapshot')
            if 'table' not in payload:
                return self.snapshot_reply(payload, {'employees': rows}, 4)
            self.assertEqual(payload['limit'], 1000)
            self.assertEqual(self.store.state()['revision'], 1)
            self.assertFalse(self.store.state()['ready'])
            offset = payload['offset']
            pages.append(offset)
            end = offset + payload['limit']
            return {'schema_version': 1, 'revision': 4, 'table': 'employees',
                    'rows': rows[offset:end], 'next_offset': end if end < len(rows) else None}
        self.transport_impl = snapshot
        await self.router.bootstrap()
        self.assertEqual(pages, [0, 1000])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1709)
        self.assertEqual(self.store.state()['revision'], 4)
        self.assertTrue(self.store.state()['ready'])

    async def test_oversized_snapshot_page_cannot_activate_the_fallback(self):
        rows = [{'id': number, 'employee_id': f'{number:07}', 'checked_in_at': None}
                for number in range(1, 1002)]
        async def oversized(kind, payload):
            return self.snapshot_reply(payload, {'employees': rows}, 4)
        self.transport_impl = oversized
        with self.assertRaises(PrimaryProtocolError):
            await self.router.bootstrap()
        self.assertEqual(self.store.state()['revision'], 1)
        self.assertFalse(self.store.state()['ready'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)

    async def test_missing_receipt_proof_cannot_clear_an_unknown_draw(self):
        self.transport_impl = self.unavailable
        await self.router.handle(self.request('/api/draw', 'POST'))
        self.transport_impl = self.success
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 503)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))

    async def test_cancelled_receipt_releases_new_actions_without_executing_the_old_draw(self):
        self.transport_impl = self.unavailable
        await self.router.handle(self.request('/api/draw', 'POST'))
        async def cancel(kind, payload):
            if payload.get('receipt_only'):
                return self.reply(payload, status=409, cancelled=True, receipt_found=False)
            return self.reply(payload)
        self.transport_impl = cancel
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.store.pending(), [])

    async def test_known_sql_success_is_not_lost_when_snapshot_transport_is_down(self):
        async def unavailable_snapshot(kind, payload):
            if kind == 'snapshot':
                raise PrimaryUnavailable()
            return self.reply(payload, snapshot_required=True)
        self.transport_impl = unavailable_snapshot
        response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')

    async def test_conflicting_checkin_requires_review_instead_of_reconfirming_offline(self):
        self.transport_impl = self.unavailable
        request = self.request('/api/scan/checkin', 'POST')
        await self.router.handle(request)
        async def reject(kind, payload):
            return self.reply(payload, status=403)
        self.transport_impl = reject
        response = await self.router.handle(request)
        self.assertEqual(response['status'], 503)
        self.assertIn('review', json.loads(response['body'])['error'])

    async def test_read_only_polling_does_not_rewrite_unchanged_replica_metadata(self):
        count = self.db.total_changes
        await self.router.handle(self.request())
        self.assertEqual(self.db.total_changes, count)

    async def test_oversized_offline_checkin_does_not_enter_the_queue(self):
        self.transport_impl = self.unavailable
        response = await self.router.handle(self.request('/api/scan/checkin', 'POST', body_base64=encode({'junk': 'x' * 20000})))
        self.assertEqual(response['status'], 413)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.fallback_calls, [])

    async def test_unsafe_write_requires_a_durable_intent_before_any_sql_dispatch(self):
        with patch.object(self.store, 'enqueue', side_effect=RuntimeError('Write quota')):
            response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertIn('Changes are paused', json.loads(response['body'])['error'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fallback_calls, [])
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))

    async def test_offline_checkin_cannot_succeed_when_intent_storage_is_unavailable(self):
        self.transport_impl = self.unavailable
        with patch.object(self.store, 'enqueue', side_effect=RuntimeError('Write quota')):
            response = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fallback_calls, [])
        self.assertEqual(self.store.pending(), [])

    async def test_atomic_callback_must_explicitly_confirm_intent_persistence(self):
        original = self.atomic
        def unavailable_enqueue(action):
            if action.__name__ == 'persist_intent':
                return None
            return original(action)
        self.router.atomic = unavailable_enqueue
        response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.calls, [])

    async def test_offline_body_rewrite_drops_old_content_length(self):
        self.transport_impl = self.unavailable
        original = self.fallback
        async def with_length(envelope, kind):
            value = await original(envelope, kind)
            return {**value, 'headers': [*value['headers'], ('Content-Length', str(len(value['body'])))]}
        self.router.fallback = with_length
        response = await self.router.handle(self.request('/api/scan/checkin', 'POST'))
        self.assertEqual(response['status'], 202)
        self.assertNotIn('content-length', self.headers(response))
        self.assertTrue(json.loads(response['body'])['pending_sync'])

    async def test_successful_draw_is_preserved_when_snapshot_is_invalid(self):
        async def invalid_snapshot(kind, payload):
            if kind == 'snapshot':
                return {'schema_version': 99, 'revision': 2, 'tables': []}
            return self.reply(payload, value={'winner': 'original-winner'}, snapshot_required=True)
        self.transport_impl = invalid_snapshot
        response = await self.router.handle(self.request('/api/draw', 'POST'))
        self.assertEqual(response['status'], 200)
        self.assertEqual(json.loads(response['body'])['winner'], 'original-winner')
        self.assertEqual(self.headers(response)['x-eventdesk-backup'], 'unavailable')
        self.assertEqual(self.store.pending(), [])
        self.assertFalse(self.store.state()['ready'])

    async def test_unseeded_fallback_bootstraps_after_the_first_healthy_sql_response(self):
        with self.db:
            self.db.execute('UPDATE _eventdesk_replica_meta SET revision=0,ready=0,seeded=0')
        async def online(kind, payload):
            if kind == 'request':
                return self.reply(payload, revision=0)
            if 'table' not in payload:
                return {'schema_version': 1, 'revision': 0, 'tables': [{'name': 'employees',
                    'columns': list(TABLES['employees']['columns']), 'primary_key': ['id'], 'row_count': 1}]}
            return {'schema_version': 1, 'revision': 0, 'table': 'employees', 'rows': [
                {'id': 1, 'employee_id': '0000001', 'checked_in_at': None}], 'next_offset': None}
        self.transport_impl = online
        response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertTrue(self.store.state()['ready'])
        self.assertTrue(self.store.state()['seeded'])
        self.transport_impl = self.unavailable
        self.assertEqual((await self.router.handle(self.request()))['status'], 200)

    async def test_photo_io_does_not_block_checkin_or_rewind_its_newer_replica(self):
        photo_started, photo_release = asyncio.Event(), asyncio.Event()
        async def primary(kind, payload):
            if payload['path'].startswith('/api/employee-photo/'):
                photo_started.set()
                await photo_release.wait()
                result = self.reply(payload)
                return {**result, 'headers': [['Content-Type', 'image/jpeg']], 'body_base64': '/9j/2Q=='}
            change = {'table': 'employees', 'key': {'id': 1}, 'action': 'upsert',
                      'row': {'id': 1, 'employee_id': '0000001', 'checked_in_at': 'sql-confirmed'}}
            return self.reply(payload, revision=2, changes=[{'revision': 2, 'changes': [change]}])
        self.transport_impl = primary
        photo = asyncio.create_task(self.router.handle(self.request('/api/employee-photo/0000001')))
        await asyncio.wait_for(photo_started.wait(), 1)
        try:
            checkin = await asyncio.wait_for(self.router.handle(self.request('/api/scan/checkin', 'POST')), 0.3)
            self.assertEqual(checkin['status'], 200)
            self.assertEqual(self.store.state()['revision'], 2)
        finally:
            photo_release.set()
            response = await photo
        self.assertEqual(response['status'], 200)
        self.assertEqual(response['body'], b'\xff\xd8\xff\xd9')
        self.assertEqual(self.store.state()['revision'], 2)
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))

    async def test_late_photo_cannot_clear_a_new_offline_uncertainty(self):
        photo_started, photo_release = asyncio.Event(), asyncio.Event()
        async def primary(kind, payload):
            if payload['path'].startswith('/api/employee-photo/'):
                photo_started.set()
                await photo_release.wait()
                return self.reply(payload)
            raise PrimaryUnavailable()
        self.transport_impl = primary
        photo = asyncio.create_task(self.router.handle(self.request('/api/employee-photo/0000001')))
        await asyncio.wait_for(photo_started.wait(), 1)
        try:
            failed_draw = await asyncio.wait_for(self.router.handle(self.request('/api/draw', 'POST')), 0.3)
            self.assertEqual(failed_draw['status'], 503)
        finally:
            photo_release.set()
            response = await photo
        self.assertEqual(len(self.store.pending()), 1)
        self.assertNotIn('x-eventdesk-uncertain', self.headers(response))

    async def test_photo_outage_uses_no_sqlite_photo_path(self):
        self.transport_impl = self.unavailable
        response = await self.router.handle(self.request('/api/employee-photo/0000001'))
        self.assertEqual(response['status'], 503)
        self.assertEqual(self.fallback_calls, [])

    async def test_recovery_loads_only_metadata_and_one_pending_payload_at_a_time(self):
        self.transport_impl = self.unavailable
        await self.router.handle(self.request('/api/draw', 'POST'))
        async def resolved(kind, payload):
            return self.reply(payload, receipt_found=payload.get('receipt_only', False))
        self.transport_impl = resolved
        with patch.object(self.store, 'pending', side_effect=AssertionError('Do not load all payloads')):
            response = await self.router.handle(self.request())
        self.assertEqual(response['status'], 200)
        self.assertEqual(self.store.pending(), [])


if __name__ == '__main__':
    unittest.main()
