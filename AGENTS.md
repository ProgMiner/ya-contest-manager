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
| `make_ignored.py` | Bulk-ignore old Crash submissions | `def main()` |
| `crash_viewer.py` | View / download Crash submissions | `def main()` |
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

Contains `YandexContestAPI` (auth, request with retry, pagination), constants (`BASE_URL`, `TOKEN`, `CONCURRENCY`, `CRASH_VERDICT`), helpers for extracting fields from JSON submission objects, `parse_iso_time`, `sort_key`.

See Quirk 1 in Design Decisions for the trailing-slash / leading-slash invariant.

### `make_ignored.py` — bulk ignore Crash

### `crash_viewer.py` — view / download Crash

Complements make_ignored.py: this script keeps only the latest Crash submission per (user, problem); the other marks all-but-latest as Ignored.

**`--problem` semantics:** numeric ID → matches by `problemId`; alphabetic alias → matches by `problemAlias`. You can mix them.

### `download_contest.sh` — wrapper

Shell script: dry-run → parse lines `Задача <alias> (problemId=…)` via regex → `mkdir -p` → `touch statement.md` (if missing) → `cd solutions/ && crash_viewer.py --save --problem`.

See Quirk 2 in Design Decisions for the stdout format contract.

Env var: `TASKS_DIR` (default `./tasks`) — used by the shell script only, **not** by `review_contest.py` (which uses `--tasks-dir` flag).

### `review_contest.py` — review orchestrator

**Threshold logic:** `CONF_THRESHOLD = 15.0`. `percent ≤ CONF_THRESHOLD` → WA recommendation; `percent ≥ (100 - CONF_THRESHOLD)` → OK recommendation; in between → "N/A" (manual review). All three require operator confirmation in `interactive_review`. The relationship `100 - CONF_THRESHOLD` is the design intent and is not obvious from either literal alone — if you change the constant, re-derive the boundary.

**Ctrl+C:** See Quirk 6 for two-stage handling.

**Timeout/streaming:** Model requests are streamed (SSE, `stream: True`). `call_model` reads chunks via `_read_sse_content`. The `--timeout` value is the max pause **between** chunks; the budget to the **first** chunk is `FIRST_CHUNK_TIMEOUT` (longer, covers queue+prefill) and is not operator-configurable. The session `ClientTimeout(total=None)` disables the total cap — liveness is controlled by the inter-chunk timeout. Timeout log includes alias, attempt number, timeout value, endpoint, and model name.

**`emit_admin_js_split`:** See Quirk 4 for the submission_id security invariant.

**File lifecycle after review:** all decided solutions (OK, WA, and manual-review) are **deleted**; only skipped files remain on disk. `--keep-files` disables deletion.

---

## Tests

Two test suites; run **both** before submitting changes.

| Suite | Command | What it covers |
|---|---|---|
| `test_review_contest_async.py` | `python3 -m unittest test_review_contest_async -v` | `_parse_retry_after`, `call_model` (429/5xx retry, backoff, Retry-After, timeout, 4xx no-retry, `ModelCallError.streams`), `_read_sse_content` (stream EOF, keepalive vs `got_line`, reasoning delta budget switch, `MAX_STREAM_CHARS`, `stream: True` override, tuple return, `reasoning_debug` gating), `review_unit` (verdict parse, retry on garbage, Failure, stream dump on `ModelCallError`), `review_all` (gather, exception containment, cancel propagation), `interactive_review` (+/-/Enter/q/EOF), coroutine signatures, concurrency clamp |
| `test_e2e_review.py` | `python3 test_e2e_review.py` | Full CLI against mock server (port 18999): retry-on-429, concurrency, interactive review, JS generation, file cleanup |

---

## Design Decisions and Quirks

1. **`base_url` trailing slash + leading-slash strip** — aiohttp silently drops the path prefix if `base_url` lacks a trailing `/`. The normalization in `api.py` (`base_url.rstrip("/") + "/"`) is load-bearing; do not remove it. Symmetrically, `request()` strips the leading `/` from paths (`path.lstrip("/")`) because aiohttp's urljoin treats an absolute path as resetting the base prefix. Callers writing their own HTTP calls (like `crash_viewer.fetch_source`) must replicate this lstrip — or pass paths without the leading `/`.

2. **`download_contest.sh` parses `crash_viewer.py` stdout** — the dry-run header format `Задача <alias> (problemId=…): N посылок` is a contract between the two scripts. Changing the header in `crash_viewer.py` breaks the shell regex. Always update both.

3. **`--problem` semantics differ across scripts** — `crash_viewer.py` accepts both numeric IDs and alphabetic aliases; `review_contest.py` matches by directory name only. This is intentional: `review_contest.py` operates on local files, not the API.

4. **`emit_admin_js_split` never trusts model output for `submission_id`** — the model might hallucinate IDs. Only the filename (which was set by `download_contest.sh` from the real API) is used.

5. **`TimeoutError` not retried in `call_model`** — retry ownership belongs to `review_unit`, which controls the attempt budget. This prevents double-counting retries.

