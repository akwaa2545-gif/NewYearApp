"""Build a hash-pinned Windows SQL backend package from explicit runtime inputs.

Install production dependencies into a fresh directory with pip --target first.
This tool never installs packages, reads configuration, connects to SQL, or touches
services. Its output matches photo_api/install_event_backend.ps1. A failed copy
leaves an incomplete directory without a manifest for inspection; it is never
silently overwritten or recursively removed.
"""
from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


SOURCE_FILES = (
    'app.py', 'event_backend_entry.py',
    'cloudflare/__init__.py', 'cloudflare/handler_adapter.py',
    'cloudflare/hosted_storage.py',
    'eventdesk_storage/__init__.py', 'eventdesk_storage/compiler.py',
    'eventdesk_storage/connection.py', 'eventdesk_storage/schema.py',
    'eventdesk_storage/table_catalog.json',
    'photo_api/event_backend.py', 'photo_api/event_protocol.py',
    'photo_api/event_store.py', 'scripts/migrate_sql_event.py',
)
BINARY_FILES = ('EventDeskPhotoApi.exe', 'EventBackendService.exe')
DEPENDENCY_NAMES = frozenset((
    'cffi', 'colorama', 'cryptography', 'et_xmlfile', 'openpyxl', 'pillow',
    'pycparser', 'pyodbc', 'qrcode', 'sqlglot',
))
_MODULE_NAMES = (DEPENDENCY_NAMES - {'pillow', 'pyodbc'}) | {'PIL', 'pillow.libs'}
_DIST_INFO = re.compile(r'([A-Za-z0-9_]+)-[0-9][A-Za-z0-9_.+-]*\.dist-info')
_EXTENSION = re.compile(r'(?:_cffi_backend|pyodbc)\.cp314-win_amd64\.pyd')
_BUNDLED_DLL = re.compile(r'msvcp140-[0-9a-f]{32}\.dll')
_SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
_DEVICE = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 12000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class PackageError(ValueError):
    """Inputs cannot form a bounded installer-compatible release."""


@dataclass(frozen=True)
class _Payload:
    source: Path
    relative: str
    size: int
    sha256: str


def validate_relative_path(value: str) -> str:
    """Apply the installer's strict Windows path contract on every platform."""
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise PackageError('Invalid package path.')
    parts = value.split('/')
    if len(parts) < 2 or parts[0] not in ('src', 'packages', 'bin'):
        raise PackageError('Invalid package root.')
    if any(not _SEGMENT.fullmatch(part) or part.endswith('.') or _DEVICE.match(part)
           for part in parts):
        raise PackageError('Unsafe package path segment.')
    if parts[0] == 'bin' and value not in tuple('bin/' + name for name in BINARY_FILES):
        raise PackageError('Unexpected service binary.')
    if parts[0] == 'src' and not value.endswith(('.py', '.json')):
        raise PackageError('Unexpected runtime source type.')
    return value


def _is_reparse(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                                          & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))


def _check_path(path):
    for ancestor in (path, *path.parents):
        try:
            if _is_reparse(ancestor):
                raise PackageError('Reparse points and symbolic links are forbidden.')
        except FileNotFoundError as error:
            raise PackageError('A required input or parent directory is missing.') from error


def _directory(value):
    path = Path(os.path.abspath(value))
    _check_path(path)
    if not path.is_dir():
        raise PackageError('An input directory is required.')
    return path


def _hash_file(path):
    _check_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise PackageError('Input is not a bounded regular file.')
    digest, length = hashlib.sha256(), 0
    with path.open('rb') as source:
        while chunk := source.read(_CHUNK_BYTES):
            length += len(chunk)
            if length > MAX_FILE_BYTES:
                raise PackageError('Input file grew beyond its bound.')
            digest.update(chunk)
    if length != info.st_size:
        raise PackageError('Input file changed while hashing.')
    return length, digest.hexdigest()


