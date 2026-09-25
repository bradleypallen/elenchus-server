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

import asyncio
import contextlib
import glob
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__ as elenchus_version
from . import alerting as alerting_mod
from . import audit as audit_mod
from . import auth, invites, secretbox, study_enrolment, study_text, text_judging
from . import backup as backup_mod
from . import cost_ledger as cost_ledger_mod
from . import costs as costs_mod
from . import email_service as email_service_mod
from . import integrity as integrity_mod
from . import provider_report as provider_report_mod
from .db import get_registry, init_registry
from .db import platform as pdb
from .db.registry import SWEEP_INTERVAL_SECONDS
from .dialectical_state import DialecticalState
from .llm_client import ChatCategory
from .material_base import QuerySyntaxError
from .opponent import LLMCallError, Opponent
from .pdf_report import generate_pdf_report
from .turn_log import EventContext

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
    # Apply any admin-persisted LLM settings (model / endpoint / key),
    # overriding the env-derived config the opponent booted with.
    _apply_persisted_llm_settings()
    # Close per-base connections nobody has used for a while, so memory
    # tracks the bases in use rather than every base ever opened
    # (policy in db/registry.py).
    sweeper = asyncio.create_task(_sweep_open_bases())
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    # Shutdown: close all open DuckDB connections to release locks and
    # flush WAL files. close_all is idempotent.
    get_registry().close_all()


async def _sweep_open_bases() -> None:
    """Run the registry's eviction sweep every `SWEEP_INTERVAL_SECONDS`
    for the life of the server. The sweep closes connections, so it
    runs in a worker thread rather than on the event loop."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(get_registry().evict_idle)
        except Exception:
            logger.exception("Sweep of open bases failed")


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


# ── Runtime LLM settings persistence ──
#
# Admins set the model / API endpoint / API key at runtime via the admin
# Settings modal (PUT /api/settings). Non-secret values live in plaintext
# in `platform_settings`; the API key is stored Fernet-encrypted (see
# secretbox) so it never lands in the DB file or backups in the clear.
# At startup we load and apply any persisted values; precedence is
# persisted > environment.

_S_MODEL = "llm_model"
_S_BASE_URL = "llm_base_url"
_S_PROTOCOL = "llm_protocol"
_S_API_KEY_ENC = "llm_api_key_enc"


def _persist_llm_settings(
    *,
    model: str | None = None,
    base_url: str | None = None,
    protocol: str | None = None,
    api_key: str | None = None,
) -> bool:
    """Persist non-secret LLM settings (plaintext) and the API key
    (encrypted). Returns whether the API key was persisted — False when a
    key was supplied but no master key (ELENCHUS_SECRET_KEY) is configured,
    in which case the key is live-only and won't survive a restart."""
    reg = get_registry()
    con = reg.platform_con()
    key_persisted = False
    with reg.platform_lock:
        if model:
            pdb.set_setting(con, _S_MODEL, model)
        if base_url is not None:  # "" explicitly clears back to the default endpoint
            pdb.set_setting(con, _S_BASE_URL, base_url)
        if protocol:
            pdb.set_setting(con, _S_PROTOCOL, protocol)
        if api_key:
            if secretbox.is_available():
                pdb.set_setting(con, _S_API_KEY_ENC, secretbox.encrypt(api_key))
                key_persisted = True
            else:
                logger.warning(
                    "API key set at runtime but NOT persisted: ELENCHUS_SECRET_KEY "
                    "is unset, so it cannot be stored encrypted and will be lost on restart."
                )
    return key_persisted


def _apply_persisted_llm_settings() -> None:
    """Load persisted LLM settings and reconfigure the opponent. Called at
    startup after platform migrations. Persisted values override the
    environment-derived configuration the opponent booted with."""
    con = get_registry().platform_con()
    model = pdb.get_setting(con, _S_MODEL)
    base_url = pdb.get_setting(con, _S_BASE_URL)
    protocol = pdb.get_setting(con, _S_PROTOCOL)
    enc = pdb.get_setting(con, _S_API_KEY_ENC)
    api_key = secretbox.decrypt(enc) if enc else None
    if enc and api_key is None:
        logger.warning(
            "A persisted API key exists but could not be decrypted "
            "(ELENCHUS_SECRET_KEY is unset or has changed); leaving it unset."
        )
    if any(v is not None for v in (model, base_url, protocol, api_key)):
        opponent.reconfigure(model=model, api_key=api_key, base_url=base_url, protocol=protocol)
        logger.info(
            "Applied persisted LLM settings: model=%s, base_url=%s, api_key=%s",
            model,
            base_url or "(default)",
            "set" if api_key else "unchanged",
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


def _resolve_session_base(session_id: int, actor: dict) -> str:
    """Resolve a session id to the base name it addresses, for the
    session-keyed API. Returns the base name (the internal storage key).

    Missing sessions and sessions owned by another actor BOTH return 404
    — the same name-existence leak-prevention posture as base access.
    Admins bypass ownership. Sessions without a bound base (e.g. a study
    session still in briefing) are treated as not-found for this API,
    which only addresses working dialectic sessions.
    """
    sess = pdb.find_session(get_registry().platform_con(), session_id)
    if sess is None or sess.get("base_id") is None:
        raise HTTPException(404, f"Session {session_id} not found")
    if sess["actor_id"] != actor["id"] and actor.get("kind") != "admin":
        raise HTTPException(404, f"Session {session_id} not found")
    return sess["base_id"]


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


def _is_baseline_for_actor_and_base(actor_id: int, base_id: str) -> bool:
    """Whether `actor_id` has a live participant session in the
    `baseline` condition whose `base_id` matches the request's base.

    Used by the message route to choose between the dialectic
    opponent (Elenchus condition) and the free-form chat path
    (baseline). Returns False for any actor without a live session,
    or with a session bound to a different base — i.e. the default
    is always the dialectic path.
    """
    try:
        session = pdb.find_live_session_for_actor(get_registry().platform_con(), actor_id)
    except Exception:
        # Don't crash the message route on a routing-lookup failure;
        # fall back to dialectic mode (the safe default for any
        # actor who isn't a Sloan participant).
        logger.exception("Baseline-routing lookup failed; defaulting to dialectic")
        return False
    if session is None:
        return False
    if session.get("condition") != "baseline":
        return False
    # The attached task base — and, by naming convention, the tutorial
    # practice base — both run in the participant's condition. The
    # practice base is never attached to the session (only the task
    # base is, for export/analysis), so it's matched by name.
    if session.get("base_id") == base_id:
        return True
    return base_id == f"practice-{session['id']}"


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


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class SetPasswordRequest(BaseModel):
    new_password: str


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
    # The writing task for this session: a short title (also names the
    # task base, so the opponent sees it as the dialectic's topic) and
    # the longer framing shown in the writing pane.
    topic_title: str | None = ""
    topic_brief: str | None = ""


class TextAssignmentRequest(BaseModel):
    """Body for `POST /api/admin/study/{study_id}/text-assignments`.
    Omit `text_ids` to assign every submitted text in the study that
    the judge doesn't already have."""

    judge_actor_id: int
    text_ids: list[int] | None = None


class TextRatingRequest(BaseModel):
    """Body for `POST /api/judge/texts/{assignment_id}/rate`."""

    ratings: dict  # dimension key → integer score; see text_judging.py
    justification: str | None = ""
    condition_guess: str | None = None  # 'elenchus' | 'baseline' | 'unsure'
    confidence: int | None = None  # 1–7
    seconds_spent: int | None = None  # how long the form was open


class StudyConfigRequest(BaseModel):
    """Body for `PUT /api/admin/study/{study_id}/config`: the study's
    two topics and the minimum gap between a participant's sessions."""

    topic_a_title: str
    topic_a_brief: str = ""
    topic_b_title: str
    topic_b_brief: str = ""
    min_gap_hours: int = 48
    # How long this study's main task is meant to take (the writing
    # pane's clock). None = the server's default. Guidance only.
    task_minutes: int | None = None


class EnrolParticipantRequest(BaseModel):
    """Body for `POST /api/admin/study/{study_id}/participants`.

    Leave the allocation fields unset to draw the participant's cell by
    permuted-block randomization. Set **both** to place them by hand —
    for a replacement who should take the cell of the person replaced."""

    display_name: str
    first_condition: str | None = None  # 'elenchus' | 'baseline'
    first_topic: str | None = None  # 'A' | 'B'
    notes: str | None = ""


class StudyTextRequest(BaseModel):
    """Body for `PUT /api/study/session/text` (autosave) and
    `POST /api/study/session/finish` (submit)."""

    content: str
    trigger: str = "autosave"  # 'autosave' | 'blur' | 'paste'


class EditorEventsRequest(BaseModel):
    """Body for `POST /api/study/session/text/events`. Each event is
    `{type, ...}`; see `study_text.EDITOR_EVENT_PAYLOAD_KEYS` for the
    types and the payload keys kept."""

    events: list[dict]


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


@app.get("/api/auth/invites/{token}")
def peek_invite(token: str):
    """What the sign-up page needs to know about an invitation before
    the person fills it in: the role it grants, and whether it already
    carries their email. An invite issued without one is only usable if
    the form asks for it — it used not to, which made every such invite
    a dead end. Holding the token is the authorization; an unknown, used
    or expired token is a 404 and says nothing more."""
    invite = pdb.find_invite(get_registry().platform_con(), token)
    if invite is None or invite.get("consumed_at") is not None:
        raise HTTPException(404, "This invitation isn't valid (it may have been used already).")
    expires = invite.get("expires_at")
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            raise HTTPException(404, "This invitation has expired — ask for a new one.")
    return {"role": invite["role"], "needs_email": not invite.get("intended_email")}


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
    """Email a login link — **only to an active, registered account**,
    rate-limited (`auth.magic_link_recipient`). The response is the same
    whether or not anything was sent, so it doesn't reveal which
    addresses are registered; but this form is public, and it must not
    be a way to make the server email strangers."""
    actor = auth.magic_link_recipient(req.email)
    if actor is not None:
        base_url = str(request.base_url).rstrip("/")
        token = auth.issue_magic_link(actor["email"])
        try:
            from . import email_service

            email_service.send_magic_link_email(
                token=token, recipient=actor["email"], base_url=base_url
            )
        except Exception:
            logger.exception("Failed to send magic-link email")
    else:
        logger.info("Login link requested for an address with no active account; nothing sent")
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
        "must_change_password": bool(actor.get("must_change_password")),
    }


