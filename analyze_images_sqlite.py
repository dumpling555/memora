"""
读取指定目录的图片，通过 Anthropic Claude API 分析内容并提取文字。
结果存入 SQLite 数据库，结构化存储，一一对应。

依赖: pip install anthropic tqdm pillow-heif

运行: python analyze_images_sqlite.py

修复日志:
  - SMB 路径同时检测 \\\\ 和 // 前缀
  - file_path 作为唯一键，避免跨目录同名文件冲突
  - INSERT OR REPLACE 改为 ON CONFLICT 避免重复记录
  - 图片处理使用 BytesIO 避免 SMB 路径崩溃
  - API 调用增加 3 次重试
  - 空结果检测，失败文件记录到日志
  - 补充 HEIC 品牌码 (hif1, avif, av1f)
  - 解析空文本保护
  - 数据库连接复用
  - 添加安全提示词，避免 AI 拒绝回答敏感图片
  - 修复 overview 解析：跳过以分隔符开头的坏结果
  - 修复 overview 解析：跳过以"文字提取"开头的截断结果
  - overview 质量校验：太短或格式错误的重新请求
"""

import base64
import os
import re
import sys
import sqlite3
import io
import time
from datetime import datetime
from pathlib import Path

# Windows 下强制 UTF-8 输出
os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from tqdm import tqdm
import requests
from PIL import Image


# ============ 数据库初始化 ============

def init_db(db_path: Path) -> sqlite3.Connection:
    """创建 SQLite 数据库和表结构。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS image_analysis (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name   TEXT,
            file_path   TEXT UNIQUE NOT NULL,
            file_size   INTEGER,
            width       INTEGER,
            height      INTEGER,
            format      TEXT,

            overview    TEXT,
            extracted_text   TEXT,
            other_info TEXT,
            raw_result  TEXT,

            analyzed_at TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_image_analysis_file_path ON image_analysis(file_path);
        CREATE INDEX IF NOT EXISTS idx_image_analysis_analyzed_at ON image_analysis(analyzed_at);
    """)
    conn.commit()
    return conn


# ============ 解析 Claude 结构化输出 ============

def parse_claude_result(text: str) -> dict:
    """从 Claude 的文本输出中解析出 overview / extracted_text / other_info。"""
    if not text or not text.strip():
        return {"overview": "", "extracted_text": "", "other_info": ""}

    # 用 finditer 找到所有 === xxx === 分隔符的位置
    delimiter_pattern = r"={3,}\s*\S+\s*={3,}"
    matches = list(re.finditer(delimiter_pattern, text))

    sections = {
        "overview": "",
        "extracted_text": "",
        "other_info": "",
    }
    keys = ["overview", "extracted_text", "other_info"]

    for idx, key in enumerate(keys):
        start = matches[idx].end() if idx < len(matches) else 0
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[key] = text[start:end].strip()

    # 检测解析是否出错：overview 以分隔符开头或只有"文字提取"等截断内容
    overview = sections["overview"]
    if (overview.startswith("===") or
        overview.startswith("====") or
        overview == "文字提取" or
        (overview.startswith("文字提取") and len(overview) < 10) or
        overview == "这张图片" or
        len(overview) < 3):
        # 解析失败，返回原始文本让调用方重试
        return {"_raw": text, "overview": "", "extracted_text": "", "other_info": ""}

    return sections


# ============ 图片分析 ============

