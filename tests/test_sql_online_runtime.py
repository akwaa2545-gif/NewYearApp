"""The deployed SQL runtime keeps serving when every local storage call fails."""
import base64
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock

from cloudflare.event_runtime import HostedSQLRuntime


class SQLOnlineRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_runtime_keeps_sql_read_and_write_available_without_sqlite(self):
        owner = SimpleNamespace(config={'secret': '12' * 32},
                                initialize=Mock(side_effect=RuntimeError('Daily SQLite limit')))
        runtime = HostedSQLRuntime(owner, SimpleNamespace(), None)
        calls = []

        async def transport(kind, payload):
            calls.append(payload)
            return {'schema_version': 1, 'operation_id': payload['operation_id'],
                    'after_revision': payload['after_revision'], 'revision': 10,
                    'status': 200, 'headers': [['Content-Type', 'application/json']],
                    'body_base64': base64.b64encode(b'{"ok":true}').decode(),
                    'changes': [], 'replica_complete': False, 'snapshot_required': False}

        runtime.primary.transport = transport
        for method, path in (('GET', '/api/me'), ('POST', '/api/scan/checkin')):
            result = await runtime.primary.handle({
                'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'method': method,
                'path': path, 'headers': [], 'body_base64': '', 'client_ip': '192.0.2.10'})
            self.assertEqual(result['status'], 200)
            self.assertEqual(dict(result['headers'])['X-EventDesk-Storage'], 'sql')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call['include_replica'] is False for call in calls))
        owner.initialize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
