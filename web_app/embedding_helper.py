import os
import sqlite3
import numpy as np

_model = None

def get_model():
    """Lazily load the sentence transformer model and download if not exists."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            print("[Embedding] Error: sentence-transformers is not installed.")
            raise e

        base_dir = os.path.dirname(os.path.abspath(__file__))
        local_model_path = os.path.normpath(os.path.join(base_dir, '..', 'text2vec-base-chinese'))
        
        # We check if local model path exists and has files (e.g. config.json)
        if os.path.isdir(local_model_path) and os.path.exists(os.path.join(local_model_path, 'config.json')):
            print(f"[Embedding] Loading model from local path: {local_model_path}")
            _model = SentenceTransformer(local_model_path)
        else:
            print(f"[Embedding] Local model not found at {local_model_path}. Downloading shibing624/text2vec-base-chinese...")
            _model = SentenceTransformer('shibing624/text2vec-base-chinese')
            try:
                os.makedirs(local_model_path, exist_ok=True)
                _model.save(local_model_path)
                print(f"[Embedding] Model saved locally to: {local_model_path}")
            except Exception as save_err:
                print(f"[Embedding] Failed to save model locally: {save_err}")
    return _model

def get_embedding(text):
    """Generate float32 numpy array embedding for the given text."""
    if not text or not text.strip():
        return None
    model = get_model()
    # Ensure text is processed as clean string
    emb = model.encode(text.strip())
    return emb.astype(np.float32)

def save_image_vector(db_conn, image_id, overview_text):
    """Compute embedding for the image description and save to image_vectors table."""
    try:
        emb = get_embedding(overview_text)
        if emb is None:
            return False
        
        # Convert float32 numpy array to raw bytes
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