def _approved_dependency_root(name):
    metadata = _DIST_INFO.fullmatch(name)
    return (name in _MODULE_NAMES or name == 'pyodbc.pyi' or bool(_BUNDLED_DLL.fullmatch(name))
            or bool(_EXTENSION.fullmatch(name))
            or bool(metadata and metadata.group(1) in DEPENDENCY_NAMES))


def _ignored_dependency(name):
    parts = name.split('/')
    return ('__pycache__' in parts or name.lower().endswith(('.pyc', '.pyo'))
            or parts[0] in ('bin', 'Scripts'))


def _dependency_inventory(root):
    """Require the complete production graph and each copied wheel RECORD hash."""
    inventory, distributions = {}, set()
    for directory in sorted(root.iterdir()):
        match = _DIST_INFO.fullmatch(directory.name)
        if not match:
            continue
        name = match.group(1)
        if name not in DEPENDENCY_NAMES or name in distributions:
            raise PackageError('Unexpected or duplicate dependency distribution.')
        distributions.add(name)
        record = directory / 'RECORD'
        _check_path(record)
        if record.stat().st_size > MAX_MANIFEST_BYTES:
            raise PackageError('Dependency RECORD exceeds its bound.')
        with record.open(encoding='utf-8', newline='') as stream:
            for row in csv.reader(stream):
                if len(row) != 3:
                    raise PackageError('Malformed dependency RECORD.')
                relative, digest, length = row
                launcher = {'qrcode': '../../bin/qr.exe', 'cffi': '../../bin/cffi-gen-src.exe'}
                if relative == launcher.get(name) or _ignored_dependency(relative):
                    continue
                validate_relative_path('packages/' + relative)
                if relative.casefold() in inventory:
                    raise PackageError('Duplicate dependency RECORD path.')
                if relative == directory.name + '/RECORD' and not digest and not length:
                    inventory[relative.casefold()] = None
                elif re.fullmatch(r'sha256=[A-Za-z0-9_-]{43}', digest) and length.isdecimal():
                    expected = base64.urlsafe_b64decode(digest[7:] + '=').hex()
                    inventory[relative.casefold()] = (expected, int(length))
                else:
                    raise PackageError('Dependency RECORD requires SHA256 and size.')
                if _BUNDLED_DLL.fullmatch(relative) and name != 'pyodbc':
                    raise PackageError('Bundled VC runtime must belong to pyodbc.')
                if len(inventory) > MAX_FILES:
                    raise PackageError('Dependency RECORD contains too many files.')
    if distributions != DEPENDENCY_NAMES:
        raise PackageError('The complete production dependency graph is required.')
    required = {'_cffi_backend.cp314-win_amd64.pyd', 'pyodbc.cp314-win_amd64.pyd', 'pyodbc.pyi'}
    required |= {name + '/__init__.py' for name in _MODULE_NAMES if name != 'pillow.libs'}
    if not {name.casefold() for name in required}.issubset(inventory):
        raise PackageError('Required production dependency modules are missing.')
    return inventory


def _dependency_files(root):
    stack, visited = [root], 0
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), reverse=True):
            visited += 1
            if visited > 2 * MAX_FILES:
                raise PackageError('Dependency tree contains too many entries.')
            _check_path(path)
            relative = path.relative_to(root).as_posix()
            parts = relative.split('/')
            if _ignored_dependency(relative):
                continue
            if len(parts) > 32:
                raise PackageError('Dependency tree is too deep.')
            if (not _approved_dependency_root(parts[0]) or path.suffix.lower() == '.pth'
                    or path.name.lower() in ('sitecustomize.py', 'usercustomize.py')):
                raise PackageError('Unexpected dependency or Python startup hook.')
            validate_relative_path('packages/' + relative)
            if path.is_dir():
                stack.append(path)
            elif path.is_file():
                yield path, 'packages/' + relative
            else:
                raise PackageError('Dependency is not a regular file or directory.')


