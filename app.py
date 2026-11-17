"""Company Party EventDesk: standard-library Python web server + SQLite.

Supports multi-station QR check-in, physical-ID confirmation and an atomic draw.
Run setup.py first. Production must be behind a trusted HTTPS reverse proxy.
"""
from __future__ import annotations

import base64
import binascii
import csv
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / 'static'
CONFIG_PATH = Path(os.environ.get('PARTY_CONFIG', ROOT / 'data' / 'config.json'))
DB_PATH = Path(os.environ.get('PARTY_DATABASE', ROOT / 'data' / 'party.sqlite3'))
DEFAULT_DB_PATH = ROOT / 'data' / 'party.sqlite3'
EMPLOYEE_JSON_PATH = Path(os.environ.get('PARTY_EMPLOYEE_JSON', ROOT / 'data' / 'employees.json'))
MAX_BODY = 8 * 1024 * 1024
PRIZE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
MAX_EMPLOYEES = 5000
MAX_IMPORT_ROWS = 12000
IMPORT_PREVIEW_TTL_SECONDS = 3600
TOKEN_PREFIX = 'PARTY:'
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()
NAMED_ROLES = ('admin', 'checkin_staff', 'verification_staff', 'lucky_draw_operator')
SESSION_ROLES = NAMED_ROLES + ('staff',)
WRISTBAND_ACTIVE_STATUSES = ('not_issued', 'issued')
DEFAULT_SETTINGS = {
    'require_verification_for_draw': False,
    'allow_multiple_wins': False,
    'lucky_participants_configured': False,
    'accent_color': '#1976d2',
    'projector_title': 'Lucky Draw',
    'projector_theme': 'gold',
    'projector_show_employee_id': True,
}
PROJECTOR_SPIN_SECONDS = 5.0
EMPLOYEE_ID_WIDTH = 7
PHOTO_MAX_BYTES = 256 * 1024
PHOTO_PROXY_SLOTS = threading.BoundedSemaphore(4)
PHOTO_RATE_LIMIT = 120
PHOTO_RATE_ATTEMPTS = {}
PHOTO_RATE_LOCK = threading.Lock()


class EmployeePhotoError(Exception):
    def __init__(self, status: int = 503) -> None:
        self.status = status
        super().__init__('No photo.' if status == 404 else 'Photo unavailable.')


def employee_photo_exists(employee_id: str) -> bool:
    """Check the current employee master before exposing its private photo route."""
    if not isinstance(employee_id, str) or not re.fullmatch(r'[0-9]{7}', employee_id):
        return False
    with db_connection() as db:
        return db.execute('SELECT 1 FROM employees WHERE employee_id=?', (employee_id,)).fetchone() is not None


def photo_request_allowed(user: Mapping[str, object]) -> bool:
    """Bound per-session photo work and keep only a small active rate window."""
    global PHOTO_RATE_ATTEMPTS
    session_id = user.get('sid')
    if not session_id:
        return False
    current = time.monotonic()
    with PHOTO_RATE_LOCK:
        active = {key: value for key, value in PHOTO_RATE_ATTEMPTS.items()
                  if current - value[0] < 60}
        started, count = active.get(session_id, (current, 0))
        if count >= PHOTO_RATE_LIMIT or (session_id not in active and len(active) >= 256):
            PHOTO_RATE_ATTEMPTS = active
            return False
        PHOTO_RATE_ATTEMPTS = {**active, session_id: (started, count + 1)}
        return True


def _read_employee_photo(response, deadline):
    if response.status != 200 or response.headers.get('Content-Type', '').lower() != 'image/jpeg':
        raise EmployeePhotoError()
    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise EmployeePhotoError()
    length = response.headers.get('Content-Length')
    expected_length = int(length) if length is not None else None
    if expected_length is not None and not 3 <= expected_length <= PHOTO_MAX_BYTES:
        raise EmployeePhotoError()
    chunks, size = [], 0
    while True:
        if time.monotonic() >= deadline:
            raise EmployeePhotoError()
        chunk = response.read1(min(16384, PHOTO_MAX_BYTES + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > PHOTO_MAX_BYTES:
            raise EmployeePhotoError()
        chunks.append(chunk)
    data = b''.join(chunks)
    if ((expected_length is not None and len(data) != expected_length)
            or not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9')):
        raise EmployeePhotoError()
    return data


def fetch_employee_photo(employee_id: str) -> bytes:
    """Server-side photo fetch; the caller authorizes the current employee first."""
    if not isinstance(employee_id, str) or not re.fullmatch(r'[0-9]{7}', employee_id):
        raise EmployeePhotoError(400)
    token = os.environ.get('PARTY_PHOTO_API_TOKEN', '')
    if not (32 <= len(token) <= 512 and all(33 <= ord(character) <= 126 for character in token)):
        raise EmployeePhotoError()
    if not PHOTO_PROXY_SLOTS.acquire(blocking=False):
        raise EmployeePhotoError()
    try:
        # urllib networking is unavailable in the hosted Python runtime. Keep it
        # lazy: the main hosted app has no pilot credential and stays disabled.
        from http.client import HTTPException
        from urllib.error import HTTPError
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        class NoPhotoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = Request('https://eventdesk-photo-pilot.pages.dev/v1/employees/' + employee_id + '/photo',
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'image/jpeg',
                                   'Accept-Encoding': 'identity', 'User-Agent': 'EventDeskPhotoProxy/1.0'})
        deadline = time.monotonic() + 6
        try:
            with build_opener(NoPhotoRedirect()).open(request, timeout=4) as response:
                return _read_employee_photo(response, deadline)
        except HTTPError as error:
            status = 404 if error.code == 404 else 503
            error.close()
            raise EmployeePhotoError(status) from None
        except (OSError, ValueError, HTTPException):
            raise EmployeePhotoError() from None
    finally:
        PHOTO_PROXY_SLOTS.release()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize_employee_id(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    employee_id = str(value).strip()
    if employee_id.isdigit() and len(employee_id) < EMPLOYEE_ID_WIDTH:
        return employee_id.zfill(EMPLOYEE_ID_WIDTH)
    return employee_id


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as fp:
        config = json.load(fp)
    for key in ('secret', 'admin_password', 'staff_password'):
        if not config.get(key):
            raise RuntimeError('Invalid setup config, missing ' + key)
    return config


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 350_000).hex()


def check_password(password, entry):
    if not isinstance(password, str) or len(password) > 512:
        return False
    received = password_hash(password, entry['salt'])
    return hmac.compare_digest(received, entry['hash'])


def sign_session(role, config, username=None, display_name=None, account_type=None):
    claims = {'role': role, 'exp': int(time.time()) + 12 * 3600, 'csrf': secrets.token_urlsafe(24),
              'sid': secrets.token_urlsafe(24)}
    if username:
        claims['username'] = username
    if display_name:
        claims['display_name'] = display_name
    if account_type:
        claims['account_type'] = account_type
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).decode().rstrip('=')
    mac = hmac.new(bytes.fromhex(config['secret']), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + mac, claims


def parse_session(raw, config):
    try:
        payload, mac = raw.split('.', 1)
        expected = hmac.new(bytes.fromhex(config['secret']), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, mac):
            return None
        decoded = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
        claims = json.loads(decoded)
        if claims.get('role') not in SESSION_ROLES or claims.get('exp', 0) < time.time():
            return None
        if not isinstance(claims.get('csrf'), str):
            return None
        return claims
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, binascii.Error,
            json.JSONDecodeError, OverflowError):
        return None


@contextmanager
def db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
    finally:
        db.close()


def init_db():
    with db_connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                employee_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                qr_token TEXT NOT NULL UNIQUE,
                checked_in_at TEXT,
                verified_at TEXT,
                extra_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS ix_employees_name ON employees(name);
            CREATE INDEX IF NOT EXISTS ix_employees_checkin ON employees(checked_in_at);
            CREATE TABLE IF NOT EXISTS winners (
                id INTEGER PRIMARY KEY,
                employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(id),
                prize TEXT NOT NULL,
                drawn_at TEXT NOT NULL,
                drawn_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY,
                employee_id INTEGER REFERENCES employees(id),
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_event_log_time ON event_log(id DESC);
        ''')
        _run_migrations(db)


def _table_columns(db, table):
    return {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}


def _ensure_column(db, table, column, declaration):
    if column not in _table_columns(db, table):
        db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + declaration)


def _run_migrations(db):
    """Additive migrations keep databases created by the prototype usable."""
    db.executescript('''
        CREATE TABLE IF NOT EXISTS schema_version (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
    ''')
    _ensure_column(db, 'employees', 'extra_json', "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(db, 'winners', 'status', "TEXT NOT NULL DEFAULT 'active'")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_name TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','checkin_staff','verification_staff','lucky_draw_operator')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS staff_sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_staff_sessions_activity ON staff_sessions(last_seen);
        CREATE TABLE IF NOT EXISTS wristbands (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('not_issued','issued','replaced','revoked')),
            created_at TEXT NOT NULL,
            issued_at TEXT,
            issued_by TEXT,
            invalidated_at TEXT,
            replaced_by_id INTEGER REFERENCES wristbands(id),
            reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_wristbands_active_employee
            ON wristbands(employee_id) WHERE status IN ('not_issued','issued');
        CREATE INDEX IF NOT EXISTS ix_wristbands_token_status ON wristbands(token,status);
        CREATE TABLE IF NOT EXISTS wristband_distribution_history (
            id INTEGER PRIMARY KEY,
            wristband_id INTEGER NOT NULL REFERENCES wristbands(id),
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_wristband_history_employee
            ON wristband_distribution_history(employee_id,id DESC);
        CREATE TABLE IF NOT EXISTS event_checkins (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(id),
            wristband_id INTEGER REFERENCES wristbands(id),
            checked_in_at TEXT NOT NULL,
            staff_username TEXT NOT NULL,
            station TEXT,
            legacy_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_event_checkins_time ON event_checkins(checked_in_at);
        CREATE TABLE IF NOT EXISTS checkin_wristband_exceptions (
            employee_id INTEGER PRIMARY KEY REFERENCES employees(id),
            reason TEXT NOT NULL CHECK(reason IN ('lost', 'unreadable', 'other')),
            detail TEXT NOT NULL DEFAULT '',
            recorded_by TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance_removals (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            checked_in_at TEXT NOT NULL,
            checkin_json TEXT NOT NULL,
            removed_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            removed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS draw_history_removals (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL UNIQUE,
            history_json TEXT NOT NULL,
            removed_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            removed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_verification_history (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER REFERENCES employees(id),
            wristband_id INTEGER REFERENCES wristbands(id),
            result TEXT NOT NULL CHECK(result IN ('verified','identity_mismatch','not_checked_in','invalid_wristband')),
            reason TEXT,
            verified_at TEXT NOT NULL,
            staff_username TEXT NOT NULL,
            scanned_token_hash TEXT,
            legacy_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_verification_employee_time
            ON identity_verification_history(employee_id,id DESC);
        CREATE TABLE IF NOT EXISTS import_history (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            mapping_json TEXT,
            summary_json TEXT,
            reconciliation_json TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            committed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS import_previews (
            id INTEGER PRIMARY KEY,
            upload_id TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            parsed_json TEXT NOT NULL,
            preview_json TEXT,
            mapping_json TEXT,
            status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            committed_import_id INTEGER REFERENCES import_history(id)
        );
        CREATE INDEX IF NOT EXISTS ix_import_previews_expiry ON import_previews(expires_at);
        CREATE TABLE IF NOT EXISTS lucky_draw_participants (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(id),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            source_import_id INTEGER REFERENCES import_history(id),
            added_at TEXT NOT NULL,
            removed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS prizes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reward_images (
            prize_id INTEGER PRIMARY KEY REFERENCES prizes(id),
            content_type TEXT NOT NULL, data BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reward_drafts (
            id INTEGER PRIMARY KEY, source_id INTEGER UNIQUE REFERENCES prizes(id),
            name TEXT NOT NULL, quantity INTEGER NOT NULL, action TEXT NOT NULL DEFAULT 'save',
            content_type TEXT, image BLOB, image_changed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lucky_draw_rounds (
            id INTEGER PRIMARY KEY,
            prize_id INTEGER REFERENCES prizes(id),
            round_number INTEGER NOT NULL,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL,
            drawn_at TEXT,
            drawn_by TEXT,
            reroll_of_winner_id INTEGER,
            legacy_source TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS lucky_draw_winners (
            id INTEGER PRIMARY KEY,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            round_id INTEGER NOT NULL REFERENCES lucky_draw_rounds(id),
            prize_id INTEGER REFERENCES prizes(id),
            prize_name TEXT NOT NULL,
            won_at TEXT NOT NULL,
            won_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','rerolled')),
            rerolled_at TEXT,
            rerolled_by TEXT,
            reroll_reason TEXT,
            legacy_winner_id INTEGER UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_draw_winners_employee_status
            ON lucky_draw_winners(employee_id,status);
        CREATE INDEX IF NOT EXISTS ix_draw_winners_prize_status
            ON lucky_draw_winners(prize_id,status);
        CREATE TABLE IF NOT EXISTS event_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lucky_draw_presentation (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            sequence INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'waiting' CHECK(phase IN
                ('waiting','preparing','spinning','winner_selected','winner_announced','ready_for_next')),
            winner_id INTEGER REFERENCES lucky_draw_winners(id),
            prize_name TEXT,
            round_name TEXT,
            winner_json TEXT,
            spin_started_at TEXT,
            reveal_at TEXT,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_audit_logs_time ON audit_logs(id DESC);
    ''')
    db.execute('''INSERT OR IGNORE INTO lucky_draw_presentation
                  (singleton,sequence,phase,updated_at,updated_by)
                  VALUES (1,0,'waiting',?,'system')''', (now(),))
    applied = {row['version'] for row in db.execute('SELECT version FROM migrations')}
    if 1 not in applied:
        db.execute('BEGIN IMMEDIATE')
        try:
            stamp = now()
            for employee in db.execute('SELECT id,qr_token,checked_in_at,verified_at FROM employees').fetchall():
                cursor = db.execute('''
                    INSERT OR IGNORE INTO wristbands
                        (employee_id,token,status,created_at,issued_at,issued_by)
                    VALUES (?,?, 'issued', ?, ?, 'migration')
                ''', (employee['id'], employee['qr_token'], stamp, stamp))
                wristband = db.execute('SELECT id FROM wristbands WHERE token=?',
                                       (employee['qr_token'],)).fetchone()
                if cursor.rowcount and wristband:
                    db.execute('''INSERT INTO wristband_distribution_history
                        (wristband_id,employee_id,action,status,actor,occurred_at)
                        VALUES (?,?,'migrated','issued','migration',?)''',
                               (wristband['id'], employee['id'], stamp))
                if employee['checked_in_at']:
                    db.execute('''INSERT OR IGNORE INTO event_checkins
                        (employee_id,wristband_id,checked_in_at,staff_username,legacy_key)
                        VALUES (?,?,?,'migration',?)''',
                               (employee['id'], wristband['id'] if wristband else None,
                                employee['checked_in_at'], 'employee:' + str(employee['id'])))
                if employee['verified_at']:
                    db.execute('''INSERT OR IGNORE INTO identity_verification_history
                        (employee_id,wristband_id,result,verified_at,staff_username,legacy_key)
                        VALUES (?,?,'verified',?,'migration',?)''',
                               (employee['id'], wristband['id'] if wristband else None,
                                employee['verified_at'], 'employee:' + str(employee['id'])))
            for winner in db.execute('SELECT * FROM winners').fetchall():
                prize = db.execute('SELECT id FROM prizes WHERE name=? COLLATE NOCASE',
                                   (winner['prize'],)).fetchone()
                if not prize:
                    cursor = db.execute('''INSERT INTO prizes(name,quantity,active,created_at,updated_at)
                                           VALUES (?, ?, 1, ?, ?)''',
                                        (winner['prize'], MAX_EMPLOYEES, stamp, stamp))
                    prize_id = cursor.lastrowid
                else:
                    prize_id = prize['id']
                round_cursor = db.execute('''INSERT OR IGNORE INTO lucky_draw_rounds
                    (prize_id,round_number,label,status,created_at,drawn_at,drawn_by,legacy_source)
                    VALUES (?,1,?,'completed',?,?,?,?)''',
                    (prize_id, winner['prize'], winner['drawn_at'], winner['drawn_at'],
                     winner['drawn_by'], 'winner:' + str(winner['id'])))
                round_row = db.execute('SELECT id FROM lucky_draw_rounds WHERE legacy_source=?',
                                       ('winner:' + str(winner['id']),)).fetchone()
                db.execute('''INSERT OR IGNORE INTO lucky_draw_winners
                    (employee_id,round_id,prize_id,prize_name,won_at,won_by,status,legacy_winner_id)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (winner['employee_id'], round_row['id'], prize_id, winner['prize'],
                     winner['drawn_at'], winner['drawn_by'], winner['status'], winner['id']))
            for key, value in DEFAULT_SETTINGS.items():
                db.execute('''INSERT OR IGNORE INTO event_settings(key,value_json,updated_at,updated_by)
                              VALUES (?,?,?,'migration')''',
                           (key, json.dumps(value), stamp))
            db.execute('INSERT INTO migrations(version,name,applied_at) VALUES (1,?,?)',
                       ('normalized_event_schema', stamp))
            db.execute('''INSERT INTO schema_version(singleton,version) VALUES (1,1)
                          ON CONFLICT(singleton) DO UPDATE SET version=excluded.version''')
            db.commit()
        except Exception:
            db.rollback()
            raise
    else:
        db.execute('''INSERT INTO schema_version(singleton,version) VALUES (1,1)
                      ON CONFLICT(singleton) DO UPDATE SET version=MAX(version,excluded.version)''')
    applied = {row['version'] for row in db.execute('SELECT version FROM migrations')}
    if 2 not in applied:
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = db.execute('SELECT id,employee_id FROM employees').fetchall()
            targets = {row['id']: normalize_employee_id(row['employee_id']) for row in rows}
            values = list(targets.values())
            if len(values) != len(set(values)):
                raise RuntimeError('Cannot pad employee IDs because normalized values collide.')
            changed = [row for row in rows if targets[row['id']] != row['employee_id']]
            for row in changed:
                db.execute('UPDATE employees SET employee_id=? WHERE id=?',
                           ('__employee_id_padding_' + str(row['id']), row['id']))
            for row in changed:
                db.execute('UPDATE employees SET employee_id=? WHERE id=?',
                           (targets[row['id']], row['id']))
            stamp = now()
            db.execute('INSERT INTO migrations(version,name,applied_at) VALUES (2,?,?)',
                       ('pad_numeric_employee_ids_to_seven_digits', stamp))
            db.execute('''INSERT INTO schema_version(singleton,version) VALUES (1,2)
                          ON CONFLICT(singleton) DO UPDATE SET version=MAX(version,excluded.version)''')
            db.commit()
        except Exception:
            db.rollback()
            raise


def actor_name(user_or_actor):
    if isinstance(user_or_actor, dict):
        return user_or_actor.get('username') or ('legacy:' + user_or_actor.get('role', 'unknown'))
    return str(user_or_actor or 'system')


def audit(db, actor, action, entity_type, entity_id=None, details=None):
    db.execute('''INSERT INTO audit_logs(actor,action,entity_type,entity_id,details_json,occurred_at)
                  VALUES (?,?,?,?,?,?)''',
               (actor_name(actor), action, entity_type,
                None if entity_id is None else str(entity_id),
                json.dumps(details or {}, ensure_ascii=False, separators=(',', ':'), default=str), now()))


def get_setting(key, default=None, db=None):
    if default is None:
        default = DEFAULT_SETTINGS.get(key)
    if db is not None:
        row = db.execute('SELECT value_json FROM event_settings WHERE key=?', (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row['value_json'])
        except (json.JSONDecodeError, TypeError):
            return default
    with db_connection() as connection:
        return get_setting(key, default, connection)


def set_setting(db, key, value, actor):
    db.execute('''INSERT INTO event_settings(key,value_json,updated_at,updated_by)
                  VALUES (?,?,?,?)
                  ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                      updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
               (key, json.dumps(value, ensure_ascii=False), now(), actor_name(actor)))


def _detect_prize_image_type(payload):
    if payload.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if payload.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if len(payload) >= 12 and payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        return 'image/webp'
    return None


def get_projector_prize_image(db=None):
    metadata = get_setting('projector_prize_image', None, db)
    if not isinstance(metadata, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(metadata.get('id') or '')):
        return None, None
    path = DB_PATH.parent / 'prize-images' / (metadata['id'] + '.bin')
    try:
        payload = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None, None
    content_type = _detect_prize_image_type(payload)
    if not content_type or content_type != metadata.get('content_type'):
        return None, None
    return metadata, payload


def update_projector_prize_image(data, actor):
    if not isinstance(data, dict):
        raise ValueError('Invalid prize image request.')
    metadata = None
    if not data.get('clear'):
        data_url = str(data.get('data_url') or '')
        match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)', data_url)
        if not match:
            raise ValueError('Choose a PNG, JPEG, or WebP image.')
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('The prize image is not valid.') from None
        if not payload or len(payload) > PRIZE_IMAGE_MAX_BYTES:
            raise ValueError('Prize image must be 4 MB or smaller.')
        content_type = _detect_prize_image_type(payload)
        if content_type != match.group(1):
            raise ValueError('The prize image content does not match its file type.')
        image_id = hashlib.sha256(payload).hexdigest()
        image_dir = DB_PATH.parent / 'prize-images'
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / (image_id + '.bin')
        if not image_path.exists():
            temporary = image_dir / ('.' + image_id + '.' + secrets.token_hex(6) + '.tmp')
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, image_path)
            finally:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass
        filename = Path(str(data.get('filename') or 'prize-image')).name[:120]
        metadata = {'id': image_id, 'content_type': content_type, 'filename': filename,
                    'size': len(payload), 'updated_at': now()}
    old_image_id = None
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            old_metadata = get_setting('projector_prize_image', None, db)
            if isinstance(old_metadata, dict) and re.fullmatch(r'[0-9a-f]{64}', str(old_metadata.get('id') or '')):
                old_image_id = old_metadata['id']
            set_setting(db, 'projector_prize_image', metadata, actor)
            db.execute('''UPDATE lucky_draw_presentation SET sequence=sequence+1,
                          updated_at=?,updated_by=? WHERE singleton=1''', (now(), actor_name(actor)))
            audit(db, actor, 'projector.prize_image_' + ('cleared' if metadata is None else 'updated'),
                  'presentation', 1, {} if metadata is None else {
                      'filename': metadata['filename'], 'content_type': metadata['content_type'],
                      'size': metadata['size']})
            db.commit()
        except Exception:
            db.rollback()
            raise
    new_image_id = metadata.get('id') if metadata else None
    if old_image_id and old_image_id != new_image_id:
        try: (DB_PATH.parent / 'prize-images' / (old_image_id + '.bin')).unlink(missing_ok=True)
        except OSError: pass
    return metadata


