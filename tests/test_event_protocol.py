"""Validate the private event API boundary before application or SQL work."""
import base64
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photo_api import event_protocol as protocol


class EventProtocolTests(unittest.TestCase):
    def command(self, **changes):
        return {
            'schema_version': 1, 'operation_id': str(uuid.uuid4()),
            'method': 'POST', 'path': '/api/scan/checkin',
            'headers': [['Cookie', 'party_session=test'], ['Content-Type', 'application/json']],
            'body_base64': base64.b64encode(b'{"employee_id":"0000123"}').decode(),
            'client_ip': '127.0.0.1', 'after_revision': 4, **changes,
        }

    def test_valid_request_retains_exact_body_and_normalizes_headers(self):
        parsed = protocol.parse_command(self.command())
        self.assertEqual(parsed.body, b'{"employee_id":"0000123"}')
        self.assertEqual(parsed.headers[0], ('Cookie', 'party_session=test'))
        self.assertEqual(parsed.after_revision, 4)

    def test_transport_cursor_is_not_part_of_idempotency_hash(self):
        value = self.command()
        left = protocol.parse_command(value)
        right = protocol.parse_command({**value, 'after_revision': 99})
        self.assertEqual(protocol.command_hash(left), protocol.command_hash(right))
        other = protocol.parse_command({**value, 'body_base64': base64.b64encode(b'{}').decode()})
        self.assertNotEqual(protocol.command_hash(left), protocol.command_hash(other))

    def test_invalid_methods_paths_headers_and_identifiers_are_rejected(self):
        cases = [
            {'method': 'DELETE'}, {'path': '//example.invalid/api/me'},
            {'path': '/api/../api/settings'}, {'path': '/api/%73ettings'},
            {'path': '/api/employee-photo/000x123'}, {'path': '/__deployment/import'},
            {'path': '/static/app.js'}, {'path': '/api/settings#fragment'},
            {'path': '/api/settings\\suffix'}, {'path': '/api/unknown'},
            {'path': '//['},
            {'headers': [['Authorization', 'Bearer test']]},
            {'headers': [['Host', 'other.invalid']]},
            {'headers': [['Cookie', 'safe\r\nInjected: yes']]},
            {'headers': [['Cookie', '\ud800']]},
            {'headers': [['Cookie', 'a'], ['cookie', 'b']]},
            {'operation_id': 'not-a-uuid'}, {'operation_id': None},
            {'after_revision': True}, {'after_revision': -1},
            {'client_ip': 'not-an-address'}, {'body_base64': '%%%notbase64'},
            {'schema_version': True}, {'unexpected': 'field'},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                protocol.parse_command(self.command(**changes))

    def test_get_is_allowlisted_and_has_no_body(self):
        parsed = protocol.parse_command(self.command(method='GET', path='/api/attendance?page=2',
                                                     body_base64='', operation_id=None))
        self.assertIsNone(parsed.operation_id)
        for changes in ({'path': '/api/scan/checkin'}, {'body_base64': 'e30='}):
            value = {**self.command(method='GET', path='/api/stats', body_base64=''), **changes}
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_command(value)

    def test_snapshot_discovery_and_revision_pinned_pages(self):
        discovery = protocol.parse_snapshot({'schema_version': 1})
        self.assertIsNone(discovery.table)
        page = protocol.parse_snapshot({'schema_version': 1, 'table': 'employees',
                                        'offset': 0, 'limit': 100, 'revision': 3})
        self.assertEqual((page.table, page.revision, page.limit), ('employees', 3, 100))
        for extra in ({'limit': 1001}, {'table': 'dbo;DROP TABLE users'},
                      {'revision': None}, {'offset': -1}):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_snapshot({'schema_version': 1, 'table': 'employees',
                                         'offset': 0, 'limit': 100, 'revision': 3, **extra})

    def test_snapshot_page_accepts_one_thousand_rows_but_retains_typed_bounds(self):
        request = {'schema_version': 1, 'table': 'employees', 'offset': 1000,
                   'limit': 1000, 'revision': 3}
        page = protocol.parse_snapshot(request)
        self.assertEqual((page.offset, page.limit, page.revision), (1000, 1000, 3))
        for limit in (0, -1, 1001, True, 1000.0, '1000'):
            with self.subTest(limit=limit), self.assertRaises(protocol.ProtocolError):
                protocol.parse_snapshot({**request, 'limit': limit})

    def test_replay_and_receipt_modes_are_typed_and_limited_to_safe_routes(self):
        for changes in ({'replay': 1}, {'replay': True}, {'queue_proof': 'f' * 64},
                        {'cancel_if_missing': True}, {'recorded_at': 'not-a-time'},
                        {'recorded_at': '2026-11-17T10:00:00'},
                        {'recorded_at': '2026-11-17T10:00:00+07:00'},
                        {'path': '/api/draw', 'recorded_at': '2026-11-17T00:00:00Z'}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ProtocolError):
                protocol.parse_command(self.command(**changes))
        parsed = protocol.parse_command(self.command(receipt_only=True, cancel_if_missing=True))
        self.assertTrue(parsed.cancel_if_missing)

    def test_payload_limits_and_response_validation_fail_closed(self):
        from unittest.mock import patch
        with patch.object(protocol, 'MAX_BODY_BYTES', 2), self.assertRaises(protocol.ProtocolError):
            protocol.parse_command(self.command())
        with patch.object(protocol, 'MAX_REQUEST_BYTES', 2), self.assertRaises(protocol.ProtocolError):
            protocol.decode_json(b'{"schema_version":1}')
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_response({'status': True, 'body': b''})
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_value(float('nan'))
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_value({'$bytes': '%%%bad'})

    def test_strict_json_rejects_duplicate_fields_and_nonfinite_values(self):
        for raw in (b'{"schema_version":1,"schema_version":2}', b'{"x":NaN}', b'[]',
                    b'\xff\xfe{\x00}\x00'):
            with self.assertRaises(protocol.ProtocolError):
                protocol.decode_json(raw)

    def test_replication_values_round_trip_binary_without_losing_unicode(self):
        source = {'id': 7, 'name': '\u0e04\u0e19\u0e17\u0e14\u0e2a\u0e2d\u0e1a', 'image': b'\x00\xff', 'empty': None}
        encoded = protocol.encode_value(source)
        self.assertEqual(encoded['image'], {'$bytes': 'AP8='})
        self.assertEqual(protocol.decode_value(encoded), source)

    def test_app_response_filters_hop_by_hop_headers_and_encodes_bytes(self):
        value = protocol.encode_response({'status': 200, 'headers': [
            ('Content-Type', 'image/png'), ('Set-Cookie', 'a=b; Secure'),
            ('Connection', 'close'), ('Transfer-Encoding', 'chunked')], 'body': b'\x89PNG'})
        self.assertEqual(value['body_base64'], 'iVBORw==')
        self.assertEqual([item[0] for item in value['headers']], ['Content-Type', 'Set-Cookie'])
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_response({'status': 200, 'headers': [('X-Bad', 'a\r\nb')], 'body': b''})


if __name__ == '__main__':
    unittest.main()
