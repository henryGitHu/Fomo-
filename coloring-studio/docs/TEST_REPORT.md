# Test report

Run date: 2026-11-09, Linux, Python 3.13, Node 22, Chromium (Playwright 1.63).

**Every test below is SIMULATED** unless it says otherwise: images come from the offline
mock providers (`mock`, `mock_alt`), and the real provider adapters are tested against
mocked HTTP. **No live provider call was made.** This environment had no provider
credentials, and the network policy blocked the provider hosts and Wikipedia.

## Results

| Suite | Result |
|---|---|
| Backend pytest, SQLite | **71 passed, 6 skipped** (4 min 7 s) |
| Backend pytest, PostgreSQL 16 (`STUDIO_TEST_DATABASE_URL`, 40-page book test excluded for time) | **70 passed, 6 skipped** |
| Frontend `tsc --noEmit` + `next build` | pass |
| Browser E2E (`frontend/e2e/run-e2e.mjs`, real UI + real API + worker) | **15/15 steps passed** |
| Live provider smoke tests (`-m live`) | **NOT RUN.** Blocked: no API keys, and the network blocked the provider hosts |

The 6 skipped tests are the live provider smoke tests (OpenAI, Google, BFL, Stability, Replicate, fal).

## Spec tests → where they are covered

| Spec test | Test(s) | Status |
|---|---|---|
| A Multi-provider integration (live) | `test_providers.py::test_A_live_provider_smoke` | **Blocked** (credentials). Contract tests for all 6 adapters pass on mocked HTTP: request shape, unsupported-param filtering, async polling, response parsing, error mapping (auth, rate-limit, quota, safety, 5xx, timeout) |
| B Provider switching | `test_generation.py::test_B_…` | pass (mock ↔ mock_alt) |
| C Model comparison | `test_C_model_battle_does_not_silently_replace`, `test_C_battle_cannot_replace_locked` | pass |
| D 40-illustration book (84 pages) | `test_full_book.py` | pass |
| E Batch approval | `test_E_batch_approval_reject_and_edit` | pass |
| F Targeted chat edit (page 17 checksums) | `test_editor.py::test_F_…` | pass |
| G Provider failure recovery | `test_G_provider_failure_retry_and_recovery`, `test_G_fallback_respects_budget` | pass |
| H Persistence | `test_research_security.py::test_H_…` + E2E reload step | pass |
| I KDP export PDF inspection | `test_full_book.py` (page count, 8.5×11 dimensions, embedded fonts, art on rectos, blank versos, cover geometry, ZIP) | pass |
| J Interrupted generation | `test_J_interrupted_generation_resumes_without_duplicates`, `test_pause_and_resume` | pass |
| K Locked pages | `test_editor.py::test_K_…` + E2E | pass |
| L Dynamic sections 20/40/60/80/100 | `test_planning.py::test_L_dynamic_sections[*]` | pass |
| M Spending limits | `test_M_spending_limits`, `test_autopilot_continues_until_done_or_budget` | pass |
| N Originality & research | `test_N_…`, `test_originality_guard_…`, `test_web_research_uses_official_api_…` (mocked Wikipedia) | pass. Live web research was **blocked** by the network |

## Known limitations

* Live image generation, live research and text-model synthesis are unverified. Before
  publishing with a provider, run `STUDIO_LIVE_TESTS=1 python -m pytest -m live`.
* Provider prices are list-price estimates written into the adapters. They must be checked
  against the providers' current pricing pages. Unknown prices are shown as "unknown".
* Mock-provider artwork is simple procedural line art for testing workflows. It is not suitable for publishing.
* Text and watermark detection in images is not implemented (no OCR engine is configured). QA reports this explicitly.
* Optional vectorisation requires `potrace` on the host. It is reported as unavailable otherwise.
* Subjective "AI vision judge" scoring is not configured. Battle and benchmark scores are objective pixel metrics only.
* Covers are colourised automatically from approved line art (procedural flood fill). Generating dedicated AI cover art is not wired up yet.
* Only the chemical-engineering topic has a curated, referenced knowledge pack. Other topics fall
  back to web research, then a text model, then generic templates. Template output is flagged for human review.
* KDP figures follow KDP's published guidance as known at build time. The local preflight does not guarantee KDP approval.
