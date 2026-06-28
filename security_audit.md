# 🔒 Memora 项目安全与逻辑审计报告

> **审计时间**: 2026-06-28  
> **审计范围**: 全部后端 Python 源码 + 全部前端 HTML/JS 源码  
> **审计方式**: 逐行静态代码分析  

---

## 📊 总览

| 严重程度 | 后端 | 前端 | 合计（去重后） |
|---------|------|------|--------------|
| 🔴 严重 (Critical) | 4 | 5 | **7** |
| 🟠 高危 (High) | 5 | 3 | **6** |
| 🟡 中等 (Medium) | 10 | 5 | **14** |
| 🟢 低危 (Low) | 9 | 5 | **12** |
| **合计** | **28** | **18** | **39** |

> [!NOTE]
> 好消息：**未发现 SQL 注入漏洞**——所有数据库查询均正确使用了参数化语句。也**未发现不安全的反序列化**。

---

## 🔴 严重 (Critical)

---

### C-1: 全局无 CSRF 防护

| 属性 | 值 |
|------|-----|
| **分类** | CSRF（跨站请求伪造） |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py)、[admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py)、[admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) |
| **影响** | 攻击者可通过恶意网页伪造请求，执行扫描、删除媒体源、更改密码等所有管理操作 |

**描述**: 应用程序没有任何 CSRF 保护机制。没有 `flask-wtf` CSRFProtect，没有 CSRF Token，没有设置 `SameSite` Cookie 属性。所有 POST/PUT/DELETE 端点（包括 `/api/login`、`/api/change-password`、所有 `/admin/api/*`）都可被跨站伪造。

前端所有 `fetch()` 调用均未携带任何 CSRF Token：

```js
// admin.html — 无 CSRF Token
resp = await fetch('/admin/api/sources', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, root_path, is_active: 1})
});
```

**建议修复**:
```python
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
```
前端所有请求需携带 `X-CSRF-Token` Header。

---

### C-2: 默认硬编码管理员凭据，且未强制修改

| 属性 | 值 |
|------|-----|
| **分类** | 认证 / 默认凭据 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L121-127 |
| **影响** | 使用默认密码 `admin@123` 即可接管系统 |

**描述**: 系统初始化时使用硬编码的 `admin` / `admin@123` 作为默认凭据。虽然设置了 `must_change_password` 标志，但这仅是建议性的——API 返回 `need_change: true` 后由客户端提示，用户可直接忽略并继续使用默认密码。

```python
pw_hash = generate_password_hash('admin@123')
c.execute("INSERT INTO admin_settings (key, value) VALUES ('admin_username', 'admin')")
```

**建议修复**: 在 `require_auth` 装饰器中，若 `must_change_password == '1'`，则拒绝除 `/api/change-password` 以外的所有请求，强制用户修改密码。

---

### C-3: 登录接口无速率限制——可暴力破解

| 属性 | 值 |
|------|-----|
| **分类** | 认证 / 暴力破解 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L244-269 |
| **影响** | 攻击者可无限次尝试登录，结合弱默认密码尤为危险 |

**描述**: `/api/login` 端点没有速率限制、账户锁定或延迟机制。

**建议修复**: 使用 `flask-limiter` 实现速率限制，或在连续失败 N 次后锁定账户一段时间。

---

### C-4: 路径探测接口可枚举任意服务器目录

| 属性 | 值 |
|------|-----|
| **分类** | 路径遍历 / 信息泄露 |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L182-218 |
| **影响** | 已认证攻击者可探测服务器上任意文件系统路径 |

**描述**: `GET /admin/api/sources/test-connectivity?path=...` 接受任意用户输入的路径，使用 `os.path.exists()`、`os.path.isdir()` 和 `os.scandir()` 进行探测。攻击者可枚举 `C:\Windows\System32`、`/etc/` 等敏感目录，错误消息还会泄露 SMB 错误详情。

```python
root_path = request.args.get('path', '').strip()
# 无任何验证——接受任意路径
if not os.path.exists(root_path):
    return jsonify({'ok': False, 'message': 'Path does not exist'})
for entry in os.scandir(root_path):  # 枚举任意目录
```

**建议修复**: 白名单限制允许的基础目录，或至少屏蔽系统路径。

---

### C-5: 前端多处 XSS（跨站脚本攻击）漏洞

