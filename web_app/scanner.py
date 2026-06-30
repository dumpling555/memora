"""
Background scanner: walk a media source, update image_analysis with file metadata.

Two modes:
  - incremental (quick): skip existing DB records entirely, only add NEW files
  - full (deep): scan everything — add new, update changed, delete removed

Uses ThreadPoolExecutor for parallel file stat() over SMB.
"""
import os
import time
import sqlite3
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

_scanner_lock = threading.Lock()
_active_scan_sources = set()

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')
THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', 'thumbnails')
IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.bmp', '.gif',
    '.tiff', '.tif', '.webp', '.arw', '.heic', '.heif'
}
BATCH_COMMIT = 500
MAX_WORKERS = 16           # concurrent file stat / DB write (NAS can handle many concurrent requests)


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _walk_parallel(root, max_workers=8, abort_event=None, dir_timeout=30):
    """Parallel scandir directory walk. Sibling directories scanned concurrently.

    abort_event: threading.Event — if set, walk stops early.
    dir_timeout: seconds to wait for each directory result before giving up.
                 Prevents permanent hang when NAS paths become unresponsive.
    """
    q = queue.Queue()
    visited = set()
    pending = {os.path.normpath(root)}

    def _scan(path):
        dirs, files = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs.append(e.name)
                        elif e.is_file(follow_symlinks=False):
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in IMAGE_EXTENSIONS:
                                files.append(e.name)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            pass
        q.put((path, sorted(dirs), sorted(files)))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pool.submit(_scan, root)
        while len(visited) < len(pending):
            if abort_event and abort_event.is_set():
                break
            try:
                path, dirs, files = q.get(timeout=dir_timeout)
            except queue.Empty:
                # A directory worker timed out (NAS unresponsive).
                # Treat all still-pending directories as visited (empty) so
                # the walk can terminate instead of hanging forever.
                stuck = pending - visited
                print(f'[scanner] _walk_parallel: {len(stuck)} dir(s) timed out after {dir_timeout}s, skipping: {list(stuck)[:5]}')
                visited.update(stuck)
                break
            visited.add(path)
            yield path, files  # files already filtered by extension
            for d in dirs:
                sub = os.path.normpath(os.path.join(path, d))
                if sub not in visited and sub not in pending:
                    pending.add(sub)
                    pool.submit(_scan, sub)


