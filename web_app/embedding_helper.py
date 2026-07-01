import base64
import os
import sqlite3
import time
import numpy as np

_model = None

# =============================================================================
# Model loading
# =============================================================================

def get_model():
    """Lazily load the sentence transformer model (BAAI/bge-base-zh-v1.5)."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            print("[Embedding] Error: sentence-transformers is not installed.")
            raise e

        base_dir = os.path.dirname(os.path.abspath(__file__))
        local_model_path = os.path.normpath(os.path.join(base_dir, '..', 'bge-base-zh-v1.5'))

        # Configure HuggingFace mirror for domestic users in China
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        if os.path.isdir(local_model_path) and os.path.exists(os.path.join(local_model_path, 'config.json')):
            print(f"[Embedding] Loading BGE model from: {local_model_path}")
            _model = SentenceTransformer(local_model_path)
        else:
            print(f"[Embedding] ERROR: BGE model not found at: {local_model_path}")
            print("[Embedding] Please run the model download script first.")
            raise FileNotFoundError(f"BGE model not found at {local_model_path}")
    return _model


def get_embedding(text):
    """Generate float32 numpy array embedding for the given text."""
    if not text or not text.strip():
        return None
    model = get_model()
    emb = model.encode(text.strip(), normalize_embeddings=True)
    return emb.astype(np.float32)


def save_image_vector(db_conn, image_id, overview_text):
    """Compute embedding for the image description and save to image_vectors table."""
    try:
        emb = get_embedding(overview_text)
        if emb is None:
            return False

        vector_bytes = emb.tobytes()

        db_conn.execute(
            "INSERT OR REPLACE INTO image_vectors (image_id, embedding) VALUES (?, ?)",
            (image_id, vector_bytes)
        )
        db_conn.commit()
        return True
    except Exception as e:
        print(f"[Embedding] Failed to save vector for image {image_id}: {e}")
        return False


# =============================================================================
# Vector cache — loaded once at startup, used for fast in-memory search
# =============================================================================

_vector_cache = {
    'mat': None,       # np.ndarray (N, dim) float32, L2-normalized
    'ids': None,       # np.ndarray (N,) int64
    'norms': None,     # np.ndarray (N,) float32
    'count': 0,
}


def get_vector_cache():
    """Return the in-memory vector cache dict."""
    return _vector_cache


def init_vector_cache(db_path=None):
    """Load all image vectors from SQLite into memory at startup."""
    if db_path is None:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'res.sqlite')

    start = time.perf_counter()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT image_id, embedding FROM image_vectors").fetchall()
    except Exception:
        rows = []
    conn.close()

    if not rows:
        _vector_cache['mat'] = None
        _vector_cache['ids'] = None
        _vector_cache['norms'] = None
        _vector_cache['count'] = 0
        print("[VectorCache] No vectors found, cache empty.")
        return 0

    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(mat, axis=1)

    _vector_cache['mat'] = mat
    _vector_cache['ids'] = ids
    _vector_cache['norms'] = norms
    _vector_cache['count'] = len(rows)

    elapsed = time.perf_counter() - start
    print(f"[VectorCache] Loaded {len(rows)} vectors ({mat.shape[1]}d) in {elapsed*1000:.0f}ms")
    return len(rows)


def rebuild_vector_cache(db_path=None):
    """Alias for init_vector_cache, used after new vectors are added."""
    return init_vector_cache(db_path)
