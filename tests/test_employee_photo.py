"""Private employee-photo proxy tests; all upstream responses are synthetic."""
import io
import json
import os
import secrets
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


TOKEN = secrets.token_hex(32)
JPEG = b'\xff\xd8\xffsynthetic-photo\xff\xd9'


class PhotoResponse(io.BytesIO):
    def __init__(self, body=JPEG, content_type='image/jpeg', length=None, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Type': content_type}
        if length is not None:
            self.headers['Content-Length'] = str(length)

    def read1(self, size=-1):
        return self.read(size)


class EmployeePhotoTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'PARTY_PHOTO_API_TOKEN': TOKEN})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def fetch(self, response=None, error=None, employee_id='1001519'):
        opener = unittest.mock.Mock()
        opener.open.side_effect = error
        opener.open.return_value = response or PhotoResponse()
        with patch('urllib.request.build_opener', return_value=opener) as builder:
            result = app.fetch_employee_photo(employee_id)
        return result, opener, builder

    def test_fetch_uses_fixed_https_origin_private_bearer_and_no_redirect_handler(self):
        result, opener, builder = self.fetch()
        self.assertEqual(result, JPEG)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://eventdesk-photo-pilot.pages.dev/v1/employees/1001519/photo')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(request.get_header('Accept'), 'image/jpeg')
        self.assertEqual(request.get_header('User-agent'), 'EventDeskPhotoProxy/1.0')
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 5)
        redirect_handler = builder.call_args.args[0]
        self.assertIsNone(redirect_handler.redirect_request(None, None, 302, '', {}, 'https://other.invalid'))

    def test_disabled_and_invalid_ids_never_open_network(self):
        cases = [('1001519', '', 503), ('1001519', 'bad\ncredential', 503),
                 ('1001519/../../admin', TOKEN, 400),
                 ('１００１５１９', TOKEN, 400), ('1001519?x=1', TOKEN, 400)]
        with patch('urllib.request.build_opener') as opener:
            for employee_id, token, status in cases:
                with self.subTest(employee_id=employee_id, status=status):
                    with patch.dict(os.environ, {'PARTY_PHOTO_API_TOKEN': token}):
                        with self.assertRaises(app.EmployeePhotoError) as error:
                            app.fetch_employee_photo(employee_id)
                    self.assertEqual(error.exception.status, status)
            opener.assert_not_called()

    def test_fetch_supports_imported_employee_outside_original_pilot(self):
        result, opener, _ = self.fetch(employee_id='2412577')
        self.assertEqual(result, JPEG)
        self.assertEqual(opener.open.call_args.args[0].full_url,
                         'https://eventdesk-photo-pilot.pages.dev/v1/employees/2412577/photo')

    def test_invalid_response_mime_magic_size_length_and_encoding_are_rejected(self):
        cases = [PhotoResponse(content_type='text/html'), PhotoResponse(body=b'not-a-jpeg'),
                 PhotoResponse(body=JPEG[:-2]), PhotoResponse(body=JPEG + b'x' * (256 * 1024)),
                 PhotoResponse(length=262145), PhotoResponse(length='invalid'),
                 PhotoResponse(length=999), PhotoResponse(status=302)]
        encoded = PhotoResponse()
        encoded.headers['Content-Encoding'] = 'gzip'
        cases.append(encoded)
        for response in cases:
            with self.subTest(headers=response.headers, status=response.status):
                with self.assertRaises(app.EmployeePhotoError) as error:
                    self.fetch(response)
                self.assertEqual(error.exception.status, 503)

    def test_upstream_failures_are_sanitized(self):
        cases = [(HTTPError('https://secret.invalid', 404, TOKEN, {}, None), 404),
                 (HTTPError('https://secret.invalid', 302, TOKEN, {}, None), 503),
                 (HTTPError('https://secret.invalid', 401, TOKEN, {}, None), 503),
                 (URLError(TOKEN), 503), (TimeoutError(TOKEN), 503), (OSError(TOKEN), 503)]
        for upstream_error, status in cases:
            with self.subTest(status=status):
                with self.assertRaises(app.EmployeePhotoError) as error:
                    self.fetch(error=upstream_error)
                self.assertEqual(error.exception.status, status)
                self.assertNotIn(TOKEN, str(error.exception))

    def test_deadline_and_concurrency_are_bounded(self):
        with patch('app.time.monotonic', side_effect=[0, 10]):
            with self.assertRaises(app.EmployeePhotoError) as error:
                self.fetch()
        self.assertEqual(error.exception.status, 503)
        with patch.object(app, 'PHOTO_PROXY_SLOTS') as slots:
            slots.acquire.return_value = False
            with patch('urllib.request.build_opener') as opener:
                with self.assertRaises(app.EmployeePhotoError):
                    app.fetch_employee_photo('1001519')
                opener.assert_not_called()

    def test_rate_windows_expire_and_memory_stays_bounded(self):
        old = {'active': (190, 1), 'expired': (100, 119)}
        with patch.object(app, 'PHOTO_RATE_ATTEMPTS', old), patch('app.time.monotonic', return_value=200):
            self.assertFalse(app.photo_request_allowed({}))
            self.assertTrue(app.photo_request_allowed({'sid': 'active'}))
            self.assertEqual(app.PHOTO_RATE_ATTEMPTS, {'active': (190, 2)})
            self.assertEqual(old['active'], (190, 1), 'the existing window map is not mutated')
        full = {str(index): (200, 1) for index in range(256)}
        with patch.object(app, 'PHOTO_RATE_ATTEMPTS', full):
            with patch('app.time.monotonic', return_value=201):
                self.assertFalse(app.photo_request_allowed({'sid': 'new'}))
                self.assertTrue(app.photo_request_allowed({'sid': '0'}))
                self.assertEqual(len(app.PHOTO_RATE_ATTEMPTS), 256)
            with patch('app.time.monotonic', return_value=261):
                self.assertTrue(app.photo_request_allowed({'sid': 'new'}))
                self.assertEqual(app.PHOTO_RATE_ATTEMPTS, {'new': (261, 1)})


class EmployeePhotoHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=app.ROOT / 'data')
        cls.old_database = app.DB_PATH
        cls.old_config = app.PartyHandler.config
        cls.old_rate_attempts = app.PHOTO_RATE_ATTEMPTS
        app.DB_PATH = Path(cls.temp.name) / 'photo.db'
        app.init_db()
        with app.db_connection() as db:
            for employee_id in ('1001519', '0001600', '2412577', '0305528'):
                db.execute('INSERT INTO employees(employee_id,name,department,qr_token) VALUES (?,?,?,?)',
                           (employee_id, 'Synthetic Photo Employee', 'IT', 'photo-' + employee_id))
        cls.config = {'secret': '01' * 32}
        app.PartyHandler.config = cls.config
        cls.server = app.PartyServer(('127.0.0.1', 0), app.PartyHandler)
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.DB_PATH = cls.old_database
        app.PartyHandler.config = cls.old_config
        app.PHOTO_RATE_ATTEMPTS = cls.old_rate_attempts
        cls.temp.cleanup()

    def cookie(self, role='staff'):
        token, claims = app.sign_session(role, self.config)
        with app.db_connection() as db:
            db.execute('INSERT INTO staff_sessions VALUES (?,?,?,?,?,?,NULL)',
                       (claims['sid'], 'photo-test', 'Photo Test', role, app.now(), claims['exp']))
        return 'party_session=' + token

    def request(self, path='/api/employee-photo/1001519', cookie=None):
        request = Request(self.base + path, headers={'Cookie': cookie} if cookie else {})
        try:
            response = build_opener().open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.read(), response.headers

    def test_route_requires_session_and_allowed_role_before_fetch(self):
        with patch('app.fetch_employee_photo', return_value=JPEG) as fetch:
            self.assertEqual(self.request()[0], 401)
            self.assertEqual(self.request(cookie=self.cookie('lucky_draw_operator'))[0], 403)
            fetch.assert_not_called()
            for role in ('staff', 'checkin_staff', 'verification_staff', 'admin'):
                self.assertEqual(self.request(cookie=self.cookie(role))[0], 200)

    def test_route_rejects_queries_and_malformed_ids_without_fetch(self):
        with patch('app.fetch_employee_photo') as fetch:
            for path in ('/api/employee-photo/1001519?token=private', '/api/employee-photo/1001519?',
                         '/api/employee-photo/%31' + '001519', '/api/employee-photo/1001519/extra'):
                self.assertEqual(self.request(path, self.cookie())[0], 400)
            fetch.assert_not_called()

    def test_photo_is_private_no_store_and_does_not_change_attendance(self):
        with patch('app.fetch_employee_photo', return_value=JPEG):
            status, body, headers = self.request(cookie=self.cookie())
        self.assertEqual((status, body, headers['Content-Type']), (200, JPEG, 'image/jpeg'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Vary'], 'Cookie')
        with app.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 0)

    def test_unavailable_and_notfound_do_not_expose_upstream_or_credentials(self):
        for status in (404, 503):
            with patch('app.fetch_employee_photo', side_effect=app.EmployeePhotoError(status)):
                actual, body, _ = self.request(cookie=self.cookie())
            self.assertEqual(actual, status)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn(b'pages.dev', body)
            self.assertEqual(set(json.loads(body)), {'error'})

    def test_current_employee_outside_pilot_is_available_but_unknown_never_fetches(self):
        with patch('app.fetch_employee_photo', return_value=JPEG) as fetch:
            self.assertEqual(self.request('/api/employee-photo/2412577', self.cookie())[0], 200)
            fetch.assert_called_once_with('2412577')
            fetch.reset_mock()
            self.assertEqual(self.request('/api/employee-photo/9999999', self.cookie())[0], 404)
            fetch.assert_not_called()

    def test_current_employee_without_sql_photo_returns_notfound(self):
        with patch('app.fetch_employee_photo', side_effect=app.EmployeePhotoError(404)) as fetch:
            self.assertEqual(self.request('/api/employee-photo/0305528', self.cookie())[0], 404)
        fetch.assert_called_once_with('0305528')

    def test_per_session_rate_limit_is_bounded(self):
        cookie = self.cookie()
        with patch.object(app, 'PHOTO_RATE_LIMIT', 2), patch('app.fetch_employee_photo', return_value=JPEG):
            self.assertEqual(self.request(cookie=cookie)[0], 200)
            self.assertEqual(self.request(cookie=cookie)[0], 200)
            self.assertEqual(self.request(cookie=cookie)[0], 429)
            self.assertEqual(self.request(cookie=self.cookie())[0], 200)


if __name__ == '__main__':
    unittest.main()