def run_scan(source_id, log_id, mode='incremental', abort_event=None, scan_mode='quick_manual'):
    """Scan a media source and update image_analysis."""
    with _scanner_lock:
        if source_id in _active_scan_sources:
            print(f"[scanner] Scan already running for source {source_id}, skipping duplicate request")
            # Retry up to 3 times in case of a transient DB lock — a silent pass here
            # would leave the log entry stuck in 'running' forever.
            for _attempt in range(3):
                try:
                    conn = get_db()
                    _finish_scan(conn, log_id, 'cancelled', error_message='Another scan already running for this source')
                    conn.close()
                    break
                except Exception as _e:
                    time.sleep(0.2)
                    if _attempt == 2:
                        print(f"[scanner] WARNING: could not mark log_id={log_id} as cancelled after 3 attempts: {_e}")
            return
        _active_scan_sources.add(source_id)

    conn = get_db()
    cursor = conn.cursor()
    start_time = time.time()

    # Write scan_mode to the log entry
    try:
        cursor.execute("UPDATE scan_log SET scan_mode = ? WHERE id = ?", (scan_mode, log_id))
        conn.commit()
    except Exception:
        pass

    try:
        # 1. Get source info
        cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
        source = cursor.fetchone()
        if not source:
            _finish_scan(conn, log_id, 'failed', error_message='Source not found')
            return

        root = source['root_path']
        if not os.path.isdir(root):
            _finish_scan(conn, log_id, 'failed', error_message='Directory not accessible')
            return

        # 2. Load existing DB records into memory
        if mode == 'incremental':
            cursor.execute('SELECT file_path FROM image_analysis WHERE file_path LIKE ?', (root + '%',))
            existing_paths = set(os.path.normpath(row['file_path']) for row in cursor.fetchall())
            existing_by_path = None
        else:
            cursor.execute('SELECT id, file_path, file_size, created_at FROM image_analysis WHERE file_path LIKE ?', (root + '%',))
            existing_by_path = {}
            for row in cursor.fetchall():
                existing_by_path[row['file_path']] = {
                    'id': row['id'],
                    'file_size': row['file_size'],
                    'created_at': row['created_at'],
                }
            existing_paths = None

        # 3. Walk directory tree (parallel scandir over SMB)
        total = new = updated = skipped = deleted = errors = 0
        seen_paths = set()
        pending_inserts = []   # batched for batch insert
        pending_checks = []    # [(full_path, fname, ext), ...]

        for dirpath, filenames in _walk_parallel(root, abort_event=abort_event):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()

                full_path = os.path.normpath(os.path.join(dirpath, fname))
                total += 1
                seen_paths.add(full_path)

                if mode == 'incremental':
                    # --- INCREMENTAL: skip existing, collect new for batch ---
                    if full_path in existing_paths:
                        skipped += 1
                        continue
                    pending_inserts.append((full_path, fname, ext))
                else:
                    # --- FULL SCAN: collect all for parallel stat ---
                    existing_record = existing_by_path.get(full_path)
                    pending_checks.append((full_path, fname, ext, existing_record))

                # Process batches when threshold reached
                if len(pending_inserts) >= BATCH_COMMIT:
                    processed = _process_new_files_batch(pending_inserts, conn)
                    new += processed
                    pending_inserts = []
                    conn.commit()
                    _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)

                if len(pending_checks) >= BATCH_COMMIT:
                    n, u, s, e = _process_full_batch(pending_checks, conn, abort_event)
                    new += n
                    updated += u
                    skipped += s
                    errors += e
                    pending_checks = []
                    conn.commit()
                    _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)

                if abort_event and abort_event.is_set():
                    conn.commit()
                    _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)
                    _finish_scan(conn, log_id, 'cancelled', total, new, updated, skipped, deleted, errors,
                                 error_message='Cancelled by user')
                    conn.close()
                    return

        if abort_event and abort_event.is_set():
            _finish_scan(conn, log_id, 'cancelled', total, new, updated, skipped, deleted, errors,
                         error_message='Cancelled by user')
            conn.close()
            return

        # Flush remaining batches
        if pending_inserts:
            new += _process_new_files_batch(pending_inserts, conn)
            pending_inserts = []
        if pending_checks:
            n, u, s, e = _process_full_batch(pending_checks, conn, abort_event)
            new += n
            updated += u
            skipped += s
            errors += e
            pending_checks = []

        conn.commit()
        _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)

        # --- Post-walk: delete removed files (full scan only, same source only) ---
        if mode == 'full':
            for path, rec in existing_by_path.items():
                if path not in seen_paths and path.startswith(root):
                    cursor.execute("DELETE FROM image_analysis WHERE id = ?", (rec['id'],))
                    deleted += 1
                    if deleted % BATCH_COMMIT == 0:
                        conn.commit()
                        _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)
        elif mode == 'incremental':
            deleted_paths = existing_paths - seen_paths
            if deleted_paths:
                for path in deleted_paths:
                    cursor.execute("DELETE FROM image_analysis WHERE file_path = ?", (path,))
                    deleted += 1
                    if deleted % BATCH_COMMIT == 0:
                        conn.commit()
                        _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors)

        conn.commit()
        elapsed = time.time() - start_time
        print(f'[scan] {mode} done: total={total} new={new} updated={updated} '
              f'skipped={skipped} deleted={deleted} errors={errors} in {elapsed:.0f}s')
        _finish_scan(conn, log_id, 'completed', total, new, updated, skipped, deleted, errors)

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            _finish_scan(conn, log_id, 'failed', error_message=str(e))
        except Exception:
            pass
    finally:
        with _scanner_lock:
            _active_scan_sources.discard(source_id)
        conn.close()


