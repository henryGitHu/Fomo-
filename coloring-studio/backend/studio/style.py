"""Visual Style DNA: book-level style profile and prompt composition.

Prompts alone cannot guarantee identical style across providers; this module
(1) gives every provider the same explicit constraints, (2) passes approved
anchor images as references where the model supports them, and (3) the QA
step measures stroke weight / density against the anchor and flags drift.
"""
from __future__ import annotations

import re

AGE_DETAIL = {"3-5": "low", "4-8": "medium", "6-9": "medium", "8-12": "high", "10-14": "high", "adult": "high"}

DEFAULT_STYLE = {
    "art_style": "clean cartoon line art",
    "line_thickness": "bold, uniform outlines (about 3-4 pt at print size)",
    "outline": "closed black contours, no sketchy or broken lines",
    "proportions": "friendly, slightly rounded proportions; large heads for characters",
    "detail_density": "medium",
    "background_complexity": "simple background with a few supporting objects",
    "composition": "single clear focal subject, centered, fully inside the frame",
    "whitespace": "large open areas suitable for crayons and markers",
    "age_group": "6-9",
    "exclusions": ["text", "letters", "numbers", "watermark", "signature", "shading", "grayscale", "color fill",
                   "hatching", "photorealism"],
    "character_designs": [],
    "line_weight_px_adjust": 0,
}

# Names of protected characters/brands that must not appear in prompts (originality guard).
PROTECTED_TERMS = [
    "disney", "pixar", "mickey", "minnie", "elsa", "frozen", "pokemon", "pikachu", "marvel", "spider-man",
    "spiderman", "batman", "superman", "dc comics", "paw patrol", "peppa pig", "bluey", "sonic the hedgehog",
    "mario", "nintendo", "hello kitty", "barbie", "lego", "minecraft", "sesame street", "dora the explorer",
    "harry potter", "star wars", "shrek", "scooby", "snoopy", "garfield", "spongebob",
]


def default_style(project_style: str, age: str) -> dict:
    s = dict(DEFAULT_STYLE)
    s["exclusions"] = list(DEFAULT_STYLE["exclusions"])
    s["art_style"] = project_style or s["art_style"]
    s["age_group"] = age
    s["detail_density"] = AGE_DETAIL.get(age, "medium")
    if s["detail_density"] == "low":
        s["line_thickness"] = "extra bold, thick uniform outlines"
        s["background_complexity"] = "almost no background"
    return s


def originality_issues(text: str) -> list[str]:
    low = text.lower()
    hits = [t for t in PROTECTED_TERMS if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low)]
    if re.search(r"in the style of [A-Z][a-z]+ [A-Z][a-z]+", text):
        hits.append("named-artist style imitation")
    return hits


def compose_prompt(*, subject: str, scene: str, section_instructions: str, style: dict,
                   complexity: str | None = None, excluded: str = "", extra: str = "") -> tuple[str, str]:
    """Returns (prompt, negative_prompt)."""
    detail = complexity or style.get("detail_density", "medium")
    lines = [
        f"Black-and-white coloring book page for children aged {style.get('age_group', '6-9')}.",
        f"Subject: {subject}. Scene: {scene}.",
        f"Art style: {style.get('art_style')}; {style.get('line_thickness')}; {style.get('outline')}.",
        f"Proportions: {style.get('proportions')}. Detail level: {detail}. Background: {style.get('background_complexity')}.",
        f"Composition: {style.get('composition')}. {style.get('whitespace')}.",
        "Pure white background, crisp black outlines only, no shading, no gray tones, no color, no text or lettering, "
        "no watermark, no border frame. Original artwork.",
    ]
    if section_instructions:
        lines.append(f"Section guidance: {section_instructions}")
    chars = style.get("character_designs") or []
    if chars:
        lines.append("Recurring characters: " + "; ".join(str(c) for c in chars))
    if extra:
        lines.append(extra)
    neg = list(style.get("exclusions", []))
    if excluded:
        neg.extend([e.strip() for e in excluded.split(",") if e.strip()])
    prompt = " ".join(lines)
    issues = originality_issues(prompt)
    if issues:
        raise ValueError(f"prompt references protected or imitative content: {', '.join(issues)}")
    return prompt, ", ".join(dict.fromkeys(neg))
