"""
Admin blueprint: media sources, folder visibility, scan logs, scheduler.
"""
import os
import time
import sqlite3
import threading
from flask import Blueprint, render_template, jsonify, request, abort, current_app

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif',
                    '.tiff', '.tif', '.webp', '.arw', '.heic', '.heif'}
THUMB_DIR = os.path.join(_BASE, 'cache', 'thumbnails')

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn




# =============================================================================
# Admin dashboard page
# =============================================================================

@admin_bp.route('/')
def admin_index():
    return render_template('admin.html')


# =============================================================================
# Media Sources CRUD
# =============================================================================

@admin_bp.route('/api/sources', methods=['GET'])
def list_sources():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_sources ORDER BY id')
    rows = cursor.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@admin_bp.route('/api/sources', methods=['POST'])
def create_source():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    root_path = (data.get('root_path') or '').strip()
    is_active = data.get('is_active', 1)

    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if not root_path:
        return jsonify({'error': 'Root path is required'}), 400

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO media_sources (name, root_path, is_active)
            VALUES (?, ?, ?)
        """, (name, root_path, is_active))
        conn.commit()
        new_id = cursor.lastrowid

        cursor.execute('SELECT * FROM media_sources WHERE id = ?', (new_id,))
        row = cursor.fetchone()
        conn.close()

        # Trigger an initial quick scan immediately in the background
        if is_active:
            try:
                _start_scan(new_id, 'incremental')
            except Exception as e:
                print(f'[admin] failed to start initial scan for source {new_id}: {e}')

        return jsonify(dict(row)), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Root path already exists'}), 409


@admin_bp.route('/api/sources/<int:source_id>', methods=['PUT'])
def update_source(source_id):
    data = request.get_json(force=True)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    if not cursor.fetchone():
        conn.close()
        abort(404)

    updates = []
    params = []
    for field in ('name', 'root_path', 'is_active'):
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    if updates:
        updates.append("updated_at = datetime('now')")
        params.append(source_id)
        cursor.execute(f"UPDATE media_sources SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    row = cursor.fetchone()
    conn.close()
    return jsonify(dict(row))


@admin_bp.route('/api/sources/<int:source_id>', methods=['DELETE'])
def delete_source(source_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    if not cursor.fetchone():
        conn.close()
        abort(404)
    # Cascade: folder_visibility, scan_schedules handled by ON DELETE CASCADE
    cursor.execute('DELETE FROM media_sources WHERE id = ?', (source_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# =============================================================================
# Connectivity Test
# =============================================================================

@admin_bp.route('/api/sources/<int:source_id>/test', methods=['POST'])
def test_source(source_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    source = cursor.fetchone()
    conn.close()

    if not source:
        abort(404)

    root_path = source['root_path']

    if not os.path.exists(root_path):
        return jsonify({'ok': False, 'message': 'Path does not exist', 'stats': None})

    if not os.path.isdir(root_path):
        return jsonify({'ok': False, 'message': 'Path exists but is not a directory', 'stats': None})

    folders = 0
    files = 0
    errors = 0
    try:
        for entry in os.scandir(root_path):
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders += 1
                elif entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in IMAGE_EXTENSIONS:
                        files += 1
            except (OSError, PermissionError):
                errors += 1
    except PermissionError:
        return jsonify({'ok': False, 'message': 'Permission denied accessing the path', 'stats': None})
    except OSError as e:
        return jsonify({'ok': False, 'message': f'SMB / filesystem error: {e}', 'stats': None})

    msg = f'Path accessible: {folders} folders, {files} image files found (top level)'
    return jsonify({
        'ok': True,
        'message': msg,
        'stats': {'folders': folders, 'files': files, 'errors': errors}
    })


@admin_bp.route('/api/sources/test-connectivity', methods=['GET'])
def test_path_direct():
    """Test a raw path (not saved to DB yet)."""
    root_path = request.args.get('path', '').strip()
    if not root_path:
        return jsonify({'ok': False, 'message': 'No path provided'}), 400

    if not os.path.exists(root_path):
        return jsonify({'ok': False, 'message': 'Path does not exist', 'stats': None})

    if not os.path.isdir(root_path):
        return jsonify({'ok': False, 'message': 'Path is not a directory', 'stats': None})

    folders = 0
    files = 0
    errors = 0
    try:
        for entry in os.scandir(root_path):
            try:
                if entry.is_dir(follow_symlinks=False):
                    folders += 1
                elif entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in IMAGE_EXTENSIONS:
                        files += 1
            except (OSError, PermissionError):
                errors += 1
    except PermissionError:
        return jsonify({'ok': False, 'message': 'Permission denied'})
    except OSError as e:
        return jsonify({'ok': False, 'message': f'SMB error: {e}'})

    return jsonify({
        'ok': True,
        'message': f'Path accessible: {folders} folders, {files} image files',
        'stats': {'folders': folders, 'files': files, 'errors': errors}
    })


# =============================================================================
# Folder Visibility
# =============================================================================

@admin_bp.route('/api/folders/<int:source_id>', methods=['GET'])
def get_folder_tree(source_id):
    """Return folder tree for a source, with visibility status.
    Builds tree from DB paths for speed (avoids slow SMB os.walk).
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    source = cursor.fetchone()
    if not source:
        conn.close()
        abort(404)

    root_path = source['root_path']
    prefix_len = len(root_path)

    # Get hidden folder paths for this source
    cursor.execute("""
        SELECT folder_path FROM folder_visibility
        WHERE media_source_id = ? AND is_hidden = 1
    """, (source_id,))
    hidden_set = set(r['folder_path'] for r in cursor.fetchall())

    # Get all relative paths and extract first-level folder in Python
    cursor.execute("""
        SELECT SUBSTR(file_path, ?) AS rel_path
        FROM image_analysis
        WHERE SUBSTR(file_path, 1, ?) = ?
    """, (prefix_len + 1, prefix_len, root_path))

    from collections import Counter
    folder_counts = Counter()
    for row in cursor.fetchall():
        rel = row['rel_path']
        if not rel:
            continue
        # Strip leading backslash (present for sources without trailing BS in root_path)
        if rel.startswith(chr(92)):
            rel = rel[1:]
        parts = rel.split(chr(92))
        # Only count directory entries (has at least one more segment after the folder)
        if len(parts) >= 2:
            first = parts[0].strip()
            if first:
                folder_counts[first] += 1
        else:
            # File at root — don't include as a folder entry
            pass

    tree = []
    for folder, cnt in sorted(folder_counts.items(), key=lambda x: x[0].lower()):
        tree.append({
            'name': folder,
            'path': folder,
            'count': cnt,
            'is_hidden': folder in hidden_set,
            'children': [],
        })

    conn.close()

    return jsonify({
        'source_id': source_id,
        'source_name': source['name'],
        'root_path': root_path,
        'tree': tree
    })