@app.post("/api/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Self-service reset request. Always returns 200 with the same body
    (no email enumeration). If the email belongs to an active,
    password-holding account and isn't rate-limited, issue a reset token
    and email the link (console backend just logs it). Does NOT revoke any
    sessions — otherwise anyone could log a user out by typing their email."""
    from . import email_service

    con = get_registry().platform_con()
    actor = pdb.find_actor_by_email(con, req.email) if req.email else None
    if (
        actor
        and actor.get("deactivated_at") is None
        and actor.get("password_hash")
        and not auth.reset_rate_limited(actor["id"])
    ):
        ip = request.client.host if request.client else None
        token = auth.issue_password_reset(actor["id"], request_ip=ip)
        base_url = str(request.base_url).rstrip("/")
        try:
            email_service.send_password_reset_email(token, actor["email"], base_url)
        except Exception:
            logger.exception("Failed to send password-reset email")
    return {"status": "ok"}


@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    """Consume a reset token and set a new password. Revokes all of the
    actor's sessions (no session issued here — they sign in fresh)."""
    complaint = auth.password_complaint(req.new_password)
    if complaint:
        raise HTTPException(400, complaint)
    actor = auth.consume_password_reset(req.token, req.new_password)
    if actor is None:
        raise HTTPException(400, "This reset link is invalid or has expired.")
    if actor.get("email"):
        from . import email_service

        try:
            email_service.send_password_changed_notification(actor["email"])
        except Exception:
            logger.exception("Failed to send password-changed notification")
    return {"status": "ok"}


@app.post("/api/auth/set-password")
def set_password(
    req: SetPasswordRequest, response: Response, actor: dict = Depends(auth.current_actor)
):
    """Set a new password during a forced change (must_change_password).
    Only valid when the flag is set — normal changes use change-password
    with the old password. Re-issues the current session afterward."""
    if not actor.get("must_change_password"):
        raise HTTPException(403, "No password change is required for this account.")
    complaint = auth.password_complaint(req.new_password)
    if complaint:
        raise HTTPException(400, complaint)
    auth.force_set_password(actor["id"], req.new_password)
    token = auth.create_session(actor["id"])
    _set_session_cookie(response, token)
    if actor.get("email"):
        from . import email_service

        try:
            email_service.send_password_changed_notification(actor["email"])
        except Exception:
            logger.exception("Failed to send password-changed notification")
    return {"status": "ok"}


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
    issued = invites.issue_invite_with_outcome(
        role=req.role,
        issued_by=actor["id"],
        intended_email=req.intended_email,
        ttl=ttl,
        base_url=base_url,
    )
    return {
        "token": issued["token"],
        "role": req.role,
        "intended_email": req.intended_email,
        # True = emailed; False = the email FAILED (pass the link on by
        # hand); None = nothing to email, or no mail backend.
        "emailed": issued["emailed"],
    }


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


class CostBudgetRequest(BaseModel):
    """The budget lines the cost dashboard reports against — LLM,
    infrastructure, or both, over one period. All fields None clears it."""

    llm_usd: float | None = None
    infra_usd: float | None = None
    period_start: str | None = None
    period_end: str | None = None
    label: str | None = None


@app.get("/api/admin/costs")
def admin_costs(
    days: int = 30,
    actor: dict = Depends(auth.require_admin),
):
    """The cost dashboard: LLM spend priced **at read time** from the
    recorded token counts (`costs.py`), so a corrected price table
    corrects the history too and an unpriced model is reported as
    unpriced rather than as $0. `days` is the window for the by-day /
    by-model / by-purpose / by-actor / waste views (0 = all time);
    totals, the study rollup and the budget line ignore it."""
    if days < 0 or days > 3660:
        raise HTTPException(422, "days must be between 0 and 3660")
    reg = get_registry()
    with reg.platform_lock:
        return costs_mod.build_report(reg.platform_con(), days=days)


@app.put("/api/admin/costs/budget")
def admin_set_cost_budget(
    req: CostBudgetRequest,
    actor: dict = Depends(auth.require_admin),
):
    """Set (or clear) the LLM budget line shown on the cost dashboard.
    Display only — nothing is cut off when it is exceeded."""
    reg = get_registry()
    clearing = (
        req.llm_usd is None
        and req.infra_usd is None
        and not req.period_start
        and not req.period_end
    )
    with reg.platform_lock:
        try:
            budget = costs_mod.set_budget(
                reg.platform_con(), None if clearing else req.model_dump()
            )
        except ValueError as e:
            raise HTTPException(422, detail={"user_message": str(e)}) from None
    logger.info(
        "Cost budget %s by actor=%s: %s", "cleared" if clearing else "set", actor["id"], budget
    )
    return {"budget": budget}


# ── System (admin dashboard) ──
#
# What an admin needs to keep the platform healthy, without a shell:
# is it up and on which release, can it reach the LLM, can it send mail,
# what has it been alerting about, when was it last backed up.

_SERVER_STARTED_AT = datetime.now(UTC)


@app.get("/api/admin/system")
def admin_system(actor: dict = Depends(auth.require_admin)):
    """One read for the System tab: release and schema, LLM and email
    configuration (never a secret), disk space, backups, and the newest
    alerts."""
    import shutil

    reg = get_registry()
    con = reg.platform_con()
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    backups_dir = os.path.join(DATA_DIR, "backups")
    backups = []
    for path in backup_mod.list_backups(backups_dir)[:10]:
        with contextlib.suppress(OSError):
            stat = os.stat(path)
            backups.append(
                {
                    "name": os.path.basename(path),
                    "size_bytes": stat.st_size,
                    "modified_utc": datetime.fromtimestamp(stat.st_mtime, UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            )
    disk = shutil.disk_usage(DATA_DIR)
    alerts = alerting_mod.list_alerts(con, limit=50)
    day_ago = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "version": elenchus_version,
        "schema_version": int(row[0]) if row and row[0] else None,
        "started_utc": _SERVER_STARTED_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server_timezone": time.tzname[0],
        "llm": _settings_payload(),
        "email": {
            "backend": email_service_mod.active_backend(),
            "enabled": email_service_mod.active_backend() == "smtp",
            "alert_email_to": bool(os.environ.get("ALERT_EMAIL_TO", "").strip()),
            # The most recent send since the server started — being
            # configured to send mail is not the same as mail arriving.
            "last_delivery": email_service_mod.delivery_status(),
        },
        "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
        "backups": backups,
        "backups_total": len(backup_mod.list_backups(backups_dir)),
        "alerts": alerts,
        "alerts_last_24h": sum(1 for a in alerts if a["at_utc"] >= day_ago),
        "phase_b_enabled": opponent.enable_phase_b,
    }


@app.put("/api/admin/costs/alert")
def admin_set_cost_alert(payload: dict, actor: dict = Depends(auth.require_admin)):
    """Set the daily LLM spend that triggers an alert (`cost_alerts.py`):
    `{"daily_usd": 25}`; 0 turns it off; null goes back to the
    environment variable / default. Alerting only — nothing is cut off."""
    from . import cost_alerts

    reg = get_registry()
    with reg.platform_lock:
        try:
            threshold, source = cost_alerts.set_threshold(
                reg.platform_con(), payload.get("daily_usd")
            )
        except ValueError as e:
            raise HTTPException(422, detail={"user_message": str(e)}) from None
    logger.info(
        "Daily spend alert threshold set to $%.2f (%s) by actor=%s", threshold, source, actor["id"]
    )
    return {"daily_usd": threshold, "source": source}


# ── Infrastructure ledger (cost_ledger.py) ──
#
# Hosting / domain / email charges can't be measured the way LLM tokens
# are: an admin records them from invoices. Payloads are plain dicts —
# `cost_ledger.validate_*` owns the rules and the messages, so the
# route, the CLI and the tests can't drift apart.


def _ledger_call(fn, *args, **kwargs):
    """Run a ledger write under the platform lock, turning its
    ValueError / LookupError into the structured 422 / 404 the UI shows."""
    reg = get_registry()
    with reg.platform_lock:
        try:
            return fn(reg.platform_con(), *args, **kwargs)
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, detail={"user_message": str(e)}) from None


@app.get("/api/admin/costs/ledger")
def admin_cost_ledger(actor: dict = Depends(auth.require_admin)):
    """Every ledger entry (voided ones included, flagged) and every
    recurring charge, with the category vocabulary for the forms."""
    reg = get_registry()
    with reg.platform_lock:
        con = reg.platform_con()
        return {
            "entries": cost_ledger_mod.list_entries(con),
            "recurring": cost_ledger_mod.list_recurring(con),
            "categories": cost_ledger_mod.CATEGORIES,
            "infra_categories": list(cost_ledger_mod.INFRA_CATEGORIES),
            "reconciliation_category": cost_ledger_mod.RECONCILIATION_CATEGORY,
            "provider_imports": provider_report_mod.list_imports(con),
        }


@app.post("/api/admin/costs/provider-report")
def admin_cost_provider_report(payload: dict, actor: dict = Depends(auth.require_admin)):
    """Upload a provider report — the file `elenchus-provider-report`
    writes, off the box, from the LLM provider's own usage and cost
    figures. Replaces the stored rows for the days it covers; the Costs
    tab then shows them beside the platform's own figures."""
    reg = get_registry()
    with reg.platform_lock:
        try:
            return provider_report_mod.import_report(
                reg.platform_con(), payload, actor_id=actor["id"]
            )
        except provider_report_mod.ProviderReportError as e:
            raise HTTPException(422, detail={"user_message": str(e)}) from None


@app.post("/api/admin/costs/ledger")
def admin_cost_ledger_add(payload: dict, actor: dict = Depends(auth.require_admin)):
    """Record a charge (or a credit: a negative amount)."""
    return {"entry": _ledger_call(cost_ledger_mod.create_entry, payload, actor_id=actor["id"])}


@app.put("/api/admin/costs/ledger/{entry_id}")
def admin_cost_ledger_edit(
    entry_id: int, payload: dict, actor: dict = Depends(auth.require_admin)
):
    """Correct an entry — e.g. replace an estimate with the invoiced
    amount and clear `estimated`. The change is logged field by field."""
    return {
        "entry": _ledger_call(
            cost_ledger_mod.update_entry, entry_id, payload, actor_id=actor["id"]
        )
    }


@app.post("/api/admin/costs/ledger/{entry_id}/void")
def admin_cost_ledger_void(
    entry_id: int, payload: dict | None = None, actor: dict = Depends(auth.require_admin)
):
    """Take an entry out of every sum. Nothing is deleted."""
    reason = str((payload or {}).get("reason") or "")
    return {
        "entry": _ledger_call(
            cost_ledger_mod.void_entry, entry_id, actor_id=actor["id"], reason=reason
        )
    }


@app.post("/api/admin/costs/ledger/record-recurring")
def admin_cost_ledger_record_recurring(payload: dict, actor: dict = Depends(auth.require_admin)):
    """Enter a month's expected recurring charges as estimated entries.
    Idempotent: charges already recorded for the month are skipped."""
    return _ledger_call(
        cost_ledger_mod.record_recurring_month,
        str(payload.get("month") or ""),
        actor_id=actor["id"],
    )


@app.post("/api/admin/costs/recurring")
def admin_cost_recurring_add(payload: dict, actor: dict = Depends(auth.require_admin)):
    """Add a charge expected every month or year."""
    return {
        "recurring": _ledger_call(cost_ledger_mod.create_recurring, payload, actor_id=actor["id"])
    }


@app.put("/api/admin/costs/recurring/{recurring_id}/end")
def admin_cost_recurring_end(
    recurring_id: int, payload: dict, actor: dict = Depends(auth.require_admin)
):
    """Stop expecting a recurring charge after `ends_on`."""
    return {
        "recurring": _ledger_call(
            cost_ledger_mod.end_recurring,
            recurring_id,
            ends_on=payload.get("ends_on"),
            actor_id=actor["id"],
        )
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


class ChangeRoleRequest(BaseModel):
    role: str


@app.put("/api/admin/users/{user_id}/role")
def admin_change_user_role(
    user_id: int,
    req: ChangeRoleRequest,
    actor: dict = Depends(auth.require_admin),
):
    """Move a person between `admin`, `researcher`, `user` and `judge`.
    The actor keeps their id, so everything they did (studies set up,
    participants enrolled, dialectics owned) stays theirs, and the
    change applies from their next request — no re-login.

    Refuses: your own role (another admin changes it, so nobody locks
    themselves out by accident); demoting the last active admin; the
    platform's own identities (participants, the opponent, system);
    and **any change to or from `judge` once judging work has been
    assigned** — a judge promoted to researcher or admin would see the
    unblinded study data for texts they are rating.
    """
    reg = get_registry()
    con = reg.platform_con()
    role = (req.role or "").strip().lower()
    if role not in pdb.ASSIGNABLE_KINDS:
        raise HTTPException(
            422,
            detail={"user_message": "Choose one of: " + ", ".join(pdb.ASSIGNABLE_KINDS) + "."},
        )
    target = pdb.find_actor_by_id(con, user_id)
    if target is None:
        raise HTTPException(404, f"Actor #{user_id} not found")
    if target["kind"] not in pdb.ASSIGNABLE_KINDS:
        raise HTTPException(
            400,
            detail={"user_message": f"A {target['kind']} account's role can't be changed."},
        )
    if target["kind"] == role:
        return {"status": "unchanged", "id": user_id, "role": role}
    if user_id == actor["id"]:
        raise HTTPException(
            400, detail={"user_message": "You can't change your own role — ask another admin."}
        )
    if (
        target["kind"] == "admin"
        and target.get("deactivated_at") is None
        and pdb.count_active_admins(con) <= 1
    ):
        raise HTTPException(
            400,
            detail={"user_message": "This is the last active admin; promote someone else first."},
        )
    if "judge" in (target["kind"], role) and pdb.count_judging_assignments(con, user_id) > 0:
        raise HTTPException(
            409,
            detail={
                "user_message": (
                    "This account has judging work assigned. Changing its role would break "
                    "the blinding of those ratings, so it isn't allowed."
                )
            },
        )
    with reg.platform_lock:
        pdb.update_actor_kind(con, user_id, role)
    logger.info(
        "Actor #%d role changed %s -> %s by admin #%d", user_id, target["kind"], role, actor["id"]
    )
    return {"status": "changed", "id": user_id, "role": role, "previous_role": target["kind"]}


@app.put("/api/admin/users/{user_id}/reactivate")
def admin_reactivate_user(
    user_id: int,
    require_password_change: bool = False,
    actor: dict = Depends(auth.require_admin),
):
    """Undo a deactivation. The actor can log in again; previously-revoked
    session cookies are NOT restored (they log in fresh). With
    `require_password_change=true`, they're forced to set a new password at
    that next login (use this when the deactivation was security-related)."""
    reg = get_registry()
    con = reg.platform_con()
    target = pdb.find_actor_by_id(con, user_id)
    if target is None:
        raise HTTPException(404, f"Actor #{user_id} not found")
    if target.get("deactivated_at") is None:
        return {"status": "already_active", "id": user_id}
    with reg.platform_lock:
        pdb.reactivate_actor(con, user_id)
        if require_password_change:
            pdb.set_must_change_password(con, user_id, True)
    logger.info(
        "Actor #%d reactivated by admin #%d (require_password_change=%s)",
        user_id,
        actor["id"],
        require_password_change,
    )
    return {
        "status": "reactivated",
        "id": user_id,
        "require_password_change": require_password_change,
    }


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int, request: Request, actor: dict = Depends(auth.require_admin)
):
    """Admin-initiated password reset. Issues a 24h one-time reset link,
    force-logs-out the user immediately (revokes their sessions), emails the
    link if SMTP is configured, and ALWAYS returns the shareable link so it
    works without email."""
    from . import email_service

    reg = get_registry()
    con = reg.platform_con()
    target = pdb.find_actor_by_id(con, user_id)
    if target is None:
        raise HTTPException(404, f"Actor #{user_id} not found")
    if not target.get("email"):
        raise HTTPException(400, "This account has no email to send a reset link to.")
    token = auth.issue_password_reset(user_id, ttl=auth.ADMIN_RESET_TTL, created_by=actor["id"])
    with reg.platform_lock:
        pdb.revoke_actor_sessions(con, user_id)  # admin reset = force logout now
    base_url = str(request.base_url).rstrip("/")
    emailed = email_service.active_backend() == "smtp"
    try:
        email_service.send_password_reset_email(token, target["email"], base_url)
    except Exception:
        logger.exception("Failed to send admin password-reset email")
        emailed = False
    logger.info("Admin #%d issued a password reset for actor #%d", actor["id"], user_id)
    return {
        "status": "reset_issued",
        "id": user_id,
        "reset_url": f"{base_url}/?reset={token}",
        "emailed": emailed,
        "email": target["email"],
    }


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
            topic_title=(req.topic_title or "").strip(),
            topic_brief=(req.topic_brief or "").strip(),
        )
    logger.info(
        "Issued participant token: study=%s condition=%s topic=%r actor=%d (by %d)",
        req.study_id,
        req.condition,
        (req.topic_title or "").strip(),
        participant_id,
        actor["id"],
    )
    return {
        "token": token,
        "participant_actor_id": participant_id,
        "study_id": req.study_id,
        "condition": req.condition,
        "display_name": req.display_name,
        "topic_title": (req.topic_title or "").strip(),
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


@app.get("/api/admin/study/configs")
def admin_list_study_configs(actor: dict = Depends(auth.require_researcher)):
    return {
        "studies": pdb.list_study_configs(get_registry().platform_con()),
        # What a study with no task length of its own gets.
        "default_task_minutes": _task_minutes(),
    }


@app.put("/api/admin/study/{study_id}/config")
def admin_set_study_config(
    study_id: str,
    req: StudyConfigRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Set up (or edit) a study: its two topics and the minimum gap
    between a participant's sessions.

    Topics are copied onto each participant's links when they are
    enrolled, so editing them here affects **future enrolments only** —
    a participant already holding links keeps the wording they were
    issued. The gap, by contrast, is read when a second link is opened."""
    study_id = study_id.strip()
    if not study_id:
        raise HTTPException(400, "study_id is required")
    if not req.topic_a_title.strip() or not req.topic_b_title.strip():
        raise HTTPException(400, "Both topics need a title")
    if req.topic_a_title.strip() == req.topic_b_title.strip():
        raise HTTPException(400, "The two topics must be different")
    if req.min_gap_hours < 0:
        raise HTTPException(400, "min_gap_hours can't be negative")
    if req.task_minutes is not None and not 1 <= req.task_minutes <= 600:
        raise HTTPException(400, "task_minutes must be between 1 and 600 (or empty)")
    reg = get_registry()
    before = pdb.find_study_config(reg.platform_con(), study_id)
    with reg.platform_lock:
        config = pdb.upsert_study_config(
            reg.platform_con(),
            study_id=study_id,
            topic_a_title=req.topic_a_title.strip(),
            topic_a_brief=req.topic_a_brief.strip(),
            topic_b_title=req.topic_b_title.strip(),
            topic_b_brief=req.topic_b_brief.strip(),
            min_gap_hours=req.min_gap_hours,
            task_minutes=req.task_minutes,
            actor_id=actor["id"],
        )
    logger.info(
        "Study config set: study=%s topics=(%r, %r) min_gap_hours=%d task_minutes=%s "
        "(was %s) (by %d)",
        study_id,
        config["topics"]["A"]["title"],
        config["topics"]["B"]["title"],
        config["min_gap_hours"],
        config["task_minutes"],
        before["task_minutes"] if before else "—",
        actor["id"],
    )
    return {**config, "default_task_minutes": _task_minutes()}


@app.get("/api/admin/study/{study_id}/config")
def admin_get_study_config(study_id: str, actor: dict = Depends(auth.require_researcher)):
    config = pdb.find_study_config(get_registry().platform_con(), study_id)
    if config is None:
        raise HTTPException(404, f"Study '{study_id}' has not been set up")
    return config


def _participant_view(con, participant: dict) -> dict:
    """A roster row: the participant, their allocation, and their two
    sessions with link, status and whether a text has been submitted."""
    sessions = []
    for period in (1, 2):
        token = pdb.find_participant_period_token(con, participant["id"], period)
        if token is None:
            continue
        session = pdb.find_study_session(con, token["session_id"]) if token["session_id"] else None
        gate = pdb.second_session_gate(con, token) if token["status"] == "scheduled" else None
        sessions.append(
            {
                "period": period,
                "condition": token["condition"],
                "topic_title": token["topic_title"],
                "token": token["token"],
                "token_status": token["status"],
                "session_id": token["session_id"],
                "session_state": session["state"] if session else None,
                "text_submitted": bool(
                    session and pdb.find_study_text_for_session(con, session["id"]) is not None
                ),
                "gate": gate,
            }
        )
    return {**participant, "sessions": sessions}


@app.post("/api/admin/study/{study_id}/participants")
def admin_enrol_participant(
    study_id: str,
    req: EnrolParticipantRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Enrol one person in the crossover: allocate their cell, and issue
    both of their session links in one step.

    The cell — which condition and which topic they meet first — is
    drawn by permuted-block randomization (`study_enrolment.next_cell`)
    unless the researcher places them by hand. Doing both links here,
    rather than issuing tokens one at a time, is what guarantees a
    participant gets each condition once and each topic once, and that
    their two sessions are linked in the data."""
    if not req.display_name.strip():
        raise HTTPException(400, "display_name is required")
    manual = req.first_condition is not None or req.first_topic is not None
    if manual and (req.first_condition is None or req.first_topic is None):
        raise HTTPException(400, "Set both first_condition and first_topic, or neither")

    reg = get_registry()
    con = reg.platform_con()
    config = pdb.find_study_config(con, study_id)
    if config is None:
        raise HTTPException(
            409, f"Set up study '{study_id}' (its two topics) before enrolling participants"
        )

    with reg.platform_lock:
        existing = pdb.list_study_participants(con, study_id)
        if manual:
            try:
                cell = study_enrolment.Cell(req.first_condition, req.first_topic)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        else:
            cell = study_enrolment.next_cell(
                [
                    study_enrolment.Cell(p["first_condition"], p["first_topic"])
                    for p in existing
                    if p["allocation"] == "block"
                ]
            )
        code = study_enrolment.participant_code(len(existing) + 1)
        participant_id = pdb.create_study_participant(
            con,
            study_id=study_id,
            participant_code=code,
            display_name=req.display_name.strip(),
            first_condition=cell.first_condition,
            first_topic=cell.first_topic,
            allocation="manual" if manual else "block",
            enrolled_by=actor["id"],
            notes=(req.notes or "").strip(),
        )
        for plan in study_enrolment.session_plan(cell):
            topic = config["topics"][plan["topic"]]
            # One passwordless actor per link, as for hand-issued tokens:
            # the actor is the session's identity, the participant row
            # the person's.
            session_actor = pdb.create_actor(
                con,
                kind="participant",
                email=None,
                display_name=f"{code} · session {plan['period']}",
                password_hash=None,
            )
            pdb.create_participant_token(
                con,
                token=auth.generate_token(),
                actor_id=session_actor,
                study_id=study_id,
                condition=plan["condition"],
                issued_by=actor["id"],
                topic_title=topic["title"],
                topic_brief=topic["brief"],
                participant_id=participant_id,
                period=plan["period"],
            )
    logger.info(
        "Enrolled participant: study=%s code=%s cell=%s allocation=%s (by %d)",
        study_id,
        code,
        cell.key,
        "manual" if manual else "block",
        actor["id"],
    )
    return _participant_view(con, pdb.find_study_participant(con, participant_id))


@app.get("/api/admin/study/{study_id}/participants")
def admin_list_study_participants(study_id: str, actor: dict = Depends(auth.require_researcher)):
    """The study roster, in enrolment order, with each cell's count so
    the researcher can see the balance at a glance."""
    con = get_registry().platform_con()
    participants = pdb.list_study_participants(con, study_id)
    cells = {c.key: 0 for c in study_enrolment.ALL_CELLS}
    for p in participants:
        cells[study_enrolment.Cell(p["first_condition"], p["first_topic"]).key] += 1
    return {
        "study_id": study_id,
        "participants": [_participant_view(con, p) for p in participants],
        "cell_counts": cells,
    }


@app.post("/api/admin/study/sessions/{session_id}/interrupt")
def admin_interrupt_session(session_id: int, actor: dict = Depends(auth.require_researcher)):
    """Close a session the participant abandoned (browser closed
    mid-task and never resumed). An open first session holds a
    participant's second link shut, so the researcher needs a way to
    end it. The session is marked `interrupted`, not deleted: everything
    captured up to that point stays in the export."""
    reg = get_registry()
    con = reg.platform_con()
    session = pdb.find_study_session(con, session_id)
    if session is None or not session.get("study_token"):
        raise HTTPException(404, "Study session not found")
    with reg.platform_lock:
        updated = pdb.advance_session_state(con, session_id, "interrupted")
    if updated is None:
        raise HTTPException(
            409, f"Session is already closed (state: {session['state']}) — nothing to interrupt"
        )
    logger.info(
        "Session interrupted by researcher: session=%d was_state=%s (by %d)",
        session_id,
        session["state"],
        actor["id"],
    )
    return {"session_id": session_id, "state": updated["state"], "was_state": session["state"]}


def _second_session_message(gate: dict) -> str:
    if gate["reason"] == "first_not_started":
        return (
            "This is the link for your second session. Please do your first session "
            "first — use the other link you were sent."
        )
    if gate["reason"] == "first_still_open":
        return (
            "Your first session is still open. Please finish it (use your first link), "
            "or contact the researcher."
        )
    # `opens_at` is an aware UTC datetime. The page re-renders this in
    # the participant's own time zone (it gets the ISO instant alongside);
    # this wording is the fallback, and it says UTC because it is.
    when = gate["opens_at"].strftime("%A %d %B at %H:%M UTC")
    return (
        f"Your second session isn't open yet — the two sessions are kept apart. "
        f"This link will work from {when}."
    )


@app.post("/api/study/{token}")
def consume_participant_token(token: str, response: Response):
    """Public endpoint — the participant clicks the emailed link and
    this trades the token for a session cookie tied to the underlying
    participant actor.

    First click consumes the token (`scheduled` → `active`) and opens a
    `sessions` row in the `briefing` state — the participant's lifecycle
    anchor. The `GET /api/study/session` route reads from it, `advance`
    writes to it.

    The same link doubles as a **resume link**: clicking it again while
    the participant still has a live (non-terminal) session re-issues a
    session cookie and routes them back to their current state — that's
    how they get back in from a new device or after losing the cookie,
    since the passwordless model has no other re-entry path. Only a
    terminal session (complete/expired/interrupted), a voided/expired
    token, or being outside the scheduled window returns 410 Gone (with a
    structured body so the frontend renders one message).
    """
    reg = get_registry()
    # An enrolled participant's second link stays shut until their first
    # session has ended and the study's minimum gap has passed. Checked
    # before consuming, and only for a link that hasn't been used yet —
    # resuming a second session already under way is never blocked.
    pending = pdb.find_participant_token(reg.platform_con(), token)
    if pending is not None and pending["status"] == "scheduled":
        gate = pdb.second_session_gate(reg.platform_con(), pending)
        if gate is not None:
            logger.info(
                "Second-session link held: study=%s participant_id=%s reason=%s opens_at=%s",
                pending["study_id"],
                pending["participant_id"],
                gate["reason"],
                gate.get("opens_at"),
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "not_yet",
                    "reason": gate["reason"],
                    "opens_at": gate["opens_at"].isoformat() if gate.get("opens_at") else None,
                    "user_message": _second_session_message(gate),
                },
            )
    with reg.platform_lock:
        consumed = pdb.consume_participant_token(reg.platform_con(), token)
    if consumed is None:
        existing = pdb.find_participant_token(reg.platform_con(), token)
        if existing is None:
            raise HTTPException(404, "Token not found")
        # ── Resume ──────────────────────────────────────────────────
        # A token that's already been consumed (`active`) whose
        # participant still has a live (non-terminal) session is a
        # *resume*, not an error: the same link doubles as a resume link.
        # This is how a participant gets back in from a new device, or
        # after losing the cookie — the passwordless model has no other
        # re-entry path. Re-issue a session cookie and route them back to
        # their current state without creating a new session. (Voided /
        # expired tokens, and consumed tokens whose session has reached a
        # terminal state, fall through to 410.)
        if existing["status"] == "active":
            live = pdb.find_live_session_for_actor(reg.platform_con(), existing["actor_id"])
            if live is not None:
                session_token = auth.create_session(existing["actor_id"])
                _set_session_cookie(response, session_token)
                logger.info(
                    "Participant token resumed: study=%s condition=%s actor=%d session_id=%d state=%s",
                    existing["study_id"],
                    existing["condition"],
                    existing["actor_id"],
                    live["id"],
                    live["state"],
                )
                return {
                    "study_id": existing["study_id"],
                    "condition": existing["condition"],
                    "actor_id": existing["actor_id"],
                    "session_id": live["id"],
                    "state": live["state"],
                    "resumed": True,
                }
        # Token exists but isn't resumable — used up (session terminal),
        # voided, expired, or outside its scheduled window. Structured
        # detail so the frontend renders one message.
        raise HTTPException(
            status_code=410,
            detail={
                "status": existing["status"],
                "user_message": _participant_token_message(existing),
            },
        )

    # Open the participant's lifecycle session in the briefing state
    # in the same lock-held transaction so a crash between the two
    # writes can't leave a consumed-but-stateless token.
    with reg.platform_lock:
        session_id = pdb.create_study_session(
            reg.platform_con(),
            actor_id=consumed["actor_id"],
            study_token=token,
            condition=consumed["condition"],
            initial_state="briefing",
        )
        # Link the token row back to its session so the researcher
        # dashboard can join tokens → sessions → reports.
        pdb.set_token_session(reg.platform_con(), token, session_id)

    # Issue a session cookie for the participant actor. Same shape
    # as the regular login flow.
    session_token = auth.create_session(consumed["actor_id"])
    _set_session_cookie(response, session_token)
    logger.info(
        "Participant token consumed: study=%s condition=%s actor=%d session_id=%d",
        consumed["study_id"],
        consumed["condition"],
        consumed["actor_id"],
        session_id,
    )
    return {
        "study_id": consumed["study_id"],
        "condition": consumed["condition"],
        "actor_id": consumed["actor_id"],
        "session_id": session_id,
        "state": "briefing",
    }


# ─── Phase D/9: per-study data export ────────────────────────────


@app.post("/api/admin/study/{study_id}/export")
def admin_export_study(
    study_id: str,
    actor: dict = Depends(auth.require_researcher),
):
    """Build the analysis-ready archive for one study. Returns the
    archive path, the (separately held) pseudonym-map path, and the
    per-session outcome list. Individual session failures are
    reported, not fatal.

    The archive lands under `{data_dir}/exports/` on the server; the
    researcher retrieves it out-of-band (scp, sftp) — streaming a
    multi-hundred-MB tar through the API isn't worth the complexity
    at pilot scale."""
    from . import study_export

    sessions = pdb.list_sessions_for_study(get_registry().platform_con(), study_id)
    if not sessions:
        raise HTTPException(404, f"No sessions found for study {study_id!r}")

    result = study_export.export_study(study_id)
    logger.info(
        "Study export: study=%s archive=%s exported=%d failed=%d (by actor %d)",
        study_id,
        result["archive"],
        len(result["sessions_exported"]),
        len(result["sessions_failed"]),
        actor["id"],
    )
    return result


# ── Downloading exports ──
#
# An export lands in `{data_dir}/exports/` on the server. Whoever runs
# the study shouldn't need a shell to look at what they collected, so
# the files can be downloaded. Two files, two gates: the archive holds
# no names (researcher); the pseudonym map is the key from codes back to
# people (admin only, and the UI says what it is before handing it over).
# A file is served only if it is in the listing for that study — the
# name is matched against what is on disk, never joined into a path.


def _exports_dir() -> str:
    return os.path.join(os.path.dirname(get_registry().platform_path), "exports")


def _study_exports(study_id: str) -> list[dict]:
    """This study's archives, newest first."""
    import re

    pattern = re.compile(rf"^study-{re.escape(study_id)}-(\d{{8}}-\d{{6}})\.tar\.gz$")
    directory = _exports_dir()
    found = []
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            m = pattern.match(name)
            path = os.path.join(directory, name)
            if not m or not os.path.isfile(path):
                continue
            stem = name[: -len(".tar.gz")]
            found.append(
                {
                    "name": name,
                    "created": m.group(1),
                    "size_bytes": os.path.getsize(path),
                    "pseudonym_file": (
                        f"{stem}.pseudonyms.json"
                        if os.path.isfile(os.path.join(directory, f"{stem}.pseudonyms.json"))
                        else None
                    ),
                }
            )
    return sorted(found, key=lambda e: e["created"], reverse=True)


@app.get("/api/admin/study/{study_id}/exports")
def admin_list_study_exports(study_id: str, actor: dict = Depends(auth.require_researcher)):
    """The archives already made for this study, newest first."""
    return {"exports": _study_exports(study_id.strip())}


@app.get("/api/admin/study/{study_id}/exports/{name}")
def admin_download_study_export(
    study_id: str, name: str, actor: dict = Depends(auth.require_researcher)
):
    """Download one archive. It contains no names — the pseudonym map
    is a separate file behind a separate, admin-only route."""
    if name not in {e["name"] for e in _study_exports(study_id.strip())}:
        raise HTTPException(404, "No such export for this study")
    logger.info(
        "Study export downloaded: study=%s file=%s by actor=%d", study_id, name, actor["id"]
    )
    return FileResponse(
        os.path.join(_exports_dir(), name), media_type="application/gzip", filename=name
    )


@app.get("/api/admin/study/{study_id}/exports/{name}/pseudonyms")
def admin_download_study_pseudonyms(
    study_id: str, name: str, actor: dict = Depends(auth.require_admin)
):
    """Download the pseudonym map that goes with an archive: the key
    from participant codes back to the names the researcher typed. Admin
    only. It must never travel with the archive or reach a repository."""
    match = next((e for e in _study_exports(study_id.strip()) if e["name"] == name), None)
    if match is None or not match["pseudonym_file"]:
        raise HTTPException(404, "No pseudonym map for this export")
    logger.warning(
        "Pseudonym map downloaded: study=%s file=%s by actor=%d",
        study_id,
        match["pseudonym_file"],
        actor["id"],
    )
    return FileResponse(
        os.path.join(_exports_dir(), match["pseudonym_file"]),
        media_type="application/json",
        filename=match["pseudonym_file"],
    )


# ─── Phase D/8: post-session questionnaires ──────────────────────


class SurveySubmission(BaseModel):
    """Body for `POST /api/study/session/{id}/survey`. `responses`
    maps item id → integer value; validation against the instrument
    definition is strict (every item, no extras, in range)."""

    instrument: str
    responses: dict


@app.get("/api/study/instruments")
def study_instruments(actor: dict = Depends(auth.current_actor)):
    """The four post-session instrument definitions, with version.
    The frontend renders these dynamically; the export reproduces
    exactly what each participant saw."""
    from . import questionnaires

    return {"instruments": questionnaires.list_instruments()}


@app.post("/api/study/session/{session_id}/survey")
def submit_survey(
    session_id: int,
    req: SurveySubmission,
    actor: dict = Depends(auth.current_actor),
):
    """Store one questionnaire submission for a session. Owner /
    admin / researcher. Rejected whole on any validation error —
    partial submissions would poison the study data."""
    from . import questionnaires

    reg = get_registry()
    con = reg.platform_con()
    session = pdb.find_study_session(con, session_id)
    if session is None:
        raise HTTPException(404, f"Session #{session_id} not found")
    if actor["id"] != session["actor_id"] and actor.get("kind") not in (
        "admin",
        "researcher",
    ):
        raise HTTPException(403, "Not authorized to submit for this session")

    errors = questionnaires.validate_responses(req.instrument, req.responses)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    with reg.platform_lock:
        rid = pdb.record_survey_response(
            con,
            session_id=session_id,
            instrument=req.instrument,
            instrument_version=questionnaires.INSTRUMENT_VERSION,
            responses=req.responses,
        )
    logger.info("Survey stored: id=%d session=%d instrument=%s", rid, session_id, req.instrument)
    return {"id": rid, "session_id": session_id, "instrument": req.instrument}


@app.get("/api/study/session/{session_id}/surveys")
def list_session_surveys(
    session_id: int,
    actor: dict = Depends(auth.current_actor),
):
    """Submissions for one session, newest first. Owner / admin /
    researcher. The frontend uses this to mark instruments done."""
    con = get_registry().platform_con()
    session = pdb.find_study_session(con, session_id)
    if session is None:
        raise HTTPException(404, f"Session #{session_id} not found")
    if actor["id"] != session["actor_id"] and actor.get("kind") not in (
        "admin",
        "researcher",
    ):
        raise HTTPException(403, "Not authorized to view this session's surveys")
    return {"surveys": pdb.list_survey_responses_for_session(con, session_id)}


@app.get("/api/admin/study/surveys")
def admin_list_surveys(
    instrument: str | None = None,
    actor: dict = Depends(auth.require_researcher),
):
    """Researcher cohort view of all questionnaire submissions."""
    return {
        "surveys": pdb.list_survey_responses(get_registry().platform_con(), instrument=instrument)
    }


# ─── Phase D/7: blinded judge interface ──────────────────────────


class JudgePackageRequest(BaseModel):
    """Body for `POST /api/admin/study/judge-packages`."""

    study_id: str
    report_id_elenchus: int  # which Elenchus-condition report
    report_id_baseline: int  # which baseline-condition report
    randomize_slots: bool = True  # if False, elenchus → slot A
    notes: str | None = ""


class JudgeAssignmentRequest(BaseModel):
    """Body for `POST /api/admin/study/judge-assignments`."""

    judge_actor_id: int
    package_id: int


class JudgeRatingRequest(BaseModel):
    """Body for `POST /api/judge/assignments/{id}/rate`. `ratings` is
    a free-form dict mapping dimension name → {a: 1-7, b: 1-7}; the
    front-end decides which dimensions to surface."""

    ratings: dict
    justification_a: str = ""
    justification_b: str = ""
    pairwise_winner: str  # 'a' | 'b' | 'tie'
    condition_guess_a: str | None = None  # 'elenchus' | 'baseline' | 'unsure'
    condition_guess_b: str | None = None
    confidence: int | None = None  # 1-7


@app.post("/api/admin/study/judge-packages")
def admin_create_judge_package(
    req: JudgePackageRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Create a judge package from two reports. Slot assignment is
    randomized by default so the judge can't use position as a tell
    (the slot ordering across packages they see is uncorrelated with
    the condition)."""
    import secrets

    reg = get_registry()
    con = reg.platform_con()
    # Validate both reports exist and have the right conditions.
    rows = con.execute(
        "SELECT id, condition FROM study_reports WHERE id IN (?, ?)",
        [req.report_id_elenchus, req.report_id_baseline],
    ).fetchall()
    if len(rows) != 2:
        raise HTTPException(400, "Both reports must exist")
    by_id = {r[0]: r[1] for r in rows}
    if by_id.get(req.report_id_elenchus) != "elenchus":
        raise HTTPException(400, "report_id_elenchus must reference an Elenchus-condition report")
    if by_id.get(req.report_id_baseline) != "baseline":
        raise HTTPException(400, "report_id_baseline must reference a baseline-condition report")

    # Random A/B slot assignment.
    elenchus_in_a = secrets.randbelow(2) == 0 if req.randomize_slots else True
    if elenchus_in_a:
        slot_a_report = req.report_id_elenchus
        slot_b_report = req.report_id_baseline
        slot_a_cond = "elenchus"
        slot_b_cond = "baseline"
    else:
        slot_a_report = req.report_id_baseline
        slot_b_report = req.report_id_elenchus
        slot_a_cond = "baseline"
        slot_b_cond = "elenchus"

    with reg.platform_lock:
        pid = pdb.create_judge_package(
            con,
            study_id=req.study_id,
            slot_a_report_id=slot_a_report,
            slot_b_report_id=slot_b_report,
            slot_a_condition=slot_a_cond,
            slot_b_condition=slot_b_cond,
            created_by=actor["id"],
            notes=(req.notes or "").strip(),
        )
    logger.info(
        "Created judge package id=%d study=%s a=%s b=%s",
        pid,
        req.study_id,
        slot_a_cond,
        slot_b_cond,
    )
    # Researchers DO see the slot→condition mapping; this is the
    # unblinding side. The judge route never returns these fields.
    return pdb.find_judge_package(con, pid)


@app.get("/api/admin/study/judge-packages")
def admin_list_judge_packages(
    study_id: str | None = None,
    actor: dict = Depends(auth.require_researcher),
):
    return {"packages": pdb.list_judge_packages(get_registry().platform_con(), study_id=study_id)}


@app.post("/api/admin/study/judge-assignments")
def admin_create_judge_assignment(
    req: JudgeAssignmentRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Assign a package to a judge. Same package can go to multiple
    judges for inter-rater reliability."""
    reg = get_registry()
    con = reg.platform_con()
    judge = pdb.find_actor_by_id(con, req.judge_actor_id)
    if judge is None or judge.get("kind") not in ("judge", "admin"):
        raise HTTPException(400, "judge_actor_id must reference a judge actor")
    if pdb.find_judge_package(con, req.package_id) is None:
        raise HTTPException(400, f"Package {req.package_id} not found")
    with reg.platform_lock:
        aid = pdb.create_judge_assignment(
            con,
            judge_actor_id=req.judge_actor_id,
            package_id=req.package_id,
            assigned_by=actor["id"],
        )
    return pdb.find_judge_assignment(con, aid)


# ─── Text judging: blinded absolute ratings of submitted texts ──────


@app.get("/api/admin/study/judges")
def admin_list_judges(actor: dict = Depends(auth.require_researcher)):
    """Judge accounts a researcher can assign work to. (The full user
    list is admin-only; researchers need just this slice.)"""
    return {"judges": pdb.list_judges(get_registry().platform_con())}


def _text_label(con, text: dict) -> dict:
    """The researcher's (unblinded) view of a submitted text."""
    session = pdb.find_study_session(con, text["session_id"]) or {}
    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    participant = (
        pdb.find_study_participant(con, token["participant_id"])
        if token.get("participant_id") is not None
        else None
    )
    return {
        "text_id": text["id"],
        "session_id": text["session_id"],
        "participant_code": participant["participant_code"] if participant else None,
        "period": token.get("period"),
        "condition": text["condition"],
        "topic_title": text["topic_title"],
        "word_count": text["word_count"],
        "submitted_at": text["submitted_at"],
    }


@app.get("/api/admin/study/{study_id}/texts")
def admin_list_study_texts(study_id: str, actor: dict = Depends(auth.require_researcher)):
    """Submitted texts in a study, with how far the panel has got on
    each. Metadata only — the researcher assigns and tracks; reading the
    texts is the panel's job (and the export's)."""
    con = get_registry().platform_con()
    assignments = pdb.list_text_assignments_for_study(con, study_id)
    out = []
    for text in pdb.list_study_texts(con, study_id=study_id):
        mine = [a for a in assignments if a["text_id"] == text["id"]]
        out.append(
            {
                **_text_label(con, text),
                "assigned": len(mine),
                "rated": sum(1 for a in mine if a["status"] == "completed"),
            }
        )
    judges = {j["id"]: j for j in pdb.list_judges(con)}
    progress = {}
    for a in assignments:
        row = progress.setdefault(
            a["judge_actor_id"],
            {
                "judge_actor_id": a["judge_actor_id"],
                "display_name": judges.get(a["judge_actor_id"], {}).get("display_name", "—"),
                "assigned": 0,
                "rated": 0,
            },
        )
        row["assigned"] += 1
        row["rated"] += a["status"] == "completed"
    return {"study_id": study_id, "texts": out, "judges": list(progress.values())}


@app.post("/api/admin/study/{study_id}/text-assignments")
def admin_assign_texts(
    study_id: str,
    req: TextAssignmentRequest,
    actor: dict = Depends(auth.require_researcher),
):
    """Assign texts to a judge — by default every submitted text in the
    study the judge doesn't already have, so the button can be pressed
    again as more sessions finish. Each assignment draws a random queue
    position: every judge meets the texts in their own order."""
    import random

    reg = get_registry()
    con = reg.platform_con()
    judge = pdb.find_actor_by_id(con, req.judge_actor_id)
    if judge is None or judge.get("kind") != "judge":
        raise HTTPException(404, "Judge not found")
    texts = {t["id"]: t for t in pdb.list_study_texts(con, study_id=study_id)}
    wanted = list(texts) if req.text_ids is None else req.text_ids
    unknown = [tid for tid in wanted if tid not in texts]
    if unknown:
        raise HTTPException(404, f"Not submitted texts of study '{study_id}': {unknown}")

    rng = random.SystemRandom()
    created = []
    with reg.platform_lock:
        for text_id in wanted:
            new_id = pdb.create_text_assignment(
                con,
                study_id=study_id,
                text_id=text_id,
                judge_actor_id=req.judge_actor_id,
                assigned_by=actor["id"],
                position=rng.random(),
            )
            if new_id is not None:
                created.append(new_id)
    logger.info(
        "Text assignments: study=%s judge=%d created=%d already_had=%d (by %d)",
        study_id,
        req.judge_actor_id,
        len(created),
        len(wanted) - len(created),
        actor["id"],
    )
    return {
        "created": len(created),
        "already_assigned": len(wanted) - len(created),
        "assignment_ids": created,
    }


@app.get("/api/judge/rubric")
def judge_rubric(actor: dict = Depends(auth.require_judge)):
    return text_judging.rubric()


@app.get("/api/judge/texts")
def judge_text_queue(actor: dict = Depends(auth.require_judge)):
    """The judge's texts, in that judge's own random order. Just enough
    to show a queue: nothing here says who wrote a text or how."""
    con = get_registry().platform_con()
    queue = []
    for a in pdb.list_text_assignments_for_judge(con, actor["id"]):
        text = pdb.find_study_text(con, a["text_id"]) or {}
        queue.append(
            {
                "assignment_id": a["id"],
                "status": a["status"],
                "topic_title": text.get("topic_title", ""),
                "word_count": text.get("word_count"),
            }
        )
    return {"assignments": queue}


def _judge_text_assignment(con, assignment_id: int, actor: dict) -> dict:
    assignment = pdb.find_text_assignment(con, assignment_id)
    if assignment is None:
        raise HTTPException(404, "Assignment not found")
    if assignment["judge_actor_id"] != actor["id"] and actor.get("kind") != "admin":
        raise HTTPException(403, "Not your assignment")
    return assignment


@app.get("/api/judge/texts/{assignment_id}")
def judge_view_text(assignment_id: int, actor: dict = Depends(auth.require_judge)):
    """One text to rate, **blinded**: the topic the writer was given,
    the text, the rubric, and the judge's own latest rating. Never the
    condition, the participant, the session, or even the text's id —
    ids are handed out in submission order, which a judge could read."""
    con = get_registry().platform_con()
    assignment = _judge_text_assignment(con, assignment_id, actor)
    text = pdb.find_study_text(con, assignment["text_id"])
    if text is None:
        raise HTTPException(404, "The text for this assignment is gone")
    session = pdb.find_study_session(con, text["session_id"]) or {}
    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    latest = pdb.latest_text_rating(con, assignment_id)
    return {
        "assignment_id": assignment_id,
        "status": assignment["status"],
        "topic_title": text["topic_title"],
        "topic_brief": token.get("topic_brief", ""),
        "content": text["content"],
        "word_count": text["word_count"],
        "rubric": text_judging.rubric(),
        "rating": (
            {k: latest[k] for k in ("ratings", "justification", "condition_guess", "confidence")}
            if latest
            else None
        ),
    }


@app.post("/api/judge/texts/{assignment_id}/rate")
def judge_rate_text(
    assignment_id: int,
    req: TextRatingRequest,
    actor: dict = Depends(auth.require_judge),
):
    """Submit (or revise) a rating. Validated strictly and rejected
    whole on any error, so every stored rating is complete. A revision
    is a new row; the newest counts and the earlier ones are kept."""
    try:
        ratings = text_judging.validate_ratings(req.ratings)
    except ValueError as e:
        raise HTTPException(400, {"user_message": str(e)}) from None
    if (
        req.condition_guess is not None
        and req.condition_guess not in text_judging.CONDITION_GUESSES
    ):
        raise HTTPException(
            400, "condition_guess must be 'elenchus', 'baseline', 'unsure' or null"
        )
    if req.confidence is not None and not 1 <= req.confidence <= 7:
        raise HTTPException(400, "confidence must be between 1 and 7")
    if req.seconds_spent is not None and req.seconds_spent < 0:
        raise HTTPException(400, "seconds_spent can't be negative")

    reg = get_registry()
    con = reg.platform_con()
    _judge_text_assignment(con, assignment_id, actor)
    with reg.platform_lock:
        rating_id = pdb.record_text_rating(
            con,
            assignment_id=assignment_id,
            rubric_version=text_judging.RUBRIC_VERSION,
            ratings=ratings,
            justification=(req.justification or "").strip(),
            condition_guess=req.condition_guess,
            confidence=req.confidence,
            seconds_spent=req.seconds_spent,
        )
    logger.info(
        "Text rated: assignment=%d judge=%d rating_id=%d rubric=v%s seconds_spent=%s guess=%s",
        assignment_id,
        actor["id"],
        rating_id,
        text_judging.RUBRIC_VERSION,
        req.seconds_spent,
        req.condition_guess,
    )
    return {"id": rating_id, "assignment_id": assignment_id, "status": "submitted"}


@app.get("/api/judge/queue")
def judge_queue(
    status: str | None = None,
    actor: dict = Depends(auth.require_judge),
):
    """The judge's queue. `status='pending'` filters to unrated
    packages; default returns everything."""
    return {
        "assignments": pdb.list_assignments_for_judge(
            get_registry().platform_con(),
            actor["id"],
            status=status,
        )
    }


@app.get("/api/judge/assignments/{assignment_id}")
def judge_view_assignment(
    assignment_id: int,
    actor: dict = Depends(auth.require_judge),
):
    """Return the package's two outputs under NEUTRAL slot labels.
    Condition labels are stripped — the judge sees only `slot_a` and
    `slot_b` with content and report_id. The judge's own assignment
    and any existing rating are included so the UI can render edit
    affordances."""
    con = get_registry().platform_con()
    assignment = pdb.find_judge_assignment(con, assignment_id)
    if assignment is None:
        raise HTTPException(404, "Assignment not found")
    if assignment["judge_actor_id"] != actor["id"] and actor.get("kind") != "admin":
        raise HTTPException(403, "Not your assignment")
    package = pdb.find_judge_package(con, assignment["package_id"])
    if package is None:
        raise HTTPException(404, "Package gone — researcher deleted it?")
    # Pull the two reports — content only. The judge route NEVER
    # surfaces condition. We also strip generator_model and cost
    # fields so a clever judge can't infer condition from those.
    rows = con.execute(
        "SELECT id, content FROM study_reports WHERE id IN (?, ?)",
        [package["slot_a_report_id"], package["slot_b_report_id"]],
    ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    rating = pdb.find_rating_for_assignment(con, assignment_id)
    return {
        "assignment_id": assignment_id,
        "status": assignment["status"],
        "slot_a": {
            "report_id": package["slot_a_report_id"],
            "content": by_id.get(package["slot_a_report_id"], ""),
        },
        "slot_b": {
            "report_id": package["slot_b_report_id"],
            "content": by_id.get(package["slot_b_report_id"], ""),
        },
        "rating": rating,
    }


@app.post("/api/judge/assignments/{assignment_id}/rate")
def judge_submit_rating(
    assignment_id: int,
    req: JudgeRatingRequest,
    actor: dict = Depends(auth.require_judge),
):
    """Submit a rating for an assignment. Validates fields, inserts
    one row, marks the assignment completed. A second submission is
    allowed (idempotent regeneration); only the newest row counts at
    analysis time."""
    if req.pairwise_winner not in ("a", "b", "tie"):
        raise HTTPException(400, "pairwise_winner must be 'a', 'b', or 'tie'")
    if req.condition_guess_a is not None and req.condition_guess_a not in (
        "elenchus",
        "baseline",
        "unsure",
    ):
        raise HTTPException(
            400, "condition_guess_a must be 'elenchus', 'baseline', 'unsure', or null"
        )
    if req.condition_guess_b is not None and req.condition_guess_b not in (
        "elenchus",
        "baseline",
        "unsure",
    ):
        raise HTTPException(
            400, "condition_guess_b must be 'elenchus', 'baseline', 'unsure', or null"
        )
    if req.confidence is not None and not (1 <= req.confidence <= 7):
        raise HTTPException(400, "confidence must be between 1 and 7")

    reg = get_registry()
    con = reg.platform_con()
    assignment = pdb.find_judge_assignment(con, assignment_id)
    if assignment is None:
        raise HTTPException(404, "Assignment not found")
    if assignment["judge_actor_id"] != actor["id"] and actor.get("kind") != "admin":
        raise HTTPException(403, "Not your assignment")

    with reg.platform_lock:
        rid = pdb.record_judge_rating(
            con,
            assignment_id=assignment_id,
            ratings=req.ratings,
            justification_a=req.justification_a or "",
            justification_b=req.justification_b or "",
            pairwise_winner=req.pairwise_winner,
            condition_guess_a=req.condition_guess_a,
            condition_guess_b=req.condition_guess_b,
            confidence=req.confidence,
        )
        pdb.mark_assignment_completed(con, assignment_id)
    return {"id": rid, "assignment_id": assignment_id, "status": "submitted"}


# ─── Phase D/5: structured report generation ──────────────────────


@app.post("/api/study/session/{session_id}/generate-report")
async def generate_session_report(
    session_id: int,
    actor: dict = Depends(auth.current_actor),
):
    """Generate the structured report for a study session and store
    it. Idempotent in the sense that a second call generates a
    second row (so a researcher can regenerate after tuning the
    template); `find_study_report_for_session` returns the newest.

    Authorization: the session's actor (the participant), an admin,
    or a researcher can trigger generation. Regular users can't
    touch other users' sessions because the lookup is keyed on
    session_id and we check ownership.
    """
    from . import study_reports as study_reports_mod

    reg = get_registry()
    con = reg.platform_con()
    session = pdb.find_study_session(con, session_id)
    if session is None:
        raise HTTPException(404, f"Session #{session_id} not found")
    # Authorization: own session, admin, or researcher.
    if actor["id"] != session["actor_id"] and actor.get("kind") not in (
        "admin",
        "researcher",
    ):
        raise HTTPException(403, "Not authorized to generate this session's report")
    if not session.get("base_id"):
        raise HTTPException(
            400,
            "Session has no base attached yet — generate-report requires "
            "completing the dialectic / chat first.",
        )
    if not session.get("condition"):
        raise HTTPException(
            400,
            "Session has no condition set — not a study session.",
        )

    # Load the per-base state.
    base_id = session["base_id"]
    try:
        reg.get(base_id)
    except FileNotFoundError as e:
        raise HTTPException(404, f"Base '{base_id}' file not found") from e
    except ValueError as e:
        raise HTTPException(422, f"Base '{base_id}' is corrupt: {e}") from e

    # Generate. LLMCallError propagates up; the existing message-route
    # handler converts it to the structured user-facing body for free
    # — same exception path. The handle is pinned across the LLM call
    # so the registry's sweep can't close the base under it.
    try:
        with reg.hold(base_id) as handle:
            result = await study_reports_mod.generate_report(
                handle.state,
                condition=session["condition"],
                opponent=opponent,
                session_id=session_id,
                actor_id=actor["id"],
                base_id=base_id,
            )
    except LLMCallError as e:
        status = _http_status_for_chat_category(e.result.category)
        raise HTTPException(
            status_code=status,
            detail={
                "category": e.result.category.value,
                "attempts": e.result.attempts,
                "user_message": _user_message_for_chat_category(e.result.category),
            },
        ) from e

    # Persist with pricing applied (matching the usage-table convention).
    from . import pricing

    cost = pricing.compute_cost(
        result["model"], result["prompt_tokens"], result["completion_tokens"]
    )
    with reg.platform_lock:
        rid = pdb.record_study_report(
            con,
            session_id=session_id,
            condition=session["condition"],
            content=result["content"],
            generator_model=result["model"],
            prompt_tokens=result["prompt_tokens"],
            completion_tokens=result["completion_tokens"],
            cost_usd=cost,
            metadata={"attempts": result["attempts"], "latency_ms": result["latency_ms"]},
        )
    logger.info(
        "Generated study report id=%d session=%d condition=%s tokens=%d cost=%.4f",
        rid,
        session_id,
        session["condition"],
        result["prompt_tokens"] + result["completion_tokens"],
        cost,
    )
    return {
        "id": rid,
        "session_id": session_id,
        "condition": session["condition"],
        "content": result["content"],
        "model": result["model"],
        "cost_usd": cost,
    }


@app.get("/api/study/session/{session_id}/report")
def fetch_session_report(
    session_id: int,
    actor: dict = Depends(auth.current_actor),
):
    """Retrieve the most recent stored report for a session.

    Authorization: own session, admin, or researcher. Judges go
    through a separate (blinded) interface — see Phase D/7."""
    reg = get_registry()
    con = reg.platform_con()
    session = pdb.find_study_session(con, session_id)
    if session is None:
        raise HTTPException(404, f"Session #{session_id} not found")
    if actor["id"] != session["actor_id"] and actor.get("kind") not in (
        "admin",
        "researcher",
    ):
        raise HTTPException(403, "Not authorized to view this session's report")
    report = pdb.find_study_report_for_session(con, session_id)
    if report is None:
        raise HTTPException(404, "No report generated for this session yet")
    return report


@app.get("/api/admin/study/reports")
def admin_list_reports(
    condition: str | None = None,
    actor: dict = Depends(auth.require_researcher),
):
    """Researcher cohort view — every report, newest first, optionally
    filtered by condition for cross-condition comparison."""
    return {"reports": pdb.list_study_reports(get_registry().platform_con(), condition=condition)}


# ─── Phase D/2: participant session state machine ──────────────────


class AdvanceSessionRequest(BaseModel):
    """Body for `POST /api/study/session/advance`."""

    to_state: str


@app.get("/api/study/session")
def study_session_current(actor: dict = Depends(auth.current_actor)):
    """Return the participant's currently-live session, or 404 if
    they don't have one. The frontend hits this on every page load
    to decide whether to show the briefing / tutorial / dialectic
    interface / post-session summary / questionnaire."""
    session = pdb.find_live_session_for_actor(get_registry().platform_con(), actor["id"])
    if session is None:
        raise HTTPException(404, "No active study session for this participant")
    return _study_session_payload(session)


# How long the main task is meant to take. Guidance, not a cutoff: the
# participant sees an elapsed timer and a soft warning as the time runs
# down and again when it is up, and ends the task themselves. Override
# A study can set its own length (`study_configs.task_minutes`, Study
# tab) — a TRAINING study at 5 minutes beside a PILOT at 60; this is the
# server-wide default for studies that don't (ELENCHUS_TASK_MINUTES).
def _task_minutes(study_id: str | None = None) -> int:
    if study_id:
        config = pdb.find_study_config(get_registry().platform_con(), study_id)
        if config and config.get("task_minutes"):
            return int(config["task_minutes"])
    try:
        return max(1, int(os.environ.get("ELENCHUS_TASK_MINUTES", "60")))
    except ValueError:
        return 60


PRACTICE_TOPIC_TITLE = "Practice: kinds of pets"
PRACTICE_TOPIC_BRIEF = (
    "A warm-up to try the interface. Write two or three sentences introducing the "
    "kinds of pets people keep and how you would group them. This text is not part "
    "of the study."
)


def _study_session_payload(session: dict) -> dict:
    """The participant-facing view of a study session: the lifecycle
    row plus what the working screen needs — the writing task, the
    timer, and whether the text is already in. Every route that hands
    the frontend a session goes through here, because the frontend
    replaces its session object with whatever a route returns."""
    con = get_registry().platform_con()
    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    task_minutes = _task_minutes(token.get("study_id"))
    payload = {
        **session,
        "topic_title": token.get("topic_title", ""),
        "topic_brief": token.get("topic_brief", ""),
        "practice_topic_title": PRACTICE_TOPIC_TITLE,
        "practice_topic_brief": PRACTICE_TOPIC_BRIEF,
        "task_minutes": task_minutes,
        "soft_warning_minutes": sorted({max(1, task_minutes - 10), task_minutes}),
        "state_elapsed_seconds": pdb.session_state_elapsed_seconds(con, session["id"]),
        "text_submitted": pdb.find_study_text_for_session(con, session["id"]) is not None,
    }
    # The token is the participant's credential; the page doesn't need it back.
    payload.pop("study_token", None)
    return payload


def _study_working_base(session: dict) -> str:
    """The base a participant's writing belongs to right now: the
    practice base during the tutorial, the task base during the task."""
    if session["state"] == "tutorial":
        return f"practice-{session['id']}"
    if session["state"] == "active" and session.get("base_id"):
        return session["base_id"]
    raise HTTPException(
        409,
        {
            "current_state": session["state"],
            "user_message": "There is no text to work on at this point in the session.",
        },
    )


def _live_study_session(actor: dict) -> dict:
    session = pdb.find_live_session_for_actor(get_registry().platform_con(), actor["id"])
    if session is None:
        raise HTTPException(404, "No active study session for this participant")
    return session


@app.get("/api/study/session/text")
async def study_text_get(actor: dict = Depends(auth.current_actor)):
    """The participant's latest saved draft for the base they are
    working in (empty if they haven't written anything yet)."""
    session = _live_study_session(actor)
    handle = get_registry().get_handle(_study_working_base(session))
    async with handle.lock:
        latest = study_text.latest_snapshot(handle.state.base.con)
    return {
        "content": latest["content"] if latest else "",
        "word_count": latest["word_count"] if latest else 0,
        "saved_at": latest["at_utc"] if latest else None,
    }


@app.put("/api/study/session/text")
async def study_text_save(req: StudyTextRequest, actor: dict = Depends(auth.current_actor)):
    """Autosave. Appends a snapshot to the working base's draft history
    (identical consecutive content is not re-stored).

    Async + the per-base lock so a save can never land inside an
    opponent turn's transaction on the same DuckDB connection — a
    rolled-back turn would otherwise take the snapshot with it."""
    if req.trigger not in ("autosave", "blur", "paste"):
        raise HTTPException(400, "trigger must be 'autosave', 'blur' or 'paste'")
    session = _live_study_session(actor)
    handle = get_registry().get_handle(_study_working_base(session))
    try:
        async with handle.lock:
            snapshot = study_text.save_snapshot(
                handle.state.base.con, req.content, trigger=req.trigger, actor_id=actor["id"]
            )
    except ValueError as e:
        raise HTTPException(413, {"user_message": str(e)}) from None
    return snapshot


@app.post("/api/study/session/text/events")
async def study_text_events(req: EditorEventsRequest, actor: dict = Depends(auth.current_actor)):
    """Editor events the snapshots can't show — a paste (length only),
    a soft timer warning being displayed."""
    session = _live_study_session(actor)
    handle = get_registry().get_handle(_study_working_base(session))
    async with handle.lock:
        stored = study_text.record_editor_events(
            handle.state.base.con, req.events, actor_id=actor["id"]
        )
    return {"stored": stored}


@app.post("/api/study/session/finish")
async def study_finish(req: StudyTextRequest, actor: dict = Depends(auth.current_actor)):
    """End the main task: submit the text and move `active →
    post_session`. The text is the study's judged artifact, so the task
    can't be finished without one."""
    reg = get_registry()
    con = reg.platform_con()
    session = _live_study_session(actor)
    if session["state"] != "active" or not session.get("base_id"):
        raise HTTPException(
            400,
            {
                "current_state": session["state"],
                "user_message": "The task can only be finished while it is in progress.",
            },
        )
    content = req.content.strip()
    if not content:
        raise HTTPException(
            400,
            {"user_message": "Your text is empty — please write your introduction first."},
        )

    handle = reg.get_handle(session["base_id"])
    try:
        async with handle.lock:
            snapshot = study_text.save_snapshot(
                handle.state.base.con, content, trigger="submit", actor_id=actor["id"]
            )
    except ValueError as e:
        raise HTTPException(413, {"user_message": str(e)}) from None

    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    elapsed = pdb.session_state_elapsed_seconds(con, session["id"])
    with reg.platform_lock:
        text_id = pdb.create_study_text(
            con,
            session_id=session["id"],
            actor_id=actor["id"],
            condition=session["condition"],
            topic_title=token.get("topic_title", ""),
            content=content,
            word_count=snapshot["word_count"],
            active_elapsed_seconds=elapsed,
        )
        updated = pdb.advance_session_state(con, session["id"], "post_session")
    if updated is None:
        raise HTTPException(409, "Session state changed concurrently — reload and retry")
    logger.info(
        "Study text submitted: session=%d condition=%s topic=%r words=%d elapsed_s=%s text_id=%s",
        session["id"],
        session["condition"],
        token.get("topic_title", ""),
        snapshot["word_count"],
        elapsed,
        text_id,
    )
    return _study_session_payload(updated)


@app.post("/api/study/session/advance")
def study_session_advance(
    req: AdvanceSessionRequest,
    actor: dict = Depends(auth.current_actor),
):
    """Move the participant forward in the state machine. Validates
    the requested transition against `study_flow.ALLOWED_TRANSITIONS`;
    rejects with 400 on an invalid move."""
    reg = get_registry()
    session = pdb.find_live_session_for_actor(reg.platform_con(), actor["id"])
    if session is None:
        raise HTTPException(404, "No active study session for this participant")
    # Leaving the task goes through `finish`, which takes the text with
    # it. Without this guard a client could skip to the questionnaires
    # and leave the session with nothing for the judges to rate.
    if (
        req.to_state == "post_session"
        and pdb.find_study_text_for_session(reg.platform_con(), session["id"]) is None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "current_state": session["state"],
                "requested_state": req.to_state,
                "user_message": "Please submit your text to finish the task.",
            },
        )

    with reg.platform_lock:
        updated = pdb.advance_session_state(reg.platform_con(), session["id"], req.to_state)
    if updated is None:
        # find_study_session would have failed already if the session
        # vanished; the only other reason is invalid transition.
        raise HTTPException(
            status_code=400,
            detail={
                "current_state": session["state"],
                "requested_state": req.to_state,
                "user_message": (
                    f"Can't move from '{session['state']}' to '{req.to_state}' — please try again."
                ),
            },
        )
    return _study_session_payload(updated)


def _create_study_base(reg, actor_id: int, base_name: str, topic: str) -> str:
    """Create + register a base for the study flow. Returns the base
    name. 409s if a base with that name already exists (e.g. a
    double-clicked button) — the caller treats that as already-done."""
    con = reg.platform_con()
    if pdb.find_base(con, base_name) is not None:
        return base_name  # idempotent — already created by an earlier click
    path = reg.db_path(base_name, actor_id=actor_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = DialecticalState.create(path, topic)
    reg.put(base_name, state)
    with reg.platform_lock:
        pdb.create_base(con, base_id=base_name, name=topic, owner_id=actor_id)
    return base_name


@app.post("/api/study/session/begin-tutorial")
def study_begin_tutorial(actor: dict = Depends(auth.current_actor)):
    """Briefing → tutorial. Creates the participant's practice base
    (`practice-{session_id}`) so the tutorial uses the real interface
    on a throwaway warm-up topic. The practice base is NOT attached
    to the session — only the task base is — but the baseline-routing
    predicate recognizes the naming convention so baseline-condition
    participants practice in their actual condition."""
    reg = get_registry()
    session = pdb.find_live_session_for_actor(reg.platform_con(), actor["id"])
    if session is None:
        raise HTTPException(404, "No active study session for this participant")
    if session["state"] != "briefing":
        raise HTTPException(
            400,
            {
                "current_state": session["state"],
                "user_message": "The tutorial can only start from the briefing screen.",
            },
        )
    base_name = _create_study_base(
        reg, actor["id"], f"practice-{session['id']}", PRACTICE_TOPIC_TITLE
    )
    with reg.platform_lock:
        updated = pdb.advance_session_state(reg.platform_con(), session["id"], "tutorial")
    if updated is None:
        raise HTTPException(409, "Session state changed concurrently — reload and retry")
    return {**_study_session_payload(updated), "practice_base_id": base_name}


@app.post("/api/study/session/begin-task")
def study_begin_task(actor: dict = Depends(auth.current_actor)):
    """Tutorial → active. Creates the task base (`task-{session_id}`),
    attaches it to the session (this is what the export and the
    baseline router key on), and advances the state."""
    reg = get_registry()
    con = reg.platform_con()
    session = pdb.find_live_session_for_actor(con, actor["id"])
    if session is None:
        raise HTTPException(404, "No active study session for this participant")
    if session["state"] != "tutorial":
        raise HTTPException(
            400,
            {
                "current_state": session["state"],
                "user_message": "The task can only start from the tutorial screen.",
            },
        )
    # The base is named after the participant's topic, which is how the
    # Elenchus opponent learns it ("Topic: ..." heads the state it is
    # shown) and what the baseline assistant is told.
    token = pdb.find_participant_token(con, session.get("study_token") or "") or {}
    base_name = _create_study_base(
        reg, actor["id"], f"task-{session['id']}", token.get("topic_title") or "Study task"
    )
    with reg.platform_lock:
        pdb.attach_base_to_session(con, session["id"], base_name)
        updated = pdb.advance_session_state(con, session["id"], "active")
    if updated is None:
        raise HTTPException(409, "Session state changed concurrently — reload and retry")
    return {**_study_session_payload(updated), "task_base_id": base_name}


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


def _settings_payload() -> dict:
    """Current LLM settings for the admin UI. Never includes the key value;
    `has_api_key` reports whether one is configured, `key_persisted` whether
    it survives a restart, and `persistence_available` whether a master key
    is set so the key *can* be persisted."""
    con = get_registry().platform_con()
    return {
        "model": opponent.model,
        "base_url": opponent.base_url or "",
        "protocol": opponent.protocol,
        "has_api_key": opponent._has_api_key,
        "key_persisted": pdb.get_setting(con, _S_API_KEY_ENC) is not None,
        "persistence_available": secretbox.is_available(),
    }


@app.get("/api/settings")
def get_settings(actor: dict = Depends(auth.require_admin)):
    """Return current LLM settings (admin only; never exposes the key)."""
    return _settings_payload()


@app.put("/api/settings")
def update_settings(req: SettingsUpdate, actor: dict = Depends(auth.require_admin)):
    """Update LLM settings at runtime (admin only). Reconfigures the live
    opponent and persists the change: non-secret values in plaintext, the
    API key Fernet-encrypted (when a master key is configured)."""
    opponent.reconfigure(
        model=req.model,
        api_key=req.api_key,
        base_url=req.base_url,
        protocol=req.protocol,
    )
    key_persisted = _persist_llm_settings(
        model=req.model,
        base_url=req.base_url,
        protocol=req.protocol,
        api_key=req.api_key,
    )
    logger.info(
        "Settings updated by admin id=%s: model=%s, base_url=%s, api_key_provided=%s, key_persisted=%s",
        actor["id"],
        req.model,
        req.base_url,
        bool(req.api_key),
        key_persisted,
    )
    return _settings_payload()


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
        # Open a working session over the new base so the session-keyed
        # API (`/api/sessions/{id}/...`) can address it. The base name
        # stays the internal/storage key; the session id is the public
        # handle.
        session_id = pdb.create_session(reg.platform_con(), actor_id=actor["id"], base_id=name)
    return {"name": name, "session_id": session_id, "state": state.to_dict()}


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
    _get_state(name)  # 404 / 422 for a missing or corrupt file

    # Sloan-condition routing: if the caller has an active study session
    # in the BASELINE condition bound to *this* base, dispatch to the
    # AI-as-tool free-form chat path. Everything else (regular users,
    # admins, and Elenchus-condition participants) uses the dialectic
    # opponent. The check is at message-time rather than at base-create
    # time because the session.condition is set when the token is
    # consumed, not when the base is created.
    is_baseline = _is_baseline_for_actor_and_base(actor["id"], name)

    # `hold` pins the handle for the whole turn: the state is used
    # after the LLM call, which runs without the per-base lock, and a
    # pinned handle is never closed by the registry's sweep.
    with get_registry().hold(name) as handle:
        return await _send_message_held(handle, name, req, actor, is_baseline)


async def _send_message_held(
    handle, name: str, req: MessageRequest, actor: dict, is_baseline: bool
) -> dict:
    try:
        if is_baseline:
            result = await opponent.async_baseline_respond(
                req.message,
                handle.state,
                lock=handle.lock,
                actor_id=actor["id"],
                base_id=name,
            )
        else:
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
            "condition": "baseline" if is_baseline else "elenchus",
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
    # Phase 1 of the two-phase UI flow mutates state without the LLM;
    # the event context is what marks the change as a button press in
    # the capture log (the follow-up message is a separate turn).
    event = EventContext(source="ui", actor_id=actor["id"])
    if req.action == "accept":
        result = state.accept_tension(tid, event=event)
        if not result:
            raise HTTPException(404, f"Tension #{tid} not found or not open")
        logger.info("Tension #%d accepted in '%s' → material implication", tid, name)
        return {"accepted": result, "state": state.to_dict()}
    elif req.action == "contest":
        if not state.contest_tension(tid, event=event):
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
    state.retract_prop(req.proposition, event=EventContext(source="ui", actor_id=actor["id"]))
    return {"retracted": req.proposition, "state": state.to_dict()}


@app.post("/api/dialectics/{name}/derive")
def derive(name: str, req: DeriveRequest, actor: dict = Depends(auth.current_actor)):
    """Check derivability in the material base."""
    state = _authorize_and_get_state(name, actor)
    try:
        result = state.derive_with_trace(req.gamma, req.delta)
    except QuerySyntaxError as e:
        # A malformed query sentence (dangling connective, unclosed
        # `<...>` quote, ...) is the caller's error, not a 500. Only
        # that: any other ValueError from in here — pyNMMS rejecting an
        # atom this server built, say — is a server fault and must
        # surface as a 5xx, not be reported as the user's mistake.
        logger.warning(
            "Derive rejected: dialectic=%s, gamma=%r, delta=%r: %s", name, req.gamma, req.delta, e
        )
        raise HTTPException(422, str(e)) from None
    logger.info(
        "Derive: dialectic=%s, gamma=%r, delta=%r → %s",
        name,
        req.gamma,
        req.delta,
        result.derivable,
    )
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
    summary = opponent.generate_summary(state, actor_id=actor["id"], base_id=name)
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


# ── Session-keyed API (primary) ──────────────────────────────────────
#
# These are the going-forward routes: a dialectic is addressed by a
# numeric session id rather than its human-readable name. Each route is
# a thin adapter — it resolves `session_id → base name` (with the same
# ownership/404 check) and delegates to the existing name-keyed handler,
# so there is exactly one copy of the message/tension/derive/etc. logic.
# The `/api/dialectics/{name}/...` routes are retained as a compatibility
# alias (the constrained study-participant flow and existing clients
# still use them). See docs/session-api-migration.md.


@app.post("/api/sessions")
def create_session_route(req: CreateRequest, actor: dict = Depends(auth.current_actor)):
    """Create a dialectic and open a session over it. Returns the same
    body as `POST /api/dialectics`, including `session_id`."""
    return create_dialectic(req, actor)


@app.get("/api/sessions")
def list_sessions_route(actor: dict = Depends(auth.current_actor)):
    """List the current actor's sessions (admins: every base), each with
    its base name + position counts. Bases that predate session-keying
    get a session opened lazily here, so existing dialectics surface with
    a stable id."""
    reg = get_registry()
    con = reg.platform_con()
    if actor.get("kind") == "admin":
        bases = [r["id"] for r in pdb.list_bases(con)]
    else:
        bases = [r["id"] for r in pdb.list_bases_for_actor(con, actor["id"])]

    existing = {
        s["base_id"]: s["id"]
        for s in pdb.list_sessions_for_actor(con, actor["id"], status=None)
        if s.get("base_id")
    }
    result = []
    for base_id in bases:
        sid = existing.get(base_id)
        if sid is None:
            with reg.platform_lock:
                sid = pdb.create_session(con, actor_id=actor["id"], base_id=base_id)
            existing[base_id] = sid
        try:
            d = _get_state(base_id).to_dict()
            counts = {
                "topic": d["name"],
                "commitments": len(d["commitments"]),
                "denials": len(d["denials"]),
                "tensions": len(d["tensions"]),
                "implications": len(d["implications"]),
            }
        except Exception:
            logger.debug("Failed to open dialectic '%s' for session list", base_id)
            counts = {
                "topic": base_id,
                "commitments": 0,
                "denials": 0,
                "tensions": 0,
                "implications": 0,
            }
        result.append({"session_id": sid, "name": base_id, **counts})
    return result


@app.get("/api/sessions/{session_id}")
def get_session_route(session_id: int, actor: dict = Depends(auth.current_actor)):
    return get_dialectic(_resolve_session_base(session_id, actor), actor)


@app.post("/api/sessions/{session_id}/message")
async def session_message_route(
    session_id: int, req: MessageRequest, actor: dict = Depends(auth.current_actor)
):
    return await send_message(_resolve_session_base(session_id, actor), req, actor)


@app.post("/api/sessions/{session_id}/tensions/{tid}")
def session_tension_route(
    session_id: int, tid: int, req: TensionAction, actor: dict = Depends(auth.current_actor)
):
    return resolve_tension(_resolve_session_base(session_id, actor), tid, req, actor)


@app.post("/api/sessions/{session_id}/retract")
def session_retract_route(
    session_id: int, req: RetractRequest, actor: dict = Depends(auth.current_actor)
):
    return retract(_resolve_session_base(session_id, actor), req, actor)


@app.post("/api/sessions/{session_id}/derive")
def session_derive_route(
    session_id: int, req: DeriveRequest, actor: dict = Depends(auth.current_actor)
):
    return derive(_resolve_session_base(session_id, actor), req, actor)


@app.get("/api/sessions/{session_id}/report")
def session_report_route(session_id: int, actor: dict = Depends(auth.current_actor)):
    return report(_resolve_session_base(session_id, actor), actor)


@app.get("/api/sessions/{session_id}/report.pdf")
def session_report_pdf_route(session_id: int, actor: dict = Depends(auth.current_actor)):
    return download_report_pdf(_resolve_session_base(session_id, actor), actor)


@app.delete("/api/sessions/{session_id}")
def session_delete_route(session_id: int, actor: dict = Depends(auth.current_actor)):
    base = _resolve_session_base(session_id, actor)
    reg = get_registry()
    with reg.platform_lock:
        pdb.close_session(reg.platform_con(), session_id)
    return delete_dialectic(base, actor)


# ── Health check ──


@app.get("/healthz")
def healthz(response: Response):
    """Unauthenticated liveness + readiness probe for external
    monitoring (uptime checks, load-balancer health, systemd
    watchdog). Returns 200 with `{status: "ok", ...}` when the
    platform DB is reachable and migrated; 503 with
    `{status: "degraded", ...}` otherwise.

    Deliberately cheap: a single `SELECT 1`-class read against the
    already-open platform connection. No per-base files are touched
    (a corrupt base must not flap the health check), and no auth is
    required (monitors don't carry credentials)."""
    checks: dict[str, str] = {}
    healthy = True

    # Platform DB reachable + schema version readable.
    try:
        con = get_registry().platform_con()
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        schema_version = int(row[0]) if row and row[0] else 0
        checks["platform_db"] = "ok"
    except Exception as e:
        logger.warning("Health check: platform DB probe failed: %s", e)
        checks["platform_db"] = f"error: {e}"
        schema_version = None
        healthy = False

    # Data directory writable (backups, exports, new bases all need it).
    try:
        if os.access(DATA_DIR, os.W_OK):
            checks["data_dir"] = "ok"
        else:
            checks["data_dir"] = "not writable"
            healthy = False
    except Exception as e:
        checks["data_dir"] = f"error: {e}"
        healthy = False

    body = {
        "status": "ok" if healthy else "degraded",
        # The *running* code's version. After a deploy, this is how to
        # tell the service really restarted onto the new release (a pip
        # upgrade alone leaves the old process serving the old code).
        "version": elenchus_version,
        "schema_version": schema_version,
        "phase_b_enabled": opponent.enable_phase_b,
        "llm_configured": opponent._has_api_key,
        # Whether the server can actually send mail. The sign-in page
        # uses it to say so, instead of promising a reset link or a
        # login link that will never arrive.
        "email_enabled": email_service_mod.active_backend() == "smtp",
        "checks": checks,
    }
    if not healthy:
        response.status_code = 503
    return body


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


def _run_costs(args) -> None:
    """Print the cost report (`costs.build_report`) — the same figures
    as the admin dashboard, for a grant report or a post-run record."""
    import json

    reg = get_registry()
    reg.migrate_platform()
    report = costs_mod.build_report(reg.platform_con(), days=args.days)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(costs_mod.format_report(report))


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


def _run_sim(args) -> None:
    """Run the agent-driven pilot simulation and print the report.
    Exits non-zero if any problems were found, so CI fails on a broken
    flow."""
    import sys

    from .sim import run_simulation
    from .sim.report import render_text

    report = run_simulation(
        driver_mode=args.driver,
        participants=args.participants,
        judges=args.judges,
        study_id=args.study_id,
    )
    print(render_text(report, show_timeline=not args.quiet))
    sys.exit(0 if report.ok else 1)


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

    # `costs` subcommand — the cost dashboard's report, for the record.
    costs_p = subparsers.add_parser(
        "costs",
        help="Print LLM spend priced from recorded tokens (stop the server first: "
        "DuckDB allows one process per file)",
    )
    costs_p.add_argument(
        "--days", type=int, default=30, help="Window for the breakdowns; 0 = all time"
    )
    costs_p.add_argument("--json", action="store_true", help="Emit the full report as JSON")

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

    # `sim` subcommand — agent-driven pilot simulation.
    sim = subparsers.add_parser(
        "sim",
        help="Run an agent-driven pilot-study simulation (platform robustness check)",
    )
    sim.add_argument(
        "--driver",
        choices=["scripted", "llm"],
        default="scripted",
        help="Persona engine: 'scripted' (free, deterministic, CI) or "
        "'llm' (real LLM personas; needs an API key)",
    )
    sim.add_argument("--participants", type=int, default=4, help="Number of participants")
    sim.add_argument("--judges", type=int, default=2, help="Number of judges")
    sim.add_argument("--study-id", default="SIM", help="Study identifier")
    sim.add_argument("--quiet", action="store_true", help="Suppress the step-by-step timeline")

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
    elif args.command == "costs":
        _run_costs(args)
    elif args.command == "migrate-legacy":
        _run_migrate_legacy(args)
    elif args.command == "sim":
        _run_sim(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
