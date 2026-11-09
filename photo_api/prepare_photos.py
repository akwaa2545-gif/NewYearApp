"""Prepare a private, reviewable employee photo batch; never connect to SQL Server.

Run with the project virtualenv Python. Originals and the employee master are
read-only inputs. Different source bytes for one ID exclude the entire ID unless
the operator explicitly prefers one unambiguous source outside JPEG subfolders.
"""
import argparse
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Sequence, TypedDict
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_PIXELS = 20_000_000
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
PHOTO_SIZE = (256, 256)
EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.bmp', '.mpo'})
FORMATS = frozenset({'JPEG', 'PNG', 'BMP', 'MPO'})
IMAGE_ERRORS = (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning)
DEFAULT_EMPLOYEES = Path(__file__).resolve().parents[1] / 'data' / 'employees.json'


class PhotoEntry(TypedDict):
    employee_id: str
    file: str
    sha256: str
    bytes: int
    content_type: str
    width: int
    height: int


class Manifest(TypedDict):
    schema_version: int
    batch_id: str
    employee_source_count: int
    entries: list[PhotoEntry]


class PhotoGroup(TypedDict):
    employee_id: str
    paths: list[str]


class PhotoIssue(TypedDict):
    employee_id: str
    path: str
    reason: str


class PreferredMainPhoto(TypedDict):
    employee_id: str
    selected_path: str
    source_sha256: str
    excluded_paths: list[str]


class ReportCounts(TypedDict):
    source_images: int
    employee_source: int
    prepared: int
    conflicts: int
    unknown: int
    missing: int
    failed: int
    ignored_files: int
    identical_duplicates: int
    preferred_main: int


class Report(TypedDict):
    schema_version: int
    batch_id: str
    counts: ReportCounts
    conflicts: list[PhotoGroup]
    unknown: list[PhotoGroup]
    missing_employee_ids: list[str]
    errors: list[PhotoIssue]
    preferred_main_photos: list[PreferredMainPhoto]


def normalize_id(value: object) -> str:
    """Match the app's seven-digit numeric IDs without accepting unsafe paths."""
    value = str(value).strip()
    if not re.fullmatch(r'[0-9]{1,32}', value):
        raise ValueError('Employee IDs must contain 1 to 32 ASCII digits')
    return value.zfill(7)


def _has_link(path):
    return any(part.is_symlink() or part.is_junction()
               for part in (path, *path.parents))


def _validate_paths(photos, output):
    if '..' in Path(output).parts:
        raise ValueError('Output path must not contain parent traversal')
    photos = Path(os.path.abspath(photos))
    output = Path(os.path.abspath(output))
    if _has_link(photos) or _has_link(output):
        raise ValueError('Photo and output paths must not use symlinks or junctions')
    if not photos.is_dir():
        raise ValueError('Photo source must be an existing directory')
    if photos == output or photos in output.parents or output in photos.parents:
        raise ValueError('Output must be separate from the original photo directory')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Output must be a new or empty directory; batches are never overwritten')
    return photos, output


def _employee_ids(employees, database):
    if database is not None:
        database = Path(database).resolve(strict=True)
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            rows = connection.execute('SELECT employee_id FROM employees').fetchall()
        return frozenset(normalize_id(row[0]) for row in rows)
    with Path(employees or DEFAULT_EMPLOYEES).open(encoding='utf-8-sig') as stream:
        payload = json.load(stream)
    rows = payload['employees'] if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError('Employee JSON must contain an employees list')
    return frozenset(normalize_id(row['employee_id']) for row in rows)


