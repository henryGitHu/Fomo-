# Agent notes — Coloring Studio

Self-contained app under `coloring-studio/` (the repo root also holds an unrelated crypto scanner; don't touch it).

## Commands
- Backend tests: `cd backend && python -m pytest -q` (offline, mock providers; ~4–5 min; `-k` to narrow)
- Same suite on Postgres: `STUDIO_TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest -q`
- Live provider smoke tests (cost money, need keys): `STUDIO_LIVE_TESTS=1 python -m pytest -m live`
- Run API: `cd backend && uvicorn studio.api.main:app --port 8000`
- Frontend: `cd frontend && npm install && npm run typecheck && npm run build && npm start`
- Browser E2E: backend on :8000 + `next start` on :3000, then `cd frontend && npm run e2e`

## Conventions
- All book state lives in the DB; never keep critical state only in the browser.
- Image changes are new immutable `Revision` rows; never overwrite assets. Respect `Page.locked`
  and approved states everywhere (API, chat editor, jobs).
- Bump `version` on every mutation of pages/sections/projects; pass expected versions from the UI.
- New provider: subclass `providers/base.ImageProvider`, declare honest capabilities/prices
  (`None` when unknown), map errors with `http_error`, register in `providers/registry.py`, add a
  MockTransport contract test in `tests/test_providers.py` and a live smoke parametrisation.
- New schema change: append a migration to `db.MIGRATIONS` (never edit applied ones).
- Never log or return API keys; use `security.redact` for provider error text.
- Don't report a provider as working live unless a `live` test actually ran.
