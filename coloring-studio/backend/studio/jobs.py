"""Durable, database-backed job queue and worker.

Jobs live in the `jobs` table, so they survive restarts. A worker claims a job
with a time-limited lease; on startup (or when a lease expires) running jobs
are put back in the queue. Batch jobs commit after every page and skip pages
they already finished, so resuming never duplicates or loses work.

Redis is not required: the database is the source of truth. Multiple worker
processes are safe on PostgreSQL (SELECT ... FOR UPDATE SKIP LOCKED).
"""
from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import costs, services
from .config import get_settings
from .db import SessionLocal
from .models import Job, Page, Project
from .security import redact

log = logging.getLogger("studio.jobs")

HANDLERS: dict[str, callable] = {}


def handler(name: str):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


def now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class JobPaused(Exception):
    pass


class JobCancelled(Exception):
    pass


# ------------------------------------------------------------------ enqueue / control

def enqueue(db: Session, project_id: str | None, type_: str, payload: dict, *, estimated_cost: float | None = None,
            max_attempts: int = 3) -> Job:
    if type_ not in HANDLERS:
        raise ValueError(f"unknown job type {type_}")
    payload = dict(payload)
    if estimated_cost is not None:
        payload["estimated_cost"] = estimated_cost
    job = Job(project_id=project_id, type=type_, payload=payload, max_attempts=max_attempts,
              result={"done": [], "outcomes": []})
    db.add(job)
    db.flush()
    if type_ == "generate_batch":
        for pid in payload.get("page_ids", []):
            pg = db.get(Page, pid)
            if pg and not pg.locked and pg.status in ("planned", "failed", "revision_requested"):
                pg.status = "queued"
    return job


def request_cancel(db: Session, job: Job, pause: bool = False) -> None:
    if job.status in ("succeeded", "failed", "cancelled"):
        return
    if job.status in ("queued", "paused"):
        job.status = "paused" if pause else "cancelled"
        job.finished_at = None if pause else now()
        _release_pages(db, job)
        return
    res = dict(job.result or {})
    res["stop_mode"] = "pause" if pause else "cancel"
    job.result = res
    job.cancel_requested = True


def resume(db: Session, job: Job) -> None:
    if job.status in ("paused", "failed", "cancelled"):
        job.status = "queued"
        job.cancel_requested = False
        job.error = None
        res = dict(job.result or {})
        res.pop("stop_mode", None)
        job.result = res
        job.finished_at = None


def _release_pages(db: Session, job: Job) -> None:
    for pid in job.payload.get("page_ids", []):
        pg = db.get(Page, pid)
        if pg and pg.status in ("queued", "generating"):
            pg.status = "awaiting_review" if pg.current_revision_id else "planned"


def recover_stale(db: Session, *, force: bool = False) -> int:
    """Re-queue running jobs whose lease expired (or all running jobs when force=True, e.g. at startup)."""
    n = 0
    for job in db.scalars(select(Job).where(Job.status == "running")):
        lease = _aware(job.lease_until)
        if force or lease is None or lease < now():
            job.status = "queued"
            job.lease_until = None
            n += 1
            for pid in job.payload.get("page_ids", []):
                pg = db.get(Page, pid)
                if pg and pg.status == "generating":
                    pg.status = "queued"
    db.flush()
    return n


# ------------------------------------------------------------------ execution

def claim(db: Session) -> Job | None:
    q = select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1)
    if db.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    job = db.scalars(q).first()
    if job is None:
        return None
    lease = now() + timedelta(seconds=get_settings().job_lease_seconds)
    rows = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                      .values(status="running", lease_until=lease, started_at=job.started_at or now(),
                              attempts=Job.attempts + 1)).rowcount
    db.commit()
    if rows != 1:
        return None
    db.refresh(job)
    return job


