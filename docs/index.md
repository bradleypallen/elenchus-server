# Elenchus

A standalone system for dialectical knowledge base construction, implementing the Elenchus protocol (Allen 2026).

The respondent develops a bilateral position [C : D] through natural language dialogue with an LLM opponent. Accepted tensions become material implications in a NMMS material base satisfying Containment.

## Get Started

```bash
pip install elenchus
export ELENCHUS_API_KEY=sk-ant-...
elenchus
```

Then open `http://localhost:8741` in your browser.

## Documentation

- [User Guide](guide.md) — installation, concepts, web interface, CLI, configuration, and a worked example
- [Architecture](architecture.md) — internal design, module descriptions, DuckDB schema, and API protocol

## Links

- [GitHub Repository](https://github.com/bradleypallen/elenchus-server)
- [PyPI Package](https://pypi.org/project/elenchus/)
