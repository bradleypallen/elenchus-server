"""Judging as the study's registration describes it (§2.5–2.6): each
judge sees a participant's two texts as a pair labelled A/B (labels
drawn per participant and per judge), rates each text on four
dimensions with a one-sentence justification per dimension, ranks the
pair, and — only once every pair in their queue is done — guesses each
text's condition with a confidence.

What is tested is what would break the analysis: a leak in the blinded
view, a pair assigned before both texts exist, a rating without its
justifications, a ranking before both ratings, a guess before the queue
is done, judges seeing each other's work, and the export missing any
of it.
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

CONFIG = {"topic_a_title": "Tides", "topic_b_title": "Clouds", "min_gap_hours": 0}
GOOD = {"coverage": 5, "correctness": 6, "concision": 4, "reasoning": 5}
WHY = {
    "coverage": "Names the core concepts.",
    "correctness": "Definitions are sound.",
    "concision": "A little repetitive.",
    "reasoning": "The distinctions are motivated.",
}


@pytest.fixture(autouse=True)
def _clean():
    reg = get_registry()
    reg.migrate_platform()
    con = reg.platform_con()
    with reg.platform_lock:
        for table in (
            "text_condition_guesses",
            "text_pair_rankings",
            "text_pair_assignments",
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


def _finish(p: TestClient, text: str) -> None:
    p.post("/api/study/session/begin-tutorial")
    p.post("/api/study/session/begin-task")
    assert p.post("/api/study/session/finish", json={"content": text}).status_code == 200
    for to_state in ("surveyed", "complete"):
        p.post("/api/study/session/advance", json={"to_state": to_state})


def _study_with_texts(n_participants: int = 2, *, half_done: int = 0) -> tuple[TestClient, list]:
    """A researcher, a configured study, and `n` participants who have
    each done both sessions (2n texts) — plus `half_done` who have done
    only their first."""
    _, researcher = _actor("researcher", "researcher")
    assert researcher.put("/api/admin/study/PILOT/config", json=CONFIG).status_code == 200
    people = []
    for i in range(n_participants + half_done):
        person = researcher.post(
            "/api/admin/study/PILOT/participants", json={"display_name": f"Real Name {i}"}
        ).json()
        people.append(person)
        sessions = person["sessions"] if i < n_participants else person["sessions"][:1]
        for planned in sessions:
            p = TestClient(app)
            assert p.post(f"/api/study/{planned['token']}").status_code == 200
            _finish(p, f"An introduction to {planned['topic_title']} written under {i}.")
    return researcher, people


def _assigned(n_participants: int = 1):
    researcher, people = _study_with_texts(n_participants)
    judge_id, jc = _actor("judge", "judge1")
    r = researcher.post(
        "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
    )
    assert r.status_code == 200, r.text
    queue = jc.get("/api/judge/pairs").json()["pairs"]
    return researcher, jc, judge_id, queue


def _rate_both(jc: TestClient, pair_id: int, **overrides) -> None:
    for label in ("A", "B"):
        r = jc.post(
            f"/api/judge/pairs/{pair_id}/rate",
            json={"label": label, "ratings": GOOD, "justifications": WHY, **overrides},
        )
        assert r.status_code == 200, r.text


def _complete_pair(jc: TestClient, pair_id: int, preferred: str = "A") -> None:
    _rate_both(jc, pair_id)
    r = jc.post(f"/api/judge/pairs/{pair_id}/rank", json={"preferred": preferred})
    assert r.status_code == 200, r.text


# ── Rubric ───────────────────────────────────────────────────────────


class TestRubric:
    def test_four_dimensions_from_the_solicitation(self):
        assert text_judging.DIMENSION_KEYS == ("coverage", "correctness", "concision", "reasoning")
        r = text_judging.rubric()
        assert r["version"] == text_judging.RUBRIC_VERSION == "2"
        assert r["scale"] == {"min": 1, "max": 7, "anchors": {"1": "very poor", "7": "excellent"}}
        assert r["justification"]["required"] is True
        assert r["ranking"]["options"] == ["A", "B"]
        assert r["guess"]["when"].startswith("after every pair")

    def test_judge_facing_wording_does_not_name_the_conditions(self):
        text = json.dumps(text_judging.rubric()).lower()
        # The guess *values* carry the vocabulary (the analysis needs them);
        # nothing the judge reads does.
        for d in text_judging.DIMENSIONS:
            assert "elenchus" not in (d["label"] + d["help"]).lower()
        for label in text_judging.CONDITION_GUESS_LABELS.values():
            assert "elenchus" not in label.lower() and "baseline" not in label.lower()
        assert "structured disagreement" in text

    def test_validate_accepts_a_complete_rating(self):
        assert text_judging.validate_ratings(GOOD) == GOOD
        assert text_judging.validate_justifications({k: f"  {v} " for k, v in WHY.items()}) == WHY

    @pytest.mark.parametrize(
        "bad, message",
        [
            ({"coverage": 5}, "missing"),
            ({**GOOD, "coverage": 0}, "from 1 to 7"),
            ({**GOOD, "coverage": "5"}, "whole number"),
            ({**GOOD, "coverage": True}, "whole number"),
            ({**GOOD, "style": 5}, "Unknown"),
            ("not a dict", "object"),
        ],
    )
    def test_validate_rejects(self, bad, message):
        with pytest.raises(ValueError, match=message):
            text_judging.validate_ratings(bad)

    @pytest.mark.parametrize(
        "bad, message",
        [
            ({k: v for k, v in WHY.items() if k != "concision"}, "concision"),
            ({**WHY, "concision": "   "}, "concision"),
            ({**WHY, "concision": "x" * 601}, "over 600"),
            ({**WHY, "style": "nice"}, "Unknown"),
            ([], "object"),
        ],
    )
    def test_justifications_rejected(self, bad, message):
        with pytest.raises(ValueError, match=message):
            text_judging.validate_justifications(bad)


# ── Assignment (researcher) ──────────────────────────────────────────


class TestAssign:
    def test_texts_listing_is_the_unblinded_researcher_view(self):
        researcher, _ = _study_with_texts(1)
        listing = researcher.get("/api/admin/study/PILOT/texts").json()
        assert listing["pairs_available"] == 1
        assert {t["condition"] for t in listing["texts"]} == {"elenchus", "baseline"}
        assert all(t["participant_code"] == "P01" for t in listing["texts"])
        assert sorted(t["period"] for t in listing["texts"]) == [1, 2]

    def test_a_pair_needs_both_texts(self):
        """A participant with one session done isn't judged yet — the
        unit is the pair. They appear once the second text is in."""
        researcher, people = _study_with_texts(1, half_done=1)
        judge_id, jc = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        )
        assert r.json()["created"] == 1
        assert researcher.get("/api/admin/study/PILOT/texts").json()["pairs_available"] == 1
        # The half-done person finishes their second session…
        second = people[1]["sessions"][1]
        p = TestClient(app)
        assert p.post(f"/api/study/{second['token']}").status_code == 200
        _finish(p, "Now the second text.")
        # …and pressing the button again adds exactly that pair.
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        )
        assert (r.json()["created"], r.json()["already_assigned"]) == (1, 1)
        assert len(jc.get("/api/judge/pairs").json()["pairs"]) == 2

    def test_assign_is_idempotent(self):
        researcher, _ = _study_with_texts(2)
        judge_id, _ = _actor("judge", "judge1")
        first = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        ).json()
        again = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
        ).json()
        assert (first["created"], again["created"], again["already_assigned"]) == (2, 0, 2)

    def test_assign_a_subset(self):
        researcher, people = _study_with_texts(2)
        judge_id, jc = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments",
            json={"judge_actor_id": judge_id, "participant_ids": [people[0]["id"]]},
        )
        assert r.json()["created"] == 1
        assert len(jc.get("/api/judge/pairs").json()["pairs"]) == 1

    def test_rejects_non_judges_and_foreign_participants(self):
        researcher, _ = _study_with_texts(1)
        rid, _ = _actor("researcher", "r2")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": rid}
        )
        assert r.status_code == 404
        judge_id, _ = _actor("judge", "judge1")
        r = researcher.post(
            "/api/admin/study/PILOT/text-assignments",
            json={"judge_actor_id": judge_id, "participant_ids": [999]},
        )
        assert r.status_code == 404

    def test_labels_are_drawn_per_pair(self):
        """Which text is "A" is decided per participant and per judge —
        across many draws both orders occur."""
        researcher, people = _study_with_texts(6)
        seen = set()
        for i in range(3):
            judge_id, _ = _actor("judge", f"judge{i}")
            researcher.post(
                "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
            )
        con = get_registry().platform_con()
        for pair in pdb.list_text_pairs_for_study(con, "PILOT"):
            a = pdb.find_study_text(con, pair["label_a_text_id"])
            seen.add(a["condition"])
        assert seen == {"elenchus", "baseline"}

    def test_each_judge_gets_their_own_order(self):
        researcher, _ = _study_with_texts(6)
        orders = []
        for i in range(3):
            judge_id, jc = _actor("judge", f"judge{i}")
            researcher.post(
                "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": judge_id}
            )
            orders.append(
                tuple(p["topics"]["A"] for p in jc.get("/api/judge/pairs").json()["pairs"])
            )
        # Six pairs: three identical random orders is 1 in 518400.
        assert len(set(orders)) > 1

    def test_researcher_can_list_judges(self):
        researcher, _ = _study_with_texts(0)
        _actor("judge", "judge1")
        assert [
            j["display_name"] for j in researcher.get("/api/admin/study/judges").json()["judges"]
        ] == ["judge1"]

    def test_access(self):
        _, user = _actor("user", "someone")
        assert user.get("/api/admin/study/PILOT/texts").status_code == 403
        assert (
            user.post(
                "/api/admin/study/PILOT/text-assignments", json={"judge_actor_id": 1}
            ).status_code
            == 403
        )


# ── The judge ────────────────────────────────────────────────────────


class TestJudgeView:
    def test_queue(self):
        _, jc, _, queue = _assigned(2)
        assert len(queue) == 2
        assert set(queue[0]) == {"pair_id", "status", "topics", "rated", "ranked", "preferred"}
        assert set(queue[0]["topics"]) == {"A", "B"}
        assert {q["status"] for q in queue} == {"pending"}
        assert queue[0]["rated"] == {"A": False, "B": False} and queue[0]["ranked"] is False

    def test_view_is_blinded(self):
        """Nothing in what a judge receives says who wrote a text, under
        which condition, in which session or period — or even which text
        id it is. The pair id is the only identifier, and it is the
        judge's own."""
        _, jc, _, queue = _assigned()
        view = jc.get(f"/api/judge/pairs/{queue[0]['pair_id']}").json()
        assert set(view) == {
            "pair_id",
            "status",
            "texts",
            "rubric",
            "rated",
            "ranked",
            "preferred",
        }
        assert set(view["texts"]) == {"A", "B"}
        for label in ("A", "B"):
            t = view["texts"][label]
            assert set(t) == {"topic_title", "topic_brief", "content", "word_count", "rating"}
            assert t["content"].startswith("An introduction to") and t["rating"] is None
        about_this_pair = json.dumps({k: v for k, v in view.items() if k != "rubric"}).lower()
        for leak in (
            "elenchus",
            "baseline",
            "p01",
            "real name",
            "session",
            "text_id",
            "participant",
            "period",
            "condition",
        ):
            assert leak not in about_this_pair, leak

    def test_rate_rank_and_revise(self):
        researcher, jc, _, queue = _assigned()
        pid = queue[0]["pair_id"]
        r = jc.post(
            f"/api/judge/pairs/{pid}/rate",
            json={"label": "A", "ratings": GOOD, "justifications": WHY, "seconds_spent": 340},
        )
        assert r.status_code == 200, r.text
        view = jc.get(f"/api/judge/pairs/{pid}").json()
        assert view["texts"]["A"]["rating"] == {"ratings": GOOD, "justifications": WHY}
        assert view["rated"] == {"A": True, "B": False} and view["status"] == "pending"
        # Ranking needs both texts rated.
        r = jc.post(f"/api/judge/pairs/{pid}/rank", json={"preferred": "A"})
        assert r.status_code == 400 and "both" in r.json()["detail"]["user_message"]
        jc.post(
            f"/api/judge/pairs/{pid}/rate",
            json={"label": "B", "ratings": GOOD, "justifications": WHY},
        )
        r = jc.post(f"/api/judge/pairs/{pid}/rank", json={"preferred": "B", "seconds_spent": 20})
        assert r.status_code == 200 and r.json()["completed"] is True
        view = jc.get(f"/api/judge/pairs/{pid}").json()
        assert view["status"] == "completed" and view["preferred"] == "B"
        # A revision is a new row; the newest counts, the first is kept.
        revised = {**GOOD, "coverage": 6}
        jc.post(
            f"/api/judge/pairs/{pid}/rate",
            json={"label": "A", "ratings": revised, "justifications": WHY},
        )
        jc.post(f"/api/judge/pairs/{pid}/rank", json={"preferred": "A"})
        con = get_registry().platform_con()
        pair = pdb.find_text_pair(con, pid)
        a = pdb.find_text_assignment_for_pair_text(con, pid, pair["label_a_text_id"])
        history = pdb.list_text_ratings(con, a["id"])
        assert [h["ratings"]["coverage"] for h in history] == [5, 6]
        assert history[0]["seconds_spent"] == 340 and history[0]["justifications"] == WHY
        assert {h["rubric_version"] for h in history} == {"2"}
        rankings = pdb.list_pair_rankings(con, pid)
        assert [r["preferred_text_id"] for r in rankings] == [
            pair["label_b_text_id"],
            pair["label_a_text_id"],
        ]
        # Progress shows up for the researcher.
        listing = researcher.get("/api/admin/study/PILOT/texts").json()
        j = listing["judges"][0]
        assert (j["assigned"], j["completed"], j["texts_rated"]) == (1, 1, 2)
        assert (j["guesses"], j["guesses_required"]) == (0, 2)
        assert sorted(t["rated"] for t in listing["texts"]) == [1, 1]

    @pytest.mark.parametrize(
        "body",
        [
            {"label": "A", "ratings": {"coverage": 5}, "justifications": WHY},
            {"label": "A", "ratings": {**GOOD, "coverage": 9}, "justifications": WHY},
            {"label": "A", "ratings": GOOD, "justifications": {}},
            {"label": "A", "ratings": GOOD, "justifications": {**WHY, "coverage": ""}},
            {"label": "C", "ratings": GOOD, "justifications": WHY},
            {"label": "A", "ratings": GOOD, "justifications": WHY, "seconds_spent": -1},
        ],
    )
    def test_bad_ratings_rejected_whole(self, body):
        _, jc, _, queue = _assigned()
        pid = queue[0]["pair_id"]
        assert jc.post(f"/api/judge/pairs/{pid}/rate", json=body).status_code == 400
        assert jc.get(f"/api/judge/pairs/{pid}").json()["rated"] == {"A": False, "B": False}

    def test_judges_cannot_see_each_others_pairs(self):
        _, jc, _, queue = _assigned()
        _, other = _actor("judge", "judge2")
        pid = queue[0]["pair_id"]
        assert other.get(f"/api/judge/pairs/{pid}").status_code == 403
        assert (
            other.post(
                f"/api/judge/pairs/{pid}/rate",
                json={"label": "A", "ratings": GOOD, "justifications": WHY},
            ).status_code
            == 403
        )
        assert (
            other.post(f"/api/judge/pairs/{pid}/rank", json={"preferred": "A"}).status_code == 403
        )
        assert other.get("/api/judge/pairs").json()["pairs"] == []
        assert jc.get("/api/judge/pairs/999999").status_code == 404

    def test_researchers_do_not_rate(self):
        researcher, _, _, queue = _assigned()
        assert researcher.get(f"/api/judge/pairs/{queue[0]['pair_id']}").status_code == 403
        assert researcher.get("/api/judge/rubric").status_code == 403

    def test_rubric_route(self):
        _, jc, _, _ = _assigned()
        assert jc.get("/api/judge/rubric").json() == text_judging.rubric()


