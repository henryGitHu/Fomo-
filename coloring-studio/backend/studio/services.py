"""Project, section and page operations shared by the API, worker and chat editor."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import captions, kdp, planning, storage
from .models import (Asset, CaptionRevision, EditHistory, Page, Project, ResearchReport, Revision, Section,
                     StyleProfile, new_id)
from .style import default_style

CAPTION_BAND_IN = 1.1  # height reserved under each illustration for the caption
APPROVED_STATES = {"approved", "locked", "export_ready"}


class ConflictError(Exception):
    """Optimistic-concurrency or state conflict (HTTP 409)."""


class LockedError(ConflictError):
    pass


# ------------------------------------------------------------------ layout

def geometry(project: Project) -> kdp.InteriorGeometry:
    n = kdp.interior_page_count(project.illustration_count, project.intro_pages)
    return kdp.interior_geometry(project.trim_size, project.bleed, n)


def art_box(project: Project) -> tuple[float, float]:
    return kdp.illustration_box(geometry(project), CAPTION_BAND_IN)


def target_pixels(project: Project, ppi: int = 300) -> tuple[int, int]:
    w, h = art_box(project)
    return int(round(w * ppi)), int(round(h * ppi))


# ------------------------------------------------------------------ history

def snapshot(page: Page) -> dict:
    return {"current_revision_id": page.current_revision_id, "caption_text": page.caption_text,
            "caption_status": page.caption_status, "subject": page.subject, "concept": page.concept,
            "prompt": page.prompt, "prompt_version": page.prompt_version, "status": page.status,
            "locked": page.locked, "line_weight_adjust": page.line_weight_adjust,
            "provider_override": page.provider_override, "model_override": page.model_override,
            "fact": page.fact}


def log_edit(db: Session, project_id: str, action: str, *, page: Page | None = None, before: dict | None = None,
             details: dict | None = None, source: str = "ui") -> EditHistory:
    e = EditHistory(project_id=project_id, page_id=page.id if page else None, action=action, source=source,
                    details=details or {}, before=before or {}, after=snapshot(page) if page else {})
    db.add(e)
    db.flush()
    return e


def restore_snapshot(db: Session, page: Page, snap: dict) -> None:
    if page.locked and snap.get("locked") is not False:
        raise LockedError(f"page {page.position} is locked")
    for k, v in snap.items():
        setattr(page, k, v)
    page.version += 1


def undo_last(db: Session, project_id: str, page_id: str | None = None) -> EditHistory | None:
    q = select(EditHistory).where(EditHistory.project_id == project_id, EditHistory.undone.is_(False),
                                  EditHistory.page_id.is_not(None))
    if page_id:
        q = q.where(EditHistory.page_id == page_id)
    e = db.scalars(q.order_by(EditHistory.created_at.desc())).first()
    if e is None or not e.before:
        return None
    page = db.get(Page, e.page_id)
    before_now = snapshot(page)
    restore_snapshot(db, page, e.before)
    e.undone = True
    log_edit(db, project_id, "undo", page=page, before=before_now, details={"undid": e.id, "action": e.action})
    return e


# ------------------------------------------------------------------ projects

def apply_defaults(obj) -> None:
    """Populate column defaults immediately (SQLAlchemy only applies them on INSERT)."""
    for col in obj.__table__.columns:
        if getattr(obj, col.key) is None and col.default is not None:
            arg = col.default.arg
            if callable(arg):
                try:
                    val = arg()
                except TypeError:
                    val = arg(None)
            else:
                val = arg
            setattr(obj, col.key, val)


def create_project(db: Session, data: dict) -> Project:
    p = Project(**{k: v for k, v in data.items() if v is not None})
    apply_defaults(p)
    if p.trim_size not in kdp.TRIM_SIZES:
        raise ValueError(f"unsupported trim size {p.trim_size}")
    if p.illustration_count < 1 or p.illustration_count > 400:
        raise ValueError("illustration_count must be 1..400")
    p.caption_settings = captions.settings_for(p.caption_settings or {}, age=p.audience_age)
    p.caption_settings.setdefault("topic_terms", p.theme)
    db.add(p)
    db.flush()
    db.add(StyleProfile(project_id=p.id, name=f"{p.title} style", data=default_style(p.illustration_style,
                                                                                    p.audience_age)))
    db.flush()
    return p


def style_of(db: Session, project_id: str) -> StyleProfile:
    sp = db.scalars(select(StyleProfile).where(StyleProfile.project_id == project_id)).first()
    if sp is None:
        p = db.get(Project, project_id)
        sp = StyleProfile(project_id=project_id, name="Book style", data=default_style(p.illustration_style,
                                                                                       p.audience_age))
        db.add(sp)
        db.flush()
    return sp


def latest_research(db: Session, project_id: str) -> ResearchReport | None:
    return db.scalars(select(ResearchReport).where(ResearchReport.project_id == project_id)
                      .order_by(ResearchReport.created_at.desc())).first()


def renumber(db: Session, project: Project) -> None:
    db.flush()
    pos = 1
    secs = list(db.scalars(select(Section).where(Section.project_id == project.id).order_by(Section.position)))
    for si, s in enumerate(secs, start=1):
        s.position = si
        pages = list(db.scalars(select(Page).where(Page.section_id == s.id).order_by(Page.position)))
        for pg in pages:
            pg.position = pos
            pos += 1
    orphans = list(db.scalars(select(Page).where(Page.project_id == project.id, Page.section_id.is_(None))
                              .order_by(Page.position)))
    for pg in orphans:
        pg.position = pos
        pos += 1
    project.illustration_count = pos - 1
    db.flush()


def _new_page(project: Project, section: Section, spec: dict, position: int) -> Page:
    return Page(project_id=project.id, section_id=section.id, position=position, subject=spec["subject"],
                concept=spec.get("scene", ""), fact=spec.get("fact", ""), vocabulary=spec.get("vocabulary", ""),
                question=spec.get("question", ""), source_refs=spec.get("source_ids", []),
                concept_variant=spec.get("variant", 0), status="planned")


def apply_plan(db: Session, project: Project, plan: list[dict]) -> None:
    """Create sections/pages from a plan. Refuses to discard approved or locked work."""
    existing = list(db.scalars(select(Page).where(Page.project_id == project.id)))
    protected = [p for p in existing if p.locked or p.status in APPROVED_STATES or p.current_revision_id]
    if protected:
        raise ConflictError(f"{len(protected)} page(s) already have artwork; edit sections individually instead "
                            "of rebuilding the whole plan")
    for p in existing:
        db.delete(p)
    for s in db.scalars(select(Section).where(Section.project_id == project.id)):
        db.delete(s)
    db.flush()
    pos = 1
    for i, sec in enumerate(plan, start=1):
        s = Section(project_id=project.id, position=i, name=sec["name"], illustration_count=sec["illustration_count"],
                    learning_stage=sec.get("learning_stage", ""), educational_goals=sec.get("educational_goals", ""),
                    theme_instructions=sec.get("theme_instructions", ""))
        db.add(s)
        db.flush()
        for spec in sec["pages"]:
            db.add(_new_page(project, s, spec, pos))
            pos += 1
    project.illustration_count = pos - 1
    project.status = "planned"
    db.flush()
    refresh_captions(db, project, only_missing=True)


def plan_from_research(db: Session, project: Project, sections: int | None = None) -> list[dict]:
    rr = latest_research(db, project.id)
    if rr is None:
        raise ConflictError("run Smart Theme Research first")
    k = sections or planning.recommend_sections(project.illustration_count, project.education_progression)["recommended"]
    return planning.build_plan(project.illustration_count, k, rr.data, project.education_progression)


def set_section_count(db: Session, project: Project, section: Section, new_count: int) -> None:
    """Grow or shrink one section without touching any other section's pages."""
    if new_count < 1:
        raise ValueError("a section needs at least one illustration")
    pages = list(db.scalars(select(Page).where(Page.section_id == section.id).order_by(Page.position)))
    if new_count < len(pages):
        removable = [p for p in reversed(pages) if not p.locked and p.status not in APPROVED_STATES]
        need = len(pages) - new_count
        if len(removable) < need:
            raise ConflictError("cannot shrink section: remaining pages are approved or locked")
        for p in removable[:need]:
            db.delete(p)
    elif new_count > len(pages):
        rr = latest_research(db, project.id)
        used = {p.subject for p in db.scalars(select(Page).where(Page.project_id == project.id))}
        subjects = rr.data["subjects"] if rr else [{"subject": section.name, "scene": section.name, "stage": 0}]
        stage_idx = section.position - 1
        sec_info = {"stage_indices": [min(stage_idx, max(s.get("stage", 0) for s in subjects))]}
        extra = planning.subjects_for_section(subjects, sec_info, new_count - len(pages), used)
        last = pages[-1].position if pages else 0
        for i, spec in enumerate(extra):
            db.add(_new_page(project, section, spec, last + i + 1))
    section.illustration_count = new_count
    section.version += 1
    renumber(db, project)
    refresh_captions(db, project, only_missing=True)


