"""Temporary test directories retry only transient empty-root Windows locks."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import DEFAULT, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.temp_directory_cleanup import cleanup_test_directory


def locked_root(path, code=32):
    error = PermissionError('Synthetic Windows directory lock')
    error.winerror = code
    error.filename = str(path)
    return error


class TemporaryDirectoryCleanupTests(unittest.TestCase):
    def test_empty_root_lock_recovers_without_leaving_a_directory(self):
        with TemporaryDirectory() as parent:
            folder = TemporaryDirectory(dir=parent)
            path = Path(folder.name)
            original = folder.cleanup
            folder.cleanup = Mock(wraps=original, side_effect=[locked_root(path), DEFAULT])
            with patch('tests.temp_directory_cleanup.time.sleep') as pause:
                # The successful retry must call cleanup, not treat the lock as success.
                cleanup_test_directory(folder)
            self.assertFalse(path.exists())
            self.assertEqual(folder.cleanup.call_count, 2)
            pause.assert_called_once_with(0.1)

    def test_persistent_empty_root_lock_still_fails_with_bounded_wait(self):
        with TemporaryDirectory() as parent:
            folder = Mock(name='folder')
            folder.name = parent
            error = locked_root(parent, 5)
            folder.cleanup.side_effect = error
            with patch('tests.temp_directory_cleanup.time.sleep') as pause:
                with self.assertRaises(PermissionError) as raised:
                    cleanup_test_directory(folder)
            self.assertIs(raised.exception, error)
            self.assertEqual(folder.cleanup.call_count, 10)
            self.assertEqual(pause.call_count, 9)
            self.assertLessEqual(sum(call.args[0] for call in pause.call_args_list), 0.9)

    def test_file_lock_or_nonempty_directory_is_not_retried(self):
        with TemporaryDirectory() as parent:
            root = Path(parent)
            database = root / 'test.db'
            database.write_bytes(b'fixture')
            for failed_path in (database, root):
                with self.subTest(path=failed_path):
                    folder = Mock()
                    folder.name = parent
                    folder.cleanup.side_effect = locked_root(failed_path)
                    with patch('tests.temp_directory_cleanup.time.sleep') as pause:
                        with self.assertRaises(PermissionError):
                            cleanup_test_directory(folder)
                    self.assertEqual(folder.cleanup.call_count, 1)
                    pause.assert_not_called()

    def test_unrelated_error_is_not_retried(self):
        with TemporaryDirectory() as parent:
            for error in (OSError('ordinary failure'), locked_root(parent, 3), RuntimeError('bug')):
                with self.subTest(error=type(error).__name__):
                    folder = Mock()
                    folder.name = parent
                    folder.cleanup.side_effect = error
                    with patch('tests.temp_directory_cleanup.time.sleep') as pause:
                        with self.assertRaises(type(error)):
                            cleanup_test_directory(folder)
                    self.assertEqual(folder.cleanup.call_count, 1)
                    pause.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows directory sharing regression')
    def test_real_windows_directory_lock_released_during_retry(self):
        with TemporaryDirectory() as parent:
            folder = TemporaryDirectory(dir=parent)
            path = Path(folder.name)
            child = subprocess.Popen([sys.executable, '-I', '-S', '-c',
                "import sys;print('ready',flush=True);sys.stdin.buffer.read(1)"],
                cwd=path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(child.stdout.readline(), b'ready\r\n')
            released = threading.Event()
            def release():
                time.sleep(0.15)
                child.communicate(b'x', timeout=3)
                released.set()
            thread = threading.Thread(target=release)
            thread.start()
            try:
                cleanup_test_directory(folder)
                thread.join(timeout=2)
                self.assertFalse(path.exists())
                self.assertTrue(released.is_set())
            finally:
                thread.join(timeout=2)
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)
                folder.cleanup()


if __name__ == '__main__':
    unittest.main()
