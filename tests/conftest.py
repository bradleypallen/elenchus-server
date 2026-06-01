"""Pytest configuration shared across the test suite.

Sets `BCRYPT_ROUNDS=4` before any test imports `elenchus.auth`, so
password-hashing tests run in milliseconds rather than seconds. The
production default (cost 12) is preserved when this env var is unset.
This must happen at import time (before `elenchus.auth` reads the
env var into `_BCRYPT_ROUNDS`), so we do it at module top level.
"""

import os

os.environ.setdefault("BCRYPT_ROUNDS", "4")
