"""Exercise the hosted request bridge against the existing application rules."""
from contextlib import redirect_stderr
from email.message import Message
import io
import json
import os
import secrets
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from cloudflare.handler_adapter import dispatch


class HandlerAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_password = secrets.token_hex(24)
        cls.staff_password = secrets.token_hex(24)
        def credential(password, salt):
            return {'salt': salt, 'hash': app.password_hash(password, salt)}
        cls.config = {
            'event_name': 'Hosted Test Event', 'secret': '01' * 32,
            'admin_password': credential(cls.admin_password, 'ab' * 16),
            'staff_password': credential(cls.staff_password, 'cd' * 16),
        }

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(app, 'DB_PATH', Path(self.temp.name) / 'test.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.env_patch = patch.dict(os.environ, {
            'PARTY_OPEN_STAFF': '0', 'PARTY_COOKIE_SECURE': '1',
            'PARTY_PUBLIC_ORIGIN': 'https://eventdesk.test',
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        app.init_db()

    def request(self, method, path, payload=None, cookie=None, csrf=None, **kwargs):
        headers = {'Host': 'eventdesk.test', 'Origin': 'https://eventdesk.test'}
        if cookie:
            headers['Cookie'] = cookie
        if csrf:
            headers['X-CSRF-Token'] = csrf
        body = b''
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        return dispatch(app, method, path, headers, body, config=self.config, **kwargs)

    def login(self, admin=False):
        password = self.admin_password if admin else self.staff_password
        response = self.request('POST', '/api/login', {'password': password})
        self.assertEqual(response['status'], 200)
        cookie = next(value for name, value in response['headers'] if name.lower() == 'set-cookie')
        return cookie.split(';', 1)[0], json.loads(response['body'])['csrf']

    def test_request_uses_original_security_headers_without_constructing_a_socket_handler(self):
        with patch.object(app.PartyHandler, '__init__', side_effect=AssertionError('Socket handler initialized')):
            response = self.request('GET', '/api/me')
        self.assertEqual(response['status'], 200)
        self.assertFalse(json.loads(response['body'])['authenticated'])
        headers = dict(response['headers'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(int(headers['Content-Length']), len(response['body']))

    def test_login_cookie_is_accepted_by_the_next_request(self):
        cookie, _ = self.login(admin=True)
        response = self.request('GET', '/api/me', cookie=cookie)
        self.assertEqual(response['status'], 200)
        self.assertEqual(json.loads(response['body'])['role'], 'admin')

    def test_public_font_has_font_mime_type_and_preserves_static_allowlist(self):
        with TemporaryDirectory() as folder:
            frontend = Path(folder)
            (frontend / 'fonts').mkdir()
            content = b'synthetic-font-mime-fixture'
            (frontend / 'fonts' / 'fc-pride.otf').write_bytes(content)
            with patch.object(app, 'FRONTEND', frontend):
                response = self.request('GET', '/static/fonts/fc-pride.otf')
        self.assertEqual(response['status'], 200)
        self.assertEqual(dict(response['headers'])['Content-Type'], 'font/otf')
        self.assertEqual(response['body'], content)
        self.assertEqual(self.request('GET', '/static/fonts/../app.py')['status'], 404)

    def test_missing_optional_static_asset_returns_not_found(self):
        with TemporaryDirectory() as folder, patch.object(app, 'FRONTEND', Path(folder)):
            response = self.request('GET', '/static/fonts/fc-pride.otf')
        self.assertEqual(response['status'], 404)
        self.assertNotIn(b'Traceback', response['body'])

    def test_session_cookie_preserves_secure_httponly_and_samesite_attributes(self):
        response = self.request('POST', '/api/login', {'password': self.admin_password})
        cookie = next(value for name, value in response['headers'] if name == 'Set-Cookie')
        for attribute in ('HttpOnly', 'SameSite=Strict', 'Secure', 'Path=/'):
            self.assertIn(attribute, cookie)

    def test_anonymous_and_staff_admin_requests_keep_original_authorization(self):
        self.assertEqual(self.request('GET', '/api/stats')['status'], 401)
        cookie, _ = self.login()
        response = self.request('GET', '/api/admin/activity', cookie=cookie)
        self.assertEqual(response['status'], 403)

    def test_lookup_does_not_check_in_and_confirmation_is_idempotent(self):
        app.import_employees([('E0001', 'Test Employee', 'IT')], 'fixture')
        with app.db_connection() as db:
            token = db.execute('SELECT qr_token FROM employees').fetchone()[0]
        cookie, csrf = self.login()
        payload = {'code': 'PARTY:' + token}
        response = self.request('POST', '/api/lookup', payload, cookie, csrf)
        self.assertEqual(response['status'], 200)
        self.assertIsNone(json.loads(response['body'])['person']['checked_in_at'])
        for state in ('done', 'already_done'):
            response = self.request('POST', '/api/scan/checkin', payload, cookie, csrf)
            self.assertEqual(response['status'], 200)
            self.assertEqual(json.loads(response['body'])['state'], state)
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 1)

    def test_bad_csrf_is_an_error_result_without_saving_attendance(self):
        app.import_employees([('E0002', 'Other Employee', 'IT')], 'fixture')
        cookie, _ = self.login()
        response = self.request('POST', '/api/scan/checkin', {'code': 'PARTY:invalid'}, cookie, 'wrong-token')
        self.assertEqual(response['status'], 403)
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)

    def test_malformed_json_remains_a_400_result_for_transaction_rollback(self):
        response = dispatch(app, 'POST', '/api/login', {'Content-Type': 'application/json'}, b'{', config=self.config)
        self.assertEqual(response['status'], 400)
        self.assertEqual(json.loads(response['body'])['error'], 'Invalid JSON.')

    def test_unexpected_application_error_is_a_500_result(self):
        cookie, _ = self.login()
        with patch.object(app, 'get_stats', side_effect=RuntimeError('private database details')):
            with redirect_stderr(io.StringIO()):
                response = self.request('GET', '/api/stats', cookie=cookie)
        self.assertEqual(response['status'], 500)
        self.assertNotIn(b'private database details', response['body'])

    def test_download_bytes_and_headers_are_preserved(self):
        cookie, _ = self.login(admin=True)
        response = self.request('GET', '/api/export/attendance', cookie=cookie)
        self.assertEqual(response['status'], 200)
        self.assertIsInstance(response['body'], bytes)
        self.assertIn(b'employee_id', response['body'])
        self.assertIn('attachment;', dict(response['headers'])['Content-Disposition'])

    def test_projector_events_return_one_authorized_snapshot_without_sleeping(self):
        cookie, _ = self.login()
        with patch.object(app.time, 'sleep', side_effect=AssertionError('Blocking event loop')):
            response = self.request('GET', '/api/projector/events', cookie=cookie)
        self.assertEqual(response['status'], 200)
        self.assertTrue(dict(response['headers'])['Content-Type'].startswith('text/event-stream'))
        self.assertIn(b'event: draw-state\n', response['body'])
        self.assertIn(b'retry: 3000\n', response['body'])
        anonymous = self.request('GET', '/api/projector/events')
        self.assertEqual(anonymous['status'], 401)

    def test_unknown_routes_and_unsupported_methods_have_error_statuses(self):
        self.assertEqual(self.request('GET', '/api/not-a-route')['status'], 404)
        response = self.request('PATCH', '/api/me')
        self.assertEqual(response['status'], 405)
        self.assertEqual(dict(response['headers'])['Allow'], 'GET, POST')

    def test_trusted_transport_body_length_is_computed_from_actual_bytes(self):
        body = json.dumps({'password': self.admin_password}).encode()
        headers = Message()
        headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = '0'
        response = dispatch(app, 'POST', '/api/login', headers, body, config=self.config)
        self.assertEqual(response['status'], 200)

    def test_excessive_body_is_rejected_before_business_logic(self):
        with patch.object(app, 'MAX_BODY', 4):
            response = dispatch(app, 'POST', '/api/login', {}, b'12345', config=self.config)
        self.assertEqual(response['status'], 413)

    def test_configuration_can_be_loaded_from_the_patched_application(self):
        with patch.object(app, 'load_config', return_value=self.config) as load:
            response = dispatch(app, 'GET', '/api/me', {}, b'')
        load.assert_called_once_with()
        self.assertEqual(json.loads(response['body'])['event_name'], 'Hosted Test Event')

    def test_multiple_response_cookies_remain_separate_and_ordered(self):
        def send_cookies(handler):
            handler.send_response(200)
            handler.send_header('Set-Cookie', 'first=one; HttpOnly')
            handler.send_header('Set-Cookie', 'second=two; HttpOnly')
            handler.end_headers()
            handler.wfile.write(b'cookies')
        with patch.object(app.PartyHandler, 'do_GET', send_cookies):
            response = self.request('GET', '/api/me')
        self.assertEqual(response['body'], b'cookies')
        self.assertEqual(response['headers'], [
            ('Set-Cookie', 'first=one; HttpOnly'), ('Set-Cookie', 'second=two; HttpOnly'),
        ])

    def test_invalid_paths_and_header_injection_do_not_reach_the_handler(self):
        for path, headers in [
            ('https://other.test/api/me', {}),
            ('/api/me', {'Bad Header': 'value'}),
            ('/api/me', {'Origin': 'https://eventdesk.test\r\nInjected: value'}),
        ]:
            with self.subTest(path=path, headers=headers):
                response = dispatch(app, 'GET', path, headers, config=self.config)
                self.assertEqual(response['status'], 400)


if __name__ == '__main__':
    unittest.main()