class TestGuessing:
    def test_closed_until_every_pair_is_done(self):
        _, jc, _, queue = _assigned(2)
        assert jc.get("/api/judge/pairs").json()["guessing"] == {
            "open": False,
            "required": 4,
            "done": 0,
        }
        assert jc.get("/api/judge/guesses").json()["items"] == []
        r = jc.post(
            "/api/judge/guesses",
            json={
                "pair_id": queue[0]["pair_id"],
                "label": "A",
                "guess": "elenchus",
                "confidence": 5,
            },
        )
        assert r.status_code == 400 and "every pair" in r.json()["detail"]["user_message"]
        _complete_pair(jc, queue[0]["pair_id"])
        assert jc.get("/api/judge/guesses").json()["open"] is False
        _complete_pair(jc, queue[1]["pair_id"])
        todo = jc.get("/api/judge/guesses").json()
        assert todo["open"] is True and len(todo["items"]) == 4
        assert set(todo["items"][0]) == {"pair_id", "label", "topic_title", "guess", "confidence"}
        assert [o["value"] for o in todo["options"]] == ["elenchus", "baseline", "unsure"]

    def test_guess_and_revise(self):
        researcher, jc, judge_id, queue = _assigned(1)
        pid = queue[0]["pair_id"]
        _complete_pair(jc, pid)
        r = jc.post(
            "/api/judge/guesses",
            json={"pair_id": pid, "label": "A", "guess": "elenchus", "confidence": 6},
        )
        assert r.status_code == 200, r.text
        r = jc.post("/api/judge/guesses", json={"pair_id": pid, "label": "B", "guess": "unsure"})
        assert r.status_code == 200
        items = {i["label"]: i for i in jc.get("/api/judge/guesses").json()["items"]}
        assert (items["A"]["guess"], items["A"]["confidence"]) == ("elenchus", 6)
        assert items["B"]["guess"] == "unsure"
        # Revise: the newest counts, the earlier is kept.
        jc.post(
            "/api/judge/guesses",
            json={"pair_id": pid, "label": "A", "guess": "baseline", "confidence": 2},
        )
        con = get_registry().platform_con()
        pair = pdb.find_text_pair(con, pid)
        history = pdb.list_condition_guesses(
            con, judge_actor_id=judge_id, text_id=pair["label_a_text_id"]
        )
        assert [h["guess"] for h in history] == ["elenchus", "baseline"]
        assert jc.get("/api/judge/pairs").json()["guessing"] == {
            "open": True,
            "required": 2,
            "done": 2,
        }
        j = researcher.get("/api/admin/study/PILOT/texts").json()["judges"][0]
        assert (j["guesses"], j["guesses_required"]) == (2, 2)

    @pytest.mark.parametrize(
        "body",
        [
            {"label": "A", "guess": "structured"},
            {"label": "A", "guess": "elenchus", "confidence": 8},
            {"label": "C", "guess": "elenchus"},
        ],
    )
    def test_bad_guesses_rejected(self, body):
        _, jc, _, queue = _assigned(1)
        pid = queue[0]["pair_id"]
        _complete_pair(jc, pid)
        assert jc.post("/api/judge/guesses", json={"pair_id": pid, **body}).status_code == 400
