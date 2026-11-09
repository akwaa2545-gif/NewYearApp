"""Private photo batch preparation, using only synthetic portraits."""
import hashlib
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, MpoImagePlugin


MODULE_PATH = Path(__file__).resolve().parents[1] / 'photo_api' / 'prepare_photos.py'


def load_module():
    spec = importlib.util.spec_from_file_location('prepare_photos', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreparePhotosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photos = self.root / 'originals'
        self.photos.mkdir()
        self.employees = self.root / 'employees.json'
        self.employees.write_text(json.dumps({'employees': [
            {'employee_id': '0000001', 'name': 'PRIVATE NAME'},
            {'employee_id': '0000002'}, {'employee_id': '0000003'},
            {'employee_id': '0000004'},
        ]}), encoding='utf-8')
        self.output = self.root / 'batch'

    def picture(self, relative, color='red', mode='RGB', size=(600, 400), **kwargs):
        path = self.photos / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, size, color).save(path, **kwargs)
        return path

    def run_prepare(self, **kwargs):
        module = load_module()
        module.prepare_photos(self.photos, self.employees, self.output, **kwargs)
        return (json.loads((self.output / 'manifest.json').read_text()),
                json.loads((self.output / 'report.json').read_text()))

    def test_prepares_small_jpeg_and_preserves_originals(self):
        source = self.picture('nested/1.png')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest, report = self.run_prepare()
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['employee_source_count'], 4)
        entry, = manifest['entries']
        self.assertEqual(entry['employee_id'], '0000001')
        prepared = self.output / entry['file']
        self.assertEqual(entry['sha256'], hashlib.sha256(prepared.read_bytes()).hexdigest())
        self.assertEqual(entry['bytes'], prepared.stat().st_size)
        self.assertLessEqual(entry['bytes'], 256 * 1024)
        self.assertEqual(entry['content_type'], 'image/jpeg')
        with Image.open(prepared) as image:
            self.assertEqual(image.size, (256, 171))
            self.assertEqual(image.mode, 'RGB')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertEqual(report['counts']['prepared'], 1)
        self.assertEqual(report['missing_employee_ids'], ['0000002', '0000003', '0000004'])
        self.assertNotIn('PRIVATE NAME', (self.output / 'report.json').read_text())

    def test_conflicts_quarantine_all_identical_duplicates_deduplicate(self):
        self.picture('a/1.jpg', 'red')
        self.picture('b/0000001.jpg', 'blue')
        second = self.picture('a/2.png')
        duplicate = self.photos / 'b/0000002.png'
        duplicate.write_bytes(second.read_bytes())
        self.picture('9999999.bmp')
        (self.photos / 'Thumbs.db').write_bytes(b'ignored')
        self.picture('unmatched_name.jpg')
        manifest, report = self.run_prepare()
        self.assertEqual([e['employee_id'] for e in manifest['entries']], ['0000002'])
        self.assertEqual(report['conflicts'][0]['employee_id'], '0000001')
        self.assertEqual(len(report['conflicts'][0]['paths']), 2)
        self.assertEqual(report['unknown'][0]['employee_id'], '9999999')
        self.assertEqual(report['counts']['identical_duplicates'], 1)
        self.assertEqual(report['counts']['ignored_files'], 2)
        self.assertEqual(len(list((self.output / 'photos').glob('*.jpg'))), 1)

    def test_explicit_main_folder_preference_selects_only_unambiguous_main_sources(self):
        selected_one = self.picture('main/1.jpg', 'red')
        self.picture('main/JPEG/1.jpg', 'blue')
        selected_two = self.picture('second/2.jpg', 'green')
        self.picture('second/jPeG/2.jpg', 'yellow')
        self.picture('a/3.jpg', 'red')
        self.picture('b/3.jpg', 'blue')
        self.picture('a/JPEG/3.jpg', 'green')
        self.picture('JPEG/4.jpg', 'red')
        self.picture('other/JPEG/4.jpg', 'blue')
        originals = {path: path.read_bytes() for path in self.photos.rglob('*.jpg')}
        module = load_module()
        manifest, report = module.prepare_photos(self.photos, self.employees, self.output,
                                                  prefer_main_folder=True)
        self.assertEqual([entry['employee_id'] for entry in manifest['entries']], ['0000001', '0000002'])
        self.assertEqual([group['employee_id'] for group in report['conflicts']], ['0000003', '0000004'])
        self.assertEqual(report['counts']['preferred_main'], 2)
        self.assertEqual(report['preferred_main_photos'], [
            {'employee_id': '0000001', 'selected_path': 'main/1.jpg',
             'source_sha256': hashlib.sha256(selected_one.read_bytes()).hexdigest(),
             'excluded_paths': ['main/JPEG/1.jpg']},
            {'employee_id': '0000002', 'selected_path': 'second/2.jpg',
             'source_sha256': hashlib.sha256(selected_two.read_bytes()).hexdigest(),
             'excluded_paths': ['second/jPeG/2.jpg']},
        ])
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_main_preference_accepts_identical_main_copies_and_cli_is_explicit(self):
        original = self.picture('a/1.jpg', 'red')
        duplicate = self.photos / 'b/1.jpg'
        duplicate.parent.mkdir()
        duplicate.write_bytes(original.read_bytes())
        self.picture('a/JPEG/1.jpg', 'blue')
        command = [sys.executable, str(MODULE_PATH), '--photos', str(self.photos),
                   '--employees', str(self.employees), '--output', str(self.output), '--prefer-main-folder']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report['counts']['prepared'], 1)
        self.assertEqual(report['counts']['identical_duplicates'], 1)
        self.assertEqual(report['preferred_main_photos'][0]['selected_path'], 'a/1.jpg')

    def test_main_preference_does_not_fall_back_when_main_source_is_unreadable(self):
        main = self.picture('main/1.jpg', 'red')
        self.picture('main/JPEG/1.jpg', 'blue')
        module = load_module()
        original_hash = module._file_hash

        def read_hash(path):
            if path == main:
                raise PermissionError('synthetic source failure')
            return original_hash(path)

        with mock.patch.object(module, '_file_hash', side_effect=read_hash):
            manifest, report = module.prepare_photos(self.photos, self.employees, self.output,
                                                      prefer_main_folder=True)
        self.assertEqual(manifest['entries'], [])
        self.assertEqual(report['counts']['conflicts'], 1)
        self.assertEqual(report['preferred_main_photos'], [])
        self.assertEqual(report['errors'][0]['reason'], 'source_unreadable')

    def test_orientation_is_applied_and_metadata_removed(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = 'PRIVATE NAME'
        self.picture('1.jpg', size=(40, 80), exif=exif)
        manifest, _ = self.run_prepare()
        with Image.open(self.output / manifest['entries'][0]['file']) as image:
            self.assertEqual(image.size, (80, 40))
            self.assertEqual(len(image.getexif()), 0)

    def mpo_picture(self, relative, primary_color='red', primary_size=(40, 80)):
        path = self.photos / relative
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = 'PRIVATE MPO NAME'
        with Image.new('RGB', primary_size, primary_color) as primary, \
                Image.new('RGB', (400, 400), 'blue') as alternate:
            primary.save(path, format='MPO', save_all=True, append_images=[alternate], exif=exif)
        with Image.open(path) as image:
            self.assertEqual(image.format, 'MPO', 'fixture must be a real multi-picture container')
            self.assertEqual(image.n_frames, 2)
        return path

    def test_mpo_uses_only_primary_frame_with_orientation_and_no_metadata(self):
        source = self.mpo_picture('1.jpg')
        original = source.read_bytes()
        original_seek = MpoImagePlugin.MpoImageFile.seek

        def only_primary(image, frame):
            self.assertEqual(frame, 0, 'preparation must never seek an alternate MPO frame')
            return original_seek(image, frame)

        module = load_module()
        module.MAX_PIXELS = 5000  # Alternate frame exceeds this; only primary is decoded.
        with mock.patch.object(MpoImagePlugin.MpoImageFile, 'seek', autospec=True, side_effect=only_primary):
            manifest, report = module.prepare_photos(self.photos, self.employees, self.output)
        self.assertEqual(report['counts']['prepared'], 1)
        self.assertEqual(report['errors'], [])
        with Image.open(self.output / manifest['entries'][0]['file']) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (80, 40))
            self.assertEqual(getattr(image, 'n_frames', 1), 1)
            self.assertEqual(len(image.getexif()), 0)
            self.assertNotIn('mp', image.info)
            red, green, blue = image.getpixel((20, 20))
            self.assertGreater(red, 230)
            self.assertLess(green, 30)
            self.assertLess(blue, 30)
        self.assertEqual(source.read_bytes(), original)

    def test_mpo_extension_is_supported_and_existing_limits_still_apply(self):
        source = self.mpo_picture('1.mpo')
        module = load_module()
        manifest, report = module.prepare_photos(self.photos, self.employees, self.output)
        self.assertEqual(report['counts']['prepared'], 1)
        self.assertEqual(manifest['entries'][0]['employee_id'], '0000001')
        module.MAX_SOURCE_BYTES = source.stat().st_size - 1
        _, report = module.prepare_photos(self.photos, self.employees, self.root / 'source-bounded')
        self.assertEqual(report['errors'][0]['reason'], 'source_too_large')
        module.MAX_SOURCE_BYTES = 32 * 1024 * 1024
        module.MAX_PIXELS = 1000
        _, report = module.prepare_photos(self.photos, self.employees, self.root / 'pixel-bounded')
        self.assertEqual(report['errors'][0]['reason'], 'pixel_limit_exceeded')
        module.MAX_PIXELS = 20_000_000
        module.MAX_OUTPUT_BYTES = 10
        _, report = module.prepare_photos(self.photos, self.employees, self.root / 'output-bounded')
        self.assertEqual(report['errors'][0]['reason'], 'output_too_large')

    def test_transparency_is_composited_on_white(self):
        self.picture('1.png', color=(255, 0, 0, 0), mode='RGBA', size=(30, 30))
        manifest, _ = self.run_prepare()
        with Image.open(self.output / manifest['entries'][0]['file']) as image:
            self.assertTrue(all(value >= 250 for value in image.getpixel((0, 0))))

    def test_grayscale_alpha_is_composited_on_white(self):
        self.picture('1.png', color=(0, 0), mode='LA', size=(30, 30))
        manifest, _ = self.run_prepare()
        with Image.open(self.output / manifest['entries'][0]['file']) as image:
            self.assertTrue(all(value >= 250 for value in image.getpixel((0, 0))))

    def test_corrupt_excluded_inputs_are_also_reported(self):
        (self.photos / '9999999.jpg').write_bytes(b'corrupt unknown')
        (self.photos / '1.jpg').write_bytes(b'corrupt conflicting')
        self.picture('1.png')
        manifest, report = self.run_prepare()
        self.assertEqual(manifest['entries'], [])
        self.assertEqual(len(report['errors']), 2)
        self.assertEqual(len(report['conflicts']), 1)
        self.assertEqual(len(report['unknown']), 1)

    def test_corrupt_unsupported_and_pixel_limit_are_reported(self):
        (self.photos / '1.jpg').write_bytes(b'not a jpeg')
        self.picture('2.png', size=(60, 60))
        self.picture('3.jpg', format='GIF')
        module = load_module()
        module.MAX_PIXELS = 1000
        module.prepare_photos(self.photos, self.employees, self.output)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(manifest['entries'], [])
        self.assertEqual(len(report['errors']), 3)
        self.assertEqual(report['counts']['failed'], 3)

    def test_rejects_output_inside_source_or_nonempty(self):
        module = load_module()
        with self.assertRaises(ValueError):
            module.prepare_photos(self.photos, self.employees, self.photos / 'batch')
        self.output.mkdir()
        original = self.output / 'keep.txt'
        original.write_text('keep')
        with self.assertRaises(ValueError):
            module.prepare_photos(self.photos, self.employees, self.output)
        self.assertEqual(original.read_text(), 'keep')
        with self.assertRaises(ValueError):
            module.prepare_photos(self.photos, self.employees, self.root)

    def test_rejects_output_traversal_without_creating_directories(self):
        module = load_module()
        with self.assertRaises(ValueError):
            module.prepare_photos(self.photos, self.employees, self.root / 'unused' / '..' / 'batch')
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / 'unused').exists())

    def test_source_and_output_byte_bounds_are_enforced(self):
        self.picture('1.jpg')
        module = load_module()
        module.MAX_SOURCE_BYTES = 10
        _, report = module.prepare_photos(self.photos, self.employees, self.output)
        self.assertEqual(report['errors'][0]['reason'], 'source_too_large')
        module.MAX_SOURCE_BYTES = 32 * 1024 * 1024
        module.MAX_OUTPUT_BYTES = 10
        _, report = module.prepare_photos(self.photos, self.employees, self.root / 'batch2')
        self.assertEqual(report['errors'][0]['reason'], 'output_too_large')

    def test_rejects_symlinked_output_and_ignores_symlink_sources(self):
        target = self.root / 'target'
        target.mkdir()
        try:
            self.output.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks requires OS permission')
        module = load_module()
        with self.assertRaises(ValueError):
            module.prepare_photos(self.photos, self.employees, self.output)
        self.output.unlink()
        outside = self.root / '1.jpg'
        Image.new('RGB', (20, 20)).save(outside)
        (self.photos / '1.jpg').symlink_to(outside)
        manifest, report = self.run_prepare()
        self.assertEqual(manifest['entries'], [])
        self.assertEqual(report['counts']['ignored_files'], 1)

    def test_sqlite_current_master_is_read_only(self):
        database = self.root / 'master.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE employees (employee_id TEXT)')
            connection.execute("INSERT INTO employees VALUES ('0000001')")
            connection.commit()
        before = database.read_bytes()
        self.picture('1.jpg')
        module = load_module()
        module.prepare_photos(self.photos, None, self.output, database=database)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(manifest['employee_source_count'], 1)
        self.assertEqual(database.read_bytes(), before)

    def test_cli_runs_full_workflow_and_rejects_rerun(self):
        self.picture('1.bmp')
        command = [sys.executable, str(MODULE_PATH), '--photos', str(self.photos),
                   '--employees', str(self.employees), '--output', str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.output / 'manifest.json').exists())
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)

    def test_output_permission_error_aborts_cli_without_successful_manifest(self):
        self.picture('1.jpg')
        module = load_module()
        original_open = Path.open

        def reject_output(path, *args, **kwargs):
            if self.output in path.parents and path.parent.name == 'photos':
                raise PermissionError('destination is read-only')
            return original_open(path, *args, **kwargs)

        stdout, stderr = StringIO(), StringIO()
        with mock.patch.object(Path, 'open', autospec=True, side_effect=reject_output):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = module.main(['--photos', str(self.photos), '--employees',
                                      str(self.employees), '--output', str(self.output)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('Preparation failed:', stderr.getvalue())
        self.assertFalse((self.output / 'manifest.json').exists())

    def test_partial_destination_write_leaves_no_published_jpeg_or_manifest(self):
        self.picture('1.jpg')
        module = load_module()
        original_open = Path.open

        class BrokenDestination:
            def __init__(self, path, *args, **kwargs):
                self.stream = original_open(path, *args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, content):
                self.stream.write(content[:10])
                raise OSError('destination disk full')

        def fail_output_write(path, *args, **kwargs):
            if self.output in path.parents and path.parent.name == 'photos':
                return BrokenDestination(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'open', autospec=True, side_effect=fail_output_write):
            with self.assertRaises(OSError):
                module.prepare_photos(self.photos, self.employees, self.output)
        self.assertFalse((self.output / 'manifest.json').exists())
        self.assertEqual(list((self.output / 'photos').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
