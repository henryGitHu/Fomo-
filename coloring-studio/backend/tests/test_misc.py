"""Benchmarks, preview export, captions engine and Style DNA anchors (SIMULATED providers)."""
from studio import captions

from .conftest import generate_all, make_book, pages_of


def test_benchmark_runs_within_spend_cap_and_feeds_router(client, run_jobs):
    client.put("/api/providers/mock", json={"settings": {"simulated_price_per_image": 0.01}})
    r = client.post("/api/benchmarks", json={"contenders": [{"provider_id": "mock"}], "max_spend": 0.01})
    assert r.status_code == 402  # 8 images * $0.01 > $0.01
    r = client.post("/api/benchmarks", json={"contenders": [{"provider_id": "mock"}, {"provider_id": "mock_alt"}],
                                             "max_spend": 1.0})
    assert r.status_code == 202, r.text
    run_jobs()
    res = client.get("/api/benchmarks").json()
    assert {s["provider_id"] for s in res["summary"]} == {"mock", "mock_alt"}
    cats = res["summary"][0]["categories"]
    assert set(cats) == set(res["suite"])
    assert "style_consistency" in cats["character_consistency"]
    assert cats["editing_fidelity"]["edit_preservation"] is not None
    provs = {p["provider_id"]: p for p in client.get("/api/providers").json()}
    assert provs["mock"]["models"][0]["samples"] >= 7  # measured history now drives routing


def test_preview_export_before_completion(client, run_jobs):
    pid = make_book(client, run_jobs, count=12)
    client.post(f"/api/projects/{pid}/generate", json={"count": 2})
    run_jobs()
    r = client.post(f"/api/projects/{pid}/export", json={"final": False, "kinds": ["interior"]})
    assert r.status_code == 202
    run_jobs()
    ex = client.get(f"/api/projects/{pid}/exports").json()
    kinds = {e["kind"] for e in ex}
    assert "preview" in kinds and "interior" not in kinds
    lay = client.get(f"/api/projects/{pid}/layout").json()
    assert lay["total"] == 28 and lay["pages"][4]["kind"] == "illustration" and lay["pages"][5]["kind"] == "blank_verso"


def test_qa_blocks_publication_with_reasons(client, run_jobs):
    pid = make_book(client, run_jobs, count=9)  # 4 + 18 = 22 pages < KDP minimum of 24
    qa = client.get(f"/api/projects/{pid}/qa").json()
    failing = {c["id"] for c in qa["checks"] if c["status"] == "fail"}
    assert {"artwork_present", "all_approved", "publication_approval", "kdp_page_count"} <= failing  # 24 pages min
    assert all(c["fix"] for c in qa["checks"] if c["status"] == "fail" and c["id"] != "kdp_page_count")


def test_caption_engine_settings_and_validation(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    client.patch(f"/api/projects/{pid}", json={"caption_settings": {"vocabulary": True, "question": True,
                                                                     "length": "medium"}})
    client.post(f"/api/projects/{pid}/captions/regenerate", json={})
    p = pages_of(client, pid)[0]
    assert "New word:" in p["caption_text"] and "?" in p["caption_text"]
    long = "word " * 120
    r = client.patch(f"/api/pages/{p['id']}", json={"caption_text": long})
    assert r.json()["caption_validation"]["passed"] is False
    assert r.json()["caption_status"] == "invalid"
    r = client.patch(f"/api/pages/{p['id']}", json={"caption_text": "Try this at home: mix chemicals!"})
    assert any(c["id"] == "safety" and c["status"] == "fail" for c in r.json()["caption_validation"]["checks"])
    assert captions.fk_grade("The cat sat on the mat.") < 3


def test_style_anchor_and_consistency(client, run_jobs):
    pid = make_book(client, run_jobs, count=10)
    client.post(f"/api/projects/{pid}/generate", json={"count": 2})
    run_jobs()
    revs = [p["current_revision"]["id"] for p in pages_of(client, pid)[:2]]
    st = client.post(f"/api/projects/{pid}/style/anchor", json={"revision_ids": revs}).json()
    assert st["anchor_metrics"]["stroke_width_px"] > 0
    client.post(f"/api/projects/{pid}/generate", json={"count": 2})
    run_jobs()
    p3 = client.get(f"/api/pages/{pages_of(client, pid)[2]['id']}").json()
    assert "style_consistency" in p3["current_revision"]["scores"]
    # reusable library styles
    lib = client.post("/api/styles", json={"project_id": pid, "name": "Bold kids"}).json()
    other = make_book(client, run_jobs, count=10)
    client.post(f"/api/projects/{other}/style/apply/{lib['id']}")
    assert client.get(f"/api/projects/{other}/style").json()["anchor_metrics"] == st["anchor_metrics"]
