"""Tests for platform.duckdb schema and query helpers.

Exercises `db/platform.py` against a fresh in-memory DuckDB with the
platform migrations applied. Identity (actors), auth (sessions, magic
links), invites, bases, sessions, and settings each get a focused test.
"""

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from elenchus.db import platform as pdb
from elenchus.migrations import apply_migrations


@pytest.fixture
def con():
    """Fresh in-memory connection with platform schema applied."""
    c = duckdb.connect(":memory:")
    apply_migrations(c, "platform")
    yield c
    c.close()


# ─── Migration ────────────────────────────────────────────────────────


class TestPlatformMigration:
    def test_creates_expected_tables(self, con):
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        for required in {
            "meta",
            "actors",
            "auth_sessions",
            "magic_links",
            "invites",
            "bases",
            "sessions",
            "platform_settings",
        }:
            assert required in tables, f"platform migration did not create {required}"

    def test_seeds_signup_mode_default(self, con):
        assert pdb.get_setting(con, "signup_mode") == "invite_only"


# ─── Actors ───────────────────────────────────────────────────────────


class TestActors:
    def test_create_and_find_by_id(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="a@example.com",
            display_name="Alice",
            password_hash="hashed",
        )
        actor = pdb.find_actor_by_id(con, actor_id)
        assert actor is not None
        assert actor["email"] == "a@example.com"
        assert actor["display_name"] == "Alice"
        assert actor["kind"] == "user"

    def test_find_by_email(self, con):
        pdb.create_actor(
            con, kind="admin", email="b@example.com", display_name="Bob", password_hash="h"
        )
        actor = pdb.find_actor_by_email(con, "b@example.com")
        assert actor is not None
        assert actor["kind"] == "admin"

    def test_email_uniqueness(self, con):
        pdb.create_actor(
            con,
            kind="user",
            email="dup@example.com",
            display_name="Dup1",
            password_hash="h",
        )
        with pytest.raises(duckdb.ConstraintException):
            pdb.create_actor(
                con,
                kind="user",
                email="dup@example.com",
                display_name="Dup2",
                password_hash="h",
            )

    def test_actor_exists_returns_false_for_unknown(self, con):
        assert pdb.actor_exists(con, 9999) is False

    def test_actor_exists_true_after_create(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="e@example.com",
            display_name="E",
            password_hash="h",
        )
        assert pdb.actor_exists(con, actor_id) is True

    def test_deactivate_actor(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="d@example.com",
            display_name="D",
            password_hash="h",
        )
        pdb.deactivate_actor(con, actor_id)
        # actor_exists checks deactivated_at IS NULL.
        assert pdb.actor_exists(con, actor_id) is False

    def test_update_password(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="p@example.com",
            display_name="P",
            password_hash="old",
        )
        pdb.update_actor_password(con, actor_id, "new")
        actor = pdb.find_actor_by_id(con, actor_id)
        assert actor["password_hash"] == "new"

    def test_invalid_kind_rejected(self, con):
        with pytest.raises(duckdb.ConstraintException):
            pdb.create_actor(
                con,
                kind="bogus",
                email="x@example.com",
                display_name="X",
                password_hash="h",
            )

    def test_list_actors_excludes_deactivated_by_default(self, con):
        a = pdb.create_actor(
            con,
            kind="user",
            email="active@example.com",
            display_name="A",
            password_hash="h",
        )
        b = pdb.create_actor(
            con,
            kind="user",
            email="off@example.com",
            display_name="B",
            password_hash="h",
        )
        pdb.deactivate_actor(con, b)
        ids = {row["id"] for row in pdb.list_actors(con)}
        assert a in ids
        assert b not in ids
        ids_with_deact = {row["id"] for row in pdb.list_actors(con, include_deactivated=True)}
        assert b in ids_with_deact


# ─── Auth sessions ────────────────────────────────────────────────────


