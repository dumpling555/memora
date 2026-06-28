from flask import Flask, render_template, jsonify, request, send_file, abort, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import time
import sys
import hashlib
import io
import re
import secrets
from thumb_common import try_generate, thumb_filename

app = Flask(__name__)

# =============================================================================
# Admin tables init + scheduler registration
# =============================================================================
# CONFIGURATION
# =============================================================================
_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')

THUMB_DIR = os.path.join(_BASE, 'cache', 'thumbnails')
THUMB_MAP = {}  # {photo_id_str: file_path}

# Load or generate secret key for session signing
try:
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='secret_key'")
    row = c.fetchone()
    if row:
        app.secret_key = row[0]
    else:
        app.secret_key = os.urandom(24).hex()
        c.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES (?, ?)", ('secret_key', app.secret_key))
        conn.commit()
    conn.close()
except sqlite3.OperationalError:
    # Table doesn't exist yet on first startup; it will be created by _init_admin_db later
    app.secret_key = os.urandom(24).hex()

app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 28800  # 8 hours


# =============================================================================
# Admin tables init + scheduler registration
# =============================================================================
def _init_admin_db():
    """Create admin tables on first launch and insert default source."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS media_sources (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            root_path   TEXT NOT NULL UNIQUE,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS folder_visibility (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            media_source_id INTEGER NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
            folder_path     TEXT NOT NULL,
            is_hidden       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(media_source_id, folder_path)
        );
        CREATE TABLE IF NOT EXISTS scan_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            media_source_id INTEGER REFERENCES media_sources(id),
            status          TEXT NOT NULL DEFAULT 'running',
            total_files     INTEGER DEFAULT 0,
            new_files       INTEGER DEFAULT 0,
            updated_files   INTEGER DEFAULT 0,
            skipped_files   INTEGER DEFAULT 0,
            error_files     INTEGER DEFAULT 0,
            deleted_files   INTEGER DEFAULT 0,
            started_at      TEXT,
            finished_at     TEXT,
            error_message   TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scan_schedules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            media_source_id INTEGER NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
            interval_minutes INTEGER NOT NULL DEFAULT 1440,
            is_active       INTEGER NOT NULL DEFAULT 1,
            last_run_at     TEXT,
            next_run_at     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS admin_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS image_analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name       TEXT,
            file_path       TEXT UNIQUE NOT NULL,
            file_size       INTEGER,
            width           INTEGER,
            height          INTEGER,
            format          TEXT,
            overview        TEXT,
            extracted_text  TEXT,
            other_info      TEXT,
            tags            TEXT,
            raw_result      TEXT,
            analyzed_at     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            has_thumbnail   INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_image_analysis_file_path ON image_analysis(file_path);
        CREATE INDEX IF NOT EXISTS idx_image_analysis_analyzed_at ON image_analysis(analyzed_at);
    """)

    # Seed admin settings - scheduler enabled by default for auto quick-scan
    c.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('scheduler_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('llm_api_url', 'http://172.18.18.100:1234/v1')")
    c.execute("INSERT OR IGNORE INTO admin_settings (key, value) VALUES ('llm_model_name', 'google/gemma-4-26b-a4b')")

    # Seed initial admin credentials if not set
    c.execute("SELECT value FROM admin_settings WHERE key='admin_username'")
    if not c.fetchone():
        pw_hash = generate_password_hash('admin@123')
        c.execute("INSERT INTO admin_settings (key, value) VALUES ('admin_username', 'admin')")
        c.execute("INSERT INTO admin_settings (key, value) VALUES ('password_hash', ?)", (pw_hash,))
        c.execute("INSERT INTO admin_settings (key, value) VALUES ('must_change_password', '1')")
        print('[admin] Default admin credentials seeded')

    conn.commit()

    # --- Migrate: add deleted_files column if older schema misses it ---
    try:
        c.execute("ALTER TABLE scan_log ADD COLUMN deleted_files INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # --- Migrate: add has_thumbnail column if older schema misses it ---
    try:
        c.execute("ALTER TABLE image_analysis ADD COLUMN has_thumbnail INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.close()


def _init_scheduler(app_instance):
    """Register admin blueprint, init tables, and start scheduler + thumb daemon."""
    _init_admin_db()

    # Clean up stale running scans and thumb jobs (from previous process)
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute("UPDATE scan_log SET status='cancelled', finished_at=datetime('now'), error_message='Process restarted' WHERE status='running'")
        conn.commit()
        conn.close()
    except Exception:
        pass

    from admin_routes import admin_bp
    app_instance.register_blueprint(admin_bp)

    from scheduler import ScanScheduler
    from quick_scanner import QuickScanner
    from scanner import run_scan as scanner_run_scan
    from thumb_daemon import ThumbnailDaemon
    from analysis_daemon import AnalysisDaemon
    scheduler = ScanScheduler(check_interval=60)
    qs = QuickScanner(check_interval=1800)  # Check every 30 minutes to allow NAS disks to sleep
    thumb_d = ThumbnailDaemon()
    thumb_d.start()
    app_instance.config['THUMB_DAEMON'] = thumb_d
    analysis_d = AnalysisDaemon()
    analysis_d.start()
    app_instance.config['ANALYSIS_DAEMON'] = analysis_d

    def _trigger_scan_fn(source_id, mode='incremental'):
        conn2 = sqlite3.connect(DATABASE)
        cur2 = conn2.cursor()
        cur2.execute("INSERT INTO scan_log (media_source_id, status, started_at) VALUES (?, 'running', ?)",
                     (source_id, time.strftime('%Y-%m-%d %H:%M:%S')))
        log_id = cur2.lastrowid
        conn2.commit()
        conn2.close()
        import threading

        def _scan_and_thumb():
            try:
                scanner_run_scan(source_id, log_id, mode=mode)
            finally:
                thumb_d.run_once(source_id)
                analysis_d.run_once()
                try:
                    _build_thumb_map_wrapper()
                except Exception:
                    pass

        thread = threading.Thread(target=_scan_and_thumb, args=(),
                                  daemon=True, name=f'sched-scan-{source_id}')
        thread.start()
        return log_id

    scheduler.trigger_scan_fn = _trigger_scan_fn
    app_instance.config['SCAN_SCHEDULER'] = scheduler

    # Start scheduler thread; _loop will read scheduler_enabled from DB
    scheduler.start()
    print('[admin] Scheduler thread started')

    qs.start()
    app_instance.config['QUICK_SCANNER'] = qs
    print('[admin] QuickScanner started')

    print('[admin] Blueprint registered')

# =============================================================================
# Authentication
# =============================================================================

# Login rate limiting (in-memory)
_login_attempts = {}  # IP -> list of timestamps

@app.before_request
def require_auth():
    if request.path in ('/login', '/api/login', '/api/logout', '/api/change-password') or request.path.startswith('/static/'):
        return None

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='password_hash'")
    row = c.fetchone()
    conn.close()

    if not row:
        return render_template('landing.html', need_setup=True)

    if not session.get('logged_in'):
        if '/api/' in request.path:
            return jsonify({'error': 'Authentication required'}), 401
        return render_template('landing.html', need_setup=False)

    # CSRF protection for state-changing requests
    if request.method in ('POST', 'PUT', 'DELETE') and request.path not in ('/api/login', '/api/logout'):
        csrf_token = request.headers.get('X-CSRF-Token', '')
        if not csrf_token or csrf_token != session.get('csrf_token', ''):
            return jsonify({'error': 'CSRF token missing or invalid'}), 403

    # Force password change check
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='must_change_password'")
    mcp_row = c.fetchone()
    conn.close()
    if mcp_row and mcp_row[0] == '1':
        if request.path not in ('/api/change-password', '/api/csrf-token', '/api/logout'):
            return jsonify({'error': 'Password change required', 'need_change': True}), 403


@app.route('/login')
def login_page():
    return render_template('landing.html', need_setup=False, error=request.args.get('error', ''))


@app.route('/api/login', methods=['POST'])
def api_login():
    # Rate limiting
    ip = request.remote_addr or '127.0.0.1'
    now = time.time()
    window = 300  # 5 minutes
    max_attempts = 5

    # Clean up old entries and check rate limit
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < window]
    _login_attempts[ip] = attempts

    if len(attempts) >= max_attempts:
        return jsonify({'error': 'Too many login attempts. Please try again later.'}), 429

    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='password_hash'")
    pw_row = c.fetchone()
    c.execute("SELECT value FROM admin_settings WHERE key='admin_username'")
    user_row = c.fetchone()
    c.execute("SELECT value FROM admin_settings WHERE key='must_change_password'")
    change_row = c.fetchone()
    conn.close()

    stored_user = user_row[0] if user_row else ''
    if not pw_row or not check_password_hash(pw_row[0], password) or username != stored_user:
        # Record failed attempt
        _login_attempts[ip].append(now)
        return jsonify({'ok': False, 'error': 'Invalid credentials'}), 401

    # Clear rate limit on successful login
    _login_attempts.pop(ip, None)

    session['logged_in'] = True
    session['username'] = username
    session['csrf_token'] = secrets.token_hex(32)
    session.permanent = True

    need_change = change_row and change_row[0] == '1'
    return jsonify({'ok': True, 'need_change': need_change})


@app.route('/api/change-password', methods=['POST'])
def api_change_password():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Not authenticated'}), 401

    data = request.get_json()
    current = data.get('current_password', '')
    new_pw = data.get('new_password', '')

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_settings WHERE key='password_hash'")
    row = c.fetchone()
    if not row or not check_password_hash(row[0], current):
        conn.close()
        return jsonify({'ok': False, 'error': 'Current password is incorrect'}), 401

    if len(new_pw) < 6:
        conn.close()
        return jsonify({'ok': False, 'error': 'Password must be at least 6 characters'}), 400
    if not re.search(r'[a-zA-Z]', new_pw):
        conn.close()
        return jsonify({'ok': False, 'error': 'Password must contain at least one letter'}), 400
    if not re.search(r'\d', new_pw):
        conn.close()
        return jsonify({'ok': False, 'error': 'Password must contain at least one digit'}), 400
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', new_pw):
        conn.close()
        return jsonify({'ok': False, 'error': 'Password must contain at least one special character'}), 400

    new_hash = generate_password_hash(new_pw)
    c.execute("UPDATE admin_settings SET value = ? WHERE key = 'password_hash'", (new_hash,))
    c.execute("UPDATE admin_settings SET value = '0' WHERE key = 'must_change_password'")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/csrf-token')
def api_csrf_token():
    return jsonify({'token': session.get('csrf_token', '')})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('logged_in', None)
    session.pop('csrf_token', None)
    return jsonify({'ok': True})


def _build_thumb_map_wrapper():
    """Rebuild THUMB_MAP from disk, verifying hash matches current DB file_path."""
    new_map = {}
    start = time.perf_counter()
    count = 0
    skipped = 0
    conn = None
    try:
        # Load all photo file paths from DB for hash verification
        db_paths = {}
        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute("SELECT id, file_path FROM image_analysis")
            for row in cursor.fetchall():
                db_paths[str(row[0])] = row[1]
        except Exception:
            pass
        finally:
            if conn:
                conn.close()
                conn = None

        for fname in os.listdir(THUMB_DIR):
            if not fname.endswith('.jpg'):
                continue
            parts = fname.split('-', 1)
            if len(parts) != 2:
                continue
            pid, h = parts
            h = h[:-4]  # remove '.jpg' suffix
            # Verify hash matches current DB file_path
            db_path = db_paths.get(pid)
            if db_path:
                expected_hash = hashlib.sha256(db_path.encode('utf-8')).hexdigest()
                if h != expected_hash:
                    skipped += 1
                    continue
            new_map[pid] = os.path.join(THUMB_DIR, fname)
            count += 1
    except FileNotFoundError:
        pass
    THUMB_MAP.clear()
    THUMB_MAP.update(new_map)
    elapsed = time.perf_counter() - start
    print(f'Thumbnail map rebuilt: {count} entries valid, {skipped} skipped (hash mismatch) in {elapsed*1000:.0f}ms')


# Run early init
_init_scheduler(app)

_build_thumb_map_wrapper()

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_active_source_roots():
    """Return list of root paths (normalized with trailing backslash) for active media sources."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT root_path FROM media_sources WHERE is_active = 1")
    rows = cursor.fetchall()
    conn.close()
    bs = chr(92)
    roots = []
    for r in rows:
        root = r['root_path']
        if not root.endswith(bs):
            root += bs
        roots.append(root)
    return roots


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/photos')
def get_photos():
    query = request.args.get('q', '').strip()
    folder_names = request.args.getlist('folder')
    year_filter = request.args.get('year', '').strip()
    month_filter = request.args.get('month', '').strip()
    date_filter = request.args.get('date', '').strip()
    limit = request.args.get('limit', type=int)
    after_cursor = request.args.get('after', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor()

    sql = 'SELECT id, file_name, file_path, overview, tags, created_at, width, height, format FROM image_analysis WHERE 1=1'
    params = []

    # Hidden folders filter (always applied)
    try:
        c_hidden = conn.cursor()
        c_hidden.execute("SELECT folder_path FROM folder_visibility WHERE is_hidden = 1")
        hidden_paths = [r['folder_path'] for r in c_hidden.fetchall()]
        for hp in hidden_paths:
            sql += ' AND INSTR(file_path, ?) = 0'
            params.append(chr(92) + hp + chr(92))
    except Exception:
        pass

    # Active sources filter
    active_roots = get_active_source_roots()
    if not active_roots:
        conn.close()
        return jsonify([])
    sql += ' AND ('
    for i, root in enumerate(active_roots):
        if i > 0:
            sql += ' OR '
        sql += ' INSTR(file_path, ?) = 1'
        params.append(root)
    sql += ')'

    # Folder filter
    if folder_names:
        sql += ' AND ('
        for i, folder in enumerate(folder_names):
            if i > 0:
                sql += ' OR '
            sql += ' INSTR(file_path, ?) > 0'
            params.append(chr(92) + folder + chr(92))
        sql += ')'

    # Year filter (YYYY)
    if year_filter:
        sql += ' AND SUBSTR(created_at, 1, 4) = ?'
        params.append(year_filter)

    # Month filter (YYYY-MM)
    if month_filter:
        sql += ' AND SUBSTR(created_at, 1, 7) = ?'
        params.append(month_filter)

    # Day filter (YYYY-MM-DD)
    if date_filter:
        sql += ' AND SUBSTR(created_at, 1, 10) = ?'
        params.append(date_filter)

    if query:
        sql += ' AND (overview LIKE ? OR extracted_text LIKE ? OR other_info LIKE ? OR tags LIKE ? OR file_name LIKE ? OR file_path LIKE ?)'
        search_param = '%' + query + '%'
        params.extend([search_param] * 6)

    # Keyset pagination: after=created_at|id
    if after_cursor:
        parts = after_cursor.split('|', 1)
        if len(parts) == 2:
            after_mtime = parts[0]
            try:
                after_id = int(parts[1])
            except ValueError:
                conn.close()
                return jsonify({'error': 'Invalid cursor'}), 400
            sql += ' AND (created_at < ? OR (created_at = ? AND id < ?))'
            params.extend([after_mtime, after_mtime, after_id])

    sql += ' ORDER BY created_at DESC, id DESC'

    if limit:
        sql += ' LIMIT ?'
        params.append(limit)

    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()

    photos = []
    for row in rows:
        photos.append({
            'id': row['id'],
            'file_name': row['file_name'],
            'file_path': row['file_path'].replace(chr(92), '/'),
            'overview': row['overview'],
            'tags': row['tags'],
            'created_at': row['created_at'],
            'width': row['width'],
            'height': row['height'],
            'format': row['format']
        })

    return jsonify(photos)

@app.route('/api/folders')
def get_folders():
    """Return folders grouped by first-level directory across active sources.
    Uses per-source root_path length for correct SUBSTR offset."""
    conn = get_db_connection()
    cursor = conn.cursor()

    active_roots = get_active_source_roots()
    if not active_roots:
        conn.close()
        return jsonify([])

    from collections import Counter
    folder_counts = Counter()
    bs = chr(92)

    for root in active_roots:
        prefix_len = len(root)
        cursor.execute(f"""
            SELECT DISTINCT SUBSTR(SUBSTR(file_path, ?), 1,
                   INSTR(SUBSTR(file_path, ?), ?) - 1) as folder,
                   COUNT(*) as count
            FROM image_analysis
            WHERE INSTR(file_path, ?) = 1
            GROUP BY folder
            ORDER BY folder
        """, (prefix_len + 1, prefix_len + 1, bs, root))
        for row in cursor.fetchall():
            if row['folder'] and row['folder'].strip():
                folder_counts[row['folder']] += row['count']

    # Get hidden folders from folder_visibility (separate cursor)
    hidden = set()
    try:
        c_hidden = conn.cursor()
        c_hidden.execute("SELECT folder_path FROM folder_visibility WHERE is_hidden = 1")
        hidden = set(r['folder_path'] for r in c_hidden.fetchall())
    except Exception:
        pass

    folders = []
    for name, cnt in sorted(folder_counts.items(), key=lambda x: x[0].lower()):
        if name not in hidden:
            folders.append({'name': name, 'count': cnt})
    conn.close()
    return jsonify(folders)

@app.route('/api/timeline')
def get_timeline():
    """Return distinct dates for the current filter (folder + query)."""
    query = request.args.get('q', '').strip()

    conn = get_db_connection()
    cursor = conn.cursor()

    sql = """
        SELECT DISTINCT SUBSTR(created_at, 1, 10) as date
        FROM image_analysis
        WHERE created_at IS NOT NULL AND 1=1
    """
    params = []

    # Hidden folders filter (always applied)
    try:
        c_hidden = conn.cursor()
        c_hidden.execute("SELECT folder_path FROM folder_visibility WHERE is_hidden = 1")
        hidden_paths = [r['folder_path'] for r in c_hidden.fetchall()]
        for hp in hidden_paths:
            sql += ' AND INSTR(file_path, ?) = 0'
            params.append(chr(92) + hp + chr(92))
    except Exception:
        pass

    # Active sources filter
    active_roots = get_active_source_roots()
    if not active_roots:
        conn.close()
        return jsonify([])
    sql += ' AND ('
    for i, root in enumerate(active_roots):
        if i > 0:
            sql += ' OR '
        sql += ' INSTR(file_path, ?) = 1'
        params.append(root)
    sql += ')'

    if query:
        sql += ' AND (overview LIKE ? OR extracted_text LIKE ? OR other_info LIKE ? OR tags LIKE ? OR file_name LIKE ? OR file_path LIKE ?)'
        search_param = '%' + query + '%'
        params.extend([search_param] * 6)

    sql += ' ORDER BY date DESC'
    cursor.execute(sql, params)
    dates = [row['date'] for row in cursor.fetchall()]
    conn.close()
    return jsonify(dates)

@app.route('/api/file/<int:photo_id>')
def serve_file(photo_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT file_path FROM image_analysis WHERE id = ?', (photo_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        abort(404)

    # Reject photos from inactive sources
    active_roots = get_active_source_roots()
    matched_root = None
    for root in active_roots:
        if row['file_path'].startswith(root):
            matched_root = root
            break
    if not matched_root:
        abort(404)

    # Resolve local file path using the matched active source root
    db_path = row['file_path']
    rel_path = db_path[len(matched_root):]
    local_path = os.path.join(matched_root.rstrip(chr(92)), rel_path.lstrip(chr(92)))
    local_path = os.path.normpath(local_path)

    if not os.path.exists(local_path):
        print('File not found: ' + local_path)
        abort(404)

    ext = os.path.splitext(local_path)[1].lower()
    if ext in ('.heic', '.heif'):
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        try:
            img = Image.open(local_path)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=90)
            buf.seek(0)
            return send_file(buf, mimetype='image/jpeg')
        except Exception as e:
            print(f'HEIC conversion failed: {e}')
            abort(500)
    return send_file(local_path)

@app.route('/api/thumb/<int:photo_id>')
def serve_thumbnail(photo_id):
    # Verify photo belongs to an active source
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT file_path FROM image_analysis WHERE id = ?', (photo_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        active_roots = get_active_source_roots()
        if not any(row['file_path'].startswith(root) for root in active_roots):
            abort(404)

    path = THUMB_MAP.get(str(photo_id))
    if path:
        resp = send_file(path, last_modified=os.path.getmtime(path))
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return resp

    # Not in map - check disk or generate on the fly
    if row:
        file_path = row['file_path']
        expected = thumb_filename(photo_id, file_path)
        thumb_path = os.path.join(THUMB_DIR, expected)
        if os.path.exists(thumb_path):
            THUMB_MAP[str(photo_id)] = thumb_path
            resp = send_file(thumb_path, last_modified=os.path.getmtime(thumb_path))
            resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
            return resp
        try:
            os.makedirs(THUMB_DIR, exist_ok=True)
            res = try_generate(file_path, thumb_path)
            if res:
                THUMB_MAP[str(photo_id)] = thumb_path
                w, h = res
                try:
                    conn2 = sqlite3.connect(DATABASE)
                    conn2.execute(
                        "UPDATE image_analysis SET width=?, height=?, has_thumbnail=1 WHERE id=?",
                        (w, h, photo_id))
                    conn2.commit()
                    conn2.close()
                except Exception:
                    pass
                resp = send_file(thumb_path, last_modified=os.path.getmtime(thumb_path))
                resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
                return resp
        except Exception as e:
            print(f'[thumb] on-the-fly generation failed for {photo_id}: {e}')
    abort(404)

@app.route('/api/folder-visibility-version')
def folder_visibility_version():
    """Return current folder_visibility version. Clients poll this to detect changes."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM admin_settings WHERE key='folder_visibility_version'")
        row = cursor.fetchone()
        conn.close()
        version = row['value'] if row else ''
    except Exception:
        version = ''
    return jsonify({'version': version})

if __name__ == '__main__':
    app.run(debug=False, port=5000)