def _unique_qr_token(db):
    for _ in range(12):
        token = secrets.token_urlsafe(24)
        if not db.execute('SELECT 1 FROM wristbands WHERE token=?', (token,)).fetchone() and not db.execute(
                'SELECT 1 FROM employees WHERE qr_token=?', (token,)).fetchone():
            return token
    raise RuntimeError('Could not allocate a unique wristband token.')


def active_wristband(db, employee_id):
    return db.execute('''SELECT * FROM wristbands WHERE employee_id=?
                         AND status IN ('not_issued','issued') ORDER BY id DESC LIMIT 1''',
                      (employee_id,)).fetchone()


def ensure_wristband(db, employee_id, actor='system', status='not_issued', token=None):
    current = active_wristband(db, employee_id)
    if current:
        return current, False
    token = token or _unique_qr_token(db)
    stamp = now()
    issued_at = stamp if status == 'issued' else None
    issued_by = actor_name(actor) if status == 'issued' else None
    cursor = db.execute('''INSERT INTO wristbands
        (employee_id,token,status,created_at,issued_at,issued_by)
        VALUES (?,?,?,?,?,?)''',
        (employee_id, token, status, stamp, issued_at, issued_by))
    wristband_id = cursor.lastrowid
    db.execute('UPDATE employees SET qr_token=? WHERE id=?', (token, employee_id))
    db.execute('''INSERT INTO wristband_distribution_history
        (wristband_id,employee_id,action,status,actor,occurred_at)
        VALUES (?,?,?,?,?,?)''',
        (wristband_id, employee_id, 'generated', status, actor_name(actor), stamp))
    audit(db, actor, 'wristband.generated', 'wristband', wristband_id,
          {'employee_id': employee_id, 'status': status})
    return db.execute('SELECT * FROM wristbands WHERE id=?', (wristband_id,)).fetchone(), True


def resolve_wristband(db, token):
    row = db.execute('''SELECT w.id AS wristband_id,w.employee_id AS wristband_employee_id,
                               w.token,w.status AS wristband_status,w.issued_at,w.issued_by,
                               e.*
                        FROM wristbands w JOIN employees e ON e.id=w.employee_id
                        WHERE w.token=?''', (token,)).fetchone()
    if row:
        return row
    # Compatibility for a database opened between legacy import and migration.
    employee = db.execute('SELECT * FROM employees WHERE qr_token=?', (token,)).fetchone()
    if not employee:
        return None
    wristband, _ = ensure_wristband(db, employee['id'], 'compatibility', 'issued', token)
    return db.execute('''SELECT w.id AS wristband_id,w.employee_id AS wristband_employee_id,
                               w.token,w.status AS wristband_status,w.issued_at,w.issued_by,e.*
                        FROM wristbands w JOIN employees e ON e.id=w.employee_id WHERE w.id=?''',
                      (wristband['id'],)).fetchone()


def normalize_token(code):
    if not isinstance(code, str):
        raise ValueError('QR code is required.')
    code = code.strip()
    if code.startswith(TOKEN_PREFIX):
        code = code[len(TOKEN_PREFIX):]
    if len(code) < 18 or len(code) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', code):
        raise ValueError('Not a valid party QR code.')
    return code


def employee_data(row):
    if not row:
        return None
    d = dict(row)
    result = {key: d.get(key) for key in
              ('id', 'employee_id', 'name', 'department', 'checked_in_at', 'verified_at')}
    try:
        result['extra'] = json.loads(d.get('extra_json') or '{}')
    except (json.JSONDecodeError, TypeError):
        result['extra'] = {}
    if 'wristband_status' in d:
        result['wristband_status'] = d.get('wristband_status')
        result['wristband_id'] = d.get('wristband_id')
    return result


def find_employee_by_external_id(db, value):
    """Find an ID exactly, then tolerate spreadsheet-trimmed leading zeroes.

    Numeric company badge QR codes may retain display zeroes that Excel removed
    from the imported master ID. The normalized fallback is accepted only when
    it identifies exactly one employee; exact text matches always win.
    """
    employee_id = str(value or '').strip()
    if not employee_id:
        return None
    exact = db.execute('SELECT * FROM employees WHERE employee_id=?',
                       (employee_id,)).fetchone()
    if exact or not employee_id.isdigit():
        return exact
    normalized = employee_id.lstrip('0') or '0'
    matches = db.execute('''SELECT * FROM employees
                            WHERE employee_id NOT GLOB '*[^0-9]*'
                              AND COALESCE(NULLIF(ltrim(employee_id,'0'),''),'0')=?
                            LIMIT 2''', (normalized,)).fetchall()
    return matches[0] if len(matches) == 1 else None


def lookup_by_token(db, token):
    resolved = resolve_wristband(db, token)
    if not resolved or resolved['wristband_status'] not in WRISTBAND_ACTIVE_STATUSES:
        return None
    return resolved


def scan_action(token, action, actor, wristband_reason=None, wristband_detail=''):
    """Serialized write: prevents double check-in at concurrent stations."""
    if action not in ('checkin', 'verify'):
        raise ValueError('Invalid scan action.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        employee = resolve_wristband(db, token)
        if not employee:
            db.rollback()
            return None, 'not_found'
        if employee['wristband_status'] not in WRISTBAND_ACTIVE_STATUSES:
            state = employee['wristband_status']
            person = employee_data(employee)
            db.rollback()
            return person, state
        if action == 'verify' and not employee['checked_in_at']:
            db.rollback()
            return employee_data(employee), 'checkin_required'
        column = 'checked_in_at' if action == 'checkin' else 'verified_at'
        if employee[column]:
            audit(db, actor, 'attendance.duplicate_scan' if action == 'checkin' else 'verification.duplicate_scan',
                  'employee', employee['id'], {'wristband_id': employee['wristband_id']})
            db.commit()
            return employee_data(employee), 'already_done'
        stamp = now()
        if action == 'checkin':
            existing = db.execute('SELECT checked_in_at FROM event_checkins WHERE employee_id=?',
                                  (employee['id'],)).fetchone()
            if existing:
                db.execute('UPDATE employees SET checked_in_at=COALESCE(checked_in_at,?) WHERE id=?',
                           (existing['checked_in_at'], employee['id']))
                audit(db, actor, 'attendance.duplicate_scan', 'employee', employee['id'],
                      {'wristband_id': employee['wristband_id']})
                db.commit()
                refreshed = db.execute('SELECT * FROM employees WHERE id=?', (employee['id'],)).fetchone()
                return employee_data(refreshed), 'already_done'
            db.execute('''INSERT INTO event_checkins
                (employee_id,wristband_id,checked_in_at,staff_username)
                VALUES (?,?,?,?)''',
                (employee['id'], employee['wristband_id'], stamp, actor_name(actor)))
            if wristband_reason:
                db.execute('''INSERT INTO checkin_wristband_exceptions
                    (employee_id,reason,detail,recorded_by,recorded_at) VALUES (?,?,?,?,?)''',
                    (employee['id'], wristband_reason, wristband_detail, actor_name(actor), stamp))
        else:
            db.execute('''INSERT INTO identity_verification_history
                (employee_id,wristband_id,result,verified_at,staff_username)
                VALUES (?,?,'verified',?,?)''',
                (employee['id'], employee['wristband_id'], stamp, actor_name(actor)))
        db.execute('UPDATE employees SET ' + column + '=? WHERE id=?', (stamp, employee['id']))
        db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (?,?,?,?)',
                   (employee['id'], action, actor_name(actor), stamp))
        audit(db, actor, 'attendance.checked_in' if action == 'checkin' else 'verification.verified',
              'employee', employee['id'], {'wristband_id': employee['wristband_id'],
                                           'wristband_reason': wristband_reason, 'wristband_detail': wristband_detail})
        result = db.execute('SELECT * FROM employees WHERE id=?', (employee['id'],)).fetchone()
        db.commit()
        return employee_data(result), 'done'


def mark_checked_in_wristband_lost(employee_id, actor):
    """Record a lost wristband after check-in without adding another check-in."""
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        employee = find_employee_by_external_id(db, employee_id)
        if not employee:
            db.rollback()
            return None, 'not_found'
        checked_in = employee['checked_in_at'] or db.execute(
            'SELECT checked_in_at FROM event_checkins WHERE employee_id=?', (employee['id'],)
        ).fetchone()
        if not checked_in:
            db.rollback()
            return employee_data(employee), 'checkin_required'
        existing = db.execute('SELECT reason FROM checkin_wristband_exceptions WHERE employee_id=?',
                              (employee['id'],)).fetchone()
        if existing and existing['reason'] == 'lost':
            db.rollback()
            return employee_data(employee), 'already_lost'
        stamp = now()
        db.execute('''INSERT INTO checkin_wristband_exceptions
            (employee_id,reason,detail,recorded_by,recorded_at) VALUES (?,'lost','',?,?)
            ON CONFLICT(employee_id) DO UPDATE SET reason='lost', detail='',
                recorded_by=excluded.recorded_by, recorded_at=excluded.recorded_at''',
            (employee['id'], actor_name(actor), stamp))
        audit(db, actor, 'attendance.wristband_lost', 'employee', employee['id'],
              {'previous_reason': existing['reason'] if existing else None})
        db.commit()
        person = employee_data(employee)
        person['checkin_wristband_reason'] = 'lost'
        return person, 'updated'


def remove_attendance(payload, actor):
    """Archive and remove active attendance in one transaction."""
    scope = str(payload.get('scope') or '')
    reason = str(payload.get('reason') or '').strip()
    if scope not in ('one', 'all') or not reason or len(reason) > 500:
        raise ValueError('Choose a scope and provide a reason (up to 500 characters).')
    expected_confirmation = 'RESET ALL' if scope == 'all' else 'REMOVE'
    if payload.get('confirmation') != expected_confirmation:
        raise ValueError('Type the requested confirmation exactly.')
    try:
        expected_count = int(payload.get('expected_count'))
    except (ValueError, TypeError):
        raise ValueError('Refresh the attendance count before continuing.') from None
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            employee = None
            if scope == 'one':
                employee = find_employee_by_external_id(db, payload.get('employee_id'))
                if not employee:
                    raise ValueError('Employee not found.')
            actual_count = db.execute('''SELECT COUNT(*) FROM employees e WHERE e.checked_in_at IS NOT NULL
                OR EXISTS (SELECT 1 FROM event_checkins c WHERE c.employee_id=e.id)''').fetchone()[0]
            if actual_count != expected_count:
                raise ValueError('Attendance changed. Refresh the count and review it again.')
            rows = db.execute('''SELECT e.id,e.employee_id,COALESCE(c.checked_in_at,e.checked_in_at) AS stamp,
                       c.wristband_id,c.staff_username,c.station,c.legacy_key,
                       (SELECT x.reason FROM checkin_wristband_exceptions x WHERE x.employee_id=e.id) AS wristband_reason,
                       (SELECT x.detail FROM checkin_wristband_exceptions x WHERE x.employee_id=e.id) AS wristband_detail
                FROM employees e LEFT JOIN event_checkins c ON c.employee_id=e.id
                WHERE (e.checked_in_at IS NOT NULL OR c.employee_id IS NOT NULL)''' +
                (' AND e.id=?' if employee else ''), (employee['id'],) if employee else ()).fetchall()
            if not rows:
                raise ValueError('No matching check-in records to remove.')
            batch_id, removed_at = secrets.token_urlsafe(16), now()
            for row in rows:
                details = {key: row[key] for key in ('wristband_id', 'staff_username', 'station', 'legacy_key', 'wristband_reason', 'wristband_detail')}
                db.execute('''INSERT INTO attendance_removals
                    (batch_id,employee_id,checked_in_at,checkin_json,removed_by,reason,removed_at)
                    VALUES (?,?,?,?,?,?,?)''', (batch_id, row['id'], row['stamp'],
                    json.dumps(details, ensure_ascii=False), actor_name(actor), reason, removed_at))
                db.execute('DELETE FROM event_checkins WHERE employee_id=?', (row['id'],))
                db.execute('DELETE FROM checkin_wristband_exceptions WHERE employee_id=?', (row['id'],))
                db.execute('UPDATE employees SET checked_in_at=NULL WHERE id=?', (row['id'],))
            audit(db, actor, 'attendance.removed', 'attendance', batch_id,
                  {'scope': scope, 'count': len(rows), 'reason': reason})
            db.commit()
            return {'removed': len(rows), 'batch_id': batch_id}
        except Exception:
            db.rollback()
            raise


def remove_draw_history(payload, actor):
    reason = str(payload.get('reason') or '').strip()
    if not reason or len(reason) > 500:
        raise ValueError('Provide a reason (up to 500 characters).')
    if payload.get('confirmation') != 'RESET DRAW':
        raise ValueError('Type RESET DRAW to confirm.')
    try:
        expected = int(payload.get('expected_count'))
    except (ValueError, TypeError):
        raise ValueError('Refresh the winner count before continuing.') from None
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            presentation = db.execute('SELECT * FROM lucky_draw_presentation WHERE singleton=1').fetchone()
            if presentation and presentation['phase'] in ('preparing', 'spinning', 'winner_selected'):
                raise ValueError('A draw is in progress. Wait until it finishes.')
            winners = [dict(row) for row in db.execute('SELECT * FROM lucky_draw_winners ORDER BY id')]
            if len(winners) != expected:
                raise ValueError('Winner history changed. Refresh and review the count again.')
            if not winners:
                raise ValueError('There is no winner history to remove.')
            snapshot = {'winners': winners,
                        'rounds': [dict(row) for row in db.execute('SELECT * FROM lucky_draw_rounds ORDER BY id')],
                        'legacy_winners': [dict(row) for row in db.execute('SELECT * FROM winners ORDER BY id')]}
            batch_id, stamp = secrets.token_urlsafe(16), now()
            db.execute('''INSERT INTO draw_history_removals(batch_id,history_json,removed_by,reason,removed_at)
                          VALUES (?,?,?,?,?)''',
                       (batch_id, json.dumps(snapshot, ensure_ascii=False), actor_name(actor), reason, stamp))
            db.execute('''UPDATE lucky_draw_presentation SET sequence=sequence+1,phase='waiting',winner_id=NULL,
                          prize_name=NULL,round_name=NULL,winner_json=NULL,spin_started_at=NULL,reveal_at=NULL,
                          updated_at=?,updated_by=? WHERE singleton=1''', (stamp, actor_name(actor)))
            db.execute('DELETE FROM lucky_draw_winners')
            db.execute('DELETE FROM lucky_draw_rounds')
            db.execute('DELETE FROM winners')
            audit(db, actor, 'draw.history_removed', 'draw', batch_id, {'count':len(winners), 'reason':reason})
            db.commit()
            return {'removed':len(winners), 'batch_id':batch_id}
        except Exception:
            db.rollback()
            raise


def _eligible_sql(db, exclude_employee_id=None):
    clauses = [
        "(e.checked_in_at IS NOT NULL OR EXISTS (SELECT 1 FROM event_checkins c WHERE c.employee_id=e.id))",
        "NOT EXISTS (SELECT 1 FROM checkin_wristband_exceptions x WHERE x.employee_id=e.id AND x.reason IN ('lost','other'))"
    ]
    params = []
    if bool(get_setting('lucky_participants_configured', False, db)):
        clauses.append('EXISTS (SELECT 1 FROM lucky_draw_participants p WHERE p.employee_id=e.id AND p.active=1)')
    if bool(get_setting('require_verification_for_draw', False, db)):
        clauses.append("EXISTS (SELECT 1 FROM identity_verification_history v WHERE v.employee_id=e.id AND v.result='verified')")
    if not bool(get_setting('allow_multiple_wins', False, db)):
        clauses.append("NOT EXISTS (SELECT 1 FROM lucky_draw_winners w WHERE w.employee_id=e.id AND w.status='active')")
    if exclude_employee_id is not None:
        clauses.append('e.id<>?')
        params.append(exclude_employee_id)
    return ' AND '.join(clauses), params


def set_projector_state(db, phase, actor, winner=None, prize=None, round_name=None):
    allowed = {'waiting', 'preparing', 'spinning', 'winner_selected',
               'winner_announced', 'ready_for_next'}
    if phase not in allowed:
        raise ValueError('Invalid projector state.')
    stamp = now()
    winner_json = spin_started_at = reveal_at = winner_id = None
    if winner:
        winner_id = winner.get('winner_id')
        prize = winner.get('prize') or prize
        round_name = winner.get('round_name') or round_name
        winner_json = json.dumps({
            'name': winner.get('name'),
            'employee_id': winner.get('employee_id'),
            'department': winner.get('department'),
            'prize': prize,
            'round_name': round_name,
        }, ensure_ascii=False, separators=(',', ':'))
        spin_started_at = stamp
        reveal_at = (datetime.now(timezone.utc) + timedelta(seconds=PROJECTOR_SPIN_SECONDS)).isoformat(timespec='milliseconds')
    db.execute('''UPDATE lucky_draw_presentation SET sequence=sequence+1,phase=?,winner_id=?,
                  prize_name=?,round_name=?,winner_json=?,spin_started_at=?,reveal_at=?,
                  updated_at=?,updated_by=? WHERE singleton=1''',
               (phase, winner_id, prize, round_name, winner_json, spin_started_at, reveal_at,
                stamp, actor_name(actor)))


