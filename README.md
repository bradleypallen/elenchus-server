# Elenchus

[![CI](https://github.com/bradleypallen/elenchus-server/actions/workflows/ci.yml/badge.svg)](https://github.com/bradleypallen/elenchus-server/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/elenchus)](https://pypi.org/project/elenchus/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/bradleypallen/elenchus-server/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-user%20guide-blue)](https://bradleypallen.github.io/elenchus-server/guide/)

A standalone system for dialectical knowledge base construction, implementing the Elenchus protocol (Allen 2026) with a DuckDB material base backend.

The respondent develops a bilateral position [C : D] through natural language dialogue with an LLM opponent. Accepted tensions become material implications in a NMMS material base satisfying Containment.

## Requirements

- Python 3.11+
- An LLM API key (Anthropic, OpenRouter, or any OpenAI-compatible provider)

## Installation

```bash
pip install elenchus
export ELENCHUS_API_KEY=sk-ant-...    # or ANTHROPIC_API_KEY
```

For development:

```bash
git clone https://github.com/bradleypallen/elenchus-server.git
cd elenchus-server
pip install -e ".[dev]"
```

**[User Guide](https://bradleypallen.github.io/elenchus-server/guide/)** — full documentation covering concepts, the web interface, CLI, configuration, and a worked example.

## Usage

### First-time setup (multi-user mode)

Elenchus is a multi-user platform. Before the first sign-in you create
an admin actor; everyone else joins by accepting an invite that admin
issues.

```bash
# 1. Bootstrap the admin.
elenchus admin create --email admin@local --name "Admin"
# (prompts for a password, or reads ELENCHUS_ADMIN_PASSWORD)

# 2. Start the server.
elenchus

# 3. Open the browser and sign in as the admin.
# 4. In the admin dashboard, issue an invite to each user.
#    The dashboard renders the full ?token=... URL — share that link.
# 5. Users click the link, choose a display name and password, and land
#    in the app.
```

Upgrading from a single-user install? Run `elenchus migrate-legacy` once
to register every existing `./dialectics/*.duckdb` under the admin's
account and relocate it into the per-actor directory layout. Idempotent.

### Web interface

```bash
elenchus
```

Options: `--port`, `--model`, `--api-key`, `--base-url`, `--protocol`, `--data-dir` (see `elenchus --help`).

Open the URL shown in the terminal (default `http://localhost:8741`). The web interface provides:

- Creating and resuming dialectics scoped to the current user
- Natural language dialogue with the LLM opponent
- Live bilateral state display [C : D]
- Tension resolution (accept/contest)
- Material implications accumulating in I
- Derivability queries against the material base
- An admin-only dashboard for issuing invites and managing users

### Command line

```bash
# Interactive session (in-memory)
elenchus-cli --name "My Inquiry"

# Persistent session (saved to DuckDB file)
elenchus-cli --db my_inquiry.duckdb --name "My Inquiry"

# Resume a saved session
elenchus-cli --db my_inquiry.duckdb
```

### API

All `/api/sessions/*` and `/api/admin/*` routes require an authenticated
session. The flow is: POST to `/api/auth/login` to get a session cookie,
then send subsequent requests with that cookie. A dialectic is addressed
by the numeric `session_id` returned on create.

```bash
# 1. Log in (saves the cookie to ./cookies.txt).
curl -c cookies.txt -X POST http://localhost:8741/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "..."}'

# 2. Create a dialectic — the response includes "session_id" (e.g. 1).
curl -b cookies.txt -X POST http://localhost:8741/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "prov-o", "topic": "PROV-O Starting Point Terms"}'

# 3. Send a message.
curl -b cookies.txt -X POST http://localhost:8741/api/sessions/1/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Entity is a thing with fixed aspects."}'

# 4. Get state.
curl -b cookies.txt http://localhost:8741/api/sessions/1

# 5. Accept a tension.
curl -b cookies.txt -X POST http://localhost:8741/api/sessions/1/tensions/1 \
  -H "Content-Type: application/json" \
  -d '{"action": "accept"}'

# 6. Check derivability.
curl -b cookies.txt -X POST http://localhost:8741/api/sessions/1/derive \
  -H "Content-Type: application/json" \
  -d '{"gamma": ["entity_fixed_aspects"], "delta": ["individuation"]}'

# 7. List your sessions (admins see every base in the platform).
curl -b cookies.txt http://localhost:8741/api/sessions
```

> The legacy name-keyed routes (`/api/dialectics/{name}/...`) are
> retained as a compatibility alias — the study-participant flow still
> uses them — but new clients should use `/api/sessions/{id}`.

Admin-only routes: `/api/admin/invites`, `/api/admin/users`,
`/api/admin/users/{id}/{deactivate,reactivate}`, `/api/admin/backup`,
`/api/admin/audit`. Non-admins get a 403.

## Architecture

```text
src/elenchus/
├── server.py ──→ opponent.py ──→ LLM API (Anthropic / OpenAI-compatible)
│       ↓
│   dialectical_state.py
│       ↓
│   material_base.py
│       ↓
│   dialectics/*.duckdb
├── static/index.html
├── cli.py
└── pdf_report.py
```

- **server.py**: FastAPI app serving the API and static frontend
- **opponent.py**: LLM oracle — sends state to LLM API (Anthropic or OpenAI-compatible), parses structured responses, applies state transitions
- **dialectical_state.py**: Definition 4 — S = ⟨[C : D], T, I⟩ backed by DuckDB
- **material_base.py**: Definition 5 — B = ⟨L_B, |∼_B⟩ with pyNMMS-based derivability
- **dialectics/*.duckdb**: Persistent state files (one per dialectic)

## Persistence

The data directory (`$ELENCHUS_DATA`, default `./dialectics/`) holds:

- `platform.duckdb` — actors, auth sessions, invites, magic links, base
  ownership, platform settings. Held open by the server for its
  lifetime.
- `bases/{actor_id}/{base_name}.duckdb` — one file per dialectic, owned
  by `actor_id`. Each file contains the atomic language L_B, all
  assessments (the base consequence relation |∼_B), the bilateral
  position [C : D], open and resolved tensions, and conversation
  history.
- `backups/elenchus-*.tar.gz` — output of `scripts/backup.py` /
  `POST /api/admin/backup` (see *Operations* below).

Schema versions live in each file's `meta.schema_version`. The
migration runner brings every file forward at open time; see
[`src/elenchus/migrations/README.md`](https://github.com/bradleypallen/elenchus-server/blob/main/src/elenchus/migrations/README.md)
for how to add a new migration.

## Operations

Full deployment guide — systemd unit, Nginx + TLS, backup cron, log
rotation, and a pre-pilot checklist — is in
[`docs/OPERATIONS.md`](https://github.com/bradleypallen/elenchus-server/blob/main/docs/OPERATIONS.md). See also the task-oriented
guides on the [documentation site](https://bradleypallen.github.io/elenchus-server/):
[Administration](https://github.com/bradleypallen/elenchus-server/blob/main/docs/administration.md) (roles, dashboard, invites,
users, cost, audit, backups, alerting), [Running a Study](https://github.com/bradleypallen/elenchus-server/blob/main/docs/study.md)
(conditions, participant flow, questionnaires, blinded judging, export),
and [Deployment](https://github.com/bradleypallen/elenchus-server/blob/main/docs/deployment.md) (local / production VM / cloud).
Quick reference:

```bash
# Liveness + readiness probe (unauthenticated; for uptime monitors).
curl -sf http://localhost:8741/healthz
# {"status":"ok","schema_version":7,"phase_b_enabled":false,
#  "llm_configured":true,"checks":{"platform_db":"ok","data_dir":"ok"}}

# Audit drift between platform.duckdb and the filesystem.
elenchus audit

# Run a one-shot backup (everything → backups/elenchus-YYYYmmdd-HHMMSS.tar.gz).
# Server must be running; this hits POST /api/admin/backup.
python scripts/backup.py --email admin@local --password ...

# Cron entry (3 AM daily, keep latest 14 archives):
# 0 3 * * * ELENCHUS_BACKUP_EMAIL=admin@local ELENCHUS_BACKUP_PASSWORD=... \
#           python /opt/elenchus/scripts/backup.py >> /var/log/elenchus-backup.log 2>&1

# Agent-driven pilot simulation — robustness check before a live study.
# Drives researcher + participants (both conditions) + judges + export
# end-to-end against the real API. 'scripted' is free + deterministic
# (CI gate); 'llm' uses real personas for a pre-pilot dress rehearsal.
elenchus sim                       # 4 participants, 2 judges, scripted
elenchus sim --driver llm          # real LLM personas (needs an API key)
```

> The simulation validates the **platform** (every role, every
> transition, blinding mechanics, cost, error handling under the real
> request stack) — not the **science**. LLM participants prove the
> machinery is robust; whether the two conditions produce
> different-quality outputs is what the human pilot measures.

```bash
# Per-persona MP4 walkthroughs — watch the system being used through
# the real UI, one video per perspective (participant in each
# condition, judge, researcher). Drives the served frontend in a
# headless browser; needs the [e2e] extra + a one-time chromium install.
pip install -e ".[e2e]" && python -m playwright install chromium
python scripts/record_demo.py --out demo-videos            # scripted (free)
python scripts/record_demo.py --driver llm                 # real dialogue
```

```bash
# Watch a dialectic unfold between two LLMs from a positum: a respondent
# (a domain expert defending a posited paragraph) vs the real Elenchus
# opponent. Prints the transcript + the resulting bilateral position
# [C : D], accepted material implications, and open tensions.
python scripts/run_dialectic.py                            # default sci-ontology positum
python scripts/run_dialectic.py --positum @paragraph.txt \
    --domain "the Gene Ontology" --turns 6 \
    --respondent-model claude-sonnet-4-6 --out dialectic.json
```

DuckDB is a single-writer-per-file store, so the production server
runs as **one process**; horizontal scaling means migrating the
platform DB to Postgres (the `db/registry.py` boundary is shaped to
absorb that swap without touching route handlers). See
[`design-notes/architecture-vision.md`](https://github.com/bradleypallen/elenchus-server/blob/main/design-notes/architecture-vision.md)
for the larger framing.

## Configuration

Environment variables:

- `ELENCHUS_API_KEY`: Required. Your LLM API key (also accepts `ANTHROPIC_API_KEY`).
- `ELENCHUS_MODEL`: LLM model for the oracle (default: `claude-opus-4-6`)
- `ELENCHUS_BASE_URL`: API base URL for OpenAI-compatible providers (e.g. `https://openrouter.ai/api/v1`)
- `ELENCHUS_PROTOCOL`: API protocol — `anthropic` or `openai` (auto-detected from base URL)
- `ELENCHUS_DATA`: Directory for `.duckdb` files (default: `./dialectics`)
- `PORT`: Server port (default: `8741`)
- `SESSION_COOKIE_SECURE`: Set to `true` in production behind HTTPS so the
  auth cookie carries the `Secure` flag.
- `BCRYPT_ROUNDS`: bcrypt cost factor for password hashing (default 12).
  Tests override this to 4 to keep the suite fast — do not lower in
  production.
- `ELENCHUS_ENABLE_PHASE_B`: opt-in flag for the theory-articulation
  speech acts (`ASSERT_IMPLICATION`, `INTRODUCE_BEARER`,
  `RETRACT_IMPLICATION`). **Off by default**: the live message route
  exposes only `{COMMIT, DENY, ACCEPT_TENSION, CONTEST_TENSION, RETRACT,
  REFINE}` to match the Sloan study's Elenchus-condition vocabulary.
  Set to `1`/`true` outside study contexts.

### Using OpenRouter

```bash
export ELENCHUS_API_KEY=sk-or-...
export ELENCHUS_BASE_URL=https://openrouter.ai/api/v1
export ELENCHUS_MODEL=anthropic/claude-opus-4-6
elenchus
```

Any OpenAI-compatible endpoint works the same way (Together, Groq, etc.).

## License

[MIT](https://github.com/bradleypallen/elenchus-server/blob/main/LICENSE) — Copyright © 2026 University of Amsterdam. Author: Bradley P. Allen.
