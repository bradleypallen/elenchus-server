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

from . import auth, invites
from .db import get_registry, init_registry
from .db import platform as pdb
from .dialectical_state import DialecticalState
from .opponent import Opponent
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
opponent = Opponent(
    model=os.environ.get("ELENCHUS_MODEL", "claude-opus-4-6"),
    api_key=os.environ.get("ELENCHUS_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
    base_url=os.environ.get("ELENCHUS_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
    protocol=os.environ.get("ELENCHUS_PROTOCOL"),
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
    path uses the sanitized name as the base id — a future migration
    moves to `bases/{actor_id}/{base_id}.duckdb`.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    reg = get_registry()
    path = reg.db_path(name)
    if os.path.exists(path):
        raise HTTPException(409, f"Dialectic '{name}' already exists")

    # Sanity-check that no `bases` row exists either (covers the case
    # where the file was deleted out-of-band but the row remained).
    if pdb.find_base(reg.platform_con(), name) is not None:
        raise HTTPException(409, f"Dialectic '{name}' is already registered")

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
        # Walk the data directory (covers legacy files without
        # `bases` rows in addition to registered ones).
        files = glob.glob(os.path.join(DATA_DIR, "*.duckdb"))
        basenames = [Path(f).stem for f in sorted(files)]
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
        )
        return {
            "response": result.get("response", ""),
            "speech_acts": result.get("speech_acts", []),
            "new_tensions": result.get("new_tensions", []),
            "state": handle.state.to_dict(),
        }
    except Exception as e:
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
    sw_path = os.path.join(static_dir, "sw.js")
    return FileResponse(sw_path, media_type="application/javascript")


@app.get("/")
def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
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

    # `migrate-legacy` subcommand placeholder — implementation lands in
    # Week 3 D3 when per-base files are restructured.
    # subparsers.add_parser("migrate-legacy", ...) — TODO Week 3

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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
