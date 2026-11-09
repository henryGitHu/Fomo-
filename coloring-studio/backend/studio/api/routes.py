"""HTTP API (all JSON under /api)."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import (battle, benchmark, captions, costs, editor, generation, jobs, kdp, planning, qa_book, routing,
                services, storage)
from ..db import SessionLocal
from ..imaging import quality
from ..models import (Asset, Battle, BenchmarkResult, CaptionRevision, ChatMessage, EditHistory, ExportRecord, Job,
                      Page, Project, ProviderConfig, ResearchReport, Revision, Section, StyleProfile)
from ..providers import registry
from ..providers.base import GenerationRequest, ProviderError
from ..security import encrypt_secret, mask_secret, redact
from . import serializers as S

router = APIRouter(prefix="/api")
TEXT_PROVIDERS = {"anthropic": "Anthropic Claude (text: research, captions, chat)"}


def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _project(db: Session, pid: str) -> Project:
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


def _page(db: Session, pid: str) -> Page:
    p = db.get(Page, pid)
    if p is None:
        raise HTTPException(404, "page not found")
    return p


def _check_version(obj, version: int | None) -> None:
    if version is not None and obj.version != version:
        raise HTTPException(409, f"stale version: you have v{version}, current is v{obj.version}; reload and retry")


# ================================================================== health

@router.get("/health")
def health(db: Session = Depends(get_db)):
    from ..db import table_names
    return {"ok": True, "tables": len(table_names()), "time": datetime.now(timezone.utc).isoformat()}


# ================================================================== projects

class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    subtitle: str = ""
    author: str = ""
    theme: str = ""
    audience_age: str = "6-9"
    illustration_count: int = Field(20, ge=1, le=400)
    trim_size: str = "8.5x11"
    paper_type: Literal["white", "cream"] = "white"
    bleed: bool = False
    illustration_style: str = "clean cartoon line art"
    caption_settings: dict = {}
    budget_limit: float | None = Field(None, ge=0)
    generation_mode: Literal["manual", "auto", "quality_first"] = "manual"
    routing_preference: Literal["lowest_cost", "balanced", "highest_quality"] = "balanced"
    default_provider: str = "mock"
    default_model: str = "mock-lineart-v1"
    batch_size: int = Field(5, ge=1, le=50)
    autopilot: bool = False
    education_progression: bool = True
    max_retries: int = Field(2, ge=0, le=5)
    back_description: str = ""


class ProjectPatch(BaseModel):
    version: int | None = None
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    theme: str | None = None
    audience_age: str | None = None
    trim_size: str | None = None
    paper_type: Literal["white", "cream"] | None = None
    bleed: bool | None = None
    illustration_style: str | None = None
    caption_settings: dict | None = None
    budget_limit: float | None = None
    clear_budget: bool = False
    generation_mode: Literal["manual", "auto", "quality_first"] | None = None
    routing_preference: Literal["lowest_cost", "balanced", "highest_quality"] | None = None
    default_provider: str | None = None
    default_model: str | None = None
    batch_size: int | None = Field(None, ge=1, le=50)
    autopilot: bool | None = None
    education_progression: bool | None = None
    max_retries: int | None = Field(None, ge=0, le=5)
    fallback_enabled: bool | None = None
    enhance_images: bool | None = None
    back_description: str | None = None
    cover_settings: dict | None = None
    intro_pages: int | None = Field(None, ge=4, le=12)


@router.get("/projects")
def list_projects(include_archived: bool = False, db: Session = Depends(get_db)):
    q = select(Project).order_by(Project.updated_at.desc())
    if not include_archived:
        q = q.where(Project.archived.is_(False))
    return [S.project(db, p) for p in db.scalars(q)]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    if body.trim_size not in kdp.TRIM_SIZES:
        raise HTTPException(422, f"trim_size must be one of {list(kdp.TRIM_SIZES)}")
    if body.default_provider not in registry.PROVIDER_CLASSES:
        raise HTTPException(422, "unknown default_provider")
    p = services.create_project(db, body.model_dump())
    return S.project(db, p, detail=True)


@router.get("/projects/{pid}")
def get_project(pid: str, db: Session = Depends(get_db)):
    return S.project(db, _project(db, pid), detail=True)


@router.patch("/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, db: Session = Depends(get_db)):
    p = _project(db, pid)
    _check_version(p, body.version)
    data = body.model_dump(exclude_unset=True, exclude={"version", "clear_budget"})
    if "trim_size" in data and data["trim_size"] not in kdp.TRIM_SIZES:
        raise HTTPException(422, "unsupported trim size")
    for k, v in data.items():
        if v is not None:
            setattr(p, k, v)
    if body.clear_budget:
        p.budget_limit = None
    if "caption_settings" in data:
        p.caption_settings = captions.settings_for(p.caption_settings)
    p.version += 1
    return S.project(db, p, detail=True)


@router.delete("/projects/{pid}")
def delete_project(pid: str, confirm_title: str = Query(...), db: Session = Depends(get_db)):
    p = _project(db, pid)
    if confirm_title != p.title:
        raise HTTPException(400, "confirm_title must exactly match the project title")
    services.delete_project(db, p)
    return {"deleted": pid}


@router.post("/projects/{pid}/duplicate", status_code=201)
def duplicate_project(pid: str, db: Session = Depends(get_db)):
    return S.project(db, services.duplicate_project(db, _project(db, pid)))


@router.post("/projects/{pid}/archive")
def archive_project(pid: str, archived: bool = True, db: Session = Depends(get_db)):
    p = _project(db, pid)
    p.archived = archived
    if archived:
        p.status = "archived"
    elif p.status == "archived":
        p.status = "in_progress"
    p.version += 1
    return S.project(db, p)


# ================================================================== style DNA

@router.get("/projects/{pid}/style")
def get_style(pid: str, db: Session = Depends(get_db)):
    sp = services.style_of(db, _project(db, pid).id)
    return {"id": sp.id, "name": sp.name, "data": sp.data, "anchor_revision_ids": sp.anchor_revision_ids,
            "anchor_metrics": sp.anchor_metrics, "version": sp.version,
            "anchors": [S.revision(db.get(Revision, r), brief=True) for r in sp.anchor_revision_ids]}


class StyleIn(BaseModel):
    data: dict
    version: int | None = None


@router.put("/projects/{pid}/style")
def put_style(pid: str, body: StyleIn, db: Session = Depends(get_db)):
    sp = services.style_of(db, _project(db, pid).id)
    _check_version(sp, body.version)
    sp.data = {**sp.data, **body.data}
    sp.version += 1
    return get_style(pid, db)


class AnchorIn(BaseModel):
    revision_ids: list[str]


@router.post("/projects/{pid}/style/anchor")
def set_anchor(pid: str, body: AnchorIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    sp = services.style_of(db, p.id)
    metrics = []
    for rid in body.revision_ids:
        r = db.get(Revision, rid)
        if r is None or r.project_id != p.id:
            raise HTTPException(422, f"revision {rid} not in project")
        metrics.append(r.quality)
    sp.anchor_revision_ids = body.revision_ids
    sp.anchor_metrics = quality.anchor_from(metrics)
    sp.version += 1
    return get_style(pid, db)


@router.get("/styles")
def list_styles(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "data": s.data} for s in
            db.scalars(select(StyleProfile).where(StyleProfile.is_library.is_(True)))]


class SaveStyleIn(BaseModel):
    project_id: str
    name: str


@router.post("/styles", status_code=201)
def save_style(body: SaveStyleIn, db: Session = Depends(get_db)):
    sp = services.style_of(db, _project(db, body.project_id).id)
    lib = StyleProfile(project_id=None, is_library=True, name=body.name, data=dict(sp.data),
                       anchor_metrics=dict(sp.anchor_metrics))
    db.add(lib)
    db.flush()
    return {"id": lib.id, "name": lib.name, "data": lib.data}


@router.post("/projects/{pid}/style/apply/{style_id}")
def apply_style(pid: str, style_id: str, db: Session = Depends(get_db)):
    lib = db.get(StyleProfile, style_id)
    if lib is None or not lib.is_library:
        raise HTTPException(404, "library style not found")
    sp = services.style_of(db, _project(db, pid).id)
    sp.data = dict(lib.data)
    sp.anchor_metrics = dict(lib.anchor_metrics)
    sp.version += 1
    return get_style(pid, db)


# ================================================================== research & planning

class ResearchIn(BaseModel):
    topic: str | None = None


@router.post("/projects/{pid}/research", status_code=202)
def start_research(pid: str, body: ResearchIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    return S.job(jobs.enqueue(db, p.id, "research", {"topic": body.topic or p.theme or p.title}))


@router.get("/projects/{pid}/research")
def get_research(pid: str, db: Session = Depends(get_db)):
    rr = services.latest_research(db, _project(db, pid).id)
    if rr is None:
        return None
    return {"id": rr.id, "topic": rr.topic, "mode": rr.mode, "created_at": rr.created_at, **rr.data}


@router.get("/projects/{pid}/sections/recommend")
def recommend(pid: str, count: int | None = None, db: Session = Depends(get_db)):
    p = _project(db, pid)
    return planning.recommend_sections(count or p.illustration_count, p.education_progression)


class PlanIn(BaseModel):
    sections: int | None = Field(None, ge=1, le=60)
    illustration_count: int | None = Field(None, ge=1, le=400)
    dry_run: bool = False


@router.post("/projects/{pid}/plan")
def make_plan(pid: str, body: PlanIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    if body.illustration_count:
        p.illustration_count = body.illustration_count
    try:
        plan = services.plan_from_research(db, p, body.sections)
        if body.dry_run:
            return {"plan": plan}
        services.apply_plan(db, p, plan)
    except services.ConflictError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    p.version += 1
    return S.project(db, p, detail=True)


class SectionIn(BaseModel):
    name: str = "New section"
    illustration_count: int = Field(1, ge=1, le=400)
    theme_instructions: str = ""
    complexity: Literal["low", "medium", "high"] = "medium"


class SectionPatch(BaseModel):
    version: int | None = None
    name: str | None = None
    illustration_count: int | None = Field(None, ge=1, le=400)
    theme_instructions: str | None = None
    desired_scenes: list[str] | None = None
    objects: list[str] | None = None
    complexity: Literal["low", "medium", "high"] | None = None
    educational_goals: str | None = None
    caption_preferences: dict | None = None
    reference_asset_ids: list[str] | None = None
    excluded_content: str | None = None
    preferred_provider: str | None = None
    preferred_model: str | None = None
    clear_provider: bool = False
    learning_stage: str | None = None
    locked: bool | None = None


@router.post("/projects/{pid}/sections", status_code=201)
def add_section(pid: str, body: SectionIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    s = services.add_section(db, p, body.name, body.illustration_count, theme_instructions=body.theme_instructions,
                             complexity=body.complexity)
    return S.section(s)


def _section(db: Session, sid: str) -> Section:
    s = db.get(Section, sid)
    if s is None:
        raise HTTPException(404, "section not found")
    return s


@router.patch("/sections/{sid}")
def patch_section(sid: str, body: SectionPatch, db: Session = Depends(get_db)):
    s = _section(db, sid)
    _check_version(s, body.version)
    p = db.get(Project, s.project_id)
    data = body.model_dump(exclude_unset=True, exclude={"version", "illustration_count", "clear_provider"})
    if data.get("preferred_provider") and data["preferred_provider"] not in registry.PROVIDER_CLASSES:
        raise HTTPException(422, "unknown provider")
    for k, v in data.items():
        if v is not None:
            setattr(s, k, v)
    if body.clear_provider:
        s.preferred_provider = s.preferred_model = None
    try:
        if body.illustration_count and body.illustration_count != s.illustration_count:
            services.set_section_count(db, p, s, body.illustration_count)
    except services.ConflictError as e:
        raise HTTPException(409, str(e))
    s.version += 1
    return S.section(s)


@router.delete("/sections/{sid}")
def delete_section(sid: str, db: Session = Depends(get_db)):
    s = _section(db, sid)
    try:
        services.remove_section(db, db.get(Project, s.project_id), s)
    except services.ConflictError as e:
        raise HTTPException(409, str(e))
    return {"deleted": sid}


@router.post("/sections/{sid}/duplicate", status_code=201)
def dup_section(sid: str, db: Session = Depends(get_db)):
    s = _section(db, sid)
    return S.section(services.duplicate_section(db, db.get(Project, s.project_id), s))


class ReorderIn(BaseModel):
    section_ids: list[str]


@router.post("/projects/{pid}/sections/reorder")
def reorder(pid: str, body: ReorderIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    try:
        services.reorder_sections(db, p, body.section_ids)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return S.project(db, p, detail=True)


class SectionActionIn(BaseModel):
    action: Literal["approve", "lock", "unlock", "generate", "regenerate"]
    provider_id: str | None = None
    model_id: str | None = None


@router.post("/sections/{sid}/action")
def section_action(sid: str, body: SectionActionIn, db: Session = Depends(get_db)):
    s = _section(db, sid)
    p = db.get(Project, s.project_id)
    pages = list(db.scalars(select(Page).where(Page.section_id == s.id).order_by(Page.position)))
    if body.action in ("generate", "regenerate"):
        targets = [pg for pg in pages if not pg.locked and (body.action == "regenerate" or not pg.current_revision_id)
                   and pg.status not in services.APPROVED_STATES]
        return _enqueue_generation(db, p, targets, body.provider_id, body.model_id)
    changed = []
    for pg in pages:
        before = services.snapshot(pg)
        if body.action == "approve" and pg.current_revision_id and not pg.locked:
            pg.status = "approved"
        elif body.action == "lock" and pg.current_revision_id:
            pg.locked, pg.status = True, "locked"
        elif body.action == "unlock" and pg.locked:
            pg.locked, pg.status = False, "approved"
        else:
            continue
        pg.version += 1
        services.log_edit(db, p.id, body.action, page=pg, before=before)
        changed.append(pg.position)
    return {"changed": changed}


# ================================================================== pages

@router.get("/projects/{pid}/pages")
def list_pages(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    return [S.page(db, pg) for pg in db.scalars(select(Page).where(Page.project_id == p.id).order_by(Page.position))]


@router.get("/pages/{page_id}")
def get_page(page_id: str, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    d = S.page(db, pg, brief=False)
    d["revisions"] = [S.revision(r, brief=True) for r in db.scalars(
        select(Revision).where(Revision.page_id == pg.id).order_by(Revision.created_at.desc()))]
    d["caption_history"] = [{"id": c.id, "text": c.text, "source": c.source, "created_at": c.created_at}
                            for c in db.scalars(select(CaptionRevision).where(CaptionRevision.page_id == pg.id)
                                                .order_by(CaptionRevision.created_at.desc()))]
    p = db.get(Project, pg.project_id)
    if pg.caption_text:
        d["caption_validation"] = captions.validate(pg.caption_text, pg, {**captions.settings_for(
            p.caption_settings), "topic_terms": p.theme}, services.art_box(p)[0])
    return d


class PagePatch(BaseModel):
    version: int | None = None
    subject: str | None = None
    concept: str | None = None
    fact: str | None = None
    vocabulary: str | None = None
    question: str | None = None
    caption_text: str | None = None
    provider_override: str | None = None
    model_override: str | None = None
    clear_provider: bool = False


@router.patch("/pages/{page_id}")
def patch_page(page_id: str, body: PagePatch, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    _check_version(pg, body.version)
    if pg.locked:
        raise HTTPException(423, f"page {pg.position} is locked")
    p = db.get(Project, pg.project_id)
    before = services.snapshot(pg)
    data = body.model_dump(exclude_unset=True, exclude={"version", "caption_text", "clear_provider"})
    if data.get("provider_override") and data["provider_override"] not in registry.PROVIDER_CLASSES:
        raise HTTPException(422, "unknown provider")
    concept_changed = any(k in data and data[k] != getattr(pg, k) for k in ("subject", "concept"))
    for k, v in data.items():
        if v is not None:
            setattr(pg, k, v)
    if body.clear_provider:
        pg.provider_override = pg.model_override = None
    if concept_changed:
        pg.prompt_version += 1
        if pg.current_revision_id:
            pg.status = "revision_requested"
        if pg.caption_status == "ok":
            pg.caption_status = "review"
    pg.version += 1
    validation = None
    if body.caption_text is not None:
        validation = services.set_caption(db, p, pg, body.caption_text, source="user")
    services.log_edit(db, p.id, "page_edit", page=pg, before=before, details={"fields": list(data) +
                      (["caption_text"] if body.caption_text is not None else [])})
    out = S.page(db, pg, brief=False)
    out["caption_validation"] = validation
    return out


class PageActionIn(BaseModel):
    version: int | None = None
    note: str = ""


def _page_action(db: Session, page_id: str, action: str, body: PageActionIn):
    pg = _page(db, page_id)
    _check_version(pg, body.version)
    before = services.snapshot(pg)
    if action == "approve":
        if pg.locked:
            raise HTTPException(423, "page is locked")
        if not pg.current_revision_id:
            raise HTTPException(409, "page has no artwork to approve")
        pg.status = "approved"
        if pg.caption_status == "review":
            pg.caption_status = "ok"  # human reviewed caption together with artwork
    elif action == "reject":
        if pg.locked:
            raise HTTPException(423, "page is locked")
        pg.status = "revision_requested"
    elif action == "lock":
        if not pg.current_revision_id:
            raise HTTPException(409, "only pages with artwork can be locked")
        pg.locked, pg.status = True, "locked"
    elif action == "unlock":
        pg.locked = False
        pg.status = "approved" if pg.current_revision_id else "planned"
    pg.version += 1
    services.log_edit(db, pg.project_id, action, page=pg, before=before, details={"note": body.note})
    return S.page(db, pg)


@router.post("/pages/{page_id}/approve")
def approve_page(page_id: str, body: PageActionIn = PageActionIn(), db: Session = Depends(get_db)):
    return _page_action(db, page_id, "approve", body)


@router.post("/pages/{page_id}/reject")
def reject_page(page_id: str, body: PageActionIn = PageActionIn(), db: Session = Depends(get_db)):
    return _page_action(db, page_id, "reject", body)


@router.post("/pages/{page_id}/lock")
def lock_page(page_id: str, body: PageActionIn = PageActionIn(), db: Session = Depends(get_db)):
    return _page_action(db, page_id, "lock", body)


@router.post("/pages/{page_id}/unlock")
def unlock_page(page_id: str, body: PageActionIn = PageActionIn(), db: Session = Depends(get_db)):
    return _page_action(db, page_id, "unlock", body)


class RestoreIn(BaseModel):
    revision_id: str


@router.post("/pages/{page_id}/restore")
def restore_page(page_id: str, body: RestoreIn, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    if pg.locked:
        raise HTTPException(423, "page is locked")
    r = db.get(Revision, body.revision_id)
    if r is None or r.page_id != pg.id or r.status == "failed":
        raise HTTPException(422, "revision does not belong to this page")
    before = services.snapshot(pg)
    pg.current_revision_id = r.id
    pg.status = "awaiting_review"
    pg.version += 1
    services.log_edit(db, pg.project_id, "restore", page=pg, before=before, details={"revision": r.id})
    return S.page(db, pg)


class LineWeightIn(BaseModel):
    steps: int = Field(ge=-3, le=3)


@router.post("/pages/{page_id}/line-weight")
def page_line_weight(page_id: str, body: LineWeightIn, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    job = jobs.enqueue(db, pg.project_id, "line_weight", {"page_ids": [pg.id], "steps": body.steps})
    return S.job(job)


# ================================================================== generation

class GenerateIn(BaseModel):
    page_ids: list[str] | None = None
    count: int | None = Field(None, ge=1, le=100)
    provider_id: str | None = None
    model_id: str | None = None
    allow_unknown_cost: bool = False
    allow_approved: bool = False
    extra_prompt: str = ""


def _enqueue_generation(db: Session, p: Project, pages: list[Page], provider_id: str | None, model_id: str | None,
                        *, allow_unknown: bool = False, allow_approved: bool = False, extra: str = "",
                        autopilot: bool = False) -> dict:
    if not pages:
        raise HTTPException(409, "no eligible pages (locked, approved or already generated)")
    override = None
    if provider_id:
        if not registry.is_usable(db, provider_id):
            raise HTTPException(422, f"provider {provider_id} is not configured or disabled")
        prov = registry.get_provider(db, provider_id)
        model_id = model_id or registry.get_config(db, provider_id).default_model or prov.list_models()[0].model_id
        try:
            prov.get_capabilities(model_id)
        except ProviderError as e:
            raise HTTPException(422, str(e))
        override = [provider_id, model_id]
        tw, th = services.target_pixels(p)
        e = prov.estimate_cost(GenerationRequest(p.id, None, None, provider_id, model_id, "", width=tw, height=th))
        est = None if e.amount is None else e.amount * len(pages)
    else:
        est = jobs.estimate_pages(db, p, pages)
    try:
        costs.check_budget(db, p, est, allow_unknown=allow_unknown)
    except costs.BudgetExceeded as e:
        raise HTTPException(402, str(e))
    except costs.UnknownCost as e:
        raise HTTPException(402, str(e))
    job = jobs.enqueue(db, p.id, "generate_batch", {"page_ids": [x.id for x in pages], "override": override,
                                                    "allow_unknown_cost": allow_unknown,
                                                    "allow_approved": allow_approved, "extra_prompt": extra,
                                                    "autopilot": autopilot}, estimated_cost=est)
    out = S.job(job)
    out["estimated_cost"] = est
    return out


@router.post("/projects/{pid}/generate", status_code=202)
def generate(pid: str, body: GenerateIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    if body.page_ids:
        pages = [db.get(Page, x) for x in body.page_ids]
        pages = [x for x in pages if x is not None and x.project_id == p.id and not x.locked and
                 (body.allow_approved or x.status not in services.APPROVED_STATES)]
    else:
        pages = jobs.next_batch_pages(db, p, body.count or p.batch_size)
    return _enqueue_generation(db, p, pages, body.provider_id, body.model_id, allow_unknown=body.allow_unknown_cost,
                               allow_approved=body.allow_approved, extra=body.extra_prompt,
                               autopilot=p.autopilot and not body.page_ids)


@router.get("/projects/{pid}/route-preview")
def route_preview(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    tw, th = services.target_pixels(p)
    ranked = routing.rank(db, p, max(tw, th), prefer_real=False)
    return {"mode": p.generation_mode, "preference": p.routing_preference,
            "manual_choice": {"provider_id": p.default_provider, "model_id": p.default_model},
            "ranking": [c.__dict__ for c in ranked]}


# ================================================================== jobs

@router.get("/jobs")
def list_jobs(project_id: str | None = None, status: str | None = None, limit: int = 50,
              db: Session = Depends(get_db)):
    q = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 200))
    if project_id:
        q = q.where(Job.project_id == project_id)
    if status:
        q = q.where(Job.status == status)
    return [S.job(j) for j in db.scalars(q)]


def _job(db: Session, jid: str) -> Job:
    j = db.get(Job, jid)
    if j is None:
        raise HTTPException(404, "job not found")
    return j


@router.get("/jobs/{jid}")
def get_job(jid: str, db: Session = Depends(get_db)):
    return S.job(_job(db, jid))


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: str, db: Session = Depends(get_db)):
    j = _job(db, jid)
    jobs.request_cancel(db, j)
    return S.job(j)


@router.post("/jobs/{jid}/pause")
def pause_job(jid: str, db: Session = Depends(get_db)):
    j = _job(db, jid)
    jobs.request_cancel(db, j, pause=True)
    return S.job(j)


@router.post("/jobs/{jid}/resume")
def resume_job(jid: str, db: Session = Depends(get_db)):
    j = _job(db, jid)
    jobs.resume(db, j)
    return S.job(j)


# ================================================================== battles

class BattleIn(BaseModel):
    page_id: str
    contenders: list[dict] = Field(min_length=2)
    allow_unknown_cost: bool = False


@router.post("/projects/{pid}/battles", status_code=202)
def create_battle(pid: str, body: BattleIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    pg = _page(db, body.page_id)
    for c in body.contenders:
        if not registry.is_usable(db, c.get("provider_id", "")):
            raise HTTPException(422, f"provider {c.get('provider_id')} is not configured")
    try:
        b = battle.create(db, p, pg, [dict(c) for c in body.contenders], allow_unknown_cost=body.allow_unknown_cost)
    except (costs.BudgetExceeded, costs.UnknownCost) as e:
        raise HTTPException(402, str(e))
    except (ValueError, ProviderError) as e:
        raise HTTPException(422, str(e))
    return _battle_view(db, b)


def _battle_view(db: Session, b: Battle) -> dict:
    pg = db.get(Page, b.page_id)
    return {"id": b.id, "page_id": b.page_id, "page_position": pg.position if pg else None,
            "page_subject": pg.subject if pg else None, "status": b.status, "contenders": b.contenders,
            "winner_revision_id": b.winner_revision_id, "job_id": b.job_id, "created_at": b.created_at,
            "current_revision_id": pg.current_revision_id if pg else None,
            "candidates": battle.candidates(db, b)}


@router.get("/projects/{pid}/battles")
def list_battles(pid: str, db: Session = Depends(get_db)):
    return [_battle_view(db, b) for b in db.scalars(select(Battle).where(Battle.project_id == pid)
                                                     .order_by(Battle.created_at.desc()))]


@router.get("/battles/{bid}")
def get_battle(bid: str, db: Session = Depends(get_db)):
    b = db.get(Battle, bid)
    if b is None:
        raise HTTPException(404, "battle not found")
    return _battle_view(db, b)


class WinnerIn(BaseModel):
    revision_id: str
    confirm_replace_approved: bool = False
    approve: bool = False


@router.post("/battles/{bid}/select")
def select_winner(bid: str, body: WinnerIn, db: Session = Depends(get_db)):
    b = db.get(Battle, bid)
    if b is None:
        raise HTTPException(404, "battle not found")
    try:
        battle.select_winner(db, b, body.revision_id, confirm_replace_approved=body.confirm_replace_approved,
                             approve=body.approve)
    except services.LockedError as e:
        raise HTTPException(423, str(e))
    except services.ConflictError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _battle_view(db, b)


# ================================================================== providers (AI Model Control Center)

def _provider_view(db: Session, pid: str, stats: dict) -> dict:
    if pid in TEXT_PROVIDERS:
        cfg = db.get(ProviderConfig, pid)
        from ..security import decrypt_secret
        from ..config import get_settings
        key = decrypt_secret(cfg.encrypted_api_key) if cfg else None
        src = "stored" if key else ("env" if get_settings().env_credentials.get(pid) else "none")
        return {"provider_id": pid, "display_name": TEXT_PROVIDERS[pid], "kind": "text", "enabled": cfg.enabled if cfg
                else True, "key_source": src, "key_masked": mask_secret(key), "models": [], "is_mock": False}
    cfg = registry.get_config(db, pid)
    key, src = registry.resolve_api_key(cfg)
    cls = registry.PROVIDER_CLASSES[pid]
    prov = cls(api_key=key, settings=cfg.settings or {})
    models = []
    for m in prov.list_models():
        d = m.to_dict()
        st = stats.get((pid, m.model_id), {})
        d.update({"measured_avg_quality": st.get("avg_quality"), "measured_avg_latency": st.get("avg_latency"),
                  "measured_failure_rate": st.get("failure_rate"), "samples": st.get("samples", 0)})
        models.append(d)
    usable = registry.is_usable(db, pid)
    status = "simulated" if cls.is_mock else ("ready" if usable and cfg.last_test_ok else
                                               ("untested" if usable else ("disabled" if not cfg.enabled else
                                                                           "needs_key")))
    if cfg.last_test_ok is False and usable:
        status = "error"
    return {"provider_id": pid, "display_name": cls.display_name, "kind": "image", "docs_url": cls.docs_url,
            "is_mock": cls.is_mock, "enabled": cfg.enabled, "priority": cfg.priority,
            "default_model": cfg.default_model or (models[0]["model_id"] if models else None),
            "key_source": src, "key_masked": mask_secret(key), "requires_api_key": cls.requires_api_key,
            "status": status, "usable": usable, "last_test_at": cfg.last_test_at, "last_test_ok": cfg.last_test_ok,
            "last_test_message": cfg.last_test_message, "last_success_at": cfg.last_success_at,
            "settings": {k: v for k, v in (cfg.settings or {}).items()}, "models": models}


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)):
    stats = routing.model_stats(db)
    out = [_provider_view(db, pid, stats) for pid in registry.all_provider_ids()]
    out += [_provider_view(db, pid, stats) for pid in TEXT_PROVIDERS]
    return sorted(out, key=lambda d: (d.get("priority", 999), d["provider_id"]))


class ProviderPut(BaseModel):
    api_key: str | None = Field(None, max_length=500)
    clear_key: bool = False
    enabled: bool | None = None
    default_model: str | None = None
    priority: int | None = Field(None, ge=0, le=1000)
    settings: dict[str, Any] | None = None


@router.put("/providers/{pid}")
def put_provider(pid: str, body: ProviderPut, db: Session = Depends(get_db)):
    if pid in TEXT_PROVIDERS:
        cfg = db.get(ProviderConfig, pid) or ProviderConfig(provider_id=pid, enabled=True)
        db.add(cfg)
    elif pid in registry.PROVIDER_CLASSES:
        cfg = registry.get_config(db, pid)
    else:
        raise HTTPException(404, "unknown provider")
    if body.api_key:
        cfg.encrypted_api_key = encrypt_secret(body.api_key.strip())
        cfg.last_test_ok = None
    if body.clear_key:
        cfg.encrypted_api_key = None
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.priority is not None:
        cfg.priority = body.priority
    if body.settings is not None:
        allowed = {"simulated_price_per_image", "fail_mode", "simulated_latency", "poll_interval", "extra_models"}
        bad = set(body.settings) - allowed
        if bad:
            raise HTTPException(422, f"unsupported settings: {sorted(bad)}")
        cfg.settings = {**(cfg.settings or {}), **body.settings}
    if body.default_model is not None and pid in registry.PROVIDER_CLASSES:
        prov = registry.get_provider(db, pid, require_enabled=False)
        try:
            prov.get_capabilities(body.default_model)
        except ProviderError as e:
            raise HTTPException(422, str(e))
        cfg.default_model = body.default_model
    db.flush()
    return _provider_view(db, pid, routing.model_stats(db))


@router.post("/providers/{pid}/test")
def test_provider(pid: str, db: Session = Depends(get_db)):
    if pid not in registry.PROVIDER_CLASSES:
        raise HTTPException(404, "unknown or text-only provider")
    cfg = registry.get_config(db, pid)
    prov = registry.get_provider(db, pid, require_enabled=False)
    try:
        res = prov.validate_configuration()
        ok, msg = res.ok, res.message
    except ProviderError as e:
        ok, msg = False, str(e)
    except Exception as e:  # network etc.
        ok, msg = False, f"{type(e).__name__}"
    cfg.last_test_at = datetime.now(timezone.utc)
    cfg.last_test_ok = ok
    cfg.last_test_message = redact(msg, [prov.api_key or ""])
    return {"ok": ok, "message": cfg.last_test_message, "live": not prov.is_mock}


class BenchmarkIn(BaseModel):
    contenders: list[dict] = Field(min_length=1)
    max_spend: float = Field(ge=0)


@router.post("/benchmarks", status_code=202)
def start_benchmark(body: BenchmarkIn, db: Session = Depends(get_db)):
    for c in body.contenders:
        if not registry.is_usable(db, c.get("provider_id", "")):
            raise HTTPException(422, f"provider {c.get('provider_id')} is not configured")
    try:
        jid, est = benchmark.start(db, [dict(c) for c in body.contenders], body.max_spend)
    except ValueError as e:
        raise HTTPException(402, str(e))
    return {"job_id": jid, "estimated_cost": est}


@router.get("/benchmarks")
def benchmark_results(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(BenchmarkResult).order_by(BenchmarkResult.created_at.desc()).limit(500)))
    agg: dict = {}
    for r in rows:
        a = agg.setdefault(f"{r.provider_id}/{r.model_id}", {"provider_id": r.provider_id, "model_id": r.model_id,
                                                             "categories": {}, "runs": 0, "failures": 0})
        a["runs"] += 1
        a["failures"] += not r.ok
        a["categories"].setdefault(r.category, r.scores if r.ok else {"error": r.error})
    return {"summary": list(agg.values()), "suite": [c for c, _, _ in benchmark.SUITE]}


# ================================================================== costs

@router.get("/projects/{pid}/costs")
def project_costs(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    out = costs.summary(db, p)
    remaining = list(db.scalars(select(Page).where(Page.project_id == p.id, Page.current_revision_id.is_(None))))
    est = jobs.estimate_pages(db, p, remaining) if remaining else 0.0
    out["estimated_remaining_cost"] = est
    out["estimated_remaining_note"] = ("unknown — at least one routed provider has no verified price" if est is None
                                       else f"{len(remaining)} pages without artwork at current routing")
    secs = {s.id: s.name for s in db.scalars(select(Section).where(Section.project_id == p.id))}
    out["by_section"] = [{"section_id": k, "name": secs.get(k, "?"), "amount": round(v, 4)}
                         for k, v in out["by_section"].items()]
    return out


# ================================================================== captions

class CaptionRegenIn(BaseModel):
    page_ids: list[str] | None = None
    only_missing: bool = False


@router.post("/projects/{pid}/captions/regenerate")
def regen_captions(pid: str, body: CaptionRegenIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    n = services.refresh_captions(db, p, only_missing=body.only_missing, page_ids=body.page_ids)
    return {"updated": n}


# ================================================================== chat

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.get("/projects/{pid}/chat")
def chat_history(pid: str, db: Session = Depends(get_db)):
    _project(db, pid)
    return [{"id": m.id, "role": m.role, "content": m.content, "actions": m.actions, "created_at": m.created_at}
            for m in db.scalars(select(ChatMessage).where(ChatMessage.project_id == pid)
                                .order_by(ChatMessage.created_at))]


@router.post("/projects/{pid}/chat")
def chat(pid: str, body: ChatIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    m = editor.handle_message(db, p, body.message)
    return {"id": m.id, "role": m.role, "content": m.content, "actions": m.actions, "created_at": m.created_at}


# ================================================================== history

@router.get("/projects/{pid}/history")
def history(pid: str, limit: int = 200, db: Session = Depends(get_db)):
    _project(db, pid)
    pos = {pg.id: pg.position for pg in db.scalars(select(Page).where(Page.project_id == pid))}
    return [{"id": e.id, "action": e.action, "source": e.source, "page_id": e.page_id,
             "page_position": pos.get(e.page_id), "details": e.details, "undone": e.undone,
             "created_at": e.created_at}
            for e in db.scalars(select(EditHistory).where(EditHistory.project_id == pid)
                                .order_by(EditHistory.created_at.desc()).limit(min(limit, 1000)))]


@router.post("/projects/{pid}/undo")
def undo(pid: str, page_id: str | None = None, db: Session = Depends(get_db)):
    _project(db, pid)
    try:
        e = services.undo_last(db, pid, page_id)
    except services.LockedError as e2:
        raise HTTPException(423, str(e2))
    return {"undone": e.id if e else None, "action": e.action if e else None}


# ================================================================== QA, preview & export

@router.get("/projects/{pid}/qa")
def qa(pid: str, final: bool = True, db: Session = Depends(get_db)):
    return qa_book.validate(db, _project(db, pid), final=final)


class PublicationIn(BaseModel):
    approved: bool


@router.post("/projects/{pid}/publication-approval")
def publication_approval(pid: str, body: PublicationIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    p.publication_approved = body.approved
    p.version += 1
    return {"publication_approved": p.publication_approved}


@router.get("/projects/{pid}/layout")
def layout(pid: str, db: Session = Depends(get_db)):
    """Physical page map for the Book Preview screen."""
    p = _project(db, pid)
    pages = list(db.scalars(select(Page).where(Page.project_id == p.id).order_by(Page.position)))
    intro = p.intro_pages + (p.intro_pages % 2)
    out = [{"page": i + 1, "kind": k} for i, k in enumerate(
        (["title", "copyright", "belongs_to", "contents"] + ["blank_intro"] * (intro - 4))[:intro])]
    n = intro
    for pg in pages:
        rev = db.get(Revision, pg.current_revision_id) if pg.current_revision_id else None
        n += 1
        out.append({"page": n, "kind": "illustration", "position": pg.position, "page_id": pg.id,
                    "subject": pg.subject, "caption": pg.caption_text, "status": pg.status,
                    "image_url": S.asset_url(rev.asset_id) if rev else None})
        n += 1
        out.append({"page": n, "kind": "blank_verso", "position": pg.position})
    return {"pages": out, "total": n, "geometry": services.geometry(p).to_dict(), "art_box_in": services.art_box(p),
            "cover": kdp.cover_geometry(p.trim_size, n, p.paper_type).to_dict()}


class ExportIn(BaseModel):
    final: bool = True
    kinds: list[Literal["interior", "cover", "zip"]] | None = None


@router.post("/projects/{pid}/export", status_code=202)
def start_export(pid: str, body: ExportIn, db: Session = Depends(get_db)):
    p = _project(db, pid)
    if body.final:
        rep = qa_book.validate(db, p, final=True)
        if not rep["passed"]:
            raise HTTPException(409, {"message": "Export blocked: fix mandatory QA failures first", "qa": rep})
    return S.job(jobs.enqueue(db, p.id, "export", {"final": body.final, "kinds": body.kinds}))


@router.get("/projects/{pid}/exports")
def list_exports(pid: str, db: Session = Depends(get_db)):
    out = []
    for e in db.scalars(select(ExportRecord).where(ExportRecord.project_id == pid)
                        .order_by(ExportRecord.created_at.desc())):
        a = db.get(Asset, e.asset_id)
        out.append({"id": e.id, "kind": e.kind, "created_at": e.created_at, "size": a.size if a else None,
                    "checksum": a.checksum if a else None, "download_url": f"/api/exports/{e.id}/download",
                    "passed": e.report.get("passed") if e.kind == "qa_report" else None})
    return out


@router.get("/exports/{eid}/download")
def download_export(eid: str, db: Session = Depends(get_db)):
    e = db.get(ExportRecord, eid)
    if e is None:
        raise HTTPException(404, "export not found")
    a = db.get(Asset, e.asset_id)
    p = db.get(Project, e.project_id)
    ext = {"application/pdf": "pdf", "application/zip": "zip", "application/json": "json"}[a.mime]
    safe = "".join(ch if ch.isalnum() else "-" for ch in p.title)[:60].strip("-") or "book"
    return Response(storage.read_asset(a), media_type=a.mime,
                    headers={"Content-Disposition": f'attachment; filename="{safe}-{e.kind}.{ext}"'})


# ================================================================== assets & uploads

@router.get("/assets/{aid}")
def get_asset(aid: str, db: Session = Depends(get_db)):
    a = db.get(Asset, aid)
    if a is None:
        raise HTTPException(404, "asset not found")
    return Response(storage.read_asset(a), media_type=a.mime,
                    headers={"ETag": a.checksum, "Cache-Control": "private, max-age=31536000, immutable"})


MAX_UPLOAD = 15 * 1024 * 1024


@router.post("/projects/{pid}/uploads", status_code=201)
async def upload_reference(pid: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    p = _project(db, pid)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (max 15 MB)")
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP"):
                raise HTTPException(415, "only PNG, JPEG or WEBP images are accepted")
            if im.width * im.height > 40_000_000:
                raise HTTPException(413, "image dimensions too large")
            fmt = im.format
    except (UnidentifiedImageError, OSError):
        raise HTTPException(415, "not a valid image")
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    a = storage.store_bytes(db, data, mime=mime, kind="reference", project_id=p.id)
    return {"asset_id": a.id, "url": S.asset_url(a.id), "width": a.width, "height": a.height}


@router.get("/kdp/trim-sizes")
def trim_sizes():
    return {"trim_sizes": list(kdp.TRIM_SIZES), "paper_types": ["white", "cream"],
            "spine_per_page": kdp.SPINE_PER_PAGE, "min_pages": kdp.MIN_PAGES}


@router.get("/research-reports/{rid}")
def research_report(rid: str, db: Session = Depends(get_db)):
    rr = db.get(ResearchReport, rid)
    if rr is None:
        raise HTTPException(404, "report not found")
    return {"id": rr.id, "topic": rr.topic, "mode": rr.mode, **rr.data}


@router.post("/pages/{page_id}/caption/regenerate")
def page_caption_regen(page_id: str, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    if pg.locked:
        raise HTTPException(423, "page is locked")
    p = db.get(Project, pg.project_id)
    services.refresh_captions(db, p, page_ids=[pg.id])
    return S.page(db, pg, brief=False)


@router.post("/pages/{page_id}/generate", status_code=202)
def page_generate(page_id: str, body: GenerateIn, db: Session = Depends(get_db)):
    pg = _page(db, page_id)
    p = db.get(Project, pg.project_id)
    if pg.locked:
        raise HTTPException(423, "page is locked")
    if pg.status in services.APPROVED_STATES and not body.allow_approved:
        raise HTTPException(409, "page is approved; pass allow_approved=true to create a new version for review")
    return _enqueue_generation(db, p, [pg], body.provider_id, body.model_id, allow_unknown=body.allow_unknown_cost,
                               allow_approved=body.allow_approved, extra=body.extra_prompt)


_ = generation  # imported for side effects/type references
