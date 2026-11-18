"""Fixture cleanup tolerates a short Windows lock on an already-empty root."""
import os
from pathlib import Path
import stat
import time


def _empty_root_lock(folder, error):
    if getattr(error, 'winerror', None) not in (5, 32) or not error.filename:
        return False
    root = Path(folder.name)
    if os.path.normcase(os.path.abspath(error.filename)) != os.path.normcase(str(root.absolute())):
        return False
    try:
        if getattr(root.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        return root.is_dir() and next(root.iterdir(), None) is None
    except OSError:
        return False


def cleanup_test_directory(folder):
    """Retry final rmdir only; file locks, other failures and persistent locks fail."""
    for attempt in range(10):
        try:
            folder.cleanup()
            return
        except OSError as error:
            if attempt == 9 or not _empty_root_lock(folder, error):
                raise
            time.sleep(0.1)
