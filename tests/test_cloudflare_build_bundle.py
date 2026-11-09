"""Verify deployment artifacts contain only runtime code and public assets."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloudflare.build_bundle import CHUNK_BYTES, build_bundle


class BuildBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project'
        self.output = self.root / 'data' / 'cloudflare-deploy'
        (self.root / 'cloudflare').mkdir(parents=True)
        (self.root / 'static' / 'assets').mkdir(parents=True)
        (self.root / 'data').mkdir()
        (self.root / 'app.py').write_text('RUNTIME = True\n', encoding='utf-8')
        (self.root / 'cloudflare' / 'worker.py').write_text('ENTRYPOINT = True\n', encoding='utf-8')
        for name in ('__init__.py', 'crypto_compat.py', 'handler_adapter.py',
                     'hosted_storage.py', 'qr_image.py', 'sqlite_adapter.py'):
            (self.root / 'cloudflare' / name).write_text('RUNTIME_ADAPTER = True\n', encoding='utf-8')
        (self.root / 'static' / 'index.html').write_text('<h1>Public app</h1>', encoding='utf-8')
        self.picture = bytes(range(256)) * ((CHUNK_BYTES // 256) + 2)
        (self.root / 'static' / 'assets' / 'photo.png').write_bytes(self.picture)
        self.packages = {}
        for name in ('qrcode', 'openpyxl', 'et_xmlfile'):
            folder = Path(temporary.name) / 'dependencies' / name
            folder.mkdir(parents=True)
            (folder / '__init__.py').write_text('PACKAGE = True\n', encoding='utf-8')
            (folder / 'native.pyd').write_bytes(b'not-python')
            (folder / '__pycache__').mkdir()
            (folder / '__pycache__' / '__init__.pyc').write_bytes(b'bytecode')
            self.packages[name] = folder

    def build(self):
        return build_bundle(self.root, self.output, self.packages)

    def test_modules_and_vendor_exclude_private_state_and_native_libraries(self):
        private_marker = 'private-file-content-marker'
        (self.root / 'data' / 'config.json').write_text(private_marker)
        (self.root / 'data' / 'employees.json').write_text(private_marker)
        (self.root / 'data' / 'local-credentials.txt').write_text(private_marker)
        (self.root / 'cloudflare' / 'credentials.py').write_text(private_marker)
        result = self.build()
        self.assertEqual(set(result['modules']), {'worker.py', 'app.py', 'vendor.zip'})
        self.assertEqual((self.output / 'worker.py').read_text(), 'ENTRYPOINT = True\n')
        self.assertEqual((self.output / 'app.py').read_text(), 'RUNTIME = True\n')
        with zipfile.ZipFile(self.output / 'vendor.zip') as archive:
            names = set(archive.namelist())
            self.assertIn('cloudflare/crypto_compat.py', names)
            self.assertIn('qrcode/__init__.py', names)
            self.assertIn('openpyxl/__init__.py', names)
            self.assertIn('et_xmlfile/__init__.py', names)
            self.assertTrue(all(name.endswith('.py') for name in names))
            self.assertNotIn('cloudflare/worker.py', names)
            self.assertNotIn('cloudflare/credentials.py', names)
            self.assertFalse(any(private_marker.encode() in archive.read(name) for name in names))

    def test_asset_chunks_reconstruct_public_binary_and_use_cloudflare_keys(self):
        result = self.build()
        manifest = json.loads((self.output / 'static-manifest.json').read_text())
        image = manifest['assets/photo.png']
        digest = hashlib.sha256(self.picture).hexdigest()
        self.assertEqual(image['keys'], [digest + ':0', digest + ':1'])
        self.assertEqual(image['size'], len(self.picture))
        self.assertEqual(image['type'], 'image/png')
        chunks = {item['key']: self.output / item['file'] for item in result['asset_chunks']}
        self.assertEqual(b''.join(chunks[key].read_bytes() for key in image['keys']), self.picture)
        self.assertTrue(all(':' not in path.name for path in chunks.values()))
        self.assertTrue(all(path.stat().st_size <= CHUNK_BYTES for path in chunks.values()))
        self.assertEqual(set(manifest), {'index.html', 'assets/photo.png'})

    def test_existing_migration_artifacts_are_preserved_and_build_is_repeatable(self):
        self.output.mkdir(parents=True)
        snapshot = self.output / 'migration-snapshot.sqlite3'
        snapshot.write_bytes(b'keep-private-snapshot')
        self.build()
        vendor = (self.output / 'vendor.zip').read_bytes()
        manifest = (self.output / 'static-manifest.json').read_bytes()
        self.build()
        self.assertEqual(snapshot.read_bytes(), b'keep-private-snapshot')
        self.assertEqual((self.output / 'vendor.zip').read_bytes(), vendor)
        self.assertEqual((self.output / 'static-manifest.json').read_bytes(), manifest)

    def test_output_cannot_be_written_into_public_static_files(self):
        with self.assertRaisesRegex(ValueError, 'static'):
            build_bundle(self.root, self.root / 'static' / 'deployment', self.packages)


if __name__ == '__main__':
    unittest.main()
