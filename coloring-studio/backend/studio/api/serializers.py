"""ORM -> JSON views used by the API."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import kdp, services
from ..models import Job, Page, Project, Revision, Section


def asset_url(asset_id: str | None) -> str | None:
    return f"/api/assets/{asset_id}" if asset_id else None


def revision(r: Revision | None, brief: bool = False) -> dict | None:
    if r is None:
        return None
    q = r.quality or {}
    d = {"id": r.id, "kind": r.kind, "provider_id": r.provider_id, "model_id": r.model_id,
         "image_url": asset_url(r.asset_id), "width": r.width, "height": r.height, "cost": r.cost,
         "cost_is_estimate": r.cost_is_estimate, "status": r.status, "created_at": r.created_at,
         "qa_passed": q.get("passed"), "scores": q.get("scores", {}), "effective_ppi": q.get("effective_ppi"),
         "native_ppi": q.get("native_ppi"), "battle_id": r.battle_id, "parent_revision_id": r.parent_revision_id}
    if not brief:
        d.update({"checks": q.get("checks", []), "effective_params": {k: v for k, v in (r.effective_params or {}).items()
                                                                     if k != "enhancement"},
                  "enhancement": (r.effective_params or {}).get("enhancement"), "prompt": r.prompt,
                  "prompt_version": r.prompt_version, "provider_job_id": r.provider_job_id,
                  "duration_seconds": r.duration_seconds, "error": r.error, "metrics": {
                      k: q.get(k) for k in ("ink_coverage", "midtone_fraction", "stroke_width_px", "contrast",
                                            "sharpness_laplacian_var", "background_white_fraction")},
                  "text_detection": q.get("text_detection")})
    return d


def page(db: Session, p: Page, brief: bool = True) -> dict:
    rev = db.get(Revision, p.current_revision_id) if p.current_revision_id else None
    return {"id": p.id, "position": p.position, "section_id": p.section_id, "subject": p.subject,
            "concept": p.concept, "fact": p.fact, "vocabulary": p.vocabulary, "question": p.question,
            "status": p.status, "locked": p.locked, "caption_text": p.caption_text, "caption_status": p.caption_status,
            "caption_version": p.caption_version, "provider_override": p.provider_override,
            "model_override": p.model_override, "version": p.version, "prompt_version": p.prompt_version,
            "source_refs": p.source_refs, "current_revision": revision(rev, brief=brief),
            "updated_at": p.updated_at}


def section(s: Section, pages: list[Page] | None = None) -> dict:
    d = {c.key: getattr(s, c.key) for c in Section.__table__.columns}
    if pages is not None:
        mine = [p for p in pages if p.section_id == s.id]
        d["actual_count"] = len(mine)
        d["page_positions"] = [p.position for p in mine]
        d["approved"] = sum(1 for p in mine if p.status in services.APPROVED_STATES)
    return d


def project(db: Session, p: Project, detail: bool = False) -> dict:
    d = {c.key: getattr(p, c.key) for c in Project.__table__.columns}
    counts = dict(db.execute(select(Page.status, func.count()).where(Page.project_id == p.id)
                             .group_by(Page.status)).all())
    total = sum(counts.values())
    done = sum(v for k, v in counts.items() if k in services.APPROVED_STATES)
    generated = int(db.scalar(select(func.count()).select_from(Page).where(
        Page.project_id == p.id, Page.current_revision_id.is_not(None))) or 0)
    d["progress"] = {"planned": total, "generated": generated, "approved": done, "by_status": counts}
    first = db.scalars(select(Page).where(Page.project_id == p.id, Page.current_revision_id.is_not(None))
                       .order_by(Page.position)).first()
    rev = db.get(Revision, first.current_revision_id) if first else None
    d["thumbnail_url"] = asset_url(rev.asset_id) if rev else None
    d["interior_page_count"] = kdp.interior_page_count(p.illustration_count, p.intro_pages)
    d["active_jobs"] = int(db.scalar(select(func.count()).select_from(Job).where(
        Job.project_id == p.id, Job.status.in_(["queued", "running"]))) or 0)
    if detail:
        pages = list(db.scalars(select(Page).where(Page.project_id == p.id).order_by(Page.position)))
        secs = list(db.scalars(select(Section).where(Section.project_id == p.id).order_by(Section.position)))
        d["sections"] = [section(s, pages) for s in secs]
        geo = services.geometry(p)
        d["geometry"] = geo.to_dict()
        d["art_box_in"] = services.art_box(p)
        d["target_px"] = services.target_pixels(p)
        d["cover_geometry"] = kdp.cover_geometry(p.trim_size, d["interior_page_count"], p.paper_type).to_dict()
    return d


def job(j: Job) -> dict:
    return {c.key: getattr(j, c.key) for c in Job.__table__.columns if c.key != "lease_until"}