@admin_bp.route('/api/folders/<int:source_id>/visibility', methods=['PUT'])
def toggle_folder_visibility(source_id):
    data = request.get_json(force=True)
    folder_path = (data.get('folder_path') or '').strip()
    is_hidden = data.get('is_hidden', True)

    if not folder_path:
        return jsonify({'error': 'folder_path is required'}), 400

    conn = get_db()
    cursor = conn.cursor()

    # Check source exists
    cursor.execute('SELECT id FROM media_sources WHERE id = ?', (source_id,))
    if not cursor.fetchone():
        conn.close()
        abort(404)

    if is_hidden:
        cursor.execute("""
            INSERT INTO folder_visibility (media_source_id, folder_path, is_hidden)
            VALUES (?, ?, 1)
            ON CONFLICT(media_source_id, folder_path) DO UPDATE SET is_hidden = 1
        """, (source_id, folder_path))
    else:
        cursor.execute("""
            DELETE FROM folder_visibility
            WHERE media_source_id = ? AND folder_path = ?
        """, (source_id, folder_path))

    conn.commit()
    conn.close()

    # Update version timestamp so other clients can detect changes
    try:
        c2 = get_db()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        c2.execute("""
            INSERT INTO admin_settings (key, value) VALUES ('folder_visibility_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = ?
        """, (now, now))
        c2.commit()
        c2.close()
    except Exception:
        pass

    return jsonify({'ok': True, 'folder_path': folder_path, 'is_hidden': is_hidden})


# =============================================================================
# Scan trigger & logs
# =============================================================================

