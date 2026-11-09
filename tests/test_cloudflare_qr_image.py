"""Verify Pillow-free PNG output against native PNG decoding and QR matrices."""
import builtins
from contextlib import contextmanager
import io
from pathlib import Path
import sqlite3
import struct
import sys
import unittest
from unittest.mock import patch
import zipfile
import zlib

import qrcode
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from cloudflare.qr_image import PurePNGImage, install
from cloudflare.sqlite_adapter import SQLiteConnection, TransactionState
from test_cloudflare_sqlite_adapter import FakeSQL


class PNGImageTests(unittest.TestCase):
    def setUp(self):
        original = qrcode.QRCode.make_image
        self.addCleanup(setattr, qrcode.QRCode, 'make_image', original)

    def make_qr(self, box_size=3, border=4):
        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data('PARTY:fixed-test-token')
        qr.make(fit=True)
        return qr

    def test_native_png_decoder_matches_every_qr_module_and_quiet_border(self):
        for box_size, border in [(1, 0), (3, 2), (8, 4)]:
            with self.subTest(box_size=box_size, border=border):
                qr = self.make_qr(box_size, border)
                encoded = qr.make_image(image_factory=PurePNGImage, fill_color='black', back_color='white')
                stream = io.BytesIO()
                encoded.save(stream, format='PNG')
                with Image.open(io.BytesIO(stream.getvalue())) as decoded:
                    decoded.load()
                    matrix = qr.get_matrix()
                    expected_size = len(matrix) * box_size
                    self.assertEqual(decoded.size, (expected_size, expected_size))
                    self.assertEqual(decoded.format, 'PNG')
                    pixels = decoded.convert('L')
                    for row, cells in enumerate(matrix):
                        for column, active in enumerate(cells):
                            expected = 0 if active else 255
                            for dy in range(box_size):
                                for dx in range(box_size):
                                    self.assertEqual(pixels.getpixel((column * box_size + dx, row * box_size + dy)), expected)

    def test_png_chunks_have_correct_crc_and_complete_image_data(self):
        qr = self.make_qr()
        stream = io.BytesIO()
        qr.make_image(image_factory=PurePNGImage).save(stream)
        png = stream.getvalue()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        position = 8
        chunks = {}
        while position < len(png):
            length = struct.unpack('>I', png[position:position + 4])[0]
            kind = png[position + 4:position + 8]
            content = png[position + 8:position + 8 + length]
            crc = struct.unpack('>I', png[position + 8 + length:position + 12 + length])[0]
            self.assertEqual(crc, zlib.crc32(kind + content) & 0xffffffff)
            chunks[kind] = content
            position += length + 12
        self.assertEqual(set(chunks), {b'IHDR', b'IDAT', b'IEND'})
        width, height, bitdepth, color, *_ = struct.unpack('>IIBBBBB', chunks[b'IHDR'])
        self.assertEqual((bitdepth, color), (1, 0))
        self.assertEqual(len(zlib.decompress(chunks[b'IDAT'])), (1 + (width + 7) // 8) * height)

    def test_install_avoids_pillow_import_and_is_idempotent(self):
        install()
        installed = qrcode.QRCode.make_image
        install()
        self.assertIs(qrcode.QRCode.make_image, installed)
        original_import = builtins.__import__
        def no_pillow(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise AssertionError('Pillow is unavailable in the hosted runtime.')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', no_pillow):
            image = self.make_qr().make_image(fill_color='black', back_color='white')
            stream = io.BytesIO()
            image.save(stream, format='PNG')
        self.assertIsInstance(image, PurePNGImage)
        self.assertTrue(stream.getvalue().startswith(b'\x89PNG'))

    def test_explicit_factory_is_preserved(self):
        class CustomImage(PurePNGImage):
            pass
        install()
        self.assertIsInstance(self.make_qr().make_image(image_factory=CustomImage), CustomImage)
        qr = qrcode.QRCode(image_factory=CustomImage)
        qr.add_data('explicit constructor factory')
        self.assertIsInstance(qr.make_image(), CustomImage)

    def test_unsupported_format_and_colors_fail_explicitly(self):
        image = self.make_qr().make_image(image_factory=PurePNGImage)
        with self.assertRaises(ValueError):
            image.save(io.BytesIO(), format='JPEG')
        with self.assertRaises(ValueError):
            self.make_qr().make_image(image_factory=PurePNGImage, fill_color='red')

    def test_original_preview_and_zip_exports_use_the_hosted_factory(self):
        install()
        sql = FakeSQL()
        self.addCleanup(sql.database.close)
        state = TransactionState()
        @contextmanager
        def connection():
            db = SQLiteConnection(sql, transaction_state=state)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()
        with patch.object(app, 'db_connection', connection):
            app.init_db()
            app.import_employees([('1', 'Test Employee', 'IT')], 'test-admin')
            preview = app.wristband_qr_png('1')
            with Image.open(io.BytesIO(preview)) as decoded:
                decoded.verify()
            with zipfile.ZipFile(io.BytesIO(app.qr_zip_bytes())) as archive:
                png_names = [name for name in archive.namelist() if name.endswith('.png')]
                self.assertEqual(len(png_names), 1)
                self.assertIn('manifest.csv', archive.namelist())
                self.assertIn('labels.html', archive.namelist())
                with Image.open(io.BytesIO(archive.read(png_names[0]))) as decoded:
                    decoded.verify()


if __name__ == '__main__':
    unittest.main()
