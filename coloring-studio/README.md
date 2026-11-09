# Coloring Studio — AI Coloring Book Publishing Studio

Create, research, illustrate, edit, validate and export original educational
coloring books as Amazon KDP paperback files (interior PDF + full-wrap cover PDF).

* **Backend:** Python 3.11+ · FastAPI · SQLAlchemy 2 (SQLite dev / PostgreSQL prod) · durable DB-backed job queue · Pillow/NumPy image QA · ReportLab PDFs
* **Frontend:** Next.js 15 · React 19 · TypeScript · Tailwind CSS
* **Image providers:** OpenAI (GPT Image), Google (Gemini image / Imagen), Black Forest Labs (FLUX), Stability AI, Replicate, fal.ai — plus two offline **mock** providers for development and tests

> **Status of live integrations:** every real-provider adapter is implemented and
> contract-tested against mocked HTTP, but none has been exercised against the live
> service from this build environment (no credentials, and outbound access to the
> provider hosts was blocked). See [docs/TEST_REPORT.md](docs/TEST_REPORT.md).
> Run the live smoke tests yourself (see below) before relying on a provider.

## Quick start

```bash
cd coloring-studio/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # optional
uvicorn studio.api.main:app --port 8000
```

```bash
cd coloring-studio/frontend
npm install
npm run build && npm start    # or: npm run dev
```

Open http://localhost:3000. The frontend proxies `/api/*` to `STUDIO_API_URL`
(default `http://127.0.0.1:8000`). With no keys configured you can run the whole
workflow on the offline mock providers (images are simple procedural line art —
for testing only, not for publishing).

## Workflow (screens)

1. **Publishing Dashboard** (`/`) – library of books, progress, duplicate / archive / delete.
2. **New Book Setup** (`/new`) – title, theme, age, illustration count, trim, style, captions, budget.
3. **Smart Research** – sources, objectives, original illustration ideas, references, licensing notes, pitfalls.
4. **Section Planner** – recommended section count, exact allocation, learning progression, per-section instructions/provider, add/duplicate/reorder/remove.
5. **AI Model Control Center** (`/providers`) – keys (encrypted at rest), default models, test connection, measured quality/latency, benchmark.
6. **Illustration Studio** – batch generation (1/5/10/custom), status per page, approve / request revision / lock, page detail with QA checks, revisions and restore.
7. **AI Model Battle** – same page across models, objective scores, choose a winner (never silently replaces approved art).
8. **Captions** – Learn-While-You-Color settings, per-page editing and validation.
9. **Book Preview** – physical page spreads with margins, blank versos, cover geometry.
10. **Publish & Export** – book QA, final approval, interior/cover/ZIP/QA report downloads.

Plus **Costs**, **History** (undo) and **Settings** (routing mode, budget, autopilot, fallback, Style DNA & anchors).
The **Live Book Editor** chat is docked on every project screen; see the command list in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#live-book-editor).

## Tests

```bash
cd coloring-studio/backend && python -m pytest -q                       # all simulated tests (~4-5 min)
STUDIO_TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost/studio_test python -m pytest -q   # same suite on PostgreSQL
STUDIO_LIVE_TESTS=1 OPENAI_API_KEY=... python -m pytest -m live -q     # LIVE provider smoke tests (cost money)
```

Browser E2E (needs backend on :8000 and `next start` on :3000):

```bash
cd coloring-studio/frontend && npm run e2e
```

## Deployment notes

* Use PostgreSQL (`DATABASE_URL`), set `STUDIO_SECRET_KEY` and `STUDIO_API_TOKEN`, and put the API behind HTTPS.
* Migrations run automatically at startup (`studio/db.py`, versioned `schema_migrations` table).
* For S3-compatible storage set `STUDIO_STORAGE=s3`, `S3_BUCKET`, `S3_ENDPOINT_URL` and `pip install boto3`.
* Workers: the API process runs one worker thread by default; on PostgreSQL you can run extra
  API/worker processes safely (jobs are claimed with `FOR UPDATE SKIP LOCKED`).
* Backups: back up the database and the data directory (`assets/`) together; every asset row
  stores a SHA-256 checksum that is verified on read.
