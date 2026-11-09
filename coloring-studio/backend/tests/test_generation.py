"""Tests B, C, E, G, J, M + concurrency safeguards (SIMULATED providers)."""
import pytest

from studio import jobs, services
from studio.db import SessionLocal
from studio.models import Asset, Job, Page, Revision

from .conftest import make_book, pages_of


def _rev(client, page_id):
    return client.get(f"/api/pages/{page_id}").json()


def _checksums(pid):
    with SessionLocal() as db:
        out = {}
        for p in db.query(Page).filter_by(project_id=pid):
            if p.current_revision_id:
                r = db.get(Revision, p.current_revision_id)
                out[p.position] = (p.current_revision_id, db.get(Asset, r.asset_id).checksum)
        return out


def test_B_provider_switching_keeps_style_and_saves(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    pages = pages_of(client, pid)
    r1 = client.post(f"/api/pages/{pages[0]['id']}/generate", json={"provider_id": "mock"})
    r2 = client.post(f"/api/pages/{pages[1]['id']}/generate", json={"provider_id": "mock_alt"})
    assert r1.status_code == r2.status_code == 202
    run_jobs()
    a, b = _rev(client, pages[0]["id"]), _rev(client, pages[1]["id"])
    ra, rb = a["current_revision"], b["current_revision"]
    assert (ra["provider_id"], rb["provider_id"]) == ("mock", "mock_alt")
    # both prompts carry the identical book-level style constraints
    style = client.get(f"/api/projects/{pid}/style").json()["data"]
    for rev in (ra, rb):
        assert style["art_style"] in rev["prompt"] and style["line_thickness"] in rev["prompt"]
        assert "Pure white background" in rev["prompt"]
        assert rev["image_url"] and client.get(rev["image_url"]).status_code == 200
    # switching providers does not lose project data
    assert len(pages_of(client, pid)) == 10
    assert ra["model_id"] == "mock-lineart-v1" and rb["model_id"] == "atelier-sketch-1"


def test_C_model_battle_does_not_silently_replace(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    page = pages_of(client, pid)[2]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    run_jobs()
    client.post(f"/api/pages/{page['id']}/approve", json={})
    approved_rev = _rev(client, page["id"])["current_revision"]["id"]

    r = client.post(f"/api/projects/{pid}/battles", json={"page_id": page["id"], "contenders": [
        {"provider_id": "mock"}, {"provider_id": "mock_alt"}]})
    assert r.status_code == 202, r.text
    run_jobs()
    b = client.get(f"/api/battles/{r.json()['id']}").json()
    assert b["status"] == "awaiting_selection"
    assert len(b["candidates"]) == 2
    for c in b["candidates"]:
        assert {"line_clarity", "composition", "coloring_suitability"} <= set(c["objective_scores"])
        assert "not run" in c["subjective_evaluation"]  # objective vs subjective kept separate
    # candidates exist but the approved artwork is unchanged
    assert _rev(client, page["id"])["current_revision"]["id"] == approved_rev

    loser, winner = b["candidates"][0], b["candidates"][1]
    r = client.post(f"/api/battles/{b['id']}/select", json={"revision_id": winner["revision_id"]})
    assert r.status_code == 409  # approved page needs explicit confirmation
    r = client.post(f"/api/battles/{b['id']}/select", json={"revision_id": winner["revision_id"],
                                                            "confirm_replace_approved": True})
    assert r.status_code == 200
    d = _rev(client, page["id"])
    assert d["current_revision"]["id"] == winner["revision_id"]
    ids = {r["id"] for r in d["revisions"]}
    assert loser["revision_id"] in ids and approved_rev in ids  # alternatives retained in history


def test_C_battle_cannot_replace_locked(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    page = pages_of(client, pid)[0]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    run_jobs()
    client.post(f"/api/pages/{page['id']}/lock", json={})
    r = client.post(f"/api/projects/{pid}/battles", json={"page_id": page["id"], "contenders": [
        {"provider_id": "mock"}, {"provider_id": "mock_alt"}]})
    run_jobs()
    cand = client.get(f"/api/battles/{r.json()['id']}").json()["candidates"][0]
    r = client.post(f"/api/battles/{r.json()['id']}/select", json={"revision_id": cand["revision_id"],
                                                                   "confirm_replace_approved": True})
    assert r.status_code == 423


def test_E_batch_approval_reject_and_edit(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    r = client.post(f"/api/projects/{pid}/generate", json={})  # default batch of 5
    assert r.status_code == 202
    run_jobs()
    pages = pages_of(client, pid)
    batch = [p for p in pages if p["current_revision"]]
    assert len(batch) == 5 and all(p["status"] == "awaiting_review" for p in batch)
    for p in batch[:3]:
        assert client.post(f"/api/pages/{p['id']}/approve", json={}).status_code == 200
    assert client.post(f"/api/pages/{batch[3]['id']}/reject", json={"note": "too busy"}).status_code == 200
    edit = client.patch(f"/api/pages/{batch[4]['id']}", json={"caption_text": "Pumps push liquids through pipes!"})
    assert edit.status_code == 200
    approved_before = _checksums(pid)
    # regenerate the rejected page and continue with the next batch
    client.post(f"/api/pages/{batch[3]['id']}/generate", json={})
    client.post(f"/api/projects/{pid}/generate", json={})
    run_jobs()
    after = pages_of(client, pid)
    for p in batch[:3]:
        cur = next(x for x in after if x["id"] == p["id"])
        assert cur["status"] == "approved"
        assert approved_before[cur["position"]] == _checksums(pid)[cur["position"]]
    edited = next(x for x in after if x["id"] == batch[4]["id"])
    assert edited["caption_text"] == "Pumps push liquids through pipes!"
    assert approved_before[edited["position"]] == _checksums(pid)[edited["position"]]  # caption edit kept art
    assert sum(1 for x in after if x["current_revision"]) == 10


def test_G_provider_failure_retry_and_recovery(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    pages = pages_of(client, pid)
    # transient timeout: retried and succeeds
    client.put("/api/providers/mock", json={"settings": {"fail_mode": "timeout_once"}})
    client.post(f"/api/pages/{pages[0]['id']}/generate", json={})
    run_jobs()
    job = client.get(f"/api/jobs?project_id={pid}&limit=1").json()[0]
    out = job["result"]["outcomes"][0]
    assert out["status"] == "ok"
    assert [a["ok"] for a in out["attempts"]] == [False, True]
    assert "timed out" in out["attempts"][0]["error"] or "timeout" in out["attempts"][0]["error"]

    # quota error: not retried, clear error, page left safe
    client.put("/api/providers/mock", json={"settings": {"fail_mode": "quota"}})
    client.post(f"/api/pages/{pages[1]['id']}/generate", json={})
    run_jobs()
    out = client.get(f"/api/jobs?project_id={pid}&limit=1").json()[0]["result"]["outcomes"][0]
    assert out["status"] == "failed" and "quota" in out["message"]
    assert len(out["attempts"]) == 1
    assert _rev(client, pages[1]["id"])["status"] == "failed"

    # fallback enabled: switches provider, keeps book style and records cost
    client.patch(f"/api/projects/{pid}", json={"fallback_enabled": True})
    client.post(f"/api/pages/{pages[1]['id']}/generate", json={})
    run_jobs()
    out = client.get(f"/api/jobs?project_id={pid}&limit=1").json()[0]["result"]["outcomes"][0]
    assert out["status"] == "ok" and out["provider"] == "mock_alt"
    rev = _rev(client, pages[1]["id"])["current_revision"]
    assert "Pure white background" in rev["prompt"]


def test_G_fallback_respects_budget(client, run_jobs):
    pid = make_book(client, run_jobs, count=10, budget_limit=0.05, fallback_enabled=True)
    client.put("/api/providers/mock", json={"settings": {"fail_mode": "quota"}})
    client.put("/api/providers/mock_alt", json={"settings": {"simulated_price_per_image": 1.0}})
    client.patch(f"/api/projects/{pid}", json={"fallback_enabled": True})
    page = pages_of(client, pid)[0]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    run_jobs()
    job = client.get(f"/api/jobs?project_id={pid}&limit=1").json()[0]
    assert job["result"]["outcomes"][0]["status"] == "blocked"
    assert client.get(f"/api/projects/{pid}/costs").json()["total"] == 0


def test_J_interrupted_generation_resumes_without_duplicates(client, run_jobs, monkeypatch):
    pid = make_book(client, run_jobs, count=10)
    pages = pages_of(client, pid)
    client.post(f"/api/pages/{pages[0]['id']}/generate", json={})
    run_jobs()
    client.post(f"/api/pages/{pages[0]['id']}/approve", json={})
    approved = _checksums(pid)[1]

    r = client.post(f"/api/projects/{pid}/generate", json={"count": 6})
    job_id = r.json()["id"]
    assert len(r.json()["payload"]["page_ids"]) == 6

    from studio import generation
    real = generation.generate_page
    calls = {"n": 0}

    def crashing(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            raise SystemExit("simulated process crash")
        return real(*a, **kw)
    monkeypatch.setattr(generation, "generate_page", crashing)
    with pytest.raises(SystemExit):
        run_jobs()
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "running"  # crashed mid-flight
    monkeypatch.setattr(generation, "generate_page", real)
    # process restarts: interrupted jobs are re-queued and resumed
    with SessionLocal() as db:
        assert jobs.recover_stale(db, force=True) == 1
        db.commit()
    run_jobs()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert len(job["result"]["done"]) == 6 == len(set(job["result"]["done"]))
    with SessionLocal() as db:
        for p in db.query(Page).filter_by(project_id=pid):
            n = db.query(Revision).filter(Revision.page_id == p.id, Revision.kind == "enhanced").count()
            assert n <= 1, f"page {p.position} generated {n} times"
    assert _checksums(pid)[1] == approved
    assert sum(1 for p in pages_of(client, pid) if p["current_revision"]) == 7


def test_pause_and_resume(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 5})
    jid = r.json()["id"]
    client.post(f"/api/jobs/{jid}/pause")
    run_jobs()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "paused"
    assert not any(p["current_revision"] for p in pages_of(client, pid))
    client.post(f"/api/jobs/{jid}/resume")
    run_jobs()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"
    assert sum(1 for p in pages_of(client, pid) if p["current_revision"]) == 5


def test_M_spending_limits(client, run_jobs):
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": 0.10}})
    pid = make_book(client, run_jobs, count=10, budget_limit=0.35)
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 5})
    assert r.status_code == 402 and "Budget limit" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 3})
    assert r.status_code == 202
    run_jobs()
    c = client.get(f"/api/projects/{pid}/costs").json()
    assert c["total"] == pytest.approx(0.30)
    assert c["remaining_budget"] == pytest.approx(0.05)
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 1})
    assert r.status_code == 402
    # worker-side guard: a job enqueued before the price rose still cannot overspend
    client.patch(f"/api/projects/{pid}", json={"budget_limit": 0.45})
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 1})
    assert r.status_code == 202
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": 5.0}})
    run_jobs()
    assert client.get(f"/api/projects/{pid}/costs").json()["total"] == pytest.approx(0.30)
    # unknown pricing under a budget requires explicit confirmation
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": None}})
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 1})
    assert r.status_code == 402 and "unknown" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/generate", json={"count": 1, "allow_unknown_cost": True})
    assert r.status_code == 202
    run_jobs()
    c = client.get(f"/api/projects/{pid}/costs").json()
    assert c["unknown_cost_operations"] == 1  # surfaced, not hidden


