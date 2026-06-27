"""
Shared thumbnail utilities extracted from thumb_daemon, thumb_generator, and regenerate_thumbs.
All three now import from here to avoid code duplication.
"""
import os
import time
import hashlib
import sqlite3
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')
THUMB_DIR = os.path.join(_BASE, 'cache', 'thumbnails')
THUMB_SIZE = (400, 400)
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif',
                    '.tiff', '.tif', '.webp', '.arw', '.heic', '.heif'}


def thumb_filename(photo_id, file_path):
    h = hashlib.sha256(file_path.encode('utf-8')).hexdigest()
    return f'{photo_id}-{h}.jpg'


def existing_thumb_map(thumb_dir):
    """Build dict {photo_id: full_filename} for thumbnails on disk."""
    m = {}
    if not os.path.isdir(thumb_dir):
        return m
    for fname in os.listdir(thumb_dir):
        parts = fname.split('-', 1)
        if len(parts) == 2 and parts[0].isdigit():
            m[int(parts[0])] = fname
    return m


def find_thumb_file(photo_id, thumb_dir):
    """Find the thumbnail file on disk for a given photo_id.
    Verifies hash matches the DB file_path to avoid returning stale thumbnails.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM image_analysis WHERE id = ?", (photo_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            expected = thumb_filename(photo_id, row[0])
            expected_path = os.path.join(thumb_dir, expected)
            if os.path.exists(expected_path):
                return expected_path
    except Exception:
        pass
    # Fallback: prefix match
    prefix = f'{photo_id}-'
    for fname in os.listdir(thumb_dir):
        if fname.startswith(prefix):
            return os.path.join(thumb_dir, fname)
    return None


def try_generate(file_path, thumb_path):
    """Attempt thumbnail generation with PIL + HEIC/RAW fallback. Returns (orig_w, orig_h) or None."""
    try:
        with Image.open(file_path) as img:
            orig_w, orig_h = img.size
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            img.save(thumb_path, 'JPEG', quality=75)
        return orig_w, orig_h
    except Exception:
        pass

    # Fallback: pillow_heif (HEIC/HEIF)
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        with Image.open(file_path) as img:
            orig_w, orig_h = img.size
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            img.save(thumb_path, 'JPEG', quality=75)
        return orig_w, orig_h
    except Exception:
        pass

    # Fallback: rawpy (Sony ARW / RAW)
    try:
        import rawpy
        with rawpy.imread(file_path) as raw:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
        img = Image.fromarray(rgb)
        orig_w, orig_h = img.size
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        img.save(thumb_path, 'JPEG', quality=75)
        return orig_w, orig_h
    except Exception:
        pass

    return None


def remove_stale_thumb(photo_id, thumb_dir):
    """Remove any thumbnail file for this photo_id that has wrong hash."""
    expected = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM image_analysis WHERE id = ?", (photo_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            expected = thumb_filename(photo_id, row[0])
    except Exception:
        pass

    for fname in os.listdir(thumb_dir):
        if fname.startswith(f'{photo_id}-') and fname != expected:
            try:
                os.remove(os.path.join(thumb_dir, fname))
            except Exception:
                pass
