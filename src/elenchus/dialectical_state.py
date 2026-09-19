"""
dialectical_state.py — Elenchus dialectical state (Definition 4)

S = ⟨[C : D], T, I⟩ backed by a DuckDB material base.

The mapping to material base (Definition 7):
    L_B = C ∪ D
    |∼_B = I ∪ Cont

Every method that changes the state also appends a `state_events` row
(see turn_log.py) — here rather than in the callers, so the opponent,
the UI action routes, the CLI and the scripts are all captured and no
new code path can forget to. Callers pass an `EventContext` to say who
is behind the change; without one the event is recorded as 'direct'.
"""

from . import study_text, turn_log
from .material_base import MaterialBase, set_to_str, str_to_set
from .turn_log import EventContext


class DialecticalState:
    def __init__(self, base: MaterialBase):
        self.base = base
        self._reseed_sequences()

    @classmethod
    def create(cls, db_path: str, name: str) -> "DialecticalState":
        return cls(MaterialBase.create(db_path, name))

    @classmethod
    def open(cls, db_path: str) -> "DialecticalState":
        return cls(MaterialBase.open(db_path))

    @classmethod
    def in_memory(cls, name: str = "inquiry") -> "DialecticalState":
        return cls(MaterialBase.in_memory(name))

    def _reseed_sequences(self):
        """Re-seed the tension and conversation sequences from the current
        max(id) in their respective tables. The migration runner creates
        the tables themselves (see migrations/base/0001_initial.sql); the
        sequences are kept procedural because DuckDB sequences are
        first-class database objects but the application-level invariant
        we want — `nextval` always exceeds any existing id — is easier to
        guarantee by re-deriving on every connection open. A future
        migration will replace this pattern with identity columns.
        """
        # tension_seq tracks tensions.id
        self.base.con.execute("DROP SEQUENCE IF EXISTS tension_seq")
        max_tid = self.base.con.execute("SELECT COALESCE(MAX(id), 0) FROM tensions").fetchone()[0]
        self.base.con.execute(f"CREATE SEQUENCE tension_seq START {max_tid + 1}")
        # conv_seq tracks conversation.id
        self.base.con.execute("DROP SEQUENCE IF EXISTS conv_seq")
        max_cid = self.base.con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM conversation"
        ).fetchone()[0]
        self.base.con.execute(f"CREATE SEQUENCE conv_seq START {max_cid + 1}")
        # The capture tables' sequences (turn_log.py, study_text.py).
        for seq, table in (
            (turn_log.TURN_SEQ, "turn_log"),
            (turn_log.EVENT_SEQ, "state_events"),
            (study_text.SNAPSHOT_SEQ, "text_snapshots"),
            (study_text.EDITOR_EVENT_SEQ, "editor_events"),
        ):
            self.base.con.execute(f"DROP SEQUENCE IF EXISTS {seq}")
            max_id = self.base.con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[
                0
            ]
            self.base.con.execute(f"CREATE SEQUENCE {seq} START {max_id + 1}")

    def _log_event(
        self,
        event_type: str,
        payload: dict,
        event: EventContext | None,
        outcome: str = "applied",
        note: str = "",
    ) -> None:
        turn_log.record_state_event(
            self.base.con, event_type, payload, event=event, outcome=outcome, note=note
        )

    def _prior_positions(self, prop: str) -> list:
        """Where `prop` stood before a change — `positions` is an upsert,
        so this is the only record of what a commit/deny overwrote."""
        rows = self.base.con.execute(
            "SELECT side, status FROM positions WHERE atom=? ORDER BY side", [prop]
        ).fetchall()
        return [{"side": r[0], "status": r[1]} for r in rows]

    # ── Position [C : D] ──

    @property
    def C(self) -> list:
        rows = self.base.con.execute(
            "SELECT atom FROM positions WHERE side='C' AND status='open' ORDER BY introduced_at"
        ).fetchall()
        return [r[0] for r in rows]

    @property
    def D(self) -> list:
        rows = self.base.con.execute(
            "SELECT atom FROM positions WHERE side='D' AND status='open' ORDER BY introduced_at"
        ).fetchall()
        return [r[0] for r in rows]

    @property
    def retracted(self) -> list:
        rows = self.base.con.execute(
            "SELECT DISTINCT atom FROM positions WHERE status='retracted' ORDER BY introduced_at"
        ).fetchall()
        return [r[0] for r in rows]

    def commit(self, prop: str, *, event: EventContext | None = None):
        # INSERT OR REPLACE keeps the upsert semantics of the original
        # try/except pattern (a re-committed atom refreshes its row) while
        # remaining transaction-safe — a ConstraintException inside an
        # outer transaction would abort the transaction.
        prior = self._prior_positions(prop)
        self.base.add_atoms({prop}, contributor="respondent")
        self.base.con.execute(
            "INSERT OR REPLACE INTO positions "
            "(atom, side, status, introduced_at) "
            "VALUES (?, 'C', 'open', CURRENT_TIMESTAMP)",
            [prop],
        )
        self._log_event("COMMIT", {"proposition": prop, "prior": prior}, event)

    def deny(self, prop: str, *, event: EventContext | None = None):
        # See `commit()` for the rationale on INSERT OR REPLACE.
        prior = self._prior_positions(prop)
        self.base.add_atoms({prop}, contributor="respondent")
        self.base.con.execute(
            "INSERT OR REPLACE INTO positions "
            "(atom, side, status, introduced_at) "
            "VALUES (?, 'D', 'open', CURRENT_TIMESTAMP)",
            [prop],
        )
        self._log_event("DENY", {"proposition": prop, "prior": prior}, event)

    def retract_prop(self, prop: str, *, event: EventContext | None = None) -> bool:
        n = self.base.con.execute(
            "UPDATE positions SET status='retracted' "
            "WHERE atom=? AND status='open' RETURNING side",
            [prop],
        ).fetchall()
        self._log_event(
            "RETRACT",
            {"proposition": prop, "sides": sorted(r[0] for r in n)},
            event,
            outcome="applied" if n else "noop",
            note="" if n else "proposition not held",
        )
        return len(n) > 0

    # ── Tensions T ──

    @property
    def T(self) -> list:
        """All open tensions, ordered by id. The first is the focal tension
        (shown to respondent and opponent); the rest are queued."""
        rows = self.base.con.execute(
            "SELECT id, gamma, delta, reason FROM tensions WHERE status='open' ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "gamma": list(str_to_set(r[1])),
                "delta": list(str_to_set(r[2])),
                "reason": r[3],
            }
            for r in rows
        ]

    @property
    def focal_tension(self) -> dict | None:
        """The single open tension currently surfaced to the respondent.
        Lowest-id open tension, or None if none are open."""
        t = self.T
        return t[0] if t else None

    @property
    def queued_tensions(self) -> list:
        """Open tensions waiting their turn behind the focal one."""
        return self.T[1:]

    @property
    def contested_tensions(self) -> list:
        rows = self.base.con.execute(
            "SELECT id, gamma, delta, reason FROM tensions WHERE status='contested' ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "gamma": list(str_to_set(r[1])),
                "delta": list(str_to_set(r[2])),
                "reason": r[3],
            }
            for r in rows
        ]

    def add_tension(
        self, gamma: list, delta: list, reason: str = "", *, event: EventContext | None = None
    ) -> int:
        tid = self.base.con.execute("SELECT nextval('tension_seq')").fetchone()[0]
        self.base.con.execute(
            "INSERT INTO tensions "
            "(id, gamma, delta, reason, status, proposed_at, resolved_at) "
            "VALUES (?,?,?,?,'open',CURRENT_TIMESTAMP,NULL)",
            [tid, set_to_str(set(gamma)), set_to_str(set(delta)), reason],
        )
        self._log_event(
            "PROPOSE_TENSION",
            {
                "tension_id": tid,
                "gamma": sorted(set(gamma)),
                "delta": sorted(set(delta)),
                "reason": reason,
            },
            event,
        )
        return tid

    def accept_tension(self, tid: int, *, event: EventContext | None = None) -> dict:
        row = self.base.con.execute(
            "SELECT gamma, delta, reason FROM tensions WHERE id=? AND status='open'", [tid]
        ).fetchone()
        if not row:
            self._log_event(
                "ACCEPT_TENSION",
                {"tension_id": tid},
                event,
                outcome="noop",
                note="tension not found or not open",
            )
            return None
        gamma = list(str_to_set(row[0]))
        delta = list(str_to_set(row[1]))
        reason = row[2]
        # Provenance: this assertion exists because the respondent accepted
        # tension #tid. Downstream tooling can use `earned_via_tension` to
        # reconstruct the dialectical history of each rule.
        provenance = {
            "source": "tension",
            "earned_via_tension": tid,
            "reason": reason,
        }
        self.base.accept(
            set(gamma),
            set(delta),
            "respondent",
            f"Tension #{tid}: {reason}",
            domain="tension",
            provenance=provenance,
        )
        self.base.con.execute(
            "UPDATE tensions SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            [tid],
        )
        self._log_event(
            "ACCEPT_TENSION",
            {"tension_id": tid, "gamma": sorted(gamma), "delta": sorted(delta), "reason": reason},
            event,
        )
        return {"gamma": gamma, "delta": delta, "reason": reason}

    def contest_tension(self, tid: int, *, event: EventContext | None = None) -> bool:
        n = self.base.con.execute(
            "UPDATE tensions SET status='contested', "
            "resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status='open' "
            "RETURNING id",
            [tid],
        ).fetchall()
        self._log_event(
            "CONTEST_TENSION",
            {"tension_id": tid},
            event,
            outcome="applied" if n else "noop",
            note="" if n else "tension not found or not open",
        )
        return len(n) > 0

    # ── Phase B: direct theory articulation ──
    #
    # `assert_implication`, `introduce_bearer`, and `retract_implication`
    # let the respondent articulate theory directly — bypassing the
    # tension-resolution loop — for the cases where the respondent
    # *already knows* what rules and vocabulary they want. The opponent
    # routes natural-language phrasings into these via the
    # ASSERT_IMPLICATION / INTRODUCE_BEARER / RETRACT_IMPLICATION speech
    # acts (see opponent.py).

    def assert_implication(
        self,
        gamma: list,
        delta: list,
        reason: str = "",
        provenance: dict | None = None,
        *,
        event: EventContext | None = None,
    ) -> int:
        """Directly assert `{gamma} |~ {delta}` as a holding sequent.

        Ensures every atom referenced exists in L_B (adds them with
        contributor='respondent' if not), then inserts a 'holds'
        assessment with `domain='asserted'`. Returns the new
        assessments.id so the caller (or a later RETRACT_IMPLICATION)
        can target it.
        """
        gamma_set = set(gamma)
        delta_set = set(delta)
        for a in gamma_set | delta_set:
            self.base.add_atoms({a}, contributor="respondent")

        prov = {"source": "asserted", **(provenance or {})}
        if reason and "reason" not in prov:
            prov["reason"] = reason

        self.base.accept(
            gamma_set,
            delta_set,
            "respondent",
            reason,
            domain="asserted",
            provenance=prov,
        )
        # Recover the id of the row we just inserted so the caller can
        # log it / retract it later. Latest 'holds' row from this
        # respondent matching the same premises/conclusions/domain.
        row = self.base.con.execute(
            "SELECT id FROM assessments "
            "WHERE contributor='respondent' AND domain='asserted' "
            "AND premises=? AND conclusions=? "
            "ORDER BY assessed_at DESC, id DESC LIMIT 1",
            [set_to_str(gamma_set), set_to_str(delta_set)],
        ).fetchone()
        implication_id = int(row[0]) if row else -1
        self._log_event(
            "ASSERT_IMPLICATION",
            {
                "implication_id": implication_id,
                "gamma": sorted(gamma_set),
                "delta": sorted(delta_set),
                "reason": reason,
            },
            event,
        )
        return implication_id

    def introduce_bearer(
        self, prop: str, description: str = "", *, event: EventContext | None = None
    ) -> None:
        """Add `prop` to L_B without committing or denying it.

        Vocabulary-only contribution: the atom becomes part of the
        atomic language so future tensions and assertions can reference
        it, but the bilateral position [C : D] is untouched. Useful when
        the respondent wants to name a concept before deciding whether
        they endorse it.
        """
        if not prop:
            return
        self.base.add_atoms({prop}, contributor="respondent", description=description)
        self._log_event(
            "INTRODUCE_BEARER", {"proposition": prop, "description": description}, event
        )

    def retract_implication(
        self, implication_id: int, *, event: EventContext | None = None
    ) -> bool:
        """Retract a previously-asserted (or tension-derived) implication.

        Marks the underlying assessments row `status='retracted'`. The
        `current_assessments` view filters on `status='active'`, so the
        rule stops contributing to derivability immediately. Returns
        True if a row was retracted, False otherwise.
        """
        ok = self.base.retract_assessment(implication_id)
        self._log_event(
            "RETRACT_IMPLICATION",
            {"implication_id": implication_id},
            event,
            outcome="applied" if ok else "noop",
            note="" if ok else "implication not found or already retracted",
        )
        return ok

    # ── Material implications I ──

    @property
    def I(self) -> list:
        """All active material implications attributed to the respondent.

        Includes both `domain='tension'` (earned via tension acceptance)
        and `domain='asserted'` (Phase B direct articulation). Retracted
        rows are filtered out via `status='active'`. The `domain` field
        is exposed so the UI / report can distinguish tension-earned
        rules from directly-asserted ones.
        """
        rows = self.base.con.execute(
            "SELECT id, premises, conclusions, reason, domain FROM assessments "
            "WHERE contributor='respondent' AND domain IN ('tension','asserted') "
            "AND judgment='holds' AND status='active' "
            "ORDER BY assessed_at"
        ).fetchall()
        return [
            {
                "id": r[0],
                "gamma": list(str_to_set(r[1])),
                "delta": list(str_to_set(r[2])),
                "reason": r[3],
                "domain": r[4],
            }
            for r in rows
        ]

    # ── Conversation history (for multi-turn oracle) ──

    def get_conversation(self) -> list:
        """Get conversation history as API message format."""
        rows = self.base.con.execute(
            "SELECT role, content FROM conversation ORDER BY id"
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]

    def add_conversation(self, role: str, content: str) -> int:
        """Store a conversation turn. Only the natural language, not the
        full state context — the formal state is reconstructed from
        the DuckDB tables on each turn. Returns the new row's id (the
        turn log links to it)."""
        row = self.base.con.execute(
            "INSERT INTO conversation (id, role, content) "
            "VALUES (nextval('conv_seq'), ?, ?) RETURNING id",
            [role, content],
        ).fetchone()
        return row[0]

    def get_summary(self) -> str:
        """Get the running summary of the dialectic."""
        r = self.base.con.execute("SELECT value FROM meta WHERE key='summary'").fetchone()
        return r[0] if r else ""

    def set_summary(self, summary: str):
        """Update the running summary."""
        # INSERT OR REPLACE is transaction-safe; the original try/except
        # pattern would abort an outer transaction on conflict.
        self.base.con.execute("INSERT OR REPLACE INTO meta VALUES ('summary', ?)", [summary])

    # ── Derivability ──

    def derives(self, gamma: list, delta: list) -> bool:
        return self.base.derives(set(gamma), set(delta))

    def derive_with_trace(self, gamma: list, delta: list):
        """Return a `DerivationResult` (derivable, trace, depth). Raises
        `QuerySyntaxError` if a query sentence is malformed."""
        return self.base.derive_with_trace(set(gamma), set(delta))

    # ── Atom IDs (sequential by creation order) ──

    @property
    def atom_ids(self) -> dict:
        """Return {atom_text: sequential_id} ordered by added_at."""
        rows = self.base.con.execute(
            "SELECT sentence FROM atoms ORDER BY added_at, sentence"
        ).fetchall()
        return {r[0]: i + 1 for i, r in enumerate(rows)}

    # ── Full state as dict (for API) ──

    def to_dict(self) -> dict:
        focal = self.focal_tension
        queued = self.queued_tensions
        return {
            "name": self.base.name,
            "commitments": self.C,
            "denials": self.D,
            # 'tensions' holds only the focal tension (single-element list or
            # empty) — the respondent addresses one at a time. Queued open
            # tensions are exposed separately.
            "tensions": [focal] if focal else [],
            "queued_tensions": queued,
            "implications": self.I,
            "retracted": self.retracted,
            "contested": self.contested_tensions,
            "atom_ids": self.atom_ids,
        }