| 属性 | 值 |
|------|-----|
| **分类** | XSS（DOM 型 / 存储型） |
| **涉及文件** | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L332, 423, 476, 569, 659；[index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L372, 376 |
| **影响** | 如果媒体源名称、标签或文件名包含恶意 HTML，将在浏览器中执行任意 JavaScript |

**描述**: 多处使用 `innerHTML` 直接插入用户可控数据，且未转义。

**漏洞位置一览**:

| 文件 | 行号 | 注入来源 |
|------|------|---------|
| admin.html | 332 | Toast 消息中的 `message` 参数 |
| admin.html | 423 | 错误消息 `e.message` |
| admin.html | 476 | 连接测试错误 `e.message` |
| admin.html | 569, 571 | 数据源名称 `s.name` |
| admin.html | 659 | 文件夹加载错误 `e.message` |
| index.html | 372 | 照片标签 `photo.tags`（**存储型 XSS**） |
| index.html | 376 | 文件名 `photo.file_name` |

**示例** — Toast 消息注入:
```js
// admin.html:332 — message 直接进入 innerHTML
el.innerHTML = `<i class="${icons[type] || icons.info}"></i> ${message}`;
```

**示例** — 存储型 XSS（照片标签）:
```js
// index.html:372 — tags 来自数据库，无任何转义
${photo.tags ? photo.tags.split(',').map(t =>
    `<span class="tag-chip">${t.trim()}</span>`).join('') : ''}
```

> [!CAUTION]
> `index.html` 中**完全没有** `esc()` 转义函数！

**建议修复**:
```js
// 为 Toast 消息使用安全方式
el.innerHTML = `<i class="${icons[type] || icons.info}"></i> <span></span>`;
el.querySelector('span').textContent = message;

// 为 index.html 添加 esc() 函数并应用到所有用户 data
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
```

---

### C-6: `get_json(force=True)` 削弱 CSRF 防御

| 属性 | 值 |
|------|-----|
| **分类** | 输入验证 / CSRF |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L53, 91, 297, 558, 602, 641 |
| **影响** | 绕过浏览器 CORS 预检，使 CSRF 攻击更容易实施 |

**描述**: 使用 `force=True` 会无视 `Content-Type` Header，直接将请求体解析为 JSON。正常情况下浏览器发送 `application/json` 会触发 CORS 预检，而使用 `force=True` 后，表单提交的请求也能被解析。

**建议修复**: 移除 `force=True`，要求正确的 `Content-Type: application/json` Header。

---

### C-7: Secret Key 以明文存储在数据库中

| 属性 | 值 |
|------|-----|
| **分类** | 信息泄露 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L26-40 |
| **影响** | 拥有数据库文件读取权限的人可伪造 Session Cookie |

**描述**: Flask 的 `secret_key`（用于签名 Session Cookie）以明文形式存储在 `admin_settings` 表中。

**建议修复**: 将 Secret Key 存储在环境变量或受保护的配置文件中。

---

## 🟠 高危 (High)

---

### H-1: Logout 使用 GET 请求——可被 CSRF 强制登出

| 属性 | 值 |
|------|-----|
| **分类** | CSRF / 会话管理 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L310-313 |
| **影响** | 任何网页均可通过 `<img src="/api/logout">` 强制用户登出 |

```python
@app.route('/api/logout')  # GET 方法！
def api_logout():
    session.pop('logged_in', None)
    return jsonify({'ok': True})
```

**建议修复**: 改为 `methods=['POST']` 并验证 CSRF Token。

---

### H-2: Session Cookie 缺少安全属性

| 属性 | 值 |
|------|-----|
| **分类** | 会话管理 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) |
| **影响** | Session Cookie 可被截获或滥用 |

**缺失配置**:
- `SESSION_COOKIE_SECURE` — Cookie 以明文通过 HTTP 传输
- `SESSION_COOKIE_SAMESITE` — 容易被 CSRF 利用
- `PERMANENT_SESSION_LIFETIME` — `session.permanent = True` 使用 Flask 默认 31 天，Session 几乎不会过期

**建议修复**:
```python
app.config['SESSION_COOKIE_SECURE'] = True       # 仅 HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
```

---

### H-3: 媒体源接受任意文件系统路径——无验证

| 属性 | 值 |
|------|-----|
| **分类** | 路径遍历 |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L51-86 |
| **影响** | 管理员可将媒体源指向任意服务器目录，配合文件服务接口实现任意文件读取 |

