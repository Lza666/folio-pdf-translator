from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from redis import Redis
from rq import Queue
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, JobStatus
from app.security import secret_store


def queue_health() -> str:
    settings = get_settings()
    if settings.queue_mode == "inline":
        return "inline"
    try:
        redis = Redis.from_url(settings.redis_url, socket_connect_timeout=0.4)
        return "rq" if redis.ping() else "unavailable"
    except Exception:
        return "inline" if settings.queue_mode == "auto" else "unavailable"


def enqueue_job(job_id: str) -> str:
    settings = get_settings()
    mode = queue_health()
    if mode == "rq":
        queue = Queue("folio", connection=Redis.from_url(settings.redis_url))
        queue.enqueue("app.tasks.process_job", job_id, job_timeout="4h", result_ttl=86400)
    elif settings.queue_mode == "rq":
        raise RuntimeError("Redis/RQ 不可用")
    else:
        from app.services.pipeline import process_job

        thread = threading.Thread(target=process_job, args=(job_id,), daemon=True, name=f"folio-{job_id[:8]}")
        thread.start()
    return mode


def cleanup_expired_jobs(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    removed = 0
    with SessionLocal() as db:
        jobs = list(
            db.scalars(
                select(Job).where(Job.expires_at < now, Job.status != JobStatus.expired.value)
            )
        )
        for job in jobs:
            job_dir = Path(job.source_path).parent.resolve()
            jobs_root = get_settings().jobs_dir.resolve()
            if jobs_root in job_dir.parents:
                shutil.rmtree(job_dir, ignore_errors=True)
            secret_store.delete(f"pdf-password:{job.id}")
            job.status = JobStatus.expired.value
            job.source_path = ""
            removed += 1
        db.commit()
    return removed

