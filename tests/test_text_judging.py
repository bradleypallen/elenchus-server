"""Tests for blinded, absolute rating of the participants' texts: the
versioned rubric, assignment (idempotent, randomly ordered per judge),
the judge's blinded view, strict rating validation, revisions, and the
access rules around all of it.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from elenchus import auth, text_judging
from elenchus.db import get_registry
from elenchus.db import platform as pdb
from elenchus.server import app

client = TestClient(app)

CONFIG = {
    "topic_a_title": "Occurrence and its relatives in Darwin Core",
    "topic_a_brief": "Occurrence, Organism, Event, MaterialSample.",
    "topic_b_title": "Taxon names and taxon concepts",
    "topic_b_brief": "Name, concept, usage, circumscription.",
    "min_gap_hours": 0,
}
GOOD = {"coverage": 5, "correctness": 6, "concision": 4, "reasoning": 5}


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "text_ratings",
            "text_assignments",
            "study_participants",
            "study_configs",
            "study_texts",
            "participant_session_tokens",
            "usage",
            "auth_sessions",
            "sessions",
            "bases",
            "actors",
        ):
            con.execute(f"DELETE FROM {table}")
    for _name, handle in list(reg._handles.items()):
        with contextlib.suppress(Exception):
            handle.state.base.con.close()
    reg._handles.clear()
    client.cookies.clear()
    yield
    client.cookies.clear()


def _actor(kind: str, label: str) -> tuple[int, TestClient]:
    con = get_registry().platform_con()
    actor_id = pdb.create_actor(
        con,
        kind=kind,
        email=f"{label}@example.com",
        display_name=label,
        password_hash=auth.hash_password("pw"),
    )
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(actor_id))
    return actor_id, c


def _study_with_texts(n_participants: int = 2) -> tuple[TestClient, list[dict]]:
    """A researcher, a configured study, and `n` participants who have
    each done both sessions — so 2n submitted texts."""
    _, researcher = _actor("researcher", "researcher")
    assert researcher.put("/api/admin/study/PILOT/config", json=CONFIG).status_code == 200
    for i in range(n_participants):
        person = researcher.post(
            "/api/admin/study/PILOT/participants", json={"display_name": f"Real Name {i}"}
        ).json()
        for planned in person["sessions"]:
            p = TestClient(app)
            assert p.post(f"/api/study/{planned['token']}").status_code == 200
            p.post("/api/study/session/begin-tutorial")
            p.post("/api/study/session/begin-task")
            text = f"An introduction to {planned['topic_title']} written under {i}."
            assert p.post("/api/study/session/finish", json={"content": text}).status_code == 200
            for to_state in ("surveyed", "complete"):
                p.post("/api/study/session/advance", json={"to_state": to_state})
    texts = researcher.get("/api/admin/study/PILOT/texts").json()["texts"]
    return researcher, texts


# ── Rubric ───────────────────────────────────────────────────────────


class TestRubric:
    def test_four_dimensions_from_the_solicitation(self):
        assert text_judging.DIMENSION_KEYS == ("coverage", "correctness", "concision", "reasoning")
        rubric = text_judging.rubric()
        assert rubric["version"] == text_judging.RUBRIC_VERSION
        assert (rubric["scale"]["min"], rubric["scale"]["max"]) == (1, 7)
        assert all(d["help"] for d in rubric["dimensions"])

    def test_judge_facing_wording_does_not_name_the_conditions(self):
        """The guess options describe a way of working; the study's
        internal condition names stay internal."""
        shown = json.dumps(
            [text_judging.DIMENSIONS, list(text_judging.CONDITION_GUESS_LABELS.values())]
        ).lower()
        assert "elenchus" not in shown and "baseline" not in shown

    def test_validate_accepts_a_complete_rating(self):
        assert text_judging.validate_ratings(dict(GOOD)) == GOOD

    @pytest.mark.parametrize(
        ("bad", "message"),
        [
            ({k: v for k, v in GOOD.items() if k != "reasoning"}, "missing: reasoning"),
            ({**GOOD, "fidelity": 3}, "Unknown rating dimension"),
            ({**GOOD, "coverage": 0}, "from 1 to 7"),
            ({**GOOD, "coverage": 8}, "from 1 to 7"),
            ({**GOOD, "coverage": 4.5}, "whole number"),
            ({**GOOD, "coverage": "5"}, "whole number"),
            ({**GOOD, "coverage": True}, "whole number"),
            ([5, 6, 4, 5], "must be an object"),
        ],
    )
    def test_validate_rejects(self, bad, message):
        with pytest.raises(ValueError, match=message):
            text_judging.validate_ratings(bad)


# ── Researcher side ──────────────────────────────────────────────────


class TestAssign:
    def test_texts_listing_is_the_unblinded_researcher_view(self):
        _, texts = _study_with_texts(1)
        assert len(texts) == 2
        assert {t["condition"] for t in texts} == {"elenchus", "baseline"}
        assert {t["participant_code"] for t in texts} == {"P01"}
        assert sorted(t["period"] for t in texts) == [1, 2]
        assert all(t["assigned"] == 0 and t["rated"] == 0 for t in texts)
        assert all("content" not in t for t in texts)

    def test_assign_all_is_idempotent_and_picks_up_new_texts(self):
        researcher, texts = _study_with_texts(1)
        judge_id, _ = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        )
        assert r.json()["created"] == 2
        again = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        ).json()
        assert (again["created"], again["already_assigned"]) == (0, 2)

        listing = researcher.get("/api/admin/study/PILOT/texts").json()
        assert all(t["assigned"] == 1 for t in listing["texts"])
        assert listing["judges"] == [
            {"judge_actor_id": judge_id, "display_name": "judge1", "assigned": 2, "rated": 0}
        ]

    def test_assign_a_subset(self):
        researcher, texts = _study_with_texts(1)
        judge_id, _ = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments",
            json={"judge_actor_id": judge_id, "text_ids": [texts[0]["text_id"]]},
        )
        assert r.json()["created"] == 1

    def test_rejects_non_judges_and_foreign_texts(self):
        researcher, texts = _study_with_texts(1)
        user_id, _ = _actor("user", "someone")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": user_id}
        )
        assert r.status_code == 404
        judge_id, _ = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments",
            json={"judge_actor_id": judge_id, "text_ids": [999999]},
        )
        assert r.status_code == 404

    def test_researcher_can_list_judges(self):
        """The Judging tab used the admin-only user list for this, which
        a researcher can't read."""
        researcher, _ = _study_with_texts(1)
        judge_id, _ = _actor("judge", "judge1")
        judges = researcher.get("/api/admin/study/judges").json()["judges"]
        assert judges == [
            {"id": judge_id, "display_name": "judge1", "email": "judge1@example.com"}
        ]

    def test_each_judge_gets_their_own_order(self):
        researcher, _ = _study_with_texts(4)  # 8 texts
        orders = []
        for n in range(4):
            judge_id, jc = _actor("judge", f"judge{n}")
            researcher.post(
                "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
            )
            con = get_registry().platform_con()
            orders.append(
                tuple(a["text_id"] for a in pdb.list_text_assignments_for_judge(con, judge_id))
            )
        assert all(sorted(o) == sorted(orders[0]) and len(o) == 8 for o in orders)
        assert len(set(orders)) > 1  # 4 judges sharing one of 8! orders: ~1e-14

    def test_access(self):
        researcher, _ = _study_with_texts(1)
        judge_id, jc = _actor("judge", "judge1")
        _, user = _actor("user", "someone")
        for c in (jc, user):
            assert c.get("/api/admin/study/PILOT/texts").status_code == 403
            assert c.get("/api/admin/study/judges").status_code == 403
            assert (
                c.post(
                    "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
                ).status_code
                == 403
            )
        assert TestClient(app).get("/api/judge/texts").status_code == 401


