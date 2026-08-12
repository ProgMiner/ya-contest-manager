# Yandex Contest Manager — AGENTS.md

Instructions for developers and AI agents modifying this codebase.

**README.md** contains minimal user-facing docs: what the project does, requirements, env vars, one-line launch examples. Detailed options and exit codes are available via `--help` and in the source code — do not duplicate them here.

> **Maintenance rules:**
> - When you discover a new design decision, quirk, or invariant not documented here, you MUST add it to this file. Stale or missing AGENTS.md causes avoidable bugs.
> - Keep this file **minimal**. Only document what the source code cannot easily reveal: non-obvious design intent, cross-module contracts, API gotchas, security invariants, and behavioral quirks. Do not clutter the agent's context with facts it can read directly from the code (CLI flags, defaults, obvious data structures, etc.).

---

## Project Files

| File | Purpose | Entry point |
|---|---|---|
| `api.py` | Shared module: `YandexContestAPI`, retry, pagination, helpers | — (imported) |
| `make_ignored.py` | Bulk-ignore old Crash submissions | `async def main()` |
| `crash_viewer.py` | View / download Crash submissions | `async def main()` |
| `download_contest.sh` | Wrapper over `crash_viewer.py` | direct execution |
| `review_contest.py` | LLM-based contest review | `async def main()` |
| `review_prompt.md` | System prompt for the model | — (read by `review_contest.py`) |
| `test_review_contest_async.py` | Unit tests for async logic of `review_contest.py` | `python3 -m unittest test_review_contest_async -v` |
| `test_e2e_review.py` | E2E test: CLI vs mock server (port 18999) | `python3 test_e2e_review.py` |

---

## Yandex Contest API v2 (Public)

### Authentication
OAuth token in `Authorization: OAuth {token}` header. Env var: `YANDEX_CONTEST_TOKEN`.

### Pagination
**Pages are 1-based!** `page=0` triggers HTTP 500.

### Key Endpoints

- `GET /contests/{contestId}` — contest info (non-critical on error)
- `GET /contests/{contestId}/submissions?page=1&pageSize=100` — submissions with pagination. Response: `{"count": N, "submissions": [...]}`
- `GET /contests/{contestId}/submissions/{submissionId}/source` — source code, returns `application/octet-stream` (raw bytes, NOT JSON). **Do not use `api.request()`** — use `api.session.request()` with `headers={"Accept": "*/*"}` and manual `response.read()`. Empty body (`b""`) is valid. Binary submissions detected by null bytes or UTF-8 decode failure.
- `POST /submissions/{submissionId}/rejudge` — rejudge

### Important Notes
- `problemId` is a **string**, not a number
- Verdict is a string — compare **case-insensitive**
- When `submissionTime` values tie, all such submissions are treated as "latest"; a warning is printed
- HTTP 5xx retried with exponential backoff (3 retries, 1s → 2s → 4s; up to 4 total requests)
- **Verdict cannot be changed via the public API** — only via the admin panel (see Admin API below)

---

## Admin API (Yandex Contest Admin Panel)

Verdict changes require the internal admin-panel API. The scripts cannot call it directly (needs a browser-session CSRF token), so they generate JavaScript for the user to paste into DevTools console.

```
PATCH /api/admin/contest/{contestId}/submission/verdict
     ?verdict=Ignored
     &filter=runId%3D{submissionId}
```

CSRF token extracted from `<meta name="secretkey">` or cookie, header: `x-csrf-token`. `submissionId` from the public API = `runId` in the admin filter.

---

## Code Architecture

### `api.py` — shared module for Yandex Contest API

Contains `YandexContestAPI` (auth, request with retry, pagination), constants (`BASE_URL`, `TOKEN`, `CONCURRENCY`, `CRASH_VERDICT`), helpers for extracting fields from JSON submission objects, `parse_iso_time`, `sort_key`. `BASE_URL` is a hardcoded constant — not configurable via env vars or CLI flags.

