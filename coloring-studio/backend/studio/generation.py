"""Illustration generation pipeline for one page.

prompt (Style DNA) -> route -> budget check -> provider call with retry policy
-> store original revision -> enhancement revision -> objective QA -> commit
with optimistic concurrency (stale results never overwrite newer work or
locked/approved pages) -> caption refresh -> cost ledger.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import costs, routing, services, storage
from .imaging import enhance, quality
from .models import Asset, Page, Project, Revision, Section
from .providers import registry
from .providers.base import GenerationRequest, ProviderError
from .security import redact
from .style import compose_prompt

RETRY_BACKOFF = [1.0, 3.0, 8.0]
backoff_scale = 1.0  # tests set this to 0


class GenerationSkipped(Exception):
    pass


@dataclass
class Outcome:
    page_id: str
    status: str  # ok | stale | failed | skipped
    revision_id: str | None = None
    message: str = ""
    cost: float | None = None
    provider_id: str = ""
    model_id: str = ""
    attempts: list = field(default_factory=list)


def build_prompt(db: Session, project: Project, page: Page, extra: str = "") -> tuple[str, str, dict]:
    sp = services.style_of(db, project.id)
    section = db.get(Section, page.section_id) if page.section_id else None
    style = dict(sp.data)
    style["line_weight_px_adjust"] = style.get("line_weight_px_adjust", 0)
    instr = section.theme_instructions if section else ""
    prompt, neg = compose_prompt(subject=page.subject, scene=page.concept or page.subject,
                                 section_instructions=instr, style=style,
                                 complexity=section.complexity if section else None,
                                 excluded=section.excluded_content if section else "", extra=extra)
    return prompt, neg, style


def anchor_refs(db: Session, project_id: str, limit: int = 2) -> list[bytes]:
    sp = services.style_of(db, project_id)
    out = []
    for rid in (sp.anchor_revision_ids or [])[:limit]:
        rev = db.get(Revision, rid)
        if rev and rev.asset_id:
            a = db.get(Asset, rev.asset_id)
            if a:
                out.append(storage.read_asset(a))
    return out


def other_hashes(db: Session, project_id: str, exclude_page: str) -> dict[str, str]:
    out = {}
    q = select(Page).where(Page.project_id == project_id, Page.current_revision_id.is_not(None),
                           Page.id != exclude_page)
    for pg in db.scalars(q):
        rev = db.get(Revision, pg.current_revision_id)
        if rev and rev.quality.get("dhash"):
            out[pg.id] = rev.quality["dhash"]
    return out


def _call_with_retries(provider, req: GenerationRequest, max_retries: int, edit: bool, attempts: list):
    last: Exception | None = None
    for i in range(max_retries + 1):
        try:
            res = provider.edit_image(req) if edit else provider.generate_image(req)
            attempts.append({"provider": req.provider_id, "model": req.model_id, "ok": True})
            return res
        except ProviderError as e:
            msg = redact(str(e), [provider.api_key or ""])
            attempts.append({"provider": req.provider_id, "model": req.model_id, "ok": False, "error": msg,
                             "retryable": e.retryable})
            last = e
            if not e.retryable or i == max_retries:
                break
            time.sleep(RETRY_BACKOFF[min(i, len(RETRY_BACKOFF) - 1)] * backoff_scale)
    assert last is not None
    raise last


def store_and_qa(db: Session, project: Project, page: Page, res, *, kind: str, prompt: str, job_id: str | None,
                 battle_id: str | None = None, parent_id: str | None = None, line_weight_steps: int = 0) -> Revision:
    """Persist original + enhanced revisions and run QA; returns the revision to use."""
    box = services.art_box(project)
    tw, th = services.target_pixels(project)
    orig_asset = storage.store_bytes(db, res.image, mime=res.mime, kind="image", project_id=project.id)
    orig = Revision(project_id=project.id, page_id=page.id, kind=kind if kind != "candidate" else "candidate_original",
                    parent_revision_id=parent_id, asset_id=orig_asset.id, provider_id=res.provider_id,
                    model_id=res.model_id, effective_params=res.effective_params, prompt=prompt,
                    prompt_version=page.prompt_version, width=orig_asset.width, height=orig_asset.height,
                    provider_job_id=res.provider_job_id, cost=res.cost, cost_is_estimate=res.cost_is_estimate,
                    duration_seconds=res.duration_seconds, job_id=job_id, battle_id=battle_id)
    db.add(orig)
    db.flush()
    sp = services.style_of(db, project.id)
    hashes = other_hashes(db, project.id, page.id)
    orig.quality = quality.analyze(res.image, box_in=box, anchor=sp.anchor_metrics or None, other_hashes=hashes)
    chosen = orig
    if project.enhance_images:
        out, rep = enhance.enhance(res.image, target_px=(tw, th), line_weight_steps=line_weight_steps)
        if rep["detail_preservation"] >= 0.6:
            ea = storage.store_bytes(db, out, mime="image/png", kind="image", project_id=project.id)
            enh = Revision(project_id=project.id, page_id=page.id, kind="candidate" if kind == "candidate" else
                           "enhanced", parent_revision_id=orig.id, original_revision_id=orig.id, asset_id=ea.id,
                           provider_id=res.provider_id, model_id=res.model_id,
                           effective_params={**res.effective_params, "enhancement": rep}, prompt=prompt,
                           prompt_version=page.prompt_version, width=ea.width, height=ea.height,
                           cost=res.cost, cost_is_estimate=res.cost_is_estimate,
                           duration_seconds=res.duration_seconds, job_id=job_id, battle_id=battle_id)
            enh.quality = quality.analyze(out, box_in=box, native_px=(orig_asset.width, orig_asset.height),
                                          anchor=sp.anchor_metrics or None, other_hashes=hashes)
            enh.quality["enhancement"] = rep
            db.add(enh)
            db.flush()
            chosen = enh
        else:
            orig.quality["enhancement_rejected"] = (f"enhancement lost detail (preservation "
                                                    f"{rep['detail_preservation']}); original kept")
    return chosen


def generate_page(db: Session, project: Project, page: Page, *, job_id: str | None = None,
                  override: tuple[str, str] | None = None, expected_version: int | None = None,
                  extra_prompt: str = "", edit_from_current: bool = False, battle_id: str | None = None,
                  allow_unknown_cost: bool = False, allow_approved: bool = False) -> Outcome:
    if page.locked and battle_id is None:
        return Outcome(page.id, "skipped", message=f"page {page.position} is locked")
    if page.status in services.APPROVED_STATES and battle_id is None and not allow_approved:
        return Outcome(page.id, "skipped", message=f"page {page.position} is approved")
    start_version = expected_version if expected_version is not None else page.version
    prompt, neg, style = build_prompt(db, project, page, extra_prompt)
    tw, th = services.target_pixels(project)
    tried: set[str] = set()
    attempts: list = []
    choice = routing.choose(db, project, page, need_px=max(tw, th), override=override)
    init_image = None
    if edit_from_current and page.current_revision_id:
        cur = db.get(Revision, page.current_revision_id)
        src = db.get(Revision, cur.original_revision_id or cur.id) if cur else None
        if src and src.asset_id:
            init_image = storage.read_asset(db.get(Asset, src.asset_id))

    while True:
        provider = registry.get_provider(db, choice.provider_id)
        caps = provider.get_capabilities(choice.model_id)
        req = GenerationRequest(project_id=project.id, page_id=page.id, section_id=page.section_id,
                                provider_id=choice.provider_id, model_id=choice.model_id, prompt=prompt,
                                negative_prompt=neg, width=tw, height=th, style_guide=style,
                                reference_images=anchor_refs(db, project.id) if caps.reference_images else [],
                                init_image=init_image, seed=None, quality=None)
        est = provider.estimate_cost(req)
        costs.check_budget(db, project, est.amount, exclude_job=job_id, include_reserved=False,
                           allow_unknown=allow_unknown_cost)
        try:
            use_edit = init_image is not None and caps.image_edit
            res = _call_with_retries(provider, req, project.max_retries, use_edit, attempts)
            break
        except ProviderError as e:
            tried.add(choice.provider_id)
            if project.fallback_enabled and battle_id is None and override is None:
                ranked = [c for c in routing.rank(db, project, max(tw, th), exclude=tried, prefer_real=False)]
                if ranked:
                    choice = ranked[0]
                    choice.reason = f"fallback after {e.__class__.__name__}"
                    continue
            page = db.get(Page, page.id)
            if page.status == "generating":
                page.status = "failed" if not page.current_revision_id else "awaiting_review"
            msg = redact(str(e))
            return Outcome(page.id, "failed", message=msg, attempts=attempts)

    rev = store_and_qa(db, project, page, res, kind="candidate" if battle_id else "generated", prompt=prompt,
                       job_id=job_id, battle_id=battle_id, parent_id=page.current_revision_id,
                       line_weight_steps=page.line_weight_adjust)
    costs.record(db, project_id=project.id, provider_id=res.provider_id, model_id=res.model_id, amount=res.cost,
                 is_estimate=res.cost_is_estimate, operation="edit" if init_image else "generate", page_id=page.id,
                 section_id=page.section_id, job_id=job_id, is_retry=len(attempts) > 1)
    registry.get_config(db, res.provider_id).last_success_at = rev.created_at

    # Quality-first: one targeted regeneration if mandatory checks fail (bounded by retries + budget)
    if (project.generation_mode == "quality_first" and not rev.quality.get("passed") and battle_id is None
            and not extra_prompt.endswith("[qa-retry]") and project.max_retries > 0):
        try:
            return generate_page(db, project, page, job_id=job_id, override=(res.provider_id, res.model_id),
                                 expected_version=start_version,
                                 extra_prompt=(extra_prompt + " Use cleaner, thicker outlines and a pure white "
                                               "background. [qa-retry]").strip(),
                                 allow_unknown_cost=allow_unknown_cost, allow_approved=allow_approved)
        except costs.BudgetExceeded:
            pass

    if battle_id:
        return Outcome(page.id, "ok", rev.id, "candidate stored", res.cost, res.provider_id, res.model_id, attempts)

    db.refresh(page)
    if page.locked or page.version != start_version:
        rev.status = "stale"
        rev.error = "page changed while generating; kept as alternative revision"
        return Outcome(page.id, "stale", rev.id, rev.error, res.cost, res.provider_id, res.model_id, attempts)
    before = services.snapshot(page)
    subject_changed = page.caption_validated_revision_id is not None and before["prompt"] != prompt
    page.current_revision_id = rev.id
    page.prompt = prompt
    page.status = "awaiting_review"
    page.version += 1
    if not page.caption_text:
        services.refresh_captions(db, project, page_ids=[page.id])
    elif subject_changed and page.caption_status == "ok":
        page.caption_status = "review"  # artwork changed substantially -> revalidate caption
    services.log_edit(db, project.id, "generate", page=page, before=before, source="job",
                      details={"provider": res.provider_id, "model": res.model_id, "revision": rev.id,
                               "reason": choice.reason})
    return Outcome(page.id, "ok", rev.id, choice.reason, res.cost, res.provider_id, res.model_id, attempts)


def apply_line_weight(db: Session, project: Project, page: Page, steps: int, source: str = "ui") -> Revision:
    """Local edit: thicken/thin lines of the current artwork without calling any provider."""
    if page.locked:
        raise services.LockedError(f"page {page.position} is locked")
    if not page.current_revision_id:
        raise services.ConflictError(f"page {page.position} has no artwork yet")
    cur = db.get(Revision, page.current_revision_id)
    data = storage.read_asset(db.get(Asset, cur.asset_id))
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(data)).convert("L")
    im = enhance.adjust_line_weight(im, steps)
    out = enhance.to_png(im)
    asset = storage.store_bytes(db, out, mime="image/png", kind="image", project_id=project.id)
    rev = Revision(project_id=project.id, page_id=page.id, kind="edited", parent_revision_id=cur.id,
                   original_revision_id=cur.original_revision_id or cur.id, asset_id=asset.id,
                   provider_id="local", model_id="line-weight", effective_params={"line_weight_steps": steps},
                   prompt=cur.prompt, prompt_version=cur.prompt_version, width=asset.width, height=asset.height,
                   cost=0.0, cost_is_estimate=False)
    sp = services.style_of(db, project.id)
    orig_rev = db.get(Revision, cur.original_revision_id) if cur.original_revision_id else None
    rev.quality = quality.analyze(out, box_in=services.art_box(project),
                                  native_px=(orig_rev.width, orig_rev.height) if orig_rev else None,
                                  anchor=sp.anchor_metrics or None, other_hashes=other_hashes(db, project.id, page.id))
    db.add(rev)
    db.flush()
    before = services.snapshot(page)
    page.current_revision_id = rev.id
    page.line_weight_adjust += steps
    if page.status in services.APPROVED_STATES:
        page.status = "awaiting_review"
    page.version += 1
    services.log_edit(db, project.id, "line_weight", page=page, before=before, source=source,
                      details={"steps": steps, "revision": rev.id})
    return rev