def prepare_image(image_path: str) -> tuple:
    """读取/转换图片，返回 (image_data, media_type, w, h)。"""
    with open(image_path, "rb") as f:
        image_data = f.read()

    media_type_orig = None
    # 检测 HEIC/HEIF 格式
    if len(image_data) > 12 and image_data[4:8] == b"ftyp":
        brand = image_data[8:12]
        if brand in (b"mif1", b"heic", b"hevx", b"heim", b"heis", b"hevs", b"hif1", b"avif", b"av1f"):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
                img = Image.open(image_path)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                buf.seek(0)
                image_data = buf.read()
                media_type_orig = "image/jpeg"
                print(f"  [HEIC] 已转换为 JPEG ({len(image_data)} bytes)")
            except Exception as e:
                print(f"  [警告] HEIC 转换失败: {e}，尝试原格式发送")

    ext = Path(image_path).suffix.lower()
    media_type_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".bmp": "image/bmp", ".gif": "image/gif", ".tiff": "image/tiff",
        ".tif": "image/tiff", ".webp": "image/webp",
    }
    media_type = media_type_map.get(ext, "image/png")
    if media_type_orig:
        media_type = media_type_orig

    # 检测 ARW (Sony RAW) 格式
    is_arw = False
    if ext == ".arw":
        # 尝试用 rawpy 解码 ARW
        try:
            import rawpy
            with rawpy.imread(image_path) as raw:
                params = rawpy.Params(no_auto_bright=True, output_bps=8)
                rgb = raw.postprocess(params)
                from PIL import Image as PILImage
                img = PILImage.fromarray(rgb, mode="RGB")
                w, h = img.size
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                buf.seek(0)
                image_data = buf.read()
                media_type = "image/jpeg"
                is_arw = True
                print(f"  [ARW] 已转换为 JPEG ({len(image_data)} bytes)")
        except Exception as e:
            print(f"  [警告] ARW 解码失败: {e}")
            is_arw = False

    if not is_arw:
        # 压缩大图片（非 ARW 路径）
        img_buf = io.BytesIO(image_data)
        img = Image.open(img_buf)
        w, h = img.size
        MAX_WIDTH = 1920
        if w > MAX_WIDTH:
            ratio = MAX_WIDTH / w
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            buf.seek(0)
            image_data = buf.read()
            print(f"  压缩: {w}x{h} -> {new_w}x{new_h}, {len(image_data)} bytes")

    return image_data, media_type, w, h


def image_to_b64(image_data: bytes) -> str:
    return base64.b64encode(image_data).decode("utf-8")


