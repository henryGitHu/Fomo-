"""Tests F and K plus the documented Live Book Editor commands (SIMULATED providers)."""
from studio.db import SessionLocal
from studio.models import Asset, Page, Revision

from .conftest import generate_all, make_book, pages_of


def checksums(pid):
    with SessionLocal() as db:
        out = {}
        for p in db.query(Page).filter_by(project_id=pid):
            if p.current_revision_id:
                r = db.get(Revision, p.current_revision_id)
                out[p.position] = db.get(Asset, r.asset_id).checksum
        return out


def chat(client, pid, msg):
    r = client.post(f"/api/projects/{pid}/chat", json={"message": msg})
    assert r.status_code == 200, r.text
    return r.json()


def test_F_targeted_chat_edit_changes_only_page_17(client, run_jobs):
    pid = make_book(client, run_jobs, count=20)
    generate_all(client, run_jobs, pid)
    before = checksums(pid)
    caps_before = {p["position"]: p["caption_text"] for p in pages_of(client, pid)}
    msg = chat(client, pid, "Change page 17 to show a distillation tower instead of a mixing tank.")
    assert "page 17" in msg["content"].lower() or "page(s) [17]" in msg["content"]
    run_jobs()
    after = checksums(pid)
    assert after[17] != before[17]
    for pos in before:
        if pos != 17:
            assert after[pos] == before[pos], f"page {pos} changed"
    pages = {p["position"]: p for p in pages_of(client, pid)}
    assert pages[17]["subject"].lower().startswith("a distillation tower")
    assert pages[17]["caption_status"] == "review"  # artwork changed substantially -> caption revalidation
    for pos, cap in caps_before.items():
        if pos != 17:
            assert pages[pos]["caption_text"] == cap


def test_K_locked_pages_survive_broad_commands(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    generate_all(client, run_jobs, pid)
    pages = pages_of(client, pid)
    for p in pages[:4]:
        client.post(f"/api/pages/{p['id']}/approve", json={})
    m = chat(client, pid, "Keep all approved pages unchanged.")
    assert "Locked 4" in m["content"]
    before = checksums(pid)
    caps = {p["position"]: p["caption_text"] for p in pages_of(client, pid)}
    chat(client, pid, "Regenerate all pages")
    chat(client, pid, "Make the lines thicker on pages 1-10")
    chat(client, pid, "Rewrite the caption on pages 1-10")
    run_jobs()
    after = checksums(pid)
    now = {p["position"]: p for p in pages_of(client, pid)}
    for pos in range(1, 5):
        assert after[pos] == before[pos] and now[pos]["locked"] and now[pos]["caption_text"] == caps[pos]
    assert all(after[pos] != before[pos] for pos in range(5, 11))
    # direct API edits are refused too
    r = client.patch(f"/api/pages/{now[1]['id']}", json={"caption_text": "x"})
    assert r.status_code == 423


def test_caption_edit_does_not_regenerate_art(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    generate_all(client, run_jobs, pid)
    before = checksums(pid)
    m = chat(client, pid, "Rewrite the caption on page 9.")
    assert "Page 9 caption" in m["content"]
    m = chat(client, pid, 'Set caption on page 3 to: Valves control the flow of liquids, like a faucet!')
    run_jobs()
    assert checksums(pid) == before
    p3 = next(p for p in pages_of(client, pid) if p["position"] == 3)
    assert p3["caption_text"] == "Valves control the flow of liquids, like a faucet!"


def test_documented_commands(client, run_jobs):
    pid = make_book(client, run_jobs, count=20)
    m = chat(client, pid, "Generate the next five illustrations.")
    assert "next 5" in m["content"]
    run_jobs()
    assert sum(1 for p in pages_of(client, pid) if p["current_revision"]) == 5

    m = chat(client, pid, "Regenerate page 4 using the mock_alt provider.")
    run_jobs()
    p4 = next(p for p in pages_of(client, pid) if p["position"] == 4)
    assert p4["current_revision"]["provider_id"] == "mock_alt"

    m = chat(client, pid, "Try the same illustration in Atelier and mock and compare the results.")
    assert "battle for page 4" in m["content"]  # "same illustration" resolved from conversation context
    run_jobs()
    battles = client.get(f"/api/projects/{pid}/battles").json()
    assert len(battles[0]["candidates"]) == 2

    m = chat(client, pid, "Regenerate page 14 using the OpenAI provider.")
    assert "not configured" in m["content"]  # no key in this environment -> clear message, nothing queued

    m = chat(client, pid, "Make Section 2 more technically realistic.")
    assert "Section 2" in m["content"]
    proj = client.get(f"/api/projects/{pid}").json()
    assert "more technically realistic" in proj["sections"][1]["theme_instructions"]

    m = chat(client, pid, "Use the highest-rated available provider for the next section.")
    assert "will use" in m["content"]

    before = checksums(pid)
    m = chat(client, pid, "Make the lines thicker on pages 1-3")
    run_jobs()
    after = checksums(pid)
    assert all(after[i] != before[i] for i in (1, 2, 3)) and after[5] == before[5]
    m = chat(client, pid, "undo")
    assert "Undid" in m["content"]

    m = chat(client, pid, "approve pages 1-2")
    assert "[1, 2]" in m["content"]
    m = chat(client, pid, "regenerate page 1")
    assert "confirm" in m["content"].lower()  # approved page -> confirmation required
    m = chat(client, pid, "confirm")
    assert "Queued regeneration" in m["content"]
    run_jobs()
    hist = client.get(f"/api/projects/{pid}/chat").json()
    assert len(hist) >= 20  # persisted conversation


def test_edits_queue_behind_running_generation(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    client.post(f"/api/projects/{pid}/generate", json={"count": 3})
    # submitted while generation is still pending
    chat(client, pid, "Make the lines thicker on pages 1-3")
    run_jobs()
    hist = client.get(f"/api/projects/{pid}/history").json()
    lw = [h for h in hist if h["action"] == "line_weight"]
    assert len(lw) == 3  # applied after the generation finished, not lost


def test_undo_and_restore_api(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    page = pages_of(client, pid)[0]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    run_jobs()
    first = client.get(f"/api/pages/{page['id']}").json()["current_revision"]["id"]
    client.post(f"/api/pages/{page['id']}/generate", json={})
    run_jobs()
    second = client.get(f"/api/pages/{page['id']}").json()["current_revision"]["id"]
    assert first != second
    r = client.post(f"/api/pages/{page['id']}/restore", json={"revision_id": first})
    assert r.json()["current_revision"]["id"] == first
    client.post(f"/api/projects/{pid}/undo")
    assert client.get(f"/api/pages/{page['id']}").json()["current_revision"]["id"] == second
