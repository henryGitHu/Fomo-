"""Provider/model selection: manual, smart auto-router and quality-first.

Uses measured history (QA scores, latency, failures from stored revisions and
benchmarks) when it exists; with no history a model gets a neutral prior and
the router says so in its explanation rather than inventing a ranking.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BenchmarkResult, Page, Project, Revision, Section
from .providers import registry
from .providers.base import ProviderError

WEIGHTS = {
    "lowest_cost": {"quality": 0.2, "cost": 0.6, "latency": 0.1, "resolution": 0.1},
    "balanced": {"quality": 0.45, "cost": 0.3, "latency": 0.1, "resolution": 0.15},
    "highest_quality": {"quality": 0.75, "cost": 0.05, "latency": 0.05, "resolution": 0.15},
}


@dataclass
class Choice:
    provider_id: str
    model_id: str
    reason: str
    score: float | None = None


def model_stats(db: Session) -> dict[tuple[str, str], dict]:
    stats: dict[tuple[str, str], dict] = {}
    for r in db.scalars(select(Revision).where(Revision.kind.in_(["generated", "candidate"]))):
        d = stats.setdefault((r.provider_id, r.model_id), {"q": [], "lat": [], "fail": 0, "n": 0, "style": []})
        d["n"] += 1
        if r.status == "failed":
            d["fail"] += 1
            continue
        sc = (r.quality or {}).get("scores", {})
        if sc:
            d["q"].append((sc.get("line_clarity", 0) + sc.get("coloring_suitability", 0) + sc.get("composition", 0)) / 3)
            if "style_consistency" in sc:
                d["style"].append(sc["style_consistency"])
        if r.duration_seconds is not None:
            d["lat"].append(r.duration_seconds)
    for b in db.scalars(select(BenchmarkResult)):
        d = stats.setdefault((b.provider_id, b.model_id), {"q": [], "lat": [], "fail": 0, "n": 0, "style": []})
        d["n"] += 1
        if not b.ok:
            d["fail"] += 1
            continue
        if b.scores:
            d["q"].append(b.scores.get("overall", 0))
        if b.latency_seconds is not None:
            d["lat"].append(b.latency_seconds)
    out = {}
    for k, d in stats.items():
        avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        out[k] = {"samples": d["n"], "avg_quality": avg(d["q"]), "avg_latency": avg(d["lat"]),
                  "failure_rate": d["fail"] / d["n"] if d["n"] else None, "avg_style": avg(d["style"])}
    return out


def candidates(db: Session, exclude: set[str] | None = None, need_edit: bool = False) -> list[tuple[str, object]]:
    out = []
    for pid in registry.all_provider_ids():
        if exclude and pid in exclude:
            continue
        if not registry.is_usable(db, pid):
            continue
        try:
            prov = registry.get_provider(db, pid)
            for caps in prov.list_models():
                if need_edit and not caps.image_edit:
                    continue
                out.append((pid, caps))
        except ProviderError:
            continue
    return out


def rank(db: Session, project: Project, need_px: int, *, exclude: set[str] | None = None,
         preference: str | None = None, prefer_real: bool = True) -> list[Choice]:
    pref = preference or project.routing_preference
    w = WEIGHTS.get(pref, WEIGHTS["balanced"])
    stats = model_stats(db)
    cands = candidates(db, exclude)
    real = [c for c in cands if not registry.PROVIDER_CLASSES[c[0]].is_mock]
    if prefer_real and real:
        cands = real
    prices = [c.price_per_image for _, c in cands if c.price_per_image is not None]
    max_price = max(prices) if prices else 1.0
    lats = [s["avg_latency"] for s in stats.values() if s["avg_latency"]]
    max_lat = max(lats) if lats else 1.0
    ranked = []
    for pid, caps in cands:
        st = stats.get((pid, caps.model_id), {})
        q = st.get("avg_quality")
        q_norm = (q / 100) if q is not None else 0.5
        if st.get("failure_rate"):
            q_norm *= (1 - st["failure_rate"])
        cost_norm = 1 - (caps.price_per_image / max_price) if (caps.price_per_image is not None and max_price) else 0.3
        lat = st.get("avg_latency")
        lat_norm = 1 - (lat / max_lat) if lat else 0.5
        res_norm = min(1.0, caps.max_side / max(need_px, 1))
        score = w["quality"] * q_norm + w["cost"] * cost_norm + w["latency"] * lat_norm + w["resolution"] * res_norm
        why = (f"quality {'%.0f' % q if q is not None else 'no history'}, "
               f"price {'$%.3f' % caps.price_per_image if caps.price_per_image is not None else 'unknown'}, "
               f"max side {caps.max_side}px")
        ranked.append(Choice(pid, caps.model_id, f"{pref} router: {why}", round(score, 4)))
    ranked.sort(key=lambda c: -c.score)
    return ranked


def choose(db: Session, project: Project, page: Page | None, *, need_px: int, override: tuple[str, str] | None = None,
           exclude: set[str] | None = None) -> Choice:
    if override and override[0]:
        prov = registry.get_provider(db, override[0])
        model = override[1] or (registry.get_config(db, override[0]).default_model or prov.list_models()[0].model_id)
        return Choice(override[0], model, "explicit request")
    section = db.get(Section, page.section_id) if page and page.section_id else None
    if page and page.provider_override:
        return Choice(page.provider_override, page.model_override or _default_model(db, page.provider_override),
                      "page override")
    if section and section.preferred_provider:
        return Choice(section.preferred_provider,
                      section.preferred_model or _default_model(db, section.preferred_provider), "section preference")
    if project.generation_mode in ("auto", "quality_first"):
        pref = "highest_quality" if project.generation_mode == "quality_first" else None
        ranked = rank(db, project, need_px, exclude=exclude, preference=pref)
        if ranked:
            return ranked[0]
    return Choice(project.default_provider, project.default_model or _default_model(db, project.default_provider),
                  "project default (manual mode)")


def _default_model(db: Session, provider_id: str) -> str:
    cfg = registry.get_config(db, provider_id)
    if cfg.default_model:
        return cfg.default_model
    return registry.get_provider(db, provider_id, require_enabled=False).list_models()[0].model_id