def _process_new_files_batch(items, conn):
    """Batch INSERT new files found by incremental scan.
    Uses thread pool for parallel os.stat() over SMB.
    Returns count of successfully inserted files.
    """
    if not items:
        return 0

    # Parallel stat over SMB
    stat_results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(_quick_stat, path): path for path, _, _ in items}
        for fut in as_completed(fut_map):
            path = fut_map[fut]
            try:
                stat_results[path] = fut.result()
            except Exception:
                stat_results[path] = None

    # Batch INSERT
    values = []
    count = 0
    for full_path, fname, ext in items:
        st = stat_results.get(full_path)
        if st is None:
            continue
        fsize, mtime_str = st
        fmt = ext[1:].upper()
        values.append((fname, full_path, fsize, fmt, mtime_str))
        count += 1

        if len(values) >= 200:
            conn.executemany("""
                INSERT INTO image_analysis
                    (file_name, file_path, file_size, format, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, values)
            values = []

    if values:
        conn.executemany("""
            INSERT INTO image_analysis
                (file_name, file_path, file_size, format, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, values)

    return count


def _process_full_batch(items, conn, abort_event):
    """Process a batch of files for full scan (add + update).
    Uses thread pool for parallel os.stat() over SMB.
    """
    if not items:
        return 0, 0, 0, 0

    # Parallel stat over SMB
    stat_results = {}
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            fut_map = {pool.submit(_quick_stat, path): path for path, _, _, _ in items}
            for fut in as_completed(fut_map):
                path = fut_map[fut]
                try:
                    stat_results[path] = fut.result()
                except Exception:
                    stat_results[path] = None
    except RuntimeError:
        # Interpreter shutting down — bail out gracefully
        return 0, 0, 0, len(items)

    new = updated = skipped = errors = 0
    insert_values = []
    update_values = []

    for full_path, fname, ext, existing_record in items:
        st = stat_results.get(full_path)
        if st is None:
            errors += 1
            continue

        fsize, mtime_str = st

        if existing_record is None:
            # New file
            fmt = ext[1:].upper()
            insert_values.append((fname, full_path, fsize, fmt, mtime_str))
            new += 1
        elif (existing_record['file_size'] != fsize or
              existing_record['created_at'] != mtime_str):
            # Changed file — only update changed fields, preserve overview
            fmt = ext[1:].upper()
            update_values.append((fname, fsize, fmt, mtime_str, existing_record['id']))
            updated += 1
        else:
            skipped += 1

        if abort_event and abort_event.is_set():
            break

    # Batch INSERT
    if insert_values:
        for i in range(0, len(insert_values), 500):
            batch = insert_values[i:i + 500]
            conn.executemany("""
                INSERT INTO image_analysis
                    (file_name, file_path, file_size, format, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, batch)

    # Batch UPDATE
    if update_values:
        for i in range(0, len(update_values), 500):
            batch = update_values[i:i + 500]
            conn.executemany("""
                UPDATE image_analysis SET
                    file_name = ?, file_size = ?,
                    format = ?, created_at = ?
                WHERE id = ?
            """, batch)

    return new, updated, skipped, errors


def _quick_stat(path):
    """Quick stat() call — runs in thread pool to parallelize SMB latency."""
    st = os.stat(path)
    return (st.st_size, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)))


def _update_scan_progress(conn, log_id, total, new, updated, skipped, deleted, errors):
    try:
        conn.execute("""
            UPDATE scan_log SET
                total_files = ?, new_files = ?, updated_files = ?,
                skipped_files = ?, error_files = ?, deleted_files = ?
            WHERE id = ?
        """, (total, new, updated, skipped, errors, deleted, log_id))
        conn.commit()
    except Exception:
        pass


def _finish_scan(conn, log_id, status, total=0, new=0, updated=0, skipped=0, deleted=0, errors=0, error_message=''):
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        UPDATE scan_log SET
            status = ?, total_files = ?, new_files = ?, updated_files = ?,
            skipped_files = ?, error_files = ?, deleted_files = ?,
            finished_at = ?, error_message = ?
        WHERE id = ?
    """, (status, total, new, updated, skipped, errors, deleted, now, error_message, log_id))
    conn.commit()