def _inputs(source, dependencies, binaries):
    inventory = _dependency_inventory(dependencies)
    files = [(source / name, 'src/' + name) for name in SOURCE_FILES]
    files += [(binaries / name, 'bin/' + name) for name in BINARY_FILES]
    files += list(_dependency_files(dependencies))
    if len(files) > MAX_FILES:
        raise PackageError('Package contains too many files.')
    names, payloads, total = set(), [], 0
    for path, relative in sorted(files, key=lambda item: item[1]):
        validate_relative_path(relative)
        if relative.casefold() in names:
            raise PackageError('Case-insensitive package path collision.')
        names.add(relative.casefold())
        size, digest = _hash_file(path)
        if relative.startswith('packages/'):
            name = relative.removeprefix('packages/').casefold()
            if name not in inventory or inventory[name] not in (None, (digest, size)):
                raise PackageError('Dependency payload does not match its wheel RECORD.')
        if relative.startswith('bin/'):
            with path.open('rb') as binary:
                if binary.read(2) != b'MZ':
                    raise PackageError('Service binary is not a Windows executable.')
        total += size
        if total > MAX_TOTAL_BYTES:
            raise PackageError('Package exceeds its total size bound.')
        payloads.append(_Payload(path, relative, size, digest))
    packaged = {item.relative.removeprefix('packages/').casefold() for item in payloads
                if item.relative.startswith('packages/')}
    if packaged != set(inventory):
        raise PackageError('Dependency RECORD payload files are missing.')
    return tuple(payloads), total


def _copy_file(source, target, digest, size):
    _check_path(source)
    _check_path(target.parent)
    actual, length = hashlib.sha256(), 0
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        while chunk := incoming.read(_CHUNK_BYTES):
            length += len(chunk)
            if length > size:
                raise PackageError('Input changed after package preflight.')
            actual.update(chunk)
            outgoing.write(chunk)
    if length != size or actual.hexdigest() != digest:
        raise PackageError('Input changed after package preflight.')


def build_package(source_root: str | Path, dependencies: str | Path,
                  binaries: str | Path, output: str | Path) -> dict[str, int | str]:
    source, deps, compiled = map(_directory, (source_root, dependencies, binaries))
    target = Path(os.path.abspath(output))
    if target.exists() or target.is_symlink():
        raise PackageError('Output already exists; choose a new release directory.')
    _check_path(target.parent)
    if any(target.is_relative_to(root) for root in (deps, compiled)):
        raise PackageError('Output must be outside dependency and binary inputs.')
    payloads, total = _inputs(source, deps, compiled)
    manifest = {'schema_version': 1,
                'files': {item.relative: item.sha256 for item in payloads}}
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise PackageError('Package manifest exceeds its size bound.')
    target.mkdir()
    for item in payloads:
        destination = target.joinpath(*item.relative.split('/'))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(item.source, destination, item.sha256, item.size)
    _check_path(target)
    with (target / 'package_manifest.json').open('xb') as manifest_file:
        manifest_file.write(encoded)
    return {'schema_version': 1, 'files': len(payloads), 'bytes': total,
            'manifest_sha256': hashlib.sha256(encoded).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1],
                        help='EventDesk source root; only the fixed runtime allowlist is copied.')
    parser.add_argument('--dependencies', type=Path, required=True,
                        help='Fresh pip --target directory containing production dependencies only.')
    parser.add_argument('--binaries', type=Path, required=True,
                        help='Directory containing the two compiled x64 service executables.')
    parser.add_argument('--output', type=Path, required=True,
                        help='New release directory with an existing parent; existing output is refused.')
    args = parser.parse_args(argv)
    try:
        result = build_package(args.source, args.dependencies, args.binaries, args.output)
    except (OSError, PackageError) as error:
        parser.exit(1, f'Package build failed: {error}\n')
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
