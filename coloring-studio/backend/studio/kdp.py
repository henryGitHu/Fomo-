"""Amazon KDP paperback geometry.

Values follow KDP's published paperback guidance (help topics "Set Trim Size,
Bleed, and Margins" and "Create a Paperback Cover"), as known at the time of
writing. Re-verify against https://kdp.amazon.com/help before publishing;
local checks are a preflight, not a guarantee of KDP acceptance.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

BLEED_IN = 0.125
# Spine thickness per page (inches), by interior/paper type.
SPINE_PER_PAGE = {"white": 0.002252, "cream": 0.0025, "standard_color": 0.002252, "premium_color": 0.002347}
MIN_PAGES = 24
MAX_PAGES = {"white": 828, "cream": 776, "standard_color": 600, "premium_color": 828}
SPINE_TEXT_MIN_PAGES = 79  # KDP: spine text only for books with more than 79 pages
SPINE_TEXT_MARGIN_IN = 0.0625
BARCODE_W_IN, BARCODE_H_IN = 2.0, 1.2
COVER_SAFE_IN = 0.125  # keep text this far inside the trim (plus bleed)

TRIM_SIZES = {  # common KDP paperback trims (inches)
    "8.5x11": (8.5, 11.0), "8x10": (8.0, 10.0), "8.25x11": (8.25, 11.0), "8.5x8.5": (8.5, 8.5),
    "8.25x8.25": (8.25, 8.25), "7x10": (7.0, 10.0), "6x9": (6.0, 9.0),
}


def inside_margin(page_count: int) -> float:
    """Minimum gutter (inside) margin by page count."""
    if page_count <= 150:
        return 0.375
    if page_count <= 300:
        return 0.5
    if page_count <= 500:
        return 0.625
    if page_count <= 700:
        return 0.75
    return 0.875


def outside_margin(bleed: bool) -> float:
    return 0.375 if bleed else 0.25


@dataclass
class InteriorGeometry:
    trim_w: float
    trim_h: float
    bleed: bool
    page_w: float
    page_h: float
    inside: float
    outside: float
    top: float
    bottom: float
    page_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def interior_geometry(trim: str, bleed: bool, page_count: int, *, comfort_margin: float = 0.5) -> InteriorGeometry:
    """Page box and margins. `comfort_margin` is our design margin (≥ the KDP minimum)."""
    tw, th = TRIM_SIZES[trim]
    pw, ph = (tw + BLEED_IN, th + 2 * BLEED_IN) if bleed else (tw, th)
    ins = max(inside_margin(page_count), comfort_margin)
    out = max(outside_margin(bleed), comfort_margin)
    return InteriorGeometry(tw, th, bleed, pw, ph, ins, out, out, out, page_count)


def illustration_box(geo: InteriorGeometry, caption_height_in: float) -> tuple[float, float]:
    """Inches available for the artwork on a recto page (above the caption band)."""
    w = geo.page_w - geo.inside - geo.outside
    h = geo.page_h - geo.top - geo.bottom - caption_height_in
    return round(w, 4), round(h, 4)


@dataclass
class CoverGeometry:
    trim_w: float
    trim_h: float
    page_count: int
    paper_type: str
    spine_w: float
    full_w: float
    full_h: float
    spine_text_allowed: bool
    barcode_box: tuple[float, float, float, float]  # x, y, w, h (inches, from bottom-left of full cover)

    def to_dict(self) -> dict:
        return asdict(self)


def spine_width(page_count: int, paper_type: str) -> float:
    return round(page_count * SPINE_PER_PAGE[paper_type], 4)


def cover_geometry(trim: str, page_count: int, paper_type: str = "white") -> CoverGeometry:
    tw, th = TRIM_SIZES[trim]
    sw = spine_width(page_count, paper_type)
    full_w = BLEED_IN + tw + sw + tw + BLEED_IN
    full_h = BLEED_IN + th + BLEED_IN
    # Barcode area: bottom-right of back cover, inside safe zone.
    bx = BLEED_IN + tw - COVER_SAFE_IN - 0.125 - BARCODE_W_IN
    by = BLEED_IN + COVER_SAFE_IN + 0.125
    return CoverGeometry(tw, th, page_count, paper_type, sw, round(full_w, 4), round(full_h, 4),
                         page_count >= SPINE_TEXT_MIN_PAGES, (round(bx, 4), round(by, 4), BARCODE_W_IN, BARCODE_H_IN))


def interior_page_count(illustrations: int, intro_pages: int) -> int:
    """Each illustration is single-sided: recto artwork + blank verso.

    Intro pages are padded to an even count so every illustration lands on
    a recto (odd) page.
    """
    intro = intro_pages + (intro_pages % 2)
    return intro + 2 * illustrations


def validate_page_count(n: int, paper_type: str) -> list[str]:
    errs = []
    if n < MIN_PAGES:
        errs.append(f"KDP paperbacks need at least {MIN_PAGES} pages (have {n})")
    if n > MAX_PAGES[paper_type]:
        errs.append(f"{paper_type} paper allows at most {MAX_PAGES[paper_type]} pages (have {n})")
    return errs
