"""AI Image Model Battle: same concept, several models, side-by-side, pick a winner.

Candidates are stored as revisions linked to the battle; nothing replaces the
page's current artwork until the user selects a winner (and, for an approved
page, explicitly confirms the replacement). Locked pages cannot be replaced.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import costs, generation, services
from .jobs import JobContext, enqueue
from .models import Battle, Page, Project, Revision
from .providers import registry
from .providers.base import GenerationRequest


def create(db: Session, project: Project, page: Page, contenders: list[dict], *,
           allow_unknown_cost: bool = False) -> Battle:
    if len(contenders) < 2:
        raise ValueError("a battle needs at least two provider/model contenders")
    tw, th = services.target_pixels(project)
    total = 0.0
    unknown = False
    for c in contenders:
        prov = registry.get_provider(db, c["provider_id"])
        model = c.get("model_id") or prov.list_models()[0].model_id
        c["model_id"] = model
        prov.get_capabilities(model)
        est = prov.estimate_cost(GenerationRequest(project.id, page.id, page.section_id, c["provider_id"], model, "",
                                                   width=tw, height=th))
        if est.amount is None:
            unknown = True
        else:
            total += est.amount
    costs.check_budget(db, project, None if unknown else total, allow_unknown=allow_unknown_cost)
    prompt, _, _ = generation.build_prompt(db, project, page)
    b = Battle(project_id=project.id, page_id=page.id, prompt=prompt, contenders=contenders, status="queued")
    db.add(b)
    db.flush()
    job = enqueue(db, project.id, "battle", {"battle_id": b.id, "allow_unknown_cost": allow_unknown_cost},
                  estimated_cost=None if unknown else total)
    b.job_id = job.id
    return b


def run(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    b = db.get(Battle, job.payload["battle_id"])
    project = db.get(Project, b.project_id)
    page = db.get(Page, b.page_id)
    b.status = "running"
    done = {(o["provider_id"], o["model_id"]) for o in (job.result or {}).get("outcomes", [])}
    for i, c in enumerate(b.contenders):
        if (c["provider_id"], c["model_id"]) in done:
            continue
        ctx.checkpoint()
        ctx.progress(i / len(b.contenders), f"{c['provider_id']}/{c['model_id']}")
        try:
            out = generation.generate_page(db, project, page, job_id=job.id,
                                           override=(c["provider_id"], c["model_id"]), battle_id=b.id,
                                           allow_unknown_cost=job.payload.get("allow_unknown_cost", False))
            res = {"provider_id": c["provider_id"], "model_id": c["model_id"], "status": out.status,
                   "revision_id": out.revision_id, "message": out.message}
        except (costs.BudgetExceeded, costs.UnknownCost) as e:
            res = {"provider_id": c["provider_id"], "model_id": c["model_id"], "status": "blocked",
                   "message": str(e)}
        ctx.add_result(outcomes=[res])
        db.commit()
    b.status = "awaiting_selection"


def candidates(db: Session, battle: Battle) -> list[dict]:
    out = []
    for r in db.scalars(select(Revision).where(Revision.battle_id == battle.id, Revision.kind == "candidate")):
        q = r.quality or {}
        out.append({"revision_id": r.id, "provider_id": r.provider_id, "model_id": r.model_id,
                    "image_url": f"/api/assets/{r.asset_id}" if r.asset_id else None,
                    "width": r.width, "height": r.height, "cost": r.cost, "cost_is_estimate": r.cost_is_estimate,
                    "duration_seconds": r.duration_seconds,
                    "objective_scores": q.get("scores", {}), "effective_ppi": q.get("effective_ppi"),
                    "native_ppi": q.get("native_ppi"), "qa_passed": q.get("passed"),
                    "subjective_evaluation": q.get("ai_evaluation") or "not run (no vision-capable evaluator configured)",
                    "is_winner": battle.winner_revision_id == r.id})
    return out


def select_winner(db: Session, battle: Battle, revision_id: str, *, confirm_replace_approved: bool = False,
                  approve: bool = False) -> Page:
    rev = db.get(Revision, revision_id)
    if rev is None or rev.battle_id != battle.id or rev.kind != "candidate":
        raise ValueError("revision is not a candidate of this battle")
    page = db.get(Page, battle.page_id)
    if page.locked:
        raise services.LockedError(f"page {page.position} is locked; unlock it before replacing its artwork")
    if page.status in services.APPROVED_STATES and not confirm_replace_approved:
        raise services.ConflictError("page is approved; confirm replacement explicitly")
    before = services.snapshot(page)
    page.current_revision_id = rev.id
    page.prompt = rev.prompt
    page.status = "approved" if approve else "awaiting_review"
    page.version += 1
    if page.caption_status == "ok":
        page.caption_status = "review"
    battle.winner_revision_id = rev.id
    battle.status = "decided"
    services.log_edit(db, battle.project_id, "battle_winner", page=page, before=before,
                      details={"battle": battle.id, "revision": rev.id, "provider": rev.provider_id,
                               "model": rev.model_id})
    return page