# Store active scan threads to avoid duplicates
_active_scans = {}  # {source_id: log_id}
_scan_abort = {}    # {log_id: threading.Event()}
_scan_lock = threading.Lock()


def _start_scan(source_id, mode='incremental'):
    """Helper to programmatically start a scan thread for a source."""
    conn = get_db()
    cursor = conn.cursor()

    # Create scan_log entry
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("""
        INSERT INTO scan_log (media_source_id, status, started_at)
        VALUES (?, 'running', ?)
    """, (source_id, now))
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()

    with _scan_lock:
        _active_scans[source_id] = log_id
        event = threading.Event()
        _scan_abort[log_id] = event

    # Launch background thread
    from scanner import run_scan

    def _scan_wrapper(src_id, lid, evt, scan_mode):
        try:
            run_scan(src_id, lid, mode=scan_mode, abort_event=evt)
        finally:
            with _scan_lock:
                if _active_scans.get(src_id) == lid:
                    del _active_scans[src_id]
                _scan_abort.pop(lid, None)
            try:
                from thumb_daemon import ThumbnailDaemon
                td = ThumbnailDaemon()
                td.run_once(src_id)
                from app import _build_thumb_map_wrapper
                _build_thumb_map_wrapper()
                ad = current_app.config.get('ANALYSIS_DAEMON')
                if ad:
                    ad.run_once()
            except Exception:
                pass

    thread = threading.Thread(target=_scan_wrapper, args=(source_id, log_id, event, mode),
                              daemon=True, name=f'scan-{source_id}')
    thread.start()
    return log_id