class TestAuthSessions:
    def test_create_and_resolve_token(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="s@example.com",
            display_name="S",
            password_hash="h",
        )
        pdb.create_auth_session(con, token="tok1", actor_id=actor_id)
        actor = pdb.resolve_auth_token(con, "tok1")
        assert actor is not None
        assert actor["id"] == actor_id

    def test_revoked_token_does_not_resolve(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="r@example.com",
            display_name="R",
            password_hash="h",
        )
        pdb.create_auth_session(con, token="tok2", actor_id=actor_id)
        pdb.revoke_auth_session(con, "tok2")
        assert pdb.resolve_auth_token(con, "tok2") is None

    def test_expired_token_does_not_resolve(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="x@example.com",
            display_name="X",
            password_hash="h",
        )
        pdb.create_auth_session(con, token="tok3", actor_id=actor_id, ttl=timedelta(seconds=-1))
        assert pdb.resolve_auth_token(con, "tok3") is None

    def test_deactivated_actor_token_does_not_resolve(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="da@example.com",
            display_name="DA",
            password_hash="h",
        )
        pdb.create_auth_session(con, token="tok4", actor_id=actor_id)
        pdb.deactivate_actor(con, actor_id)
        assert pdb.resolve_auth_token(con, "tok4") is None

    def test_revoke_all_actor_sessions(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="m@example.com",
            display_name="M",
            password_hash="h",
        )
        pdb.create_auth_session(con, token="m1", actor_id=actor_id)
        pdb.create_auth_session(con, token="m2", actor_id=actor_id)
        pdb.revoke_actor_sessions(con, actor_id)
        assert pdb.resolve_auth_token(con, "m1") is None
        assert pdb.resolve_auth_token(con, "m2") is None


# ─── Magic links ──────────────────────────────────────────────────────


class TestMagicLinks:
    def test_consume_returns_email_on_first_call(self, con):
        pdb.create_magic_link(con, token="mlk1", email="ml@example.com")
        assert pdb.consume_magic_link(con, "mlk1") == "ml@example.com"

    def test_consume_returns_none_on_replay(self, con):
        pdb.create_magic_link(con, token="mlk2", email="ml@example.com")
        pdb.consume_magic_link(con, "mlk2")
        assert pdb.consume_magic_link(con, "mlk2") is None

    def test_expired_link_not_consumable(self, con):
        pdb.create_magic_link(con, token="mlk3", email="ml@example.com", ttl=timedelta(seconds=-1))
        assert pdb.consume_magic_link(con, "mlk3") is None


# ─── Invites ──────────────────────────────────────────────────────────


class TestInvites:
    def _admin(self, con):
        return pdb.create_actor(
            con,
            kind="admin",
            email="admin@example.com",
            display_name="Admin",
            password_hash="h",
        )

    def test_create_and_find(self, con):
        admin_id = self._admin(con)
        pdb.create_invite(con, token="inv1", role="user", issued_by=admin_id)
        invite = pdb.find_invite(con, "inv1")
        assert invite is not None
        assert invite["role"] == "user"

    def test_consume_returns_invite_first_time(self, con):
        admin_id = self._admin(con)
        new_user = pdb.create_actor(
            con,
            kind="user",
            email="new@example.com",
            display_name="New",
            password_hash="h",
        )
        pdb.create_invite(con, token="inv2", role="user", issued_by=admin_id)
        invite = pdb.consume_invite(con, "inv2", consumed_by=new_user)
        assert invite is not None
        assert invite["role"] == "user"

    def test_consume_twice_returns_none(self, con):
        admin_id = self._admin(con)
        u = pdb.create_actor(
            con,
            kind="user",
            email="u@example.com",
            display_name="U",
            password_hash="h",
        )
        pdb.create_invite(con, token="inv3", role="user", issued_by=admin_id)
        pdb.consume_invite(con, "inv3", consumed_by=u)
        assert pdb.consume_invite(con, "inv3", consumed_by=u) is None

    def test_revoke_unconsumed(self, con):
        admin_id = self._admin(con)
        pdb.create_invite(con, token="inv4", role="user", issued_by=admin_id)
        assert pdb.revoke_invite(con, "inv4") is True
        # After revocation, consumed invite path returns None
        u = pdb.create_actor(
            con,
            kind="user",
            email="ru@example.com",
            display_name="RU",
            password_hash="h",
        )
        assert pdb.consume_invite(con, "inv4", consumed_by=u) is None

    def test_expired_invite_not_consumable(self, con):
        admin_id = self._admin(con)
        past = datetime.now(UTC) - timedelta(days=1)
        pdb.create_invite(con, token="inv5", role="user", issued_by=admin_id, expires_at=past)
        u = pdb.create_actor(
            con,
            kind="user",
            email="ex@example.com",
            display_name="EX",
            password_hash="h",
        )
        assert pdb.consume_invite(con, "inv5", consumed_by=u) is None

    def test_invalid_role_rejected(self, con):
        admin_id = self._admin(con)
        with pytest.raises(duckdb.ConstraintException):
            pdb.create_invite(con, token="bad", role="superuser", issued_by=admin_id)


