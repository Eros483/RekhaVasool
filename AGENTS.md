# AGENTS.md — RekhaVasool (Razorpay Guarded Agentic Checkout + Recovery)

> Source of truth: `docs/spec.md`. This file is an implementation index — spec wins on conflicts.

## Project Overview
Guarded agentic checkout for **1 merchant (freshmart), 3 SKUs, ₹5k fixed mandate**. A Gemini buyer agent emits only `{sku,qty}` intent; a deterministic Python policy engine (`policy/authorize.py:18`) gates every money action via HMAC mandate verification, allowlist, expiry, catalog version, canonical price, and injection checks. Razorpay test-mode + MCP creates Payment Links; `audit.jsonl` hash-chains every decision. A rule-based recovery loop handles `payment.failed` → retry → WA nudge to **+91-9560452773** → dashboard `₹ recovered`. For Razorpay Buildathon tracks 01 Agentic Commerce + 03 Revenue Recovery. Problem: scoped authorization + prompt-injection resistance + recoverable payments.

## Development Philosophy
- Deterministic policy is source of truth — LLM never decides amount (`catalog/freshmart.json` canonical price only). Ponytail: `// ponytail: HMAC for demo, Ed25519/JWS per AP2 spec if prod matters`
- TDD for policy/harness: `20/20` red-team must pass with `GEMINI_ENABLED=false` (regex alone).
- Thin web layer — logic lives in `policy/` and `recovery/`, never in `web/app.py`.
- Explicit over clever; boring over clever; fewest files possible (Python-only, no Node).
- If it isn't runnable via `make`, it isn't done.
- YAGNI per spec §1.3: no multi-merchant crawler, no A2A negotiation, no real UAP/RBI rail, no live telephony, no full AP2 JWS, no recon engine, no prod WABA/DLT.

## Tech Stack
- Runtime: **Python 3.11+ / FastAPI + Jinja** + SQLite + stdlib `hmac`/`hashlib`/`sqlite3`. No Node.
- LLM: **Gemini 2.0 Flash** via `google-generativeai` (free tier, 15 RPM). Buyer intent only + optional `is_injected` second-opinion; recovery is rule-based (no LLM) to avoid 50-batch throttling.
- Razorpay: `rzp_test_*` + `api.razorpay.com/v1` + `@razorpay/mcp` — `POST /v1/payment_links`, `GET /v1/payment_links/{id}` (polling fallback every 2s/15s), `POST /v1/payment_links/{id}/cancel`, webhooks `payment_link.paid`/`payment.failed`.
- WA: **Meta WhatsApp Cloud API test number**, whitelisted **+91-9560452773** only (`hello_world` or raw `text` inside 24h window). No custom template approval.
- Voice: **Mocked via `gTTS`** generated on-demand, labeled `mock — live via Sarvam+Exotel in prod`, 0 bytes checked in.
- Package manager: `uv`. Formatter: `black`, Linter: `ruff`. Build/run: `make`.

## Key Commands
All via `make` from repo root. Never call `uvicorn`/`pytest` directly.

```bash
make setup   ## sync deps (uv sync), create .env from .env.example if missing
make dev     ## uvicorn web/app.py --reload (single server; no frontend split)
make test    ## pytest tests/harness.py -v  (must show 20/20) + any unit tests
make style   ## black . && ruff check --fix .
make build   ## no-op / collect checks (Python-only, no bundle)
make clean   ## rm -rf __pycache__ .pytest_cache audit.jsonl recovery.db logs/
make harness ## GEMINI_ENABLED=false pytest tests/harness.py -v
```

## Directory Structure
Spec §3.2 — fewest files, Python-only. Do not add `frontend/` or `backend/` splits.

```
.
├── catalog/freshmart.json          # 3 SKUs, catalog_version 1.0, paise prices
├── policy/
│   ├── mandate.py                  # sign/verify HMAC-SHA256 (stdlib hmac)
│   └── authorize.py                # authorize(intent,mandate,catalog) — 8 checks in order
├── agent/
│   └── buyer.py                    # Gemini tool-calling → intent {sku,qty} (no amount); inline MCP config :12
├── web/
│   └── app.py                      # FastAPI: catalog + approve mandate + audit view + /dashboard
├── recovery/
│   └── orchestrator.py             # webhook + poll fallback + WA sender + stopping rules
├── tests/
│   ├── redteam.jsonl               # 20 attacks, 5×4 categories incl. ₹500001 boundary
│   └── harness.py                  # runner → 20/20, GEMINI_ENABLED=false support
├── audit.jsonl                     # append-only hash chain (prev_hash = sha256(prev_line))
├── recovery.db                     # SQLite — recovery_attempts table
├── .env.example                    # RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, GEMINI_API_KEY, MANDATE_SECRET
├── Makefile
└── docs/spec.md
```

Data models: see spec §4.1–4.5 (`catalog`, `mandate` JWT HS256, `intent`, `audit.jsonl` entry, `recovery_attempts`).

## Conventions

