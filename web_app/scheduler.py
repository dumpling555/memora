"""
Full scan scheduler: DB-driven per-source scheduling from scan_schedules table.

Design:
- Each active schedule (is_active=1) defines its own interval_minutes
- No default interval — only sources with a schedule are scanned
- Overlap prevention: a source is not re-scanned while a previous scan is running
- Stop support: deactivating a schedule immediately aborts the running scan
- Restart recovery: next_run_at is read from DB on startup
"""
import os
import sqlite3
import threading
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')


class ScanScheduler:
    """DB-driven full scan scheduler, one schedule per media source."""

    def __init__(self, check_interval=60):
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # source_id -> {'thread': Thread, 'abort_event': Event, 'log_id': int}
        self._running_sources = {}

        # External callback: trigger_scan_fn(source_id, mode, abort_event) -> (log_id, thread)
        self.trigger_scan_fn = None

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='scheduler')
        self._thread.start()
        print('[scheduler] started (DB-driven per-source scheduling)')

    def stop(self):
        with self._lock:
            self._running = False
        # Abort all running scans
        with self._lock:
            running_ids = list(self._running_sources.keys())
        for src_id in running_ids:
            self.stop_source(src_id)
        print('[scheduler] stopped (all running scans aborted)')

    @property
    def is_running(self):
        return self._running

    def reload(self):
        """Called after schedule config changes in admin UI. No-op for now since
        _check() reads from DB every cycle. Could be extended to force an immediate check."""
        print('[scheduler] config reload requested')

    def is_source_running(self, source_id):
        """Check if a scan is currently running for the given source."""
        with self._lock:
            entry = self._running_sources.get(source_id)
            if entry and entry['thread'].is_alive():
                return True
            # Clean up dead thread entry
            if entry and not entry['thread'].is_alive():
                del self._running_sources[source_id]
            return False

    def stop_source(self, source_id):
        """Abort a running scan for the given source."""
        with self._lock:
            entry = self._running_sources.get(source_id)
            if entry and entry['thread'].is_alive():
                entry['abort_event'].set()
                print(f'[scheduler] abort signal sent for source_id={source_id}')
                # Update scan_log status
                try:
                    conn = sqlite3.connect(DATABASE)
                    conn.execute(
                        "UPDATE scan_log SET status='cancelled', finished_at=datetime('now'), "
                        "error_message='Stopped by user' WHERE id=? AND status='running'",
                        (entry['log_id'],)
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f'[scheduler] failed to update scan_log: {e}')
            # Clean up
            self._running_sources.pop(source_id, None)

        # Update schedule: set is_active=0 and clear next_run_at
        try:
            conn = sqlite3.connect(DATABASE)
            conn.execute(
                "UPDATE scan_schedules SET is_active=0, updated_at=datetime('now') "
                "WHERE media_source_id=?",
                (source_id,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f'[scheduler] failed to deactivate schedule: {e}')

    def _loop(self):
        while self._running:
            try:
                self._check()
            except Exception as e:
                print(f'[scheduler] error: {e}')
            # Sleep in small increments so stop() is responsive
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _get_schedules(self):
        """Fetch all active schedules from DB."""
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Check global scheduler_enabled flag
        c.execute("SELECT value FROM admin_settings WHERE key='scheduler_enabled'")
        row = c.fetchone()
        if not row or row[0] != '1':
            conn.close()
            return []

        c.execute("""
            SELECT ss.id, ss.media_source_id, ss.interval_minutes, ss.is_active,
                   ss.last_run_at, ss.next_run_at, ms.is_active AS source_active
            FROM scan_schedules ss
            LEFT JOIN media_sources ms ON ss.media_source_id = ms.id
            WHERE ss.is_active = 1 AND ms.is_active = 1
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def _check(self):
        """Check all active schedules and trigger scans that are due."""
        schedules = self._get_schedules()
        now = time.time()

        # Clean up finished threads
        with self._lock:
            dead = [sid for sid, e in self._running_sources.items()
                    if not e['thread'].is_alive()]
            for sid in dead:
                entry = self._running_sources.pop(sid)
                # Scan finished — update last_run_at and next_run_at in DB
                self._update_timestamps(sid, entry.get('interval_minutes', 1440))

        for sched in schedules:
            src_id = sched['media_source_id']
            interval = sched['interval_minutes']
            next_run_str = sched['next_run_at']

            # Check if already running (overlap prevention)
            if self.is_source_running(src_id):
                continue

            # Parse next_run_at from DB
            if next_run_str:
                try:
                    next_run_ts = time.mktime(time.strptime(next_run_str, '%Y-%m-%d %H:%M:%S'))
                except (ValueError, OverflowError):
                    next_run_ts = 0
            else:
                # No next_run_at means it's due immediately
                next_run_ts = 0

            if now < next_run_ts:
                continue  # Not due yet

            # Trigger full scan
            self._trigger(src_id, interval)

    def _trigger(self, source_id, interval_minutes):
        """Trigger a full scan for a source."""
        if not self.trigger_scan_fn:
            return

        abort_event = threading.Event()
        try:
            log_id, thread = self.trigger_scan_fn(source_id, mode='full', abort_event=abort_event)
        except Exception as e:
            print(f'[scheduler] full scan trigger failed for source_id={source_id}: {e}')
            return

        with self._lock:
            self._running_sources[source_id] = {
                'thread': thread,
                'abort_event': abort_event,
                'log_id': log_id,
                'interval_minutes': interval_minutes,
            }
        print(f'[scheduler] full scan triggered for source_id={source_id}, interval={interval_minutes}min')

    def _update_timestamps(self, source_id, interval_minutes):
        """Update last_run_at and next_run_at after a scan completes."""
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        next_run = time.strftime('%Y-%m-%d %H:%M:%S',
                                 time.localtime(time.time() + interval_minutes * 60))
        try:
            conn = sqlite3.connect(DATABASE)
            conn.execute(
                "UPDATE scan_schedules SET last_run_at=?, next_run_at=?, updated_at=datetime('now') "
                "WHERE media_source_id=?",
                (now, next_run, source_id)
            )
            conn.commit()
            conn.close()
            print(f'[scheduler] source_id={source_id}: last_run={now}, next_run={next_run}')
        except Exception as e:
            print(f'[scheduler] failed to update timestamps for source_id={source_id}: {e}')
