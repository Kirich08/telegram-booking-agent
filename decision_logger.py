"""Decision log: one TSV row per real message.

Columns: timestamp, direction, message, understood, decided, message_id

Two kinds of facts are kept apart:
  - TRANSPORT facts (what really arrived or was really sent: exact text, time,
    message id, direction) are written by the PROGRAM. The model cannot change them.
  - INTERPRETATION (what the agent understood and decided) comes from the model's
    log_decision calls and is only ATTACHED to the matching real message.

A sent message is recorded only after the transport reports success. A failed send
is recorded as direction "send_failed", never as "sent". Tabs and newlines inside
values are escaped so every record stays on one line. Timestamps are UTC ISO.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from booking_state import normalize

HEADER = "timestamp\tdirection\tmessage\tunderstood\tdecided\tmessage_id"
NO_INTERPRETATION = "(not provided by the model)"


def escape_field(value: str) -> str:
    """Make a value safe for one TSV cell (reversible: backslashes are escaped first)."""
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


class DecisionLogger:
    """Append-only TSV writer. Writes are serialised with an asyncio lock."""

    def __init__(self, path: str | Path = "decisions.log") -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self.path.write_text(HEADER + "\n", encoding="utf-8")

    async def log(
        self,
        direction: str,
        message: str,
        understood: str = "",
        decided: str = "",
        message_id: str = "",
        timestamp: datetime | None = None,
    ) -> None:
        """Append one row."""
        stamp = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        row = "\t".join(escape_field(v) for v in (stamp, direction, message, understood, decided, message_id))
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(row + "\n")


@dataclass
class PendingRecord:
    """A real message waiting for the end of the turn to be written."""

    direction: str
    text: str
    message_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    understood: str = ""
    decided: str = ""
    annotated: bool = False


class TurnLog:
    """Collects transport records during a turn and writes them in order at the end."""

    def __init__(self, logger: DecisionLogger, sent_direction: str = "sent") -> None:
        self.logger = logger
        self.sent_direction = sent_direction  # "sent_dry_run" in dry-run mode
        self._pending: list[PendingRecord] = []
        self._waiting: dict[str, tuple[str, str]] = {}  # interpretation that arrived before its message

    def add(self, direction: str, text: str, message_id: str = "", understood: str = "", decided: str = "") -> PendingRecord:
        """Record a real message (called by the program, never by the model)."""
        rec = PendingRecord(direction, text, message_id, understood=understood, decided=decided)
        if direction in self._waiting and not understood and not decided:
            rec.understood, rec.decided = self._waiting.pop(direction)
            rec.annotated = True
        self._pending.append(rec)
        return rec

    def annotate(self, direction: str, understood: str, decided: str, claimed_text: str = "") -> None:
        """Attach the model's interpretation to the latest un-annotated real message of that direction.

        If the model's quoted text differs from the real text, the real text stays
        and a note is added.
        """
        direction = self.sent_direction if direction == "sent" else direction
        target = next((r for r in reversed(self._pending) if r.direction == direction and not r.annotated), None)
        if target is None:
            self._waiting[direction] = (understood, decided)  # message not recorded yet; keep for it
            return
        if claimed_text and normalize(claimed_text) != normalize(target.text):
            understood = f"{understood} [model quoted different text; real text logged]".strip()
        target.understood, target.decided, target.annotated = understood, decided, True

    async def flush(self) -> None:
        """Write all pending records, oldest first, then clear them (no duplicates)."""
        pending, self._pending = self._pending, []
        self._waiting.clear()
        for rec in pending:
            await self.logger.log(
                rec.direction,
                rec.text,
                rec.understood or NO_INTERPRETATION,
                rec.decided or NO_INTERPRETATION,
                rec.message_id,
                rec.created_at,
            )
