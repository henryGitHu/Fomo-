"""KDP interior + full-wrap cover PDF generation, inspection and packaging."""
from __future__ import annotations

import io
import json
import random
import zipfile
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.lib.colors import Color, black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import captions, kdp, qa_book, services, storage
from .fonts import BODY_FONT, BOLD_FONT, register_fonts
from .models import Asset, ExportRecord, Page, Project, Revision, Section

PT = 72.0


class ExportBlocked(Exception):
    def __init__(self, report: dict):
        super().__init__("export blocked by mandatory QA failures")
        self.report = report


def _page_image(db: Session, page: Page) -> bytes | None:
    if not page.current_revision_id:
        return None
    rev = db.get(Revision, page.current_revision_id)
    if not rev or not rev.asset_id:
        return None
    return storage.read_asset(db.get(Asset, rev.asset_id))


def _wrap(c: canvas.Canvas, text: str, x: float, y: float, width: float, font: str, size: float,
          leading: float | None = None, center: bool = False) -> float:
    leading = leading or size * 1.3
    for line in captions.wrap_lines(text, width, font, size):
        c.setFont(font, size)
        if center:
            c.drawCentredString(x + width / 2, y, line)
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


# ------------------------------------------------------------------ interior

def build_interior(db: Session, project: Project, *, preview: bool = False) -> tuple[bytes, dict]:
    register_fonts()
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.position)))
    sections = list(db.scalars(select(Section).where(Section.project_id == project.id).order_by(Section.position)))
    geo = services.geometry(project)
    W, H = geo.page_w * PT, geo.page_h * PT
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H), pageCompression=1, initialFontName=BODY_FONT, initialFontSize=12)
    c.setTitle(project.title)
    c.setAuthor(project.author or "")
    c.setCreator("Coloring Studio")
    intro = project.intro_pages + (project.intro_pages % 2)
    bleed_off = kdp.BLEED_IN * PT if geo.bleed else 0.0
    layout: list[dict] = []
    pno = 0

    def margins(page_no: int) -> tuple[float, float]:
        """(left, right) in points; gutter is on the left of recto (odd) pages."""
        ins, outs = geo.inside * PT, geo.outside * PT
        return (ins, outs) if page_no % 2 == 1 else (outs, ins)

    def new_page(kind: str, **kw):
        nonlocal pno
        pno += 1
        layout.append({"page": pno, "kind": kind, **kw})

    # 1: title
    new_page("title")
    l, r = margins(pno)
    cw = W - l - r
    c.setFont(BOLD_FONT, 34)
    y = _wrap(c, project.title, l, H * 0.62, cw, BOLD_FONT, 34, center=True)
    if project.subtitle:
        _wrap(c, project.subtitle, l, y - 14, cw, BODY_FONT, 18, center=True)
    if project.author:
        c.setFont(BODY_FONT, 16)
        c.drawCentredString(l + cw / 2, H * 0.2, project.author)
    c.showPage()
    # 2: copyright
    new_page("copyright")
    l, r = margins(pno)
    year = datetime.now().year
    txt = (f"{project.title}. Copyright © {year} {project.author or 'the author'}. All rights reserved. "
           "No part of this book may be reproduced without permission, except for personal coloring use. "
           "Illustrations are original artwork created with the assistance of AI image-generation tools and "
           "reviewed by the publisher. Educational facts were checked against the sources listed by the publisher; "
           "this book does not suggest experiments for children to perform.")
    _wrap(c, txt, l, H * 0.35, W - l - r, BODY_FONT, 10)
    c.showPage()
    # 3: this book belongs to / how to use
    new_page("belongs_to")
    l, r = margins(pno)
    cw = W - l - r
    c.setFont(BOLD_FONT, 26)
    c.drawCentredString(l + cw / 2, H * 0.7, "This book belongs to")
    c.setLineWidth(1.5)
    c.line(l + cw * 0.15, H * 0.6, l + cw * 0.85, H * 0.6)
    _wrap(c, "Color each picture, then read the caption underneath to learn something new! Tip: slip a sheet of "
             "paper behind the page you are coloring if you use markers.", l + cw * 0.1, H * 0.45, cw * 0.8,
          BODY_FONT, 14, center=True)
    c.showPage()
    # 4: contents
    new_page("contents")
    l, r = margins(pno)
    cw = W - l - r
    c.setFont(BOLD_FONT, 24)
    c.drawString(l, H - geo.top * PT - 30, "Contents")
    y = H - geo.top * PT - 70
    for s in sections:
        first = next((p for p in pages if p.section_id == s.id), None)
        if not first:
            continue
        recto_no = intro + 2 * (first.position - 1) + 1
        c.setFont(BODY_FONT, 13)
        c.drawString(l, y, f"{s.position}. {s.name}"[:80])
        c.drawRightString(l + cw, y, str(recto_no))
        y -= 22
        if y < geo.bottom * PT + 20:
            break
    c.showPage()
    for _ in range(intro - 4):
        new_page("blank_intro")
        c.showPage()

    box_w, box_h = services.art_box(project)
    cap_settings = captions.settings_for(project.caption_settings)
    for page in pages:
        # recto: artwork + caption
        new_page("illustration", page_id=page.id, position=page.position)
        assert pno % 2 == 1, "illustrations must start on recto pages"
        l, r = margins(pno)
        img = _page_image(db, page)
        x0, ytop = l, H - geo.top * PT
        if img:
            ir = Image.open(io.BytesIO(img)).convert("L")
            if preview:
                ir.thumbnail((900, 900))
            s = min(box_w * PT / ir.width, box_h * PT / ir.height)
            dw, dh = ir.width * s, ir.height * s
            dx = x0 + (box_w * PT - dw) / 2
            dy = ytop - dh - (box_h * PT - dh) / 2
            c.drawImage(ImageReader(ir), dx, dy, dw, dh)
            layout[-1]["image_box_in"] = [round(dx / PT, 3), round(dy / PT, 3), round(dw / PT, 3), round(dh / PT, 3)]
            layout[-1]["image_ppi"] = round(ir.width / (dw / PT), 1)
        elif preview:
            c.setFont(BODY_FONT, 14)
            c.drawCentredString(W / 2, H / 2, f"[Page {page.position}: {page.subject} — not generated yet]")
        if cap_settings.get("enabled", True) and page.caption_text:
            size = cap_settings.get("font_size", 13)
            cap_top = geo.bottom * PT + services.CAPTION_BAND_IN * PT - size
            _wrap(c, page.caption_text, x0, cap_top, box_w * PT, BODY_FONT, size, center=True)
            layout[-1]["caption_lines"] = len(captions.wrap_lines(page.caption_text, box_w * PT, BODY_FONT, size))
        c.setFont(BODY_FONT, 9)
        c.drawCentredString(W / 2, max(geo.bottom * PT / 2, 12 + bleed_off), str(pno))
        if preview:
            c.setFillColor(Color(0.85, 0.1, 0.1))
            c.setFont(BOLD_FONT, 9)
            c.drawString(8 + bleed_off, 8 + bleed_off, "PREVIEW")
            c.setFillColor(black)
        c.showPage()
        # verso: intentionally blank (single-sided coloring pages prevent bleed-through)
        new_page("blank_verso", position=page.position)
        c.showPage()
    c.save()
    return buf.getvalue(), {"layout": layout, "page_count": pno, "geometry": geo.to_dict()}


