"""
Regenerate all thumbnails for all sources, update generated=1 when done.
Progress written to gen.txt in real-time.
"""
import os, time, sqlite3, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from thumb_common import (THUMB_DIR, THUMB_SIZE, DATABASE,
                          thumb_filename, try_generate,
                          remove_stale_thumb, existing_thumb_map)

MAX_WORKERS = 24
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gen.txt')
BATCH = 200


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def generate_one(photo_id, file_path):
    """Generate thumbnail for one photo. Removes stale thumbs first. Returns (photo_id, success)."""
    # Remove any old wrong-hash thumbnails before generating
    remove_stale_thumb(photo_id, THUMB_DIR)

    thumb_path = os.path.join(THUMB_DIR, thumb_filename(photo_id, file_path))

    if os.path.exists(thumb_path):
        # Verify integrity: if too small (< 2 KB), regenerate
        if os.path.getsize(thumb_path) < 2048:
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        else:
            return photo_id, True

    ok = try_generate(file_path, thumb_path)

    # If failed, remove any partial file
    if not ok:
        try:
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
        except Exception:
            pass

    return photo_id, ok


def run():
    os.makedirs(THUMB_DIR, exist_ok=True)

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")

    # Step 1: Mark already-correct thumbnails as generated=1
    import hashlib
    c.execute("SELECT id, file_path FROM image_analysis")
    all_rows = c.fetchall()
    thumb_dir = THUMB_DIR
    updated_ids = []
    for pid, fp in all_rows:
        expected = f'{pid}-{hashlib.sha256(fp.encode()).hexdigest()}.jpg'
        if os.path.exists(os.path.join(thumb_dir, expected)):
            updated_ids.append(pid)
    if updated_ids:
        for upid in updated_ids:
            c.execute("UPDATE image_analysis SET generated = 1 WHERE id = ?", (upid,))
        conn.commit()
    already_done = len(updated_ids)
    log(f'Already have correct thumbnails: {already_done}')

    # Step 2: Load only the remaining (generated=0) photos
    c.execute("SELECT id, file_path FROM image_analysis WHERE generated = 0 ORDER BY id")
    remaining = c.fetchall()
    total = len(remaining)
    log(f'Remaining to process: {total}')
    if not remaining:
        log('Nothing to do!')
        conn.close()
        return

    done = success = fail = 0
    t0 = time.time()
    updated_ids = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(generate_one, pid, fp): pid for pid, fp in remaining}

        for fut in as_completed(fut_map):
            pid, ok = fut.result()
            done += 1
            updated_ids.append(pid)
            if ok:
                success += 1
            else:
                fail += 1

            if len(updated_ids) >= BATCH or done == total:
                try:
                    c2 = sqlite3.connect(DATABASE)
                    c2.execute("PRAGMA journal_mode=WAL")
                    for upid in updated_ids:
                        c2.execute("UPDATE image_analysis SET generated = 1 WHERE id = ?", (upid,))
                    c2.commit()
                    c2.close()
                except Exception as e:
                    log(f'DB update error: {e}')
                updated_ids = []

            if done % BATCH == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                log(f'Progress: {done}/{total} ({100*done//total}%) '
                    f'success={success} fail={fail} '
                    f'{rate:.0f} photos/s ETA {eta:.0f}s')

    elapsed = time.time() - t0
    log(f'=== Complete: {total} photos in {elapsed:.0f}s ({total/elapsed:.0f}/s) ===')
    log(f'Success: {success}, Failed: {fail}')
    conn.close()


if __name__ == '__main__':
    run()