def test_stale_job_never_overwrites_newer_revision(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    page = pages_of(client, pid)[0]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    # user edits the page (subject change) after the job was queued, before it runs
    from studio import generation
    real = generation.generate_page

    def edit_midway(db, project, pg, **kw):
        out = None
        with SessionLocal() as other:
            p2 = other.get(Page, pg.id)
            p2.subject = "A brand-new subject"
            p2.version += 1
            other.commit()
        out = real(db, project, pg, **kw)
        return out
    import studio.job_handlers as jh
    jh.generation.generate_page = edit_midway
    try:
        run_jobs()
    finally:
        jh.generation.generate_page = real
    out = client.get(f"/api/jobs?project_id={pid}&limit=1").json()[0]["result"]["outcomes"][0]
    assert out["status"] == "stale"
    d = _rev(client, page["id"])
    assert d["current_revision"] is None  # stale result kept only as an alternative
    assert any(r["status"] == "stale" for r in d["revisions"])


def test_quality_first_mode_and_routing(client, run_jobs):
    pid = make_book(client, run_jobs, count=10, generation_mode="auto", routing_preference="lowest_cost")
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": 0.02}})
    client.put("/api/providers/mock_alt", json={"settings": {"simulated_price_per_image": 0.01}})
    prev = client.get(f"/api/projects/{pid}/route-preview").json()
    assert prev["ranking"][0]["provider_id"] == "mock_alt"  # cheapest wins under lowest_cost
    client.post(f"/api/projects/{pid}/generate", json={"count": 1})
    run_jobs()
    rev = pages_of(client, pid)[0]["current_revision"]
    assert rev["provider_id"] == "mock_alt"


def test_autopilot_continues_until_done_or_budget(client, run_jobs):
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": 0.01}})
    pid = make_book(client, run_jobs, count=10, autopilot=True, budget_limit=0.07, batch_size=3)
    client.post(f"/api/projects/{pid}/generate", json={})
    run_jobs()
    done = sum(1 for p in pages_of(client, pid) if p["current_revision"])
    assert done == 6  # 3 + 3; the third batch (0.09 total) would exceed $0.07
    assert client.get(f"/api/projects/{pid}/costs").json()["total"] <= 0.07
