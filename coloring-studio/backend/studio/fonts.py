"""Embedded TrueType fonts (KDP requires all fonts embedded in the PDF).

Uses DejaVu Sans if present on the system; set STUDIO_FONT_REGULAR /
STUDIO_FONT_BOLD to other .ttf files. Fonts are bundled into the PDF as
TrueType subsets by ReportLab.
"""
from __future__ import annotations

import os
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BODY_FONT = "StudioSans"
BOLD_FONT = "StudioSans-Bold"

_CANDIDATES = {
    BODY_FONT: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf",
                "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    BOLD_FONT: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
                "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}
_registered = False


def _find(name: str) -> str:
    env = os.environ.get("STUDIO_FONT_REGULAR" if name == BODY_FONT else "STUDIO_FONT_BOLD")
    for p in ([env] if env else []) + _CANDIDATES[name]:
        if p and Path(p).exists():
            return p
    raise RuntimeError(f"No TrueType font found for {name}; set STUDIO_FONT_REGULAR/STUDIO_FONT_BOLD")


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    for name in (BODY_FONT, BOLD_FONT):
        pdfmetrics.registerFont(TTFont(name, _find(name)))
    _registered = True
