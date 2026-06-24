"""
Full scan scheduler: triggers a full scan for all sources every 24 hours.
Quick scans are handled by the independent QuickScanner (quick_scanner.py).
"""
import os
import threading
import time
import sqlite3

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')


class ScanScheduler:
    """Triggers full scan for all sources every 24h."""

    def __init__(self, check_interval=60):
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._last_full_run = 0
        self._full_interval = 1440 * 60
        self.trigger_scan_fn = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='scheduler')
        self._thread.start()
        print(f'[scheduler] started (full scan every 24h)')

    def stop(self):
        self._running = False
        print('[scheduler] stopped')

    @property
    def is_running(self):
        return self._running

    def _loop(self):
        while self._running:
            try:
                self._check_full()
            except Exception as e:
                print(f'[scheduler] error: {e}')
            time.sleep(self.check_interval)

    def _check_full(self):
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT value FROM admin_settings WHERE key='scheduler_enabled'")
        row = c.fetchone()
        if not row or row[0] != '1':
            conn.close()
            return

        c.execute("SELECT id FROM media_sources")
        all_sources = [r[0] for r in c.fetchall()]
        conn.close()

        now = time.time()
        if now - self._last_full_run < self._full_interval:
            return

        self._last_full_run = now
        print('[scheduler] 24h full scan triggered')
        for src_id in all_sources:
            if not self._running or not self.trigger_scan_fn:
                break
            try:
                self.trigger_scan_fn(src_id, mode='full')
            except Exception as e:
                print(f'[scheduler] full scan failed for source_id={src_id}: {e}')
