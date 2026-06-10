"""
server.py — Elenchus web server

FastAPI app that:
- Serves a static HTML/JS frontend
- Manages dialectical states in DuckDB files
- Proxies LLM oracle calls through the Anthropic SDK
- Supports creating, listing, resuming, and exporting dialectics

Run: elenchus
Or:  uvicorn elenchus.server:app --reload
"""

import glob
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit as audit_mod
from . import auth, invites
from . import backup as backup_mod
from . import integrity as integrity_mod
from .db import get_registry, init_registry
from .db import platform as pdb
from .dialectical_state import DialecticalState
from .llm_client import ChatCategory
from .opponent import LLMCallError, Opponent
from .pdf_report import generate_pdf_report

logger = logging.getLogger(__name__)

# ── Config ──

DATA_DIR = os.environ.get("ELENCHUS_DATA", "./dialectics")
os.makedirs(DATA_DIR, exist_ok=True)

# Initialize the process-wide DBRegistry. This must happen before any
# route handler runs. The registry owns DuckDB connection lifecycle;
# direct `duckdb.connect` calls outside the registry are forbidden.
init_registry(DATA_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup: registry is already initialized at module load. Apply
    # platform-DB migrations so the actors / invites / sessions tables
    # exist before any request hits an auth check.
    version = get_registry().migrate_platform()
    logger.info("Platform DB at schema version %d", version)
    yield
    # Shutdown: close all open DuckDB connections to release locks and
    # flush WAL files. close_all is idempotent.
    get_registry().close_all()


app = FastAPI(title="Elenchus", version="0.1.0", lifespan=lifespan)


def _env_phase_b_enabled() -> bool:
    """Parse ELENCHUS_ENABLE_PHASE_B as a boolean. The default
    deployment must be Sloan-compliant (Phase B off), so anything
    other than an explicit truthy value resolves to False."""
    return os.environ.get("ELENCHUS_ENABLE_PHASE_B", "").lower() in ("1", "true", "yes", "on")


opponent = Opponent(
    model=os.environ.get("ELENCHUS_MODEL", "claude-opus-4-6"),
    api_key=os.environ.get("ELENCHUS_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
    base_url=os.environ.get("ELENCHUS_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
    protocol=os.environ.get("ELENCHUS_PROTOCOL"),
    enable_phase_b=_env_phase_b_enabled(),
)


def _get_state(name: str) -> DialecticalState:
    """Return the cached DialecticalState for `name`, translating
    registry exceptions into HTTP responses. Thin wrapper over the
    DBRegistry that keeps route handlers ignorant of registry internals.

    This function performs *no* authorization check. Use
    `_authorize_and_get_state` for protected routes.
    """
    try:
        return get_registry().get(name)
    except FileNotFoundError as e:
        raise HTTPException(404, f"Dialectic '{name}' not found") from e
    except ValueError as e:
        logger.error("Corrupt dialectic file for '%s': %s", name, e)
        raise HTTPException(422, f"Dialectic '{name}' has a corrupt database file") from e


def _authorize_base_access(name: str, actor: dict) -> None:
    """Verify the current actor is authorized to access the named
    dialectic. Admins can access any base; other roles can only access
    bases they own.

    Looks up the base in `platform.bases` (id = sanitized name).
    Non-owners and missing-base responses both return 404 — leaking
    that a name exists but is owned by someone else is an information
    leak. Admins bypass ownership entirely.
    """
    if actor.get("kind") == "admin":
        return  # admins bypass ownership

    base = pdb.find_base(get_registry().platform_con(), name)
    if base is None or base["owner_id"] != actor["id"]:
        raise HTTPException(404, f"Dialectic '{name}' not found")


def _authorize_and_get_state(name: str, actor: dict) -> DialecticalState:
    """Combine authorization and state lookup for protected routes."""
    _authorize_base_access(name, actor)
    return _get_state(name)


# ── LLM failure → HTTP response mapping ──────────────────────────────
#
# When an LLM call fails after retries, the Opponent raises LLMCallError
# carrying a classified ChatResult. The two helpers below translate the
# category into (a) an HTTP status that downstream tools (probes, error
# trackers) can group on, and (b) a user-facing string the frontend
# shows verbatim. The frontend doesn't need to know which categories
# exist — it just renders `detail.user_message`.

_HTTP_STATUS_BY_CATEGORY: dict[ChatCategory, int] = {
    ChatCategory.AUTH_FAILURE: 503,  # platform issue, not the user's fault
    ChatCategory.RATE_LIMIT: 503,
    ChatCategory.PROVIDER_ERROR: 503,
    ChatCategory.TIMEOUT: 504,
    ChatCategory.NETWORK_ERROR: 503,
    ChatCategory.CONTENT_POLICY: 422,  # the request itself was refused
    ChatCategory.TOKEN_OVERFLOW: 413,  # payload too large
    ChatCategory.BAD_REQUEST: 400,
    ChatCategory.UNKNOWN: 500,
}

_USER_MESSAGE_BY_CATEGORY: dict[ChatCategory, str] = {
    ChatCategory.AUTH_FAILURE: (
        "The AI service can't be reached right now. The administrator has been notified."
    ),
    ChatCategory.RATE_LIMIT: (
        "The AI service is busy. Pausing for a moment — please try "
        "your message again in a few seconds."
    ),
    ChatCategory.PROVIDER_ERROR: (
        "The AI service is temporarily unavailable. Please try again shortly."
    ),
    ChatCategory.TIMEOUT: (
        "The AI took too long to respond. Try a shorter message, or try again."
    ),
    ChatCategory.NETWORK_ERROR: (
        "Couldn't reach the AI service over the network. Please try again."
    ),
    ChatCategory.CONTENT_POLICY: ("The AI declined to respond to this message. Try rephrasing."),
    ChatCategory.TOKEN_OVERFLOW: (
        "This conversation has grown too long for the AI to read in "
        "one go. Consider starting a fresh dialectic on the same topic."
    ),
    ChatCategory.BAD_REQUEST: (
        "Something was wrong with the request. Please contact your "
        "administrator if this keeps happening."
    ),
    ChatCategory.UNKNOWN: (
        "An unexpected error occurred. Please try again, or contact "
        "your administrator if it persists."
    ),
}


def _http_status_for_chat_category(category: ChatCategory) -> int:
    return _HTTP_STATUS_BY_CATEGORY.get(category, 500)


def _user_message_for_chat_category(category: ChatCategory) -> str:
    return _USER_MESSAGE_BY_CATEGORY.get(
        category,
        "Something went wrong. Please try again.",
    )


# ── API Models ──


class CreateRequest(BaseModel):
    name: str
    topic: str | None = None


class MessageRequest(BaseModel):
    message: str
    context: dict | None = None


class TensionAction(BaseModel):
    action: str  # 'accept' or 'contest'


class RetractRequest(BaseModel):
    proposition: str


class DeriveRequest(BaseModel):
    gamma: list[str]
    delta: list[str]


class SettingsUpdate(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    protocol: str | None = None


# ─── Auth request models ──────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    token: str
    display_name: str
    password: str
    email_override: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class MagicLinkRequest(BaseModel):
    email: str


class InviteCreateRequest(BaseModel):
    role: str
    intended_email: str | None = None
    ttl_days: int | None = 30


class ParticipantTokenRequest(BaseModel):
    """Body for `POST /api/admin/study/tokens`. Researchers issue one
    of these per (participant, condition) — within-subjects design
    means the same participant gets two tokens (elenchus + baseline)."""

    study_id: str
    condition: str  # 'elenchus' | 'baseline'
    display_name: str  # researcher's label for this participant
    scheduled_start: str | None = None  # ISO timestamp
    scheduled_end: str | None = None
    notes: str | None = ""


# ── API Routes ──

# ─── Auth ─────────────────────────────────────────────────────────────


def _set_session_cookie(response: Response, token: str) -> None:
    """Attach the session cookie to a response. HTTP-only + SameSite=Lax
    is the safe default; secure=True is enabled via the SESSION_COOKIE_SECURE
    env var (set in production behind HTTPS)."""
    secure = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=int(auth.SESSION_TTL.total_seconds()),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=auth.SESSION_COOKIE, samesite="lax")


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    """Verify credentials and set a session cookie."""
    actor = auth.authenticate(req.email, req.password)
    if actor is None:
        raise HTTPException(401, "Invalid email or password")
    token = auth.create_session(actor["id"])
    _set_session_cookie(response, token)
    return {"actor_id": actor["id"], "display_name": actor["display_name"], "kind": actor["kind"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    """Revoke the current session and clear its cookie. Idempotent —
    succeeds even if no cookie is present."""
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.revoke_session(token)
    _clear_session_cookie(response)
    return {"status": "logged_out"}


@app.post("/api/auth/signup")
def signup(req: SignupRequest, response: Response):
    """Consume an invite, create the actor it authorizes, and start a
    session in one atomic step."""
    result = invites.signup_with_invite(
        token=req.token,
        display_name=req.display_name,
        password=req.password,
        email_override=req.email_override,
    )
    _set_session_cookie(response, result["session_token"])
    return {"actor_id": result["actor_id"], "role": result["role"]}


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest,
    response: Response,
    actor: dict = Depends(auth.current_actor),
):
    """Change the current actor's password. All outstanding sessions
    (including this one) are revoked; this route issues a fresh session
    to keep the user logged in."""
    ok = auth.change_password(actor["id"], req.old_password, req.new_password)
    if not ok:
        raise HTTPException(400, "Old password did not verify")
    # Re-issue a session token since change_password revoked all of them.
    token = auth.create_session(actor["id"])
    _set_session_cookie(response, token)
    return {"status": "changed"}


@app.post("/api/auth/magic-link")
def request_magic_link(req: MagicLinkRequest, request: Request):
    """Email a magic-link login token. Returns 200 regardless of
    whether the email is registered (don't leak registration state)."""
    base_url = str(request.base_url).rstrip("/")
    token = auth.issue_magic_link(req.email)
    try:
        from . import email_service

        email_service.send_magic_link_email(token=token, recipient=req.email, base_url=base_url)
    except Exception:
        logger.exception("Failed to send magic-link email")
    return {"status": "sent"}


@app.get("/api/auth/magic/{token}")
def consume_magic_link(token: str, response: Response):
    """Consume a magic-link token. If valid, issues an auth session for
    the actor identified by the link's email and returns 200; if the
    actor doesn't exist, returns 404."""
    email = auth.consume_magic_link(token)
    if email is None:
        raise HTTPException(400, "Invalid or expired magic link")
    con = get_registry().platform_con()
    actor = pdb.find_actor_by_email(con, email)
    if actor is None:
        raise HTTPException(404, f"No account exists for {email}")
    session_token = auth.create_session(actor["id"])
    _set_session_cookie(response, session_token)
    return {"actor_id": actor["id"], "display_name": actor["display_name"], "kind": actor["kind"]}


@app.get("/api/auth/me")
def me(actor: dict = Depends(auth.current_actor)):
    """Return the current actor's public fields."""
    return {
        "id": actor["id"],
        "kind": actor["kind"],
        "email": actor["email"],
        "display_name": actor["display_name"],
    }


# ─── Admin: invites ───────────────────────────────────────────────────


@app.post("/api/admin/invites")
def admin_create_invite(
    req: InviteCreateRequest,
    request: Request,
    actor: dict = Depends(auth.require_admin),
):
    """Issue an invite. If `intended_email` is set, the EmailService
    delivers the invite link automatically (or logs it via the console
    backend)."""
    from datetime import timedelta

    base_url = str(request.base_url).rstrip("/")
    ttl = timedelta(days=req.ttl_days) if req.ttl_days else None
    token = invites.issue_invite(
        role=req.role,
        issued_by=actor["id"],
        intended_email=req.intended_email,
        ttl=ttl,
        base_url=base_url,
    )
    return {"token": token, "role": req.role, "intended_email": req.intended_email}


@app.get("/api/admin/invites")
def admin_list_invites(actor: dict = Depends(auth.require_admin)):
    """List all invites issued by this platform."""
    return {"invites": invites.list_invites()}


@app.delete("/api/admin/invites/{token}")
def admin_revoke_invite(token: str, actor: dict = Depends(auth.require_admin)):
    """Revoke an unconsumed invite."""
    if not invites.revoke_invite(token):
        raise HTTPException(404, "Invite not found or already consumed")
    return {"status": "revoked", "token": token}


class BackupRequest(BaseModel):
    """Body for POST /api/admin/backup. Both fields optional; defaults
    are: dump every base + platform into `{DATA_DIR}/backups/`."""

    output_dir: str | None = None
    keep: int | None = None  # retention: keep this many newest archives


@app.post("/api/admin/backup")
def admin_run_backup(req: BackupRequest, actor: dict = Depends(auth.require_admin)):
    """Run a one-shot backup. Snapshots the platform DB and every
    registered base into a single tar.gz archive, then optionally
    prunes older archives down to `keep` (default: 14)."""
    result = backup_mod.make_backup(DATA_DIR, output_dir=req.output_dir)
    output_dir = req.output_dir or os.path.join(DATA_DIR, "backups")
    keep = req.keep if req.keep is not None else backup_mod.DEFAULT_RETENTION
    pruned = backup_mod.prune_backups(output_dir, keep=keep)
    return {
        "archive": result["archive"],
        "timestamp": result["timestamp"],
        "bases_dumped": result["bases_dumped"],
        "bases_failed": result["bases_failed"],
        "pruned": pruned,
    }


@app.get("/api/admin/backup")
def admin_list_backups(actor: dict = Depends(auth.require_admin)):
    """List every backup archive currently on disk, newest first."""
    output_dir = os.path.join(DATA_DIR, "backups")
    return {"backups": backup_mod.list_backups(output_dir), "output_dir": output_dir}


@app.get("/api/admin/integrity")
def admin_integrity_summary(actor: dict = Depends(auth.require_admin)):
    """One row per registered base: total calls, cost, tokens. Sorted
    by cost descending. Cheap (usage-table-only); doesn't open any
    per-base files."""
    return {"bases": integrity_mod.list_base_integrity_summaries()}


@app.get("/api/admin/integrity/{base_id}")
def admin_integrity_detail(
    base_id: str,
    actor: dict = Depends(auth.require_admin),
):
    """Full integrity report for one base. Joins usage-table stats
    (calls by category, p50/p95 latency, mean attempts, total cost)
    with per-base content metrics (|C|, |D|, tensions by status,
    implications, conversation turns)."""
    return integrity_mod.compute_base_integrity(base_id)


@app.get("/api/admin/usage")
def admin_usage(
    days: int = 30,
    actor: dict = Depends(auth.require_admin),
):
    """Cost / token rollup for the admin dashboard.

    Returns the total over the requested window, per-day buckets for
    plotting, and a per-actor breakdown so the admin can see who's
    driving usage. `days` defaults to 30 — Phase C's budget alerts
    (next commit) will hook into the same data."""
    reg = get_registry()
    con = reg.platform_con()
    return {
        "window_days": days,
        "total": pdb.total_cost(con),
        "by_day": pdb.daily_cost(con, days=days),
        "by_actor": pdb.cost_by_actor(con),
    }


@app.get("/api/admin/audit")
def admin_audit(actor: dict = Depends(auth.require_admin)):
    """Run the cross-DB / filesystem audit and return the structured
    report. The CLI counterpart (`elenchus audit`) calls the same
    underlying function; this endpoint exists so admin dashboards can
    surface drift without shell access."""
    return audit_mod.audit_platform(DATA_DIR)


@app.put("/api/admin/users/{user_id}/deactivate")
def admin_deactivate_user(user_id: int, actor: dict = Depends(auth.require_admin)):
    """Soft-delete an actor. Their session cookies stop working
    immediately (resolve_auth_token filters on `deactivated_at IS NULL`)
    and login is refused. Per-base contributions remain attributed to
    the actor so historical attribution survives.

    Refuses to deactivate the last active admin (would lock the
    platform out of itself). Refuses to deactivate yourself for the
    same reason — use another admin to do it.
    """
    reg = get_registry()
    con = reg.platform_con()
    target = pdb.find_actor_by_id(con, user_id)
    if target is None:
        raise HTTPException(404, f"Actor #{user_id} not found")
    if target.get("deactivated_at") is not None:
        return {"status": "already_deactivated", "id": user_id}
    if user_id == actor["id"]:
        raise HTTPException(400, "Cannot deactivate yourself")
    if target["kind"] == "admin" and pdb.count_active_admins(con) <= 1:
        raise HTTPException(400, "Cannot deactivate the last active admin")
    with reg.platform_lock:
        pdb.deactivate_actor(con, user_id)
        pdb.revoke_actor_sessions(con, user_id)
    logger.info("Actor #%d deactivated by admin #%d", user_id, actor["id"])
    return {"status": "deactivated", "id": user_id}


@app.put("/api/admin/users/{user_id}/reactivate")
def admin_reactivate_user(user_id: int, actor: dict = Depends(auth.require_admin)):
    """Undo a deactivation. The actor can log in again with their
    existing credentials; previously-revoked session cookies are NOT
    restored (they have to log in fresh)."""
    reg = get_registry()
    con = reg.platform_con()
    target = pdb.find_actor_by_id(con, user_id)
    if target is None:
        raise HTTPException(404, f"Actor #{user_id} not found")
    if target.get("deactivated_at") is None:
        return {"status": "already_active", "id": user_id}
    with reg.platform_lock:
        pdb.reactivate_actor(con, user_id)
    logger.info("Actor #%d reactivated by admin #%d", user_id, actor["id"])
    return {"status": "reactivated", "id": user_id}


@app.get("/api/admin/users")
def admin_list_users(actor: dict = Depends(auth.require_admin)):
    """List all actors. Returns id, kind, email, display_name,
    deactivated_at — never password_hash."""
    rows = pdb.list_actors(get_registry().platform_con(), include_deactivated=True)
    return {
        "users": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "email": r["email"],
                "display_name": r["display_name"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "deactivated_at": str(r["deactivated_at"]) if r["deactivated_at"] else None,
            }
            for r in rows
        ]
    }


# ─── Study harness: participant session tokens (Phase D) ─────────────


@app.post("/api/admin/study/tokens")
def admin_issue_participant_token(
    req: ParticipantTokenRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Researcher issues one participant token. Creates a fresh
    `kind='participant'` actor with no password (the token itself is
    the credential — single-use, single-session) and binds the new
    token to it.

    The condition column drives the within-subjects design: the same
    physical participant gets one token for the Elenchus condition
    and one for the baseline condition, issued separately so the
    researcher controls condition order.
    """
    if req.condition not in ("elenchus", "baseline"):
        raise HTTPException(400, "condition must be 'elenchus' or 'baseline'")
    if not req.study_id.strip():
        raise HTTPException(400, "study_id is required")
    if not req.display_name.strip():
        raise HTTPException(400, "display_name is required")

    reg = get_registry()
    con = reg.platform_con()
    token = auth.generate_token()
    with reg.platform_lock:
        # Create the participant actor first — passwordless. The token
        # IS the credential; consuming it issues a session cookie tied
        # to this actor.
        participant_id = pdb.create_actor(
            con,
            kind="participant",
            email=None,
            display_name=req.display_name.strip(),
            password_hash=None,
        )
        pdb.create_participant_token(
            con,
            token=token,
            actor_id=participant_id,
            study_id=req.study_id.strip(),
            condition=req.condition,
            issued_by=actor["id"],
            scheduled_start=req.scheduled_start,
            scheduled_end=req.scheduled_end,
            notes=(req.notes or "").strip(),
        )
    logger.info(
        "Issued participant token: study=%s condition=%s actor=%d (by %d)",
        req.study_id,
        req.condition,
        participant_id,
        actor["id"],
    )
    return {
        "token": token,
        "participant_actor_id": participant_id,
        "study_id": req.study_id,
        "condition": req.condition,
        "display_name": req.display_name,
    }


@app.get("/api/admin/study/tokens")
def admin_list_participant_tokens(
    study_id: str | None = None,
    condition: str | None = None,
    actor: dict = Depends(auth.require_researcher),
):
    """List participant tokens, newest first. Filter by `study_id`
    and/or `condition` for cohort views."""
    return {
        "tokens": pdb.list_participant_tokens(
            get_registry().platform_con(),
            study_id=study_id,
            condition=condition,
        )
    }


@app.delete("/api/admin/study/tokens/{token}")
def admin_void_participant_token(
    token: str,
    actor: dict = Depends(auth.require_researcher),
):
    """Void a still-scheduled token. Idempotent — a token that's
    already been used / voided / expired returns 404."""
    reg = get_registry()
    with reg.platform_lock:
        ok = pdb.void_participant_token(reg.platform_con(), token)
    if not ok:
        raise HTTPException(404, "Token not found or already used / voided")
    return {"status": "voided", "token": token}


@app.post("/api/study/{token}")
def consume_participant_token(token: str, response: Response):
    """Public endpoint — the participant clicks the emailed link and
    this trades the token for a session cookie tied to the underlying
    participant actor.

    Single-use: the SQL `UPDATE ... WHERE status='scheduled'` makes
    repeat-clicks idempotent (the second request gets 410 Gone).
    Outside the scheduled window: 410 with the same body shape so
    the frontend renders one message.
    """
    reg = get_registry()
    with reg.platform_lock:
        consumed = pdb.consume_participant_token(reg.platform_con(), token)
    if consumed is None:
        existing = pdb.find_participant_token(reg.platform_con(), token)
        if existing is None:
            raise HTTPException(404, "Token not found")
        # Token exists but isn't consumable — used, voided, expired,
        # or outside its scheduled window. Surface a structured detail.
        raise HTTPException(
            status_code=410,
            detail={
                "status": existing["status"],
                "user_message": _participant_token_message(existing),
            },
        )

    # Issue a session cookie for the participant actor. Same shape
    # as the regular login flow.
    session_token = auth.create_session(consumed["actor_id"])
    _set_session_cookie(response, session_token)
    logger.info(
        "Participant token consumed: study=%s condition=%s actor=%d",
        consumed["study_id"],
        consumed["condition"],
        consumed["actor_id"],
    )
    return {
        "study_id": consumed["study_id"],
        "condition": consumed["condition"],
        "actor_id": consumed["actor_id"],
    }


def _participant_token_message(existing: dict) -> str:
    """Map a non-consumable token's status to a friendly message."""
    status = existing.get("status")
    if status == "voided":
        return "This study link has been cancelled. Please contact the researcher."
    if status == "expired":
        return "This study link has expired. Please contact the researcher."
    if status in ("active", "complete") or existing.get("used_at"):
        return "This study link has already been used."
    # Within scheduled window check failed.
    return (
        "This study link can't be used right now — it may be outside its "
        "scheduled window. Please check the time, or contact the researcher."
    )


# ─── Existing routes follow ──────────────────────────────────────────


@app.get("/api/settings")
def get_settings():
    """Return current LLM settings (never exposes the API key value)."""
    return {
        "model": opponent.model,
        "base_url": opponent.base_url or "",
        "protocol": opponent.protocol,
        "has_api_key": opponent._has_api_key,
    }


@app.put("/api/settings")
def update_settings(req: SettingsUpdate):
    """Update LLM settings at runtime."""
    opponent.reconfigure(
        model=req.model,
        api_key=req.api_key,
        base_url=req.base_url,
        protocol=req.protocol,
    )
    logger.info(
        "Settings updated via API: model=%s, base_url=%s, api_key_provided=%s",
        req.model,
        req.base_url,
        bool(req.api_key),
    )
    return get_settings()


@app.post("/api/dialectics")
def create_dialectic(req: CreateRequest, actor: dict = Depends(auth.current_actor)):
    """Create a new dialectic owned by the current actor.

    Registers the new base in `platform.bases` with `owner_id =
    actor.id` so subsequent routes can verify ownership. The file
    lives at ``{DATA_DIR}/bases/{actor_id}/{name}.duckdb`` — one
    directory per actor scopes file-level access naturally.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    reg = get_registry()
    # Resolve the actor-scoped path explicitly; we know the owner here
    # so we don't need the platform-DB roundtrip the no-arg form does.
    path = reg.db_path(name, actor_id=actor["id"])
    if os.path.exists(path):
        raise HTTPException(409, f"Dialectic '{name}' already exists")

    # Sanity-check that no `bases` row exists either (covers the case
    # where the file was deleted out-of-band but the row remained).
    if pdb.find_base(reg.platform_con(), name) is not None:
        raise HTTPException(409, f"Dialectic '{name}' is already registered")

    # Ensure the per-actor directory exists before DialecticalState.create
    # tries to write the file.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    topic = req.topic or name
    state = DialecticalState.create(path, topic)
    reg.put(name, state)
    with reg.platform_lock:
        pdb.create_base(
            reg.platform_con(),
            base_id=name,
            name=topic,
            owner_id=actor["id"],
        )
    return {"name": name, "state": state.to_dict()}


@app.get("/api/dialectics")
def list_dialectics(actor: dict = Depends(auth.current_actor)):
    """List the current actor's dialectics. Admins see every base in
    the platform; other actors see only their own."""
    reg = get_registry()
    if actor.get("kind") == "admin":
        # Walk `platform.bases` for the canonical list, then top up with
        # any legacy flat-layout files that lack a `bases` row (still
        # readable; `migrate-legacy` will register them).
        rows = pdb.list_bases(reg.platform_con())
        basenames = [r["id"] for r in rows]
        seen = set(basenames)
        for f in sorted(glob.glob(os.path.join(DATA_DIR, "*.duckdb"))):
            if os.path.basename(f) == "platform.duckdb":
                continue
            stem = Path(f).stem
            if stem not in seen:
                basenames.append(stem)
                seen.add(stem)
    else:
        rows = pdb.list_bases_for_actor(reg.platform_con(), actor["id"])
        basenames = [r["id"] for r in rows]

    result = []
    for basename in basenames:
        try:
            s = _get_state(basename)
            d = s.to_dict()
            result.append(
                {
                    "name": basename,
                    "topic": d["name"],
                    "commitments": len(d["commitments"]),
                    "denials": len(d["denials"]),
                    "tensions": len(d["tensions"]),
                    "implications": len(d["implications"]),
                }
            )
        except Exception:
            logger.debug("Failed to open dialectic '%s'", basename)
            result.append(
                {
                    "name": basename,
                    "topic": basename,
                    "commitments": 0,
                    "denials": 0,
                    "tensions": 0,
                    "implications": 0,
                }
            )
    return result


@app.get("/api/dialectics/{name}")
def get_dialectic(name: str, actor: dict = Depends(auth.current_actor)):
    """Get the current state of a dialectic, including conversation history."""
    state = _authorize_and_get_state(name, actor)
    result = state.to_dict()
    result["conversation"] = state.get_conversation()
    return result


@app.post("/api/dialectics/{name}/message")
async def send_message(
    name: str,
    req: MessageRequest,
    actor: dict = Depends(auth.current_actor),
):
    """
    Send a natural language message from the respondent.
    The opponent parses it, updates state, proposes tensions,
    and responds.

    Async because the LLM call dominates this route (5–30 s). Using
    `await opponent.async_respond(...)` frees the event loop to service
    other requests during the wait. The per-base async lock from the
    DBRegistry is passed through so concurrent writers on the same base
    serialize at the apply phase only — the LLM call itself runs without
    the lock so concurrent tabs don't freeze each other.
    """
    _authorize_base_access(name, actor)
    try:
        handle = get_registry().get_handle(name)
    except FileNotFoundError as e:
        raise HTTPException(404, f"Dialectic '{name}' not found") from e
    except ValueError as e:
        logger.error("Corrupt dialectic file for '%s': %s", name, e)
        raise HTTPException(422, f"Dialectic '{name}' has a corrupt database file") from e

    try:
        result = await opponent.async_respond(
            req.message,
            handle.state,
            action_context=req.context,
            lock=handle.lock,
            actor_id=actor["id"],
            base_id=name,
        )
        return {
            "response": result.get("response", ""),
            "speech_acts": result.get("speech_acts", []),
            "new_tensions": result.get("new_tensions", []),
            "state": handle.state.to_dict(),
        }
    except LLMCallError as e:
        # The LLMClient already classified the failure, recorded
        # usage, and dispatched an alert. Surface the category to the
        # frontend in a structured body so the UI can show a
        # user-friendly message ("pausing — handling a technical
        # issue") keyed to the category instead of a raw stack trace.
        status = _http_status_for_chat_category(e.result.category)
        raise HTTPException(
            status_code=status,
            detail={
                "category": e.result.category.value,
                "attempts": e.result.attempts,
                "user_message": _user_message_for_chat_category(e.result.category),
            },
        ) from e
    except Exception as e:
        logger.exception("Unhandled error in message route for base %r", name)
        raise HTTPException(500, f"Opponent error: {str(e)}") from e


@app.post("/api/dialectics/{name}/tensions/{tid}")
def resolve_tension(
    name: str,
    tid: int,
    req: TensionAction,
    actor: dict = Depends(auth.current_actor),
):
    """Accept or contest a tension directly (bypassing the oracle)."""
    state = _authorize_and_get_state(name, actor)
    logger.info("Tension action: dialectic=%s, tension=#%d, action=%s", name, tid, req.action)
    if req.action == "accept":
        result = state.accept_tension(tid)
        if not result:
            raise HTTPException(404, f"Tension #{tid} not found or not open")
        logger.info("Tension #%d accepted in '%s' → material implication", tid, name)
        return {"accepted": result, "state": state.to_dict()}
    elif req.action == "contest":
        if not state.contest_tension(tid):
            raise HTTPException(404, f"Tension #{tid} not found or not open")
        logger.info("Tension #%d contested in '%s'", tid, name)
        return {"contested": tid, "state": state.to_dict()}
    else:
        raise HTTPException(400, "Action must be 'accept' or 'contest'")


@app.post("/api/dialectics/{name}/retract")
def retract(name: str, req: RetractRequest, actor: dict = Depends(auth.current_actor)):
    """Retract a proposition directly."""
    state = _authorize_and_get_state(name, actor)
    logger.info("Retract: dialectic=%s, proposition=%r", name, req.proposition)
    state.retract_prop(req.proposition)
    return {"retracted": req.proposition, "state": state.to_dict()}


@app.post("/api/dialectics/{name}/derive")
def derive(name: str, req: DeriveRequest, actor: dict = Depends(auth.current_actor)):
    """Check derivability in the material base."""
    state = _authorize_and_get_state(name, actor)
    result = state.derive_with_trace(req.gamma, req.delta)
    return {
        "gamma": req.gamma,
        "delta": req.delta,
        "derives": result.derivable,
        "trace": result.trace,
        "depth": result.depth_reached,
    }


@app.get("/api/dialectics/{name}/report")
def report(name: str, actor: dict = Depends(auth.current_actor)):
    """Get the material base report."""
    state = _authorize_and_get_state(name, actor)
    return {"report": state.base.report()}


@app.get("/api/dialectics/{name}/report.pdf")
def download_report_pdf(name: str, actor: dict = Depends(auth.current_actor)):
    """Generate and download a PDF report of the dialectic."""
    state = _authorize_and_get_state(name, actor)
    logger.info("Generating PDF report for dialectic '%s'", name)
    summary = opponent.generate_summary(state)
    pdf_bytes = generate_pdf_report(state, summary)
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name} - Elenchus Report.pdf"'
        },
    )


@app.delete("/api/dialectics/{name}")
def delete_dialectic(name: str, actor: dict = Depends(auth.current_actor)):
    """Delete a dialectic. Removes the per-base file, the registry
    cache entry, and the platform `bases` row."""
    _authorize_base_access(name, actor)
    reg = get_registry()
    reg.remove(name)  # idempotent
    path = reg.db_path(name)
    file_existed = os.path.exists(path)
    if file_existed:
        os.remove(path)
    with reg.platform_lock:
        pdb.delete_base(reg.platform_con(), name)
    if file_existed:
        return {"deleted": name}
    raise HTTPException(404, f"Dialectic '{name}' not found")


# ── Static files ──

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/sw.js")
def service_worker():
    """Serve the service worker with no-cache headers.

    Browsers cache `sw.js` in the regular HTTP cache; if it's served
    with default heuristic caching, a deployed SW change can take up
    to 24 hours to propagate. `Cache-Control: no-cache` makes the
    browser revalidate every load, so a new SW takes effect on the
    next page visit. The SW itself uses its `CACHE_NAME` versioning
    to invalidate cached *assets* — that part has always worked; this
    just ensures the SW file *replacing* the old SW is fetched
    promptly."""
    sw_path = os.path.join(static_dir, "sw.js")
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/")
def index():
    """Serve index.html with no-cache headers for the same reason as
    sw.js: shell HTML caching delays auth / layout changes."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return HTMLResponse("<h1>Elenchus</h1><p>Place index.html in ./static/</p>")


# ── Entry point ──


def _add_serve_args(parser) -> None:
    parser.add_argument("--port", "-p", type=int, default=None, help="Server port (default: 8741)")
    parser.add_argument("--api-key", default=None, help="LLM API key")
    parser.add_argument(
        "--base-url", default=None, help="LLM API base URL (e.g. https://openrouter.ai/api/v1)"
    )
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument(
        "--protocol",
        default=None,
        choices=["anthropic", "openai"],
        help="API protocol (auto-detected from --base-url)",
    )
    parser.add_argument("--data-dir", default=None, help="Directory for .duckdb files")


def _run_serve(args) -> None:
    import uvicorn

    # CLI args override env vars
    global DATA_DIR
    if args.data_dir:
        DATA_DIR = args.data_dir
        os.makedirs(DATA_DIR, exist_ok=True)

    opponent.reconfigure(
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        protocol=args.protocol,
    )

    port = args.port or int(os.environ.get("PORT", 8741))
    logger.info("Elenchus server starting on http://localhost:%d", port)
    logger.info("Data directory: %s", os.path.abspath(DATA_DIR))
    print(f"Elenchus server starting on http://localhost:{port}")
    print(f"Data directory: {os.path.abspath(DATA_DIR)}")
    uvicorn.run(app, host="0.0.0.0", port=port)


def _run_admin_create(args) -> None:
    """Create (or update) an admin actor. Idempotent: if an actor with
    the given email already exists, optionally updates their password
    (with confirmation)."""
    import getpass

    from . import auth
    from .db import get_registry
    from .db import platform as pdb

    # The platform DB needs to be migrated before any actor can be
    # created. The registry was initialized at module import; we just
    # need to apply migrations explicitly here (lifespan-startup
    # migration only runs when serving).
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()

    existing = pdb.find_actor_by_email(con, args.email)

    password = args.password or os.environ.get("ELENCHUS_ADMIN_PASSWORD")
    if password is None:
        if existing:
            prompt = f"Actor {args.email!r} exists. New password (or empty to skip): "
        else:
            prompt = f"Password for admin {args.email!r}: "
        password = getpass.getpass(prompt)
        if password and not existing:
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Passwords do not match. Aborting.")
                return

    if existing:
        if not password:
            print(f"No change. Existing actor: id={existing['id']}, kind={existing['kind']}")
            return
        with reg.platform_lock:
            pdb.update_actor_password(con, existing["id"], auth.hash_password(password))
        print(f"Updated password for actor id={existing['id']} ({args.email})")
        return

    with reg.platform_lock:
        actor_id = pdb.create_actor(
            con,
            kind="admin",
            email=args.email,
            display_name=args.name,
            password_hash=auth.hash_password(password) if password else None,
        )
    print(f"Created admin actor id={actor_id} ({args.email})")


def _run_audit(args) -> None:
    """Walk the data directory and platform DB, print drift report."""
    from . import audit as audit_mod_local

    reg = get_registry()
    reg.migrate_platform()  # make sure tables exist before we query them
    report = audit_mod_local.audit_platform(DATA_DIR)
    print(audit_mod_local.format_report(report))


def _run_migrate_legacy(args) -> None:
    """Migrate every legacy flat-layout dialectic into the multi-user
    platform layout. Idempotent; safe to re-run."""
    from .legacy import DEFAULT_ADMIN_EMAIL, migrate_legacy

    summary = migrate_legacy(
        DATA_DIR,
        admin_email=args.admin_email or DEFAULT_ADMIN_EMAIL,
        create_admin=args.create_admin,
        admin_password=args.admin_password,
    )

    print(f"Legacy migration complete (admin id={summary['admin_id']}, {summary['admin_email']}):")
    if not summary["migrated"]:
        print("  (no legacy files found)")
    for item in summary["migrated"]:
        print(f"  {item['action']:<18} {item['name']} → {item['path']}")
    if summary["errors"]:
        print(f"\nErrors ({len(summary['errors'])}):")
        for err in summary["errors"]:
            print(f"  {err['path']}: {err['error']}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Elenchus — dialectical knowledge base construction"
    )
    subparsers = parser.add_subparsers(dest="command")

    # `serve` subcommand — same as the default no-subcommand behavior.
    serve = subparsers.add_parser("serve", help="Start the web server (default)")
    _add_serve_args(serve)

    # `admin` subcommand group
    admin = subparsers.add_parser("admin", help="Administrative commands")
    admin_subs = admin.add_subparsers(dest="admin_action")
    create = admin_subs.add_parser("create", help="Create (or update) an admin actor")
    create.add_argument("--email", required=True, help="Admin email address")
    create.add_argument("--name", required=True, help="Admin display name")
    create.add_argument(
        "--password",
        default=None,
        help="Password (prompts interactively if omitted; also reads ELENCHUS_ADMIN_PASSWORD)",
    )

    # `audit` subcommand — report drift between platform DB and disk.
    subparsers.add_parser(
        "audit",
        help="Audit platform DB vs filesystem and per-base actor references",
    )

    # `migrate-legacy` subcommand — relocate legacy single-user dialectics
    # into the multi-user platform layout.
    mig = subparsers.add_parser(
        "migrate-legacy",
        help="Migrate flat-layout dialectics into bases/{actor_id}/{name}.duckdb",
    )
    mig.add_argument(
        "--admin-email",
        default=None,
        help="Email of the admin actor that will own the migrated bases (default: admin@local)",
    )
    mig.add_argument(
        "--create-admin",
        action="store_true",
        help="Create the admin actor if it doesn't exist (default: error out)",
    )
    mig.add_argument(
        "--admin-password",
        default=None,
        help="Initial password for the admin if --create-admin is used (optional)",
    )

    # Default to `serve` when invoked without a subcommand. Re-parse
    # under the serve subparser so its args are available.
    import sys

    if len(sys.argv) == 1 or (sys.argv[1].startswith("-") and sys.argv[1] != "-h"):
        # No subcommand given, or first arg is a flag (e.g. --port) →
        # treat as serve.
        sys.argv.insert(1, "serve")

    args = parser.parse_args()

    if args.command in (None, "serve"):
        _run_serve(args)
    elif args.command == "admin":
        if args.admin_action == "create":
            _run_admin_create(args)
        else:
            admin.print_help()
    elif args.command == "audit":
        _run_audit(args)
    elif args.command == "migrate-legacy":
        _run_migrate_legacy(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