def _source_paths(photos):
    def fail_scan(error):
        raise error

    for parent, directories, files in os.walk(photos, followlinks=False, onerror=fail_scan):
        directories[:] = sorted(name for name in directories
                                if not _has_link(Path(parent) / name))
        for name in sorted(files):
            yield Path(parent) / name


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _scan_sources(photos):
    groups, errors = {}, []
    ignored, source_count = 0, 0
    for path in _source_paths(photos):
        relative = path.relative_to(photos).as_posix()
        if _has_link(path) or path.suffix.lower() not in EXTENSIONS:
            ignored += 1
            continue
        source_count += 1
        try:
            employee_id = normalize_id(path.stem)
        except ValueError:
            ignored += 1
            continue
        try:
            digest = _file_hash(path)
            item = {'path': relative, 'sha256': digest}
        except OSError:
            item = {'path': relative, 'sha256': None}
            errors.append({'employee_id': employee_id, 'path': relative, 'reason': 'source_unreadable'})
        groups[employee_id] = [*groups.get(employee_id, []), item]
    return groups, errors, ignored, source_count


def _thumbnail(path):
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('source_too_large')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if source.format not in FORMATS:
                raise ValueError('unsupported_image_format')
            if source.format == 'MPO':
                # Only the primary JPEG is an employee portrait; never iterate
                # or decode alternate views embedded in the MPO container.
                source.seek(0)
            if source.width * source.height > MAX_PIXELS:
                raise ValueError('pixel_limit_exceeded')
            source.load()
            oriented = ImageOps.exif_transpose(source)
            oriented.thumbnail(PHOTO_SIZE, Image.Resampling.LANCZOS)
            if 'A' in oriented.getbands() or 'transparency' in oriented.info:
                rgba = oriented.convert('RGBA')
                rgb = Image.new('RGB', rgba.size, 'white')
                rgb.paste(rgba, mask=rgba.getchannel('A'))
            else:
                rgb = oriented.convert('RGB')
            # A new canvas drops EXIF, XMP, ICC and other source metadata.
            clean = Image.new('RGB', rgb.size)
            clean.paste(rgb)
            buffer = BytesIO()
            clean.save(buffer, format='JPEG', quality=82, optimize=True)
            content = buffer.getvalue()
            if len(content) > MAX_OUTPUT_BYTES:
                raise ValueError('output_too_large')
            return content, clean.width, clean.height


def _atomic_json(path, payload):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, ensure_ascii=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _prepare_entry(employee_id, item, photos) -> tuple[PhotoEntry, bytes]:
    path = photos / item['path']
    if _has_link(path) or _file_hash(path) != item['sha256']:
        raise ValueError('source_changed_during_preparation')
    content, width, height = _thumbnail(path)
    if _file_hash(path) != item['sha256']:
        raise ValueError('source_changed_during_preparation')
    relative = f'photos/{employee_id}.jpg'
    entry: PhotoEntry = {'employee_id': employee_id, 'file': relative,
                         'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
                         'content_type': 'image/jpeg', 'width': width, 'height': height}
    return entry, content


def _atomic_photo(path: Path, content: bytes) -> None:
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _error_item(employee_id, item, error):
    allowed = {'source_too_large', 'unsupported_image_format', 'pixel_limit_exceeded',
               'output_too_large', 'source_changed_during_preparation'}
    reason = str(error) if str(error) in allowed else 'invalid_or_unreadable_image'
    return {'employee_id': employee_id, 'path': item['path'], 'reason': reason}


def _validate_excluded(employee_id, items, photos):
    errors = []
    for item in items:
        if item['sha256'] is None:
            continue  # Already recorded during the source scan.
        try:
            path = photos / item['path']
            if _has_link(path):
                raise ValueError('source_changed_during_preparation')
            _thumbnail(path)
        except IMAGE_ERRORS as error:
            errors.append(_error_item(employee_id, item, error))
    return errors


def _main_candidates(items):
    candidates = [item for item in items
                  if not any(part.casefold() == 'jpeg' for part in item['path'].split('/')[:-1])]
    hashes = {item['sha256'] for item in candidates}
    return candidates if candidates and len(hashes) == 1 and None not in hashes else []