```python
root_path = (data.get('root_path') or '').strip()
# 无路径验证或沙箱隔离
cursor.execute("INSERT INTO media_sources (...) VALUES (?, ?, ?)",
               (name, root_path, is_active))
```

**建议修复**: 验证 `root_path` 必须位于预定义的允许目录列表中。

---

### H-4: OSError 消息直接返回给客户端——泄露内部信息

| 属性 | 值 |
|------|-----|
| **分类** | 信息泄露 |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L171-172, 211-212 |
| **影响** | 泄露服务器内部文件系统结构、SMB 共享名、主机名等 |

```python
except OSError as e:
    return jsonify({'ok': False, 'message': f'SMB / filesystem error: {e}'})
```

**建议修复**: 在服务端记录完整错误日志，返回给客户端的只用通用错误消息。

---

### H-5: 认证路径豁免可能被滥用

| 属性 | 值 |
|------|-----|
| **分类** | 认证绕过风险 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L220-236 |
| **影响** | `/static/` 前缀豁免可能被路径遍历利用 |

```python
if request.path in ('/login', '/api/login', '/api/logout', '/api/change-password') \
   or request.path.startswith('/static/'):
    return None  # 跳过认证
```

**建议修复**: 确保 Flask 的静态文件服务被严格约束，不允许路径遍历。

---

### H-6: 前端 API 数据未验证类型安全

| 属性 | 值 |
|------|-----|
| **分类** | XSS / 注入 |
| **涉及文件** | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L397, 411-414 |
| **影响** | 若 API 返回非预期类型的 `s.id`，可突破 HTML 上下文 |

```js
// s.id 直接插入 onclick 处理器
<button onclick="triggerScan(${s.id}, 'incremental')" ...>
```

**建议修复**: 使用 `parseInt(s.id)` 验证。

---

## 🟡 中等 (Medium)

---

### M-1: 全局 `THUMB_MAP` 字典的线程安全问题

| 属性 | 值 |
|------|-----|
| **分类** | 竞态条件 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L23, 318, 350 |

**描述**: `THUMB_MAP` 是普通 Python `dict`，被多个线程并发访问。`_build_thumb_map_wrapper` 先执行 `THUMB_MAP.clear()` 再逐步填充，在此期间缩略图请求会收到 404。

**建议修复**: 在本地变量中构建新 map，然后原子性替换：
```python
new_map = {}
# ... 填充 new_map ...
THUMB_MAP.clear()
THUMB_MAP.update(new_map)
```

---

### M-2: 多处数据库连接泄漏

