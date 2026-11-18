"""Trusted offline attendance keeps its original time after direct SQL writes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_event_backend as fixtures
import app
from cloudflare.event_primary import _captured
from photo_api import event_protocol as protocol


class DirectReplayTests(unittest.TestCase):
    command = fixtures.EventBackendTests.command
    staff = fixtures.EventBackendTests.staff

    def setUp(self):
        fixtures.EventBackendTests.setUp(self)
        # Real UTC values keep the replay age window and session checks meaningful.
        from datetime import datetime, timedelta, timezone
        self.early = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec='seconds')
        self.late = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec='seconds')
        self.cookie, self.csrf = self.staff()

    def checkin(self, employee='0000123', **fields):
        return self.command('/api/scan/checkin', data={'code': employee},
                            headers=[('Cookie', self.cookie), ('X-CSRF-Token', self.csrf)],
                            include_replica=False, **fields)

    def replay(self, source, *, same_id=False, recorded=None):
        command = {**source, 'operation_id': source['operation_id'] if same_id else str(uuid.uuid4()),
                   'recorded_at': recorded or self.early, 'replay': True, 'queue_proof': '0' * 64}
        return {**command, 'queue_proof': protocol.offline_proof(self.config['secret'], protocol.parse_command(command))}

    @staticmethod
    def payload(response):
        return json.loads(base64.b64decode(response['body_base64']))

    def direct(self, command):
        with patch.object(app, 'now', return_value=self.late):
            return self.engine.handle(command)

    def attendance(self, employee='0000123'):
        with self.store.transaction() as tx:
            return tx.db.execute('''SELECT e.checked_in_at,c.checked_in_at AS attendance_time,
                (SELECT COUNT(*) FROM event_checkins c2 WHERE c2.employee_id=e.id) AS records
                FROM employees e JOIN event_checkins c ON c.employee_id=e.id WHERE e.employee_id=?''',
                (employee,)).fetchone()

    def test_earlier_offline_operation_merges_time_without_duplicate_or_exception_overwrite(self):
        direct = self.checkin()
        self.direct(direct)
        with self.store.transaction() as tx:
            employee_id = tx.db.execute('SELECT id FROM employees WHERE employee_id=?', ('0000123',)).fetchone()[0]
            tx.db.execute('''INSERT INTO checkin_wristband_exceptions
                (employee_id,reason,detail,recorded_by,recorded_at) VALUES (?,'lost','keep','operator',?)''',
                (employee_id, self.late))
        replay = self.replay(direct)
        result = self.engine.handle(replay)
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.payload(result)['person']['checked_in_at'], self.early)
        self.assertEqual(tuple(self.attendance()), (self.early, self.early, 1))
        with self.store.transaction() as tx:
            exception = tx.db.execute('SELECT reason,detail,recorded_at FROM checkin_wristband_exceptions').fetchone()
            self.assertEqual(tuple(exception), ('lost', 'keep', self.late))
            logs = tx.db.execute("SELECT occurred_at FROM event_log WHERE action='checkin'").fetchall()
            self.assertEqual([row[0] for row in logs], [self.early])
            audit = tx.db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='attendance.replay_time_reconciled'").fetchone()[0]
            self.assertEqual(audit, 1)
        retry = self.engine.handle(replay)
        self.assertTrue(retry['receipt_found'])
        self.assertEqual(retry['body_base64'], result['body_base64'])

    def test_same_uuid_updates_receipt_atomically_and_normal_retry_returns_correct_time(self):
        command = self.checkin()
        first = self.direct(command)
        result = self.engine.handle(self.replay(command, same_id=True))
        self.assertTrue(result['receipt_found'])
        self.assertGreater(result['revision'], first['revision'])
        self.assertEqual(self.payload(result)['person']['checked_in_at'], self.early)
        retry = self.engine.handle(command)
        self.assertEqual(retry['body_base64'], result['body_base64'])
        self.assertEqual(tuple(self.attendance()), (self.early, self.early, 1))

    def test_invalid_proof_cannot_adjust_matching_receipt_or_execute_uncommitted_replay(self):
        command = self.checkin()
        original = self.direct(command)
        replay = {**self.replay(command, same_id=True), 'queue_proof': '0' * 64}
        existing = self.engine.handle(replay)
        self.assertEqual(existing['body_base64'], original['body_base64'])
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))
        with self.assertRaises(protocol.ProtocolError):
            self.engine.handle({**replay, 'operation_id': str(uuid.uuid4())})

    def test_revoked_session_resolves_committed_receipt_but_cannot_adjust_or_add_replay(self):
        command = self.checkin()
        original = self.direct(command)
        with self.store.transaction() as tx:
            tx.db.execute('UPDATE staff_sessions SET ended_at=?', (self.late,))
        replay = self.replay(command, same_id=True)
        existing = self.engine.handle(replay)
        self.assertEqual(existing['body_base64'], original['body_base64'])
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))
        with self.assertRaises(protocol.ProtocolError):
            self.engine.handle(self.replay(command))

    def test_normal_request_cannot_backdate_existing_attendance(self):
        command = self.checkin()
        original = self.direct(command)
        retry = self.engine.handle({**command, 'recorded_at': self.early})
        self.assertEqual(retry['body_base64'], original['body_base64'])
        fresh = self.engine.handle({**self.checkin(), 'recorded_at': self.early})
        self.assertEqual(fresh['status'], 200)
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))

    def test_receipt_after_reset_does_not_resurrect_checkin_or_move_unrelated_log(self):
        command = self.checkin()
        original = self.direct(command)
        with self.store.transaction() as tx:
            tx.db.execute('DELETE FROM event_checkins')
            tx.db.execute('UPDATE employees SET checked_in_at=NULL')
        result = self.engine.handle(self.replay(command, same_id=True))
        self.assertEqual(result['body_base64'], original['body_base64'])
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)
            self.assertIsNone(tx.db.execute('SELECT checked_in_at FROM employees').fetchone()[0])
            self.assertEqual(tx.db.execute("SELECT occurred_at FROM event_log WHERE action='checkin'").fetchone()[0], self.late)

    def remove_attendance(self):
        from datetime import datetime, timedelta, timezone
        removed = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec='seconds')
        with self.store.transaction() as tx, self.engine._bind(tx), patch.object(app, 'now', return_value=removed):
            result = app.remove_attendance({'scope': 'one', 'employee_id': '0000123', 'reason': 'fixture reset',
                                          'confirmation': 'REMOVE', 'expected_count': 1}, 'fixture-admin')
        self.assertEqual(result['removed'], 1)

    def test_old_receipt_does_not_backdate_new_attendance_after_real_admin_reset(self):
        from datetime import datetime, timedelta, timezone
        old = self.checkin()
        original = self.direct(old)
        self.remove_attendance()
        self.late = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec='seconds')
        self.direct(self.checkin())
        result = self.engine.handle(self.replay(old, same_id=True))
        self.assertEqual(result['body_base64'], original['body_base64'])
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))

    def test_uncommitted_pre_reset_replay_is_conflict_without_resurrecting_attendance(self):
        command = self.checkin()
        self.direct(self.checkin())
        self.remove_attendance()
        result = self.engine.handle(self.replay(command, same_id=True))
        self.assertEqual(result['status'], 409)
        self.assertEqual(self.payload(result)['code'], 'attendance_removed')
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)
            self.assertIsNone(tx.db.execute('SELECT checked_in_at FROM employees').fetchone()[0])
        retry = self.engine.handle(self.replay(command, same_id=True))
        self.assertTrue(retry['receipt_found'])
        self.assertEqual(retry['body_base64'], result['body_base64'])

    def test_utc_z_recorded_time_is_compared_as_instant_and_later_replay_never_moves_time_forward(self):
        command = self.checkin()
        self.direct(command)
        early_z = self.early.replace('+00:00', 'Z')
        result = self.engine.handle(self.replay(command, recorded=early_z))
        self.assertEqual(self.payload(result)['person']['checked_in_at'], early_z)
        self.assertEqual(json.loads(_captured(result)['body'])['checkin']['checked_in_at'], early_z)
        later = self.engine.handle(self.replay(command, recorded=self.late))
        self.assertEqual(self.payload(later)['person']['checked_in_at'], early_z)
        self.assertEqual(tuple(self.attendance()), (early_z, early_z, 1))

    def test_receipt_only_replay_does_not_adjust_attendance(self):
        command = self.checkin()
        original = self.direct(command)
        found = self.engine.handle({**self.replay(command, same_id=True), 'receipt_only': True})
        self.assertEqual(found['body_base64'], original['body_base64'])
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))

    def test_receipt_update_failure_rolls_back_timestamp_audit_and_journal_together(self):
        from photo_api.event_store import EventTransaction
        command = self.checkin()
        original = self.direct(command)
        query = EventTransaction._query
        def fail_update(tx, sql, values=()):
            if sql.startswith('UPDATE ') and 'EventOperationReceipt' in sql:
                raise RuntimeError('fixture receipt write failure')
            return query(tx, sql, values)
        with patch.object(EventTransaction, '_query', fail_update), self.assertRaises(RuntimeError):
            self.engine.handle(self.replay(command, same_id=True))
        self.assertEqual(tuple(self.attendance()), (self.late, self.late, 1))
        with self.store.transaction() as tx:
            self.assertEqual(tx.revision, original['revision'])
            self.assertEqual(tx.receipt(command['operation_id'])['response']['body_base64'], original['body_base64'])
            self.assertEqual(tx.db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='attendance.replay_time_reconciled'").fetchone()[0], 0)

    def test_only_the_matching_latest_checkin_log_changes(self):
        command = self.checkin()
        self.direct(command)
        with self.store.transaction() as tx:
            employee_id = tx.db.execute('SELECT id FROM employees WHERE employee_id=?', ('0000123',)).fetchone()[0]
            tx.db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (?,?,?,?)',
                          (employee_id, 'verify', 'fixture', self.late))
            tx.db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (?,?,?,?)',
                          (employee_id, 'checkin', 'archived-fixture', self.early))
        self.engine.handle(self.replay(command))
        with self.store.transaction() as tx:
            verify = tx.db.execute("SELECT occurred_at FROM event_log WHERE action='verify'").fetchone()[0]
            archive = tx.db.execute("SELECT occurred_at FROM event_log WHERE actor='archived-fixture'").fetchone()[0]
            self.assertEqual(verify, self.late)
            self.assertEqual(archive, self.early)

    def test_concurrent_ten_employee_direct_checkins_and_queued_replays_keep_one_each(self):
        employees = [f'{number:07}' for number in range(123, 133)]
        with patch.object(app, 'DB_PATH', self.path):
            app.import_employees([(employee, f'Fixture {index}', 'Test')
                                  for index, employee in enumerate(employees[1:])], 'test')
        commands = [self.checkin(employee) for employee in employees]
        with patch.object(app, 'now', return_value=self.late), ThreadPoolExecutor(max_workers=10) as pool:
            direct = list(pool.map(self.engine.handle, [*commands, *commands]))
        self.assertTrue(all(result['status'] == 200 for result in direct))
        queued = [self.replay(command) for command in commands]
        with ThreadPoolExecutor(max_workers=10) as pool:
            replies = list(pool.map(self.engine.handle, [*queued, *queued]))
        self.assertTrue(all(result['status'] == 200 for result in replies))
        for employee in employees:
            self.assertEqual(tuple(self.attendance(employee)), (self.early, self.early, 1))
        with self.store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 10)
            self.assertEqual(tx.db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='attendance.replay_time_reconciled'").fetchone()[0], 10)


if __name__ == '__main__':
    unittest.main()
