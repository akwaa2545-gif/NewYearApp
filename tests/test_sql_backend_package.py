"""Deployment packages contain runtime code, never workstation private data."""
import hashlib
import base64
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_sql_backend_package as package


class BackendPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.deps = self.root / 'dependencies'
        self.binaries = self.root / 'compiled'
        for directory in (self.source, self.deps, self.binaries):
            directory.mkdir()
        for name in package.SOURCE_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'' if name.endswith('__init__.py') else b'# runtime\n')
        for name in package.BINARY_FILES:
            (self.binaries / name).write_bytes(b'MZ\0compiled')
        for name, content in {
            'sqlglot/__init__.py': b'# dependency\n',
            'sqlglot-30.32.0.dist-info/METADATA': b'Name: sqlglot\n',
            'sqlglot-30.32.0.dist-info/licenses/LICENSE': b'license\n',
        }.items():
            path = self.deps / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for name in package.DEPENDENCY_NAMES:
            metadata = self.deps / (name + '-1.0.dist-info')
            if name == 'sqlglot':
                metadata = self.deps / 'sqlglot-30.32.0.dist-info'
            metadata.mkdir(exist_ok=True)
            (metadata / 'METADATA').write_text('Name: ' + name + '\n', encoding='utf-8')
            (metadata / 'REQUESTED').write_bytes(b'')
        for name in package._MODULE_NAMES - {'pillow.libs'}:
            directory = self.deps / name
            directory.mkdir(exist_ok=True)
            (directory / '__init__.py').write_bytes(b'')
        for name in ('_cffi_backend.cp314-win_amd64.pyd', 'pyodbc.cp314-win_amd64.pyd',
                     'pyodbc.pyi', 'msvcp140-' + 'a' * 32 + '.dll'):
            (self.deps / name).write_bytes(b'MZ dependency')
        self.records()
        self.output = self.root / 'release'

    def records(self):
        metadata_dirs = sorted(self.deps.glob('*.dist-info'))
        inventory = {directory.name + '/RECORD': directory for directory in metadata_dirs}
        rows = {directory.name: [] for directory in metadata_dirs}
        for path in sorted(self.deps.rglob('*')):
            if not path.is_file() or path.name == 'RECORD':
                continue
            relative = path.relative_to(self.deps).as_posix()
            owner = next((directory for directory in metadata_dirs
                          if relative.startswith(directory.name + '/')), None)
            if owner is None:
                owner = next(directory for directory in metadata_dirs
                             if directory.name.startswith('pyodbc-'))
            raw = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
            rows[owner.name].append((relative, 'sha256=' + digest, str(len(raw))))
        for relative, directory in inventory.items():
            with (directory / 'RECORD').open('w', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows([*rows[directory.name], (relative, '', '')])

    def build(self):
        return package.build_package(self.source, self.deps, self.binaries, self.output)

    def test_manifest_is_exact_reproducible_and_hash_pinned(self):
        first = self.build()
        manifest_bytes = (self.output / 'package_manifest.json').read_bytes()
        manifest = json.loads(manifest_bytes)
        actual = {path.relative_to(self.output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.output.rglob('*') if path.is_file()
                  and path.name != 'package_manifest.json'}
        self.assertEqual(manifest, {'schema_version': 1, 'files': actual})
        self.assertEqual(first['manifest_sha256'], hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(first['files'], len(actual))
        second = package.build_package(self.source, self.deps, self.binaries, self.root / 'release-2')
        self.assertEqual(first, second)

    def test_private_workspace_data_and_extra_binaries_are_excluded(self):
        for name in ('data/config.json', 'data/employees.json', '.env', '.ssh/id_ed25519',
                     'Photo/1000001.jpg', 'Emp.csv', 'secret.otf', 'tests/private.py'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('PRIVATE_SENTINEL', encoding='utf-8')
        (self.binaries / 'ImportPhotos.exe').write_bytes(b'MZnot-the-service')
        self.build()
        payloads = [path.read_bytes() for path in self.output.rglob('*') if path.is_file()]
        self.assertTrue(all(b'PRIVATE_SENTINEL' not in item for item in payloads))
        self.assertEqual(sorted(path.name for path in (self.output / 'bin').iterdir()),
                         sorted(package.BINARY_FILES))

    def test_empty_python_package_initializers_are_preserved(self):
        self.build()
        self.assertEqual((self.output / 'src/cloudflare/__init__.py').read_bytes(), b'')
        self.assertEqual((self.output / 'packages/sqlglot-30.32.0.dist-info/REQUESTED').read_bytes(), b'')

    def test_existing_output_is_never_changed(self):
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_missing_runtime_source_fails_before_output_creation(self):
        (self.source / 'app.py').unlink()
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_dependency_startup_hooks_and_unknown_roots_are_rejected(self):
        for name in ('anything.pth', 'sitecustomize.py', 'usercustomize.py',
                     'pip/__init__.py', 'credentials.json', 'sqlglot/hidden.pth'):
            with self.subTest(name=name):
                path = self.deps / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'import private_bootstrap\n')
                with self.assertRaises(package.PackageError):
                    self.build()
                self.assertFalse(self.output.exists())
                path.unlink()
                if path.parent.name == 'pip':
                    path.parent.rmdir()

    def test_cache_and_generated_executable_wrappers_are_not_packaged(self):
        for name in ('sqlglot/__pycache__/ignored.pyc', 'bin/qr.exe', 'Scripts/qr.exe'):
            path = self.deps / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'ignored')
        self.build()
        self.assertFalse(any(path.suffix == '.pyc' for path in self.output.rglob('*')))
        self.assertFalse((self.output / 'packages/bin').exists())

    def test_manifest_paths_follow_windows_installer_rules(self):
        for value in ('../outside', 'src/../outside.py', 'src\\a.py', '/src/a.py',
                      'src/a.py:stream', 'src/CON.py', 'packages/x./y.py',
                      'packages/has space.txt', 'src/.env', 'src/a.py/'):
            with self.subTest(value=value), self.assertRaises(package.PackageError):
                package.validate_relative_path(value)

    def test_source_symlink_is_rejected_without_following_it(self):
        original = package._is_reparse
        with patch.object(package, '_is_reparse', side_effect=lambda path:
                          path == self.source / 'app.py' or original(path)):
            with self.assertRaises(package.PackageError):
                self.build()
        self.assertFalse(self.output.exists())

    def test_dependency_directory_reparse_is_rejected(self):
        original = package._is_reparse
        with patch.object(package, '_is_reparse', side_effect=lambda path:
                          path == self.deps / 'sqlglot' or original(path)):
            with self.assertRaises(package.PackageError):
                self.build()
        self.assertFalse(self.output.exists())

    def test_file_size_and_total_size_bounds_precede_copy(self):
        with patch.object(package, 'MAX_FILE_BYTES', 2):
            with self.assertRaises(package.PackageError):
                self.build()
        self.assertFalse(self.output.exists())
        with patch.object(package, 'MAX_TOTAL_BYTES', 2):
            with self.assertRaises(package.PackageError):
                self.build()
        self.assertFalse(self.output.exists())

    def test_non_pe_service_binary_is_rejected(self):
        (self.binaries / package.BINARY_FILES[0]).write_bytes(b'wrong')
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_missing_native_or_distribution_fails_before_output_creation(self):
        (self.deps / 'pyodbc.cp314-win_amd64.pyd').unlink()
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_modified_dependency_is_rejected_against_wheel_record(self):
        (self.deps / ('msvcp140-' + 'a' * 32 + '.dll')).write_bytes(b'MZ changed')
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_extra_private_file_under_valid_dependency_is_not_copied(self):
        (self.deps / 'sqlglot/credentials.json').write_bytes(b'PRIVATE_SENTINEL')
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_missing_distribution_and_malformed_record_fail_closed(self):
        record = self.deps / 'pyodbc-1.0.dist-info/RECORD'
        original = record.read_bytes()
        for raw in (b'../outside.py,sha256=' + b'a' * 43 + b',1\n',
                    b'pyodbc.pyi,md5=aaa,1\n', b'pyodbc.pyi,sha256=' + b'a' * 43 + b',-1\n',
                    b'pyodbc.pyi,\n'):
            with self.subTest(record=raw):
                record.write_bytes(raw)
                with self.assertRaises(package.PackageError):
                    self.build()
                self.assertFalse(self.output.exists())
        record.write_bytes(original)
        record.unlink()
        with self.assertRaises(package.PackageError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_source_changed_during_copy_leaves_no_valid_manifest(self):
        original = package._copy_file
        changed = False
        def replace(source, target, digest, size):
            nonlocal changed
            if not changed:
                source.write_bytes(source.read_bytes() + b'# changed')
                changed = True
            return original(source, target, digest, size)
        with patch.object(package, '_copy_file', side_effect=replace):
            with self.assertRaises(package.PackageError):
                self.build()
        self.assertFalse((self.output / 'package_manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