**Quirk — trailing slash required:** `base_url` must end with `/` — aiohttp silently drops the path prefix otherwise (see `api.py:49-53`). The `base_url.rstrip("/") + "/"` normalization is intentional; do not remove it.

### `make_ignored.py` — bulk ignore Crash

Imports from `api.py`. For each (user, problem) pair, marks all Crash submissions as Ignored **except** the latest by `submissionTime`. Generates JavaScript for the admin console.

### `crash_viewer.py` — view / download Crash

Imports from `api.py`. Inverse filter of `make_ignored.py`: for each (user, problem), keeps **only** the latest Crash submission. Users with any submission in the last N hours are excluded (still active).

**`--problem` semantics:** numeric ID → matches by `problemId`; alphabetic alias → matches by `problemAlias`. You can mix them.

### `download_contest.sh` — wrapper

Shell script: dry-run → parse lines `Задача <alias> (problemId=…)` via regex → `mkdir -p` → `cd solutions/ && crash_viewer.py --save --problem`.

**Quirk — stdout format contract:** `crash_viewer.py`'s dry-run output format (`Задача <alias> (problemId=…): N посылок`) is parsed by regex in this shell script. If you change the header format in `crash_viewer.py`, you **will** break the shell parser. Any header change requires updating the regex in `download_contest.sh`.

Env var: `TASKS_DIR` (default `./tasks`) — used by the shell script only, **not** by `review_contest.py` (which uses `--tasks-dir` flag).

### `review_contest.py` — review orchestrator

Does **not** make requests to the Yandex Contest API. Works with local files and sends requests to an OpenAI-compatible API.

**Data flow:** `discover_units()` → `review_all()` (concurrent via `asyncio.gather`) → `interactive_review()` → `emit_admin_js_split()` + `emit_manual_urls()` → `write_report()` → `cleanup(ok+wa)` + `park_manual(manual)`.

**Model verdict:** JSON `{"percent": 0-100, "summary": "...", "remarks": [...]}`. Parsing via `parse_verdicts` with fallback strategies and `validate_payload` (type coercion).

**Threshold logic:** `CONF_THRESHOLD = 15.0`. `percent ≤ CONF_THRESHOLD` → WA recommendation (requires operator confirmation); `percent ≥ (100 - CONF_THRESHOLD)` → auto-OK (no operator prompt); in between → "N/A" (manual review). The relationship `100 - CONF_THRESHOLD` is the design intent and is not obvious from either literal alone — if you change the constant, re-derive the boundary.

**Key constants (values in code, names only here):** `REQUEST_TIMEOUT`, `MAX_ATTEMPTS`, `API_CONCURRENCY`, `API_MAX_RETRIES`, `API_RETRY_DELAY`, `RETRY_BACKOFF`, `RETRY_AFTER_CAP`, `CANCEL_REASON`.

**Report:** JSON file `review_report.json` (default, `REPORT_FILE_DEFAULT`) written **before** file mutation; contains all verdicts (ok/wa/manual/skipped) and errors.

**MODEL_OPTIONS:** dict with model request parameters (temperature, reasoning_effort, prompt_cache_key) — values in code (`review_contest.py`, constant `MODEL_OPTIONS`).

**Ctrl+C:** First → cancels in-flight requests via `task.cancel()` + `cancel.set()`, pending tasks → `Failure(CANCEL_REASON)`. Second → `os._exit(130)`. After `review_all`, the SIGINT handler is removed; standard `KeyboardInterrupt` is restored for `interactive_review`.

**Timeout:** `TimeoutError` is **not** retried inside `call_model` — retry happens at the `review_unit` level, which owns the attempt budget. Timeout log includes alias, attempt number, timeout value, endpoint, and model name.

**`emit_admin_js_split`:** `submission_id` is taken **only** from `unit` (filename), **never** from the model response. This is a security invariant — the model must not control which submission gets a verdict applied.

**`discover_units` skip predicate:** a task is skipped if: `statement.md` is empty or missing; `solutions/` directory does not exist; filename is not numeric; dot-files (e.g. `.DS_Store`); subdirectories inside `solutions/`. This affects user-visible behavior — if you change the predicate, update Troubleshooting in README.

