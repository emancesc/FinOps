"""
Worker RQ compatibile con Windows (TimerDeathPenalty).
"""
from __future__ import annotations
import logging
import os

from rq import SimpleWorker
from rq.timeouts import TimerDeathPenalty

logger = logging.getLogger(__name__)


class WindowsWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty


def run_extraction(job_id: str, account_id: str, region: str) -> dict:
    """
    Task RQ: estrae tutte le risorse per account/region e le persiste in raw_resources.
    Ritorna un summary dict.
    """
    from .aws_client import AWSClient, DEFAULT_RESOURCE_TYPES
    from .db import upsert_resources

    assume_role_arn = os.environ.get("AWS_ASSUME_ROLE_ARN") or None
    client = AWSClient(account_id=account_id, region=region, assume_role_arn=assume_role_arn)
    resources = client.list_resources(DEFAULT_RESOURCE_TYPES)
    count = upsert_resources(job_id, resources)

    logger.info("Job %s: estratte e persistite %d risorse", job_id, count)
    return {"job_id": job_id, "resources_extracted": count}
