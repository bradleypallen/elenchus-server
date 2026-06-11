# Session-keyed API migration (task #15)

Migrate the primary dialectic API from name-keyed (`/api/dialectics/{name}/…`)
to session-keyed (`/api/sessions/{id}/…`). This is the implementation
plan; it is executed in phases that each keep the full test suite green,
so the work is committable at every checkpoint.

## Why this is not a "rename"

`/api/dialectics/{name}` resolves a **base** directly by its sanitized
name (`registry.get(name)`); the normal flow never creates a row in the
platform `sessions` table. The generic session helpers
(`pdb.create_session/find_session/list_sessions_for_actor`) exist but are
unused by routes. So session-keying means making sessions a first-class
API resource, not just swapping a path segment.

## Design decisions

1. **A session = (actor_id, base_id), one per base, created with the base.**
   `create_dialectic` creates the base *and* a `sessions` row, returning
   `session_id`. Internal logic (opponent, state, DuckDB) stays keyed by
   `base_id`; only the **route layer** gains a `session_id → base_id`
   resolution + ownership check (404 on mismatch, same leak-prevention
   posture).

2. **The study/participant flow stays base-keyed and is NOT migrated.**
   A study session already owns the `sessions` row and spans two bases
   (`practice-{id}` then `task-{id}`); forcing it onto one-session-one-base
   would be surgery for no benefit, since participants use a constrained
   UI. The study message turns keep using the internal base-keyed path.

3. **`/api/dialectics/*` is retained as a thin deprecated alias**, not
   deleted. This keeps the ~65 existing route tests and the study flow
   working untouched. New `/api/sessions/*` tests cover the new surface.
   (Full removal of the legacy routes + test migration is a separate,
   breaking follow-on — deliberately out of scope here to avoid a
   100-site churn with zero functional gain right before the pilot.)

## Route mapping

| New (primary) | Legacy alias (retained) | Resolution |
|---|---|---|
| `POST /api/sessions` `{name, topic}` | `POST /api/dialectics` | create base + session, return `{session_id, base_id, name, state}` |
| `GET /api/sessions` | `GET /api/dialectics` | `list_sessions_for_actor` joined with base counts |
| `GET /api/sessions/{id}` | `GET /api/dialectics/{name}` | `find_session`→base |
| `POST /api/sessions/{id}/message` | …/message | " |
| `POST /api/sessions/{id}/tensions/{tid}` | …/tensions/{tid} | " |
| `POST /api/sessions/{id}/retract` | …/retract | " |
| `POST /api/sessions/{id}/derive` | …/derive | " |
| `GET /api/sessions/{id}/report[.pdf]` | …/report[.pdf] | " |
| `DELETE /api/sessions/{id}` | `DELETE /api/dialectics/{name}` | close session + delete base |

A shared helper `_resolve_session(session_id, actor) -> (base_id, state)`
does find_session + ownership(404) + `_get_state(base_id)`; the legacy
handlers keep calling `_authorize_and_get_state(name, actor)`. Both paths
converge on the same internal `(base_id, state)`.

## Phases (each ends green: `pytest -q && ruff check . && RUN_UI_E2E=1 pytest tests/e2e/`)

- **P1 — backend, additive.** Add `_resolve_session`; add the
  `/api/sessions/*` routes; make `create_dialectic` (and the new
  `POST /api/sessions`) create+return a session. Backfill: lazily create
  a session on first `GET /api/sessions/{id}`-style access for legacy
  bases, or a one-time migration mapping one session per existing base.
  New `tests/test_sessions_api.py`. Legacy routes + all existing tests
  unchanged → green.
- **P2 — frontend.** Replace `current` (name) with `currentSessionId`
  (+ keep `name` for display); switch the ~13 normal-app `api()` calls to
  `/api/sessions/{id}`. The study participant UI is unaffected (still
  base-keyed). Verify via e2e + a recorded smoke.
- **P3 — docs.** README / CLAUDE.md curl examples → session-keyed, with a
  one-line "legacy `/api/dialectics` deprecated" note.
- **P4 — (separate, breaking; not in this pass)** remove the legacy
  routes, migrate the ~65 tests + sim/access/harness to session ids,
  decide the study-flow story. Track as its own task.

## Acceptance criteria (this pass = P1–P3)

1. The whole app works against `/api/sessions/*`; two users can't reach
   each other's sessions (404).
2. `/api/dialectics/*` still works (alias); 722 existing tests + e2e green.
3. New session-route tests pass; `ruff` clean.
4. README/CLAUDE updated; this doc reflects what shipped.
