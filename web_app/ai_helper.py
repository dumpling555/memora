import base64
import os
import re
import io
import time
import requests
from pathlib import Path
from PIL import Image

def parse_claude_result(text: str) -> dict:
    """Extract overview, extracted_text, and other_info from LM Studio/Claude text response."""
    if not text or not text.strip():
        return {"overview": "", "extracted_text": "", "other_info": ""}

    # Find delimiters like === Overview ===
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

    # Detect parsing failures
    overview = sections["overview"]
    if (overview.startswith("===") or
        overview.startswith("====") or
        overview == "文字提取" or
        (overview.startswith("文字提取") and len(overview) < 10) or
        overview == "这张图片" or
        len(overview) < 3):
        return {"_raw": text, "overview": "", "extracted_text": "", "other_info": ""}

    return sections

def prepare_image(image_path: str) -> tuple:
    """Read and preprocess an image, converting it to a standard 3-channel RGB JPEG format."""
    ext = Path(image_path).suffix.lower()

    # Special handling for Sony RAW (ARW)
    if ext == ".arw":
        try:
            import rawpy
            with rawpy.imread(image_path) as raw:
                params = rawpy.Params(no_auto_bright=True, output_bps=8)
                rgb = raw.postprocess(params)
                img = Image.fromarray(rgb, mode="RGB")
                w, h = img.size
                
                # Resize if necessary
                MAX_WIDTH = 1920
                if w > MAX_WIDTH:
                    ratio = MAX_WIDTH / w
                    w = int(w * ratio)
                    h = int(h * ratio)
                    img = img.resize((w, h), Image.LANCZOS)
                
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                buf.seek(0)
                image_data = buf.read()
                print(f"  [ARW] Decoded and converted to JPEG ({w}x{h}, {len(image_data)} bytes)")
                return image_data, "image/jpeg", w, h
        except Exception as e:
            print(f"  [Warning] ARW decode failed: {e}, falling back to standard loader")

    # Special handling for HEIC/HEIF
    if ext in (".heic", ".heif"):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception as e:
            print(f"  [Warning] pillow_heif register failed: {e}")

    # Standard loader using PIL
    try:
        img = Image.open(image_path)
        
        # Always convert to RGB mode to drop alpha channel (RGBA/PNG) and index color palettes (GIF/BMP)
        if img.mode != "RGB":
            img = img.convert("RGB")
            
        w, h = img.size
        MAX_WIDTH = 1920
        if w > MAX_WIDTH:
            ratio = MAX_WIDTH / w
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            w, h = new_w, new_h

        # Always save as JPEG to guarantee a 3-channel compressed representation compatible with VLM
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        image_data = buf.read()
        
        return image_data, "image/jpeg", w, h
    except Exception as e:
        # Extreme fallback: just read raw bytes
        print(f"  [Warning] PIL prepare failed: {e}, falling back to raw file bytes")
        with open(image_path, "rb") as f:
            raw_data = f.read()
        return raw_data, "image/jpeg", 0, 0

def image_to_b64(image_data: bytes) -> str:
    return base64.b64encode(image_data).decode("utf-8")

def get_llm_config():
    """Load LLM settings from the database."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'res.sqlite')
    api_url = None
    model_name = None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT key, value FROM admin_settings WHERE key IN ('llm_api_url', 'llm_model_name')")
        for key, value in c.fetchall():
            if key == 'llm_api_url' and value:
                api_url = value.strip()
            elif key == 'llm_model_name' and value:
                model_name = value.strip()
        conn.close()
    except Exception as e:
        print(f"[ai_helper] Error loading LLM config: {e}")
    return api_url, model_name

def analyze_image_with_lmstudio(image_b64: str, media_type: str, max_retries: int = 3) -> str:
    """Analyze image using LM Studio local endpoint."""
    base_url, model_name = get_llm_config()
    if not base_url or not model_name:
        raise ValueError("Local LLM is not configured. Analysis skipped.")
    api_url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model_name,
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
                raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise Exception("API returned empty choices")

            raw_result = choices[0].get("message", {}).get("content", "")
            if not raw_result.strip():
                raise Exception("API returned empty content")

            return raw_result
        except Exception as e:
            last_err = e
            print(f"  [API Retry {attempt + 1}/{max_retries}] {type(e).__name__}: {e}")
            time.sleep(5)

    raise last_err

def call_tag_lm(overviews: list, max_retries: int = 3) -> str:
    """Call LM Studio to generate Chinese tags from overviews in batch."""
    base_url, model_name = get_llm_config()
    if not base_url or not model_name:
        return ""
    api_url = f"{base_url.rstrip('/')}/chat/completions"
    lines = []
    for rid, fname, overview in overviews:
        desc = (overview or "")[:200]
        lines.append(f"{rid}: {desc}")

    prompt = (
        "请为以下每张图片生成简短中文标签，每行格式：ID 标签1、标签2、标签3\n\n"
        + "\n".join(lines)
    )

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.1,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                api_url,
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
                raise RuntimeError("LM Studio returned empty response")

            tags = []
            for line in reasoning.split("\n"):
                s = line.strip()
                m = re.match(r'^(\d+)[\s:]+(.+)$', s)
                if m and "、" in m.group(2):
                    tags.append((int(m.group(1)), m.group(2)))

            if not tags:
                raise RuntimeError("No tag lines found in reasoning_content")

            last_seen = {}
            for rid, tag_str in tags:
                last_seen[rid] = tag_str
            sorted_ids = sorted(last_seen.keys())
            return "\n".join(f"{rid}: {last_seen[rid]}" for rid in sorted_ids)

        except Exception as e:
            print(f"  [Tag Retry {attempt + 1}/{max_retries}] {type(e).__name__}: {e}")
            time.sleep(5)

    return ""

def extract_tags(tags_text: str, allowed_ids: list) -> dict:
    """Parse tag mapping from LM Studio returned text."""
    allowed = set(str(x) for x in allowed_ids)
    result = {}
    for line in tags_text.split("\n"):
        s = line.strip()
        m = re.match(r'^(\d+)[\s:]+(.+)$', s)
        if m and m.group(1) in allowed and "、" in m.group(2):
            result[int(m.group(1))] = m.group(2)
    return result