### Makefile (required)
- Root `Makefile` is mandatory, canonical control surface. Required targets: `setup`, `dev`, `test`, `style`, `build`, `clean` (plus `harness` helper).
- Each target thin-wraps `uv run ...`. `make setup` idempotent. `make dev` single FastAPI process (no concurrent frontend/backend).
- Every target has `## description` for `make help` / grep.

### Python
- `uv` only (`uv add`, `uv run`, `uv sync`). Never `pip`.
- `black` + `ruff` (incl. import sort). `snake_case` for files/vars/funcs/DB cols.
- `web/app.py` thin: validate → call `policy.authorize` / `recovery.orchestrator` → return. `policy/` and `recovery/` have zero FastAPI imports.
- Env via `utils/config.py` Pydantic `BaseSettings` — never `os.environ` elsewhere. Keys: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `GEMINI_API_KEY`, `MANDATE_SECRET`, optional `GEMINI_ENABLED`, `WA_PHONE_ID`, `WA_TOKEN`.

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    port: int = 8000
    database_url: str = "sqlite:///recovery.db"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    gemini_api_key: str = ""
    mandate_secret: str  # required — HMAC key, never logged
    gemini_enabled: bool = False
    wa_phone_id: str = ""
    wa_token: str = ""

settings = Settings()
```

- Logging via `utils/logger.py` (`from utils.logger import logger`) — never `print` or stdlib `logging` directly.

```python
import logging, os
from datetime import datetime
LOGS_DIR = "logs"; os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, f"log_{datetime.now().strftime('%Y-%m-%d')}.log")
logging.basicConfig(filename=LOG_FILE, format="%(asctime)s-%(levelname)s-%(message)s", level=logging.INFO)
def get_logger(name): return logging.getLogger(name)
```

### Policy Engine Order (spec §5 — cheapest first, never reorder)
1. `verify_hmac` → `DENIED: bad_sig` 2. `expiry` → `expired` 3. `allowlist` → `allowlist` 4. `catalog_version` → `stale_catalog` 5. `sku exists` → `unknown_sku` 6. `canonical_price <= max_amount` → `amount>max` (500000 passes, 500001 blocks) 7. `is_injected` (regex first, Gemini 2s timeout second-opinion only if enabled) → `prompt_injection` 8. else `create_payment_link` → `PAID`. Every branch logs to `audit.jsonl`.

### General
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- `.env` never committed; `.env.example` has keys, no values.
- `audit.jsonl` append-only, hash-chained; `recovery.db` idempotency via `UNIQUE(original_payment_id, attempts)` + `idempotency_key`.
- API versioning not needed — single FastAPI app. Razorpay calls via Basic Auth or MCP tools `create_payment_link`/`fetch_payment`.

## Deployment Philosophy
- Primary: `localhost` screen-record for 5-min video (spec §9). No prod deploy required.
- Optional: Render/Fly free tier single Docker (FastAPI serves Jinja). No Vercel split, no HF Spaces (no heavy ML).
- Ponytail Go-portability flag: **N/A — locked to Python per spec §3.3** (stdlib `hmac` is the point; no Node/Go rewrite).

## Multi-Agent Workflow
Spec §8 execution note — 2 subagents, shared `catalog/`, `policy/*`, `audit.jsonl`:

| Agent | Scope | Files |
|-------|-------|-------|
| **A: buyer** | Gemini intent + `policy/mandate.py` + `policy/authorize.py` + MCP + `web/app.py` catalog/approve/audit | `agent/buyer.py`, `policy/*`, `web/app.py`, `catalog/freshmart.json` |
| **B: recovery** | webhook/poll + rule classifier + WA + dashboard | `recovery/orchestrator.py`, `web/app.py:/dashboard`, `recovery.db` |

Flow: Plan → Build (up to 2 concurrent; max 3 if split further) → Verify `make test && make style`. Ponytail review on combined diff only. No playwright E2E (video is manual demo, not browser tests).

## Agent Guidelines
- Read `docs/spec.md` before any code — spec precedence over this index.
- Never modify `docs/spec.md` unless asked. Keep `audit.jsonl`/`recovery.db` out of git (append-only artifacts).
- `make style` before done; `make test` (incl. `GEMINI_ENABLED=false` harness) after changes — fix failures before moving on.
- `snake_case` Python; never `os.environ` outside `utils/config.py`; never `print`/`logging` outside `utils/logger.py`.
- Never add `frontend/`, TypeScript, `npm`, or new deps for what stdlib covers. `gTTS` is the only voice dep and is on-demand.
- Never trust LLM amount — always `catalog[sku].price` at pinned `catalog_version`.
- Update `docs/features.json` after each feature — mark done, set test status.
- If task needs design, flag before coding. If out-of-scope (spec §1.3 YAGNI), say so in one line.

`docs/features.json` (canonical tracker — keep in sync):

```json
{
  "project": "RekhaVasool",
  "last_updated": "2026-08-27",
  "summary": {"total": 6, "completed": 0, "in_progress": 0, "planned": 6, "tests_passing": 0, "tests_failing": 0, "tests_missing": 6},
  "features": [
    {"id": "F01", "name": "Catalog + Mandate (HMAC)", "description": "catalog/freshmart.json (3 SKUs, v1.0) + policy/mandate.py sign/verify HMAC-SHA256; mandate stored in Payment Link notes", "status": "planned", "priority": "high", "module": "policy", "design_doc": "docs/spec.md#4", "tests": {"status": "missing", "files": ["tests/test_mandate.py"], "notes": "sign/verify, tampered sig, expiry"}, "subtasks": [], "notes": "ponytail: HMAC for demo", "added": "2026-08-27", "completed": null},
    {"id": "F02", "name": "Policy Engine authorize()", "description": "8 checks in spec §5 order; is_injected regex-first + Gemini optional (2s timeout); every branch → audit.jsonl hash chain", "status": "planned", "priority": "high", "module": "policy", "design_doc": "docs/spec.md#5", "tests": {"status": "missing", "files": ["tests/test_authorize.py"], "notes": "all DENIED reasons + PAID; boundary 500000/500001"}, "subtasks": [], "notes": "", "added": "2026-08-27", "completed": null},
    {"id": "F03", "name": "Buyer Agent (Gemini + MCP)", "description": "agent/buyer.py emits {sku,qty,merchant,raw_text} only; inline MCP config to Razorpay @razorpay/mcp create_payment_link with canonical price", "status": "planned", "priority": "high", "module": "agent", "design_doc": "docs/spec.md#3.1", "tests": {"status": "missing", "files": ["tests/test_buyer.py"], "notes": "mock Gemini, no amount trusted"}, "subtasks": [], "notes": "Inline MCP config at agent/buyer.py:12", "added": "2026-08-27", "completed": null},
    {"id": "F04", "name": "Web (catalog + approve + audit + dashboard)", "description": "FastAPI + Jinja: catalog view, Approve Mandate (₹5k/15m JWT), audit view, /dashboard from recovery.db", "status": "planned", "priority": "high", "module": "web", "design_doc": "docs/spec.md#3.1", "tests": {"status": "missing", "files": ["tests/test_web.py"], "notes": "approve flow, dashboard counters"}, "subtasks": [], "notes": "", "added": "2026-08-27", "completed": null},
    {"id": "F05", "name": "Red-Team Harness 20/20", "description": "tests/redteam.jsonl (5×4) + tests/harness.py runner; 20/20 blocked with GEMINI_ENABLED=false", "status": "planned", "priority": "high", "module": "tests", "design_doc": "docs/spec.md#7", "tests": {"status": "missing", "files": ["tests/harness.py", "tests/redteam.jsonl"], "notes": "pytest tests/harness.py -v in video"}, "subtasks": [], "notes": "Honesty: 100% on declared harness, not absolute", "added": "2026-08-27", "completed": null},
    {"id": "F06", "name": "Recovery Loop + WA + Polling", "description": "payment.failed → retry (45s) / WA Cloud API to +91-9560452773 + fresh link (expire 15m, idempotency_key) + STOP cancel + polling fallback + gTTS mock voice labeled mock", "status": "planned", "priority": "high", "module": "recovery", "design_doc": "docs/spec.md#8", "tests": {"status": "missing", "files": ["tests/test_recovery.py"], "notes": "caps attempts≤2 wa_sent≤1, idempotency, STOP"}, "subtasks": [], "notes": "Rule-based classifier only; 50 synthetic batch; dashboard ₹ recovered", "added": "2026-08-27", "completed": null}
  ]
}
```

## Project-Specific Notes
- External APIs: Razorpay `api.razorpay.com/v1` (Basic Auth `RAZORPAY_KEY_ID:RAZORPAY_KEY_SECRET`), Meta WA Cloud API (`WA_PHONE_ID`/`WA_TOKEN`), Gemini (`GEMINI_API_KEY` via `google-generativeai`). All via `.env` → `utils/config.py:settings`; never `os.environ`.
- WA pre-build checklist (must before build): Meta app → test number → whitelist **+91-9560452773** via OTP → `curl hello_world` → screenshot. No custom template. 5-contact/90-day limit noted — prod needs WABA.
- Razorpay test cards: `4000 0000 0000 0002` success / `4000 0000 0000 0069` failure; test UPI for `payment.failed`; polling fallback `GET /v1/payment_links/{id}` every 2s/15s if webhook missed (ngrok-free localhost).
- Voice strictly mocked: `gTTS` on-demand, labeled `mock — live via Sarvam+Exotel in prod`, never committed as audio.
- Never touch: `docs/spec.md` (source of truth), `audit.jsonl`/`recovery.db` contents (append-only artifacts; gitignore them), `MANDATE_SECRET` (never log).
- Gotchas: Gemini 15 RPM — harness must pass without it; webhook delay → polling; HMAC is demo-grade (`# ponytail: ... Ed25519/JWS if prod`); prices in paise; `catalog_version` pinned in mandate + `notes`.
- Demo script: spec §9 (5-min timeline). Success criteria: spec §1.4 SC1–SC4.