def get_projector_state(db=None):
    if db is None:
        with db_connection() as connection:
            return get_projector_state(connection)
    row = db.execute('SELECT * FROM lucky_draw_presentation WHERE singleton=1').fetchone()
    if not row:
        return {'sequence': 0, 'phase': 'waiting'}
    result = dict(row)
    phase = result['phase']
    if phase == 'spinning' and result.get('reveal_at'):
        try:
            remaining = (datetime.fromisoformat(result['reveal_at']) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                phase = 'winner_announced'
            elif remaining <= .8:
                phase = 'winner_selected'
        except (TypeError, ValueError):
            pass
    winner = None
    if phase in ('winner_selected', 'winner_announced') and result.get('winner_json'):
        try:
            winner = json.loads(result['winner_json'])
        except (json.JSONDecodeError, TypeError):
            winner = None
    if winner and not bool(get_setting('projector_show_employee_id', True, db)):
        winner.pop('employee_id', None)
    predicate, params = _eligible_sql(db)
    eligible = db.execute('SELECT COUNT(*) FROM employees e WHERE ' + predicate, params).fetchone()[0]
    prize_image = get_setting('projector_prize_image', None, db)
    prize_image_valid = (isinstance(prize_image, dict) and
                         bool(re.fullmatch(r'[0-9a-f]{64}', str(prize_image.get('id') or ''))))
    prizes = [dict(row) for row in db.execute('''SELECT p.id,p.name,p.quantity,
        EXISTS(SELECT 1 FROM reward_images i WHERE i.prize_id=p.id) AS has_image
        FROM prizes p WHERE p.active=1 ORDER BY p.id''')]
    winners_by_prize = {item['id']: [] for item in prizes}
    for entry in db.execute('''SELECT w.id,w.prize_id,e.name,e.employee_id,e.department FROM lucky_draw_winners w
                              JOIN employees e ON e.id=w.employee_id
                              WHERE w.status='active' ORDER BY w.id'''):
        if entry['prize_id'] not in winners_by_prize:
            continue
        if phase != 'winner_announced' and entry['id'] == result.get('winner_id'):
            continue
        person = {'name': entry['name'], 'department': entry['department']}
        if bool(get_setting('projector_show_employee_id', True, db)):
            person['employee_id'] = entry['employee_id']
        winners_by_prize[entry['prize_id']].append(person)
    for item in prizes:
        item['winners'] = winners_by_prize[item['id']]
    return {
        'sequence': result['sequence'], 'phase': phase, 'prize': result.get('prize_name'),
        'round_name': result.get('round_name'), 'winner': winner,
        'spin_started_at': result.get('spin_started_at'), 'reveal_at': result.get('reveal_at'),
        'updated_at': result.get('updated_at'), 'eligible': eligible,
        'event_name': get_setting('event_name', 'Company Party', db),
        'title': get_setting('projector_title', 'Lucky Draw', db),
        'theme': get_setting('projector_theme', 'gold', db),
        'show_employee_id': bool(get_setting('projector_show_employee_id', True, db)),
        'prize_image_url': (('/api/rewards/image?id=' + str(next((p['id'] for p in prizes if p['name'] == result.get('prize_name') and p['has_image']), '')))
                            if any(p['name'] == result.get('prize_name') and p['has_image'] for p in prizes)
                            else (('/api/projector/prize-image?v=' + prize_image['id']) if prize_image_valid else None)),
        'prize_image_name': prize_image.get('filename') if prize_image_valid else None,
        'prizes': prizes,
        'server_time': now(),
    }


def _ensure_prize(db, name, actor, quantity=MAX_EMPLOYEES):
    row = db.execute('SELECT * FROM prizes WHERE name=? COLLATE NOCASE', (name,)).fetchone()
    if row:
        return row
    stamp = now()
    cursor = db.execute('''INSERT INTO prizes(name,quantity,active,created_at,updated_at)
                           VALUES (?,?,1,?,?)''', (name, quantity, stamp, stamp))
    audit(db, actor, 'prize.created', 'prize', cursor.lastrowid,
          {'name': name, 'quantity': quantity, 'source': 'draw'})
    return db.execute('SELECT * FROM prizes WHERE id=?', (cursor.lastrowid,)).fetchone()


def _draw_with_db(db, prize, actor, prize_id=None, enforce_quantity=False,
                  exclude_employee_id=None, reroll_of_winner_id=None, round_label=None):
    if prize_id is not None:
        prize_row = db.execute('SELECT * FROM prizes WHERE id=? AND active=1', (prize_id,)).fetchone()
        if not prize_row:
            raise ValueError('Prize not found or inactive.')
        prize = prize_row['name']
    else:
        if not isinstance(prize, str) or not 1 <= len(prize.strip()) <= 100:
            raise ValueError('Enter a prize name (1-100 characters).')
        prize = prize.strip()
        prize_row = _ensure_prize(db, prize, actor)
        prize_id = prize_row['id']
    if enforce_quantity:
        drawn = db.execute("SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'",
                           (prize_id,)).fetchone()[0]
        if drawn >= prize_row['quantity']:
            return None, 'prize_exhausted'
    predicate, params = _eligible_sql(db, exclude_employee_id)
    count = db.execute('SELECT COUNT(*) FROM employees e WHERE ' + predicate, params).fetchone()[0]
    if count == 0:
        return None, 'no_eligible'
    employee = db.execute('SELECT e.* FROM employees e WHERE ' + predicate +
                          ' ORDER BY e.id LIMIT 1 OFFSET ?', params + [secrets.randbelow(count)]).fetchone()
    stamp = now()
    round_number = db.execute('SELECT COALESCE(MAX(round_number),0)+1 FROM lucky_draw_rounds WHERE prize_id=?',
                              (prize_id,)).fetchone()[0]
    if round_label is not None:
        round_label = str(round_label).strip()
        if len(round_label) > 120:
            raise ValueError('Drawing round name must be at most 120 characters.')
    round_label = round_label or (prize + ' - Round ' + str(round_number))
    round_cursor = db.execute('''INSERT INTO lucky_draw_rounds
        (prize_id,round_number,label,status,created_at,drawn_at,drawn_by,reroll_of_winner_id)
        VALUES (?,?,?,'completed',?,?,?,?)''',
        (prize_id, round_number, round_label, stamp, stamp, actor_name(actor), reroll_of_winner_id))
    winner_cursor = db.execute('''INSERT INTO lucky_draw_winners
        (employee_id,round_id,prize_id,prize_name,won_at,won_by,status)
        VALUES (?,?,?,?,?,?,'active')''',
        (employee['id'], round_cursor.lastrowid, prize_id, prize, stamp, actor_name(actor)))
    legacy_cursor = db.execute('''INSERT OR IGNORE INTO winners(employee_id,prize,drawn_at,drawn_by,status)
                                  VALUES (?,?,?,?,'active')''',
                               (employee['id'], prize, stamp, actor_name(actor)))
    if legacy_cursor.rowcount:
        db.execute('UPDATE lucky_draw_winners SET legacy_winner_id=? WHERE id=?',
                   (legacy_cursor.lastrowid, winner_cursor.lastrowid))
    db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (?,?,?,?)',
               (employee['id'], 'winner: ' + prize, actor_name(actor), stamp))
    audit(db, actor, 'draw.winner_selected', 'winner', winner_cursor.lastrowid,
          {'employee_id': employee['employee_id'], 'prize_id': prize_id,
           'round_id': round_cursor.lastrowid, 'reroll_of_winner_id': reroll_of_winner_id})
    result = employee_data(employee)
    result.update({'winner_id': winner_cursor.lastrowid, 'round_id': round_cursor.lastrowid,
                   'round_number': round_number, 'round_name': round_label,
                   'prize_id': prize_id, 'prize': prize, 'drawn_at': stamp})
    set_projector_state(db, 'spinning', actor, result, prize, round_label)
    return result, 'drawn'


def draw_winner(prize, actor, round_label=None):
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            winner, _ = _draw_with_db(db, prize, actor, round_label=round_label)
            if winner is None:
                db.rollback()
                return None
            db.commit()
            return winner
        except Exception:
            db.rollback()
            raise


def draw_configured_prize(prize_id, actor, round_label=None, prevent_overlap=False):
    """Draw one configured prize unit atomically and enforce its quantity."""
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if prevent_overlap and get_projector_state(db)['phase'] in ('spinning', 'winner_selected'):
                db.rollback()
                return None, 'draw_in_progress'
            winner, state = _draw_with_db(
                db, None, actor, prize_id=prize_id, enforce_quantity=True,
                round_label=round_label)
            if winner is None:
                db.rollback()
                return None, state
            db.commit()
            return winner, state
        except Exception:
            db.rollback()
            raise


def get_stats():
    with db_connection() as db:
        e = db.execute('''
            SELECT COUNT(*) AS total,
                COALESCE(SUM(checked_in_at IS NOT NULL OR EXISTS
                    (SELECT 1 FROM event_checkins c WHERE c.employee_id=employees.id)),0) AS checked_in
            FROM employees
        ''').fetchone()
        verified = db.execute("SELECT COUNT(DISTINCT employee_id) FROM identity_verification_history WHERE result='verified'").fetchone()[0]
        verification_failures = db.execute("SELECT COUNT(*) FROM identity_verification_history WHERE result<>'verified'").fetchone()[0]
        winners = db.execute("SELECT COUNT(*) FROM lucky_draw_winners WHERE status='active'").fetchone()[0]
        issued = db.execute("SELECT COUNT(*) FROM wristbands WHERE status='issued'").fetchone()[0]
        not_issued = db.execute("SELECT COUNT(*) FROM wristbands WHERE status='not_issued'").fetchone()[0]
        participants = db.execute('SELECT COUNT(*) FROM lucky_draw_participants WHERE active=1').fetchone()[0]
        recent = [dict(x) for x in db.execute('''
            SELECT l.action,l.occurred_at,e.name,e.employee_id,l.actor
            FROM event_log l LEFT JOIN employees e ON e.id=l.employee_id
            ORDER BY l.id DESC LIMIT 8
        ''')]
        recent_winners = [dict(x) for x in db.execute('''
            SELECT e.name,e.employee_id,e.department,w.prize_name AS prize,w.won_at AS drawn_at
            FROM lucky_draw_winners w JOIN employees e ON e.id=w.employee_id
            WHERE w.status='active'
            ORDER BY w.id DESC LIMIT 12
        ''')]
        predicate, params = _eligible_sql(db)
        eligible = db.execute('SELECT COUNT(*) FROM employees e WHERE ' + predicate, params).fetchone()[0]
        total = e['total']
        checked = e['checked_in']
        return {
            'total': total, 'total_employees': total,
            'wristbands_issued': issued, 'wristbands_not_issued': not_issued,
            'checked_in': checked, 'not_checked_in': max(0, total - checked),
            'attendance_percentage': round((checked * 100 / total), 2) if total else 0,
            'verified': verified, 'distinct_verified': verified,
            'verification_failures': verification_failures,
            'lucky_participants': participants,
            'winners': winners, 'total_winners': winners,
            'eligible': eligible, 'eligible_participants': eligible,
            'recent': recent, 'recent_winners': recent_winners,
        }


def normalized_column(col):
    return re.sub(r'[^a-z0-9]', '', str(col or '').strip().lower())


def get_import_rows(filename, data):
    filename = filename.lower().strip()
    if filename.endswith('.csv'):
        decoded = None
        for encoding in ('utf-8-sig', 'cp874'):
            try:
                decoded = data.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError('Save your CSV as UTF-8 or use an .xlsx file.')
        rows = csv.reader(io.StringIO(decoded, newline=''))
        try:
            headers = next(rows)
        except StopIteration:
            raise ValueError('The file is empty.') from None
    elif filename.endswith('.xlsx'):
        try:
            import openpyxl
        except ImportError:
            raise ValueError('Excel import needs: pip install openpyxl') from None
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = workbook.active
            iterable = ws.iter_rows(values_only=True)
            headers = next(iterable)
            rows = iterable
        except (ValueError, KeyError, zipfile.BadZipFile, StopIteration) as exc:
            raise ValueError('Could not read the Excel spreadsheet.') from exc
    else:
        raise ValueError('Only .csv and .xlsx files are supported.')
    header_idx = {normalized_column(v): i for i, v in enumerate(headers)}
    id_aliases = ('employeeid', 'empid', 'staffid', 'personnelno', 'id')
    name_aliases = ('name', 'fullname', 'employeename', 'staffname')
    dept_aliases = ('department', 'dept', 'section', 'division')

    def find_alias(options, required=False):
        for name in options:
            if name in header_idx:
                return header_idx[name]
        if required:
            raise ValueError('Missing required column. Use employee_id and name headers.')
        return None

    id_idx = find_alias(id_aliases, True)
    name_idx = find_alias(name_aliases, True)
    dept_idx = find_alias(dept_aliases)
    result = []
    seen = set()
    for line, record in enumerate(rows, start=2):
        if line > MAX_EMPLOYEES + 2:
            raise ValueError('File exceeds the 5,000 employee safety limit.')
        if not record or all(v is None or str(v).strip() == '' for v in record):
            continue
        def value(idx):
            if idx is None or idx >= len(record) or record[idx] is None:
                return ''
            item = record[idx]
            if isinstance(item, float) and item.is_integer():
                item = int(item)
            return str(item).strip()
        eid, name, dept = normalize_employee_id(value(id_idx)), value(name_idx), value(dept_idx)
        if not eid or not name:
            raise ValueError('Row ' + str(line) + ': employee_id and name are required.')
        if len(eid) > 60 or len(name) > 160 or len(dept) > 160:
            raise ValueError('Row ' + str(line) + ': a value is too long.')
        if eid in seen:
            raise ValueError('Row ' + str(line) + ': duplicate employee ID ' + eid)
        seen.add(eid)
        result.append((eid, name, dept))
    if not result:
        raise ValueError('The spreadsheet does not contain employee data.')
    return result


def is_manager_position(position):
    return bool(re.search(r'\bmanager\b|\bmanaging\s+director\b', str(position or ''), re.IGNORECASE))


def _manual_employee_fields(data):
    if not isinstance(data, dict):
        raise ValueError('Enter employee details.')
    fields = {}
    for key, label, maximum in (('employee_id', 'Employee ID', 50), ('name', 'Name', 200),
                                ('department', 'Department', 200), ('position', 'Position', 200)):
        value = data.get(key, '')
        if not isinstance(value, str) or re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError(label + ' must be text without control characters.')
        value = value.strip()
        if len(value) > maximum or (not value and key != 'department'):
            raise ValueError(label + f' must be 1-{maximum} characters.')
        fields[key] = value
    if not re.fullmatch(r'[A-Za-z0-9_-]+', fields['employee_id']):
        raise ValueError('Employee ID can use letters, numbers, - and _.')
    allowed = data.get('eligible_for_draw', True)
    if not isinstance(allowed, bool):
        raise ValueError('Choose can win or cannot win.')
    return {**fields, 'employee_id': normalize_employee_id(fields['employee_id']),
            'eligible_for_draw': allowed and not is_manager_position(fields['position'])}


def _prepare_manual_participants(db, actor):
    """Preserve legacy employees when switching from all staff to an explicit list."""
    manual_ids = get_setting('manual_lucky_participant_ids', [], db)
    manual_ids = list(manual_ids) if isinstance(manual_ids, list) else []
    if not get_setting('lucky_participants_configured', False, db):
        stamp = now()
        manual_ids = []
        for employee in db.execute('SELECT * FROM employees').fetchall():
            if is_manager_position(employee_data(employee)['extra'].get('position')):
                db.execute('''UPDATE lucky_draw_participants SET active=0,removed_at=?
                    WHERE employee_id=? AND active=1''', (stamp, employee['id']))
                continue
            db.execute('''INSERT INTO lucky_draw_participants (employee_id,active,added_at)
                VALUES (?,1,?) ON CONFLICT(employee_id) DO UPDATE SET active=1,removed_at=NULL''',
                       (employee['id'], stamp))
            manual_ids = [*manual_ids, employee['employee_id']]
    set_setting(db, 'lucky_participants_configured', True, actor)
    return list(dict.fromkeys(manual_ids))


def add_employee(data, actor):
    fields = _manual_employee_fields(data)
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if find_employee_by_external_id(db, fields['employee_id']):
                raise ValueError('Employee ID already exists.')
            if db.execute('SELECT COUNT(*) FROM employees').fetchone()[0] >= MAX_EMPLOYEES:
                raise ValueError('Employee limit reached (5,000).')
            manual_ids = _prepare_manual_participants(db, actor)
            token = _unique_qr_token(db)
            cursor = db.execute('''INSERT INTO employees
                (employee_id,name,department,qr_token,extra_json) VALUES (?,?,?,?,?)''',
                (fields['employee_id'], fields['name'], fields['department'], token,
                 json.dumps({'position': fields['position']}, ensure_ascii=False)))
            internal_id = cursor.lastrowid
            ensure_wristband(db, internal_id, actor, 'not_issued', token)
            if fields['eligible_for_draw']:
                db.execute('''INSERT INTO lucky_draw_participants (employee_id,active,added_at)
                    VALUES (?,1,?)''', (internal_id, now()))
                manual_ids = [*manual_ids, fields['employee_id']]
            set_setting(db, 'manual_lucky_participant_ids', list(dict.fromkeys(manual_ids)), actor)
            audit(db, actor, 'employee.created', 'employee', internal_id,
                  {'employee_id': fields['employee_id'], 'eligible_for_draw': fields['eligible_for_draw']})
            person = employee_data(db.execute('SELECT * FROM employees WHERE id=?', (internal_id,)).fetchone())
            db.commit()
            return {'person': person, 'state': 'added', 'eligible_for_draw': fields['eligible_for_draw']}
        except Exception:
            db.rollback()
            raise


def import_employees(rows, actor):
    rows = [(normalize_employee_id(eid), name, dept) for eid, name, dept in rows]
    added = updated = 0
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing_count = db.execute('SELECT COUNT(*) FROM employees').fetchone()[0]
        existing_ids = {r['employee_id'] for r in db.execute('SELECT employee_id FROM employees')}
        incoming_ids = {row[0] for row in rows}
        if existing_count + len(incoming_ids - existing_ids) > MAX_EMPLOYEES:
            db.rollback()
            raise ValueError('Import would exceed the 5,000 employee limit.')
        for eid, name, dept in rows:
            if eid in existing_ids:
                db.execute('UPDATE employees SET name=?,department=? WHERE employee_id=?', (name, dept, eid))
                updated += 1
            else:
                token = _unique_qr_token(db)
                db.execute('INSERT INTO employees(employee_id,name,department,qr_token) VALUES (?,?,?,?)',
                           (eid, name, dept, token))
                employee_id = db.execute('SELECT id FROM employees WHERE employee_id=?', (eid,)).fetchone()['id']
                ensure_wristband(db, employee_id, actor, 'not_issued', token)
                added += 1
        db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (NULL,?,?,?)',
                   (f'import: {added} added, {updated} updated', actor_name(actor), now()))
        audit(db, actor, 'import.legacy_committed', 'import', None,
              {'added': added, 'updated': updated, 'count': len(rows)})
        db.commit()
    return {'added': added, 'updated': updated, 'count': len(rows)}


