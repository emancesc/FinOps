from __future__ import annotations
import os
from redis import Redis
from rq import Queue

_redis: Redis | None = None

QUEUE_NAMES = ["extraction", "enrichment", "arbitration", "graph_build"]


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        )
    return _redis


def get_queue(name: str) -> Queue:
    assert name in QUEUE_NAMES, f"Coda sconosciuta: {name}"
    return Queue(name, connection=get_redis())