def _prepare_groups(groups, employee_ids, photos, output, scan_errors, prefer_main_folder=False):
    entries, conflicts, unknown = [], [], []
    preferred_main = []
    errors, duplicates = list(scan_errors), 0
    for employee_id, items in sorted(groups.items()):
        paths = sorted(item['path'] for item in items)
        if employee_id not in employee_ids:
            unknown.append({'employee_id': employee_id, 'paths': paths})
            errors.extend(_validate_excluded(employee_id, items, photos))
            continue
        hashes = {item['sha256'] for item in items}
        preference = None
        if len(hashes) != 1 or None in hashes:
            candidates = _main_candidates(items) if prefer_main_folder else []
            if not candidates:
                conflicts.append({'employee_id': employee_id, 'paths': paths})
                errors.extend(_validate_excluded(employee_id, items, photos))
                continue
            selected = min(candidates, key=lambda candidate: candidate['path'])
            preference = {'employee_id': employee_id, 'selected_path': selected['path'],
                          'source_sha256': selected['sha256'],
                          'excluded_paths': [path for path in paths if path != selected['path']]}
            errors.extend(_validate_excluded(employee_id, [item for item in items if item not in candidates], photos))
            items = candidates
        duplicates += len(items) - 1
        item = min(items, key=lambda candidate: candidate['path'])
        try:
            entry, content = _prepare_entry(employee_id, item, photos)
        except IMAGE_ERRORS as error:
            errors.append(_error_item(employee_id, item, error))
            continue
        # Destination failures must abort the batch, never look like invalid input.
        _atomic_photo(output / entry['file'], content)
        entries.append(entry)
        if preference is not None:
            preferred_main.append(preference)
    return entries, conflicts, unknown, errors, duplicates, preferred_main


def prepare_photos(
    photos: str | Path,
    employees: str | Path | None,
    output: str | Path,
    *,
    database: str | Path | None = None,
    prefer_main_folder: bool = False,
) -> tuple[Manifest, Report]:
    """Write bounded JPEGs, manifest and exception report into a fresh directory."""
    photos, output = _validate_paths(photos, output)
    employee_ids = _employee_ids(employees, database)
    groups, scan_errors, ignored, source_count = _scan_sources(photos)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'photos').mkdir()
    entries, conflicts, unknown, errors, duplicates, preferred_main = _prepare_groups(
        groups, employee_ids, photos, output, scan_errors, prefer_main_folder)
    batch_id = str(uuid.uuid4())
    manifest: Manifest = {'schema_version': 1, 'batch_id': batch_id,
                          'employee_source_count': len(employee_ids), 'entries': entries}
    missing = sorted(employee_ids - groups.keys())
    counts: ReportCounts = {'source_images': source_count, 'employee_source': len(employee_ids),
                            'prepared': len(entries), 'conflicts': len(conflicts), 'unknown': len(unknown),
                            'missing': len(missing), 'failed': len(errors), 'ignored_files': ignored,
                            'identical_duplicates': duplicates, 'preferred_main': len(preferred_main)}
    report: Report = {'schema_version': 1, 'batch_id': batch_id, 'counts': counts,
                      'conflicts': conflicts, 'unknown': unknown, 'missing_employee_ids': missing,
                      'errors': errors, 'preferred_main_photos': preferred_main}
    _atomic_json(output / 'report.json', report)
    _atomic_json(output / 'manifest.json', manifest)
    return manifest, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--photos', type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--employees', type=Path, default=DEFAULT_EMPLOYEES)
    source.add_argument('--database', type=Path, help='Read current IDs from SQLite in read-only mode')
    parser.add_argument('--output', type=Path, required=True, help='New or empty private batch directory')
    parser.add_argument('--prefer-main-folder', action='store_true',
                        help='Prefer an unambiguous original outside JPEG subfolders when copies differ')
    args = parser.parse_args(argv)
    try:
        _, report = prepare_photos(args.photos, args.employees, args.output, database=args.database,
                                   prefer_main_folder=args.prefer_main_folder)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f'Preparation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps({'status': 'prepared_for_review', 'counts': report['counts']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
