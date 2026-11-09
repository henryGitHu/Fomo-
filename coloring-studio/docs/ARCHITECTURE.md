# Architecture & decisions

## Layout

```
coloring-studio/
  backend/studio/
    config.py          env-based settings (.env supported)
    db.py              engine, sessions, versioned migrations
    models.py          ORM: projects, sections, pages, revisions, assets, jobs, costs, chat, history, exports…
    storage.py         local / S3 asset store with SHA-256 checksums (atomic writes)
    security.py        Fernet encryption of API keys, redaction of secrets in logs/errors
    providers/         base.py (unified interface + typed errors), one adapter per provider, registry.py
    routing.py         manual / smart auto-router / quality-first selection from measured history
    style.py           Visual Style DNA + prompt composition + originality guard
    imaging/           quality.py (objective metrics & checks), enhance.py (print preparation)
    research.py        Smart Theme Research (Wikipedia API, knowledge packs, optional text model)
    knowledge/         curated, referenced knowledge packs (chemical engineering)
    planning.py        section recommendations, allocation, learning progression, page concepts
    captions.py        caption composition + validation (fit, reading level, relevance, safety, source)
    generation.py      per-page pipeline (route → budget → provider w/ retries → store → enhance → QA → commit)
    jobs.py            durable DB job queue, leases, pause/resume/cancel, crash recovery, autopilot
    job_handlers.py    batch generation, line-weight edits, battles, research, benchmarks, exports
    battle.py          AI Image Model Battle
    benchmark.py       standard provider benchmark suite
    editor.py          Live Book Editor (parser → structured commands → executors)
    kdp.py             KDP trim/margin/spine/cover geometry
    pdf_export.py      interior + cover PDFs, PDF inspection, ZIP packaging
    qa_book.py         book-level QA (structure, images, captions, PDFs)
    api/               FastAPI app, routes, serializers
  frontend/            Next.js app (app/ screens, components/, lib/api.ts)
```

## Key decisions

* **Durable queue without Redis.** Jobs live in the database (the source of truth for book
  state anyway). Workers claim jobs with a lease; expired/interrupted jobs are re-queued at
  startup. Batch jobs commit after every page and record `done` page ids, so a resumed job never
  regenerates a finished page. Redis can be added later as a wake-up signal but is not required.
* **Optimistic concurrency.** Pages, sections, projects and styles carry a `version`. A generation
  job remembers the page version it started from; if the page changed (edit, subject change, lock)
  the new image is stored as a *stale alternative revision* and never becomes current.
* **Revisions are immutable.** Original provider output, enhanced output, local edits and battle
  candidates are separate `Revision` rows with their own assets and checksums. Restore/undo just
  move `current_revision_id`.
* **Capability-driven provider requests.** Each model declares capabilities; `map_request` drops
  unsupported parameters and chooses the native size that maximises print PPI.
* **Honest costs.** Prices are per-adapter list-price estimates marked `is_estimate`; unknown prices
  are `None`, shown as "unknown", and require explicit confirmation when a budget is set.
  Budget is checked when queueing *and* again in the worker before each paid call.
* **Objective vs subjective quality.** All scores are computed from pixels (ink coverage, mid-tones,
  stroke width, sharpness, centring, duplicate hashes, style distance to the anchor). No AI
  "vision judge" is configured; the UI states that subjective evaluation was not run.
* **Style consistency across providers** = identical Style DNA text in every prompt + anchor
  images passed as references where the model supports them + measured drift vs. the anchor.
  Prompts alone cannot guarantee identical style; drift is flagged, not hidden.
* **Upscaling is disclosed.** QA reports both the effective PPI of the final asset and the native
  PPI before the (capped, 2×) Lanczos upscale. DPI metadata is never used to claim resolution.
* **Single-sided interior.** Intro pages are padded to an even count so every illustration lands on
  a recto; each is followed by a truly blank verso (verified by PDF inspection).

## Live Book Editor

Deterministic parser (works offline), with optional text-model fallback that emits the same
structured commands. Supported forms include:

| Example | Effect |
|---|---|
| Regenerate page 14 using the OpenAI provider | queued batch for page 14 with provider override |
| Try the same illustration in FLUX and compare the results | battle on the last referenced page |
| Make the lines thicker on pages 6–12 | local morphological edit (no API cost), queued behind running generation |
| Rewrite the caption on page 9 / Set caption on page 3 to: … | caption only; artwork untouched |
| Make Section 3 more technically realistic | updates section instructions; lists affected pages |
| Change page 18 to show a distillation tower | new subject, caption flagged for review, regenerate |
| Keep all approved pages unchanged | locks every approved page |
| Generate the next five illustrations | next batch (confirmation if > $2) |
| Use the highest-rated available provider for the next section | sets section provider from router ranking |
| approve / reject / lock / unlock pages …, undo, restore page N to previous | state changes with history |

Approved pages and expensive or unknown-cost operations require “confirm”. Locked pages are
always skipped and reported.

## KDP figures used (verify before publishing)

Spine = pages × 0.002252 in (white) / 0.0025 in (cream); cover = 0.125 + trim + spine + trim + 0.125
by trim height + 0.25; gutter ≥ 0.375 in (≤150 pages) … 0.875 in (>700); outside ≥ 0.25 in
(0.375 in with bleed); bleed 0.125 in; spine text only above 79 pages; minimum 24 pages.
All fonts are embedded TrueType subsets. Local checks are a preflight, not KDP approval.
