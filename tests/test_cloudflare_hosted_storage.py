"""Projector image persistence uses SQL, including large-image chunk support."""
import base64
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from cloudflare.hosted_storage import install
from cloudflare.sqlite_adapter import SQLiteConnection, TransactionState
from test_cloudflare_sqlite_adapter import FakeSQL


class HostedImageTests(unittest.TestCase):
    def setUp(self):
        self.sql = FakeSQL()
        self.addCleanup(self.sql.database.close)
        self.state = TransactionState()

        @contextmanager
        def connection():
            db = SQLiteConnection(self.sql, transaction_state=self.state)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()

        for name in ('get_projector_prize_image', 'update_projector_prize_image'):
            restore = patch.object(app, name, getattr(app, name))
            restore.start()
            self.addCleanup(restore.stop)
        replace_connection = patch.object(app, 'db_connection', connection)
        replace_connection.start()
        self.addCleanup(replace_connection.stop)
        app.init_db()
        install(app)

    def upload(self, payload=b'\x89PNG\r\n\x1a\nimage', content_type='image/png', filename='folder/prize.png'):
        return app.update_projector_prize_image({
            'data_url': 'data:' + content_type + ';base64,' + base64.b64encode(payload).decode('ascii'),
            'filename': filename,
        }, 'test-admin')

    def test_three_megabyte_image_round_trips_without_filesystem(self):
        payload = b'\x89PNG\r\n\x1a\n' + b'image' * (600 * 1024)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('No filesystem reads')), \
                patch.object(Path, 'write_bytes', side_effect=AssertionError('No filesystem writes')):
            metadata = self.upload(payload)
            loaded_metadata, loaded_payload = app.get_projector_prize_image()
        self.assertEqual(metadata, loaded_metadata)
        self.assertEqual(loaded_payload, payload)
        self.assertEqual(metadata['id'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(metadata['size'], len(payload))
        self.assertEqual(metadata['filename'], 'prize.png')
        self.assertGreater(self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_blobs').fetchone()[0], 1)

    def test_replacement_removes_previous_projector_image_row(self):
        first = self.upload()
        second = self.upload(b'\xff\xd8\xffreplacement', 'image/jpeg', 'new.jpg')
        images = self.sql.database.execute('SELECT image_id FROM _eventdesk_projector_images').fetchall()
        self.assertEqual(images, [(second['id'],)])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(app.get_projector_prize_image()[0]['filename'], 'new.jpg')

    def test_clear_removes_data_and_metadata_and_increments_sequence(self):
        self.upload()
        before = self.sql.database.execute('SELECT sequence FROM lucky_draw_presentation').fetchone()[0]
        result = app.update_projector_prize_image({'clear': True}, 'test-admin')
        self.assertIsNone(result)
        self.assertEqual(app.get_projector_prize_image(), (None, None))
        self.assertEqual(self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_projector_images').fetchone()[0], 0)
        self.assertEqual(self.sql.database.execute('SELECT sequence FROM lucky_draw_presentation').fetchone()[0], before + 1)
        actions = [row[0] for row in self.sql.database.execute("SELECT action FROM audit_logs WHERE action LIKE 'projector.prize_image_%'")]
        self.assertEqual(actions, ['projector.prize_image_updated', 'projector.prize_image_cleared'])

    def test_same_image_can_be_uploaded_twice_without_duplicate_rows(self):
        first = self.upload(filename='first.png')
        second = self.upload(filename='second.png')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_projector_images').fetchone()[0], 1)
        self.assertEqual(app.get_projector_prize_image()[0]['filename'], 'second.png')

    def test_invalid_data_does_not_replace_existing_image(self):
        metadata = self.upload()
        invalid_requests = [None, {}, {'data_url': 'not a data URL'},
                            {'data_url': 'data:image/png;base64,===='},
                            {'data_url': 'data:image/jpeg;base64,' + base64.b64encode(b'not JPEG').decode('ascii')}]
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                app.update_projector_prize_image(request, 'test-admin')
        self.assertEqual(app.get_projector_prize_image()[0], metadata)

    def test_file_type_mismatch_and_oversized_images_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'file type'):
            self.upload(b'\xff\xd8\xffimage', 'image/png')
        with self.assertRaisesRegex(ValueError, '4 MB'):
            self.upload(b'\x89PNG\r\n\x1a\n' + b'x' * app.PRIZE_IMAGE_MAX_BYTES)

    def test_corrupted_hash_or_content_type_is_not_served(self):
        metadata = self.upload()
        self.sql.database.execute('UPDATE _eventdesk_projector_images SET image=?', (b'\x89PNG\r\n\x1a\nchanged',))
        self.assertEqual(app.get_projector_prize_image(), (None, None))
        self.sql.database.execute('UPDATE _eventdesk_projector_images SET image=?', (b'\xff\xd8\xffwrong type',))
        self.assertEqual(app.get_projector_prize_image(), (None, None))

    def test_existing_connection_can_load_the_image(self):
        metadata = self.upload()
        with app.db_connection() as db:
            loaded, _payload = app.get_projector_prize_image(db)
        self.assertEqual(loaded, metadata)
        self.assertFalse(self.state.rollback_requested)

    def test_failed_update_marks_outer_rollback_and_preserves_previous_image(self):
        previous = self.upload()
        before = self.sql.database.execute('SELECT sequence FROM lucky_draw_presentation').fetchone()[0]
        self.sql.database.execute('BEGIN')
        with patch.object(app, 'audit', side_effect=RuntimeError('Audit write failed')):
            with self.assertRaisesRegex(RuntimeError, 'Audit write failed'):
                self.upload(b'\x89PNG\r\n\x1a\nreplacement')
        self.assertTrue(self.state.rollback_requested)
        self.sql.database.rollback()  # The hosted transaction owner performs this.
        self.assertEqual(app.get_projector_prize_image()[0], previous)
        self.assertEqual(self.sql.database.execute('SELECT sequence FROM lucky_draw_presentation').fetchone()[0], before)
        self.assertEqual(self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_projector_images').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
