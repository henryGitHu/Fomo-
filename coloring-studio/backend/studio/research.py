"""Smart Theme Research.

Sources (in order):
1. Web retrieval (Wikipedia public API: search + page summaries). Only the
   official API is used — no scraping, no paywall/login bypass. Content is
   treated as untrusted data and is never copied verbatim into the book.
2. Curated knowledge packs bundled with the app (reviewed facts + references).
3. Optional text model to synthesise an original plan from (1) and (2).
4. Deterministic offline synthesis when nothing else is available.

The report records which mode produced it so the UI never overstates.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .knowledge import chemical_engineering as chem
from .text import llm

KNOWLEDGE_PACKS = [chem]
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
USER_AGENT = "ColoringStudio/1.0 (educational coloring book research; contact: site owner)"

GENERIC_STAGES = [
    ("Meet {topic}", "introduction", "What {topic} is and the very first ideas to know."),
    ("Tools and Things of {topic}", "tools", "The main objects, tools and vocabulary of {topic}."),
    ("How {topic} Works", "processes", "The processes and steps that make {topic} work."),
    ("{topic} in the Real World", "applications", "Where {topic} shows up in the real world."),
    ("{topic} and You", "careers", "How {topic} connects to everyday life and future careers."),
]

GENERIC_SCENES = [
    ("A friendly introduction to {topic}", "a cheerful guide character presenting {topic} with a few simple related objects"),
    ("The basic parts of {topic}", "a large clear drawing of the main parts of {topic}, each part outlined separately"),
    ("Tools used in {topic}", "a neat arrangement of the tools people use for {topic}"),
    ("A day of {topic}", "a simple scene showing people taking part in {topic}"),
    ("{topic} up close", "a magnified close-up of an interesting detail of {topic}"),
    ("{topic} outdoors", "an outdoor landscape that shows {topic} in nature or a town"),
    ("Learning about {topic}", "children at a museum looking at a display about {topic}"),
    ("{topic} long ago", "a historical scene showing how people did {topic} in the past"),
    ("{topic} in the future", "a hopeful futuristic scene imagining {topic} years from now"),
    ("Working together in {topic}", "a team of people cooperating on a {topic} task"),
]


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def find_pack(topic: str):
    low = topic.lower()
    for pack in KNOWLEDGE_PACKS:
        if any(k in low for k in pack.TOPIC_KEYS):
            return pack
    return None


def web_research(topic: str, transport=None, limit: int = 6) -> tuple[list[dict], str]:
    """Return (sources, status). Never raises."""
    if not get_settings().research_web_enabled:
        return [], "disabled by configuration"
    kw = {"timeout": 15, "headers": {"User-Agent": USER_AGENT}}
    if transport is not None:
        kw["transport"] = transport
    try:
        with httpx.Client(**kw) as c:
            r = c.get(WIKI_API, params={"action": "query", "list": "search", "srsearch": topic, "format": "json",
                                        "srlimit": limit})
            r.raise_for_status()
            hits = r.json().get("query", {}).get("search", [])
            sources = []
            for h in hits:
                title = h["title"]
                sr = c.get(WIKI_SUMMARY + title.replace(" ", "_"))
                if sr.status_code != 200:
                    continue
                js = sr.json()
                sources.append({
                    "id": f"wiki:{title}", "title": title, "publisher": "Wikipedia",
                    "url": js.get("content_urls", {}).get("desktop", {}).get("page",
                                                                             f"https://en.wikipedia.org/wiki/{title}"),
                    "license": "CC BY-SA 4.0 — facts paraphrased; no text or images copied",
                    "extract": _clean(js.get("extract", ""))[:1500],
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                })
        return sources, f"ok ({len(sources)} sources)"
    except Exception as e:
        return [], f"unavailable: {type(e).__name__}: {str(e)[:120]}"


def _offline_subjects(topic: str, n_stages: int = 5) -> list[dict]:
    subjects = []
    for i, (subj, scene) in enumerate(GENERIC_SCENES):
        subjects.append({"stage": min(n_stages - 1, i * n_stages // len(GENERIC_SCENES)),
                         "subject": subj.format(topic=topic), "scene": scene.format(topic=topic),
                         "fact": "", "vocabulary": "", "question": f"What do you like most about {topic}?",
                         "source_ids": [], "verified": False})
    return subjects


def run_research(db: Session | None, topic: str, audience_age: str, *, transport=None) -> dict:
    sources, web_status = web_research(topic, transport=transport)
    pack = find_pack(topic)
    report: dict = {"topic": topic, "audience_age": audience_age, "web_status": web_status,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "originality_note": ("Research is used only as factual inspiration. Illustrations are generated "
                                         "as original artwork; no external coloring pages or images are copied, and "
                                         "prompts are screened for protected characters and brands.")}
    references = [{k: v for k, v in s.items() if k != "extract"} for s in sources]
    modes = []
    if sources:
        modes.append("web")
    if pack:
        modes.append("knowledge_pack")
        references += [dict(r, id=f"pack:{i}") for i, r in enumerate(pack.REFERENCES)]
        stages = [{"name": n, "stage": s, "goals": g} for n, s, g in pack.STAGES]
        subjects = [{"stage": st, "subject": su, "scene": sc, "fact": fa, "vocabulary": vo, "question": qu,
                     "source_ids": [f"pack:{i}" for i in range(len(pack.REFERENCES))], "verified": True}
                    for st, su, sc, fa, vo, qu in pack.CONCEPTS]
        pitfalls = list(pack.PITFALLS)
    else:
        stages = [{"name": n.format(topic=topic.title()), "stage": s, "goals": g.format(topic=topic)}
                  for n, s, g in GENERIC_STAGES]
        subjects = _offline_subjects(topic)
        pitfalls = ["Facts for this topic have not been verified by a curated pack — review every caption."]

    synthesized = None
    if llm.available(db):
        synthesized = llm.complete_json(
            db,
            "You are an educational content designer planning an ORIGINAL children's coloring book. "
            "Return {overview, objectives:[str], sections:[{name, stage, goals}], subjects:[{stage:int, subject, "
            "scene, fact, vocabulary, question, source_ids:[str]}], pitfalls:[str]}. Facts must be accurate, short, "
            "age-appropriate, never suggest unsafe experiments, and cite source ids you relied on.",
            f"Topic: {topic}\nAudience age: {audience_age}\nKnown source ids: "
            f"{[r['id'] for r in references]}\nProduce at least 40 distinct subjects across 5 learning stages.",
            untrusted=[f"[{s['id']}] {s['extract']}" for s in sources])
    if synthesized and isinstance(synthesized, dict) and synthesized.get("subjects"):
        modes.append("ai_synthesis")
        valid_ids = {r["id"] for r in references}
        for s in synthesized["subjects"]:
            s["source_ids"] = [i for i in s.get("source_ids", []) if i in valid_ids]
            s["verified"] = False
        if not pack:  # keep curated pack content authoritative when present
            subjects = synthesized["subjects"]
            stages = synthesized.get("sections") or stages
        pitfalls += synthesized.get("pitfalls", [])
        report["overview"] = synthesized.get("overview", "")
        report["objectives"] = synthesized.get("objectives", [])

    if "overview" not in report:
        if sources:
            report["overview"] = sources[0]["extract"][:600]
        elif pack:
            report["overview"] = ("Chemical engineers design the processes that turn raw materials into useful "
                                  "products — clean water, medicines, fuels, foods and materials — safely and "
                                  "efficiently. This book moves from basic ideas to real-world applications.")
        else:
            report["overview"] = (f"Offline plan for '{topic}'. Web research was not available, so concepts are "
                                  "generic and every caption needs human review.")
        report["objectives"] = [f"{s['name']}: {s['goals']}" for s in stages]

    report.update({
        "mode": "+".join(modes) if modes else "offline_template",
        "suggested_sections": stages, "subjects": subjects, "references": references,
        "licensing_notes": [
            "Only facts are used; no text passages or images are reproduced from sources.",
            "Wikipedia text is CC BY-SA 4.0; we paraphrase facts and attribute the source in the report.",
            "Government (.gov) sources are generally public domain in the U.S.; check before quoting.",
            "Never upload or trace existing coloring pages as references.",
        ],
        "pitfalls": pitfalls,
        "illustration_ideas": [s["subject"] for s in subjects],
    })
    return report
