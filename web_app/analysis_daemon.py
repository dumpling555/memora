"""
Background analysis daemon.
Analyzes unprocessed images (overview IS NULL) and generates tags using LM Studio.
Runs as a daemon thread from app startup.
"""
import os
import sys
import time
import sqlite3
import threading

_BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(_BASE, '..', 'res.sqlite')

CYCLE_INTERVAL = 60
MAX_RETRIES = 5
MAX_PER_CYCLE = 10  # max records to process in one cycle


class AnalysisDaemon:
    """Persistent background daemon for image analysis and tag generation."""

    def __init__(self, check_interval=CYCLE_INTERVAL):
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._retries = {}
        self._signal = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='analysis-daemon')
        self._thread.start()
        print('[analysis_daemon] started')

    def stop(self):
        self._running = False
        self._signal.set()
        print('[analysis_daemon] stopped')

    def run_once(self):
        """Signal the daemon to process pending records immediately."""
        self._signal.set()

    @property
    def is_running(self):
        return self._running

    def _loop(self):
        while self._running:
            try:
                self._process_pending()
            except Exception as e:
                print(f'[analysis_daemon] error: {e}')
            self._signal.wait(self.check_interval)
            self._signal.clear()

    def _process_pending(self):
        from ai_helper import (
            prepare_image, image_to_b64, analyze_image_with_lmstudio,
            parse_claude_result, call_tag_lm, extract_tags, get_llm_config,
        )

        api_url, model_name = get_llm_config()
        if not api_url or not model_name:
            return

        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT id, file_path, file_name FROM image_analysis WHERE overview IS NULL")
        pending = c.fetchall()
        conn.close()

        if not pending:
            return

        pending = [r for r in pending if self._retries.get(r[0], 0) < MAX_RETRIES]
        if not pending:
            return

        # Only process up to MAX_PER_CYCLE per cycle to avoid monopolizing resources
        pending = pending[:MAX_PER_CYCLE]
        print(f'[analysis_daemon] processing {len(pending)}/{len(pending)} pending records')
        results = []

        for record_id, file_path, file_name in pending:
            if not self._running:
                break

            print(f'[analysis_daemon] #{record_id}: {file_name}')
            try:
                image_data, media_type, w, h = prepare_image(file_path)
                image_b64 = image_to_b64(image_data)

                raw_result = analyze_image_with_lmstudio(image_b64, media_type)
                parsed = parse_claude_result(raw_result)

                if parsed.get("_raw"):
                    raise Exception("parse failed - retrying")

                if not parsed["overview"] and not parsed["extracted_text"]:
                    raise Exception("empty result")

                results.append((record_id, file_path, file_name, parsed, raw_result))
                self._retries.pop(record_id, None)
                time.sleep(3)  # rate limit between records

            except Exception as e:
                count = self._retries.get(record_id, 0) + 1
                self._retries[record_id] = count
                print(f'[analysis_daemon] #{record_id} failed ({count}/{MAX_RETRIES}): {type(e).__name__}: {e}')
                if count >= MAX_RETRIES:
                    print(f'[analysis_daemon] #{record_id} abandoned after {MAX_RETRIES} failures')
                time.sleep(2)
                continue

        if not results:
            return

        conn = sqlite3.connect(DATABASE)
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        for rid, fp, fn, parsed, raw in results:
            conn.execute("""
                UPDATE image_analysis SET
                    overview = ?, extracted_text = ?, other_info = ?,
                    raw_result = ?, analyzed_at = ?
                WHERE id = ?
            """, (
                parsed["overview"], parsed["extracted_text"], parsed["other_info"],
                raw, now_str, rid,
            ))
        conn.commit()

        analyzed_ids = [r[0] for r in results]
        print(f'[analysis_daemon] analyzed {len(analyzed_ids)} records')

        # Generate tags for newly analyzed records
        try:
            rows = conn.execute(
                "SELECT id, file_name, overview FROM image_analysis WHERE id IN ({}) AND overview IS NOT NULL".format(
                    ','.join('?' for _ in analyzed_ids)
                ), analyzed_ids
            ).fetchall()

            if rows:
                tag_records = [(r[0], r[1], r[2]) for r in rows if r[2]]
                tags_text = call_tag_lm(tag_records)
                if tags_text.strip():
                    parsed_tags = extract_tags(tags_text, analyzed_ids)
                    if parsed_tags:
                        for rid, tag_str in parsed_tags.items():
                            conn.execute("UPDATE image_analysis SET tags = ? WHERE id = ?", (tag_str, rid))
                        conn.commit()
                        print(f'[analysis_daemon] tags generated for {len(parsed_tags)} records')
        except Exception as e:
            print(f'[analysis_daemon] tag generation error: {e}')

        conn.close()
