"""Pytest configuration shared across the test suite.

- Sets `BCRYPT_ROUNDS=4` before any test imports `elenchus.auth`, so
  password-hashing tests run in milliseconds rather than seconds.
- Creates a single shared temp data dir and sets `ELENCHUS_DATA` to
  it before any test file imports `elenchus.server`. The registry's
  `data_dir` is captured at server-import time; sharing one dir
  across all test files ensures per-base files written by one test
  are visible to (and cleanable by) every other test.
- Provides shared SMTP / API-key defaults so route tests don't
  require real credentials.

All of these must happen at module top level (before any
`elenchus.*` import), so they sit here rather than in fixtures.
"""

import os
import tempfile

os.environ.setdefault("BCRYPT_ROUNDS", "4")

_SHARED_TEST_DATA_DIR = tempfile.mkdtemp(prefix="elenchus_pytest_")
os.environ.setdefault("ELENCHUS_DATA", _SHARED_TEST_DATA_DIR)
os.environ.setdefault("ELENCHUS_API_KEY", "test-key-for-ci")
