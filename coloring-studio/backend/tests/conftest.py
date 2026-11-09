"""Shared fixtures: every test gets an isolated data dir, database and asset store.

All tests here are SIMULATED (offline mock providers / mocked HTTP) unless marked `live`.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_RESEARCH_WEB", "0")
    monkeypatch.setenv("STUDIO_RUN_WORKER", "0")
    monkeypatch.setenv("STUDIO_TEXT_PROVIDER", "offline")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    pg = os.environ.get("STUDIO_TEST_DATABASE_URL")  # e.g. postgresql+psycopg://studio:studio@localhost/studio_test
    if pg:
        monkeypatch.setenv("DATABASE_URL", pg)
    monkeypatch.delenv("STUDIO_API_TOKEN", raising=False)
    for var in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "BFL_API_KEY", "STABILITY_API_KEY", "REPLICATE_API_TOKEN",
                "FAL_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get("STUDIO_LIVE_TESTS"):
            monkeypatch.delenv(var, raising=False)
    from studio import config, db, generation, security, storage
    from studio.providers import mock, registry
    config.reset_settings()
    db.reset_engine()
    storage.reset_backend()
    security.reset_crypto()
    mock.reset_failures()
    registry._overrides.clear()
    generation.backoff_scale = 0.0
    if pg:
        from sqlalchemy import text
        with db.get_engine().begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    db.migrate()
    yield
    db.reset_engine()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from studio.api.main import create_app
    with TestClient(create_app(start_worker=False)) as c:
        yield c


@pytest.fixture
def run_jobs():
    from studio import jobs
    return jobs.run_pending


def make_book(client, run_jobs, *, count=10, sections=None, theme="chemical engineering", **extra):
    body = {"title": f"Test Book {count}", "theme": theme, "illustration_count": count, "author": "QA Author", **extra}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/research", json={}).status_code == 202
    run_jobs()
    r = client.post(f"/api/projects/{pid}/plan", json={"sections": sections} if sections else {})
    assert r.status_code == 200, r.text
    return pid


def pages_of(client, pid):
    return client.get(f"/api/projects/{pid}/pages").json()


def generate_all(client, run_jobs, pid, batch=10, **kw):
    while True:
        r = client.post(f"/api/projects/{pid}/generate", json={"count": batch, **kw})
        if r.status_code == 409:
            break
        assert r.status_code == 202, r.text
        run_jobs()
