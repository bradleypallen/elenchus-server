"""
server.py — Elenchus web server

FastAPI app that:
- Serves a static HTML/JS frontend
- Manages dialectical states in DuckDB files
- Proxies LLM oracle calls through the Anthropic SDK
- Supports creating, listing, resuming, and exporting dialectics

Run: uvicorn server:app --reload
Or:  python server.py
"""

import glob
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dialectical_state import DialecticalState
from opponent import Opponent
from pdf_report import generate_pdf_report

logger = logging.getLogger(__name__)

# ── Config ──

DATA_DIR = os.environ.get("ELENCHUS_DATA", "./dialectics")
os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI(title="Elenchus", version="0.1.0")
opponent = Opponent(
    model=os.environ.get("ELENCHUS_MODEL", "claude-sonnet-4-20250514"),
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL"),
)

# Cache open states
_states: dict[str, DialecticalState] = {}


def _db_path(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return os.path.join(DATA_DIR, f"{safe}.duckdb")


def _get_state(name: str) -> DialecticalState:
    if name not in _states:
        path = _db_path(name)
        if os.path.exists(path):
            _states[name] = DialecticalState.open(path)
        else:
            raise HTTPException(404, f"Dialectic '{name}' not found")
    return _states[name]


# ── API Models ──


class CreateRequest(BaseModel):
    name: str
    topic: str | None = None


class MessageRequest(BaseModel):
    message: str


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


# ── API Routes ──


@app.get("/api/settings")
def get_settings():
    """Return current LLM settings (never exposes the API key value)."""
    return {
        "model": opponent.model,
        "base_url": opponent.base_url or "",
        "has_api_key": opponent._has_api_key,
    }


@app.put("/api/settings")
def update_settings(req: SettingsUpdate):
    """Update LLM settings at runtime."""
    opponent.reconfigure(
        model=req.model,
        api_key=req.api_key,
        base_url=req.base_url,
    )
    logger.info(
        "Settings updated via API: model=%s, base_url=%s, api_key_provided=%s",
        req.model,
        req.base_url,
        bool(req.api_key),
    )
    return get_settings()


@app.post("/api/dialectics")
def create_dialectic(req: CreateRequest):
    """Create a new dialectic."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    path = _db_path(name)
    if os.path.exists(path):
        raise HTTPException(409, f"Dialectic '{name}' already exists")
    topic = req.topic or name
    state = DialecticalState.create(path, topic)
    _states[name] = state
    return {"name": name, "state": state.to_dict()}


@app.get("/api/dialectics")
def list_dialectics():
    """List all saved dialectics."""
    files = glob.glob(os.path.join(DATA_DIR, "*.duckdb"))
    result = []
    for f in sorted(files):
        basename = Path(f).stem
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
def get_dialectic(name: str):
    """Get the current state of a dialectic, including conversation history."""
    state = _get_state(name)
    result = state.to_dict()
    result["conversation"] = state.get_conversation()
    return result


@app.post("/api/dialectics/{name}/message")
def send_message(name: str, req: MessageRequest):
    """
    Send a natural language message from the respondent.
    The opponent parses it, updates state, proposes tensions,
    and responds.
    """
    state = _get_state(name)
    try:
        result = opponent.respond(req.message, state)
        return {
            "response": result.get("response", ""),
            "speech_acts": result.get("speech_acts", []),
            "new_tensions": result.get("new_tensions", []),
            "state": state.to_dict(),
        }
    except Exception as e:
        raise HTTPException(500, f"Opponent error: {str(e)}") from e


@app.post("/api/dialectics/{name}/tensions/{tid}")
def resolve_tension(name: str, tid: int, req: TensionAction):
    """Accept or contest a tension directly (bypassing the oracle)."""
    state = _get_state(name)
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
def retract(name: str, req: RetractRequest):
    """Retract a proposition directly."""
    state = _get_state(name)
    logger.info("Retract: dialectic=%s, proposition=%r", name, req.proposition)
    state.retract_prop(req.proposition)
    return {"retracted": req.proposition, "state": state.to_dict()}


@app.post("/api/dialectics/{name}/derive")
def derive(name: str, req: DeriveRequest):
    """Check derivability in the material base."""
    state = _get_state(name)
    result = state.derives(req.gamma, req.delta)
    return {"gamma": req.gamma, "delta": req.delta, "derives": result}


@app.get("/api/dialectics/{name}/report")
def report(name: str):
    """Get the material base report."""
    state = _get_state(name)
    return {"report": state.base.report()}


@app.get("/api/dialectics/{name}/report.pdf")
def download_report_pdf(name: str):
    """Generate and download a PDF report of the dialectic."""
    state = _get_state(name)
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
def delete_dialectic(name: str):
    """Delete a dialectic."""
    if name in _states:
        _states[name].base.con.close()
        del _states[name]
    path = _db_path(name)
    if os.path.exists(path):
        os.remove(path)
        return {"deleted": name}
    raise HTTPException(404, f"Dialectic '{name}' not found")


# ── Static files ──

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Elenchus</h1><p>Place index.html in ./static/</p>")


# ── Entry point ──

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    print(f"Elenchus server starting on http://localhost:{port}")
    print(f"Data directory: {os.path.abspath(DATA_DIR)}")
    uvicorn.run(app, host="0.0.0.0", port=port)
