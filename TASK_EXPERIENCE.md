# 图片分析任务经验总结

## 任务概况

| 项目 | 值 |
|------|-----|
| 任务时间 | 2026-05-25 16:39:39 → 2026-06-01 16:54:23 |
| 总耗时 | **7 天 0 小时 15 分钟** |
| 处理图片数 | **27,634 张** |
| 涉及目录 | 6 个（2020~2025 年碎片） |

---

## 代码修复记录

### 1. SMB 路径兼容（`\\\\` vs `//`）

**问题**：Python 的 `Path(r"\\myDiskstation\...")` 在 bash 环境下无法识别 SMB 共享路径，导致 3 张图片被跳过。

**修复**：`analyze_images_sqlite.py` 第 230-243 行
- 同时检测 `\\` 和 `//` 两种 SMB 前缀格式
- 优先 `\\`，失败后自动降级 `//`
- 日志输出使用 `//` 格式

**作用**：跨平台（Windows / Linux bash）都能正确访问 SMB 共享目录

---

### 2. 数据库唯一键从 `file_name` 改为 `file_path`

**问题**：`file_name` 有 UNIQUE 约束，但不同目录下可以有同名文件（如 `IMG_20210404_094222.jpg` 同时存在于 2021年碎片 和 2022碎片 目录）。这导致跨目录同名文件写入冲突，产生 13 对重复记录。

**修复**：
- 数据库表结构：`file_name` 去掉 UNIQUE，`file_path` 设为 `UNIQUE NOT NULL`
- SQL 语句：`INSERT OR REPLACE` 改为 `INSERT ... ON CONFLICT(file_path) DO UPDATE`
- 索引：`idx_image_analysis_file_name` → `idx_image_analysis_file_path`

**作用**：跨目录同名文件正常存储，不再因同名被覆盖或冲突

---

### 3. `INSERT OR REPLACE` 改为 `ON CONFLICT ... DO UPDATE`

**问题**：`INSERT OR REPLACE` 以 `file_name` 为去重键，同一 `file_name` 的多条记录（不同 `file_path`）会被错误覆盖。多次运行同一脚本会产生重复 `file_path` 记录（共发现 27 个重复，72 条多余记录）。

**修复**：`analyze_images_sqlite.py` 第 343-358 行
- 使用 `ON CONFLICT(file_path) DO UPDATE` 做 UPSERT
- 重复的 `file_path` 自动更新为最新分析结果

**作用**：幂等性——多次运行不会产生重复记录

---

### 4. PIL `resize()` 在 SMB 路径上崩溃

**问题**：`Image.open(fpath)` 打开 SMB 文件后调用 `img.resize()` 会报 `assert self.fp is not None`，因为 PIL 无法在内存中重新定位 SMB 文件。

**修复**：
- `analyze_image_with_claude` 中用 `io.BytesIO(image_data)` 包装图片数据
- 获取图片尺寸时也通过 `BytesIO` 打开，而非直接传路径

**作用**：避免 SMB 路径导致 PIL 崩溃，图片压缩稳定工作

---

### 5. API 调用增加重试机制

**问题**：原代码 API 调用无重试，网络中断或超时直接导致该图片分析失败，写入空结果。

**修复**：`analyze_image_with_claude` 第 192-214 行
- 最多重试 3 次，每次间隔 5 秒
- 检查返回结果是否为空，空结果也触发重试

**作用**：网络波动时自动恢复，减少空结果数量

---

### 6. 空结果检测与失败日志

**问题**：API 返回空文本时，脚本仍写入数据库，产生 7 条空内容记录。用户无法追踪哪些文件分析失败。

**修复**：
- 分析后检查 `overview` 和 `extracted_text` 是否为空
- 空结果跳过写入，计入 `error_count`
- 所有失败写入 `failed_analysis_log.txt`，含错误类型和文件路径

**作用**：空结果不污染数据库，失败文件可追溯和重新分析

---

### 7. HEIC 品牌码补充

**问题**：原代码 HEIC 检测只覆盖 `mif1, heic, hevx, heim, heis, hevs`，遗漏 `hif1, avif, av1f` 等格式，导致部分 HEIC/AVIF 图片无法正确转换。

**修复**：`analyze_images_sqlite.py` 第 110 行
- 补充 `b"hif1", b"avif", b"av1f"` 三种品牌码

**作用**：更多 HEIF/AVIF 格式图片可正确转换为 JPEG

---

### 8. 解析空文本保护

**问题**：`parse_claude_result` 接收空字符串时，正则匹配返回空列表，`matches[idx]` 会报 `IndexError`。

**修复**：`analyze_images_sqlite.py` 第 77-78 行
- 函数开头检查 `if not text or not text.strip()`，直接返回空结果

**作用**：防止空输入导致异常崩溃

---

### 9. 数据库连接复用

**问题**：原代码每处理一张图片就 `init_db()` 打开一次数据库，`conn.close()` 关闭一次，频繁 I/O 开销大。

**修复**：`analyze_images_sqlite.py` 第 300 行
- 循环开始前打开一次 `db_conn`
- 循环结束后统一 `db_conn.close()`

**作用**：减少数据库连接/断开开销，提升处理速度

---

### 10. API 限流保护

**问题**：连续快速调用 API 可能触发限流。

**修复**：`analyze_images_sqlite.py` 第 386-387 行
- 每张图片处理后 `time.sleep(2)`

**作用**：降低 API 限流风险

---

## 经验总结

### 跨平台 SMB 访问
- Windows Python 用 `\\` 前缀，Linux bash 用 `//` 前缀
- 代码应同时检测两种格式，自动降级
- 文件操作先读入 `BytesIO`，避免后续路径问题

### 数据库设计
- 唯一键应选**全局唯一**的字段（如 `file_path`），而非可能重复的（如 `file_name`）
- 用 `ON CONFLICT ... DO UPDATE` 替代 `INSERT OR REPLACE`，语义更清晰
- 循环中复用数据库连接，减少 I/O

### 容错处理
- API 调用必须有重试机制（网络/超时/限流）
- 分析结果必须做完整性检查（非空验证）
- 失败文件必须记录日志，可追溯可重试
- HEIC/AVIF 等格式品牌码要覆盖完整

### 性能优化
- 大图片压缩（`MAX_WIDTH = 1920`）
- 图片处理用 `BytesIO` 而非直接文件路径
- 数据库连接复用
- 合理设置 API 调用间隔