class JobContext:
    def __init__(self, db: Session, job: Job):
        self.db, self.job = db, job

    def progress(self, frac: float, msg: str = "") -> None:
        self.job.progress = round(max(0.0, min(1.0, frac)), 4)
        self.job.progress_message = msg
        self.job.lease_until = now() + timedelta(seconds=get_settings().job_lease_seconds)
        self.db.commit()

    def checkpoint(self) -> None:
        """Raise if a pause/cancel was requested (checked between units of work)."""
        self.db.refresh(self.job)
        if self.job.cancel_requested:
            mode = (self.job.result or {}).get("stop_mode", "cancel")
            raise JobPaused() if mode == "pause" else JobCancelled()

    def add_result(self, **kw) -> None:
        res = dict(self.job.result or {})
        for k, v in kw.items():
            if isinstance(v, list) and isinstance(res.get(k), list):
                res[k] = res[k] + v
            else:
                res[k] = v
        self.job.result = res


def run_job(db: Session, job: Job) -> Job:
    ctx = JobContext(db, job)
    try:
        HANDLERS[job.type](ctx)
        job.status = "succeeded"
        job.progress = 1.0
        job.finished_at = now()
    except JobPaused:
        db.rollback()
        job = db.get(Job, job.id)
        job.status = "paused"
        job.cancel_requested = False
        _release_pages(db, job)
    except JobCancelled:
        db.rollback()
        job = db.get(Job, job.id)
        job.status = "cancelled"
        job.finished_at = now()
        _release_pages(db, job)
    except Exception as e:  # job failed; keep committed per-page work
        db.rollback()
        job = db.get(Job, job.id)
        job.status = "failed"
        job.error = redact(f"{type(e).__name__}: {e}")
        job.finished_at = now()
        _release_pages(db, job)
        log.warning("job %s failed: %s", job.id, redact(traceback.format_exc()))
    db.commit()
    if job.status == "succeeded" and job.type == "generate_batch":
        _autopilot_continue(db, job)
    return job


def run_pending(max_jobs: int = 1000) -> int:
    """Process queued jobs synchronously (used by tests and the CLI)."""
    n = 0
    while n < max_jobs:
        db = SessionLocal()
        try:
            job = claim(db)
            if job is None:
                return n
            run_job(db, job)
            n += 1
        finally:
            db.close()
    return n


def _autopilot_continue(db: Session, job: Job) -> None:
    project = db.get(Project, job.project_id) if job.project_id else None
    if not project or not project.autopilot or not job.payload.get("autopilot"):
        return
    nxt = next_batch_pages(db, project, project.batch_size)
    if not nxt:
        return
    try:
        est = estimate_pages(db, project, nxt)
        costs.check_budget(db, project, est)
    except (costs.BudgetExceeded, costs.UnknownCost) as e:
        log.info("autopilot stopped: %s", e)
        return
    enqueue(db, project.id, "generate_batch", {"page_ids": [p.id for p in nxt], "autopilot": True},
            estimated_cost=est)
    db.commit()


def next_batch_pages(db: Session, project: Project, size: int) -> list[Page]:
    q = select(Page).where(Page.project_id == project.id, Page.locked.is_(False),
                           Page.status.in_(["planned", "failed"])).order_by(Page.position).limit(size)
    return list(db.scalars(q))


def estimate_pages(db: Session, project: Project, pages: list[Page]) -> float | None:
    from . import routing
    from .providers import registry
    from .providers.base import GenerationRequest
    tw, th = services.target_pixels(project)
    total = 0.0
    for pg in pages:
        ch = routing.choose(db, project, pg, need_px=max(tw, th))
        prov = registry.get_provider(db, ch.provider_id)
        est = prov.estimate_cost(GenerationRequest(project.id, pg.id, pg.section_id, ch.provider_id, ch.model_id,
                                                   "", width=tw, height=th))
        if est.amount is None:
            return None
        total += est.amount
    return round(total, 4)


# ------------------------------------------------------------------ worker thread

class Worker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with SessionLocal() as db:
            n = recover_stale(db, force=True)
            db.commit()
            if n:
                log.info("re-queued %d interrupted job(s)", n)
        self._thread = threading.Thread(target=self._loop, name="studio-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        poll = get_settings().worker_poll_seconds
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                try:
                    recover_stale(db)
                    db.commit()
                    job = claim(db)
                    if job is not None:
                        run_job(db, job)
                        continue
                finally:
                    db.close()
            except Exception:  # pragma: no cover - keep the worker alive
                log.exception("worker loop error")
            self._stop.wait(poll)


# Register handlers (import side effects)
from . import job_handlers  # noqa: E402,F401
