"""
dialectical_state.py — Elenchus dialectical state (Definition 4)

S = ⟨[C : D], T, I⟩ backed by a DuckDB material base.

The mapping to material base (Definition 7):
    L_B = C ∪ D
    |∼_B = I ∪ Cont
"""

import duckdb

from material_base import MaterialBase, set_to_str, str_to_set


class DialecticalState:
    def __init__(self, base: MaterialBase):
        self.base = base
        self._ensure_tables()

    @classmethod
    def create(cls, db_path: str, name: str) -> "DialecticalState":
        return cls(MaterialBase.create(db_path, name))

    @classmethod
    def open(cls, db_path: str) -> "DialecticalState":
        return cls(MaterialBase.open(db_path))

    @classmethod
    def in_memory(cls, name: str = "inquiry") -> "DialecticalState":
        return cls(MaterialBase.in_memory(name))

    def _ensure_tables(self):
        self.base.con.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                atom VARCHAR NOT NULL,
                side VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'open',
                introduced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK(side IN ('C', 'D')),
                CHECK(status IN ('open', 'retracted')),
                PRIMARY KEY(atom, side)
            )
        """)
        self.base.con.execute("""
            CREATE TABLE IF NOT EXISTS tensions (
                id INTEGER PRIMARY KEY,
                gamma VARCHAR NOT NULL,
                delta VARCHAR NOT NULL,
                reason VARCHAR DEFAULT '',
                status VARCHAR DEFAULT 'open',
                proposed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                CHECK(status IN ('open', 'accepted', 'contested'))
            )
        """)
        # Re-seed tension sequence from max existing id to survive reconnects
        self.base.con.execute("DROP SEQUENCE IF EXISTS tension_seq")
        max_tid = self.base.con.execute("SELECT COALESCE(MAX(id), 0) FROM tensions").fetchone()[0]
        self.base.con.execute(f"CREATE SEQUENCE tension_seq START {max_tid + 1}")
        # Conversation history for multi-turn oracle
        self.base.con.execute("""
            CREATE TABLE IF NOT EXISTS conversation (
                id INTEGER PRIMARY KEY,
                role VARCHAR NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Re-seed conversation sequence from max existing id to survive reconnects
        self.base.con.execute("DROP SEQUENCE IF EXISTS conv_seq")
        max_cid = self.base.con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM conversation"
        ).fetchone()[0]
        self.base.con.execute(f"CREATE SEQUENCE conv_seq START {max_cid + 1}")

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

    def commit(self, prop: str):
        self.base.add_atoms({prop}, contributor="respondent")
        try:
            self.base.con.execute(
                "INSERT INTO positions VALUES (?, 'C', 'open', CURRENT_TIMESTAMP)", [prop]
            )
        except duckdb.ConstraintException:
            self.base.con.execute(
                "UPDATE positions SET status='open', side='C', "
                "introduced_at=CURRENT_TIMESTAMP WHERE atom=?",
                [prop],
            )

    def deny(self, prop: str):
        self.base.add_atoms({prop}, contributor="respondent")
        try:
            self.base.con.execute(
                "INSERT INTO positions VALUES (?, 'D', 'open', CURRENT_TIMESTAMP)", [prop]
            )
        except duckdb.ConstraintException:
            self.base.con.execute(
                "UPDATE positions SET status='open', side='D', "
                "introduced_at=CURRENT_TIMESTAMP WHERE atom=?",
                [prop],
            )

    def retract_prop(self, prop: str) -> bool:
        n = self.base.con.execute(
            "UPDATE positions SET status='retracted' "
            "WHERE atom=? AND status='open' RETURNING atom",
            [prop],
        ).fetchall()
        return len(n) > 0

    # ── Tensions T ──

    @property
    def T(self) -> list:
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

    def add_tension(self, gamma: list, delta: list, reason: str = "") -> int:
        tid = self.base.con.execute("SELECT nextval('tension_seq')").fetchone()[0]
        self.base.con.execute(
            "INSERT INTO tensions VALUES (?,?,?,?,'open',CURRENT_TIMESTAMP,NULL)",
            [tid, set_to_str(set(gamma)), set_to_str(set(delta)), reason],
        )
        return tid

    def accept_tension(self, tid: int) -> dict:
        row = self.base.con.execute(
            "SELECT gamma, delta, reason FROM tensions WHERE id=? AND status='open'", [tid]
        ).fetchone()
        if not row:
            return None
        gamma = list(str_to_set(row[0]))
        delta = list(str_to_set(row[1]))
        reason = row[2]
        self.base.accept(
            set(gamma), set(delta), "respondent", f"Tension #{tid}: {reason}", domain="tension"
        )
        self.base.con.execute(
            "UPDATE tensions SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            [tid],
        )
        return {"gamma": gamma, "delta": delta, "reason": reason}

    def contest_tension(self, tid: int) -> bool:
        n = self.base.con.execute(
            "UPDATE tensions SET status='contested', "
            "resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status='open' "
            "RETURNING id",
            [tid],
        ).fetchall()
        return len(n) > 0

    # ── Material implications I ──

    @property
    def I(self) -> list:
        rows = self.base.con.execute(
            "SELECT premises, conclusions, reason FROM assessments "
            "WHERE contributor='respondent' AND domain='tension' "
            "AND judgment='holds' ORDER BY assessed_at"
        ).fetchall()
        return [
            {"gamma": list(str_to_set(r[0])), "delta": list(str_to_set(r[1])), "reason": r[2]}
            for r in rows
        ]

    # ── Conversation history (for multi-turn oracle) ──

    def get_conversation(self) -> list:
        """Get conversation history as API message format."""
        rows = self.base.con.execute(
            "SELECT role, content FROM conversation ORDER BY id"
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]

    def add_conversation(self, role: str, content: str):
        """Store a conversation turn. Only the natural language, not the
        full state context — the formal state is reconstructed from
        the DuckDB tables on each turn."""
        self.base.con.execute(
            "INSERT INTO conversation (id, role, content) VALUES (nextval('conv_seq'), ?, ?)",
            [role, content],
        )

    def get_summary(self) -> str:
        """Get the running summary of the dialectic."""
        r = self.base.con.execute("SELECT value FROM meta WHERE key='summary'").fetchone()
        return r[0] if r else ""

    def set_summary(self, summary: str):
        """Update the running summary."""
        try:
            self.base.con.execute("INSERT INTO meta VALUES ('summary', ?)", [summary])
        except duckdb.ConstraintException:
            self.base.con.execute("UPDATE meta SET value=? WHERE key='summary'", [summary])

    # ── Derivability ──

    def derives(self, gamma: list, delta: list) -> bool:
        return self.base.derives(set(gamma), set(delta))

    def derive_with_trace(self, gamma: list, delta: list):
        """Return full ProofResult including trace."""
        return self.base.derive_with_trace(set(gamma), set(delta))

    # ── Full state as dict (for API) ──

    def to_dict(self) -> dict:
        return {
            "name": self.base.name,
            "commitments": self.C,
            "denials": self.D,
            "tensions": self.T,
            "implications": self.I,
            "retracted": self.retracted,
            "contested": self.contested_tensions,
        }
