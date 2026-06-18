"""Symmetric encryption for at-rest secrets (the LLM API key).

The key is derived from the ``ELENCHUS_SECRET_KEY`` environment variable —
a deploy-time *master key*, set once in the server's env file. We derive a
Fernet key (authenticated AES) from it via SHA-256, so the operator can use
any sufficiently long random string (e.g. ``openssl rand -base64 36``)
rather than a precisely-formatted Fernet key.

Security boundary: this is encryption *at rest*. The ciphertext is what
lands in ``platform.duckdb`` and in every ``EXPORT DATABASE`` backup, so a
leaked DB file or backup is useless without the master key. The master key
lives in the env file on the same host, so an attacker with full host
access can read both and decrypt — no app-managed secret on a single VM can
do better. The production upgrade is a cloud KMS/HSM holding the master key.

When ``ELENCHUS_SECRET_KEY`` is unset, encryption is unavailable: the server
still runs and the API key can still be set at runtime, but it is held in
memory only and will not survive a restart.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "ELENCHUS_SECRET_KEY"


def _fernet() -> Fernet | None:
    """Build a Fernet from the master key, or None if it isn't set.

    Re-read on every call so tests (and runtime rotation) see env changes.
    """
    secret = os.environ.get(_ENV_VAR)
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_available() -> bool:
    """True when a master key is configured (so secrets can be persisted)."""
    return _fernet() is not None


def encrypt(plaintext: str) -> str:
    """Encrypt a secret to a urlsafe token. Raises if no master key is set."""
    f = _fernet()
    if f is None:
        raise RuntimeError(f"{_ENV_VAR} is not set; cannot encrypt secrets")
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt a token, or None if the master key is missing/changed or the
    token is corrupt — callers treat None as 'no usable persisted secret'."""
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None