@admin_bp.route('/api/scan/<int:source_id>', methods=['POST'])
def trigger_scan(source_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_sources WHERE id = ?', (source_id,))
    source = cursor.fetchone()
    if not source:
        conn.close()
        abort(404)
    conn.close()

    # Determine scan mode from query param: ?mode=full for deep scan
    mode = request.args.get('mode', 'incremental')
    if mode not in ('incremental', 'full'):
        mode = 'incremental'

    # Prevent duplicate scans for same source
    with _scan_lock:
        if source_id in _active_scans:
            return jsonify({
                'error': 'Scan already running for this source',
                'scan_log_id': _active_scans[source_id]
            }), 409

    log_id = _start_scan(source_id, mode)
    return jsonify({'scan_log_id': log_id, 'status': 'running', 'mode': mode}), 202


@admin_bp.route('/api/scan/<int:log_id>', methods=['GET'])
def get_scan_status(log_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM scan_log WHERE id = ?', (log_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(dict(row))


@admin_bp.route('/api/scan/active', methods=['GET'])
def list_active_scans():
    """Return currently running scan_log entries."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sl.*, ms.name AS source_name
        FROM scan_log sl
        LEFT JOIN media_sources ms ON sl.media_source_id = ms.id
        WHERE sl.status = 'running'
        ORDER BY sl.id DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)


@admin_bp.route('/api/scan/<int:log_id>/stop', methods=['POST'])
def stop_scan(log_id):
    """Request cancellation of a running scan."""
    # First try in-memory abort
    with _scan_lock:
        event = _scan_abort.get(log_id)

    # Also handle stale entries: scan was started by old process but _scan_abort is empty
    # Force-update the DB status regardless
    try:
        conn = get_db()
        conn.execute("""
            UPDATE scan_log SET status = 'cancelled', finished_at = datetime('now'),
                error_message = 'Cancelled by user'
            WHERE id = ? AND status = 'running'
        """, (log_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass

    if event:
        event.set()
        with _scan_lock:
            _scan_abort.pop(log_id, None)
        return jsonify({'ok': True, 'message': 'Scan cancelled'})

    return jsonify({'ok': True, 'message': 'Stale scan entry cleaned up (no active thread)'})


@admin_bp.route('/api/scan', methods=['GET'])
def list_scan_logs():
    source_id = request.args.get('source_id', type=int)
    limit = request.args.get('limit', 20, type=int)

    conn = get_db()
    cursor = conn.cursor()

    if source_id:
        cursor.execute("""
            SELECT sl.*, ms.name AS source_name
            FROM scan_log sl
            LEFT JOIN media_sources ms ON sl.media_source_id = ms.id
            WHERE sl.media_source_id = ?
            ORDER BY sl.id DESC LIMIT ?
        """, (source_id, limit))
    else:
        cursor.execute("""
            SELECT sl.*, ms.name AS source_name
            FROM scan_log sl
            LEFT JOIN media_sources ms ON sl.media_source_id = ms.id
            ORDER BY sl.id DESC LIMIT ?
        """, (limit,))

    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)


# =============================================================================
# Scheduler status
# =============================================================================

@admin_bp.route('/api/scheduler/status', methods=['GET'])
def scheduler_status():
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM admin_settings WHERE key = 'scheduler_enabled'")
        row = cursor.fetchone()
        conn.close()
        enabled = (row and row['value'] == '1')
    except Exception:
        enabled = False
    return jsonify({'enabled': enabled})


# =============================================================================
# Schedules
# =============================================================================

@admin_bp.route('/api/schedules', methods=['GET'])
def list_schedules():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ss.*, ms.name AS source_name
        FROM scan_schedules ss
        LEFT JOIN media_sources ms ON ss.media_source_id = ms.id
        ORDER BY ss.id
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify(rows)


@admin_bp.route('/api/schedules', methods=['POST'])
def create_schedule():
    data = request.get_json(force=True)
    source_id = data.get('source_id')
    interval_minutes = data.get('interval_minutes', 30)
    is_active = data.get('is_active', 1)

    if not source_id:
        return jsonify({'error': 'source_id is required'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM media_sources WHERE id = ?', (source_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'error': 'Source not found'}), 404

    # Check if schedule already exists for this source
    cursor.execute('SELECT id FROM scan_schedules WHERE media_source_id = ?', (source_id,))
    existing = cursor.fetchone()
    if existing:
        # Update instead
        cursor.execute("""
            UPDATE scan_schedules SET interval_minutes = ?, is_active = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (interval_minutes, is_active, existing['id']))
        conn.commit()
        cursor.execute('SELECT * FROM scan_schedules WHERE id = ?', (existing['id'],))
        row = cursor.fetchone()
        conn.close()
        return jsonify(dict(row))

    cursor.execute("""
        INSERT INTO scan_schedules (media_source_id, interval_minutes, is_active)
        VALUES (?, ?, ?)
    """, (source_id, interval_minutes, is_active))
    conn.commit()
    new_id = cursor.lastrowid
    cursor.execute('SELECT * FROM scan_schedules WHERE id = ?', (new_id,))
    row = cursor.fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@admin_bp.route('/api/schedules/<int:schedule_id>', methods=['PUT'])
def update_schedule(schedule_id):
    data = request.get_json(force=True)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM scan_schedules WHERE id = ?', (schedule_id,))
    if not cursor.fetchone():
        conn.close()
        abort(404)

    updates = ["updated_at = datetime('now')"]
    params = []
    for field in ('interval_minutes', 'is_active'):
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    params.append(schedule_id)
    cursor.execute(f"UPDATE scan_schedules SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    cursor.execute('SELECT * FROM scan_schedules WHERE id = ?', (schedule_id,))
    row = cursor.fetchone()
    conn.close()
    return jsonify(dict(row))


@admin_bp.route('/api/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM scan_schedules WHERE id = ?', (schedule_id,))
    if not cursor.fetchone():
        conn.close()
        abort(404)
    cursor.execute('DELETE FROM scan_schedules WHERE id = ?', (schedule_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@admin_bp.route('/api/scheduler/toggle', methods=['POST'])
def toggle_scheduler():
    data = request.get_json(force=True)
    enabled = data.get('enabled', False)

    conn = get_db()
    conn.execute("""
        INSERT INTO admin_settings (key, value) VALUES ('scheduler_enabled', ?)
        ON CONFLICT(key) DO UPDATE SET value = ?
    """, ('1' if enabled else '0', '1' if enabled else '0'))
    conn.commit()
    conn.close()

    # Also start/stop the scheduler object
    try:
        from flask import current_app
        scheduler = current_app.config.get('SCAN_SCHEDULER')
        if scheduler:
            if enabled and not scheduler.is_running:
                scheduler.start()
            elif not enabled and scheduler.is_running:
                scheduler.stop()
    except Exception:
        pass

    return jsonify({'ok': True, 'enabled': enabled})

