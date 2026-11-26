"""Preflight signed claims before consuming durable intent storage.

This is a resource guard, not a replacement for the SQL backend's current-user,
session, CSRF and role checks. It never queries old Cloudflare business tables.
"""
import hmac
from http.cookies import CookieError, SimpleCookie
import re
from urllib.parse import urlsplit


PUBLIC_POSTS = frozenset(('/api/login', '/api/staff-session'))
CHECKIN = frozenset(('/api/scan/checkin', '/api/checkin/wristband-lost'))
VERIFY = frozenset(('/api/scan/verify',))
DRAW = frozenset('/api/' + path for path in (
    'draw/participants', 'rewards/draft', 'rewards/draft/delete', 'rewards/publish', 'rewards/reorder',
    'draw', 'projector/state', 'projector/prize-image', 'winners/reroll', 'winners/remove'))
ADMIN = frozenset('/api/' + path for path in (
    'wristbands/generate', 'wristbands/action', 'prizes', 'settings', 'users', 'users/update',
    'admin/employees', 'admin/attendance/remove', 'admin/draw-history/remove', 'winners/history/clear',
    'import/inspect', 'import/preview', 'import/commit', 'import'))
AUTHENTICATED = frozenset(('/api/logout', '/api/lookup'))


def preflight_mutation(envelope, config, origin, parse_session):
    if envelope['method'] != 'POST':
        return None
    path = urlsplit(envelope['path']).path
    if path in PUBLIC_POSTS:
        return None
    if path not in CHECKIN | VERIFY | DRAW | ADMIN | AUTHENTICATED:
        return 404, 'Not found.'
    if (not isinstance(config, dict) or not isinstance(config.get('secret'), str)
            or not re.fullmatch(r'[0-9a-fA-F]{64}', config['secret'])):
        return 503, 'The SQL connection needs attention.'
    headers = {name.lower(): value for name, value in envelope['headers']}
    raw_cookie = headers.get('cookie', '')
    if not isinstance(raw_cookie, str) or len(raw_cookie) > 16384:
        return 401, 'Please log in.'
    try:
        cookie = SimpleCookie(raw_cookie)
        token = cookie['party_session'].value
        user = parse_session(token, config)
    except (CookieError, KeyError, ValueError, TypeError):
        user = None
    if not user:
        return 401, 'Please log in.'
    csrf, expected = headers.get('x-csrf-token', ''), user.get('csrf', '')
    if (not isinstance(csrf, str) or not isinstance(expected, str)
            or not hmac.compare_digest(csrf.encode('utf-8'), expected.encode('utf-8'))):
        return 403, 'Security token expired. Refresh or sign in again.'
    if headers.get('origin') and headers['origin'] != origin.rstrip('/'):
        return 403, 'This request came from another website.'
    role = user['role']
    if role == 'admin':
        return None
    if path in ADMIN:
        return 403, 'Admin access required.'
    permitted = ({'checkin_staff'} if path in CHECKIN else {'verification_staff'} if path in VERIFY
                 else {'lucky_draw_operator'} if path in DRAW
                 else {'checkin_staff', 'verification_staff'} if path == '/api/lookup' else {role})
    if role != 'staff' and role not in permitted:
        return 403, 'Your account does not have permission for this action.'
    return None
