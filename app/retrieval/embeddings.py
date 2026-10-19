"""Embedding model wrapper using sentence-transformers (local, CPU)."""

import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Lazy-loaded singleton
_model = None
_model_name = None
_lock = threading.Lock()


def _resolve_name(model_name: Optional[str]) -> str:
    if model_name:
        return model_name
    from app.config import get_settings

    return get_settings().embedding_model


def get_embedding_model(model_name: Optional[str] = None):
    """Get or create the embedding model singleton.

    Uses sentence-transformers for local CPU-based embeddings. The model name
    defaults to the EMBEDDING_MODEL setting.
    """
    global _model, _model_name
    name = _resolve_name(model_name)

    with _lock:
        if _model is not None and _model_name == name:
            return _model

        logger.info("Loading embedding model: %s ...", name)

        from sentence_transformers import SentenceTransformer

        try:
            # Use the cached copy without contacting the Hub (Docker bakes the
            # model in at build time; local runs reuse HF_HOME).
            _model = SentenceTransformer(name, device="cpu", local_files_only=True)
        except Exception:
            logger.info("Embedding model not cached locally; downloading %s", name)
            _model = SentenceTransformer(name, device="cpu")
        _model_name = name

        dim_fn = getattr(_model, "get_embedding_dimension", None) or getattr(
            _model, "get_sentence_embedding_dimension", lambda: 384
        )
        logger.info("Embedding model loaded: %s (dim=%d)", name, dim_fn())

    return _model


def embed_texts(texts: list[str], model_name: Optional[str] = None) -> np.ndarray:
    """Embed a list of texts and return normalized vectors.

    Args:
        texts: List of text strings to embed
        model_name: Name of the sentence-transformers model

    Returns:
        numpy array of shape (n_texts, embedding_dim)
    """
    model = get_embedding_model(model_name)
    embeddings = model.encode(
        texts,
        show_progress_bar=False,
        normalize_embeddings=True,  # Normalize for cosine similarity via dot product
        batch_size=32,
    )
    return np.array(embeddings, dtype=np.float32)


def embed_query(query: str, model_name: Optional[str] = None) -> np.ndarray:
    """Embed a single query string.

    Args:
        query: Query text
        model_name: Name of the sentence-transformers model

    Returns:
        numpy array of shape (1, embedding_dim)
    """
    return embed_texts([query], model_name)
