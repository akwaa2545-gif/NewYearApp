"""Replace filesystem-backed projector images with Durable Object SQL storage."""
import base64
import binascii
import hashlib
from pathlib import Path
import re


def _ensure_images(db):
    db.execute('''CREATE TABLE IF NOT EXISTS _eventdesk_projector_images (
        image_id TEXT PRIMARY KEY, content_type TEXT NOT NULL, image BLOB NOT NULL
    ) WITHOUT ROWID''')


def _image_id(metadata):
    image_id = metadata.get('id') if isinstance(metadata, dict) else None
    return image_id if isinstance(image_id, str) and re.fullmatch(r'[0-9a-f]{64}', image_id) else None


def _parse_image(app, data):
    if not isinstance(data, dict):
        raise ValueError('Invalid prize image request.')
    if data.get('clear'):
        return None, None
    match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)',
                         str(data.get('data_url') or ''))
    if not match:
        raise ValueError('Choose a PNG, JPEG, or WebP image.')
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('The prize image is not valid.') from None
    if not payload or len(payload) > app.PRIZE_IMAGE_MAX_BYTES:
        raise ValueError('Prize image must be 4 MB or smaller.')
    content_type = app._detect_prize_image_type(payload)
    if content_type != match.group(1):
        raise ValueError('The prize image content does not match its file type.')
    metadata = {
        'id': hashlib.sha256(payload).hexdigest(), 'content_type': content_type,
        'filename': Path(str(data.get('filename') or 'prize-image')).name[:120],
        'size': len(payload), 'updated_at': app.now(),
    }
    return metadata, payload


def install(app):
    """Install replacements after assigning the hosted app.db_connection factory.

    The SQL adapter splits large BLOBs into bounded chunks. The hosted request
    owner remains responsible for committing or rolling back the outer Durable
    Object transaction, including all image, setting, sequence, and audit writes.
    """
    def get_projector_prize_image(db=None):
        if db is None:
            with app.db_connection() as connection:
                return get_projector_prize_image(connection)
        metadata = app.get_setting('projector_prize_image', None, db)
        image_id = _image_id(metadata)
        if not image_id:
            return None, None
        _ensure_images(db)
        row = db.execute('''SELECT image, content_type FROM _eventdesk_projector_images
                            WHERE image_id=?''', (image_id,)).fetchone()
        if not row:
            return None, None
        payload = bytes(row['image'])
        content_type = app._detect_prize_image_type(payload)
        if (not content_type or content_type != metadata.get('content_type')
                or content_type != row['content_type']
                or hashlib.sha256(payload).hexdigest() != image_id):
            return None, None
        return metadata, payload

    def update_projector_prize_image(data, actor):
        metadata, payload = _parse_image(app, data)
        new_image_id = _image_id(metadata)
        with app.db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                _ensure_images(db)
                old_image_id = _image_id(app.get_setting('projector_prize_image', None, db))
                if metadata is not None:
                    db.execute('''INSERT INTO _eventdesk_projector_images (image_id,content_type,image)
                        VALUES (?,?,?) ON CONFLICT(image_id) DO UPDATE
                        SET content_type=excluded.content_type,image=excluded.image''',
                        (new_image_id, metadata['content_type'], payload))
                app.set_setting(db, 'projector_prize_image', metadata, actor)
                db.execute('''UPDATE lucky_draw_presentation SET sequence=sequence+1,
                    updated_at=?,updated_by=? WHERE singleton=1''', (app.now(), app.actor_name(actor)))
                app.audit(db, actor, 'projector.prize_image_' + ('cleared' if metadata is None else 'updated'),
                          'presentation', 1, {} if metadata is None else {
                              'filename': metadata['filename'], 'content_type': metadata['content_type'],
                              'size': metadata['size']})
                if old_image_id and old_image_id != new_image_id:
                    db.execute('DELETE FROM _eventdesk_projector_images WHERE image_id=?', (old_image_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return metadata

    app.get_projector_prize_image = get_projector_prize_image
    app.update_projector_prize_image = update_projector_prize_image
