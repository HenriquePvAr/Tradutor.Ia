"""Server-authoritative XP/rank primitives for the beta control plane.

The ledger is append-only and idempotent by ``event_id``; callers must supply
an already-authorized server event, never client-provided XP totals.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Final

RANKS: Final[tuple[tuple[int, str], ...]] = (
    (0, "Leitor Iniciante"), (50, "Explorador de Balões"),
    (200, "Caçador de Kanji"), (500, "Tradutor do Sekai"),
    (1000, "Mestre de Capítulos"), (2500, "Guardião das Histórias"),
    (5000, "Lenda do Sekai"),
)

@dataclass(frozen=True)
class Progression:
    xp: int
    rank: str

def rank_for_xp(xp: int) -> str:
    current = RANKS[0][1]
    for threshold, name in RANKS:
        if xp >= threshold:
            current = name
        else:
            break
    return current

class ProgressionLedger:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS progression_ledger (
            event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, xp INTEGER NOT NULL,
            source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        self.db.commit()

    def award_once(self, *, user_id: str, event_id: str, xp: int, source: str) -> bool:
        if not user_id or not event_id or xp <= 0 or not source:
            raise ValueError("invalid_progression_event")
        cur = self.db.execute(
            "INSERT OR IGNORE INTO progression_ledger(event_id,user_id,xp,source) VALUES(?,?,?,?)",
            (event_id, user_id, int(xp), source),
        )
        self.db.commit()
        return cur.rowcount == 1

    def get(self, user_id: str) -> Progression:
        row = self.db.execute("SELECT COALESCE(SUM(xp),0) FROM progression_ledger WHERE user_id=?", (user_id,)).fetchone()
        xp = int(row[0] or 0)
        return Progression(xp=xp, rank=rank_for_xp(xp))
