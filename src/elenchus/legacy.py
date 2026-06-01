"""
legacy.py — migrate single-user dialectic files into the Phase A
multi-user platform layout.

A legacy install kept dialectic .duckdb files under `{DATA_DIR}/*.duckdb`,
unowned and inaccessible to the auth layer. This module walks that
directory, migrates each file to the current schema, registers it in
`platform.bases` under a designated admin actor, and moves it to its
scoped path under `{DATA_DIR}/bases/{admin_id}/{name}.duckdb`. The
process is idempotent: a re-run silently skips bases that are already
relocated and registered.

Exposed as the `elenchus migrate-legacy` subcommand.
"""

from __future__ import annotations

import logging
import os
import shutil

from . import auth
from .db import get_registry
from .db import platform as pdb
from .material_base import MaterialBase

logger = logging.getLogger(__name__)

# Default admin lookup email. Override on the CLI with --admin-email.
DEFAULT_ADMIN_EMAIL = "admin@local"


def _find_or_create_admin(con, email: str, *, create: bool, password: str | None) -> dict:
    """Return the admin actor for the migration, creating one if
    `create=True` and the email isn't found.

    Raises ValueError if the email maps to a non-admin or if it's missing
    and creation is disabled.
    """
    existing = pdb.find_actor_by_email(con, email)
    if existing is not None:
        if existing["kind"] != "admin":
            raise ValueError(
                f"Actor {email!r} exists but is kind={existing['kind']!r}, "
                f"not admin. Refusing to assign legacy bases to a non-admin."
            )
        return existing

    if not create:
        raise ValueError(
            f"No admin actor with email {email!r}. Either create one first via "
            f"`elenchus admin create --email {email} --name Admin`, or re-run "
            f"with --create-admin to have migrate-legacy create one for you."
        )

    # Create with no password set (passwordless until the operator
    # changes it via `admin create --password`). Magic-link login still
    # works because magic links check email, not password.
    password_hash = auth.hash_password(password) if password else None
    actor_id = pdb.create_actor(
        con,
        kind="admin",
        email=email,
        display_name="Legacy Admin",
        password_hash=password_hash,
    )
    logger.info("Created admin actor id=%d (%s)", actor_id, email)
    refreshed = pdb.find_actor_by_id(con, actor_id)
    assert refreshed is not None  # we just created it
    return refreshed


def _list_legacy_files(data_dir: str) -> list[str]:
    """Return absolute paths of every flat-layout .duckdb file under
    `data_dir`. Skips `platform.duckdb` (the platform DB itself) and
    files already inside `bases/` (these are already scoped)."""
    out: list[str] = []
    if not os.path.isdir(data_dir):
        return out
    for entry in sorted(os.listdir(data_dir)):
        if not entry.endswith(".duckdb"):
            continue
        if entry == "platform.duckdb":
            continue
        full = os.path.join(data_dir, entry)
        if os.path.isfile(full):
            out.append(full)
    return out


def _migrate_one(path: str, admin: dict, data_dir: str) -> dict:
    """Migrate a single legacy file. Returns a status dict with
    {action: 'moved' | 'already_migrated' | 'registered_only', ...}."""
    name = os.path.splitext(os.path.basename(path))[0]
    admin_id = admin["id"]
    reg = get_registry()
    con = reg.platform_con()

    # 1. Bring the file up to the current per-base schema. MaterialBase.open
    # applies migrations idempotently and validates the file is a real
    # Elenchus dialectic; bad files raise ValueError that propagates up.
    mb = MaterialBase.open(path)

    # 2. Rewrite the default contributor_id (sentinel 1 from v2 migration)
    # to the real admin id. No-op if admin happens to be id=1.
    if admin_id != 1:
        mb.con.execute(
            "UPDATE atoms SET contributor_id = ? WHERE contributor_id = 1",
            [admin_id],
        )
        mb.con.execute(
            "UPDATE assessments SET contributor_id = ? WHERE contributor_id = 1",
            [admin_id],
        )
        mb.con.execute(
            "UPDATE positions SET actor_id = ? WHERE actor_id = 1",
            [admin_id],
        )
    mb.con.close()

    # 3. Register the base in platform.bases. Skip if already registered.
    existing = pdb.find_base(con, name)
    if existing is None:
        with reg.platform_lock:
            pdb.create_base(con, base_id=name, name=name, owner_id=admin_id)
        logger.info("Registered base %r under admin id=%d", name, admin_id)
    elif existing["owner_id"] != admin_id:
        logger.warning(
            "Base %r is already registered under owner_id=%d; not reassigning",
            name,
            existing["owner_id"],
        )

    # 4. Move the file under bases/{admin_id}/{name}.duckdb. The owner of
    # record is whoever the platform_db says owns the base; if a prior
    # run registered it under a different actor, honor that.
    owning_actor = existing["owner_id"] if existing else admin_id
    scoped_dir = os.path.join(data_dir, "bases", str(owning_actor))
    os.makedirs(scoped_dir, exist_ok=True)
    scoped_path = os.path.join(scoped_dir, f"{name}.duckdb")

    if os.path.abspath(path) == os.path.abspath(scoped_path):
        return {"name": name, "action": "already_migrated", "path": scoped_path}

    if os.path.exists(scoped_path):
        # Both the legacy file and the scoped file exist. The scoped file
        # is canonical (we wrote bases row pointing there); the legacy
        # one is a leftover. Leave both in place and warn — the operator
        # will resolve manually.
        logger.warning(
            "Both legacy (%s) and scoped (%s) files exist for base %r; "
            "skipping move. Resolve manually.",
            path,
            scoped_path,
            name,
        )
        return {"name": name, "action": "conflict", "path": scoped_path}

    shutil.move(path, scoped_path)
    logger.info("Moved %s → %s", path, scoped_path)
    return {"name": name, "action": "moved", "path": scoped_path}


def migrate_legacy(
    data_dir: str,
    *,
    admin_email: str = DEFAULT_ADMIN_EMAIL,
    create_admin: bool = False,
    admin_password: str | None = None,
) -> dict:
    """Walk `data_dir` for legacy .duckdb files and migrate each.

    Returns a summary dict: {admin_id, migrated: [...], skipped: [...]}.
    Errors on individual files are logged and recorded in
    `summary["errors"]`; the run does not abort halfway through, so a
    single broken file does not block the rest.
    """
    reg = get_registry()
    # Ensure the platform schema is current before we read or write actors.
    reg.migrate_platform()
    con = reg.platform_con()

    admin = _find_or_create_admin(con, admin_email, create=create_admin, password=admin_password)

    files = _list_legacy_files(data_dir)
    logger.info("Found %d legacy file(s) under %s", len(files), data_dir)

    migrated: list[dict] = []
    errors: list[dict] = []
    for path in files:
        try:
            result = _migrate_one(path, admin, data_dir)
            migrated.append(result)
        except Exception as e:
            logger.exception("Migration failed for %s", path)
            errors.append({"path": path, "error": str(e)})

    return {
        "admin_id": admin["id"],
        "admin_email": admin["email"],
        "migrated": migrated,
        "errors": errors,
    }
