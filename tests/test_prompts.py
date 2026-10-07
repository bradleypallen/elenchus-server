"""The opponent's prompts as versioned files (src/elenchus/prompts/).

The pinned labels and hashes are the point: a prompt edit fails here
until the file's `version`, these pins and docs/prompts.md are updated
together, so a prompt never changes by accident — and the study's
registration can cite the label and hash of the text it froze.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from elenchus import opponent, prompts
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import _settle_prompt_override, app

# Family → (version label, sha256 of the text as shipped). Update these,
# the file header and docs/prompts.md together when a prompt changes.
PINNED = {
    "elenchus": (
        "elenchus/2026-10-07",
        "f17ed75472408458357a65ad2a4d3263090717507f9e0f84d4e43386c2ac5758",
    ),
    "baseline": (
        "baseline/2026-10-07",
        "10ca0bbfe36a495f19d113af68caa8129311f3d450386f9c4dd3870137787bac",
    ),
    "phase_b": (
        "phase_b/2026-06-10",
        "85b67c96f7a8638b1cec8aa32b3052e7afc06f509a0d77b06dd5e3ee88f8bf9c",
    ),
}


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv(prompts.ENV_DIR, raising=False)
    prompts._override_refused = False
    prompts.clear_cache()
    yield
    prompts._override_refused = False
    prompts.clear_cache()


class TestThePinnedPrompts:
    @pytest.mark.parametrize("family", prompts.FAMILIES)
    def test_label_and_hash_are_as_pinned(self, family):
        p = prompts.packaged(family)
        version, sha = PINNED[family]
        assert p.version == version, (
            f"{family}: the file says {p.version!r}; if the prompt changed, update the "
            "pin here and docs/prompts.md"
        )
        assert p.sha256 == sha and hashlib.sha256(p.text.encode()).hexdigest() == sha
        assert p.date and p.changed and not p.overridden

    def test_the_constants_are_the_shipped_text(self):
        assert prompts.packaged("elenchus").text == opponent.SLOAN_SYSTEM_PROMPT
        assert prompts.packaged("baseline").text == opponent.BASELINE_SYSTEM_PROMPT
        assert prompts.packaged("phase_b").text == opponent.PHASE_B_SYSTEM_PROMPT

    def test_the_text_is_what_the_opponent_sends(self):
        opp = opponent.Opponent(api_key="test-key", model="test-model")
        assert opp._system_prompt() == prompts.load("elenchus").text
        name, sha, version = opp._prompt_identity("elenchus")
        assert (name, version) == ("sloan", "elenchus/2026-10-07")
        assert sha == PINNED["elenchus"][1]
        name, sha, version = opp._prompt_identity(
            "baseline", opponent.baseline_system_prompt("Tides")
        )
        assert (name, version) == ("baseline", "baseline/2026-10-07")
        assert sha != PINNED["baseline"][1]  # the hash is of the text sent, topic included

    def test_versions_block(self):
        v = prompts.versions()
        assert set(v) == set(prompts.FAMILIES)
        assert v["elenchus"] == {
            "version": "elenchus/2026-10-07",
            "sha256": PINNED["elenchus"][1],
            "overridden": False,
        }


class TestTheFileFormat:
    def test_parse(self):
        p = prompts.parse(
            "---\nfamily: elenchus\nversion: elenchus/2099-01-01\ndate: 2099-01-01\n"
            "changed: a test\n---\nHello.\n",
            family="elenchus",
        )
        assert p.text == "Hello." and p.version == "elenchus/2099-01-01"

    @pytest.mark.parametrize(
        "raw",
        [
            "Hello.",
            "---\nfamily: elenchus\n---\nHello.",
            "---\nfamily: baseline\nversion: baseline/x\ndate: d\nchanged: c\n---\nHello.\n",
            "---\nfamily: elenchus\nversion: other/x\ndate: d\nchanged: c\n---\nHello.\n",
        ],
    )
    def test_bad_files_are_refused(self, raw):
        with pytest.raises(ValueError):
            prompts.parse(raw, family="elenchus")


def _override_dir(tmp_path: Path, *, elenchus_text: str = "You are a test opponent.") -> Path:
    d = tmp_path / "prompts"
    d.mkdir()
    for family in prompts.FAMILIES:
        src = prompts.packaged(family)
        text = elenchus_text if family == "elenchus" else src.text
        (d / f"{family}.md").write_text(
            f"---\nfamily: {family}\nversion: {family}/2099-01-01\ndate: 2099-01-01\n"
            f"changed: a tuning candidate\n---\n{text}\n"
        )
    return d


class TestTheOverride:
    def test_a_development_instance_uses_the_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv(prompts.ENV_DIR, str(_override_dir(tmp_path)))
        prompts.clear_cache()
        p = prompts.load("elenchus")
        assert p.text == "You are a test opponent." and p.version == "elenchus/2099-01-01"
        assert p.overridden and prompts.override_active()
        # The shipped text is untouched, and the opponent sends the override.
        assert prompts.packaged("elenchus").sha256 == PINNED["elenchus"][1]
        opp = opponent.Opponent(api_key="test-key", model="test-model")
        assert opp._system_prompt() == "You are a test opponent."
        assert opp._prompt_identity("elenchus")[1:] == (
            hashlib.sha256(b"You are a test opponent.").hexdigest(),
            "elenchus/2099-01-01",
        )

    def test_a_registered_study_refuses_it(self, tmp_path, monkeypatch):
        reg = get_registry()
        reg.migrate_platform()
        con = reg.platform_con()
        with reg.platform_lock:
            con.execute("DELETE FROM study_configs")
            con.execute(
                "INSERT INTO study_configs (study_id, topic_a_title, topic_b_title, created_by, "
                "allocation_seed) VALUES ('REG', 'A', 'B', 1, 'a secret seed')"
            )
        try:
            monkeypatch.setenv(prompts.ENV_DIR, str(_override_dir(tmp_path)))
            prompts.clear_cache()
            _settle_prompt_override()
            assert not prompts.override_active()
            assert prompts.load("elenchus").text == prompts.packaged("elenchus").text
            assert not prompts.load("elenchus").overridden
        finally:
            with reg.platform_lock, contextlib.suppress(Exception):
                con.execute("DELETE FROM study_configs WHERE study_id = 'REG'")

    def test_the_system_tab_reports_the_versions(self):
        reg = get_registry()
        reg.migrate_platform()
        con = reg.platform_con()
        from elenchus import auth

        with reg.platform_lock:
            con.execute("DELETE FROM auth_sessions")
            con.execute("DELETE FROM actors")
        admin_id = pdb.create_actor(
            con, kind="admin", email="a@example.com", display_name="A", password_hash="h"
        )
        client = TestClient(app)
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin_id))
        body = client.get("/api/admin/system").json()["prompts"]
        assert body["override_active"] is False
        assert body["versions"]["elenchus"]["version"] == "elenchus/2026-10-07"
