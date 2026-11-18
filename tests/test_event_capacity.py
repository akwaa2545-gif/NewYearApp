"""The native loopback backend admits phone bursts while keeping a hard bound."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import secrets
import socket
import threading
import unittest

from photo_api.event_backend import EventHTTPServer


class _BlockedEngine:
    def __init__(self):
        self.condition = threading.Condition()
        self.release = threading.Event()
        self.entered = 0

    def handle(self, _payload):
        with self.condition:
            self.entered += 1
            self.condition.notify_all()
        if not self.release.wait(15):
            raise AssertionError('Capacity fixture timed out.')
        return {'ok': True}

    def wait_for(self, count):
        with self.condition:
            return self.condition.wait_for(lambda: self.entered >= count, timeout=4)


class EventCapacityTests(unittest.TestCase):
    def setUp(self):
        self.engine = _BlockedEngine()
        self.token = secrets.token_urlsafe(36)
        self.server = EventHTTPServer(('127.0.0.1', 0), self.engine, self.token)
        self.serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serving.start()
        self.pool = ThreadPoolExecutor(max_workers=32)

    def tearDown(self):
        self.engine.release.set()
        self.pool.shutdown(wait=True)
        self.server.shutdown()
        self.server.server_close()
        self.serving.join(timeout=5)

    def request(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST', '/v1/event/request', body=b'{}',
                headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None
        finally:
            connection.close()

    def test_ten_simultaneous_phone_requests_are_admitted(self):
        pending = [self.pool.submit(self.request) for _ in range(10)]
        self.assertTrue(self.engine.wait_for(10), 'Ten phones must reach the backend before release.')
        self.engine.release.set()
        self.assertEqual([request.result() for request in pending], [(200, {'ok': True})] * 10)

    def test_thirty_third_request_is_rejected_and_slots_recover(self):
        pending = [self.pool.submit(self.request) for _ in range(32)]
        self.assertTrue(self.engine.wait_for(32), 'The bounded pool must admit 32 blocked requests.')
        # Saturation rejects a connection before reading HTTP. Do not race a
        # separate client body write against that deliberate close.
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=10) as connection:
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
                self.assertEqual((response.status, response.read()), (503, b''))
            finally:
                response.close()
        self.assertEqual(self.engine.entered, 32)
        self.engine.release.set()
        self.assertTrue(all(request.result() == (200, {'ok': True}) for request in pending))
        self.assertEqual(self.request(), (200, {'ok': True}))

    def test_accept_backlog_covers_the_bounded_worker_burst(self):
        self.assertEqual(self.server.request_queue_size, 32)


if __name__ == '__main__':
    unittest.main()