def add_section(db: Session, project: Project, name: str, count: int, **kw) -> Section:
    pos = (max([s.position for s in project.sections], default=0)) + 1
    s = Section(project_id=project.id, position=pos, name=name, illustration_count=0, **kw)
    db.add(s)
    db.flush()
    set_section_count(db, project, s, count)
    return s


def remove_section(db: Session, project: Project, section: Section) -> None:
    pages = list(db.scalars(select(Page).where(Page.section_id == section.id)))
    if any(p.locked or p.status in APPROVED_STATES for p in pages):
        raise ConflictError("section contains approved or locked pages; unlock them first")
    for p in pages:
        db.delete(p)
    db.delete(section)
    renumber(db, project)


def duplicate_section(db: Session, project: Project, section: Section) -> Section:
    copy = add_section(db, project, f"{section.name} (copy)", section.illustration_count,
                       theme_instructions=section.theme_instructions, complexity=section.complexity,
                       educational_goals=section.educational_goals, excluded_content=section.excluded_content,
                       learning_stage=section.learning_stage, preferred_provider=section.preferred_provider,
                       preferred_model=section.preferred_model)
    return copy


def reorder_sections(db: Session, project: Project, ordered_ids: list[str]) -> None:
    secs = {s.id: s for s in db.scalars(select(Section).where(Section.project_id == project.id))}
    if set(ordered_ids) != set(secs):
        raise ValueError("ordered_ids must list every section exactly once")
    # keep page order within each section; move whole sections
    offset = 0
    for i, sid in enumerate(ordered_ids, start=1):
        secs[sid].position = i
        for pg in sorted(db.scalars(select(Page).where(Page.section_id == sid)), key=lambda p: p.position):
            offset += 1
            pg.position = 10_000 + offset
    renumber(db, project)