def seed_employees_from_json(path=None):
    """Load the private generated employee JSON once for a fresh event database."""
    source = Path(path or EMPLOYEE_JSON_PATH)
    if not source.exists():
        return {'added': 0, 'source': str(source), 'missing': True}
    payload = json.loads(source.read_text(encoding='utf-8'))
    employees = payload.get('employees')
    if not isinstance(employees, list) or not employees:
        raise ValueError('Employee JSON does not contain any employees.')
    if len(employees) > MAX_EMPLOYEES:
        raise ValueError('Employee JSON exceeds the 5,000 employee limit.')
    validated_ids = set()
    eligible_ids = []
    for index, item in enumerate(employees, 1):
        employee_id = normalize_employee_id(item.get('employee_id'))
        name = str(item.get('name') or '').strip()
        if not employee_id or not name:
            raise ValueError(f'Employee JSON row {index} is missing an ID or name.')
        if employee_id in validated_ids:
            raise ValueError('Duplicate employee ID in JSON: ' + employee_id)
        validated_ids.add(employee_id)
        if item.get('eligible_for_draw', False):
            eligible_ids.append(employee_id)
    with db_connection() as db:
        if db.execute('SELECT COUNT(*) FROM employees').fetchone()[0]:
            db.execute('BEGIN IMMEDIATE')
            stamp = now()
            try:
                manual_ids = get_setting('manual_lucky_participant_ids', [], db)
                if not isinstance(manual_ids, list):
                    manual_ids = []
                participant_ids = list(dict.fromkeys(eligible_ids + [
                    str(value).strip() for value in manual_ids if str(value).strip()
                ]))
                db.execute('UPDATE lucky_draw_participants SET active=0,removed_at=? WHERE active=1',
                           (stamp,))
                matched = 0
                for item in employees:
                    employee_id = normalize_employee_id(item.get('employee_id'))
                    current = db.execute('SELECT id,extra_json FROM employees WHERE employee_id=?',
                                         (employee_id,)).fetchone()
                    if not current:
                        continue
                    try:
                        extra = json.loads(current['extra_json'] or '{}')
                    except (json.JSONDecodeError, TypeError):
                        extra = {}
                    extra.update({
                        'first_name': str(item.get('first_name') or '').strip(),
                        'last_name': str(item.get('last_name') or '').strip(),
                        'position': str(item.get('position') or '').strip(),
                        'route': str(item.get('route') or '').strip(),
                    })
                    db.execute('UPDATE employees SET extra_json=? WHERE id=?',
                               (json.dumps(extra, ensure_ascii=False), current['id']))
                for employee_id in participant_ids:
                    employee = db.execute('SELECT id FROM employees WHERE employee_id=?',
                                          (employee_id,)).fetchone()
                    if not employee:
                        continue
                    db.execute('''INSERT INTO lucky_draw_participants
                        (employee_id,active,added_at,removed_at) VALUES (?,1,?,NULL)
                        ON CONFLICT(employee_id) DO UPDATE SET active=1,added_at=excluded.added_at,
                            removed_at=NULL''', (employee['id'], stamp))
                    matched += 1
                set_setting(db, 'lucky_participants_configured', True, 'json-sync')
                audit(db, 'json-sync', 'import.json_participants_synced', 'employee_source', None,
                      {'source_file': payload.get('source_file'), 'participants': matched,
                       'unmatched': len(participant_ids) - matched,
                       'staff_overrides': len(manual_ids)})
                db.commit()
            except Exception:
                db.rollback()
                raise
            return {'added': 0, 'participants': matched, 'source': str(source),
                    'already_loaded': True}
        db.execute('BEGIN IMMEDIATE')
        seen_ids, seen_tokens = set(), set()
        stamp = now()
        try:
            for index, item in enumerate(employees, 1):
                employee_id = normalize_employee_id(item.get('employee_id'))
                name = str(item.get('name') or '').strip()
                department = str(item.get('department') or '').strip()
                if not employee_id or not name:
                    raise ValueError(f'Employee JSON row {index} is missing an ID or name.')
                if employee_id in seen_ids:
                    raise ValueError('Duplicate employee ID in JSON: ' + employee_id)
                token = str(item.get('qr_token') or '').strip()
                try:
                    token = normalize_token(token)
                except ValueError:
                    token = _unique_qr_token(db)
                if token in seen_tokens:
                    token = _unique_qr_token(db)
                seen_ids.add(employee_id)
                seen_tokens.add(token)
                extra = {'first_name': str(item.get('first_name') or '').strip(),
                         'last_name': str(item.get('last_name') or '').strip(),
                         'position': str(item.get('position') or '').strip(),
                         'route': str(item.get('route') or '').strip()}
                cursor = db.execute('''INSERT INTO employees
                    (employee_id,name,department,qr_token,extra_json) VALUES (?,?,?,?,?)''',
                    (employee_id, name, department, token,
                     json.dumps(extra, ensure_ascii=False)))
                ensure_wristband(db, cursor.lastrowid, 'json-seed', 'not_issued', token)
                if item.get('eligible_for_draw', False):
                    db.execute('''INSERT INTO lucky_draw_participants
                        (employee_id,active,added_at) VALUES (?,1,?)''', (cursor.lastrowid, stamp))
            set_setting(db, 'lucky_participants_configured', True, 'json-seed')
            audit(db, 'json-seed', 'import.json_seeded', 'employee_source', None,
                  {'source_file': payload.get('source_file'), 'count': len(employees)})
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'added': len(employees), 'source': str(source)}


def _json_cell(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    return str(value)


def parse_import_workbook(filename, data):
    """Parse every worksheet without assigning business meaning to row position."""
    lower = str(filename or '').lower().strip()
    sheets = []
    total_rows = 0
    if lower.endswith('.csv'):
        decoded = None
        for encoding in ('utf-8-sig', 'cp874'):
            try:
                decoded = data.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError('Save your CSV as UTF-8 or use an .xlsx file.')
        all_rows = list(csv.reader(io.StringIO(decoded, newline='')))
        if not all_rows:
            raise ValueError('The file is empty.')
        source_sheets = [('DATA', all_rows[0], all_rows[1:])]
    elif lower.endswith('.xlsx'):
        try:
            import openpyxl
        except ImportError:
            raise ValueError('Excel import needs: pip install openpyxl') from None
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            source_sheets = []
            for worksheet in workbook.worksheets:
                iterator = worksheet.iter_rows(values_only=True)
                try:
                    headers = next(iterator)
                except StopIteration:
                    headers, iterator = (), ()
                source_sheets.append((worksheet.title, headers, iterator))
        except (ValueError, KeyError, zipfile.BadZipFile, StopIteration) as exc:
            raise ValueError('Could not read the Excel spreadsheet.') from exc
    else:
        raise ValueError('Only .csv and .xlsx files are supported.')
    for sheet_name, raw_headers, raw_rows in source_sheets:
        headers = []
        for index, header in enumerate(raw_headers):
            text = '' if header is None else str(header).strip()
            headers.append(text or ('Column ' + str(index + 1)))
        records = []
        pending_blank_rows = []
        for row_number, raw_row in enumerate(raw_rows, start=2):
            values = [_json_cell(value) for value in raw_row]
            if not values or all(value is None or str(value).strip() == '' for value in values):
                pending_blank_rows.append((row_number, values))
                continue
            rows_to_add = pending_blank_rows + [(row_number, values)]
            pending_blank_rows = []
            for pending_row_number, pending_values in rows_to_add:
                if len(pending_values) < len(headers):
                    pending_values.extend([None] * (len(headers) - len(pending_values)))
                elif len(pending_values) > len(headers):
                    for index in range(len(headers), len(pending_values)):
                        headers.append('Column ' + str(index + 1))
                records.append({'row_number': pending_row_number, 'values': pending_values[:len(headers)]})
                total_rows += 1
                if total_rows > MAX_IMPORT_ROWS:
                    raise ValueError('Workbook exceeds the 12,000-row safety limit.')
        sheets.append({'name': str(sheet_name), 'headers': headers, 'rows': records})
    if not sheets:
        raise ValueError('The workbook does not contain any worksheets.')
    return {'filename': str(filename), 'sheets': sheets}


def _suggest_mapping(headers, kind='data'):
    normalized = {}
    for header in headers:
        normalized.setdefault(normalized_column(header), header)
    aliases = {
        'employee_id': ('employeeid', 'empid', 'staffid', 'personnelno', 'id'),
        'name': ('name', 'fullname', 'employeename', 'staffname'),
        'department': ('department', 'dept', 'section', 'division'),
        'prize_name': ('prize', 'prizename', 'reward', 'item'),
        'quantity': ('quantity', 'qty', 'count', 'number'),
    }
    targets = ('employee_id', 'name', 'department') if kind == 'data' else (
        ('prize_name', 'quantity') if kind == 'prizes' else ('employee_id',))
    suggestion = {}
    for target in targets:
        for alias in aliases[target]:
            if alias in normalized:
                suggestion[target] = normalized[alias]
                break
    return suggestion


def inspect_import(filename, data, actor):
    parsed = parse_import_workbook(filename, data)
    upload_id = secrets.token_urlsafe(18)
    stamp = now()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=IMPORT_PREVIEW_TTL_SECONDS)).isoformat(timespec='seconds')
    response_sheets = []
    for sheet in parsed['sheets']:
        response_sheets.append({
            'name': sheet['name'], 'headers': sheet['headers'], 'row_count': len(sheet['rows']),
            'sample': [dict(zip(sheet['headers'], row['values'])) for row in sheet['rows'][:5]],
        })
    data_sheet = next((sheet for sheet in parsed['sheets'] if sheet['name'].strip().casefold() == 'data'),
                      parsed['sheets'][0])
    lucky_sheet = next((sheet for sheet in parsed['sheets']
                        if sheet['name'].strip().casefold() == 'lucky wheel'), None)
    lucky_type = None
    lucky_mapping = {}
    if lucky_sheet:
        prize_mapping = _suggest_mapping(lucky_sheet['headers'], 'prizes')
        participant_mapping = _suggest_mapping(lucky_sheet['headers'], 'participants')
        if 'prize_name' in prize_mapping:
            lucky_type, lucky_mapping = 'prizes', prize_mapping
        elif 'employee_id' in participant_mapping:
            lucky_type, lucky_mapping = 'participants', participant_mapping
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM import_previews WHERE expires_at<? AND status<>\'committed\'', (stamp,))
        db.execute('''INSERT INTO import_previews
            (upload_id,filename,parsed_json,status,created_by,created_at,expires_at)
            VALUES (?,?,?,'inspected',?,?,?)''',
            (upload_id, str(filename)[:255], json.dumps(parsed, ensure_ascii=False, default=str),
             actor_name(actor), stamp, expires))
        audit(db, actor, 'import.inspected', 'import_preview', upload_id,
              {'filename': str(filename)[:255], 'sheets': len(parsed['sheets']), 'rows': sum(len(s['rows']) for s in parsed['sheets'])})
        db.commit()
    return {
        'upload_id': upload_id, 'filename': str(filename), 'expires_at': expires,
        'sheets': response_sheets,
        'mapping_targets': {
            'data': ['employee_id', 'name', 'department'],
            'participants': ['employee_id'],
            'prizes': ['prize_name', 'quantity'],
        },
        'suggested_data_sheet': data_sheet['name'],
        'suggested_data_mapping': _suggest_mapping(data_sheet['headers'], 'data'),
        'suggested_lucky_sheet': lucky_sheet['name'] if lucky_sheet else None,
        'suggested_lucky_type': lucky_type,
        'suggested_lucky_mapping': lucky_mapping,
    }


