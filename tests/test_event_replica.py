"""The fallback never activates partial, out-of-order or untrusted SQL data."""
import json
import sqlite3
import unittest

from cloudflare.replica import ReplicaError, ReplicaStore


TABLES = {
    'employees': {'columns': ('id', 'employee_id', 'name', 'photo'),
                  'primary_key': ('id',), 'foreign_keys': [],
                  'column_info': [{'name': 'photo', 'type': 'BLOB'}]},
    'checkins': {'columns': ('id', 'employee_id'), 'primary_key': ('id',),
                 'foreign_keys': [(0, 0, 'employees', 'employee_id', 'id')], 'column_info': []},
}


class ReplicaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''CREATE TABLE employees(id INTEGER PRIMARY KEY,employee_id TEXT UNIQUE,name TEXT,photo BLOB);
            CREATE TABLE checkins(id INTEGER PRIMARY KEY,employee_id INTEGER REFERENCES employees(id));''')
        self.store = ReplicaStore(self.db, TABLES)
        self.store.initialize()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def employee(self, identity=1, name='Synthetic employee'):
        return {'table': 'employees', 'key': {'id': identity}, 'action': 'upsert',
                'row': {'id': identity, 'employee_id': str(identity).zfill(7), 'name': name, 'photo': None}}

    def batch(self, revision, *changes):
        return {'revision': revision, 'changes': list(changes)}

    def seed(self):
        with self.db:
            self.store.replace_snapshot({'employees': [self.employee()['row']], 'checkins': []}, 4)

    def test_complete_snapshot_preserves_unicode_and_blob(self):
        person = self.employee()['row'] | {'name': '\u0e01\u0e02', 'photo': {'$bytes': '/9j/2Q=='}}
        with self.db:
            self.store.replace_snapshot({'employees': [person], 'checkins': []}, 4)
        self.assertEqual(self.db.execute('SELECT name,photo FROM employees').fetchone(), ('\u0e01\u0e02', b'\xff\xd8\xff\xd9'))
        self.assertEqual(self.store.state()['revision'], 4)
        self.assertTrue(self.store.state()['ready'])

    def test_missing_table_or_duplicate_key_never_replaces_saved_copy(self):
        self.seed()
        for snapshot in ({'employees': []}, {'employees': [self.employee()['row']] * 2, 'checkins': []}):
            with self.assertRaises(ReplicaError), self.db:
                self.store.replace_snapshot(snapshot, 5)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)
            self.assertEqual(self.store.state()['revision'], 4)

    def test_revision_gap_and_unknown_columns_are_rejected_before_changes(self):
        self.seed()
        for batches in ([self.batch(6, self.employee(2))],
                        [self.batch(5, self.employee(2)), self.batch(6, self.employee(3) | {'table': 'users; DROP TABLE employees'})],
                        [self.batch(5, self.employee(2) | {'row': self.employee(2)['row'] | {'untrusted': 'value'}})]):
            with self.assertRaises(ReplicaError), self.db:
                self.store.apply_batches(batches, 6, True)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)
            self.assertEqual(self.store.state()['revision'], 4)

    def test_incremental_batches_keep_stable_ids_and_delete_in_fk_order(self):
        self.seed()
        attendance = {'table': 'checkins', 'key': {'id': 1}, 'action': 'upsert', 'row': {'id': 1, 'employee_id': 1}}
        with self.db:
            self.store.apply_batches([self.batch(5, attendance, self.employee(name='Updated'))], 5, True)
        self.assertEqual(self.db.execute('SELECT id,name FROM employees').fetchone(), (1, 'Updated'))
        with self.db:
            self.store.apply_batches([self.batch(6,
                {'table': 'employees', 'key': {'id': 1}, 'action': 'delete', 'row': None},
                {'table': 'checkins', 'key': {'id': 1}, 'action': 'delete', 'row': None})], 6, True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 0)

    def test_replayed_batches_are_idempotent_but_future_batches_stay_incomplete(self):
        self.seed()
        with self.db:
            self.store.apply_batches([self.batch(5, self.employee(2))], 6, False)
            self.store.apply_batches([self.batch(5, self.employee(2))], 6, False)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 2)
        self.assertFalse(self.store.state()['ready'])
        with self.db:
            self.store.apply_batches([self.batch(6, self.employee(3))], 6, True)
        self.assertTrue(self.store.state()['ready'])

    def test_same_operation_cannot_be_reused_for_another_intent(self):
        payload = {'operation_id': 'operation-1', 'method': 'POST', 'path': '/api/scan/checkin', 'body_base64': 'e30='}
        with self.db:
            self.store.enqueue(payload, 'checkin', {'status': 202})
            self.store.enqueue(payload, 'checkin', {'status': 202})
        self.assertEqual(len(self.store.pending()), 1)
        with self.assertRaises(ReplicaError), self.db:
            self.store.enqueue(payload | {'body_base64': 'e10='}, 'checkin', {'status': 202})
        self.assertEqual(self.store.pending()[0]['envelope'], payload)

    def test_transport_time_and_address_changes_preserve_the_saved_intent(self):
        payload = {'operation_id': 'operation-1', 'method': 'POST', 'path': '/api/scan/checkin',
                   'body_base64': 'e30=', 'recorded_at': '2026-11-17T01:00:00+00:00', 'client_ip': '192.0.2.1'}
        with self.db:
            self.store.enqueue(payload, 'checkin', {'status': 202})
            self.store.enqueue(payload | {'recorded_at': '2026-11-17T01:00:04+00:00',
                                           'client_ip': '192.0.2.2'}, 'checkin', {'status': 202})
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.find('operation-1')['envelope'], payload)

    def test_only_a_matching_uncertain_checkin_can_be_promoted_offline(self):
        payload = {'operation_id': 'operation-1', 'method': 'POST', 'path': '/api/scan/checkin', 'body_base64': 'e30='}
        response = {'status': 202, 'headers': [], 'body_base64': 'e30='}
        with self.db:
            self.store.enqueue(payload, 'uncertain', {})
            self.store.enqueue(payload, 'checkin', response)
            self.store.enqueue(payload, 'uncertain', {})
        saved = self.store.find('operation-1')
        self.assertEqual(saved['kind'], 'checkin')
        self.assertEqual(saved['response'], response)
        unsafe = payload | {'operation_id': 'draw-1', 'path': '/api/draw'}
        with self.db:
            self.store.enqueue(unsafe, 'uncertain', {})
        with self.assertRaises(ReplicaError), self.db:
            self.store.enqueue(unsafe, 'checkin', response)

    def test_queue_survives_failed_ack_and_is_only_removed_by_explicit_ack(self):
        payload = {'operation_id': 'operation-1', 'method': 'POST', 'path': '/api/scan/checkin', 'body_base64': 'e30='}
        with self.db:
            self.store.enqueue(payload, 'checkin', {'status': 202})
        try:
            with self.db:
                self.store.acknowledge('operation-1')
                raise RuntimeError('Interrupted acknowledgement')
        except RuntimeError:
            pass
        self.assertEqual(len(self.store.pending()), 1)
        with self.db:
            self.store.acknowledge('operation-1')
        self.assertEqual(self.store.pending(), [])

    def test_snapshot_cannot_erase_pending_intents(self):
        self.seed()
        with self.db:
            self.store.enqueue({'operation_id': 'pending-1', 'method': 'POST', 'path': '/api/scan/checkin'},
                               'checkin', {'status': 202})
        with self.assertRaises(ReplicaError), self.db:
            self.store.replace_snapshot({'employees': [], 'checkins': []}, 5)
        self.assertEqual(len(self.store.pending()), 1)

    def test_replacement_references_and_cleared_references_are_atomic(self):
        tables = {'wristbands': {'columns': ('id', 'replacement'), 'primary_key': ('id',),
            'foreign_keys': [(0, 0, 'wristbands', 'replacement', 'id')], 'column_info': []}}
        self.db.execute('CREATE TABLE wristbands(id INTEGER PRIMARY KEY,replacement INTEGER REFERENCES wristbands(id))')
        store = ReplicaStore(self.db, tables)
        with self.db:
            store.replace_snapshot({'wristbands': [{'id': 1, 'replacement': 2}, {'id': 2, 'replacement': None}]}, 1)
        with self.db:
            store.apply_batches([self.batch(2,
                {'table': 'wristbands', 'key': {'id': 2}, 'action': 'delete', 'row': None},
                {'table': 'wristbands', 'key': {'id': 1}, 'action': 'upsert', 'row': {'id': 1, 'replacement': None}})], 2, True)
        self.assertEqual(self.db.execute('SELECT id,replacement FROM wristbands').fetchall(), [(1, None)])

    def test_invalid_relationship_rolls_back_at_commit(self):
        self.seed()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.store.apply_batches([self.batch(5, {'table': 'checkins', 'key': {'id': 1},
                    'action': 'upsert', 'row': {'id': 1, 'employee_id': 999}})], 5, True)
        self.assertEqual(self.store.state()['revision'], 4)

    def test_invalidating_a_backup_keeps_the_last_data_and_pending_queue(self):
        self.seed()
        with self.db:
            self.store.enqueue({'operation_id': 'pending-1', 'method': 'POST',
                                'path': '/api/scan/checkin'}, 'checkin', {'status': 202})
            self.store.invalidate()
        self.assertFalse(self.store.state()['ready'])
        self.assertTrue(self.store.state()['seeded'])
        self.assertEqual(self.store.state()['revision'], 4)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 1)
        self.assertTrue(self.store.has_pending())

    def test_pending_json_uses_binary_values_so_the_hosted_adapter_can_chunk_large_uploads(self):
        payload = {'operation_id': 'upload-1', 'method': 'POST', 'path': '/api/rewards/draft',
                   'body_base64': 'A' * (3 * 1024 * 1024)}
        with self.db:
            self.store.enqueue(payload, 'uncertain', {'status': 503})
        kinds = self.db.execute('SELECT typeof(envelope_json),typeof(response_json) FROM _eventdesk_outbox').fetchone()
        self.assertEqual(kinds, ('blob', 'blob'))
        self.assertEqual(self.store.find('upload-1')['envelope'], payload)

    def test_acknowledgement_reclaims_large_queue_payloads_without_touching_business_images(self):
        self.seed()
        payload = {'operation_id': 'upload-1', 'method': 'POST', 'path': '/api/rewards/draft',
                   'body_base64': 'A' * (3 * 1024 * 1024)}
        with self.db:
            self.store.enqueue(payload, 'uncertain', {'status': 503})
        count = self.db.execute('SELECT COUNT(*) FROM _eventdesk_outbox_payloads').fetchone()[0]
        self.assertGreater(count, 1)
        self.db.execute('UPDATE employees SET photo=?', (b'protected-business-photo',))
        with self.db:
            self.store.acknowledge('upload-1')
        self.assertFalse(self.store.has_pending())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM _eventdesk_outbox_payloads').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT photo FROM employees').fetchone()[0], b'protected-business-photo')


if __name__ == '__main__':
    unittest.main()
