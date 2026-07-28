"""Worker RQ Windows-compatibile per agent2."""
from __future__ import annotations
import asyncio
import logging

from rq import SimpleWorker
from rq.timeouts import TimerDeathPenalty

logger = logging.getLogger(__name__)


class WindowsWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty


def run_enrichment(job_id: str) -> dict:
    """Task RQ: esegue il loop di enrichment per il job."""
    from .enrichment import enrich_job
    return asyncio.run(enrich_job(job_id))
