# Design notes — Veltrix PeopleOps Assistant

## 1. Tables and OCR in ingestion
All formats are normalised to one Markdown dialect (`app/ingestion/parsers.py`), so chunking and holiday extraction see one input shape.

- **PDF**: PyMuPDF `find_tables()` → pipe tables (single-row key/value layouts become two-column tables). Headings come from font size vs. median body size; running headers/footers repeated on every page are stripped; `[Page N]` markers are kept for locators.
- **Scanned pages**: a page with < 50 characters of text layer is rasterised and OCR'd with Tesseract (`ocr.py`) — this is how MEMO-HYBRID-2026 is read.
- **DOCX / HTML**: Title/HeadingN or h1–h6 → `#` headings, tables → pipe tables, list items → `- `.
- **Holidays**: read from calendar pipe tables with a *Date* column (`pipeline.extract_holidays`). Restricted/optional/floating sections are skipped because they do not close the office. Rows go to an app-owned `holidays` table tagged with the source's effective date and access level. No corpus facts are hard-coded.

## 2. Chunking and retrieval — and why
- **Chunking** by heading section (`chunker.py`): sections ≤ 400 tokens are one chunk; longer ones split on paragraphs, and table splits repeat the header row. Each chunk starts with `[title | locator]`. Policy questions map to sections, so this gives precise citations and avoids mixing rules.
- **Embedding/search**: local `all-MiniLM-L6-v2` on CPU (no API dependency, deterministic) with exact FAISS inner-product search. The index is rebuilt only when a fingerprint of manifest + corpus bytes + pipeline version changes.
- **`Retriever.retrieve_context`**: compute eligible and upcoming sets → search only eligible → drop hits below min score → cap chunks per document → fill the 3,000-token budget (45-token label allowance per passage) → reserve ≤ 600 tokens for ≤ 2 relevant upcoming chunks → order by precedence. An assertion and `LLMClient.complete` both enforce the budget; every call logs `corpus_tokens`.

## 3. Section 5 implementation
`app/retrieval/precedence.py`:

- **Eligibility** (`is_document_eligible`): role → access level, region ∈ doc regions or GLOBAL, `effective_date ≤ reference date` (today, or a past date/year named in the question — `dates.extract_reference_date`).
- **Ordering**: `precedence_key` = (authority, scope, −recency); `compare_precedence` reports the deciding test. `resolve_winners` returns all documents tied on every test, reported as a conflict.
- **Upcoming documents**: `upcoming_outranked_by` lists current documents that would still beat an upcoming one on its effective date (it can only lose on authority or scope). This is shown to the LLM, and the handler drops an upcoming citation if a cited document outranks it — e.g. ADD-LEAVE-DE (regional) beats POL-LEAVE-GLOBAL-V3, so a DE employee is not told about V3.
- **Calendars**: `working_days.select_applicable_calendars` reuses the same rules. "Not covered" → `no_answer`; partial answers state the gap.

## 4. Where access control lives
| Concern | File / function |
|---|---|
| Header → employee (401), session owner (403) | `main.chat`, `SessionManager.get_or_create` |
| Leave data per role | `permissions.check_leave_data_access`, called **inside** `get_leave_balance`, `list_leave_requests` |
| Team leave = managers only | `permissions.check_team_leave_access` in `get_team_leave` |
| Writes self-only | `apply_leave`/`cancel_leave` use `requester.employee_id`; `LeaveRepository.cancel_leave_request` re-checks ownership in the transaction |
| Fixed refusal before any tool call | `ChatHandler._resolve_target` / `_check_target` (same text whether or not the employee exists) |
| Documents per role/region/date | `precedence.get_allowed_doc_ids` → `Retriever.retrieve_context` (filter **before** search) |
| Bulk, injection, authority claims | `planner.is_bulk_request`, `planner.detect_guard` |

Role and region always come from the database row for the header, never from the message.

## 5. Personal data in logs and prompts
- **Prompts**: system prompt holds instructions plus requester ID, role and region only. Tool results sent to the LLM carry balances, level and tenure — no names, e-mails or phones.
- **Logs**: `llm_client` logs purpose, provider, model, tokens, latency — never content. The handler logs request ID, employee ID, route, tools and status — never messages. `privacy.PIIRedactingFilter` on root and uvicorn handlers masks e-mails and phones in any record.
- **Answers**: names only for employees the requester may see (`EmployeeRepository.get_safe_name`); phones never returned; LLM output passes `sanitize_answer`.
- **Injected instructions in documents** (e.g. WIKI-NEW-JOINERS) are replaced with a placeholder before reaching the LLM (`retriever.neutralize_injections`).

## 6. Failure modes found and fixed
- **Restricted holidays** were counted as closures → wrong working days. Now excluded by section/table title; a test re-computes every stored request.
- **Running headers** became chunk headings → bad locators. Repeated lines are removed.
- **Validation races**: apply/cancel checks and the write were separate. Checks now repeat inside one `BEGIN IMMEDIATE` transaction.
- **Stale index / missing holidays** after corpus or DB swaps. Fixed by the fingerprint check and holiday re-extraction on start-up.
- **Readiness**: `/health` returned 200 before ingestion, and blocking start-up could not serve 503. Initialisation now runs in a background thread.
- **"Make it until Wednesday"** collapsed the proposal to one day (weekday matcher swallowed "until"). The cue word is now read from the match.
- **Extractive mode** quoted irrelevant passages for uncovered questions. A calibrated threshold applies (irrelevant ≤ 0.42, relevant ≥ 0.46).
- **Planner precision**: wrong regex group in authority-claim parsing; "future balance" fired on non-leave text. Both fixed.

## 7. What breaks first at 10k documents
1. **Ingestion time**: serial parse + OCR + CPU embedding with full rebuilds takes hours, far past the 10-minute start-up limit. Fix: incremental per-file-hash ingestion, an OCR worker pool, index built outside the service.
2. **Search**: filtered search scans a flat index; metadata lives in a pickle. ~50k+ chunks need IVF/HNSW with metadata pre-filtering (pgvector, Qdrant).
3. **Retrieval quality**: near-duplicate versions fill top-k with superseded documents. Needs version-family grouping (keep the precedence winner) and a cross-encoder reranker.
4. **Single-process state**: in-memory sessions and SQLite writes limit the service to one instance; Redis and Postgres would lift that.