# ── Judge side ───────────────────────────────────────────────────────


def _assigned(n_participants: int = 1):
    researcher, texts = _study_with_texts(n_participants)
    judge_id, jc = _actor("judge", "judge1")
    researcher.post("/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id})
    queue = jc.get("/api/judge/texts").json()["assignments"]
    return researcher, jc, judge_id, queue


class TestJudgeView:
    def test_queue(self):
        _, jc, _, queue = _assigned()
        assert len(queue) == 2
        assert set(queue[0]) == {"assignment_id", "status", "topic_title", "word_count"}
        assert {q["status"] for q in queue} == {"pending"}

    def test_view_is_blinded(self):
        """Nothing in what a judge receives says who wrote a text, under
        which condition, in which session — or even which text id it is."""
        _, jc, _, queue = _assigned()
        for q in queue:
            r = jc.get(f"/api/judge/texts/{q['assignment_id']}")
            assert r.status_code == 200
            view = r.json()
            assert set(view) == {
                "assignment_id",
                "status",
                "topic_title",
                "topic_brief",
                "content",
                "word_count",
                "rubric",
                "rating",
            }
            assert view["content"].startswith("An introduction to")
            assert view["topic_brief"]
            assert view["rating"] is None
            # The rubric block is the same for every text (its guess
            # options carry the analysis vocabulary); everything that
            # varies with *this* text must be silent about its origin.
            about_this_text = json.dumps({k: v for k, v in view.items() if k != "rubric"}).lower()
            for leak in (
                "elenchus",
                "baseline",
                "p01",
                "real name",
                "session",
                "text_id",
                "participant",
                "period",
            ):
                assert leak not in about_this_text, leak

    def test_rate_and_revise(self):
        researcher, jc, _, queue = _assigned()
        aid = queue[0]["assignment_id"]
        r = jc.post(
            f"/api/judge/texts/{aid}/rate",
            json={
                "ratings": GOOD,
                "justification": "  Covers the core concepts.  ",
                "condition_guess": "unsure",
                "confidence": 2,
                "seconds_spent": 340,
            },
        )
        assert r.status_code == 200, r.text
        view = jc.get(f"/api/judge/texts/{aid}").json()
        assert view["status"] == "completed"
        assert view["rating"] == {
            "ratings": GOOD,
            "justification": "Covers the core concepts.",
            "condition_guess": "unsure",
            "confidence": 2,
        }
        # A revision is a new row; the newest counts, the first is kept.
        revised = {**GOOD, "coverage": 6}
        jc.post(f"/api/judge/texts/{aid}/rate", json={"ratings": revised})
        con = get_registry().platform_con()
        history = pdb.list_text_ratings(con, aid)
        assert [h["ratings"]["coverage"] for h in history] == [5, 6]
        assert history[0]["seconds_spent"] == 340
        assert {h["rubric_version"] for h in history} == {text_judging.RUBRIC_VERSION}
        assert pdb.latest_text_rating(con, aid)["ratings"] == revised
        # Progress shows up for the researcher.
        listing = researcher.get("/api/admin/study/PILOT/texts").json()
        assert listing["judges"][0]["rated"] == 1
        assert sorted(t["rated"] for t in listing["texts"]) == [0, 1]

    @pytest.mark.parametrize(
        "body",
        [
            {"ratings": {"coverage": 5}},
            {"ratings": {**GOOD, "coverage": 9}},
            {"ratings": GOOD, "condition_guess": "structured"},
            {"ratings": GOOD, "confidence": 8},
            {"ratings": GOOD, "seconds_spent": -1},
        ],
    )
    def test_bad_ratings_rejected_whole(self, body):
        _, jc, _, queue = _assigned()
        aid = queue[0]["assignment_id"]
        assert jc.post(f"/api/judge/texts/{aid}/rate", json=body).status_code == 400
        assert pdb.latest_text_rating(get_registry().platform_con(), aid) is None
        assert jc.get(f"/api/judge/texts/{aid}").json()["status"] == "pending"

    def test_judges_cannot_see_each_others_assignments(self):
        researcher, jc, _, queue = _assigned()
        other_id, other = _actor("judge", "judge2")
        aid = queue[0]["assignment_id"]
        assert other.get(f"/api/judge/texts/{aid}").status_code == 403
        assert (
            other.post(f"/api/judge/texts/{aid}/rate", json={"ratings": GOOD}).status_code == 403
        )
        assert other.get("/api/judge/texts").json()["assignments"] == []
        assert jc.get("/api/judge/texts/999999").status_code == 404

    def test_researchers_do_not_rate(self):
        researcher, _, _, queue = _assigned()
        aid = queue[0]["assignment_id"]
        assert researcher.get(f"/api/judge/texts/{aid}").status_code == 403
        assert researcher.get("/api/judge/rubric").status_code == 403

    def test_rubric_route(self):
        _, jc, _, _ = _assigned()
        assert jc.get("/api/judge/rubric").json() == text_judging.rubric()