# ─── Bases and sessions ───────────────────────────────────────────────


class TestBases:
    def test_create_find_list(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="bs@example.com",
            display_name="BS",
            password_hash="h",
        )
        pdb.create_base(con, base_id="b1", name="Base One", owner_id=actor_id)
        found = pdb.find_base(con, "b1")
        assert found is not None
        assert found["owner_id"] == actor_id
        listed = pdb.list_bases_for_actor(con, actor_id)
        assert len(listed) == 1
        assert listed[0]["id"] == "b1"

    def test_owner_name_uniqueness(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="bn@example.com",
            display_name="BN",
            password_hash="h",
        )
        pdb.create_base(con, base_id="b1", name="dup", owner_id=actor_id)
        with pytest.raises(duckdb.ConstraintException):
            pdb.create_base(con, base_id="b2", name="dup", owner_id=actor_id)


class TestSessions:
    def test_open_and_find(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="sx@example.com",
            display_name="SX",
            password_hash="h",
        )
        pdb.create_base(con, base_id="b1", name="B", owner_id=actor_id)
        session_id = pdb.create_session(con, actor_id=actor_id, base_id="b1")
        found = pdb.find_session(con, session_id)
        assert found is not None
        assert found["actor_id"] == actor_id
        assert found["status"] == "open"

    def test_close(self, con):
        actor_id = pdb.create_actor(
            con,
            kind="user",
            email="sy@example.com",
            display_name="SY",
            password_hash="h",
        )
        pdb.create_base(con, base_id="b1", name="B", owner_id=actor_id)
        session_id = pdb.create_session(con, actor_id=actor_id, base_id="b1")
        pdb.close_session(con, session_id)
        s = pdb.find_session(con, session_id)
        assert s["status"] == "closed"
        assert s["closed_at"] is not None

    def test_list_open_for_actor(self, con):
        a1 = pdb.create_actor(
            con,
            kind="user",
            email="l1@example.com",
            display_name="L1",
            password_hash="h",
        )
        a2 = pdb.create_actor(
            con,
            kind="user",
            email="l2@example.com",
            display_name="L2",
            password_hash="h",
        )
        pdb.create_base(con, base_id="b1", name="B", owner_id=a1)
        pdb.create_session(con, actor_id=a1, base_id="b1")
        pdb.create_session(con, actor_id=a2, base_id="b1")
        actor_sessions = pdb.list_sessions_for_actor(con, a1)
        assert len(actor_sessions) == 1
        assert actor_sessions[0]["actor_id"] == a1


# ─── Settings ─────────────────────────────────────────────────────────


class TestSettings:
    def test_get_unset_returns_none(self, con):
        assert pdb.get_setting(con, "nonexistent") is None

    def test_set_and_get(self, con):
        pdb.set_setting(con, "favorite_color", "blue")
        assert pdb.get_setting(con, "favorite_color") == "blue"

    def test_set_overwrites(self, con):
        pdb.set_setting(con, "k", "v1")
        pdb.set_setting(con, "k", "v2")
        assert pdb.get_setting(con, "k") == "v2"

    def test_signup_mode_can_be_changed(self, con):
        pdb.set_setting(con, "signup_mode", "open")
        assert pdb.get_setting(con, "signup_mode") == "open"
