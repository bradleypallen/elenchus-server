#!/usr/bin/env python3
"""
backup.py — invoke the Elenchus admin backup endpoint over HTTP.

This script is intended for cron. It does NOT open DuckDB files
directly — DuckDB is single-writer per file, so a parallel process
opening the same `.duckdb` would conflict with a running server.
Instead, the script POSTs to `/api/admin/backup`, which runs the
backup *inside the server process* using `EXPORT DATABASE` under the
existing locks.

# Cron setup

The script needs admin credentials to authenticate. Two options:

    # Option A — email/password from env (preferred for cron):
    ELENCHUS_BACKUP_EMAIL=admin@local
    ELENCHUS_BACKUP_PASSWORD=...
    ELENCHUS_URL=http://localhost:8741   # optional, defaults shown

    # Option B — pass a pre-issued session cookie:
    ELENCHUS_BACKUP_COOKIE='elenchus_session=abc123...'

Example crontab entry running at 03:00 daily:

    0 3 * * * ELENCHUS_BACKUP_EMAIL=admin@local ELENCHUS_BACKUP_PASSWORD=secret \\
              /usr/local/bin/python3 /opt/elenchus/scripts/backup.py \\
              >> /var/log/elenchus-backup.log 2>&1

The server's lifespan-startup must already have run before the cron
fires; if the server isn't up, the script logs an error and exits
non-zero so cron / monitoring picks it up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _post(url: str, body: dict, cookie: str | None) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _login(base_url: str, email: str, password: str) -> str:
    """Authenticate against /api/auth/login and return the Cookie header
    value that subsequent requests should send."""
    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Parse Set-Cookie. We want the elenchus_session cookie value.
        for header in resp.getheaders():
            if header[0].lower() == "set-cookie" and header[1].startswith("elenchus_session="):
                return header[1].split(";", 1)[0]
    raise RuntimeError("Login succeeded but no elenchus_session cookie was returned")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url",
        default=os.environ.get("ELENCHUS_URL", "http://localhost:8741"),
        help="Server base URL (default: $ELENCHUS_URL or http://localhost:8741)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("ELENCHUS_BACKUP_EMAIL"),
        help="Admin email (or $ELENCHUS_BACKUP_EMAIL)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ELENCHUS_BACKUP_PASSWORD"),
        help="Admin password (or $ELENCHUS_BACKUP_PASSWORD)",
    )
    parser.add_argument(
        "--cookie",
        default=os.environ.get("ELENCHUS_BACKUP_COOKIE"),
        help="Pre-issued session cookie (or $ELENCHUS_BACKUP_COOKIE)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="Retain only the N newest archives (default: server-side default of 14)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override backup output directory (defaults to {data_dir}/backups)",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    cookie = args.cookie
    if cookie is None:
        if not args.email or not args.password:
            print(
                "error: provide --cookie or --email + --password "
                "(or the ELENCHUS_BACKUP_* env vars)",
                file=sys.stderr,
            )
            return 2
        try:
            cookie = _login(base_url, args.email, args.password)
        except urllib.error.HTTPError as e:
            print(f"login failed: HTTP {e.code} {e.reason}", file=sys.stderr)
            return 2

    body: dict = {}
    if args.keep is not None:
        body["keep"] = args.keep
    if args.output_dir:
        body["output_dir"] = args.output_dir

    try:
        result = _post(f"{base_url}/api/admin/backup", body, cookie)
    except urllib.error.HTTPError as e:
        print(f"backup request failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"backup request failed: {e.reason}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
