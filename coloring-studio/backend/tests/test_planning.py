"""Test L (dynamic sections), section-builder safety and the learning progression."""
import pytest

from studio import kdp, planning

from .conftest import make_book, pages_of


@pytest.mark.parametrize("n,lo,hi", [(10, 1, 1), (20, 2, 2), (30, 3, 4), (40, 4, 5), (60, 6, 8), (80, 8, 10),
                                     (100, 10, 12)])
def test_recommendation_table(n, lo, hi):
    r = planning.recommend_sections(n)
    assert (r["min"], r["max"]) == (lo, hi)
    assert lo <= r["recommended"] <= hi


@pytest.mark.parametrize("n", [20, 40, 60, 80, 100])
def test_L_dynamic_sections(client, run_jobs, n):
    pid = make_book(client, run_jobs, count=n)
    proj = client.get(f"/api/projects/{pid}").json()
    rec = planning.recommend_sections(n)
    secs = proj["sections"]
    assert len(secs) == rec["recommended"]
    assert sum(s["illustration_count"] for s in secs) == n
    assert all(s["actual_count"] == s["illustration_count"] for s in secs)
    pages = pages_of(client, pid)
    assert [p["position"] for p in pages] == list(range(1, n + 1))
    assert len({p["subject"] for p in pages}) == n, "no duplicated page concepts"
    # learning order never goes backwards
    order = {s: i for i, s in enumerate(planning.PROGRESSION)}
    stages = [order[s["learning_stage"]] for s in secs]
    assert stages == sorted(stages)
    assert proj["interior_page_count"] == kdp.interior_page_count(n, 4) == 4 + 2 * n


def test_section_edits_do_not_reset_rest_of_book(client, run_jobs):
    pid = make_book(client, run_jobs, count=20, sections=2)
    proj = client.get(f"/api/projects/{pid}").json()
    s1, s2 = proj["sections"]
    client.post(f"/api/projects/{pid}/generate", json={"count": 3})
    run_jobs()
    before = {p["id"]: p for p in pages_of(client, pid)}
    # grow section 2, shrink is refused only if it would drop approved/locked pages
    r = client.patch(f"/api/sections/{s2['id']}", json={"illustration_count": 13})
    assert r.status_code == 200
    after = pages_of(client, pid)
    assert len(after) == 23
    for p in after:
        if p["id"] in before:
            b = before[p["id"]]
            assert p["subject"] == b["subject"] and p["current_revision"] == b["current_revision"]
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["illustration_count"] == 23
    assert sum(s["illustration_count"] for s in proj["sections"]) == 23
    # approve section 1 pages then try to shrink below them
    for p in after[:3]:
        client.post(f"/api/pages/{p['id']}/approve", json={})
    r = client.patch(f"/api/sections/{s1['id']}", json={"illustration_count": 2})
    assert r.status_code == 409
    r = client.patch(f"/api/sections/{s1['id']}", json={"illustration_count": 5})
    assert r.status_code == 200
    # rebuilding the whole plan is refused once artwork exists
    assert client.post(f"/api/projects/{pid}/plan", json={}).status_code == 409


def test_section_add_duplicate_reorder_remove(client, run_jobs):
    pid = make_book(client, run_jobs, count=20, sections=2)
    r = client.post(f"/api/projects/{pid}/sections", json={"name": "Bonus", "illustration_count": 4})
    assert r.status_code == 201
    sid = r.json()["id"]
    d = client.post(f"/api/sections/{sid}/duplicate").json()
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["illustration_count"] == 28
    ids = [s["id"] for s in proj["sections"]]
    new_order = [ids[2], ids[0], ids[1], ids[3]]
    proj = client.post(f"/api/projects/{pid}/sections/reorder", json={"section_ids": new_order}).json()
    assert [s["id"] for s in proj["sections"]] == new_order
    assert proj["sections"][0]["page_positions"] == [1, 2, 3, 4]
    assert client.delete(f"/api/sections/{d['id']}").status_code == 200
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["illustration_count"] == 24
    assert [p["position"] for p in pages_of(client, pid)] == list(range(1, 25))


def test_education_progression_chemical_engineering(client, run_jobs):
    pid = make_book(client, run_jobs, count=40)
    pages = pages_of(client, pid)
    first = [p["subject"] for p in pages[:8]]
    assert "A chemical engineer at work" in first
    last = [p["subject"] for p in pages[-8:]]
    assert "Future engineer" in last
    assert all("experiment" not in (p["caption_text"] or "").lower() for p in pages)


def test_generic_topic_offline_plan_is_marked_for_review(client, run_jobs):
    pid = make_book(client, run_jobs, count=10, theme="ocean animals")
    rr = client.get(f"/api/projects/{pid}/research").json()
    assert rr["mode"] == "offline_template"
    assert "web research was not available" in rr["overview"].lower()
    pages = pages_of(client, pid)
    assert all(p["source_refs"] == [] for p in pages)
