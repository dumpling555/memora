"""
Independent quick scanner: runs incremental scan for ALL sources every 30 minutes.
Separate from scheduler — no schedule table dependency, no full scan.

The longer interval is intentional: it allows NAS disks to spin down and sleep
between scans, reducing wear and power consumption.
"""

CYCLE_INTERVAL = 1800  # 30 minutes — long enough to let NAS disks sleep
import os
import time
import sqlite3
import threading

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')


class QuickScanner:
    """Background thread that scans all sources every check_interval seconds."""

    def __init__(self, check_interval=CYCLE_INTERVAL):
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._active_sources = set()
        self._last_run = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='quick-scanner')
        self._thread.start()
        print('[quick_scanner] started')

    def stop(self):
        self._running = False
        print('[quick_scanner] stopped')

    @property
    def is_running(self):
        return self._running

    def _loop(self):
        while self._running:
            try:
                self._run_cycle()
            except Exception as e:
                print(f'[quick_scanner] error: {e}')
            time.sleep(self.check_interval)

    def _run_cycle(self):
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT id, root_path FROM media_sources")
        sources = c.fetchall()
        conn.close()

        now = time.time()
        for src_id, root_path in sources:
            if not self._running:
                break

            if src_id in self._active_sources:
                continue

            last = self._last_run.get(src_id, 0)
            if now - last < self.check_interval:
                continue

            self._last_run[src_id] = now
            self._active_sources.add(src_id)

            t = threading.Thread(
                target=self._scan_source,
                args=(src_id, root_path),
                daemon=True,
                name=f'qs-{src_id}'
            )
            t.start()

    def _scan_source(self, src_id, root_path):
        try:
            from scanner import run_scan
            from thumb_daemon import ThumbnailDaemon

            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            c.execute(
                "INSERT INTO scan_log (media_source_id, status, started_at) VALUES (?, 'running', ?)",
                (src_id, now_str)
            )
            log_id = c.lastrowid
            conn.commit()
            conn.close()

            run_scan(src_id, log_id, mode='incremental')

            td = ThumbnailDaemon()
            td.run_once(src_id)

        except Exception as e:
            print(f'[quick_scanner] scan failed for source {src_id}: {e}')
        finally:
            self._active_sources.discard(src_id)
