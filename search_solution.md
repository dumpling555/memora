# 语义化搜索（Semantic Search）设计方案

本方案旨在为 Memora 图像管理系统提供语义化搜索能力（例如输入“温暖的氛围”能匹配到阳光、木质家具、暖色调灯光的照片，即便文字中没有出现“温暖”二字）。

---

## 一、 当前搜索逻辑分析

### 1. 实现机制
目前 [web_app/app.py](file:///C:/Users/团子潘/Desktop/nf/web_app/app.py) 中的搜索功能基于标准的 SQL `LIKE` 模糊匹配。
```sql
AND (overview LIKE ? OR extracted_text LIKE ? OR other_info LIKE ? OR tags LIKE ? OR file_name LIKE ? OR file_path LIKE ?)
```
系统将用户的搜索词包装为 `%查询词%`，并在数据库的 `overview`、`extracted_text`、`other_info`、`tags`、`file_name`、`file_path` 六个字段中进行子串匹配。

### 2. 局限性
* **无语义关联**：仅支持精确的文本子串匹配。如果搜索“温暖的氛围”，只有当数据库中明确包含该词时才能被搜出；若照片描述为“温馨舒适的暖色灯光”，由于不含“温暖的氛围”字眼，将无法被召回。
* **语言同义词缺失**：无法识别同义词、近义词（如“猫”与“猫咪”、“小汽车”与“轿车”）。
* **无法理解意境与情绪**：无法识别抽象概念或情绪（如“孤独的背影”、“科技感”、“复古风”）。

---

## 二、 语义化搜索技术方案

为了解决这一局限性，我们设计了两种实现方案：**方案 A（向量检索，推荐）** 和 **方案 B（大模型查询扩展，轻量）**。

---

### 方案 A：基于向量嵌入（Vector Embeddings）的语义检索（推荐）

这是标准的语义搜索实现方式。通过将图片的描述和用户的查询词转化为空间高维向量，计算余弦相似度（Cosine Similarity）来找到最相关的照片。

#### 1. 架构设计
```
[图片分析阶段]
图片 -> VLM分析 -> 提取描述文本 (Overview) -> 向量化模型 (Embedding Model) -> 768维浮点向量 -> 存入数据库

[搜索阶段]
用户查询 "温暖的氛围" -> 向量化模型 (Embedding Model) -> 768维浮点向量 -> 向量相似度计算 (Vector Search) -> 返回Top K个结果
```

#### 2. 技术选型
* **向量化模型**：使用轻量级的中文双语 Embedding 模型，例如：
  * 本地部署：使用 Hugging Face 的 `shibing624/text2vec-base-chinese`（大小约 400MB，CPU 运行极快）。
  * 接口调用：使用 LM Studio 已加载的 Embedding 接口，或者使用 Ollama 的 `nomic-embed-text` 模型。
* **向量存储与计算**：
  * **轻量级实现**：在 SQLite 数据库中新建 `image_embeddings` 表，将向量以 `BLOB` (二进制) 或 `JSON` 格式存储，检索时在 Python 内存中用 Numpy 进行矩阵乘法计算相似度。
  * **高性能实现**：使用 SQLite 的向量扩展插件 **`sqlite-vec`**（新一代极简向量检索扩展，单文件即可运行）。

#### 3. 数据库表结构设计
```sql
CREATE TABLE IF NOT EXISTS image_vectors (
    image_id INTEGER PRIMARY KEY,
    embedding BLOB NOT NULL, -- 768维浮点数构成的二进制向量
    FOREIGN KEY(image_id) REFERENCES image_analysis(id) ON DELETE CASCADE
);
```

#### 4. 后台代码实现框架（Python 伪代码）

##### 步骤一：图片入库时计算并保存向量
```python
import numpy as np
from sentence_transformers import SentenceTransformer

# 初始化轻量中文向量模型
model = SentenceTransformer('shibing624/text2vec-base-chinese')

def save_image_vector(db_conn, image_id, overview_text):
    # 将图片的描述文本转化为向量
    vector = model.encode(overview_text)
    # 转化为 float32 的二进制 bytes 存入 SQLite
    vector_bytes = vector.astype(np.float32).tobytes()
    
    db_conn.execute(
        "INSERT OR REPLACE INTO image_vectors (image_id, embedding) VALUES (?, ?)",
        (image_id, vector_bytes)
    )
    db_conn.commit()
```

##### 步骤二：检索时进行相似度计算
```python
def search_semantic(db_conn, query_text, limit=20):
    # 1. 计算查询词向量
    query_vector = model.encode(query_text).astype(np.float32)
    
    # 2. 读取库中所有的向量
    rows = db_conn.execute("SELECT image_id, embedding FROM image_vectors").fetchall()
    
    results = []
    for image_id, emb_bytes in rows:
        emb_vector = np.frombuffer(emb_bytes, dtype=np.float32)
        # 计算余弦相似度
        similarity = np.dot(query_vector, emb_vector) / (np.linalg.norm(query_vector) * np.linalg.norm(emb_vector))
        results.append((image_id, float(similarity)))
    
    # 3. 按相似度倒序排序，取前 Top N
    results.sort(key=lambda x: x[1], reverse=True)
    top_results = results[:limit]
    
    # 4. 从 image_analysis 中查出图片详情
    ids = [r[0] for r in top_results]
    # 根据这些 ID 拼出 SQL 返回结果
    return ids
```

---

### 方案 B：基于大模型查询扩展（LLM Query Expansion）的混合检索

如果不想在系统中引入额外的深度学习框架（如 PyTorch / numpy / transformers），可以利用现有的本地大模型（LM Studio / Ollama）在搜索时做关键词扩展。

#### 1. 架构设计
```
[搜索词] "温暖的氛围" 
     │
     ▼ (输入给本地大语言模型)
[Prompt]: "请将搜索词'温暖的氛围'扩展为5-10个用于照片检索的中文关键词，包含颜色、物品、场景等。仅输出关键词，用逗号隔开。"
     │
     ▼ (LLM 输出扩展词)
"阳光, 暖色调, 壁炉, 橙色, 咖啡, 灯光, 温馨, 舒适"
     │
     ▼ (在原有 SQL 中进行多关键词模糊匹配)
SELECT * FROM image_analysis WHERE 
   overview LIKE '%阳光%' OR overview LIKE '%暖色调%' OR overview LIKE '%温馨%' ...
```

#### 2. 实现成本
* **优点**：**完全无需引入任何新库**，直接使用目前项目中的 `get_llm_config()` 获取大模型接口，改动仅限 API 逻辑层。
* **缺点**：每次搜索时需要调用大模型接口进行词汇扩展，搜索会有 1~2 秒的接口延迟，用户体验较方案 A 稍慢。

---

## 三、 推荐落地实施路线

建议采用 **方案 A 的轻量化 Numpy 实现**，具体落地步骤如下：

1. **安装轻量级依赖**：
   ```bash
   pip install sentence-transformers numpy
   ```
2. **在 `analysis_daemon.py` 中集成向量生成**：
   在后台线程生成 `overview` 后，顺便调用 `model.encode(overview)` 生成向量，并将其保存到 `image_vectors` 表中。
3. **在后端增加 `/api/search/semantic` 接口**：
   当用户在前端发起搜索时，优先使用语义搜索接口，若语义搜索未初始化，再优雅降级到原有的 `LIKE` 模糊匹配。
4. **前端无感升级**：
   搜索框 UI 保持一致，后台自动融合关键词搜索（匹配文件名）与语义搜索（匹配画面意境），返回最符合用户心智的照片。
