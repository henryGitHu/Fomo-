"""Learn While You Color caption engine (generation + validation)."""
from __future__ import annotations

import re

from reportlab.pdfbase.pdfmetrics import stringWidth

from .fonts import BODY_FONT, register_fonts

DEFAULTS = {"enabled": True, "reading_level": "early", "length": "short", "tone": "cheerful",
            "fun_fact": True, "vocabulary": False, "explanation": True, "question": False,
            "font_size": 13, "max_lines": 3}
LENGTH_CHARS = {"short": 170, "medium": 260, "long": 360}
GRADE_LIMIT = {"early": 5.0, "intermediate": 8.0, "advanced": 11.0}
AGE_READING = {"3-5": "early", "4-8": "early", "6-9": "intermediate", "8-12": "advanced", "10-14": "advanced",
               "adult": "advanced"}


def settings_for(project_settings: dict | None, section_prefs: dict | None = None, age: str | None = None) -> dict:
    s = dict(DEFAULTS)
    if age:
        s["reading_level"] = AGE_READING.get(age, "intermediate")
    s.update(project_settings or {})
    s.update(section_prefs or {})
    return s


def compose(page, settings: dict) -> str:
    """Deterministic caption from the page's planned educational content.

    Pages that revisit a concept (variant > 0) lead with the question and
    vocabulary instead of repeating the same definition.
    """
    fact = (page.fact or "").strip()
    vocab = (page.vocabulary or "").strip()
    q = (page.question or "").strip()
    parts: list[str] = []
    if getattr(page, "concept_variant", 0) and q:
        parts.append(q)
        if vocab:
            parts.append(f"Word to know: {vocab}.")
    elif fact:
        parts.append(fact if settings.get("explanation", True) or not settings.get("fun_fact", True)
                     else "Fun fact: " + fact)
    if settings.get("vocabulary") and vocab and not any(p.startswith("Word to know") for p in parts):
        parts.append(f"New word: {vocab}.")
    if settings.get("question") and q and q not in parts:
        parts.append(q)
    if not parts:
        parts.append(f"Color the picture of {page.subject.lower()}!")
    text = " ".join(parts)
    limit = LENGTH_CHARS.get(settings.get("length", "short"), 170)
    return _trim_sentences(text, limit) if len(text) > limit else text


def _trim_sentences(text: str, limit: int) -> str:
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sent) + 1 > limit:
            break
        out = (out + " " + sent).strip()
    return out or text[:limit].rsplit(" ", 1)[0] + "…"


def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and n > 1 and not w.endswith("le"):
        n -= 1
    return max(1, n)


def fk_grade(text: str, taught_words: set[str] | None = None) -> float:
    """Flesch-Kincaid grade. Deliberately taught topic words count as one syllable."""
    taught = taught_words or set()
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return 0.0
    syl = sum(1 if (w.lower() in taught or w.lower().rstrip("s") in taught) else syllables(w) for w in words)
    return round(0.39 * len(words) / sentences + 11.8 * syl / len(words) - 15.59, 1)


def wrap_lines(text: str, width_pt: float, font: str, size: float) -> list[str]:
    register_fonts()
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if stringWidth(trial, font, size) <= width_pt:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


STOP = set("a an the of and or to in on for with is are be it this that by at as from its their they you your "
           "can how what why which into out up down".split())


def keywords(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        if w not in STOP and len(w) > 2:
            out.add(w)
            if w.endswith("s") and len(w) > 3:
                out.add(w[:-1])
    return out


def validate(text: str, page, settings: dict, box_width_in: float) -> dict:
    checks = []
    size = settings.get("font_size", 13)
    lines = wrap_lines(text, box_width_in * 72, BODY_FONT, size)
    fits = len(lines) <= settings.get("max_lines", 3)
    checks.append({"id": "fits_caption_area", "status": "pass" if fits else "fail",
                   "message": f"{len(lines)} line(s) at {size}pt (max {settings.get('max_lines', 3)})",
                   "mandatory": True})
    checks.append({"id": "legible_size", "status": "pass" if size >= 11 else "fail",
                   "message": f"{size}pt font (≥11pt recommended for children)", "mandatory": True})
    taught = keywords(f"{page.subject} {page.vocabulary} {settings.get('topic_terms', '')}")
    grade = fk_grade(text, taught)
    limit = GRADE_LIMIT.get(settings.get("reading_level", "early"), 6.5)
    checks.append({"id": "reading_level", "status": "pass" if grade <= limit else "warn",
                   "message": f"Flesch-Kincaid grade {grade} (target ≤ {limit})", "mandatory": False})
    rel = keywords(text) & keywords(f"{page.subject} {page.concept} {page.vocabulary}")
    checks.append({"id": "relevance", "status": "pass" if rel else "warn",
                   "message": "Mentions: " + ", ".join(sorted(rel)[:5]) if rel else "Caption shares no keywords with the scene",
                   "mandatory": False})
    unsafe = re.search(r"\b(try this at home|mix (?:the )?chemicals|light a fire|drink it|taste it)\b", text, re.I)
    checks.append({"id": "safety", "status": "fail" if unsafe else "pass",
                   "message": "Suggests an unsafe activity" if unsafe else "No unsafe suggestions", "mandatory": True})
    verified = bool(page.source_refs)
    checks.append({"id": "fact_source", "status": "pass" if verified else "warn",
                   "message": "Fact backed by referenced source(s)" if verified else
                   "No source attached — review the fact manually", "mandatory": False})
    return {"lines": lines, "grade": grade, "checks": checks,
            "passed": all(c["status"] != "fail" for c in checks if c["mandatory"])}
