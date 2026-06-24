# Memora — 智能照片档案管理系统

**English:** Intelligent Photo Archive Manager &nbsp;|&nbsp; **日本語:** スマート写真アーカイブ管理システム

基于 Flask + SQLite 的轻量级 NAS 照片管理平台。自动扫描、AI 描述与标签、缩略图生成、网页图库浏览。

Lightweight NAS photo management platform on Flask + SQLite. Auto-scan, AI description & tags, thumbnail generation, web gallery browsing.

軽量な NAS 写真管理プラットフォーム（Flask + SQLite）。自動スキャン、AI 説明文・タグ生成、サムネイル作成、Web ギャラリー閲覧。

---

## 功能 / Features / 機能

| 中文 | English | 日本語 |
|---|---|---|
| 统一登录（session 认证） | Unified login (session auth) | 統一ログイン（セッション認証） |
| AI 自动分析与标签生成 (LM Studio) | AI auto-analysis & tags (LM Studio) | AI 自動分析・タグ生成 (LM Studio) |
| 后台分析守护进程 | Background analysis daemon | バックグラウンド分析デーモン |
| 多源媒体库管理 | Multi-source media management | 複数メディアソース管理 |
| 文件夹可见性控制 | Folder visibility control | フォルダ表示制御 |
| 时间线浏览 | Timeline browsing | タイムライン表示 |
| 无限滚动加载 | Infinite scroll | 無限スクロール |
| 后台自动扫描 | Auto background scanning | 自動スキャン |
| 缩略图后台生成 | Background thumbnail generation | サムネイル自動生成 |
| 明暗主题切换 | Dark/light theme toggle | ダーク/ライト切替 |
| 中/英/日多语言 | Chinese/English/Japanese i18n | 中国語/英語/日本語対応 |

## 技术栈 / Tech Stack / 技術スタック

| Layer | 技术 | Technology | 技術 |
|---|---|---|---|
| 后端 | Flask (Python) | Flask (Python) | Flask (Python) |
| 数据库 | SQLite (`res.sqlite`) | SQLite (`res.sqlite`) | SQLite (`res.sqlite`) |
| AI 后端 | LM Studio (本地 LLM) | LM Studio (local LLM) | LM Studio (ローカル LLM) |
| 前端 | Tailwind CSS + RemixIcon | Tailwind CSS + RemixIcon | Tailwind CSS + RemixIcon |
| 媒体源 | NAS / SMB 本地映射路径 | NAS / SMB mapped path | NAS / SMB マッピングパス |

## 环境要求 / Prerequisites / 前提条件

- Python 3.7+
- NAS 存储映射为本地盘符（如 `Z:\`）
- （可选）LM Studio 本地运行以启用 AI 分析

## 安装与运行 / Installation / インストール

```bash
pip install -r requirements.txt
python web_app/app.py
```

打开浏览器访问 **http://127.0.0.1:5000**

首次使用：
1. 用 `admin` / `admin@123` 登录（强制修改密码）
2. 进入 `/admin` → 媒体源 → 添加你的 NAS 照片目录

## 数据库 / Database / データベース

首次启动自动创建 `res.sqlite`：

| 表 / Table | 用途 / Purpose |
|---|---|
| `image_analysis` | 照片元数据（路径、描述、标签、OCR文本、尺寸等） |
| `media_sources` | NAS 媒体源路径（启用/停用） |
| `folder_visibility` | 文件夹可见性设置 |
| `scan_log` | 扫描历史（状态、文件数、时间戳） |
| `scan_schedules` | 定时扫描配置 |
| `admin_settings` | 键值存储（凭据、调度开关、secret key） |
| `thumb_progress` | 缩略图生成进度 |

## 项目结构 / Project Structure / プロジェクト構成

```
.
├── web_app/
│   ├── app.py              # Flask 入口 — 认证、路由、调度初始化
│   ├── admin_routes.py     # 管理后台 (/admin)
│   ├── analysis_daemon.py  # AI 分析守护进程
│   ├── scanner.py          # 照片目录扫描器
│   ├── quick_scanner.py    # 快速增量扫描
│   ├── scheduler.py        # 定时调度
│   ├── thumb_daemon.py     # 缩略图后台生成
│   ├── thumb_generator.py  # 缩略图批量生成
│   ├── thumb_common.py     # 缩略图公共工具
│   ├── templates/          # HTML 模板
│   └── static/             # 静态资源
├── res.sqlite              # 运行时数据库
├── requirements.txt
└── README.md
```

## 关键设计 / Key Concepts / 設計のポイント

- **Active Sources**: 从 `media_sources` 表动态读取源路径，无硬编码
- **路径分隔符**: Windows 反斜杠 `\`，统一用 `os.path.normpath()` 处理
- **后台线程**: Scanner / QuickScanner / Scheduler / AnalysisDaemon / ThumbnailDaemon 均以 daemon 线程运行
- **扫描模式**: `incremental`（仅新增） / `full`（新增 + 更新 + 删除）
- **数据库连接**: 统一使用 `get_db_connection()`（设置 `row_factory` 和 `PRAGMA journal_mode=WAL`）

## 注意事项 / Notes / 注意事項

- `res.sqlite` 是生产数据库，勿提交或删除
- `web_app/cache/`（缩略图）为运行时生成，勿提交
- NAS 路径必须本地可访问
- AI 分析需要 LM Studio API 本地运行
- `send_file()` 用于照片/缩略图流式传输，生产环境建议增加目录遍历防护

## 许可证 / License / ライセンス

MIT