6. **Ctrl+C two-stage handling** — first press is graceful (cancels tasks, collects `Failure(CANCEL_REASON)` results); second press force-exits. The handler is removed after `review_all` completes so `interactive_review` gets standard `KeyboardInterrupt`.

7. **1-based pagination** — `page=0` causes HTTP 500. This is a Yandex Contest API quirk; the code always starts from page 1.

8. **`/source` endpoint returns raw bytes** — not JSON. Using `api.request()` will fail; must use `api.session.request()` with `headers={"Accept": "*/*"}` and `response.read()`. Empty body (`b""`) is valid (submission exists but has no source).

9. **`TASKS_DIR` env var vs `--tasks-dir` flag** — the env var is read only by `download_contest.sh` (shell); `review_contest.py` reads `--tasks-dir` flag (Python). They default to the same value but are configured differently.

10. **File lifecycle: delete OK/WA/manual** — destructive by default. Manual-review solutions are deleted too (operator uses admin-panel URLs to view them). `--keep-files` is the escape hatch. This design prevents re-reviewing already-processed solutions.

11. **SSE streaming, not a single JSON body** — `call_model` sends `stream: True` and parses `data:` frames. The `if not line: break` check in `_read_sse_content` is mandatory: EOF without `[DONE]` must terminate the loop, or the reader spins hot. Streaming is mandatory for call_model; `stream: True` is always forced regardless of `MODEL_OPTIONS` contents. The `got_line` flag that controls the timeout budget switch (`FIRST_CHUNK_TIMEOUT` → `INTER_CHUNK_TIMEOUT`) is set upon receiving **any** SSE data frame with a non-empty `choices` array — this includes reasoning-only deltas (`reasoning_content` without `content`), which is the core purpose of the streaming design: the model is alive when it thinks, even before producing visible output. SSE comment lines (`: keepalive`) and empty separator lines do **not** set `got_line` — they carry no `choices` and prove nothing about model liveness.

    `_read_sse_content` returns `tuple[str, dict[str, str] | None]` — the concatenated `"content"` key (or `""`) and the `streams` dict. Internally it accumulates all string-valued keys of each delta/message into `streams: dict[str, str]` (incremental concatenation). Reasoning output (non-`content` keys with `"reasoning"` in the name) is gated by `reasoning_debug`: when **False** (default), reasoning is silently accumulated into `streams` without printing; when **True**, it is printed in blocks as the accumulated value exceeds `REASONING_THRESHOLD` (preview truncated at last whitespace before threshold, remainder kept), and remaining tails are printed at stream end. After printing tails, `streams` is set to `None` (consumed), so the caller receives no leftover dict. When `reasoning_debug=False`, `streams` is returned intact so callers can inspect it.

    `ModelCallError` carries an optional `streams` attribute (the `streams` dict from `_read_sse_content`). When `call_model` raises `ModelCallError` (empty content, stream overflow, etc.), any accumulated streams are attached so the caller can log them. `review_unit` exploits this: on `ModelCallError`, if `exc.streams is not None`, each key-value pair is printed as `--- Лог {key} --- / value / --- Конец лога ---`.

12. **Exception-handler order in `call_model`**: `except TimeoutError: raise` must come **before** `except aiohttp.ClientError`. Load-bearing because `SocketTimeoutError` inherits from both.

13. **`FIRST_CHUNK_TIMEOUT` vs `INTER_CHUNK_TIMEOUT`** — the first-chunk timeout is deliberately longer than the inter-chunk timeout (covers queue+prefill). The switch is triggered by `got_line` (see Quirk 11 for semantics).

14. **`download_contest.sh` creates empty `statement.md` → `discover_units` skips it** — the shell script `touch`es `statement.md` if missing, producing an empty file. `review_contest.py`'s `discover_units` skips tasks where `statement.md` is empty, so freshly-seeded tasks are **always skipped** until the user fills in the statement. This is intentional but the cross-module interaction is non-obvious.

15. **`crash_viewer.fetch_source` has its own retry logic** — independent from `api.py`'s retry. Changing retry parameters in `api.py` will **not** affect `crash_viewer`'s download path. Callers writing new source-fetching code must implement their own retry.

---

## Doc Conventions

- **README.md** — minimal user-facing docs: what the project does, requirements, env vars, one-line launch examples. Detailed options are available via `--help`.
- **AGENTS.md** (this file) — non-obvious design intent, cross-module contracts, API gotchas, security invariants, behavioral quirks, and test instructions. Everything else the agent can read from the code.
- **No duplication.** A fact lives in one file. Cross-references are allowed.
- **Constants by name, not value** — reference constant names; values live only in code.
- **When adding new information, update this file.** When discovering a new quirk or design decision, add it to the "Design Decisions and Quirks" section.
- **Keep minimal.** An oversized AGENTS.md wastes the agent's context window. If in doubt whether something belongs here, ask: "Would an agent modifying this code need to know this to avoid introducing a bug?" If not, leave it out.
