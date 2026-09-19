"""Elenchus — Dialectical knowledge base construction via the Elenchus protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The installed distribution's version — single source of truth is
    # pyproject.toml, so this can't drift from what was actually deployed.
    __version__ = version("elenchus")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"