| 属性 | 值 |
|------|-----|
| **分类** | 资源泄漏 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L326-333；[admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L330-337 |

**描述**: 若在 `conn = sqlite3.connect()` 之后、`conn.close()` 之前抛出异常，连接将泄漏。宽泛的 `except Exception: pass` 无声吞噬错误。

**建议修复**: 使用 `try/finally` 或上下文管理器确保 `conn.close()` 始终被调用。

---

### M-3: `_walk_parallel` 潜在死锁风险

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 / 死锁 |
| **涉及文件** | [scanner.py](file:///C:/Users/团子潘/Desktop/nf/web_app/scanner.py) L38-73 |

**描述**: `while len(visited) < len(pending)` 循环等待队列项。若线程池任务引发意外错误（如 `MemoryError`），`q.put()` 不会执行，导致 `len(visited)` 永远无法追上 `len(pending)`，在 `q.get()` 上无限阻塞。

**建议修复**: 使用 `q.get(timeout=300)` 并添加超时中止逻辑。

---

### M-4: 重复扫描检查与扫描启动之间的 TOCTOU 竞态

| 属性 | 值 |
|------|-----|
| **分类** | 竞态条件 / TOCTOU |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L419-427 |

```python
with _scan_lock:
    if source_id in _active_scans:
        return jsonify({...}), 409
# 锁在此处释放——另一个请求可以在这里插入
log_id = _start_scan(source_id, mode)  # 内部再次获取锁，但不检查重复
```

**建议修复**: 将检查和启动合并到同一个临界区中。

---

### M-5: Full Scan 加载全部记录到内存（未按源过滤）

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 / 性能 |
| **涉及文件** | [scanner.py](file:///C:/Users/团子潘/Desktop/nf/web_app/scanner.py) L113 |

**描述**: Full 模式下 `SELECT id, file_path, file_size, created_at FROM image_analysis` 加载所有源的全部记录。增量模式正确使用了 `WHERE file_path LIKE root + '%'`。

**建议修复**: Full 模式同样添加 `WHERE file_path LIKE ?` 过滤。

---

### M-6: `_scan_wrapper` 中创建了新的 ThumbnailDaemon 而非使用单例

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 |
| **涉及文件** | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L386-388 |

**描述**: 创建了新的 `ThumbnailDaemon()` 实例而非使用 `app.config['THUMB_DAEMON']` 中存储的单例。新实例的 `_retries` 和 `_abandoned` 为空，会重试已放弃的缩略图。

**建议修复**: 使用 `current_app.config.get('THUMB_DAEMON')`。

---

### M-7: `send_file` 可读取任意已配置路径下的文件

| 属性 | 值 |
|------|-----|
| **分类** | 任意文件读取 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L593-640 |

**描述**: `serve_file` 端点从数据库读取 `file_path` 并通过 `send_file` 提供。虽然检查了 `active_roots`，但根路径本身是用户可配置的（见 H-3）。

---

### M-8: 分页 Cursor 参数未做整数校验

| 属性 | 值 |
|------|-----|
| **分类** | 输入验证 / 拒绝服务 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L462-468 |

```python
after_id = int(parts[1])  # 若非有效整数则崩溃，返回 500
```

**建议修复**: 包裹 `try/except ValueError`。

---

### M-9: `rstrip('.jpg')` 行为非预期

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 |
| **涉及文件** | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L342 |

**描述**: `str.rstrip()` 移除的是**字符集**而非后缀。`rstrip('.jpg')` 会移除所有尾部的 `.`、`j`、`p`、`g` 字符。虽然 SHA256 十六进制中不含 `j`、`p`、`g`，碰巧结果正确，但这是代码异味。

**建议修复**: 使用 `h = h[:-4]` 或 `h.removesuffix('.jpg')`（Python 3.9+）。

---

### M-10: 搜索输入未做防抖——每次按键触发双倍 API 请求

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 / 性能 |
| **涉及文件** | [index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L584-587 |

**描述**: 每次键入都立刻触发 `fetchTimeline()` + `fetchPhotos()`。输入 "hello" 会发送 10 次 API 请求。

**建议修复**: 添加 300ms 防抖。

---

### M-11: BroadcastChannel 消息触发重复 `fetchPhotos()` 调用

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 |
| **涉及文件** | [index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L656-657 |

```js
bc.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'folder-visibility-changed') {
        fetchPhotos();
        fetchPhotos();  // ← 重复调用
    }
});
```

**建议修复**: 移除重复的 `fetchPhotos()` 调用。

---

### M-12: 文件夹可见性级联逻辑 Bug

| 属性 | 值 |
|------|-----|
| **分类** | 逻辑漏洞 |
| **涉及文件** | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L733-735 |

**描述**: `row.parentElement.querySelectorAll('.fv-row')` 选择了父元素 `<li>` 的所有后代行（包括当前行自身），导致同一行被更新两次。

---

### M-13: CDN 资源未使用 SRI（子资源完整性）

| 属性 | 值 |
|------|-----|
| **分类** | 供应链安全 |
| **涉及文件** | [index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L7-9, 290 |

**描述**: Tailwind CSS、Swiper、Remixicon 从 CDN 加载但未添加 `integrity` 和 `crossorigin` 属性。CDN 被入侵时可注入恶意代码。`admin.html` 使用本地资源但 `index.html` 使用 CDN，存在不一致。

---

### M-14: 多个 `fetch()` 调用未检查响应状态

| 属性 | 值 |
|------|-----|
| **分类** | 错误处理 |
| **涉及文件** | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L388-389；[index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L330-331 |

**描述**: 多处 `fetch()` 调用直接调用 `.json()` 而未检查 `resp.ok`。401/500 响应会导致隐晦错误。

---

## 🟢 低危 (Low)

---

### L-1: `THUMB_DIR` 导入后被重新定义（变量遮蔽）

| 涉及文件 | [app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) L10, 22 |
|---------|-----|

L10 从 `thumb_common` 导入 `THUMB_DIR`，L22 重新定义为本地变量，导入成为死代码。

---

### L-2: 全局异常吞噬（`except Exception: pass`）

| 涉及文件 | 所有文件（多处） |
|---------|-----|

大量 `except Exception: pass` 无声吞噬错误，使调试几乎不可能。**建议**: 至少记录异常日志再抑制。

---

### L-3: `ImageFile.LOAD_TRUNCATED_IMAGES = True` 全局设置

| 涉及文件 | [thumb_common.py](file:///C:/Users/团子潘/Desktop/nf/web_app/thumb_common.py) L10 |
|---------|-----|

允许 PIL 处理可能恶意的截断图片文件，增加攻击面。

---

### L-4: 符号链接跟随未一致阻止

| 涉及文件 | [scanner.py](file:///C:/Users/团子潘/Desktop/nf/web_app/scanner.py) L51-53 |
|---------|-----|

虽然 `is_dir()` / `is_file()` 使用了 `follow_symlinks=False`，但后续的 `os.stat`、`Image.open` 仍会跟随符号链接。

---

### L-5: 确认弹窗在异步回调完成前关闭

| 涉及文件 | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L908-911 |
|---------|-----|

```js
document.getElementById('confirm-btn').addEventListener('click', () => {
    if (_confirmCallback) _confirmCallback();  // 异步操作
    closeConfirmModal();  // 立即关闭，不等待完成
});
```

---

### L-6: 数据库连接管理未使用连接池

| 涉及文件 | 所有 Python 文件 |
|---------|-----|

每次请求和后台操作都创建新的 `sqlite3.connect()` 调用，未使用 Flask 的 `g` 对象进行请求级连接管理。

---

### L-7: 时间戳使用本地时区而非 UTC

| 涉及文件 | [scanner.py](file:///C:/Users/团子潘/Desktop/nf/web_app/scanner.py) L354 |
|---------|-----|

使用 `time.localtime()` 格式化时间，时区变更会导致不必要的文件重新处理。

---

### L-8: 外部占位图泄露隐私

| 涉及文件 | [index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L375 |
|---------|-----|

缩略图加载失败时使用 `via.placeholder.com` 外部服务。在内网/离线环境中不可用且泄露请求信息。

---

### L-9: Admin 链接始终对所有用户可见

| 涉及文件 | [index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L216-218 |
|---------|-----|

无论用户是否认证，Admin 链接始终渲染在页头中，暴露管理路径。

---

### L-10: 调度间隔 `interval_minutes` 无输入验证

| 涉及文件 | [admin_routes.py](file:///C:/Users/团子潘/Desktop/nf/web_app/admin_routes.py) L557-597 |
|---------|-----|

0 或负值可能导致无限循环或除零错误。**建议**: 验证为 1-10080 范围内的正整数。

---

### L-11: `localStorage` 语言/主题值无边界验证

| 涉及文件 | [i18n.js](file:///C:/Users/团子潘/Desktop/nf/web_app/static/js/i18n.js) L287-289；[theme.js](file:///C:/Users/团子潘/Desktop/nf/web_app/static/js/theme.js) L31 |
|---------|-----|

虽然现有验证基本足够，但可增强对非预期值的防御。

---

### L-12: admin.html 与 index.html 的 CDN 与本地加载不一致

| 涉及文件 | [admin.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/admin.html) L7；[index.html](file:///C:/Users/团子潘/Desktop/nf/web_app/templates/index.html) L7 |
|---------|-----|

`admin.html` 从 `/static/` 加载 Tailwind，`index.html` 从 CDN 加载。**建议**: 统一使用本地资源。

---

## 📋 修复优先级建议

> [!IMPORTANT]
> 若计划对外开放使用，以下问题**必须在上线前修复**：

### 第一优先级（上线前必修）
1. **C-1** CSRF 防护（安装 `flask-wtf`）
2. **C-2** 强制修改默认密码
3. **C-3** 登录速率限制
4. **C-5** 修复所有 XSS 漏洞
5. **H-1** Logout 改为 POST
6. **H-2** Session Cookie 安全属性

### 第二优先级（尽快修复）
7. **C-4** 限制路径探测范围
8. **H-3** 媒体源路径验证
9. **H-4** 错误消息脱敏
10. **M-4** 修复 TOCTOU 竞态
11. **M-6** 使用 ThumbnailDaemon 单例

### 第三优先级（质量改进）
12. **M-1** THUMB_MAP 原子替换
13. **M-2** 数据库连接泄漏修复
14. **M-5** Full Scan 按源过滤
15. **M-10** 搜索防抖
16. **M-11** 移除重复 fetchPhotos
17. **L-2** 异常日志记录
