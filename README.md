# Elenchus

A standalone system for dialectical knowledge base construction, implementing the Elenchus protocol (Allen 2026) with a DuckDB material base backend.

The respondent develops a bilateral position [C : D] through natural language dialogue with an LLM opponent. Accepted tensions become material implications in a NMMS material base satisfying Containment.

## Requirements

- Python 3.10+
- An Anthropic API key

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Web interface

```bash
python server.py
```

Open `http://localhost:8000`. The web interface provides:

- Creating and resuming dialectics
- Natural language dialogue with the LLM opponent
- Live bilateral state display [C : D]
- Tension resolution (accept/contest)
- Material implications accumulating in I
- Derivability queries against the material base

### Command line

```bash
# Interactive session (in-memory)
python elenchus_cli.py --name "My Inquiry"

# Persistent session (saved to DuckDB file)
python elenchus_cli.py --db my_inquiry.duckdb --name "My Inquiry"

# Resume a saved session
python elenchus_cli.py --db my_inquiry.duckdb
```

### API

```bash
# Create a dialectic
curl -X POST http://localhost:8000/api/dialectics \
  -H "Content-Type: application/json" \
  -d '{"name": "prov-o", "topic": "PROV-O Starting Point Terms"}'

# Send a message
curl -X POST http://localhost:8000/api/dialectics/prov-o/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Entity is a thing with fixed aspects."}'

# Get state
curl http://localhost:8000/api/dialectics/prov-o

# Accept a tension
curl -X POST http://localhost:8000/api/dialectics/prov-o/tensions/1 \
  -H "Content-Type: application/json" \
  -d '{"action": "accept"}'

# Check derivability
curl -X POST http://localhost:8000/api/dialectics/prov-o/derive \
  -H "Content-Type: application/json" \
  -d '{"gamma": ["entity_fixed_aspects"], "delta": ["individuation"]}'

# List all dialectics
curl http://localhost:8000/api/dialectics
```

## Architecture

```
respondent ──→ server.py ──→ opponent.py ──→ Anthropic API
    ↑              ↓
    └── static/    ↓
        index.html dialectical_state.py
                       ↓
                   material_base.py
                       ↓
                   dialectics/*.duckdb
```

- **server.py**: FastAPI app serving the API and static frontend
- **opponent.py**: LLM oracle — sends state to Anthropic, parses structured responses, applies state transitions
- **dialectical_state.py**: Definition 4 — S = ⟨[C : D], T, I⟩ backed by DuckDB
- **material_base.py**: Definition 5 — B = ⟨L_B, |∼_B⟩ with Projection-based derivability
- **dialectics/*.duckdb**: Persistent state files (one per dialectic)

## Persistence

Each dialectic is a single `.duckdb` file in the `dialectics/` directory. The file contains:

- The atomic language L_B
- All assessments (the base consequence relation |∼_B)
- The bilateral position [C : D]
- Open and resolved tensions
- Conversation history (for multi-turn oracle context)

To back up a dialectic, copy the `.duckdb` file. To share one, send the file. To resume, just point the server at the directory containing it.

## Configuration

Environment variables:

- `ANTHROPIC_API_KEY`: Required. Your Anthropic API key.
- `ELENCHUS_MODEL`: LLM model for the oracle (default: `claude-sonnet-4-20250514`)
- `ELENCHUS_DATA`: Directory for `.duckdb` files (default: `./dialectics`)
- `PORT`: Server port (default: `8000`)
