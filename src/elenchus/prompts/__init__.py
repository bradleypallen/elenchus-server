"""
prompts — the opponent's system prompts as versioned files.

Each family (`elenchus`, `baseline`, `phase_b`) is one file in this
package: a short `---` header — `family`, a `version` label such as
`elenchus/2026-06-10`, the `date`, and a line on what `changed` — then
the prompt text exactly as the model receives it. Every turn in the
capture log records the family, the version label and the SHA-256 of
the text sent, so an analysis can group turns by a name a person
recognises and a reviewer can check the text against the hash
(`docs/prompts.md` keeps the history).

`tests/test_prompts.py` pins each family's label and hash: an edit to a
prompt fails the suite until the label, the hash and the history are
updated together, so a prompt never changes by accident.

**Override for development instances:** `ELENCHUS_PROMPT_DIR` names a
directory holding files of the same names; it is read instead of the
package and the hash of what is actually sent is still what gets
recorded. The server refuses the override — logs it and ignores it —
when any study on the instance carries an allocation seed, because that
is a registered study whose prompt is frozen (`refuse_override`).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FAMILIES = ("elenchus", "baseline", "phase_b")
ENV_DIR = "ELENCHUS_PROMPT_DIR"

_PACKAGE_DIR = Path(__file__).resolve().parent
_override_refused = False
_cache: dict[tuple[str, str], Prompt] = {}


@dataclass(frozen=True)
class Prompt:
    family: str
    version: str
    date: str
    changed: str
    text: str
    sha256: str
    source: str  # the file it came from

    @property
    def overridden(self) -> bool:
        return not self.source.startswith(str(_PACKAGE_DIR))


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse(raw: str, *, family: str, source: str = "") -> Prompt:
    """A prompt file: `---`, header lines `key: value`, `---`, the text.
    The text is everything after the closing line, less one trailing
    newline (the file's own)."""
    if not raw.startswith("---\n"):
        raise ValueError(f"prompt file for {family!r} has no header")
    end = raw.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"prompt file for {family!r} has an unterminated header")
    header: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if ":" not in line:
            raise ValueError(f"prompt file for {family!r}: bad header line {line!r}")
        key, value = line.split(":", 1)
        header[key.strip()] = value.strip()
    text = raw[end + len("\n---\n") :]
    if text.endswith("\n"):
        text = text[:-1]
    for key in ("family", "version", "date", "changed"):
        if not header.get(key):
            raise ValueError(f"prompt file for {family!r}: header lacks {key!r}")
    if header["family"] != family:
        raise ValueError(f"prompt file for {family!r} says family {header['family']!r}")
    if not header["version"].startswith(family + "/"):
        raise ValueError(
            f"prompt {family!r}: version {header['version']!r} must start with {family!r}/"
        )
    return Prompt(
        family=family,
        version=header["version"],
        date=header["date"],
        changed=header["changed"],
        text=text,
        sha256=fingerprint(text),
        source=source,
    )


def override_dir() -> Path | None:
    """The directory `ELENCHUS_PROMPT_DIR` names, unless the override was
    refused or the variable is unset or empty."""
    if _override_refused:
        return None
    value = os.environ.get(ENV_DIR, "").strip()
    return Path(value) if value else None


def prompt_dir() -> Path:
    return override_dir() or _PACKAGE_DIR


def override_active() -> bool:
    return override_dir() is not None


def refuse_override(reason: str) -> None:
    """Ignore `ELENCHUS_PROMPT_DIR` for the rest of this process."""
    global _override_refused
    if override_dir() is not None:
        logger.error("Ignoring %s=%s: %s", ENV_DIR, os.environ.get(ENV_DIR), reason)
    _override_refused = True
    clear_cache()


def clear_cache() -> None:
    _cache.clear()


def load(family: str) -> Prompt:
    """The prompt for `family`, from the override directory if one is in
    force, else the package. Cached per source file."""
    if family not in FAMILIES:
        raise ValueError(f"unknown prompt family {family!r}")
    path = prompt_dir() / f"{family}.md"
    key = (family, str(path))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    prompt = parse(path.read_text(encoding="utf-8"), family=family, source=str(path))
    _cache[key] = prompt
    return prompt


def packaged(family: str) -> Prompt:
    """The prompt as shipped in the package, override or not."""
    path = _PACKAGE_DIR / f"{family}.md"
    return parse(path.read_text(encoding="utf-8"), family=family, source=str(path))


def all_prompts() -> dict[str, Prompt]:
    return {family: load(family) for family in FAMILIES}


def versions() -> dict[str, dict]:
    """`{family: {version, sha256, overridden}}` — what the export manifest
    and the System tab show."""
    return {
        family: {"version": p.version, "sha256": p.sha256, "overridden": p.overridden}
        for family, p in all_prompts().items()
    }