# ------------------------------------------------------------------ captions

def refresh_captions(db: Session, project: Project, *, only_missing: bool = False,
                     page_ids: list[str] | None = None) -> int:
    n = 0
    box_w = art_box(project)[0]
    secs = {s.id: s for s in db.scalars(select(Section).where(Section.project_id == project.id))}
    q = select(Page).where(Page.project_id == project.id)
    for page in db.scalars(q):
        if page_ids and page.id not in page_ids:
            continue
        if only_missing and page.caption_text:
            continue
        if page.locked:
            continue
        sec = secs.get(page.section_id)
        st = captions.settings_for(project.caption_settings, sec.caption_preferences if sec else None)
        set_caption(db, project, page, captions.compose(page, st), source="generated", box_w=box_w, settings=st)
        n += 1
    return n


def set_caption(db: Session, project: Project, page: Page, text: str, *, source: str = "user",
                box_w: float | None = None, settings: dict | None = None) -> dict:
    if page.locked:
        raise LockedError(f"page {page.position} is locked")
    st = dict(settings or captions.settings_for(project.caption_settings))
    st.setdefault("topic_terms", project.theme)
    res = captions.validate(text, page, st, box_w or art_box(project)[0])
    page.caption_text = text
    page.caption_version += 1
    page.caption_status = "ok" if res["passed"] else "invalid"
    if res["passed"] and any(c["status"] == "warn" for c in res["checks"]):
        page.caption_status = "review"
    page.caption_validated_revision_id = page.current_revision_id
    page.version += 1
    db.add(CaptionRevision(page_id=page.id, text=text, source=source))
    db.flush()
    return res


# ------------------------------------------------------------------ duplication / deletion

def duplicate_project(db: Session, project: Project) -> Project:
    cols = [c.key for c in Project.__table__.columns if c.key not in ("id", "created_at", "updated_at", "version")]
    data = {c: getattr(project, c) for c in cols}
    data["title"] = f"{project.title} (copy)"
    data["publication_approved"] = False
    data["status"] = "draft" if data["status"] == "archived" else data["status"]
    data["archived"] = False
    copy = Project(**data)
    db.add(copy)
    db.flush()
    sp = style_of(db, project.id)
    db.add(StyleProfile(project_id=copy.id, name=sp.name, data=dict(sp.data), anchor_metrics=dict(sp.anchor_metrics)))
    sec_map = {}
    for s in db.scalars(select(Section).where(Section.project_id == project.id)):
        scols = {c.key: getattr(s, c.key) for c in Section.__table__.columns if c.key not in ("id", "project_id")}
        ns = Section(project_id=copy.id, **scols)
        db.add(ns)
        db.flush()
        sec_map[s.id] = ns.id
    for pg in db.scalars(select(Page).where(Page.project_id == project.id)):
        pcols = {c.key: getattr(pg, c.key) for c in Page.__table__.columns
                 if c.key not in ("id", "project_id", "section_id", "current_revision_id", "updated_at")}
        npg = Page(project_id=copy.id, section_id=sec_map.get(pg.section_id), **pcols)
        db.add(npg)
        db.flush()
        if pg.current_revision_id:
            rev = db.get(Revision, pg.current_revision_id)
            asset = db.get(Asset, rev.asset_id) if rev and rev.asset_id else None
            if asset:
                data_b = storage.read_asset(asset)
                na = storage.store_bytes(db, data_b, mime=asset.mime, kind="image", project_id=copy.id)
                rcols = {c.key: getattr(rev, c.key) for c in Revision.__table__.columns
                         if c.key not in ("id", "project_id", "page_id", "asset_id", "created_at")}
                nr = Revision(id=new_id(), project_id=copy.id, page_id=npg.id, asset_id=na.id, **rcols)
                db.add(nr)
                db.flush()
                npg.current_revision_id = nr.id
    for rr in db.scalars(select(ResearchReport).where(ResearchReport.project_id == project.id)):
        db.add(ResearchReport(project_id=copy.id, topic=rr.topic, mode=rr.mode, data=rr.data))
    db.flush()
    return copy


def delete_project(db: Session, project: Project) -> None:
    for a in db.scalars(select(Asset).where(Asset.project_id == project.id)):
        try:
            storage.backend().delete(a.storage_key)
        except Exception:
            pass
    db.delete(project)
    db.flush()
