"""Provider benchmarking with a standard set of coloring-book prompts.

Results (objective QA scores, latency, cost) are stored and feed the smart
router. Runs only on explicitly selected contenders within a spending cap.
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from .imaging import enhance, quality
from .jobs import JobContext, enqueue
from .models import BenchmarkResult
from .providers import registry
from .providers.base import GenerationRequest, ProviderError
from .security import redact
from .style import compose_prompt, default_style

SUITE = [
    ("simple_cartoon", "a happy cartoon turtle", "a happy cartoon turtle sitting on a rock"),
    ("educational", "a water cycle diagram", "clouds, rain, a river and the sun showing the water cycle"),
    ("technical_machinery", "an industrial water pump", "a large industrial pump with a motor and pipes"),
    ("nature", "a forest scene", "tall trees, mushrooms and a deer in a forest"),
    ("character_consistency", "the same robot mascot twice", "a friendly round robot mascot waving"),
    ("print_readability", "bold simple shapes", "a big star, a heart and a circle with very bold outlines"),
    ("editing_fidelity", "add a hat to the turtle", "a happy cartoon turtle sitting on a rock"),
]
BOX = (7.5, 8.4)


def estimate(db: Session, contenders: list[dict]) -> float | None:
    total = 0.0
    for c in contenders:
        prov = registry.get_provider(db, c["provider_id"])
        est = prov.estimate_cost(GenerationRequest("", None, None, c["provider_id"], c["model_id"], ""))
        if est.amount is None:
            return None
        total += est.amount * (len(SUITE) + 1)  # +1: consistency needs two images
    return round(total, 4)


def start(db: Session, contenders: list[dict], max_spend: float) -> tuple[str, float | None]:
    for c in contenders:
        prov = registry.get_provider(db, c["provider_id"])
        c["model_id"] = c.get("model_id") or prov.list_models()[0].model_id
    est = estimate(db, contenders)
    if est is None:
        raise ValueError("cost of at least one contender is unknown; benchmark refused under a spending cap")
    if est > max_spend + 1e-9:
        raise ValueError(f"estimated benchmark cost ${est:.2f} exceeds max_spend ${max_spend:.2f}")
    job = enqueue(db, None, "benchmark", {"contenders": contenders, "max_spend": max_spend}, estimated_cost=est)
    return job.id, est


def run(ctx: JobContext) -> None:
    db, job = ctx.db, ctx.job
    style = default_style("clean cartoon line art", "6-9")
    contenders = job.payload["contenders"]
    spent = 0.0
    total_steps = len(contenders) * len(SUITE)
    step = 0
    for c in contenders:
        prov = registry.get_provider(db, c["provider_id"])
        caps = prov.get_capabilities(c["model_id"])
        for cat, subject, scene in SUITE:
            ctx.checkpoint()
            step += 1
            ctx.progress(step / total_steps, f"{c['provider_id']}/{c['model_id']}: {cat}")
            prompt, neg = compose_prompt(subject=subject, scene=scene, section_instructions="", style=style)
            req = GenerationRequest("benchmark", None, None, c["provider_id"], c["model_id"], prompt,
                                    negative_prompt=neg, width=1536, height=2048, style_guide=style, seed=7)
            br = BenchmarkResult(provider_id=c["provider_id"], model_id=c["model_id"], category=cat, prompt=prompt,
                                 job_id=job.id)
            t0 = time.monotonic()
            try:
                res = prov.generate_image(req)
                spent += res.cost or 0.0
                q = quality.analyze(res.image, box_in=BOX)
                sc = dict(q["scores"])
                if cat == "character_consistency":
                    res2 = prov.generate_image(GenerationRequest(**{**req.__dict__, "seed": 8}))
                    spent += res2.cost or 0.0
                    q2 = quality.analyze(res2.image, box_in=BOX)
                    sc["style_consistency"] = quality.style_consistency(q2, quality.anchor_from([q]))
                if cat == "editing_fidelity":
                    if caps.image_edit:
                        er = prov.edit_image(GenerationRequest(**{**req.__dict__, "prompt": "Add a small hat. " + prompt,
                                                                  "init_image": res.image}))
                        spent += er.cost or 0.0
                        sc["edit_preservation"] = round(100 * enhance.detail_preservation(
                            quality.load_gray(res.image), quality.load_gray(er.image)), 1)
                    else:
                        sc["edit_preservation"] = None
                vals = [v for v in sc.values() if isinstance(v, (int, float))]
                sc["overall"] = round(sum(vals) / len(vals), 1) if vals else 0
                br.scores, br.cost, br.ok = sc, res.cost, True
            except ProviderError as e:
                br.ok, br.error = False, redact(str(e))
            br.latency_seconds = round(time.monotonic() - t0, 3)
            db.add(br)
            ctx.add_result(spent=spent)
            db.commit()
            if spent > job.payload["max_spend"]:
                ctx.add_result(stopped_reason="max_spend reached")
                return
