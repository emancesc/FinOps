"""
Embedding deterministico basato su hash SHA-256 + random projection.
Nessun modello ML richiesto; sostituire con sentence-transformers in produzione.
"""
from __future__ import annotations
import hashlib
import numpy as np

EMBEDDING_DIM = 256


def embed(text: str) -> np.ndarray:
    """
    Genera un vettore float32 normalizzato di dimensione EMBEDDING_DIM.
    Deterministico: lo stesso testo produce sempre lo stesso vettore.
    """
    seed = int(hashlib.sha256(text.lower().encode()).hexdigest(), 16) % (2**31)
    rng = np.random.RandomState(seed)
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity tra due vettori normalizzati."""
    return float(np.dot(a, b))


def from_bytes(data: bytes) -> np.ndarray:
    """Deserializza un embedding da bytes."""
    return np.frombuffer(data, dtype=np.float32)
