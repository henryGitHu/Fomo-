"""Cost tracking and budget enforcement."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import CostEntry, Job, Project


class BudgetExceeded(Exception):
    def __init__(self, message: str, *, spent: float, limit: float, requested: float | None):
        super().__init__(message)
        self.spent, self.limit, self.requested = spent, limit, requested


class UnknownCost(Exception):
    pass


def spent(db: Session, project_id: str) -> float:
    v = db.scalar(select(func.coalesce(func.sum(CostEntry.amount), 0.0)).where(CostEntry.project_id == project_id))
    return float(v or 0.0)


def unknown_cost_count(db: Session, project_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(CostEntry).where(
        CostEntry.project_id == project_id, CostEntry.amount.is_(None))) or 0)


def reserved(db: Session, project_id: str, exclude_job: str | None = None) -> float:
    """Estimated cost of queued/running jobs not yet spent."""
    total = 0.0
    q = select(Job).where(Job.project_id == project_id, Job.status.in_(["queued", "running", "paused"]))
    for j in db.scalars(q):
        if j.id == exclude_job:
            continue
        est = (j.payload or {}).get("estimated_cost") or 0.0
        done = (j.result or {}).get("spent", 0.0)
        total += max(0.0, float(est) - float(done))
    return total


def check_budget(db: Session, project: Project, amount: float | None, *, include_reserved: bool = True,
                 exclude_job: str | None = None, allow_unknown: bool = False) -> None:
    if amount is None:
        if project.budget_limit is not None and not allow_unknown:
            raise UnknownCost("Provider price is unknown; confirm the operation explicitly to proceed under a budget.")
        return
    if project.budget_limit is None:
        return
    s = spent(db, project.id)
    r = reserved(db, project.id, exclude_job=exclude_job) if include_reserved else 0.0
    if s + r + amount > project.budget_limit + 1e-9:
        raise BudgetExceeded(
            f"Budget limit ${project.budget_limit:.2f} would be exceeded: spent ${s:.2f}, reserved ${r:.2f}, "
            f"this operation ${amount:.2f}", spent=s, limit=project.budget_limit, requested=amount)


def record(db: Session, *, project_id: str, provider_id: str, model_id: str, amount: float | None,
           is_estimate: bool, operation: str = "generate", page_id: str | None = None,
           section_id: str | None = None, job_id: str | None = None, is_retry: bool = False) -> CostEntry:
    e = CostEntry(project_id=project_id, provider_id=provider_id, model_id=model_id, amount=amount,
                  is_estimate=is_estimate, operation=operation, page_id=page_id, section_id=section_id,
                  job_id=job_id, is_retry=is_retry)
    db.add(e)
    db.flush()
    if job_id:
        j = db.get(Job, job_id)
        if j is not None:
            res = dict(j.result or {})
            res["spent"] = float(res.get("spent", 0.0)) + float(amount or 0.0)
            j.result = res
    return e


def summary(db: Session, project: Project) -> dict:
    rows = list(db.scalars(select(CostEntry).where(CostEntry.project_id == project.id)))
    by_provider: dict[str, dict] = {}
    by_section: dict[str, float] = {}
    retries = 0
    for r in rows:
        k = f"{r.provider_id}/{r.model_id}"
        d = by_provider.setdefault(k, {"provider": r.provider_id, "model": r.model_id, "count": 0, "amount": 0.0,
                                       "unknown": 0, "estimated": 0})
        d["count"] += 1
        d["amount"] += r.amount or 0.0
        d["unknown"] += r.amount is None
        d["estimated"] += bool(r.is_estimate)
        if r.section_id:
            by_section[r.section_id] = by_section.get(r.section_id, 0.0) + (r.amount or 0.0)
        retries += r.is_retry
    total = sum(r.amount or 0.0 for r in rows)
    return {"total": round(total, 4), "budget_limit": project.budget_limit,
            "remaining_budget": None if project.budget_limit is None else round(project.budget_limit - total, 4),
            "reserved": round(reserved(db, project.id), 4), "generations": len(rows), "retries": retries,
            "unknown_cost_operations": sum(1 for r in rows if r.amount is None),
            "by_provider": list(by_provider.values()), "by_section": by_section}
