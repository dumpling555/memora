"""
Background thumbnail generator.
Reads original images from SMB, resizes, saves JPEG to local cache.
Updates thumb_progress table for real-time UI.
"""
import os
import time
import sqlite3
from PIL import Image
from thumb_common import (THUMB_DIR, THUMB_SIZE, DATABASE, IMAGE_EXTENSIONS,
                          thumb_filename, existing_thumb_map, try_generate)

BATCH_COMMIT = 50


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_thumb_dir():
    os.makedirs(THUMB_DIR, exist_ok=True)


def run_thumb_gen(source_id, log_id, abort_event=None):
    """Generate thumbnails for all images in a media source."""
    conn = get_db()
    cursor = conn.cursor()
    start_time = time.time()

    try:
        cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
        source = cursor.fetchone()
        if not source:
            _finish_progress(conn, log_id, 'failed', error_message='Source not found')
            return

        root = source['root_path']
        if not os.path.isdir(root):
            _finish_progress(conn, log_id, 'failed', error_message='Directory not accessible')
            return

        _ensure_thumb_dir()

        existing = existing_thumb_map(THUMB_DIR)
        print(f'[thumb_gen] Existing thumbs: {len(existing)} IDs')

        prefix_len = len(root)
        cursor.execute("""
            SELECT id, file_path, file_name
            FROM image_analysis
            WHERE SUBSTR(file_path, 1, ?) = ?
            ORDER BY id
        """, (prefix_len, root))

        records = cursor.fetchall()
        total = len(records)

        wait_cycles = 0
        while total == 0 and wait_cycles < 10:
            if abort_event and abort_event.is_set():
                _finish_progress(conn, log_id, 'cancelled', 0, 0, 0,
                                 error_message='Cancelled by user')
                conn.close()
                return
            time.sleep(3)
            conn.commit()
            cursor.execute("""
                SELECT id, file_path, file_name
                FROM image_analysis
                WHERE SUBSTR(file_path, 1, ?) = ?
                ORDER BY id
            """, (prefix_len, root))
            records = cursor.fetchall()
            total = len(records)
            wait_cycles += 1

        completed = 0
        errors = 0

        conn.execute("UPDATE thumb_progress SET total = ? WHERE id = ?", (total, log_id))
        conn.commit()

        print(f'[thumb_gen] Starting for source_id={source_id}: {total} images')

        for row in records:
            if abort_event and abort_event.is_set():
                conn.commit()
                _update_progress(conn, log_id, total, completed, errors)
                _finish_progress(conn, log_id, 'cancelled', total, completed, errors,
                                 error_message='Cancelled by user')
                conn.close()
                return

            photo_id = row['id']
            file_path = row['file_path']
            expected = thumb_filename(photo_id, file_path)

            if existing.get(photo_id) == expected:
                completed += 1
                _maybe_commit_and_update(conn, log_id, total, completed, errors)
                continue
            elif existing.get(photo_id) is not None:
                stale = os.path.join(THUMB_DIR, existing[photo_id])
                try:
                    os.remove(stale)
                except Exception:
                    pass

            thumb_path = os.path.join(THUMB_DIR, expected)
            if try_generate(file_path, thumb_path):
                completed += 1
                existing[photo_id] = expected
            else:
                errors += 1
                if errors <= 10:
                    print(f'  [thumb_gen] FAIL id={photo_id} {file_path[:60]}: unable to decode')

            _maybe_commit_and_update(conn, log_id, total, completed, errors)

        conn.commit()
        elapsed = time.time() - start_time
        print(f'[thumb_gen] done: {completed}/{total} completed, {errors} errors in {elapsed:.0f}s')
        _finish_progress(conn, log_id, 'completed', total, completed, errors)

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            _finish_progress(conn, log_id, 'failed', error_message=str(e))
        except Exception:
            pass
    finally:
        conn.close()


def _maybe_commit_and_update(conn, log_id, total, completed, errors):
    if (completed + errors) % BATCH_COMMIT == 0:
        conn.commit()
        _update_progress(conn, log_id, total, completed, errors)


def _update_progress(conn, log_id, total, completed, errors):
    try:
        conn.execute("""
            UPDATE thumb_progress SET total = ?, completed = ?, errors = ?
            WHERE id = ?
        """, (total, completed, errors, log_id))
        conn.commit()
    except Exception:
        pass


def _finish_progress(conn, log_id, status, total=0, completed=0, errors=0, error_message=''):
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        UPDATE thumb_progress SET
            status = ?, total = ?, completed = ?, errors = ?,
            finished_at = ?, error_message = ?
        WHERE id = ?
    """, (status, total, completed, errors, now, error_message, log_id))
    conn.commit()
