"""
Retrieval per similarità coseno tra embedding delle query e dei chunk.
Eseguito in Python (pgvector non disponibile su Windows nativo).
"""
from __future__ import annotations
import logging
from typing import Callable

import numpy as np

from .embedding import embed as default_embed, from_bytes, cosine_similarity
from .db import load_chunks

logger = logging.getLogger(__name__)


def retrieve(
    document_ids: list[str],
    query: str,
    top_k: int = 5,
    embed_fn: Callable | None = None,
) -> list[str]:
    """
    Ritorna i top_k contenuti di chunk più simili alla query.
    """
    _embed = embed_fn or default_embed
    if not document_ids:
        return []

    chunks = load_chunks(document_ids)
    if not chunks:
        return []

    query_vec = _embed(query)

    scored: list[tuple[float, str]] = []
    for _chunk_id, content, emb_bytes, _meta in chunks:
        if not emb_bytes:
            continue
        try:
            chunk_vec = from_bytes(emb_bytes)
            if chunk_vec.shape[0] != query_vec.shape[0]:
                continue
            score = cosine_similarity(query_vec, chunk_vec)
            scored.append((score, content))
        except Exception as exc:
            logger.debug("Errore deserializzazione embedding: %s", exc)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scored[:top_k]]
