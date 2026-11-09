"""Book-level quality assurance (structure, images, captions, PDFs)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import captions, kdp, services
from .imaging.quality import hamming
from .models import Page, Project, Revision, Section


def _c(cid: str, ok: bool, msg: str, *, mandatory: bool = True, warn: bool = False, page: int | None = None,
       fix: str | None = None) -> dict:
    return {"id": cid, "status": "pass" if ok else ("warn" if warn else "fail"), "mandatory": mandatory,
            "message": msg, "page": page, "fix": fix}


def validate(db: Session, project: Project, *, final: bool = True) -> dict:
    checks: list[dict] = []
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id).order_by(Page.position)))
    sections = list(db.scalars(select(Section).where(Section.project_id == project.id)))
    n = len(pages)
    checks.append(_c("illustration_count", n == project.illustration_count and n > 0,
                     f"{n} planned illustrations (book total {project.illustration_count})"))
    alloc = sum(s.illustration_count for s in sections)
    real_alloc = {s.id: sum(1 for p in pages if p.section_id == s.id) for s in sections}
    checks.append(_c("section_allocation", alloc == n and all(real_alloc[s.id] == s.illustration_count for s in sections),
                     f"Sections allocate {alloc} of {n} illustrations", fix="Adjust section counts in the planner"))
    missing = [p.position for p in pages if not p.current_revision_id]
    checks.append(_c("artwork_present", not missing, "All pages have artwork" if not missing else
                     f"Missing artwork on pages {missing[:20]}", mandatory=final, warn=not final,
                     fix="Generate the remaining batches"))
    unapproved = [p.position for p in pages if p.status not in services.APPROVED_STATES and not p.locked]
    checks.append(_c("all_approved", not unapproved, "All pages approved" if not unapproved else
                     f"{len(unapproved)} page(s) not approved: {unapproved[:20]}", mandatory=final, warn=not final,
                     fix="Review and approve pages in the Illustration Studio"))
    checks.append(_c("publication_approval", project.publication_approved, "Final publication approval given" if
                     project.publication_approved else "Publication not approved yet", mandatory=final,
                     warn=not final, fix="Approve publication on the Publishing screen"))

    # images
    hashes: dict[int, str] = {}
    img_fail: list[int] = []
    ppi_fail: list[int] = []
    for p in pages:
        if not p.current_revision_id:
            continue
        rev = db.get(Revision, p.current_revision_id)
        q = rev.quality or {}
        if q.get("dhash"):
            hashes[p.position] = q["dhash"]
        for ch in q.get("checks", []):
            if ch["mandatory"] and ch["status"] == "fail":
                (ppi_fail if ch["id"] == "print_resolution" else img_fail).append(p.position)
                break
    checks.append(_c("image_quality", not img_fail, "All images pass mandatory image checks" if not img_fail else
                     f"Image checks failed on pages {sorted(set(img_fail))[:20]}", fix="Regenerate or enhance"))
    checks.append(_c("print_resolution", not ppi_fail, "All images ≥ 300 effective PPI" if not ppi_fail else
                     f"Low resolution on pages {ppi_fail[:20]}", fix="Regenerate at higher resolution or enhance"))
    dups = []
    items = sorted(hashes.items())
    for i, (pa, ha) in enumerate(items):
        for pb, hb in items[i + 1:]:
            if hamming(ha, hb) <= 6:
                dups.append((pa, pb))
    checks.append(_c("unique_illustrations", not dups, "No duplicate illustrations" if not dups else
                     f"Near-identical pages: {dups[:10]}", fix="Regenerate one of each duplicate pair"))

    # captions
    cs = captions.settings_for(project.caption_settings)
    if cs.get("enabled", True):
        no_cap = [p.position for p in pages if not p.caption_text]
        bad_cap = [p.position for p in pages if p.caption_text and p.caption_status == "invalid"]
        review = [p.position for p in pages if p.caption_status == "review"]
        checks.append(_c("captions_present", not no_cap, "Every page has a caption" if not no_cap else
                         f"Missing captions on {no_cap[:20]}", fix="Generate captions"))
        checks.append(_c("captions_fit", not bad_cap, "All captions fit and pass checks" if not bad_cap else
                         f"Caption problems on {bad_cap[:20]}", fix="Shorten captions in the Caption Editor"))
        checks.append(_c("captions_reviewed", not review, "No captions awaiting review" if not review else
                         f"Captions to review on {review[:20]}", mandatory=False, warn=True))
        subjects = [p.subject for p in pages]
        checks.append(_c("subject_variety", len(set(subjects)) == len(subjects), "Every page has a distinct subject",
                         mandatory=False, warn=True))

    # structure
    total = kdp.interior_page_count(n, project.intro_pages)
    errs = kdp.validate_page_count(total, project.paper_type)
    checks.append(_c("kdp_page_count", not errs, f"{total} interior pages" + (f": {'; '.join(errs)}" if errs else "")))
    geo = services.geometry(project)
    checks.append(_c("margins", geo.inside >= kdp.inside_margin(total) and geo.outside >= kdp.outside_margin(project.bleed),
                     f"Gutter {geo.inside}in (min {kdp.inside_margin(total)}), outside {geo.outside}in"))
    checks.append(_c("trim_size", project.trim_size in kdp.TRIM_SIZES, f"Trim {project.trim_size}"))
    passed = all(c["status"] != "fail" for c in checks if c["mandatory"])
    return {"passed": passed, "final": final, "checks": checks, "interior_page_count": total,
            "illustrations": n}


def check_interior_pdf(project: Project, meta: dict, insp: dict) -> list[dict]:
    geo = meta["geometry"]
    checks = []
    expected = kdp.interior_page_count(project.illustration_count, project.intro_pages)
    checks.append(_c("pdf_page_count", insp["page_count"] == expected == meta["page_count"],
                     f"PDF has {insp['page_count']} pages (expected {expected})"))
    dims_ok = all(abs(p["w_in"] - geo["page_w"]) < 0.01 and abs(p["h_in"] - geo["page_h"]) < 0.01 for p in insp["pages"])
    checks.append(_c("pdf_page_size", dims_ok, f"All pages {geo['page_w']}×{geo['page_h']} in"))
    checks.append(_c("fonts_embedded", insp["all_fonts_embedded"],
                     f"Fonts: {', '.join(f'{k} ({'embedded' if v else 'NOT embedded'})' for k, v in insp['fonts'].items())}"))
    rectos_ok, versos_ok = True, True
    for item, pg in zip(meta["layout"], insp["pages"]):
        if item["kind"] == "illustration":
            rectos_ok &= item["page"] % 2 == 1 and pg["images"] == 1
        if item["kind"] == "blank_verso":
            versos_ok &= item["page"] % 2 == 0 and pg["blank"]
    checks.append(_c("illustrations_on_recto", rectos_ok, "Every illustration is on an odd (right-hand) page with one image"))
    checks.append(_c("blank_versos", versos_ok, "Every illustration's reverse side is blank"))
    low = [i["position"] for i in meta["layout"] if i.get("image_ppi") and i["image_ppi"] < 299.5]
    checks.append(_c("placed_ppi", not low, "Placed images ≥ 300 PPI" if not low else f"Low placed PPI on pages {low[:20]}"))
    caps = [i for i in meta["layout"] if i.get("caption_lines", 0) > 3]
    checks.append(_c("caption_placement", not caps, "Captions fit in the caption band"))
    return checks


def check_cover_pdf(cgeo: dict, insp: dict) -> list[dict]:
    p = insp["pages"][0] if insp["pages"] else {"w_in": 0, "h_in": 0}
    ok = insp["page_count"] == 1 and abs(p["w_in"] - cgeo["full_w"]) < 0.01 and abs(p["h_in"] - cgeo["full_h"]) < 0.01
    return [_c("cover_dimensions", ok, f"Cover {p['w_in']}×{p['h_in']} in (expected {cgeo['full_w']}×{cgeo['full_h']}; "
                                       f"spine {cgeo['spine_w']} in)"),
            _c("cover_fonts_embedded", insp["all_fonts_embedded"], "Cover fonts embedded")]