**File lifecycle after review:** OK/WA solution files are **deleted**; solutions deferred to manual review are **moved** to `tasks/<alias>/manual/`. `--keep-files` disables both. The report is written **before** any file mutation, so even if the script crashes mid-cleanup, the verdicts are preserved.

---

## Tests

Two test suites; run **both** before submitting changes.

| Suite | Command | What it covers |
|---|---|---|
| `test_review_contest_async.py` | `python3 -m unittest test_review_contest_async -v` | `_parse_retry_after`, `call_model` (429/5xx retry, backoff, Retry-After, timeout), `review_unit` (verdict parse, retry on garbage, Failure), `review_all` (gather, exception containment), coroutine signatures, concurrency clamp |
| `test_e2e_review.py` | `python3 test_e2e_review.py` | Full CLI against mock server (port 18999): retry-on-429, concurrency, interactive review, JS generation, file cleanup. Creates artifacts in `.test_tmp/`, server script `.test_e2e_server.py`, log `.test_e2e_log.json` |

E2E test requires port 18999 to be free.

---

## Design Decisions and Quirks

1. **`base_url` trailing slash** — aiohttp silently drops the path prefix if `base_url` lacks a trailing `/`. The normalization in `api.py` (`base_url.rstrip("/") + "/"`) is load-bearing; do not remove.

2. **`download_contest.sh` parses `crash_viewer.py` stdout** — the dry-run header format `Задача <alias> (problemId=…): N посылок` is a contract between the two scripts. Changing the header in `crash_viewer.py` breaks the shell regex. Always update both.

3. **`--problem` semantics differ across scripts** — `crash_viewer.py` accepts both numeric IDs and alphabetic aliases; `review_contest.py` matches by directory name only. This is intentional: `review_contest.py` operates on local files, not the API.

4. **`emit_admin_js_split` never trusts model output for `submission_id`** — the model might hallucinate IDs. Only the filename (which was set by `download_contest.sh` from the real API) is used.

5. **Report written before file mutation** — ensures crash-safety. The verdicts survive even if cleanup is interrupted.

6. **`TimeoutError` not retried in `call_model`** — retry ownership belongs to `review_unit`, which controls the attempt budget. This prevents double-counting retries.

7. **Ctrl+C two-stage handling** — first press is graceful (cancels tasks, collects `Failure(CANCEL_REASON)` results); second press force-exits. The handler is removed after `review_all` completes so `interactive_review` gets standard `KeyboardInterrupt`.

8. **1-based pagination** — `page=0` causes HTTP 500. This is a Yandex Contest API quirk; the code always starts from page 1.

9. **`/source` endpoint returns raw bytes** — not JSON. Using `api.request()` will fail; must use `api.session.request()` with `headers={"Accept": "*/*"}` and `response.read()`. Empty body (`b""`) is valid (submission exists but has no source).

10. **`TASKS_DIR` env var vs `--tasks-dir` flag** — the env var is read only by `download_contest.sh` (shell); `review_contest.py` reads `--tasks-dir` flag (Python). They default to the same value but are configured differently.

11. **File lifecycle: delete OK/WA, park manual** — destructive by default. `--keep-files` is the escape hatch. This design prevents re-reviewing already-processed solutions.

---

## Doc Conventions

- **README.md** — minimal user-facing docs: what the project does, requirements, env vars, one-line launch examples. Detailed options are available via `--help`.
- **AGENTS.md** (this file) — non-obvious design intent, cross-module contracts, API gotchas, security invariants, behavioral quirks, and test instructions. Everything else the agent can read from the code.
- **No duplication.** A fact lives in one file. Cross-references are allowed.
- **Constants by name, not value** — reference constant names; values live only in code.
- **When adding new information, update this file.** When discovering a new quirk or design decision, add it to the "Design Decisions and Quirks" section.
- **Keep minimal.** An oversized AGENTS.md wastes the agent's context window. If in doubt whether something belongs here, ask: "Would an agent modifying this code need to know this to avoid introducing a bug?" If not, leave it out.