def _sheet_by_name(parsed, name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Choose a worksheet.')
    exact = next((sheet for sheet in parsed['sheets'] if sheet['name'] == name), None)
    if exact:
        return exact
    folded = [sheet for sheet in parsed['sheets'] if sheet['name'].casefold() == name.casefold()]
    if len(folded) == 1:
        return folded[0]
    raise ValueError('Worksheet not found: ' + name)


def _mapping_indexes(sheet, mapping, required):
    if not isinstance(mapping, dict):
        raise ValueError('Column mapping is required.')
    by_name = {}
    for index, header in enumerate(sheet['headers']):
        by_name.setdefault(header, []).append(index)
    result = {}
    for target, source in mapping.items():
        if not isinstance(target, str) or not isinstance(source, str) or not source.strip():
            continue
        matches = by_name.get(source)
        if not matches:
            matches = [index for index, header in enumerate(sheet['headers'])
                       if header.casefold() == source.casefold()]
        if len(matches) != 1:
            raise ValueError('Mapped column is missing or ambiguous: ' + source)
        result[target] = matches[0]
    missing = [target for target in required if target not in result]
    if missing:
        raise ValueError('Missing required mapping: ' + ', '.join(missing))
    return result


def _mapped_value(row, index):
    if index is None or index >= len(row['values']):
        return ''
    value = row['values'][index]
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def build_import_preview(upload_id, options, actor):
    if not isinstance(upload_id, str) or not upload_id:
        raise ValueError('upload_id is required.')
    with db_connection() as db:
        stored = db.execute('SELECT * FROM import_previews WHERE upload_id=?', (upload_id,)).fetchone()
        if not stored:
            raise ValueError('Import upload not found.')
        if stored['status'] == 'committed':
            raise ValueError('This import has already been committed.')
        if stored['expires_at'] < now():
            raise ValueError('Import preview expired. Upload the workbook again.')
        parsed = json.loads(stored['parsed_json'])
        data_sheet = _sheet_by_name(parsed, options.get('data_sheet'))
        data_mapping = options.get('data_mapping') or {}
        data_indexes = _mapping_indexes(data_sheet, data_mapping, ('employee_id', 'name'))
        existing = {row['employee_id']: row['id'] for row in db.execute('SELECT id,employee_id FROM employees')}
    errors = []
    employees = []
    seen_ids = {}
    for row in data_sheet['rows']:
        employee_id = normalize_employee_id(_mapped_value(row, data_indexes['employee_id']))
        name = _mapped_value(row, data_indexes['name'])
        department = _mapped_value(row, data_indexes.get('department'))
        if not employee_id:
            errors.append({'sheet': data_sheet['name'], 'row': row['row_number'], 'field': 'employee_id',
                           'code': 'required', 'message': 'Employee ID is required.'})
            continue
        if not name:
            errors.append({'sheet': data_sheet['name'], 'row': row['row_number'], 'field': 'name',
                           'code': 'required', 'message': 'Employee name is required.'})
            continue
        if len(employee_id) > 60 or len(name) > 160 or len(department) > 160:
            errors.append({'sheet': data_sheet['name'], 'row': row['row_number'], 'field': 'row',
                           'code': 'too_long', 'message': 'Employee ID, name, or department is too long.'})
            continue
        if employee_id in seen_ids:
            errors.append({'sheet': data_sheet['name'], 'row': row['row_number'], 'field': 'employee_id',
                           'code': 'duplicate',
                           'message': 'Duplicate employee ID; first seen on row ' + str(seen_ids[employee_id]) + '.'})
            continue
        seen_ids[employee_id] = row['row_number']
        source_fields = {header: row['values'][index] if index < len(row['values']) else None
                         for index, header in enumerate(data_sheet['headers'])}
        mapped_fields = {target: _mapped_value(row, index) for target, index in data_indexes.items()
                         if target not in ('employee_id', 'name', 'department')}
        employees.append({'row': row['row_number'], 'employee_id': employee_id, 'name': name,
                          'department': department, 'extra': {'source': source_fields, 'mapped': mapped_fields},
                          'operation': 'update' if employee_id in existing else 'add'})
    lucky_type = options.get('lucky_type') or 'none'
    if lucky_type not in ('none', 'participants', 'prizes'):
        raise ValueError('lucky_type must be none, participants, or prizes.')
    participants, prizes, unmatched = [], [], []
    reconciliation = {'matched': [], 'unmatched': [], 'master_only': []}
    if lucky_type != 'none':
        lucky_sheet = _sheet_by_name(parsed, options.get('lucky_sheet'))
        lucky_mapping = options.get('lucky_mapping') or {}
        if lucky_type == 'participants':
            lucky_indexes = _mapping_indexes(lucky_sheet, lucky_mapping, ('employee_id',))
            participant_seen = {}
            master_ids = {item['employee_id'] for item in employees}
            known_ids = master_ids | set(existing)
            for row in lucky_sheet['rows']:
                employee_id = normalize_employee_id(_mapped_value(row, lucky_indexes['employee_id']))
                if not employee_id:
                    errors.append({'sheet': lucky_sheet['name'], 'row': row['row_number'], 'field': 'employee_id',
                                   'code': 'required', 'message': 'Lucky participant employee ID is required.'})
                    continue
                if employee_id in participant_seen:
                    errors.append({'sheet': lucky_sheet['name'], 'row': row['row_number'], 'field': 'employee_id',
                                   'code': 'duplicate',
                                   'message': 'Duplicate lucky participant; first seen on row ' + str(participant_seen[employee_id]) + '.'})
                    continue
                participant_seen[employee_id] = row['row_number']
                item = {'row': row['row_number'], 'employee_id': employee_id}
                if employee_id in known_ids:
                    participants.append(item)
                    reconciliation['matched'].append(item)
                else:
                    unmatched.append(item)
                    reconciliation['unmatched'].append(item)
            participant_ids = set(participant_seen)
            reconciliation['master_only'] = [
                {'employee_id': item['employee_id'], 'row': item['row']}
                for item in employees if item['employee_id'] not in participant_ids
            ]
        else:
            lucky_indexes = _mapping_indexes(lucky_sheet, lucky_mapping, ('prize_name', 'quantity'))
            prize_seen = {}
            for row in lucky_sheet['rows']:
                name = _mapped_value(row, lucky_indexes['prize_name'])
                quantity_text = _mapped_value(row, lucky_indexes['quantity'])
                try:
                    quantity_number = float(quantity_text)
                    quantity = int(quantity_number)
                    if quantity_number != quantity or quantity <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    errors.append({'sheet': lucky_sheet['name'], 'row': row['row_number'], 'field': 'quantity',
                                   'code': 'invalid', 'message': 'Prize quantity must be a positive whole number.'})
                    continue
                if not name or len(name) > 100:
                    errors.append({'sheet': lucky_sheet['name'], 'row': row['row_number'], 'field': 'prize_name',
                                   'code': 'invalid', 'message': 'Prize name is required and must be at most 100 characters.'})
                    continue
                key = name.casefold()
                if key in prize_seen:
                    errors.append({'sheet': lucky_sheet['name'], 'row': row['row_number'], 'field': 'prize_name',
                                   'code': 'duplicate',
                                   'message': 'Duplicate prize; first seen on row ' + str(prize_seen[key]) + '.'})
                    continue
                prize_seen[key] = row['row_number']
                prizes.append({'row': row['row_number'], 'name': name, 'quantity': quantity})
    summary = {
        'data_rows': len(data_sheet['rows']), 'valid_employees': len(employees),
        'employees_to_add': sum(1 for item in employees if item['operation'] == 'add'),
        'employees_to_update': sum(1 for item in employees if item['operation'] == 'update'),
        'participant_rows': len(participants) + len(unmatched), 'matched_participants': len(participants),
        'unmatched_participants': len(unmatched), 'prizes': len(prizes), 'error_count': len(errors),
    }
    preview = {'upload_id': upload_id, 'summary': summary, 'employees': employees,
               'participants': participants, 'prizes': prizes, 'errors': errors,
               'reconciliation': reconciliation, 'lucky_type': lucky_type}
    mapping_record = {
        'data_sheet': data_sheet['name'], 'data_mapping': data_mapping,
        'lucky_sheet': options.get('lucky_sheet'), 'lucky_type': lucky_type,
        'lucky_mapping': options.get('lucky_mapping') or {},
    }
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''UPDATE import_previews SET preview_json=?,mapping_json=?,status='previewed'
                      WHERE upload_id=? AND status<>'committed' ''',
                   (json.dumps(preview, ensure_ascii=False, default=str),
                    json.dumps(mapping_record, ensure_ascii=False), upload_id))
        audit(db, actor, 'import.previewed', 'import_preview', upload_id, summary)
        db.commit()
    return preview


def commit_import(upload_id, actor):
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            stored = db.execute('SELECT * FROM import_previews WHERE upload_id=?', (upload_id,)).fetchone()
            if not stored:
                raise ValueError('Import preview not found.')
            if stored['status'] == 'committed':
                history = db.execute('SELECT * FROM import_history WHERE id=?',
                                     (stored['committed_import_id'],)).fetchone()
                db.commit()
                return {'import_id': history['id'], **json.loads(history['summary_json']), 'already_committed': True}
            if stored['expires_at'] < now():
                raise ValueError('Import preview expired. Upload the workbook again.')
            if stored['status'] != 'previewed' or not stored['preview_json']:
                raise ValueError('Preview the import before committing it.')
            preview = json.loads(stored['preview_json'])
            if preview.get('errors'):
                raise ValueError('Resolve all import validation errors before committing.')
            if not preview.get('employees'):
                raise ValueError('The preview has no valid master employee rows.')
            history_cursor = db.execute('''INSERT INTO import_history
                (filename,status,mapping_json,summary_json,reconciliation_json,created_by,created_at,committed_at)
                VALUES (?,'committed',?,?,?,?,?,?)''',
                (stored['filename'], stored['mapping_json'], json.dumps(preview['summary'], ensure_ascii=False),
                 json.dumps(preview['reconciliation'], ensure_ascii=False), actor_name(actor),
                 stored['created_at'], now()))
            import_id = history_cursor.lastrowid
            added = updated = 0
            for item in preview['employees']:
                current = db.execute('SELECT id FROM employees WHERE employee_id=?',
                                     (item['employee_id'],)).fetchone()
                extra_json = json.dumps(item.get('extra') or {}, ensure_ascii=False, default=str)
                if current:
                    db.execute('''UPDATE employees SET name=?,department=?,extra_json=? WHERE id=?''',
                               (item['name'], item.get('department', ''), extra_json, current['id']))
                    updated += 1
                else:
                    if db.execute('SELECT COUNT(*) FROM employees').fetchone()[0] >= MAX_EMPLOYEES:
                        raise ValueError('Import would exceed the 5,000 employee limit.')
                    token = _unique_qr_token(db)
                    employee_cursor = db.execute('''INSERT INTO employees
                        (employee_id,name,department,qr_token,extra_json) VALUES (?,?,?,?,?)''',
                        (item['employee_id'], item['name'], item.get('department', ''), token, extra_json))
                    ensure_wristband(db, employee_cursor.lastrowid, actor, 'not_issued', token)
                    added += 1
            if preview.get('lucky_type') == 'participants':
                stamp = now()
                db.execute('''UPDATE lucky_draw_participants SET active=0,removed_at=? WHERE active=1''', (stamp,))
                for item in preview.get('participants', []):
                    employee = db.execute('SELECT id FROM employees WHERE employee_id=?',
                                          (item['employee_id'],)).fetchone()
                    if not employee:
                        continue
                    db.execute('''INSERT INTO lucky_draw_participants
                        (employee_id,active,source_import_id,added_at,removed_at)
                        VALUES (?,1,?,?,NULL)
                        ON CONFLICT(employee_id) DO UPDATE SET active=1,
                            source_import_id=excluded.source_import_id,added_at=excluded.added_at,removed_at=NULL''',
                        (employee['id'], import_id, stamp))
                set_setting(db, 'lucky_participants_configured', True, actor)
            for item in preview.get('prizes', []):
                stamp = now()
                existing_prize = db.execute('SELECT id FROM prizes WHERE name=? COLLATE NOCASE',
                                            (item['name'],)).fetchone()
                if existing_prize:
                    active_winners = db.execute('''SELECT COUNT(*) FROM lucky_draw_winners
                        WHERE prize_id=? AND status='active' ''', (existing_prize['id'],)).fetchone()[0]
                    if item['quantity'] < active_winners:
                        raise ValueError('Prize quantity for ' + item['name'] +
                                         ' cannot be lower than its active winner count.')
                db.execute('''INSERT INTO prizes(name,quantity,active,created_at,updated_at)
                    VALUES (?,?,1,?,?)
                    ON CONFLICT(name) DO UPDATE SET quantity=excluded.quantity,active=1,updated_at=excluded.updated_at''',
                    (item['name'], item['quantity'], stamp, stamp))
            final_summary = dict(preview['summary'])
            final_summary.update({'added': added, 'updated': updated, 'count': len(preview['employees'])})
            db.execute('UPDATE import_history SET summary_json=? WHERE id=?',
                       (json.dumps(final_summary, ensure_ascii=False), import_id))
            db.execute('''UPDATE import_previews SET status='committed',committed_import_id=?,
                          parsed_json='{}',preview_json=NULL WHERE id=?''',
                       (import_id, stored['id']))
            db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (NULL,?,?,?)',
                       (f'import: {added} added, {updated} updated', actor_name(actor), now()))
            audit(db, actor, 'import.committed', 'import_history', import_id, final_summary)
            db.commit()
            return {'import_id': import_id, **final_summary}
        except Exception:
            db.rollback()
            raise


def generate_wristbands(employee_ids, actor):
    if employee_ids is not None and not isinstance(employee_ids, list):
        raise ValueError('employee_ids must be a list when provided.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if employee_ids:
                cleaned = [str(value).strip() for value in employee_ids if str(value).strip()]
                placeholders = ','.join('?' for _ in cleaned)
                employees = db.execute('SELECT id,employee_id FROM employees WHERE employee_id IN (' +
                                       placeholders + ') ORDER BY employee_id', cleaned).fetchall() if cleaned else []
                found = {row['employee_id'] for row in employees}
                missing = [value for value in cleaned if value not in found]
                if missing:
                    raise ValueError('Unknown employee ID: ' + missing[0])
            else:
                employees = db.execute('SELECT id,employee_id FROM employees ORDER BY employee_id').fetchall()
            generated = existing = 0
            items = []
            for employee in employees:
                wristband, created = ensure_wristband(db, employee['id'], actor, 'not_issued')
                generated += int(created)
                existing += int(not created)
                items.append({'employee_id': employee['employee_id'], 'wristband_id': wristband['id'],
                              'status': wristband['status'],
                              'qr_value': TOKEN_PREFIX + wristband['token'] if created else None})
            audit(db, actor, 'wristband.bulk_generate', 'wristband', None,
                  {'requested': len(employees), 'generated': generated, 'existing': existing})
            db.commit()
            return {'generated': generated, 'existing': existing, 'items': items}
        except Exception:
            db.rollback()
            raise


def change_wristband(employee_external_id, action, actor, reason=None):
    if action not in ('issue', 'replace', 'revoke', 'generate'):
        raise ValueError('Wristband action must be issue, replace, revoke, or generate.')
    reason = str(reason or '').strip()[:500] or None
    if action in ('replace', 'revoke') and not reason:
        raise ValueError('A reason is required to replace or revoke a wristband.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            employee = db.execute('SELECT id,employee_id,name,department FROM employees WHERE employee_id=?',
                                  (normalize_employee_id(employee_external_id),)).fetchone()
            if not employee:
                raise ValueError('Employee not found.')
            current = active_wristband(db, employee['id'])
            stamp = now()
            if action == 'generate':
                wristband, created = ensure_wristband(db, employee['id'], actor, 'not_issued')
                state = 'generated' if created else 'already_exists'
            elif action == 'issue':
                if not current:
                    current, _ = ensure_wristband(db, employee['id'], actor, 'not_issued')
                if current['status'] == 'issued':
                    wristband, state = current, 'already_issued'
                else:
                    db.execute('''UPDATE wristbands SET status='issued',issued_at=?,issued_by=? WHERE id=?''',
                               (stamp, actor_name(actor), current['id']))
                    db.execute('''INSERT INTO wristband_distribution_history
                        (wristband_id,employee_id,action,status,actor,reason,occurred_at)
                        VALUES (?,?,'issued','issued',?,?,?)''',
                        (current['id'], employee['id'], actor_name(actor), reason, stamp))
                    audit(db, actor, 'wristband.issued', 'wristband', current['id'],
                          {'employee_id': employee['employee_id']})
                    wristband = db.execute('SELECT * FROM wristbands WHERE id=?', (current['id'],)).fetchone()
                    state = 'issued'
            elif action == 'replace':
                if not current:
                    raise ValueError('Employee has no active wristband to replace.')
                db.execute('''UPDATE wristbands SET status='replaced',invalidated_at=?,reason=? WHERE id=?''',
                           (stamp, reason, current['id']))
                db.execute('''INSERT INTO wristband_distribution_history
                    (wristband_id,employee_id,action,status,actor,reason,occurred_at)
                    VALUES (?,?,'replaced','replaced',?,?,?)''',
                    (current['id'], employee['id'], actor_name(actor), reason, stamp))
                wristband, _ = ensure_wristband(db, employee['id'], actor, 'issued')
                db.execute('UPDATE wristbands SET replaced_by_id=? WHERE id=?',
                           (wristband['id'], current['id']))
                audit(db, actor, 'wristband.replaced', 'wristband', current['id'],
                      {'employee_id': employee['employee_id'], 'replacement_id': wristband['id'],
                       'reason': reason})
                state = 'replaced'
            else:
                if not current:
                    raise ValueError('Employee has no active wristband to revoke.')
                db.execute('''UPDATE wristbands SET status='revoked',invalidated_at=?,reason=? WHERE id=?''',
                           (stamp, reason, current['id']))
                db.execute('''INSERT INTO wristband_distribution_history
                    (wristband_id,employee_id,action,status,actor,reason,occurred_at)
                    VALUES (?,?,'revoked','revoked',?,?,?)''',
                    (current['id'], employee['id'], actor_name(actor), reason, stamp))
                audit(db, actor, 'wristband.revoked', 'wristband', current['id'],
                      {'employee_id': employee['employee_id'], 'reason': reason})
                wristband, state = db.execute('SELECT * FROM wristbands WHERE id=?',
                                              (current['id'],)).fetchone(), 'revoked'
            db.commit()
            return {'state': state, 'employee_id': employee['employee_id'], 'name': employee['name'],
                    'wristband_id': wristband['id'], 'status': wristband['status'],
                    'issued_at': wristband['issued_at'], 'issued_by': wristband['issued_by'],
                    'qr_value': TOKEN_PREFIX + wristband['token'] if wristband['status'] in WRISTBAND_ACTIVE_STATUSES else None}
        except Exception:
            db.rollback()
            raise


def _verification_result(value):
    normalized = str(value or '').strip().lower().replace(' ', '_').replace('-', '_')
    aliases = {'success': 'verified', 'failed': 'identity_mismatch', 'mismatch': 'identity_mismatch'}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ('verified', 'identity_mismatch'):
        raise ValueError('Invalid verification result.')
    return normalized


def record_verification(code, result, reason, actor, employee_external_id=None):
    requested = _verification_result(result)
    reason = str(reason or '').strip()[:500] or None
    if requested == 'identity_mismatch' and not reason:
        raise ValueError('A reason is required for failed identity verification.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            resolved = None
            raw_code = str(code or '').strip()
            if employee_external_id:
                employee = db.execute('SELECT * FROM employees WHERE employee_id=?',
                                      (str(employee_external_id).strip(),)).fetchone()
                if employee:
                    wristband = active_wristband(db, employee['id'])
                    if wristband:
                        resolved = db.execute('''SELECT w.id AS wristband_id,w.employee_id AS wristband_employee_id,
                            w.token,w.status AS wristband_status,w.issued_at,w.issued_by,e.*
                            FROM wristbands w JOIN employees e ON e.id=w.employee_id WHERE w.id=?''',
                            (wristband['id'],)).fetchone()
            elif raw_code:
                try:
                    token = normalize_token(raw_code)
                except ValueError:
                    token = None
                if token:
                    resolved = resolve_wristband(db, token)
            token_hash = hashlib.sha256(raw_code.encode('utf-8')).hexdigest() if raw_code else None
            if not resolved or resolved['wristband_status'] not in WRISTBAND_ACTIVE_STATUSES:
                db.rollback()
                return employee_data(resolved) if resolved else None, {
                    'id': None, 'result': 'invalid_wristband', 'reason': None,
                    'verified_at': None, 'verified_by': None}
            employee_id = resolved['id']
            wristband_id = resolved['wristband_id']
            checked_in = bool(resolved['checked_in_at']) or bool(db.execute(
                'SELECT 1 FROM event_checkins WHERE employee_id=?', (employee_id,)).fetchone())
            if not checked_in:
                db.rollback()
                return employee_data(resolved), {
                    'id': None, 'result': 'checkin_required', 'reason': None,
                    'verified_at': None, 'verified_by': None}
            actual = requested
            stamp = now()
            cursor = db.execute('''INSERT INTO identity_verification_history
                (employee_id,wristband_id,result,reason,verified_at,staff_username,scanned_token_hash)
                VALUES (?,?,?,?,?,?,?)''',
                (employee_id, wristband_id, actual, reason, stamp, actor_name(actor), token_hash))
            if actual == 'verified' and employee_id is not None:
                db.execute('UPDATE employees SET verified_at=COALESCE(verified_at,?) WHERE id=?',
                           (stamp, employee_id))
                db.execute('INSERT INTO event_log(employee_id,action,actor,occurred_at) VALUES (?,?,?,?)',
                           (employee_id, 'verify', actor_name(actor), stamp))
            audit(db, actor, 'verification.recorded', 'verification', cursor.lastrowid,
                  {'employee_id': employee_id, 'wristband_id': wristband_id,
                   'requested_result': requested, 'result': actual, 'reason': reason})
            db.commit()
            person = employee_data(resolved) if resolved else None
            verification = {'id': cursor.lastrowid, 'result': actual, 'reason': reason,
                            'verified_at': stamp, 'verified_by': actor_name(actor)}
            return person, verification
        except Exception:
            db.rollback()
            raise


def add_lucky_participant(employee_external_id, actor):
    employee_external_id = normalize_employee_id(employee_external_id)
    if not employee_external_id:
        raise ValueError('Employee ID is required.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            employee = db.execute('SELECT * FROM employees WHERE employee_id=?',
                                  (employee_external_id,)).fetchone()
            if not employee:
                db.rollback()
                return None, 'not_found'
            if is_manager_position(employee_data(employee)['extra'].get('position')):
                raise ValueError('Managers cannot join the lucky wheel.')
            existing = db.execute('SELECT active FROM lucky_draw_participants WHERE employee_id=?',
                                  (employee['id'],)).fetchone()
            state = 'already_added' if existing and existing['active'] else 'added'
            stamp = now()
            db.execute('''INSERT INTO lucky_draw_participants
                (employee_id,active,source_import_id,added_at,removed_at)
                VALUES (?,1,NULL,?,NULL)
                ON CONFLICT(employee_id) DO UPDATE SET active=1,source_import_id=NULL,
                    added_at=excluded.added_at,removed_at=NULL''', (employee['id'], stamp))
            set_setting(db, 'lucky_participants_configured', True, actor)
            manual_ids = get_setting('manual_lucky_participant_ids', [], db)
            if not isinstance(manual_ids, list):
                manual_ids = []
            if employee_external_id not in manual_ids:
                manual_ids.append(employee_external_id)
                set_setting(db, 'manual_lucky_participant_ids', manual_ids, actor)
            audit(db, actor, 'draw.participant_added', 'employee', employee['id'],
                  {'employee_id': employee_external_id, 'state': state})
            eligible_sql, eligible_params = _eligible_sql(db)
            eligible_now = bool(db.execute('SELECT 1 FROM employees e WHERE e.id=? AND ' + eligible_sql,
                                           [employee['id']] + eligible_params).fetchone())
            db.commit()
            person = employee_data(employee)
            person['eligible_now'] = eligible_now
            return person, state
        except Exception:
            db.rollback()
            raise


def save_reward_draft(data):
    name = str(data.get('name') or '').strip()
    if not 1 <= len(name) <= 100:
        raise ValueError('Reward name must be 1-100 characters.')
    try:
        quantity = int(data.get('quantity', 1))
    except (ValueError, TypeError):
        raise ValueError('Enter a valid quantity.') from None
    if not 1 <= quantity <= MAX_EMPLOYEES:
        raise ValueError('Quantity must be between 1 and 5,000.')
    action = data.get('action', 'save')
    if action not in ('save', 'remove'):
        raise ValueError('Invalid reward action.')
    source_id = data.get('source_id') or None
    draft_id = data.get('id') or None
    image, content_type = None, None
    image_changed = bool(data.get('remove_image'))
    if data.get('data_url'):
        match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', data['data_url'])
        if not match:
            raise ValueError('Choose a PNG, JPEG, or WebP image.')
        try: image = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError): raise ValueError('Invalid image.') from None
        if not image or len(image) > PRIZE_IMAGE_MAX_BYTES or _detect_prize_image_type(image) != match.group(1):
            raise ValueError('Image must be a valid PNG, JPEG, or WebP, at most 4 MB.')
        content_type, image_changed = match.group(1), True
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if source_id and not db.execute('SELECT 1 FROM prizes WHERE id=? AND active=1', (source_id,)).fetchone():
            raise ValueError('Reward no longer exists. Refresh the list.')
        if draft_id:
            previous = db.execute('SELECT * FROM reward_drafts WHERE id=?', (draft_id,)).fetchone()
            if not previous: raise ValueError('Draft no longer exists. Refresh the list.')
            source_id = previous['source_id']
        if source_id:
            current = db.execute('SELECT name FROM prizes WHERE id=? AND active=1', (source_id,)).fetchone()
            if not current: raise ValueError('Reward no longer exists. Refresh the list.')
            drawn = db.execute("SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'", (source_id,)).fetchone()[0]
            if drawn and (action == 'remove' or name != current['name']):
                raise ValueError('This reward already has winners and cannot be deleted or renamed.')
            if quantity < drawn:
                raise ValueError('Quantity cannot be less than the number of winners.')
        if draft_id:
            if not image_changed:
                image, content_type, image_changed = previous['image'], previous['content_type'], previous['image_changed']
            db.execute('UPDATE reward_drafts SET name=?,quantity=?,action=?,content_type=?,image=?,image_changed=? WHERE id=?',
                       (name,quantity,action,content_type,image,image_changed,draft_id))
        else:
            try:
                draft_id = db.execute('INSERT INTO reward_drafts(source_id,name,quantity,action,content_type,image,image_changed) VALUES(?,?,?,?,?,?,?)',
                                      (source_id,name,quantity,action,content_type,image,image_changed)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError('This reward already has a pending change.') from None
        db.commit()
    return {'id': draft_id}


def publish_reward_drafts(data, actor):
    ids = data.get('ids')
    if not isinstance(ids, list) or not ids: raise ValueError('Add a reward first.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            drafts = db.execute('SELECT * FROM reward_drafts ORDER BY id').fetchall()
            if sorted(str(row['id']) for row in drafts) != sorted(str(i) for i in ids):
                raise ValueError('The reward list changed. Refresh and review it before submitting.')
            if get_projector_state(db)['phase'] in ('spinning', 'winner_selected'):
                raise ValueError('Wait for the current draw to finish.')
            stamp = now()
            for row in drafts:
                prize_id = row['source_id']
                if prize_id:
                    current = db.execute('SELECT * FROM prizes WHERE id=? AND active=1', (prize_id,)).fetchone()
                    if not current: raise ValueError('A reward no longer exists. Refresh the list.')
                    drawn = db.execute("SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'", (prize_id,)).fetchone()[0]
                    if drawn and (row['action'] == 'remove' or row['name'] != current['name']):
                        raise ValueError(f"Remove the pending change for '{current['name']}': rewards with winners cannot be deleted or renamed.")
                    if row['quantity'] < drawn: raise ValueError('Quantity cannot be less than the number of winners.')
                    db.execute('UPDATE prizes SET name=?,quantity=?,active=?,updated_at=? WHERE id=?',
                               (row['name'],row['quantity'],row['action'] != 'remove',stamp,prize_id))
                else:
                    if row['action'] == 'remove': continue
                    prize_id = db.execute('INSERT INTO prizes(name,quantity,active,created_at,updated_at) VALUES(?,?,1,?,?)',
                                          (row['name'],row['quantity'],stamp,stamp)).lastrowid
                if row['image_changed']:
                    db.execute('DELETE FROM reward_images WHERE prize_id=?', (prize_id,))
                    if row['image']:
                        db.execute('INSERT INTO reward_images(prize_id,content_type,data) VALUES(?,?,?)', (prize_id,row['content_type'],row['image']))
                audit(db, actor, 'reward.published', 'prize', prize_id, {'name':row['name'],'action':row['action']})
            db.execute('DELETE FROM reward_drafts')
            db.execute('UPDATE lucky_draw_presentation SET sequence=sequence+1,updated_at=?,updated_by=? WHERE singleton=1', (stamp,actor_name(actor)))
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ValueError('A reward with that name already exists.') from exc
        except Exception:
            db.rollback()
            raise
    return {'published': len(drafts)}


def save_prize(payload, actor):
    if not isinstance(payload, dict):
        raise ValueError('Invalid prize request.')
    name = str(payload.get('name') or '').strip()
    if not 1 <= len(name) <= 100:
        raise ValueError('Prize name must be 1-100 characters.')
    try:
        quantity = int(payload.get('quantity', 1))
    except (TypeError, ValueError):
        raise ValueError('Prize quantity must be a positive whole number.') from None
    if quantity < 1 or quantity > MAX_EMPLOYEES:
        raise ValueError('Prize quantity must be between 1 and 5,000.')
    active = 1 if payload.get('active', True) is not False else 0
    prize_id = payload.get('id') or payload.get('prize_id')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if get_projector_state(db)['phase'] in ('spinning', 'winner_selected'):
                raise ValueError('Wait for the current draw to finish before changing rewards.')
            stamp = now()
            if prize_id is not None:
                current = db.execute('SELECT * FROM prizes WHERE id=?', (prize_id,)).fetchone()
                if not current:
                    raise ValueError('Prize not found.')
                drawn = db.execute("SELECT COUNT(*) FROM lucky_draw_winners WHERE prize_id=? AND status='active'",
                                   (prize_id,)).fetchone()[0]
                if quantity < drawn:
                    raise ValueError('Quantity cannot be lower than the number of active winners.')
                db.execute('UPDATE prizes SET name=?,quantity=?,active=?,updated_at=? WHERE id=?',
                           (name, quantity, active, stamp, prize_id))
                action = 'prize.updated'
            else:
                cursor = db.execute('''INSERT INTO prizes(name,quantity,active,created_at,updated_at)
                                       VALUES (?,?,?,?,?)''',
                                    (name, quantity, active, stamp, stamp))
                prize_id, action = cursor.lastrowid, 'prize.created'
            audit(db, actor, action, 'prize', prize_id,
                  {'name': name, 'quantity': quantity, 'active': bool(active)})
            db.execute('''UPDATE lucky_draw_presentation SET sequence=sequence+1,updated_at=?,updated_by=?
                          WHERE singleton=1''', (stamp, actor_name(actor)))
            db.commit()
            return dict(db.execute('SELECT * FROM prizes WHERE id=?', (prize_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ValueError('A prize with that name already exists.') from exc
        except Exception:
            db.rollback()
            raise


def create_user(payload, actor):
    if not isinstance(payload, dict):
        raise ValueError('Invalid user request.')
    username = str(payload.get('username') or '').strip()
    display_name = str(payload.get('display_name') or username).strip()
    password = payload.get('password')
    role = str(payload.get('role') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', username):
        raise ValueError('Username must be 3-64 letters, numbers, dots, dashes, or underscores.')
    if username.casefold().startswith('legacy-'):
        raise ValueError('Usernames beginning with legacy- are reserved.')
    if not display_name or len(display_name) > 120:
        raise ValueError('Display name is required and must be at most 120 characters.')
    if role not in NAMED_ROLES:
        raise ValueError('Invalid role.')
    if not isinstance(password, str) or len(password) < 10 or len(password) > 512:
        raise ValueError('Password must be 10-512 characters.')
    salt = secrets.token_hex(16)
    stamp = now()
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            cursor = db.execute('''INSERT INTO users
                (username,display_name,password_salt,password_hash,role,active,created_at,updated_at)
                VALUES (?,?,?,?,?,1,?,?)''',
                (username, display_name, salt, password_hash(password, salt), role, stamp, stamp))
            audit(db, actor, 'user.created', 'user', cursor.lastrowid,
                  {'username': username, 'role': role})
            db.commit()
            return {'id': cursor.lastrowid, 'username': username, 'display_name': display_name,
                    'role': role, 'active': True, 'created_at': stamp, 'updated_at': stamp}
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ValueError('That username already exists.') from exc
        except Exception:
            db.rollback()
            raise


def update_user(payload, actor):
    if not isinstance(payload, dict):
        raise ValueError('Invalid user request.')
    identifier = payload.get('id')
    username = payload.get('username')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            current = db.execute('SELECT * FROM users WHERE id=?', (identifier,)).fetchone() if identifier is not None else db.execute(
                'SELECT * FROM users WHERE username=? COLLATE NOCASE', (str(username or '').strip(),)).fetchone()
            if not current:
                raise ValueError('User not found.')
            display_name = str(payload.get('display_name', current['display_name'])).strip()
            role = str(payload.get('role', current['role'])).strip()
            active = 1 if payload.get('active', bool(current['active'])) is not False else 0
            if not display_name or len(display_name) > 120 or role not in NAMED_ROLES:
                raise ValueError('Invalid display name or role.')
            if current['role'] == 'admin' and current['active'] and (role != 'admin' or not active):
                other_admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                                          (current['id'],)).fetchone()[0]
                # Legacy admin remains available, but retaining a named admin avoids accidental lockout.
                if other_admins == 0:
                    raise ValueError('Create another active named administrator first.')
            updates = {'display_name': display_name, 'role': role, 'active': active,
                       'updated_at': now(), 'password_salt': current['password_salt'],
                       'password_hash': current['password_hash']}
            password = payload.get('password')
            if password is not None:
                if not isinstance(password, str) or len(password) < 10 or len(password) > 512:
                    raise ValueError('Password must be 10-512 characters.')
                updates['password_salt'] = secrets.token_hex(16)
                updates['password_hash'] = password_hash(password, updates['password_salt'])
            db.execute('''UPDATE users SET display_name=?,role=?,active=?,password_salt=?,password_hash=?,updated_at=?
                          WHERE id=?''',
                       (updates['display_name'], updates['role'], updates['active'], updates['password_salt'],
                        updates['password_hash'], updates['updated_at'], current['id']))
            if not active or role != current['role'] or password is not None:
                db.execute('''UPDATE staff_sessions SET ended_at=?
                              WHERE username=? COLLATE NOCASE AND ended_at IS NULL''',
                           (now(), current['username']))
            audit(db, actor, 'user.updated', 'user', current['id'],
                  {'username': current['username'], 'role': role, 'active': bool(active),
                   'password_changed': password is not None})
            db.commit()
            return {'id': current['id'], 'username': current['username'], 'display_name': display_name,
                    'role': role, 'active': bool(active), 'updated_at': updates['updated_at']}
        except Exception:
            db.rollback()
            raise


def update_settings(payload, actor):
    allowed = {'event_name', 'require_verification_for_draw', 'allow_multiple_wins', 'accent_color',
               'projector_title', 'projector_theme', 'projector_show_employee_id'}
    if not isinstance(payload, dict) or not set(payload).intersection(allowed):
        raise ValueError('No supported settings were provided.')
    values = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key in ('require_verification_for_draw', 'allow_multiple_wins', 'projector_show_employee_id'):
            if not isinstance(value, bool):
                raise ValueError(key + ' must be true or false.')
        elif key == 'event_name':
            value = str(value or '').strip()
            if not 1 <= len(value) <= 90:
                raise ValueError('Event name must be 1-90 characters.')
        elif key == 'projector_title':
            value = str(value or '').strip()
            if not 1 <= len(value) <= 90:
                raise ValueError('Projector title must be 1-90 characters.')
        elif key == 'projector_theme':
            value = str(value or '').strip().lower()
            if value not in ('gold', 'indigo', 'emerald'):
                raise ValueError('Projector theme must be gold, indigo, or emerald.')
        elif key == 'accent_color':
            value = str(value or '').strip()
            if not re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
                raise ValueError('Accent color must be a six-digit hex color.')
        values[key] = value
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for key, value in values.items():
            set_setting(db, key, value, actor)
        audit(db, actor, 'settings.updated', 'settings', None, values)
        db.commit()
    return values


def reroll_winner(winner_id, reason, actor):
    reason = str(reason or '').strip()[:500]
    if not reason:
        raise ValueError('A reroll reason is required.')
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            original = db.execute('SELECT * FROM lucky_draw_winners WHERE id=? AND status=\'active\'',
                                  (winner_id,)).fetchone()
            if not original:
                raise ValueError('Active winner not found.')
            stamp = now()
            db.execute('''UPDATE lucky_draw_winners SET status='rerolled',rerolled_at=?,rerolled_by=?,reroll_reason=?
                          WHERE id=?''', (stamp, actor_name(actor), reason, original['id']))
            if original['legacy_winner_id']:
                db.execute("UPDATE winners SET status='rerolled' WHERE id=?", (original['legacy_winner_id'],))
            replacement, state = _draw_with_db(
                db, original['prize_name'], actor, prize_id=original['prize_id'], enforce_quantity=True,
                exclude_employee_id=original['employee_id'], reroll_of_winner_id=original['id'])
            if replacement is None:
                raise ValueError('No eligible replacement winner is available.')
            audit(db, actor, 'draw.winner_rerolled', 'winner', original['id'],
                  {'reason': reason, 'replacement_winner_id': replacement['winner_id']})
            db.commit()
            return {'rerolled_winner_id': original['id'], 'reason': reason,
                    'winner': replacement, 'state': state}
        except Exception:
            db.rollback()
            raise


def csv_safe(value):
    """Prevent spreadsheet formula execution when admins open exports."""
    s = '' if value is None else str(value)
    return "'" + s if s.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) else s


def make_csv(headers, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return output.getvalue().encode('utf-8-sig')


def qr_zip_bytes():
    try:
        import qrcode
    except ImportError:
        raise ValueError('QR export needs: pip install qrcode[pil]') from None
    out = io.BytesIO()
    with db_connection() as db, zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        employees = db.execute('''SELECT e.id,e.employee_id,e.name,e.department,w.token AS qr_token,
                                         w.status,w.issued_at,w.issued_by
                                  FROM employees e JOIN wristbands w ON w.employee_id=e.id
                                  WHERE w.status IN ('not_issued','issued') ORDER BY e.employee_id''')
        manifest = []
        label_cards = []
        for employee in employees:
            qr = qrcode.QRCode(box_size=5, border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(TOKEN_PREFIX + employee['qr_token'])
            qr.make(fit=True)
            image = qr.make_image(fill_color='black', back_color='white')
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', employee['employee_id'])[:50]
            filename = f"qr/{employee['id']:05d}_{safe_id}.png"
            archive.writestr(filename, buffer.getvalue())
            manifest.append((employee['employee_id'], employee['name'], employee['department'],
                             employee['status'], employee['issued_at'], employee['issued_by'],
                             filename, employee['name'] + ' - ' + employee['employee_id']))
            label_cards.append(
                '<article class="label"><img src="' + html.escape(filename, quote=True) +
                '" alt="QR code"><div><strong>' + html.escape(employee['name']) +
                '</strong><span>' + html.escape(employee['employee_id']) + '</span><small>' +
                html.escape(employee['department'] or '') + '</small></div></article>')
        archive.writestr('manifest.csv', make_csv(
            ['employee_id', 'name', 'department', 'wristband_status', 'issued_at', 'issued_by',
             'qr_image', 'label_text'], manifest))
        labels_html = '''<!doctype html><html><head><meta charset="utf-8"><title>Wristband labels</title>
<style>@page{size:A4;margin:8mm}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;
display:grid;grid-template-columns:repeat(3,1fr);gap:4mm}.label{height:35mm;border:1px dashed #999;
padding:3mm;display:flex;align-items:center;gap:3mm;break-inside:avoid}.label img{width:27mm;height:27mm}
.label div{min-width:0}.label strong,.label span,.label small{display:block;overflow-wrap:anywhere}
.label strong{font-size:11pt}.label span{font:10pt monospace;margin-top:1mm}.label small{font-size:8pt;margin-top:1mm}
@media print{.label{border-color:#bbb}}</style></head><body>''' + ''.join(label_cards) + '</body></html>'
        archive.writestr('labels.html', labels_html.encode('utf-8'))
    return out.getvalue()


def wristband_qr_png(employee_external_id):
    try:
        import qrcode
    except ImportError:
        raise ValueError('QR preview needs: pip install qrcode[pil]') from None
    with db_connection() as db:
        row = db.execute('''SELECT w.token FROM wristbands w JOIN employees e ON e.id=w.employee_id
                            WHERE e.employee_id=? AND w.status IN ('not_issued','issued')
                            ORDER BY w.id DESC LIMIT 1''', (normalize_employee_id(employee_external_id),)).fetchone()
    if not row:
        raise ValueError('No active wristband is available for this employee.')
    qr = qrcode.QRCode(box_size=8, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(TOKEN_PREFIX + row['token']); qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    output = io.BytesIO(); image.save(output, format='PNG')
    return output.getvalue()


def _report_datasets(db, report_type):
    datasets = []
    if report_type == 'master':
        rows = [tuple(row) for row in db.execute('''SELECT employee_id,name,department,extra_json
                                                     FROM employees ORDER BY employee_id''')]
        datasets.append(('Master Employees', ['employee_id', 'name', 'department', 'extra_json'], rows, {'employee_id'}))
    elif report_type == 'wristbands':
        rows = [tuple(row) for row in db.execute('''
            SELECT e.employee_id,e.name,e.department,w.status,w.created_at,w.issued_at,w.issued_by,
                   w.invalidated_at,w.reason
            FROM wristbands w JOIN employees e ON e.id=w.employee_id ORDER BY e.employee_id,w.id''')]
        datasets.append(('Wristbands', ['employee_id', 'name', 'department', 'status', 'created_at',
                         'issued_at', 'issued_by', 'invalidated_at', 'reason'], rows, {'employee_id'}))
    elif report_type in ('attendance', 'not_checked_in'):
        predicate = "WHERE COALESCE(c.checked_in_at,e.checked_in_at) IS NULL" if report_type == 'not_checked_in' else ''
        rows = [tuple(row) for row in db.execute('''
            SELECT e.employee_id,e.name,e.department,COALESCE(c.checked_in_at,e.checked_in_at),
                   c.staff_username
            FROM employees e LEFT JOIN event_checkins c ON c.employee_id=e.id ''' + predicate +
            ' ORDER BY e.employee_id')]
        title = 'Not Checked In' if report_type == 'not_checked_in' else 'Attendance'
        datasets.append((title, ['employee_id', 'name', 'department', 'checked_in_at', 'checked_in_by'],
                         rows, {'employee_id'}))
    elif report_type == 'verifications':
        rows = [tuple(row) for row in db.execute('''
            SELECT e.employee_id,e.name,e.department,v.result,v.reason,v.verified_at,v.staff_username
            FROM identity_verification_history v LEFT JOIN employees e ON e.id=v.employee_id
            ORDER BY v.id''')]
        datasets.append(('Verifications', ['employee_id', 'name', 'department', 'result', 'reason',
                         'verified_at', 'verified_by'], rows, {'employee_id'}))
    elif report_type == 'reconciliation':
        history = db.execute('''SELECT id,filename,reconciliation_json FROM import_history
                                WHERE reconciliation_json IS NOT NULL ORDER BY id DESC LIMIT 1''').fetchone()
        rows = []
        if history:
            try:
                reconciliation = json.loads(history['reconciliation_json'])
            except (json.JSONDecodeError, TypeError):
                reconciliation = {}
            for category in ('matched', 'unmatched', 'master_only'):
                for item in reconciliation.get(category, []):
                    rows.append((history['id'], history['filename'], category,
                                 item.get('employee_id'), item.get('row')))
        datasets.append(('Reconciliation', ['import_id', 'filename', 'category', 'employee_id', 'source_row'],
                         rows, {'employee_id'}))
    elif report_type == 'eligibility':
        predicate, params = _eligible_sql(db)
        rows = [tuple(row) for row in db.execute('''
            SELECT e.employee_id,e.name,e.department,COALESCE(c.checked_in_at,e.checked_in_at)
            FROM employees e LEFT JOIN event_checkins c ON c.employee_id=e.id WHERE ''' + predicate +
            ' ORDER BY e.employee_id', params)]
        datasets.append(('Draw Eligibility', ['employee_id', 'name', 'department', 'checked_in_at'],
                         rows, {'employee_id'}))
    elif report_type == 'winners':
        rows = [tuple(row) for row in db.execute('''
            SELECT e.employee_id,e.name,e.department,w.prize_name,r.round_number,r.label,w.status,w.won_at,w.won_by,
                   w.reroll_reason,w.rerolled_at,w.rerolled_by
            FROM lucky_draw_winners w JOIN employees e ON e.id=w.employee_id
            JOIN lucky_draw_rounds r ON r.id=w.round_id ORDER BY w.id''')]
        datasets.append(('Winners', ['employee_id', 'name', 'department', 'prize', 'round_number', 'round_name', 'status',
                         'won_at', 'won_by', 'reroll_reason', 'rerolled_at', 'rerolled_by'], rows, {'employee_id'}))
    elif report_type == 'summary':
        stats = get_stats()
        keys = ('total_employees', 'wristbands_issued', 'wristbands_not_issued', 'checked_in',
                'not_checked_in', 'attendance_percentage', 'distinct_verified', 'verification_failures',
                'lucky_participants', 'eligible_participants', 'total_winners')
        datasets.append(('Event Summary', ['metric', 'value'], [(key, stats[key]) for key in keys], set()))
    else:
        raise ValueError('Unknown report type.')
    return datasets


def xlsx_report_bytes(report_type):
    try:
        import openpyxl
    except ImportError:
        raise ValueError('Excel export needs: pip install openpyxl') from None
    allowed = ('master', 'wristbands', 'attendance', 'not_checked_in', 'verifications',
               'reconciliation', 'eligibility', 'winners', 'summary', 'all')
    if report_type not in allowed:
        raise ValueError('Unknown report type.')
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    requested = allowed[:-1] if report_type == 'all' else (report_type,)
    with db_connection() as db:
        datasets = []
        for kind in requested:
            datasets.extend(_report_datasets(db, kind))
    used_titles = set()
    for title, headers, rows, text_columns in datasets:
        base = re.sub(r'[:\\/?*\[\]]', ' ', title)[:31] or 'Report'
        sheet_title = base
        counter = 2
        while sheet_title in used_titles:
            suffix = ' ' + str(counter)
            sheet_title = base[:31 - len(suffix)] + suffix
            counter += 1
        used_titles.add(sheet_title)
        worksheet = workbook.create_sheet(sheet_title)
        worksheet.freeze_panes = 'A2'
        worksheet.append(headers)
        for cell in worksheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        for raw_row in rows:
            safe_row = [csv_safe(value) if isinstance(value, str) else value for value in raw_row]
            worksheet.append(safe_row)
            row_number = worksheet.max_row
            for index, header in enumerate(headers, start=1):
                if header in text_columns:
                    cell = worksheet.cell(row_number, index)
                    cell.value = '' if raw_row[index - 1] is None else csv_safe(str(raw_row[index - 1]))
                    cell.number_format = '@'
        worksheet.auto_filter.ref = ('A1:' + openpyxl.utils.get_column_letter(len(headers)) +
                                     str(max(1, worksheet.max_row)))
        for index, header in enumerate(headers, start=1):
            max_length = len(str(header))
            for cell in list(worksheet.columns)[index - 1][:200]:
                max_length = max(max_length, len(str(cell.value or '')))
            worksheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = min(50, max_length + 2)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class PartyServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


class PartyHandler(BaseHTTPRequestHandler):
    server_version = 'EventDesk/1.0'
    config = None

    def log_message(self, fmt, *args):
        # Paths could contain sensitive tokens if future routes change.
        message = (fmt % args).encode('ascii', 'backslashreplace').decode('ascii')
        print('HTTP', self.client_address[0], message)

    def send_bytes(self, data, content_type, status=200, filename=None, extra_headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Permissions-Policy',
                         'camera=(self), microphone=(), '
                         'autoplay=(self "https://www.youtube-nocookie.com")')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                         "connect-src 'self'; media-src 'self' blob:; worker-src 'self' blob:; "
                         "frame-src https://www.youtube-nocookie.com; "
                         "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def respond(self, data, status=200, headers=None):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode('utf-8'),
                        'application/json; charset=utf-8', status, extra_headers=headers)

    def fail(self, message, status=400):
        self.respond({'error': message}, status)

    def fail_code(self, message, code, status=400, extra=None):
        payload = {'error': message, 'code': code}
        if extra:
            payload.update(extra)
        self.respond(payload, status)

    def read_body(self):
        size = self.headers.get('Content-Length')
        try:
            length = int(size) if size is not None else -1
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            raise ValueError('Request is too large or missing Content-Length.')
        return self.rfile.read(length)

    def read_json(self):
        if not self.headers.get('Content-Type', '').lower().startswith('application/json'):
            raise ValueError('Expected application/json.')
        try:
            data = json.loads(self.read_body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Invalid JSON.') from None
        if not isinstance(data, dict):
            raise ValueError('Invalid request body.')
        return data

    def read_upload(self):
        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('multipart/form-data'):
            raise ValueError('Upload a CSV or Excel file.')
        body = self.read_body()
        raw = ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body
        message = BytesParser(policy=policy.default).parsebytes(raw)
        for part in message.iter_parts():
            if part.get_param('name', header='content-disposition') == 'file':
                filename = part.get_filename() or ''
                payload = part.get_payload(decode=True)
                if filename and payload:
                    return filename, payload
        raise ValueError('Choose a CSV or Excel file.')

    def get_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['party_session'].value if 'party_session' in cookie else ''
        except Exception:
            token = ''
        user = parse_session(token, self.config)
        is_legacy = (user and user.get('account_type') == 'legacy' and
                     user.get('username') in ('legacy-admin', 'legacy-staff'))
        if user and user.get('username') and not is_legacy:
            with db_connection() as db:
                current = db.execute('''SELECT username,display_name,role,active FROM users
                                        WHERE username=? COLLATE NOCASE''', (user['username'],)).fetchone()
            if not current or not current['active'] or current['role'] != user['role']:
                return None
            user['username'] = current['username']
            user['display_name'] = current['display_name']
        if user and user.get('sid'):
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec='seconds')
            with db_connection() as db:
                session = db.execute('SELECT ended_at FROM staff_sessions WHERE session_id=?', (user['sid'],)).fetchone()
                if not session or session['ended_at']:
                    return None
                db.execute('UPDATE staff_sessions SET last_seen=? WHERE session_id=? AND last_seen<?',
                           (now(), user['sid'], cutoff))
        return user

    def authorize(self, admin=False, modify=False, roles=None):
        user = self.get_user()
        if not user:
            self.fail('Please log in.', 401)
            return None
        if admin and user['role'] != 'admin':
            self.fail('Admin access required.', 403)
            return None
        if roles and user['role'] != 'admin':
            permitted = set(roles)
            if user['role'] == 'staff':
                legacy_roles = {'checkin_staff', 'verification_staff', 'lucky_draw_operator'}
                allowed = bool(permitted.intersection(legacy_roles))
            else:
                allowed = user['role'] in permitted
            if not allowed:
                self.fail('Your account does not have permission for this action.', 403)
                return None
        if modify:
            csrf = self.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(csrf, user['csrf']):
                self.fail('Security token expired. Refresh or sign in again.', 403)
                return None
            origin = self.headers.get('Origin')
            if origin:
                # Origin matching mitigates CSRF; behind a proxy configure PARTY_PUBLIC_ORIGIN.
                expected = os.environ.get('PARTY_PUBLIC_ORIGIN')
                if expected and origin.rstrip('/') != expected.rstrip('/'):
                    self.fail('Invalid request origin.', 403)
                    return None
        return user

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == '/' or path == '/index.html':
                return self.serve_static(FRONTEND / 'index.html')
            if path == '/favicon.ico':
                return self.send_bytes(b'', 'image/x-icon', 204)
            if path in ('/projector', '/projector/'):
                return self.serve_static(FRONTEND / 'projector.html')
            if path.startswith('/static/'):
                filename = path[len('/static/'):]
                if filename not in ('styles.css', 'app.js', 'theme-init.js', 'projector.css',
                                    'projector.js', 'vendor/html5-qrcode.min.js', 'vendor/jsQR.js',
                                    'camera-controls.js', 'qr-detail-scanner.js', 'qr-detail-worker.js',
                                    'wristband-support.js', 'employee-portrait.js',
                                    'fonts/fc-pride.otf',
                                    'assets/thai-temple-fair-background.png', 'assets/hometown-festival-background.png',
                                    'assets/childhood-hometown-background.png',
                                    'assets/tokin-logo.png'):
                    return self.fail('Not found.', 404)
                return self.serve_static(FRONTEND / filename)
            if path == '/api/me':
                user = self.get_user()
                event_name = get_setting('event_name', self.config.get('event_name', 'Company Party'))
                return self.respond({'authenticated': bool(user), 'role': user['role'] if user else None,
                                     'username': user.get('username') if user else None,
                                     'display_name': user.get('display_name') if user else None,
                                     'csrf': user['csrf'] if user else None, 'event_name': event_name})
            if path.startswith('/api/employee-photo/'):
                user = self.authorize(roles=('checkin_staff', 'verification_staff'))
                if not user:
                    return
                employee_id = path[len('/api/employee-photo/'):]
                if '?' in self.path or not re.fullmatch(r'[0-9]{7}', employee_id):
                    return self.fail('Invalid employee ID.', 400)
                if not photo_request_allowed(user):
                    return self.fail('Try again shortly.', 429)
                if not employee_photo_exists(employee_id):
                    return self.fail('No photo.', 404)
                try:
                    photo = fetch_employee_photo(employee_id)
                except EmployeePhotoError as error:
                    return self.fail(str(error), error.status)
                return self.send_bytes(photo, 'image/jpeg', extra_headers={'Vary': 'Cookie'})
            if path == '/api/stats':
                if not self.authorize(): return
                return self.respond(get_stats())
            if path == '/api/attendance':
                if not self.authorize(roles=('checkin_staff',)): return
                query = parse_qs(urlsplit(self.path).query)
                employee_id = query.get('employee_id', [''])[0].strip()[:50]
                status = query.get('status', ['all'])[0]
                if status not in ('all', 'checked', 'pending', 'lost', 'unreadable'):
                    return self.fail('Invalid attendance filter.', 400)
                try:
                    page = max(1, min(1000, int(query.get('page', ['1'])[0])))
                except ValueError:
                    page = 1
                clauses, params = [], []
                if employee_id:
                    escaped_id = employee_id.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    clauses.append("e.employee_id LIKE ? ESCAPE '\\'")
                    params.append('%' + escaped_id + '%')
                checked = '(c.employee_id IS NOT NULL OR e.checked_in_at IS NOT NULL)'
                if status == 'checked': clauses.append(checked)
                elif status == 'pending': clauses.append('NOT ' + checked)
                elif status in ('lost', 'unreadable'):
                    clauses.append(checked)
                    clauses.append('x.reason=?')
                    params.append(status)
                where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
                from_sql = ''' FROM employees e
                    LEFT JOIN event_checkins c ON c.employee_id=e.id
                    LEFT JOIN checkin_wristband_exceptions x ON x.employee_id=e.id'''
                with db_connection() as db:
                    total = db.execute('SELECT COUNT(*)' + from_sql + where, params).fetchone()[0]
                    items = [dict(row) for row in db.execute('''SELECT e.employee_id,e.name,e.department,
                        COALESCE(c.checked_in_at,e.checked_in_at) AS checked_in_at,
                        c.staff_username AS checked_in_by,x.reason AS wristband_reason,
                        x.recorded_by AS wristband_recorded_by''' + from_sql + where +
                        ' ORDER BY checked_in_at DESC,e.employee_id COLLATE NOCASE LIMIT 40 OFFSET ?',
                        params + [(page - 1) * 40])]
                return self.respond({'items': items, 'total': total, 'page': page, 'page_size': 40})
            if path == '/api/employees':
                if not self.authorize(admin=True): return
                query = parse_qs(urlsplit(self.path).query)
                term = query.get('q', [''])[0].strip()[:100]
                status = query.get('status', ['all'])[0]
                department = query.get('department', ['all'])[0].strip()[:160]
                wristband = query.get('wristband', ['all'])[0]
                sort = query.get('sort', ['employee_id'])[0]
                try:
                    page = max(1, min(1000, int(query.get('page', ['1'])[0])))
                except ValueError:
                    page = 1
                predicates, params = [], []
                if term:
                    predicates.append('(e.name LIKE ? OR e.employee_id LIKE ? OR e.department LIKE ? OR e.extra_json LIKE ?)')
                    params += [f'%{term}%'] * 4
                checked_predicate = "(e.checked_in_at IS NOT NULL OR EXISTS (SELECT 1 FROM event_checkins c WHERE c.employee_id=e.id))"
                verified_predicate = "EXISTS (SELECT 1 FROM identity_verification_history v WHERE v.employee_id=e.id AND v.result='verified')"
                if status == 'checkedin': predicates.append(checked_predicate)
                if status == 'verified': predicates.append(verified_predicate)
                if status in ('pending', 'not_checked_in'): predicates.append('NOT ' + checked_predicate)
                if status == 'verification_failed': predicates.append("EXISTS (SELECT 1 FROM identity_verification_history v WHERE v.employee_id=e.id AND v.result<>'verified')")
                if status == 'lucky_participant': predicates.append('EXISTS (SELECT 1 FROM lucky_draw_participants p WHERE p.employee_id=e.id AND p.active=1)')
                if status == 'winner': predicates.append("EXISTS (SELECT 1 FROM lucky_draw_winners lw WHERE lw.employee_id=e.id AND lw.status='active')")
                with db_connection() as db:
                    eligible_predicate, eligible_params = _eligible_sql(db)
                    if status == 'eligible': predicates.append(eligible_predicate); params.extend(eligible_params)
                    if department and department != 'all': predicates.append('e.department=?'); params.append(department)
                    if wristband in ('not_issued', 'issued', 'replaced', 'revoked'):
                        predicates.append('(SELECT wb.status FROM wristbands wb WHERE wb.employee_id=e.id ORDER BY wb.id DESC LIMIT 1)=?')
                        params.append(wristband)
                    where = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
                    order_by = {
                        'employee_id': 'e.employee_id COLLATE NOCASE', 'name': 'e.name COLLATE NOCASE',
                        'department': 'e.department COLLATE NOCASE,e.name COLLATE NOCASE',
                        'checkin_desc': 'checked_in_at DESC,e.employee_id COLLATE NOCASE'
                    }.get(sort, 'e.employee_id COLLATE NOCASE')
                    count = db.execute('SELECT COUNT(*) FROM employees e ' + where, params).fetchone()[0]
                    items = [dict(r) for r in db.execute('''
                        SELECT e.employee_id,e.name,e.department,e.extra_json,
                               COALESCE((SELECT c.checked_in_at FROM event_checkins c WHERE c.employee_id=e.id),e.checked_in_at) AS checked_in_at,
                               COALESCE((SELECT MIN(v.verified_at) FROM identity_verification_history v
                                         WHERE v.employee_id=e.id AND v.result='verified'),e.verified_at) AS verified_at,
                               (SELECT lw.prize_name FROM lucky_draw_winners lw WHERE lw.employee_id=e.id
                                AND lw.status='active' ORDER BY lw.id DESC LIMIT 1) AS prize,
                               (SELECT wb.status FROM wristbands wb WHERE wb.employee_id=e.id ORDER BY wb.id DESC LIMIT 1) AS wristband_status,
                               EXISTS (SELECT 1 FROM lucky_draw_participants p WHERE p.employee_id=e.id AND p.active=1) AS lucky_participant,
                               CASE WHEN ''' + eligible_predicate + ''' THEN 1 ELSE 0 END AS draw_eligible,
                               (SELECT v.result FROM identity_verification_history v WHERE v.employee_id=e.id ORDER BY v.id DESC LIMIT 1) AS latest_verification_result
                        FROM employees e ''' + where +
                        ' ORDER BY ' + order_by + ' LIMIT 40 OFFSET ?', eligible_params + params + [(page - 1) * 40])]
                    departments = [row[0] for row in db.execute("SELECT DISTINCT department FROM employees WHERE TRIM(COALESCE(department,''))<>'' ORDER BY department COLLATE NOCASE")]
                for item in items:
                    try: item['extra'] = json.loads(item.pop('extra_json') or '{}')
                    except (json.JSONDecodeError, TypeError): item['extra'] = {}
                return self.respond({'items': items, 'total': count, 'page': page, 'page_size': 40,
                                     'departments': departments})
            if path == '/api/wristbands':
                if not self.authorize(admin=True): return
                query = parse_qs(urlsplit(self.path).query)
                term = query.get('q', [''])[0].strip()[:100]
                status = query.get('status', ['all'])[0]
                try: page = max(1, int(query.get('page', ['1'])[0]))
                except ValueError: page = 1
                clauses, params = [], []
                if term:
                    clauses.append('(e.employee_id LIKE ? OR e.name LIKE ? OR e.department LIKE ?)')
                    params += [f'%{term}%'] * 3
                if status in ('not_issued', 'issued', 'replaced', 'revoked'):
                    clauses.append('w.status=?'); params.append(status)
                where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
                with db_connection() as db:
                    counts = {row['status']: row['count'] for row in db.execute(
                        'SELECT status,COUNT(*) AS count FROM wristbands GROUP BY status')}
                    total = db.execute('SELECT COUNT(*) FROM wristbands w JOIN employees e ON e.id=w.employee_id' + where,
                                       params).fetchone()[0]
                    items = [dict(row) for row in db.execute('''SELECT w.id AS wristband_id,e.employee_id,e.name,
                        e.department,w.status,w.created_at,w.issued_at,w.issued_by,w.invalidated_at,w.reason
                        FROM wristbands w JOIN employees e ON e.id=w.employee_id''' + where +
                        ' ORDER BY e.employee_id,w.id DESC LIMIT 50 OFFSET ?', params + [(page - 1) * 50])]
                return self.respond({'items': items, 'total': total, 'counts': counts,
                                     'page': page, 'page_size': 50})
            if path == '/api/wristbands/qr':
                if not self.authorize(admin=True): return
                employee_id = parse_qs(urlsplit(self.path).query).get('employee_id', [''])[0]
                return self.send_bytes(wristband_qr_png(employee_id), 'image/png')
            if path == '/api/import/history':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    items = []
                    for row in db.execute('SELECT * FROM import_history ORDER BY id DESC LIMIT 100'):
                        item = dict(row)
                        for key in ('mapping_json', 'summary_json', 'reconciliation_json'):
                            try: item[key[:-5]] = json.loads(item.pop(key) or '{}')
                            except (json.JSONDecodeError, TypeError): item[key[:-5]] = {}
                        items.append(item)
                return self.respond({'items': items, 'total': len(items)})
            if path == '/api/draw/config':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                with db_connection() as db:
                    prizes = [dict(row) for row in db.execute('''SELECT p.*,
                        (SELECT COUNT(*) FROM lucky_draw_winners w WHERE w.prize_id=p.id AND w.status='active') AS drawn
                        FROM prizes p ORDER BY p.active DESC,p.id''')]
                    for prize in prizes:
                        prize['remaining'] = max(0, prize['quantity'] - prize['drawn'])
                    rounds = [dict(row) for row in db.execute('''SELECT r.*,p.name AS prize_name
                        FROM lucky_draw_rounds r LEFT JOIN prizes p ON p.id=r.prize_id ORDER BY r.id DESC LIMIT 100''')]
                    settings = {key: get_setting(key, value, db) for key, value in DEFAULT_SETTINGS.items()}
                return self.respond({'prizes': prizes, 'rounds': rounds, 'settings': settings})
            if path == '/api/draw/eligible-ids':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                with db_connection() as db:
                    if not bool(get_setting('projector_show_employee_id', True, db)):
                        return self.respond({'items': []})
                    predicate, params = _eligible_sql(db)
                    ids = [row[0] for row in db.execute(
                        'SELECT e.employee_id FROM employees e WHERE ' + predicate + ' ORDER BY e.employee_id COLLATE NOCASE', params)]
                return self.respond({'items': ids})
            if path == '/api/rewards':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                with db_connection() as db:
                    drafts = [dict(row) for row in db.execute('''SELECT d.id,d.source_id,d.name,d.quantity,d.action,
                        d.image_changed, CASE WHEN d.image_changed THEN d.image IS NOT NULL ELSE
                        EXISTS(SELECT 1 FROM reward_images i WHERE i.prize_id=d.source_id) END AS has_image
                        FROM reward_drafts d ORDER BY d.id''')]
                    prizes = [dict(row) for row in db.execute('''SELECT p.id,p.name,p.quantity,
                        EXISTS(SELECT 1 FROM reward_images i WHERE i.prize_id=p.id) AS has_image,
                        (SELECT COUNT(*) FROM lucky_draw_winners w WHERE w.prize_id=p.id AND w.status='active') AS drawn
                        FROM prizes p WHERE p.active=1 ORDER BY p.id''')]
                return self.respond({'drafts': drafts, 'prizes': prizes})
            if path == '/api/rewards/image':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                params = parse_qs(urlsplit(self.path).query)
                with db_connection() as db:
                    if 'draft' in params:
                        draft = db.execute('SELECT * FROM reward_drafts WHERE id=?', (params['draft'][0],)).fetchone()
                        if draft and draft['image_changed']:
                            row = {'content_type': draft['content_type'], 'data': draft['image']} if draft['image'] else None
                        else:
                            row = db.execute('SELECT content_type,data FROM reward_images WHERE prize_id=?',
                                             (draft['source_id'] if draft else None,)).fetchone()
                    else:
                        row = db.execute('SELECT content_type,data FROM reward_images WHERE prize_id=?',
                                         (params.get('id', [''])[0],)).fetchone()
                if not row: return self.fail('Reward image not found.', 404)
                return self.send_bytes(row['data'], row['content_type'])
            if path == '/api/projector/state':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                state = get_projector_state()
                if state['event_name'] == 'Company Party':
                    state['event_name'] = self.config.get('event_name', 'Company Party')
                return self.respond(state)
            if path == '/api/projector/prize-image':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                metadata, payload = get_projector_prize_image()
                if not metadata or payload is None:
                    return self.fail('Prize image not found.', 404)
                return self.send_bytes(payload, metadata['content_type'])
            if path == '/api/projector/events':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                return self.stream_projector_events()
            if path == '/api/winners':
                if not self.authorize(roles=('lucky_draw_operator',)): return
                with db_connection() as db:
                    items = [dict(row) for row in db.execute('''SELECT w.id AS winner_id,e.employee_id,e.name,e.department,
                        w.prize_id,w.prize_name AS prize,r.round_number,r.label AS round_name,
                        w.status,w.won_at AS drawn_at,w.won_by AS drawn_by,
                        w.reroll_reason,w.rerolled_at,w.rerolled_by
                        FROM lucky_draw_winners w JOIN employees e ON e.id=w.employee_id
                        JOIN lucky_draw_rounds r ON r.id=w.round_id ORDER BY w.id DESC''')]
                return self.respond({'items': items, 'total': len(items)})
            if path == '/api/settings':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    settings = {key: get_setting(key, value, db) for key, value in DEFAULT_SETTINGS.items()}
                    settings['event_name'] = get_setting('event_name', self.config.get('event_name', 'Company Party'), db)
                return self.respond(settings)
            if path == '/api/users':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    items = [dict(row) for row in db.execute('''SELECT id,username,display_name,role,active,created_at,updated_at
                                                                 FROM users ORDER BY username COLLATE NOCASE''')]
                for item in items: item['active'] = bool(item['active'])
                return self.respond({'items': items, 'total': len(items), 'legacy_accounts': ['admin', 'staff']})
            if path == '/api/admin/draw-history':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    count = db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0]
                return self.respond({'count':count})
            if path == '/api/admin/activity':
                if not self.authorize(admin=True): return
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(timespec='seconds')
                with db_connection() as db:
                    rows = db.execute('''SELECT username,display_name,role,MAX(last_seen) AS last_seen,
                           COUNT(*) AS sessions FROM staff_sessions
                           WHERE ended_at IS NULL AND expires_at>? AND last_seen>=?
                           GROUP BY username,display_name,role ORDER BY last_seen DESC''',
                           (int(time.time()), cutoff)).fetchall()
                return self.respond({'items': [dict(row) for row in rows], 'active_accounts': len(rows),
                                     'window_seconds': 90})
            if path == '/api/verifications':
                if not self.authorize(roles=('verification_staff',)): return
                query = parse_qs(urlsplit(self.path).query)
                employee_id = query.get('employee_id', [''])[0].strip()
                where, params = (' WHERE e.employee_id=?', [employee_id]) if employee_id else ('', [])
                with db_connection() as db:
                    items = [dict(row) for row in db.execute('''SELECT v.id,e.employee_id,e.name,e.department,v.result,
                        v.reason,v.verified_at,v.staff_username AS verified_by
                        FROM identity_verification_history v LEFT JOIN employees e ON e.id=v.employee_id''' + where +
                        ' ORDER BY v.id DESC LIMIT 250', params)]
                return self.respond({'items': items, 'total': len(items)})
            if path == '/api/export/attendance':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    rows = [tuple(r) for r in db.execute('''
                        SELECT e.employee_id,e.name,e.department,COALESCE(c.checked_in_at,e.checked_in_at),
                               COALESCE((SELECT MIN(v.verified_at) FROM identity_verification_history v
                                         WHERE v.employee_id=e.id AND v.result='verified'),e.verified_at),
                               COALESCE((SELECT lw.prize_name FROM lucky_draw_winners lw WHERE lw.employee_id=e.id
                                         AND lw.status='active' ORDER BY lw.id DESC LIMIT 1),'')
                        FROM employees e LEFT JOIN event_checkins c ON c.employee_id=e.id ORDER BY e.employee_id
                    ''')]
                return self.send_bytes(make_csv(['employee_id','name','department','checked_in_at','verified_at','prize'],rows),
                                       'text/csv; charset=utf-8', filename='attendance.csv')
            if path == '/api/export/winners':
                if not self.authorize(admin=True): return
                with db_connection() as db:
                    rows = [tuple(r) for r in db.execute('''
                        SELECT e.employee_id,e.name,e.department,w.prize_name,w.won_at
                        FROM lucky_draw_winners w JOIN employees e ON e.id=w.employee_id
                        WHERE w.status='active' ORDER BY w.id
                    ''')]
                return self.send_bytes(make_csv(['employee_id','name','department','prize','drawn_at'],rows),
                                       'text/csv; charset=utf-8', filename='winners.csv')
            if path == '/api/export/qr':
                if not self.authorize(admin=True): return
                return self.send_bytes(qr_zip_bytes(), 'application/zip', filename='employee_qr_codes.zip')
            if path == '/api/export/report':
                if not self.authorize(admin=True): return
                report_type = parse_qs(urlsplit(self.path).query).get('type', [''])[0]
                return self.send_bytes(xlsx_report_bytes(report_type),
                                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                       filename='event_' + report_type + '.xlsx')
            return self.fail('Not found.', 404)
        except ValueError as exc:
            return self.fail(str(exc))
        except Exception:
            self.log_error('Unexpected GET error at %s', path)
            import traceback; traceback.print_exc()
            return self.fail('Server error. Check the server log.', 500)

    def serve_static(self, filename):
        if not filename.is_file():
            return self.fail('Not found.', 404)
        raw = filename.read_bytes()
        content_type = {
            '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
            '.png': 'image/png', '.otf': 'font/otf',
        }.get(filename.suffix, 'text/javascript; charset=utf-8')
        return self.send_bytes(raw, content_type)

    def stream_projector_events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        last_signature = None
        try:
            for tick in range(240):
                state = get_projector_state()
                if state['event_name'] == 'Company Party':
                    state['event_name'] = self.config.get('event_name', 'Company Party')
                signature = (state['sequence'], state['phase'])
                if signature != last_signature:
                    payload = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
                    self.wfile.write(('id: ' + str(state['sequence']) + '\n').encode('utf-8'))
                    self.wfile.write(('event: draw-state\ndata: ' + payload + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                    last_signature = signature
                elif tick % 20 == 0:
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                time.sleep(.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            if path == '/api/staff-session':
                if os.environ.get('PARTY_OPEN_STAFF') != '1':
                    return self.fail('Direct staff access is not enabled.', 403)
                origin = self.headers.get('Origin')
                if origin:
                    expected = os.environ.get('PARTY_PUBLIC_ORIGIN')
                    if expected:
                        valid_origin = origin.rstrip('/') == expected.rstrip('/')
                    else:
                        parsed_origin = urlsplit(origin)
                        valid_origin = parsed_origin.scheme in ('http', 'https') and parsed_origin.netloc == self.headers.get('Host')
                    if not valid_origin:
                        return self.fail('Invalid request origin.', 403)
                if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                    return self.fail('Invalid request origin.', 403)
                token, user = sign_session('staff', self.config, 'legacy-staff', 'Event Staff', 'legacy')
                with db_connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('''INSERT INTO staff_sessions(session_id,username,display_name,role,last_seen,expires_at)
                                  VALUES (?,?,?,?,?,?)''',
                               (user['sid'], user['username'], user['display_name'], user['role'], now(), user['exp']))
                    audit(db, user, 'auth.staff_direct', 'session')
                    db.commit()
                secure = '; Secure' if os.environ.get('PARTY_COOKIE_SECURE', '0') == '1' else ''
                cookie = 'party_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200' + secure
                return self.respond({'authenticated': True, 'role': 'staff', 'username': user['username'],
                                     'display_name': user['display_name'], 'csrf': user['csrf'],
                                     'event_name': get_setting('event_name', self.config.get('event_name', 'Company Party'))},
                                    headers={'Set-Cookie': cookie})
            if path == '/api/login':
                data = self.read_json()
                password = data.get('password', '')
                requested_username = str(data.get('username') or '').strip()
                admin_only = data.get('admin_only') is True
                ip = self.client_address[0]
                attempt_key = ip + '|' + requested_username.casefold()
                with LOGIN_LOCK:
                    recent = [t for t in LOGIN_ATTEMPTS.get(attempt_key, []) if time.time() - t < 600]
                    LOGIN_ATTEMPTS[attempt_key] = recent
                    if len(recent) >= 20:
                        return self.fail('Too many login attempts. Try again later.', 429)
                role = username = display_name = None
                if requested_username:
                    with db_connection() as db:
                        named = db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1',
                                           (requested_username,)).fetchone()
                    if named and check_password(password, {'salt': named['password_salt'], 'hash': named['password_hash']}):
                        role, username, display_name = named['role'], named['username'], named['display_name']
                else:
                    if check_password(password, self.config['admin_password']):
                        role, username, display_name = 'admin', 'legacy-admin', 'Administrator'
                    elif not admin_only and check_password(password, self.config['staff_password']):
                        role, username, display_name = 'staff', 'legacy-staff', 'Event Staff'
                if admin_only and role != 'admin':
                    role = None
                if not role:
                    with LOGIN_LOCK:
                        LOGIN_ATTEMPTS.setdefault(attempt_key, []).append(time.time())
                    return self.fail('Incorrect username or password.', 401)
                with LOGIN_LOCK:
                    LOGIN_ATTEMPTS.pop(attempt_key, None)
                account_type = 'named' if requested_username else 'legacy'
                token, user = sign_session(role, self.config, username, display_name, account_type)
                with db_connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('''INSERT INTO staff_sessions(session_id,username,display_name,role,last_seen,expires_at)
                                  VALUES (?,?,?,?,?,?)''',
                               (user['sid'], username, display_name, role, now(), user['exp']))
                    audit(db, user, 'auth.login', 'session', None, {'ip': ip})
                    db.commit()
                secure = '; Secure' if os.environ.get('PARTY_COOKIE_SECURE', '0') == '1' else ''
                cookie = 'party_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200' + secure
                return self.respond({'role': role, 'username': username, 'display_name': display_name,
                                     'csrf': user['csrf']}, headers={'Set-Cookie': cookie})
            if path == '/api/logout':
                user = self.authorize(modify=True)
                if not user: return
                with db_connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if user.get('sid'):
                        db.execute('UPDATE staff_sessions SET ended_at=? WHERE session_id=?', (now(), user['sid']))
                    audit(db, user, 'auth.logout', 'session')
                    db.commit()
                secure = '; Secure' if os.environ.get('PARTY_COOKIE_SECURE', '0') == '1' else ''
                return self.respond({'ok': True}, headers={'Set-Cookie':
                    'party_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0' + secure})
            if path == '/api/lookup':
                if not self.authorize(modify=True, roles=('checkin_staff', 'verification_staff')): return
                data = self.read_json()
                scanned_code = str(data.get('code') or '').strip()
                # Company ID badges in the supplied employee data encode the numeric
                # employee ID. Treat those as the existing authorized lookup fallback;
                # all other QR values must still pass wristband-token validation.
                badge_employee_id = scanned_code if re.fullmatch(r'\d{4,7}', scanned_code) else None
                requested_employee_id = data.get('employee_id') or badge_employee_id
                with db_connection() as db:
                    if requested_employee_id:
                        employee = find_employee_by_external_id(db, requested_employee_id)
                        if not employee: return self.fail_code('Employee not found.', 'employee_not_found', 404)
                        wristband = active_wristband(db, employee['id'])
                        person = employee_data(employee)
                        wristband_data = {'id': wristband['id'], 'status': wristband['status']} if wristband else None
                        effective_lookup = {'code': scanned_code} if badge_employee_id else {'employee_id': person['employee_id']}
                    else:
                        code = normalize_token(scanned_code)
                        resolved = resolve_wristband(db, code)
                        if not resolved: return self.fail_code('QR code not found. Check the ticket.', 'unknown_token', 404)
                        if resolved['wristband_status'] not in WRISTBAND_ACTIVE_STATUSES:
                            return self.fail_code('This wristband is ' + resolved['wristband_status'] + '.',
                                                  resolved['wristband_status'] + '_wristband', 409,
                                                  {'person': employee_data(resolved)})
                        person = employee_data(resolved)
                        wristband_data = {'id': resolved['wristband_id'], 'status': resolved['wristband_status']}
                        effective_lookup = {'code': scanned_code}
                    exception = db.execute('SELECT reason FROM checkin_wristband_exceptions WHERE employee_id=?',
                                           (person['id'],)).fetchone()
                    person['checkin_wristband_reason'] = exception['reason'] if exception else None
                lookup_source = ('id_badge' if badge_employee_id else
                                 ('employee_id' if data.get('employee_id') else 'wristband'))
                return self.respond({'person': person, 'wristband': wristband_data,
                                     'lookup': effective_lookup, 'lookup_source': lookup_source})
            if path == '/api/scan/checkin':
                user = self.authorize(modify=True, roles=('checkin_staff',))
                if not user: return
                data = self.read_json()
                scanned_code = str(data.get('code') or '').strip()
                badge_employee_id = scanned_code if re.fullmatch(r'\d{4,7}', scanned_code) else None
                manual_employee_id = data.get('employee_id')
                wristband_reason = data.get('wristband_reason')
                wristband_detail = str(data.get('wristband_detail') or '').strip()
                if manual_employee_id and wristband_reason not in ('lost', 'unreadable', 'other'):
                    return self.fail('Select why the employee ID is being used.', 400)
                if wristband_reason == 'other' and (not wristband_detail or len(wristband_detail) > 200):
                    return self.fail('Enter a short reason for Other (up to 200 characters).', 400)
                if wristband_reason != 'other' and wristband_detail:
                    return self.fail('A reason is only required for Other.', 400)
                if not manual_employee_id and wristband_reason:
                    return self.fail('Wristband reason is only used for manual check-in.', 400)
                if manual_employee_id or badge_employee_id:
                    with db_connection() as db:
                        employee = find_employee_by_external_id(db, manual_employee_id or badge_employee_id)
                        wristband = active_wristband(db, employee['id']) if employee else None
                    if not employee: return self.fail_code('Employee not found.', 'employee_not_found', 404)
                    if not wristband: return self.fail_code('Employee has no active wristband.', 'no_active_wristband', 409)
                    code = wristband['token']
                else:
                    code = normalize_token(data.get('code'))
                person, state = scan_action(code, 'checkin', user, wristband_reason=wristband_reason,
                                            wristband_detail=wristband_detail)
                if state == 'not_found': return self.fail_code('QR code not found.', 'unknown_token', 404)
                if state in ('revoked', 'replaced'):
                    return self.fail_code('This wristband is ' + state + '.', state + '_wristband', 409,
                                          {'person': person, 'state': state})
                return self.respond({'person': person, 'state': state,
                                     'checkin': {'checked_in_at': person.get('checked_in_at')}})
            if path == '/api/checkin/wristband-lost':
                user = self.authorize(modify=True, roles=('checkin_staff',))
                if not user: return
                data = self.read_json()
                person, state = mark_checked_in_wristband_lost(data.get('employee_id'), user)
                if state == 'not_found':
                    return self.fail_code('Employee not found.', 'employee_not_found', 404)
                if state == 'checkin_required':
                    return self.fail_code('This employee has not checked in yet.', 'checkin_required', 409)
                return self.respond({'person': person, 'state': state})
            if path == '/api/scan/verify':
                user = self.authorize(modify=True, roles=('verification_staff',))
                if not user: return
                data = self.read_json()
                legacy_request = 'result' not in data and 'confirmed_match' in data
                if legacy_request and data.get('confirmed_match') is not True:
                    return self.fail('Staff must visually confirm the physical ID or badge.', 400)
                result = 'verified' if legacy_request else data.get('result')
                person, verification = record_verification(data.get('code'), result, data.get('reason'), user,
                                                           data.get('employee_id'))
                actual = verification['result']
                if actual == 'invalid_wristband':
                    return self.fail_code('QR code is unknown, revoked, or replaced.', 'invalid_wristband', 409,
                                          {'person': person, 'verification': verification,
                                           'state': 'invalid_wristband'})
                if actual == 'checkin_required':
                    return self.respond({'person': person, 'verification': verification,
                                         'state': 'checkin_required'}, 409)
                return self.respond({'person': person, 'verification': verification,
                                     'state': 'done' if legacy_request else 'recorded'})
            if path == '/api/draw/participants':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                data = self.read_json()
                person, state = add_lucky_participant(data.get('employee_id'), user)
                if not person:
                    return self.fail_code('Employee ID was not found.', 'employee_not_found', 404)
                return self.respond({'person': person, 'state': state})
            if path == '/api/rewards/draft':
                if not self.authorize(modify=True, roles=('lucky_draw_operator',)): return
                return self.respond(save_reward_draft(self.read_json()))
            if path == '/api/rewards/draft/delete':
                if not self.authorize(modify=True, roles=('lucky_draw_operator',)): return
                data = self.read_json()
                with db_connection() as db:
                    db.execute('DELETE FROM reward_drafts WHERE id=?', (data.get('id'),))
                    db.commit()
                return self.respond({'ok': True})
            if path == '/api/rewards/publish':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                return self.respond(publish_reward_drafts(self.read_json(), user))
            if path == '/api/draw':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                data = self.read_json()
                prize_id = data.get('prize_id')
                round_name = data.get('round_name')
                if prize_id is not None:
                    winner, state = draw_configured_prize(prize_id, user, round_name, prevent_overlap=True)
                else:
                    winner = draw_winner(data.get('prize'), user, round_name)
                    state = 'drawn' if winner else 'no_eligible'
                if winner is None:
                    message = {'prize_exhausted':'Prize quantity has already been fully drawn.',
                               'draw_in_progress':'A draw is already in progress. Wait for the winner.'}.get(state, 'No eligible employees are available.')
                    return self.fail_code(message, state, 409)
                return self.respond({'winner': winner})
            if path == '/api/projector/state':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                data = self.read_json()
                phase = str(data.get('phase') or '').strip()
                if phase not in ('waiting', 'preparing', 'ready_for_next', 'winner_announced'):
                    return self.fail('Unsupported projector state.', 400)
                with db_connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if get_projector_state(db)['phase'] in ('spinning', 'winner_selected'):
                        db.rollback()
                        return self.fail_code('A draw is already in progress. Wait for the winner.', 'draw_in_progress', 409)
                    set_projector_state(db, phase, user, prize=data.get('prize'),
                                        round_name=data.get('round_name'))
                    audit(db, user, 'projector.state_changed', 'presentation', 1, {'phase': phase})
                    db.commit()
                state = get_projector_state()
                if state['event_name'] == 'Company Party':
                    state['event_name'] = self.config.get('event_name', 'Company Party')
                return self.respond(state)
            if path == '/api/projector/prize-image':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                metadata = update_projector_prize_image(self.read_json(), user)
                state = get_projector_state()
                if state['event_name'] == 'Company Party':
                    state['event_name'] = self.config.get('event_name', 'Company Party')
                return self.respond({'image': metadata, 'state': state})
            if path == '/api/winners/reroll':
                user = self.authorize(modify=True, roles=('lucky_draw_operator',))
                if not user: return
                data = self.read_json()
                return self.respond(reroll_winner(data.get('winner_id'), data.get('reason'), user))
            if path == '/api/wristbands/generate':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(generate_wristbands(self.read_json().get('employee_ids'), user))
            if path == '/api/wristbands/action':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                data = self.read_json()
                return self.respond(change_wristband(data.get('employee_id'), data.get('action'), user,
                                                      data.get('reason')))
            if path == '/api/prizes':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(save_prize(self.read_json(), user))
            if path == '/api/settings':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(update_settings(self.read_json(), user))
            if path == '/api/users':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(create_user(self.read_json(), user), 201)
            if path == '/api/users/update':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(update_user(self.read_json(), user))
            if path == '/api/admin/employees':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(add_employee(self.read_json(), user), 201)
            if path == '/api/admin/attendance/remove':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(remove_attendance(self.read_json(), user))
            if path == '/api/admin/draw-history/remove':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(remove_draw_history(self.read_json(), user))
            if path == '/api/import/inspect':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                filename, payload = self.read_upload()
                return self.respond(inspect_import(filename, payload, user))
            if path == '/api/import/preview':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                data = self.read_json()
                return self.respond(build_import_preview(data.get('upload_id'), data, user))
            if path == '/api/import/commit':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                return self.respond(commit_import(self.read_json().get('upload_id'), user))
            if path == '/api/import':
                user = self.authorize(admin=True, modify=True)
                if not user: return
                filename, payload = self.read_upload()
                return self.respond(import_employees(get_import_rows(filename, payload), user))
            return self.fail('Not found.', 404)
        except ValueError as exc:
            return self.fail(str(exc))
        except Exception:
            self.log_error('Unexpected POST error at %s', path)
            import traceback; traceback.print_exc()
            return self.fail('Server error. Check the server log.', 500)


def make_server(host='127.0.0.1', port=8080, config=None, cert_file=None, key_file=None):
    PartyHandler.config = config or load_config()
    init_db()
    if DB_PATH.resolve() == DEFAULT_DB_PATH.resolve():
        seed_employees_from_json()
    server = PartyServer((host, port), PartyHandler)
    if cert_file or key_file:
        if not cert_file or not key_file:
            server.server_close()
            raise ValueError('Both PARTY_CERT_FILE and PARTY_KEY_FILE are required for HTTPS.')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main():
    if not CONFIG_PATH.exists():
        print('First-time setup required. Run: python setup.py')
        raise SystemExit(1)
    # Listen on every network interface by default so event staff can open the
    # workspace from other devices on the same trusted LAN. Set PARTY_HOST to
    # 127.0.0.1 when running exclusively behind a local reverse proxy.
    host = os.environ.get('PARTY_HOST', '0.0.0.0')
    port = int(os.environ.get('PARTY_PORT', '8080'))
    default_cert = ROOT / 'data' / 'lan-server-cert.pem'
    default_key = ROOT / 'data' / 'lan-server-key.pem'
    cert_file = Path(os.environ.get('PARTY_CERT_FILE', default_cert))
    key_file = Path(os.environ.get('PARTY_KEY_FILE', default_key))
    use_https = cert_file.exists() and key_file.exists()
    server = make_server(host, port, cert_file=cert_file if use_https else None,
                         key_file=key_file if use_https else None)
    scheme = 'https' if use_https else 'http'
    print(f'EventDesk listening on {scheme}://{host}:{port}')
    if not use_https:
        print('For mobile camera scanning, enable HTTPS. See README.md.')
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print('\nStopping...')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
