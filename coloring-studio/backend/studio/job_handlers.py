"""Job handlers: batch generation, battles, research, benchmarks, edits, exports."""
from __future__ import annotations

from . import battle as battle_mod
from . import benchmark as bench_mod
from . import costs, generation, pdf_export, research, services
from .jobs import JobContext, handler
from .models import Page, Project, ResearchReport


@handler("generate_batch")
def generate_batch(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    project = db.get(Project, job.project_id)
    ids = job.payload.get("page_ids", [])
    done = set((job.result or {}).get("done", []))
    override = tuple(job.payload["override"]) if job.payload.get("override") else None
    for i, pid in enumerate(ids):
        if pid in done:
            continue  # resumed job: never redo finished pages
        ctx.checkpoint()
        page = db.get(Page, pid)
        if page is None:
            ctx.add_result(done=[pid], outcomes=[{"page_id": pid, "status": "skipped", "message": "page deleted"}])
            continue
        prev_status = page.status
        if not page.locked:
            page.status = "generating"
        ctx.progress(i / max(len(ids), 1), f"Generating page {page.position}")
        expected = page.version
        try:
            out = generation.generate_page(
                db, project, page, job_id=job.id, override=override, expected_version=expected,
                extra_prompt=job.payload.get("extra_prompt", ""),
                edit_from_current=job.payload.get("edit_from_current", False),
                allow_unknown_cost=job.payload.get("allow_unknown_cost", False),
                allow_approved=job.payload.get("allow_approved", False))
        except (costs.BudgetExceeded, costs.UnknownCost) as e:
            page.status = prev_status if prev_status not in ("queued", "generating") else (
                "awaiting_review" if page.current_revision_id else "planned")
            ctx.add_result(outcomes=[{"page_id": pid, "position": page.position, "status": "blocked",
                                      "message": str(e)}], stopped_reason=str(e))
            db.commit()
            # release the rest of the batch and stop
            for rest in ids[i + 1:]:
                pg = db.get(Page, rest)
                if pg and pg.status == "queued":
                    pg.status = "awaiting_review" if pg.current_revision_id else "planned"
            db.commit()
            return
        if out.status == "skipped" and page.status == "generating":
            page.status = prev_status if prev_status not in ("queued", "generating") else "planned"
        ctx.add_result(done=[pid], outcomes=[{"page_id": pid, "position": page.position, "status": out.status,
                                              "message": out.message, "provider": out.provider_id,
                                              "model": out.model_id, "cost": out.cost,
                                              "attempts": out.attempts}])
        db.commit()
    ctx.progress(1.0, "Batch complete")
    if project.status in ("draft", "planned"):
        project.status = "in_progress"


@handler("line_weight")
def line_weight(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    project = db.get(Project, job.project_id)
    steps = int(job.payload.get("steps", 1))
    for pid in job.payload.get("page_ids", []):
        if pid in set((job.result or {}).get("done", [])):
            continue
        page = db.get(Page, pid)
        try:
            generation.apply_line_weight(db, project, page, steps, source=job.payload.get("source", "job"))
            ctx.add_result(done=[pid], outcomes=[{"page_id": pid, "position": page.position, "status": "ok"}])
        except services.ConflictError as e:
            ctx.add_result(done=[pid], outcomes=[{"page_id": pid, "position": page.position, "status": "skipped",
                                                  "message": str(e)}])
        db.commit()


@handler("battle")
def run_battle(ctx: JobContext) -> None:
    battle_mod.run(ctx)


@handler("benchmark")
def run_benchmark(ctx: JobContext) -> None:
    bench_mod.run(ctx)


@handler("research")
def run_research(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    project = db.get(Project, job.project_id)
    ctx.progress(0.1, "Researching theme")
    data = research.run_research(db, job.payload.get("topic") or project.theme or project.title, project.audience_age)
    rr = ResearchReport(project_id=project.id, topic=data["topic"], mode=data["mode"], data=data)
    db.add(rr)
    db.flush()
    ctx.add_result(report_id=rr.id, mode=data["mode"], web_status=data["web_status"])


@handler("export")
def run_export(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    project = db.get(Project, job.project_id)
    ctx.progress(0.05, "Validating book")
    out = pdf_export.export(db, project, kinds=job.payload.get("kinds"), final=job.payload.get("final", True),
                            progress=ctx.progress)
    ctx.add_result(**out)
