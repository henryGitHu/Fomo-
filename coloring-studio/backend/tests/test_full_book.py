"""Test D + Test I (SIMULATED providers): a complete 40-illustration chemical engineering book,
from project creation through validated KDP interior/cover PDFs."""
import io
import zipfile

import pytest
from pypdf import PdfReader

from studio import kdp
from studio.pdf_export import inspect_pdf

from .conftest import generate_all, make_book, pages_of


@pytest.mark.slow
def test_40_illustration_chemical_engineering_book(client, run_jobs):
    pid = make_book(client, run_jobs, count=40)
    proj = client.get(f"/api/projects/{pid}").json()
    # 5 themed sections x 8, following the learning progression
    assert [s["name"] for s in proj["sections"]] == ["Meet the Chemical Engineers", "Equipment and Machinery",
                                                    "Chemical Processes", "Industrial Applications",
                                                    "Engineering in Everyday Life"]
    assert [s["illustration_count"] for s in proj["sections"]] == [8] * 5
    assert [s["learning_stage"] for s in proj["sections"]] == ["introduction", "tools", "processes", "applications",
                                                              "careers"]
    pages = pages_of(client, pid)
    assert len(pages) == 40
    assert len({p["subject"] for p in pages}) == 40, "every page must be a distinct concept"
    assert all(p["caption_text"] for p in pages), "40 captions"

    generate_all(client, run_jobs, pid, batch=10)
    pages = pages_of(client, pid)
    assert all(p["current_revision"] for p in pages)
    assert all(p["current_revision"]["qa_passed"] for p in pages), [p["position"] for p in pages if not p["current_revision"]["qa_passed"]]
    assert all(p["current_revision"]["effective_ppi"] >= 300 for p in pages)

    # final export is blocked until approval
    r = client.post(f"/api/projects/{pid}/export", json={"final": True})
    assert r.status_code == 409
    for p in pages:
        assert client.post(f"/api/pages/{p['id']}/approve", json={}).status_code == 200
    client.post(f"/api/projects/{pid}/publication-approval", json={"approved": True})
    qa = client.get(f"/api/projects/{pid}/qa").json()
    assert qa["passed"], [c for c in qa["checks"] if c["status"] == "fail"]
    assert qa["interior_page_count"] == 84

    r = client.post(f"/api/projects/{pid}/export", json={"final": True})
    assert r.status_code == 202, r.text
    run_jobs()
    job = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["page_count"] == 84
    exports = {e["kind"]: e for e in client.get(f"/api/projects/{pid}/exports").json()}
    rep = client.get(exports["qa_report"]["download_url"]).json()
    bad = [c for c in rep.get("pdf_checks", []) + rep.get("cover_checks", []) if c["status"] != "pass"]
    assert job["result"]["passed"] is True, bad

    exports = {e["kind"]: e for e in client.get(f"/api/projects/{pid}/exports").json()}
    assert {"interior", "cover", "zip", "qa_report"} <= set(exports)

    # ---- Test I: inspect the actual PDFs
    interior = client.get(exports["interior"]["download_url"]).content
    insp = inspect_pdf(interior)
    assert insp["page_count"] == 84
    assert all(p["w_in"] == 8.5 and p["h_in"] == 11.0 for p in insp["pages"])
    assert insp["fonts"] and insp["all_fonts_embedded"], insp["fonts"]
    # pages 1-4 intro, then recto illustration (odd) + blank verso (even)
    for n in range(5, 85):
        pg = insp["pages"][n - 1]
        if n % 2 == 1:
            assert pg["images"] == 1 and pg["has_text"], f"page {n} should hold art + caption"
        else:
            assert pg["blank"], f"page {n} should be blank"
    cover = client.get(exports["cover"]["download_url"]).content
    cinsp = inspect_pdf(cover)
    g = kdp.cover_geometry("8.5x11", 84, "white")
    assert cinsp["page_count"] == 1
    assert abs(cinsp["pages"][0]["w_in"] - g.full_w) < 0.01 and abs(cinsp["pages"][0]["h_in"] - g.full_h) < 0.01
    assert g.spine_w == pytest.approx(84 * 0.002252, abs=1e-4)
    assert g.spine_text_allowed  # 84 > 79 pages
    assert cinsp["all_fonts_embedded"]
    z = zipfile.ZipFile(io.BytesIO(client.get(exports["zip"]["download_url"]).content))
    assert set(z.namelist()) == {"interior.pdf", "cover.pdf", "qa_report.json", "project.json"}
    assert len(PdfReader(io.BytesIO(z.read("interior.pdf"))).pages) == 84