def analyze_image_with_lmstudio(image_b64: str, media_type: str, max_retries: int = 3) -> str:
    """使用 LM Studio (http://172.18.18.100:1234/v1) 分析图片内容并提取文字。"""
    api_url = "http://172.18.18.100:1234/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": "google/gemma-4-26b-a4b",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "请客观描述这张图片的内容。只需描述画面中实际存在的内容，不要做价值判断。\n"
                            "严格按以下格式输出：\n"
                            "=== 概况 ===\n"
                            "<对图片整体内容、场景、主要元素、用途的简要描述，不超过100字>\n"
                            "\n"
                            "=== 文字提取 ===\n"
                            "<提取图片中可见的所有文字，保持原文顺序。代码请保留缩进格式。\n"
                            "如果是界面文字，按从上到下、从左到右的顺序逐行输出。\n"
                            "如果是表格，用文本表格形式呈现。>\n"
                            "\n"
                            "=== 其他信息 ===\n"
                            "<颜色、风格、构图等补充信息>\n"
                            "\n"
                            "注意：概览部分必须是对图片内容的实际描述，不要输出'文字提取'等占位文字。"
                        ),
                    },
                ],
            }
        ],
    }

    last_err = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=120)
            if resp.status_code != 200:
                raise Exception(f"API 返回 {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise Exception("API 返回空 choices")
            raw_result = choices[0].get("message", {}).get("content", "")

            if not raw_result.strip():
                raise Exception("API 返回空结果")

            return raw_result

        except Exception as e:
            last_err = e
            print(f"  [API 重试 {attempt + 1}/{max_retries}] {type(e).__name__}: {e}")
            time.sleep(5)

    raise last_err


# ============ 标签生成 ============

def call_tag_lm(overviews: list, max_retries: int = 3) -> str:
    """批量调用 LM Studio 为多条 overview 生成中文标签。"""
    lines = []
    for rid, fname, overview in overviews:
        desc = (overview or "")[:200]
        lines.append(f"{rid}: {desc}")

    prompt = (
        "请为以下每张图片生成简短中文标签，每行格式：ID 标签1、标签2、标签3\n\n"
        + "\n".join(lines)
    )

    payload = {
        "model": "google/gemma-4-26b-a4b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.1,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "http://172.18.18.100:1234/v1/chat/completions",
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"].get("content", "")
            if content:
                return content

            reasoning = body["choices"][0]["message"].get("reasoning_content", "")
            if not reasoning:
                raise RuntimeError("LM Studio 返回空")

            tags = []
            for line in reasoning.split("\n"):
                s = line.strip()
                m = re.match(r'^(\d+)[\s:]+(.+)$', s)
                if m and "、" in m.group(2):
                    tags.append((int(m.group(1)), m.group(2)))

            if not tags:
                raise RuntimeError("未从 reasoning 中找到标签行")

            last_seen = {}
            for rid, tag_str in tags:
                last_seen[rid] = tag_str
            sorted_ids = sorted(last_seen.keys())
            return "\n".join(f"{rid}: {last_seen[rid]}" for rid in sorted_ids)

        except Exception as e:
            print(f"  [标签重试 {attempt + 1}/{max_retries}] {type(e).__name__}: {e}")
            time.sleep(5)

    return ""


def extract_tags(tags_text: str, allowed_ids: list) -> dict:
    """从 LM Studio 返回的文本中解析标签。"""
    allowed = set(str(x) for x in allowed_ids)
    result = {}
    for line in tags_text.split("\n"):
        s = line.strip()
        m = re.match(r'^(\d+)[\s:]+(.+)$', s)
        if m and m.group(1) in allowed and "、" in m.group(2):
            result[int(m.group(1))] = m.group(2)
    return result


# ============ 主流程 ============

def main():
    db_path = Path(__file__).parent / "res.sqlite"

    # === 模式选择 ===
    TEST_MODE = True  # True = 分析 + 标签生成（末10条），False = 全量扫描

    img_exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp", ".arw"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if TEST_MODE:
        # 取最后 10 条记录，是否有 overview 都行
        cursor.execute("SELECT id, file_path, file_name, overview FROM image_analysis ORDER BY id DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            print("数据库中没有记录。")
            return
        print(f"测试模式：处理最后 {len(rows)} 条记录")
        # 分离需要分析的（无 overview）和已就绪的
        need_analysis = [(Path(r['file_path']), r['file_name'], r['id']) for r in rows if not r['overview']]
        ready_for_tags = [(r['id'], r['file_name'], r['overview']) for r in rows if r['overview']]
        pending_paths = [(fp, fn, rid) for fp, fn, rid in need_analysis]
        if pending_paths:
            print(f"需图片分析: {len(pending_paths)} 张")
            for fp, fn, _ in pending_paths:
                print(f"  - {fn}")
        if ready_for_tags:
            print(f"已有概述，直接标签: {len(ready_for_tags)} 条")
        print()

    else:
        # 从 media_sources 表读取源目录
        cursor.execute("SELECT id, name, root_path FROM media_sources")
        sources = cursor.fetchall()
        conn.close()

        if not sources:
            print("数据库中没有媒体源，请先在 Admin 页面添加。")
            return

        target_dirs = []
        for src in sources:
            p = Path(src['root_path'])
            if p.exists():
                target_dirs.append(p)
            else:
                print(f"  [跳过] 路径不存在: {src['root_path']}")

        if not target_dirs:
            print("没有可用的目标目录。")
            return

        # 收集所有图片文件
        img_files = []
        for d in target_dirs:
            img_files.extend(f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in img_exts)
        img_files = sorted(set(img_files))

        if not img_files:
            print("目录中没有找到图片文件。")
            return

        # 排除已分析过的
        conn2 = sqlite3.connect(str(db_path))
        c2 = conn2.execute("SELECT file_path FROM image_analysis")
        analyzed_paths = {row[0] for row in c2.fetchall()}
        conn2.close()

        pending_paths = [(f, f.name) for f in img_files if str(f) not in analyzed_paths]
        if not pending_paths:
            print(f"共 {len(img_files)} 张图片，全部已分析完毕，无需重复分析。")
            return

        print(f"共找到 {len(img_files)} 张图片，已有 {len(analyzed_paths)} 张已分析")
        print(f"本次待分析: {len(pending_paths)} 张\n")

        for src in target_dirs:
            print(f"  - {src}")

    # ---- 清理证书环境变量 ----
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    os.environ.pop("CURL_CA_BUNDLE", None)

    print(f"数据库:   {db_path}")
    print(f"API:      http://172.18.18.100:1234/v1 (google/gemma-4-26b-a4b)\n")

    # 失败日志
    fail_log = Path(__file__).parent / "failed_analysis_log.txt"
    with open(fail_log, "a", encoding="utf-8") as log_f:
        log_f.write(f"\n{'=' * 70}\n")
        log_f.write(f"失败记录追加 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        log_f.write(f"{'=' * 70}\n")

    results = {}
    error_count = 0

    db_conn = init_db(db_path)

    for fpath, fname, rid in tqdm(pending_paths, desc="分析进度", unit="张", ncols=80, bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"):
        fpath_str = str(fpath)
        print(f"\n{'─' * 60}")
        print(f"文件: {fname}")

        try:
            fsize = fpath.stat().st_size
            print(f"大小: {fsize / 1024:.1f} KB")
        except (OSError, FileNotFoundError) as e:
            print(f"  [错误] 无法访问文件: {e}")
            with open(fail_log, "a", encoding="utf-8") as log_f:
                log_f.write(f"[文件不可访问] {fpath_str} - {e}\n")
            error_count += 1
            continue

        try:
            image_data, media_type, w, h = prepare_image(fpath_str)
            image_b64 = image_to_b64(image_data)

            raw_result = analyze_image_with_lmstudio(image_b64, media_type)
            parsed = parse_claude_result(raw_result)

            if parsed.get("_raw"):
                print(f"  [解析失败] 检测到坏结果，重新请求...")
                retry_text = (
                    "请描述这张图片的内容。注意：概览必须是实际描述，不要输出占位文字。\n"
                    "=== 概况 ===\n<实际描述>\n"
                    "=== 文字提取 ===\n<提取的文字>\n"
                    "=== 其他信息 ===\n<补充信息>\n"
                )
                retry_resp = requests.post(
                    "http://172.18.18.100:1234/v1/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": "google/gemma-4-26b-a4b",
                        "max_tokens": 4096,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
                                {"type": "text", "text": retry_text}
                            ]
                        }]
                    },
                    timeout=120
                )
                retry_data = retry_resp.json()
                retry_choices = retry_data.get("choices", [])
                retry_result = retry_choices[0].get("message", {}).get("content", "") if retry_choices else ""
                if retry_result.strip():
                    parsed = parse_claude_result(retry_result)
                    raw_result = retry_result
                    print(f"  [重试成功] 概览: {parsed['overview'][:60]}")
                else:
                    print(f"  [警告] 重试仍返回空，跳过写入")
                    with open(fail_log, "a", encoding="utf-8") as log_f:
                        log_f.write(f"[解析失败且重试为空] {fpath_str}\n")
                    error_count += 1
                    continue

            if not parsed["overview"] and not parsed["extracted_text"]:
                print(f"  [警告] 分析结果为空，跳过写入")
                with open(fail_log, "a", encoding="utf-8") as log_f:
                    log_f.write(f"[分析结果为空] {fpath_str}\n")
                error_count += 1
                continue

            overview = parsed["overview"]
            if overview and len(overview) < 5:
                print(f"  [警告] overview 太短 ({len(overview)} 字符)，重新请求...")
                retry_text = (
                    "请客观描述这张图片的内容。概览部分必须是实际描述，不超过100字。\n"
                    "=== 概况 ===\n<实际描述>\n"
                    "=== 文字提取 ===\n<提取的文字>\n"
                    "=== 其他信息 ===\n<补充信息>\n"
                )
                retry_resp2 = requests.post(
                    "http://172.18.18.100:1234/v1/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": "google/gemma-4-26b-a4b",
                        "max_tokens": 4096,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
                                {"type": "text", "text": retry_text}
                            ]
                        }]
                    },
                    timeout=120
                )
                retry_data2 = retry_resp2.json()
                retry_choices2 = retry_data2.get("choices", [])
                retry_result = retry_choices2[0].get("message", {}).get("content", "") if retry_choices2 else ""
                if retry_result.strip():
                    parsed = parse_claude_result(retry_result)
                    raw_result = retry_result
                    print(f"  [重试成功] 概览: {parsed['overview'][:60]}")
                else:
                    print(f"  [警告] 重试仍返回空，跳过写入")
                    with open(fail_log, "a", encoding="utf-8") as log_f:
                        log_f.write(f"[overview太短且重试为空] {fpath_str}\n")
                    error_count += 1
                    continue

            try:
                buf = io.BytesIO()
                with open(fpath_str, "rb") as f:
                    buf.write(f.read())
                buf.seek(0)
                with Image.open(buf) as img:
                    w, h = img.size
                    fmt = img.format or "JPEG"
            except Exception:
                w, h, fmt = None, None, None

            db_conn.execute("""
                INSERT INTO image_analysis
                    (file_name, file_path, file_size, width, height, format,
                     overview, extracted_text, other_info, raw_result, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    width = excluded.width,
                    height = excluded.height,
                    format = excluded.format,
                    overview = excluded.overview,
                    extracted_text = excluded.extracted_text,
                    other_info = excluded.other_info,
                    raw_result = excluded.raw_result,
                    analyzed_at = excluded.analyzed_at
            """, (
                fname,
                fpath_str,
                fsize,
                w, h, fmt,
                parsed["overview"], parsed["extracted_text"], parsed["other_info"],
                raw_result, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            db_conn.commit()

            results[fpath_str] = parsed
            print(f"\n  概况:   {parsed['overview'][:80]}..." if len(parsed['overview']) > 80 else f"  概况:   {parsed['overview']}")
            text_len = len(parsed['extracted_text'])
            print(f"  文字提取: {text_len} 字符")
            print(f"  -> 已保存至 {db_path.name}")

        except (OSError, FileNotFoundError) as e:
            print(f"  [网络错误] 无法读取文件: {e}")
            with open(fail_log, "a", encoding="utf-8") as log_f:
                log_f.write(f"[网络错误] {fpath_str} - {e}\n")
            error_count += 1
        except Exception as e:
            print(f"  [错误] {type(e).__name__}: {e}")
            with open(fail_log, "a", encoding="utf-8") as log_f:
                log_f.write(f"[{type(e).__name__}] {fpath_str} - {e}\n")
            error_count += 1

        time.sleep(2)

    db_conn.close()

    # ============ 标签生成 ============
    print(f"\n{'=' * 60}")
    print("开始批量生成标签...")

    # 收集刚分析完 + 已有 overview 的记录
    conn = sqlite3.connect(str(db_path))
    if TEST_MODE:
        rows = conn.execute("SELECT id, file_name, overview FROM image_analysis ORDER BY id DESC LIMIT 10").fetchall()
    else:
        rows = conn.execute("SELECT id, file_name, overview FROM image_analysis WHERE overview IS NOT NULL AND tags IS NULL").fetchall()
    conn.close()

    tag_records = [(r[0], r[1], r[2]) for r in rows if r[2]]
    if tag_records:
        print(f"共 {len(tag_records)} 条记录需要生成标签，分批处理...")
        BATCH_SIZE = 10
        all_tags = {}

        for i in range(0, len(tag_records), BATCH_SIZE):
            batch = tag_records[i:i + BATCH_SIZE]
            batch_ids = [r[0] for r in batch]
            print(f"  标签批次 {i // BATCH_SIZE + 1} (ID {batch_ids[0]}-{batch_ids[-1]})...")

            tags_text = call_tag_lm(batch)
            if not tags_text.strip():
                print(f"  [警告] 标签生成返回空")
                continue

            parsed = extract_tags(tags_text, batch_ids)
            all_tags.update(parsed)
            print(f"  成功: {len(parsed)} 条")

            time.sleep(1)  # 避免限流

        # 写入 DB
        if all_tags:
            conn = sqlite3.connect(str(db_path))
            for rid, tag_str in all_tags.items():
                conn.execute("UPDATE image_analysis SET tags = ? WHERE id = ?", (tag_str, rid))
            conn.commit()
            conn.close()
            print(f"\n已更新 {len(all_tags)} 条标签到数据库")
    else:
        print("没有需要生成标签的记录。")

    # ============ 汇总 ============
        log_f.write(f"\n[汇总] 本次失败: {error_count} 张\n")

    print(f"\n{'=' * 60}")
    print(f"完成! 共分析 {len(results)} 张图片")
    print(f"失败: {error_count} 张")
    print(f"结果已存入: {db_path}")
    print(f"失败记录: {fail_log}")

    conn = init_db(db_path)
    total = conn.execute("SELECT COUNT(*) FROM image_analysis").fetchone()[0]
    print(f"数据库中累计: {total} 条记录")
    conn.close()


if __name__ == "__main__":
    main()
