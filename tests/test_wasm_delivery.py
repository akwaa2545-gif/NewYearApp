"""Regression checks for removing the former WASM scanner runtime."""
import http.client
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import app


class WasmDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        root = Path(cls.temp.name)
        (root / 'vendor').mkdir()
        for name, data in {'index.html': b'<html></html>', 'app.js': b'void 0;',
                           'qr-detail-worker.js': b'void 0;',
                           'vendor/zxing-wasm-reader.js': b'void 0;',
                           'vendor/zxing_reader.wasm': b'\x00asm\x01\x00\x00\x00'}.items():
            (root / name).write_bytes(data)
        cls.frontend = patch.object(app, 'FRONTEND', root)
        cls.frontend.start()
        cls.server = app.PartyServer(('127.0.0.1', 0), app.PartyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.frontend.stop()
        cls.temp.cleanup()

    def get(self, path):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_former_wasm_assets_are_not_public_even_if_present_on_disk(self):
        for name in ['zxing-wasm-reader.js', 'zxing_reader.wasm']:
            with self.subTest(name=name):
                status, headers, _ = self.get('/static/vendor/' + name)
                self.assertEqual(status, 404)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.get('/static/vendor/private.wasm')[0], 404)

    def test_pages_and_decoder_keep_strict_script_policy(self):
        for path in ['/', '/static/app.js', '/static/qr-detail-worker.js']:
            with self.subTest(path=path):
                status, headers, _ = self.get(path)
                self.assertEqual(status, 200)
                policy = headers['Content-Security-Policy']
                self.assertNotIn("'wasm-unsafe-eval'", policy)
                self.assertNotIn("'unsafe-eval'", policy)
                self.assertIn("connect-src 'self'", policy)
