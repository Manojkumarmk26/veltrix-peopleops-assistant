# Startup Guide

How to install, configure, run and check the Veltrix PeopleOps Assistant. For what the project does and how it is built, see [PROJECT_EXPLANATION.md](PROJECT_EXPLANATION.md). For the design rationale, see [DESIGN.md](../DESIGN.md).

| Step | What |
|---|---|
| 1 | [Get the code and the data](#1-get-the-code-and-the-data) |
| 2 | [Configure the LLM](#2-configure-the-llm) |
| 3 | Run: [Docker](#3a-run-with-docker) · [Windows + Conda](#3b-run-locally-on-windows-conda-inside-the-project) · [Linux / macOS](#3c-run-locally-on-linux--macos-venv) |
| 4 | [Check it works](#4-check-it-works) |
| 5 | [Run the tests](#5-run-the-tests) |
| 6 | [Troubleshooting](#6-troubleshooting) |

---

## 1. Get the code and the data

```bash
git clone <repository-url> veltrix-peopleops-assistant
cd veltrix-peopleops-assistant
```

The data is **not** in the repository (the brief says the reviewers supply their own). Put it in `./data`:

```
data/
  corpus/          # the 30 policy documents (PDF, DOCX, HTML, MD)
  manifest.json
  veltrix.db
```

No database set-up step is needed. At start-up the service adds its own tables (`holidays`, `ingestion_state`), extracts the holidays from the calendars, and builds the search index in `data/vector_store/`.

> [!NOTE]
> The service **writes to `data/veltrix.db`**: it adds tables, and confirmed leave requests and cancellations are saved. To keep the original intact, run against a copy (see [Reset the data](#reset-the-data)).

---

## 2. Configure the LLM

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

Edit `.env` and set **one** provider:

| Provider | Settings in `.env` |
|---|---|
| OpenAI | `LLM_PROVIDER=openai`, `OPENAI_API_KEY=sk-...` (model default `gpt-4o-mini`) |
| Anthropic | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` (default `claude-3-5-haiku-latest`) |
| Google Gemini | `LLM_PROVIDER=google`, `GOOGLE_API_KEY=...` (default `gemini-2.0-flash`) |
| OpenRouter (free models, for development) | `LLM_PROVIDER=openai`, `OPENAI_API_KEY=sk-or-...`, `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, `LLM_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free`, `OPENAI_EXTRA_BODY={"reasoning": {"enabled": false}}` |

Optional: `APP_TODAY=2026-10-01` fixes "today", so the date rules give repeatable answers.

Every variable is documented in [.env.example](../.env.example). `.env` is git-ignored; never commit a real key.

> [!IMPORTANT]
> Variables set in your shell **override** `.env`. If the start-up log says `LLM configured: False` even though `.env` has a key, a stale shell variable is the usual cause. Clear it (`Remove-Item Env:OPENAI_API_KEY` in PowerShell, `unset OPENAI_API_KEY` in bash) and restart.

Without any key, the service still runs: tool, clarify and refuse routes work normally, and document questions are answered by quoting the most relevant permitted passages.

---

## 3a. Run with Docker

This is the path the reviewers use.

```bash
docker compose up --build
```

- The service listens on **port 8000**. `./data` is mounted at `/app/data`.
- `GET /health` returns **503** while ingestion runs, then **200** `{"status":"ok"}`.
- The embedding model and tokenizer are baked into the image, so start-up needs no downloads.
- Stop with `Ctrl+C`, or `docker compose down`.

> [!NOTE]
> The Docker path has not been built on the development machine. The same application code was verified under uvicorn (sections 3b and 4).

---

## 3b. Run locally on Windows (Conda, inside the project)

Prerequisites:
- Anaconda or Miniconda.
- **Tesseract OCR** on `PATH`, needed to read the scanned memo. Installer: https://github.com/UB-Mannheim/tesseract/wiki

**One-time set-up.** This creates `conda_env/` and keeps every cache inside the project folder, so nothing large lands on C:.

```powershell
cd D:\myproject\veltrix_assessment\veltrix-peopleops-assistant
powershell -ExecutionPolicy Bypass -File scripts\setup_conda_env.ps1
```

**Start the API.** Use a fresh PowerShell window:

```powershell
cd D:\myproject\veltrix_assessment\veltrix-peopleops-assistant
$env:HF_HOME = "$PWD\.hf_cache"          # embedding model stored in the project
$env:PYTHONIOENCODING = "utf-8"
# optional: run against a copy so data\veltrix.db stays pristine
Copy-Item data\veltrix.db data\veltrix_dev.db -Force
$env:DB_PATH = "data/veltrix_dev.db"

.\conda_env\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The first start builds the index, which took about 75 s on the development laptop. Later starts reuse it (about 8 s). Wait for:

```
Service ready in ...s (LLM configured: True)
```

---

## 3c. Run locally on Linux / macOS (venv)

```bash
sudo apt-get install -y tesseract-ocr     # macOS: brew install tesseract
python3.11 -m venv .venv && source .venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Use **one worker**: sessions and pending confirmations are held in process memory.

---

## 4. Check it works

Interactive API docs are at **http://127.0.0.1:8000/docs**. Use "Try it out" on `POST /chat` and put an employee ID such as `E2004` in the `X-Employee-Id` header.

**PowerShell:**

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health            # {"status":"ok"} when ready

$h = @{ "X-Employee-Id" = "E2004" }
Invoke-RestMethod http://127.0.0.1:8000/chat -Method Post -Headers $h -ContentType "application/json" `
  -Body '{"session_id":"demo-1","message":"What is my leave balance?"}' | ConvertTo-Json -Depth 5
```

**bash:**

```bash
curl -s localhost:8000/health
curl -s localhost:8000/chat -H "Content-Type: application/json" -H "X-Employee-Id: E2004" \
     -d '{"session_id":"demo-1","message":"What is my leave balance?"}'
```

**Expected results** (data as provided, `APP_TODAY=2026-10-01`):

| Header | Message | Expected `route` and result |
|---|---|---|
| E2004 | What is my leave balance? | `tool`: annual 3.5, sick 12 |
| E2004 | How many sick days do I get per year? | `rag`: 12 days, cites POL-LEAVE-GLOBAL-V2 |
| E2004 | What was the sick leave allowance in 2025? | `rag`: 10 days, cites POL-LEAVE-GLOBAL-V1 |
| E4004 | How many days can I carry forward? | `rag`: ADD-LEAVE-DE (10 days, use by 31 March); V3 not mentioned |
| E2004 | How many annual leave days will I have by December? | `hybrid`: 3.5 + 3 × 1.75 = 8.75 |
| E2004 | Is 2 October a public holiday for me? | `rag`: yes, Gandhi Jayanti (CAL-HOL-IN-2026) |
| E2004 | I want to apply for leave next week | `clarify`: asks the leave type → reply `sick` → proposal → reply `yes` → `apply_leave` success |
| E2004 | What is E2005's leave balance? | `refuse`: "I can't share information about that employee." |
| E2002 | Who on my team is on leave in December? | `tool`: `get_team_leave` (managers only) |
| E2004 | What are the salary bands for L5? | `no_answer` (HR-only document) |
| E2004 | How do I reset my laptop password? | `refuse` (not an HR topic) |

Use the **same `session_id`** for follow-up turns such as "sick" and "yes".

**Expected error codes:**
- **401**: no `X-Employee-Id` header, or an unknown ID.
- **400**: malformed JSON, or a missing `session_id` or `message`.
- **403**: a `session_id` that belongs to another employee.

### Reset the data

```powershell
Copy-Item data\veltrix.db data\veltrix_dev.db -Force      # then restart the API
```

---

## 5. Run the tests

```powershell
$env:HF_HOME = "$PWD\.hf_cache"
.\conda_env\python.exe -m pytest              # 140 tests
# or: make test PYTHON=./conda_env/python.exe
```

On Linux / macOS: `make test`, or `python -m pytest`.

The tests:
- use a temporary copy of `data/veltrix.db` and never modify `./data`;
- set `APP_TODAY=2026-10-01`;
- replace the LLM with a deterministic fake, so no network or key is needed.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LLM configured: False` although `.env` has a key | A shell variable overrides `.env` (often a placeholder such as `sk-...`). Clear it and restart. Placeholders count as "not configured". |
| `/health` stays 503 | Ingestion is still running; watch the log. If it shows `Initialisation failed`, check `CORPUS_DIR`, `MANIFEST_PATH` and `DB_PATH`. |
| `OCR failed` / the hybrid-work memo is never cited | Tesseract is not on `PATH` (local runs only). |
| Policy answers are quotes instead of prose | No LLM key configured, or the LLM call failed or timed out. Check the log for `LLM unavailable`. |
| `LLM call exceeded deadline` in the log | The provider took longer than `LLM_DEADLINE_SECONDS` (18 s). The answer falls back to quoted passages. Common with free models at busy times. |
| `404 model not found` from the provider | Set `LLM_MODEL` to a current model name. |
| `MemoryError` at start-up | Not enough free RAM. The service needs about 0.6–1 GB, so close other heavy programs. |
| Port 8000 already in use | Another server is running. Stop it with `Ctrl+C`, or find it: `Get-NetTCPConnection -LocalPort 8000`. |
| Garbled characters in redirected output | `$env:PYTHONIOENCODING = "utf-8"`. |
