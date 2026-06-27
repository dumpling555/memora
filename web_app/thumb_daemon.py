"""
Background thumbnail daemon.
Continuously generates thumbnails for all active media sources.
Retries failed items up to N times, then abandons them permanently.
Runs as a daemon thread from app startup.
Uses ThreadPoolExecutor for parallel SMB thumbnail generation.
"""
import os
import time
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from thumb_common import (THUMB_DIR, THUMB_SIZE, DATABASE,
                          thumb_filename, existing_thumb_map,
                          try_generate, find_thumb_file)

CYCLE_INTERVAL = 60
MAX_RETRIES = 5
MAX_WORKERS = 24


class ThumbnailDaemon:
    """Persistent background daemon for thumbnail generation."""

    def __init__(self, check_interval=CYCLE_INTERVAL, max_retries=MAX_RETRIES):
        self.check_interval = check_interval
        self.max_retries = max_retries
        self._running = False
        self._thread = None
        self._retries = {}
        self._abandoned = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='thumb-daemon')
        self._thread.start()
        print('[thumb_daemon] started')

    def stop(self):
        self._running = False
        print('[thumb_daemon] stopped')

    def run_once(self, source_id):
        """Synchronously process a single source for missing thumbnails."""
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT root_path FROM media_sources WHERE id = ?", (source_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            self._process_source(source_id, row[0])

    @property
    def is_running(self):
        return self._running

    def _loop(self):
        os.makedirs(THUMB_DIR, exist_ok=True)
        while self._running:
            try:
                self._process_all_sources()
            except Exception as e:
                print(f'[thumb_daemon] error: {e}')
            time.sleep(self.check_interval)

    def _process_all_sources(self):
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, root_path FROM media_sources WHERE is_active = 1")
        sources = cursor.fetchall()
        conn.close()
        for src_id, root in sources:
            if not self._running:
                break
            self._process_source(src_id, root)

    def _process_source(self, source_id, root_path):
        if not os.path.isdir(root_path):
            return

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        prefix_len = len(root_path)

        # Only select records that actually need attention: missing thumbnail OR missing dimensions
        cursor.execute("""
            SELECT id, file_path, width, height FROM image_analysis
            WHERE SUBSTR(file_path, 1, ?) = ? AND (has_thumbnail = 0 OR width IS NULL OR height IS NULL)
            ORDER BY id
        """, (prefix_len, root_path))
        records = cursor.fetchall()
        conn.close()

        if not records:
            return

        if source_id not in self._retries:
            self._retries[source_id] = {}
        if source_id not in self._abandoned:
            self._abandoned[source_id] = set()

        jobs = []
        completed_records = []    # [(photo_id, w, h)]
        missing_dimensions = []   # [(photo_id, file_path)]

        for photo_id, file_path, w, h in records:
            expected = thumb_filename(photo_id, file_path)
            thumb_path = os.path.join(THUMB_DIR, expected)

            if os.path.exists(thumb_path):
                # Thumbnail already exists on disk
                if not w or not h:
                    missing_dimensions.append((photo_id, file_path))
                else:
                    # Dimensions exist but has_thumbnail was 0; queue for DB sync
                    completed_records.append((photo_id, w, h))
            else:
                # Thumbnail does not exist on disk
                if photo_id in self._abandoned[source_id]:
                    continue
                jobs.append((photo_id, file_path, thumb_path))

        newly_completed = 0
        newly_errors = 0

        if jobs:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                fut_map = {pool.submit(try_generate, fp, tp): (pid, fp)
                           for pid, fp, tp in jobs if self._running}
                for fut in as_completed(fut_map):
                    if not self._running:
                        return
                    photo_id, file_path = fut_map[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        res = None

                    if res:
                        newly_completed += 1
                        w, h = res
                        completed_records.append((photo_id, w, h))
                        self._retries[source_id].pop(photo_id, None)
                        self._abandoned[source_id].discard(photo_id)
                    else:
                        count = self._retries[source_id].get(photo_id, 0) + 1
                        self._retries[source_id][photo_id] = count
                        newly_errors += 1
                        if count >= self.max_retries:
                            self._abandoned[source_id].add(photo_id)
                            print(f'[thumb_daemon] abandoned id={photo_id} after {count} failures')
                            try:
                                conn2 = sqlite3.connect(DATABASE)
                                conn2.execute("UPDATE image_analysis SET has_thumbnail = -1 WHERE id = ?", (photo_id,))
                                conn2.commit()
                                conn2.close()
                            except Exception:
                                pass

        # Sync completed thumbnails to database
        if completed_records:
            try:
                conn = sqlite3.connect(DATABASE)
                for pid, w, h in completed_records:
                    conn.execute(
                        "UPDATE image_analysis SET width=?, height=?, has_thumbnail=1 WHERE id=?",
                        (w, h, pid))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[thumb_daemon] error updating dimensions/has_thumbnail for completed thumbs: {e}")

        # Sync missing dimensions of existing thumbnails
        if missing_dimensions and self._running:
            print(f'[thumb_daemon] backfilling dimensions for {len(missing_dimensions)} existing thumbnails...')
            try:
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    def _get_size(pid, fp):
                        try:
                            with Image.open(fp) as img:
                                return pid, img.size[0], img.size[1]
                        except Exception:
                            return pid, None, None

                    fut_dims = [pool.submit(_get_size, pid, fp) for pid, fp in missing_dimensions if self._running]
                    conn = sqlite3.connect(DATABASE)
                    for fut in as_completed(fut_dims):
                        if not self._running:
                            break
                        pid, w, h = fut.result()
                        if w and h:
                            conn.execute(
                                "UPDATE image_analysis SET width=?, height=?, has_thumbnail=1 WHERE id=?",
                                (w, h, pid))
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f"[thumb_daemon] error backfilling dimensions: {e}")

        if newly_completed or newly_errors:
            print(f'[thumb_daemon] source_id={source_id}: '
                  f'+{newly_completed} thumbs, {newly_errors} fail, '
                  f'{len(self._abandoned[source_id])} abandoned total')
