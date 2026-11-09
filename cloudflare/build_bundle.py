"""Build upload artifacts without collecting configuration or employee data.

Run with the project's virtual environment:
    python -m cloudflare.build_bundle [--output data/cloudflare-deploy]
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import mimetypes
from pathlib import Path
import shutil
import zipfile
import zlib


CHUNK_BYTES = 512 * 1024
RUNTIME_FILES = (
    '__init__.py', 'crypto_compat.py', 'handler_adapter.py', 'hosted_storage.py',
    'qr_image.py', 'sqlite_adapter.py',
)
PACKAGES = ('qrcode', 'openpyxl', 'et_xmlfile')


def _package_roots():
    roots = {}
    for package in PACKAGES:
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.origin:
            raise RuntimeError(f'Install project requirements before building: {package} is missing.')
        roots[package] = Path(spec.origin).resolve().parent
    return roots


def _safe_files(folder, pattern='*'):
    folder = folder.resolve()
    for file in sorted(folder.rglob(pattern)):
        if file.is_file():
            if not file.resolve().is_relative_to(folder):
                raise ValueError('A source file links outside its allowed folder: ' + str(file))
            yield file


def _write_vendor(root, output, package_roots):
    entries = {}
    for name in RUNTIME_FILES:
        entries['cloudflare/' + name] = root / 'cloudflare' / name
    for package in PACKAGES:
        folder = Path(package_roots[package]).resolve()
        for file in _safe_files(folder, '*.py'):
            entries[package + '/' + file.relative_to(folder).as_posix()] = file
    with zipfile.ZipFile(output / 'vendor.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, file in sorted(entries.items()):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, file.read_bytes(), compresslevel=9)


def _write_assets(root, output):
    folder = root / 'static'
    chunk_folder = output / 'assets'
    chunk_folder.mkdir(exist_ok=True)
    manifest, chunks = {}, {}
    for file in _safe_files(folder):
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        keys = []
        offsets = range(0, len(data), CHUNK_BYTES) if data else (0,)
        for index, offset in enumerate(offsets):
            key = digest + ':' + str(index)
            chunk_file = 'assets/' + digest + '-' + str(index) + '.bin'
            (output / chunk_file).write_bytes(data[offset:offset + CHUNK_BYTES])
            chunks[key] = {'key': key, 'file': chunk_file}
            keys.append(key)
        manifest[file.relative_to(folder).as_posix()] = {
            'size': len(data),
            'type': mimetypes.guess_type(file.name)[0] or 'application/octet-stream',
            'keys': keys,
        }
    return manifest, list(chunks.values())


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def build_bundle(project_root=None, output=None, package_roots=None):
    """Build three Worker modules and content-addressed public-asset chunks.

    Only explicit runtime files, pure Python dependency sources, and files from
    ``static/`` are read. Existing migration snapshots and other output files
    are preserved. Relative output paths are relative to the project root.
    """
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    destination = Path(output or root / 'data' / 'cloudflare-deploy').expanduser()
    destination = (destination if destination.is_absolute() else root / destination).resolve()
    if destination.is_relative_to((root / 'static').resolve()):
        raise ValueError('Build output must be outside the public static folder.')
    dependencies = _package_roots() if package_roots is None else package_roots
    destination.mkdir(parents=True, exist_ok=True)
    _write_vendor(root, destination, dependencies)
    shutil.copyfile(root / 'cloudflare' / 'worker.py', destination / 'worker.py')
    shutil.copyfile(root / 'app.py', destination / 'app.py')
    manifest, chunks = _write_assets(root, destination)
    result = {
        'modules': ['worker.py', 'app.py', 'vendor.zip'],
        'asset_chunks': chunks,
        'static_manifest': manifest,
    }
    _write_json(destination / 'static-manifest.json', manifest)
    compact_manifest = json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    encoded_manifest = 'zlib:' + base64.b64encode(zlib.compress(compact_manifest, level=9)).decode('ascii')
    (destination / 'static-manifest-binding.txt').write_text(encoded_manifest, encoding='ascii')
    _write_json(destination / 'module-manifest.json', {
        'main_module': 'worker.py', 'modules': result['modules'], 'asset_chunks': chunks,
    })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Output folder, relative to the project root unless absolute.')
    arguments = parser.parse_args()
    result = build_bundle(output=arguments.output)
    print(f"Built {len(result['modules'])} Worker modules, "
          f"{len(result['static_manifest'])} public files, and "
          f"{len(result['asset_chunks'])} KV chunks.")


if __name__ == '__main__':
    main()