# ------------------------------------------------------------------ cover

def colorize(line_art: bytes, seed: int = 3, max_side: int = 900) -> Image.Image:
    """Full-color cover art from approved line art by flood-filling closed regions."""
    im = Image.open(io.BytesIO(line_art)).convert("L")
    im.thumbnail((max_side, max_side))
    bw = im.point(lambda v: 0 if v < 128 else 255).convert("RGB")
    rng = random.Random(seed)
    palette = [(255, 196, 61), (94, 197, 236), (255, 120, 120), (126, 217, 87), (186, 140, 255), (255, 160, 90)]
    arr = np.asarray(bw)
    h, w = arr.shape[:2]
    ImageDraw.floodfill(bw, (1, 1), (235, 246, 255), thresh=40)
    step = max(8, min(w, h) // 30)
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            if bw.getpixel((x, y)) == (255, 255, 255):
                ImageDraw.floodfill(bw, (x, y), rng.choice(palette), thresh=40)
    return bw


def build_cover(db: Session, project: Project, interior_pages: int) -> tuple[bytes, dict]:
    register_fonts()
    g = kdp.cover_geometry(project.trim_size, interior_pages, project.paper_type)
    W, H = g.full_w * PT, g.full_h * PT
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H), pageCompression=1, initialFontName=BODY_FONT, initialFontSize=12)
    c.setTitle(f"{project.title} — cover")
    b = kdp.BLEED_IN * PT
    safe = kdp.COVER_SAFE_IN * PT
    back_x0, spine_x0 = 0, b + g.trim_w * PT
    front_x0 = spine_x0 + g.spine_w * PT
    settings = project.cover_settings or {}
    bg = settings.get("background", [0.16, 0.38, 0.72])
    c.setFillColor(Color(*bg))
    c.rect(0, 0, W, H, stroke=0, fill=1)  # background extends into bleed

    pages = list(db.scalars(select(Page).where(Page.project_id == project.id, Page.current_revision_id.is_not(None))
                            .order_by(Page.position)))
    feature_id = settings.get("feature_page_id")
    feature = next((p for p in pages if p.id == feature_id), None) or (pages[0] if pages else None)
    # front cover
    fx, fw = front_x0, g.trim_w * PT
    c.setFillColor(white)
    c.roundRect(fx + safe + 18, H * 0.22, fw - 2 * safe - 36, H * 0.52, 18, stroke=0, fill=1)
    if feature:
        art = colorize(_page_image(db, feature))
        aw, ah = fw - 2 * safe - 60, H * 0.52 - 24
        s = min(aw / art.width, ah / art.height)
        c.drawImage(ImageReader(art), fx + (fw - art.width * s) / 2, H * 0.22 + 12 + (ah - art.height * s) / 2,
                    art.width * s, art.height * s)
    c.setFillColor(white)
    y = _wrap(c, project.title, fx + safe + 24, H - b - safe - 60, fw - 2 * safe - 48, BOLD_FONT, 36, center=True)
    if project.subtitle:
        _wrap(c, project.subtitle, fx + safe + 24, y - 6, fw - 2 * safe - 48, BODY_FONT, 16, center=True)
    if project.author:
        c.setFont(BOLD_FONT, 16)
        c.drawCentredString(fx + fw / 2, b + safe + 40, project.author)
    # spine (text only when KDP allows it)
    if g.spine_text_allowed:
        c.saveState()
        c.translate(spine_x0 + g.spine_w * PT / 2, H / 2)
        c.rotate(-90)
        size = min(14, g.spine_w * PT - 2 * kdp.SPINE_TEXT_MARGIN_IN * PT - 2)
        c.setFont(BOLD_FONT, max(6, size))
        c.setFillColor(white)
        c.drawCentredString(0, -size / 3, f"{project.title}  ·  {project.author}"[:90])
        c.restoreState()
    # back cover
    bx0, bw = back_x0 + b, g.trim_w * PT
    c.setFillColor(white)
    desc = project.back_description or (f"Color and learn! {project.illustration_count} original illustrations "
                                        f"with fun educational captions about {project.theme or project.title}.")
    _wrap(c, desc, bx0 + safe + 30, H - b - safe - 60, bw - 2 * safe - 60, BODY_FONT, 14)
    thumbs = pages[1:5]
    for i, p in enumerate(thumbs):
        img = Image.open(io.BytesIO(_page_image(db, p))).convert("L")
        img.thumbnail((500, 500))
        tw = (bw - 2 * safe - 80) / 2
        s = min(tw / img.width, (H * 0.2) / img.height)
        x = bx0 + safe + 30 + (i % 2) * (tw + 20)
        yy = H * 0.52 - (i // 2) * (H * 0.2 + 16)
        c.setFillColor(white)
        c.rect(x - 4, yy - 4, img.width * s + 8, img.height * s + 8, stroke=0, fill=1)
        c.drawImage(ImageReader(img), x, yy, img.width * s, img.height * s)
    # barcode-safe area (left empty/white for KDP's barcode)
    x, y, w_, h_ = g.barcode_box
    c.setFillColor(white)
    c.rect(x * PT, y * PT, w_ * PT, h_ * PT, stroke=0, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue(), g.to_dict()


# ------------------------------------------------------------------ inspection

def _no_marks(raw: bytes) -> bool:
    """True when a content stream paints nothing (only state ops such as an identity `cm`, q, Q)."""
    import re as _re
    stripped = _re.sub(rb"1 0 0 1 0 0 cm|\bq\b|\bQ\b|\s+", b"", raw)
    return stripped == b""


def inspect_pdf(data: bytes) -> dict:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    fonts: dict[str, bool] = {}
    for p in reader.pages:
        mb = p.mediabox
        res = p.get("/Resources") or {}
        res = res.get_object() if hasattr(res, "get_object") else res
        xobj = res.get("/XObject") if res else None
        n_images = 0
        if xobj:
            for _, ref in xobj.get_object().items():
                if ref.get_object().get("/Subtype") == "/Image":
                    n_images += 1
        font_res = res.get("/Font") if res else None
        if font_res:
            for _, ref in font_res.get_object().items():
                f = ref.get_object()
                name = str(f.get("/BaseFont"))
                desc = f.get("/FontDescriptor")
                if desc is None and f.get("/DescendantFonts"):
                    desc = f["/DescendantFonts"][0].get_object().get("/FontDescriptor")
                desc = desc.get_object() if desc is not None else None
                embedded = bool(desc and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")))
                fonts[name] = fonts.get(name, True) and embedded
        content = p.get_contents()
        raw = content.get_data() if content is not None else b""
        pages.append({"w_in": round(float(mb.width) / PT, 4), "h_in": round(float(mb.height) / PT, 4),
                      "images": n_images, "has_text": b"Tj" in raw or b"TJ" in raw,
                      "blank": n_images == 0 and _no_marks(raw)})
    return {"page_count": len(pages), "pages": pages, "fonts": fonts,
            "all_fonts_embedded": all(fonts.values()) if fonts else True}


# ------------------------------------------------------------------ export orchestration

def export(db: Session, project: Project, *, kinds: list[str] | None = None, final: bool = True,
           progress=None) -> dict:
    kinds = kinds or ["interior", "cover", "zip"]
    report = qa_book.validate(db, project, final=final)
    if final and not report["passed"]:
        rec = _save(db, project, "qa_report", json.dumps(report, indent=2, default=str).encode(), "application/json",
                    report)
        raise ExportBlocked({**report, "qa_report_export_id": rec.id})
    out: dict = {"exports": {}}
    interior, meta = build_interior(db, project, preview=not final)
    if progress:
        progress(0.5, "Interior built")
    insp = inspect_pdf(interior)
    struct = qa_book.check_interior_pdf(project, meta, insp)
    report["pdf_checks"] = struct
    cover = None
    if "cover" in kinds or "zip" in kinds:
        cover, cgeo = build_cover(db, project, meta["page_count"])
        cinsp = inspect_pdf(cover)
        report["cover_checks"] = qa_book.check_cover_pdf(cgeo, cinsp)
        report["cover_geometry"] = cgeo
    report["interior_geometry"] = meta["geometry"]
    report["interior_page_count"] = meta["page_count"]
    hard_fail = [c for c in struct + report.get("cover_checks", []) if c["status"] == "fail" and c["mandatory"]]
    report["passed"] = report["passed"] and not hard_fail
    report["disclaimer"] = "Local preflight only — KDP's own review is the final authority."
    kind_interior = "interior" if final else "preview"
    out["exports"][kind_interior] = _save(db, project, kind_interior, interior, "application/pdf", meta).id
    if cover is not None and ("cover" in kinds or "zip" in kinds):
        out["exports"]["cover"] = _save(db, project, "cover" if final else "cover_preview", cover, "application/pdf",
                                        report.get("cover_geometry", {})).id
    rep_bytes = json.dumps(report, indent=2, default=str).encode()
    out["exports"]["qa_report"] = _save(db, project, "qa_report", rep_bytes, "application/json", report).id
    if "zip" in kinds and cover is not None:
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("interior.pdf", interior)
            z.writestr("cover.pdf", cover)
            z.writestr("qa_report.json", rep_bytes)
            z.writestr("project.json", json.dumps(project_manifest(db, project), indent=2, default=str))
        out["exports"]["zip"] = _save(db, project, "zip", zbuf.getvalue(), "application/zip", {}).id
    if final and not hard_fail:
        for p in db.scalars(select(Page).where(Page.project_id == project.id)):
            if p.status in ("approved", "locked"):
                p.status = "export_ready" if not p.locked else "locked"
        project.status = "exported"
    out["passed"] = report["passed"]
    out["page_count"] = meta["page_count"]
    if progress:
        progress(1.0, "Export complete")
    return out


def _save(db: Session, project: Project, kind: str, data: bytes, mime: str, report: dict) -> ExportRecord:
    a = storage.store_bytes(db, data, mime=mime, kind="export", project_id=project.id)
    rec = ExportRecord(project_id=project.id, kind=kind, asset_id=a.id, report=report if kind == "qa_report" else {
        k: v for k, v in report.items() if k != "layout"})
    db.add(rec)
    db.flush()
    return rec


def project_manifest(db: Session, project: Project) -> dict:
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.position)))
    out = {"title": project.title, "subtitle": project.subtitle, "author": project.author, "theme": project.theme,
           "exported_at": datetime.now(timezone.utc).isoformat(), "pages": []}
    for p in pages:
        rev = db.get(Revision, p.current_revision_id) if p.current_revision_id else None
        out["pages"].append({"position": p.position, "subject": p.subject, "caption": p.caption_text,
                             "provider": rev.provider_id if rev else None, "model": rev.model_id if rev else None,
                             "sources": p.source_refs})
    rr = services.latest_research(db, project.id)
    out["references"] = rr.data.get("references", []) if rr else []
    return out
