"""FAISS vector store for chunk retrieval."""

import json
import logging
import os
import pickle
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import faiss
import numpy as np

from app.ingestion.chunker import Chunk

logger = logging.getLogger(__name__)


class VectorStore:
    """FAISS-based vector store for document chunks."""

    def __init__(self, dimension: int = 384):
        """Initialize the vector store.

        Args:
            dimension: Embedding vector dimension (384 for all-MiniLM-L6-v2)
        """
        self.dimension = dimension
        self.index = faiss.IndexFlatIP(dimension)  # Inner product (cosine sim with normalized vectors)
        self.chunks: list[Chunk] = []
        self.doc_metadata: dict[str, dict] = {}

    def add_chunks(self, chunks: list[Chunk], embeddings: np.ndarray) -> None:
        """Add chunks and their embeddings to the index.

        Args:
            chunks: List of Chunk objects
            embeddings: Embedding vectors (n_chunks, dimension)
        """
        if len(chunks) != embeddings.shape[0]:
            raise ValueError(
                f"Mismatch: {len(chunks)} chunks but {embeddings.shape[0]} embeddings"
            )

        # Normalize embeddings for cosine similarity
        faiss.normalize_L2(embeddings)

        self.index.add(embeddings)
        self.chunks.extend(chunks)

        logger.info("Added %d chunks to vector store (total: %d)", len(chunks), len(self.chunks))

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 20,
        doc_id_filter: Optional[set[str]] = None,
    ) -> list[tuple[Chunk, float]]:
        """Search for similar chunks.

        Args:
            query_embedding: Query vector (1, dimension)
            top_k: Number of results to return
            doc_id_filter: Optional set of allowed doc_ids (for access control)

        Returns:
            List of (chunk, score) tuples, sorted by relevance
        """
        if self.index.ntotal == 0:
            logger.warning("Vector store is empty!")
            return []

        # Normalize query (copy so the caller's array is not modified)
        query_embedding = np.array(query_embedding, dtype="float32", copy=True).reshape(1, -1)
        faiss.normalize_L2(query_embedding)

        # With an access filter, scan the full flat index so filtered-out
        # (restricted/ineligible) chunks cannot crowd out allowed ones.
        if doc_id_filter is not None:
            search_k = self.index.ntotal
        else:
            search_k = min(top_k, self.index.ntotal)
        scores, indices = self.index.search(query_embedding, search_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self.chunks):
                continue

            chunk = self.chunks[idx]

            # Apply doc_id filter (access control)
            if doc_id_filter is not None and chunk.doc_id not in doc_id_filter:
                continue

            results.append((chunk, float(score)))

            if len(results) >= top_k:
                break

        return results

    def save(self, directory: Path) -> None:
        """Save the vector store to disk.

        Args:
            directory: Directory to save index and metadata
        """
        directory.mkdir(parents=True, exist_ok=True)

        # Save FAISS index
        index_path = directory / "index.faiss"
        faiss.write_index(self.index, str(index_path))

        # Save chunks metadata
        chunks_data = []
        for chunk in self.chunks:
            chunks_data.append({
                "doc_id": chunk.doc_id,
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "locator": chunk.locator,
                "metadata": chunk.metadata,
            })

        chunks_path = directory / "chunks.json"
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump(chunks_data, f, ensure_ascii=False)

        # Save doc metadata
        meta_path = directory / "doc_metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(self.doc_metadata, f, ensure_ascii=False)

        logger.info("Vector store saved to %s", directory)

    @classmethod
    def load(cls, directory: Path) -> "VectorStore":
        """Load a vector store from disk.

        Args:
            directory: Directory containing saved index and metadata

        Returns:
            Loaded VectorStore instance
        """
        # Load FAISS index
        index_path = directory / "index.faiss"
        index = faiss.read_index(str(index_path))

        store = cls(dimension=index.d)
        store.index = index

        # Load chunks
        chunks_path = directory / "chunks.json"
        with open(chunks_path, "r", encoding="utf-8") as f:
            chunks_data = json.load(f)

        store.chunks = [
            Chunk(
                doc_id=c["doc_id"],
                chunk_id=c["chunk_id"],
                text=c["text"],
                token_count=c["token_count"],
                locator=c["locator"],
                metadata=c.get("metadata", {}),
            )
            for c in chunks_data
        ]

        # Load doc metadata
        meta_path = directory / "doc_metadata.json"
        with open(meta_path, "r", encoding="utf-8") as f:
            store.doc_metadata = json.load(f)

        logger.info(
            "Vector store loaded: %d chunks, %d documents",
            len(store.chunks),
            len(store.doc_metadata),
        )
        return store
