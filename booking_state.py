"""Verified booking state: the part of the system that decides what is TRUE.

Golden rule of this project: the model may interpret the conversation and suggest
the next action, but this code decides whether a critical action is allowed.

Facts such as "the restaurant is The Ivy" or "the manager agreed" only enter this
state through validators that check them against the REAL text of a REAL incoming
manager message. The model's own claims are never trusted on their own.

Limits (stated honestly): the validators are conservative text checks, not language
understanding. When a message is unclear they refuse and the agent asks a question.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Callable

from utils import LONDON, get_current_time_london

logger = logging.getLogger(__name__)

REQUESTED_TIME = time(17, 0)
PARTY_SIZE = 2


class Status(str, Enum):
    """Conversation lifecycle. The last six are terminal (final)."""

    NEW = "NEW"
    PROCESSING = "PROCESSING"
    WAITING_FOR_MANAGER = "WAITING_FOR_MANAGER"
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    NO_RESPONSE = "NO_RESPONSE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset(
    {Status.CONFIRMED, Status.DECLINED, Status.EXPIRED, Status.NO_RESPONSE, Status.FAILED, Status.CANCELLED}
)


class Availability(str, Enum):
    """What the manager has said about the requested slot."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ALTERNATIVE = "alternative"
    AMBIGUOUS = "ambiguous"


INTENTS = (
    "availability_confirmed",
    "availability_declined",
    "alternative_offered",
    "restaurant_identified",
    "requests_guest_details",
    "clarification_requested",
    "ambiguous",
    "other",
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lower-case, unify quotes and spaces, so quotes can be compared reliably."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def contains_quote(message_text: str, quote: str) -> bool:
    """True if `quote` really occurs inside `message_text` (after normalising)."""
    q = normalize(quote)
    return len(q) >= 3 and q in normalize(message_text)


_GENERIC_NAMES = {
    "restaurant", "the restaurant", "our restaurant", "your restaurant", "a restaurant",
    "us", "we", "the manager", "manager", "the team", "reception", "unknown", "n/a", "none",
}

_REFUSAL = re.compile(
    r"fully booked|fully-booked|no (?:tables?|availability|space|room)|not available|unavailable|"
    r"unfortunately|\bcan'?t\b|\bcannot\b|\bcan not\b|\bunable\b|sold out|no longer|"
    r"won'?t be able|(?:do(?:es)? not|don'?t|doesn'?t) have|\bfull\b|\bbooked up\b"
)
_ALTERNATIVE = re.compile(r"\binstead\b|alternativ|how about|we can offer|could offer|can offer|would .{0,25} work|\bor later\b|\bor earlier\b")
_OTHER_DAY = re.compile(r"\btomorrow\b|\bnext (?:week|day|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\bday after\b")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_AFFIRM = re.compile(
    r"\b(?:yes|yeah|yep|sure|certainly|of course|absolutely|definitely|no problem|that works|works for us|"
    r"available|we have|we can|we do|confirmed|booked|reserved|happy to|pleasure|that(?:'s| is) fine)\b"
)
_SLOT_CUE = re.compile(
    r"\b(?:table|tables|available|availability|accommodate|space|book(?:ed|ing)?|reserv\w*)\b|"
    r"\b5\s*(?:pm|p\.m\.)|\b17[:.]00\b|\b5:00\b|\bfor (?:two|2)\b|\b(?:two|2) (?:guests|people|persons|of you)\b"
)
_TIME_AMPM = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_TIME_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_OCLOCK = re.compile(r"\b(\d{1,2})\s*o'?clock\b")
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_PARTY = re.compile(
    r"\bfor (\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b(?! ?(?:o'?clock|pm|p\.m\.|am|a\.m\.|:))|"
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten) (?:people|guests|persons|covers|diners)\b"
)


def mentioned_times(text: str) -> set[tuple[int, int]]:
    """All clock times mentioned in a text, as 24-hour (hour, minute) pairs."""
    t = normalize(text)
    found: set[tuple[int, int]] = set()
    for hour, minute, mer in _TIME_AMPM.findall(t):
        h = int(hour) % 12 + (12 if mer.startswith("p") else 0)
        found.add((h, int(minute or 0)))
    spans = [m.span() for m in _TIME_AMPM.finditer(t)]
    for m in _TIME_24.finditer(t):
        if not any(s <= m.start() < e for s, e in spans):
            found.add((int(m.group(1)), int(m.group(2))))
    for m in _OCLOCK.finditer(t):
        found.add((int(m.group(1)) + (12 if int(m.group(1)) < 12 else 0), 0))
    return found


def mentioned_party_sizes(text: str) -> set[int]:
    """Party sizes mentioned in a text ('for four', '3 guests')."""
    sizes: set[int] = set()
    for m in _PARTY.finditer(normalize(text)):
        raw = m.group(1) or m.group(2)
        sizes.add(int(raw) if raw.isdigit() else _NUM_WORDS[raw])
    return sizes


@dataclass
class IncomingMessage:
    """One real message from the manager, as received."""

    id: str
    text: str
    received_at: datetime


@dataclass
class ReplyResult:
    """What recording an interpretation achieved."""

    recorded: bool
    problems: list[str] = field(default_factory=list)
    restaurant_verified: str | None = None
    agreement_verified: bool = False
    availability: str = Availability.UNKNOWN.value
    hint: str = ""


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------

@dataclass
class BookingState:
    """Everything that is known and VERIFIED about this booking attempt."""

    clock: Callable[[], datetime] = get_current_time_london
    status: Status = Status.NEW
    availability: Availability = Availability.UNKNOWN

    restaurant_name: str | None = None
    restaurant_source_id: str | None = None
    restaurant_evidence: str | None = None

    manager_agreed: bool = False
    agreement_source_id: str | None = None
    agreement_evidence: str | None = None
    alternative_evidence: str | None = None

    operator_input_needed: str | None = None
    end_reason: str | None = None
    failed_reason: str | None = None
    last_outgoing_text: str = ""
    confirmed_at: datetime | None = None

    incoming: dict[str, IncomingMessage] = field(default_factory=dict)
    transitions: list[tuple[datetime, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The target day is fixed when the attempt begins: if the clock passes
        # midnight, this is NOT silently turned into a booking for another day.
        self.target_date = self.clock().astimezone(LONDON).date()

    # -- time -------------------------------------------------------------

    def target_datetime(self) -> datetime:
        """17:00 on the target day, as an aware Europe/London datetime."""
        return datetime.combine(self.target_date, REQUESTED_TIME, tzinfo=LONDON)

    def slot_expired(self) -> bool:
        """True once 17:00 has been reached (at 17:00:00 the slot is already blocked)."""
        return self.clock().astimezone(LONDON) >= self.target_datetime()

    def expire_if_needed(self) -> bool:
        """Move to EXPIRED if the slot has passed and nothing final happened yet."""
        if self.is_terminal or not self.slot_expired():
            return self.status is Status.EXPIRED
        self.set_status(Status.EXPIRED, "17:00 London time has already passed")
        return True

    # -- status -----------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def set_status(self, new: Status, reason: str = "") -> bool:
        """Change status. A terminal status can never be left. Every change is logged."""
        if self.is_terminal and new is not self.status:
            logger.debug("Ignored status change %s -> %s (already terminal)", self.status.value, new.value)
            return False
        if new is self.status:
            return True
        logger.info("State: %s -> %s%s", self.status.value, new.value, f" ({reason})" if reason else "")
        self.transitions.append((self.clock(), new.value, reason))
        self.status = new
        if new is Status.FAILED:
            self.failed_reason = reason
        return True

    # -- incoming messages ------------------------------------------------

    def record_incoming(self, message_id: str, text: str) -> bool:
        """Remember a real manager message. Returns False for a duplicate id."""
        if message_id in self.incoming:
            return False
        self.incoming[message_id] = IncomingMessage(message_id, text, self.clock())
        return True

    def last_outgoing_mentions_slot(self) -> bool:
        """True if the agent's last real message stated the slot (17:00 and two)."""
        t = normalize(self.last_outgoing_text)
        return bool(re.search(r"17[:.]00|\b5\s*(?:pm|p\.m\.)", t)) and bool(re.search(r"\b(?:two|2)\b", t))

    # -- validators -------------------------------------------------------

    def agreement_problem(self, text: str, evidence: str) -> str | None:
        """Why this evidence does NOT prove agreement to the exact slot, or None if it does."""
        if not contains_quote(text, evidence):
            return "the evidence quote is not in the manager's message"
        t, ev = normalize(text), normalize(evidence)
        if _REFUSAL.search(t):
            return "the message contains a refusal or limitation"
        if _ALTERNATIVE.search(t):
            return "the message offers an alternative"
        times = mentioned_times(text)
        if any(h_m != (REQUESTED_TIME.hour, REQUESTED_TIME.minute) for h_m in times):
            return "the message mentions a time other than 17:00"
        if any(size != PARTY_SIZE for size in mentioned_party_sizes(text)):
            return "the message mentions a party size other than two"
        if _OTHER_DAY.search(t):
            return "the message refers to a day other than today"
        target_day = _WEEKDAYS[self.target_date.weekday()]
        if any(day in t and day != target_day for day in _WEEKDAYS):
            return "the message refers to a day other than today"
        if not _AFFIRM.search(ev):
            return "the quote contains no affirmation"
        if _SLOT_CUE.search(ev):
            return None
        # A bare "Yes, that is fine." only counts as an answer to a request that
        # stated the exact slot, and only if it is short and not itself a question.
        if self.last_outgoing_mentions_slot() and len(t.split()) <= 12 and "?" not in t:
            return None
        return "the affirmation does not refer to a table, the time, or the party"

    @staticmethod
    def restaurant_problem(text: str, name: str, evidence: str) -> str | None:
        """Why this restaurant name is NOT proven by the manager's message, or None."""
        name = (name or "").strip()
        if not name or normalize(name) in _GENERIC_NAMES or not re.search(r"[A-Za-z\u00C0-\u024F]", name) or len(name) > 80:
            return "the restaurant name is empty or generic"
        if not contains_quote(text, evidence):
            return "the restaurant evidence quote is not in the manager's message"
        if normalize(name) not in normalize(evidence):
            return "the restaurant name is not inside the evidence quote"
        return None

    # -- recording the model's interpretation ------------------------------

    def record_reply(
        self,
        message_id: str,
        intent: str,
        evidence: str = "",
        restaurant_name: str = "",
        restaurant_evidence: str = "",
    ) -> ReplyResult:
        """Validate the model's reading of ONE real manager message and update the state."""
        msg = self.incoming.get(message_id)
        if msg is None:
            return ReplyResult(False, [f"unknown message_id {message_id!r}: use the id shown with the manager's message"])
        if intent not in INTENTS:
            return ReplyResult(False, [f"unknown intent {intent!r}"])

        problems: list[str] = []
        self.operator_input_needed = None  # re-set below only if THIS message asks for guest details

        if restaurant_name:
            problem = self.restaurant_problem(msg.text, restaurant_name, restaurant_evidence)
            if problem:
                problems.append(f"restaurant name rejected: {problem}")
            elif self.restaurant_name and normalize(self.restaurant_name) != normalize(restaurant_name):
                problems.append("restaurant name conflicts with the name verified earlier; ask the manager to clarify")
            elif not self.restaurant_name:
                self.restaurant_name = restaurant_name.strip()
                self.restaurant_source_id = message_id
                self.restaurant_evidence = restaurant_evidence.strip()

        if intent in ("availability_confirmed", "availability_declined", "alternative_offered", "requests_guest_details"):
            if not contains_quote(msg.text, evidence):
                problems.append("evidence quote is not in the manager's message")
            elif intent == "availability_confirmed":
                problem = self.agreement_problem(msg.text, evidence)
                if problem:
                    problems.append(f"agreement not proven: {problem}")
                    self.availability = Availability.AMBIGUOUS
                    self.manager_agreed = False
                else:
                    self.availability = Availability.AVAILABLE
                    self.manager_agreed = True
                    self.agreement_source_id = message_id
                    self.agreement_evidence = evidence.strip()
            elif intent == "availability_declined":
                self.availability = Availability.UNAVAILABLE
                self.manager_agreed = False
            elif intent == "alternative_offered":
                self.availability = Availability.ALTERNATIVE
                self.manager_agreed = False
                self.alternative_evidence = evidence.strip()
            elif intent == "requests_guest_details":
                self.operator_input_needed = evidence.strip()
        elif intent == "ambiguous":
            self.availability = Availability.AMBIGUOUS

        return ReplyResult(
            recorded=True,
            problems=problems,
            restaurant_verified=self.restaurant_name,
            agreement_verified=self.manager_agreed,
            availability=self.availability.value,
            hint=self.next_step_hint(problems),
        )

    def next_step_hint(self, problems: list[str] | None = None) -> str:
        """Plain-language guidance for the model after a recording."""
        if self.availability is Availability.UNAVAILABLE:
            return "The manager declined. Thank them politely with send_message, then call end_conversation."
        if self.availability is Availability.ALTERNATIVE:
            return ("The manager offered a different slot. You may not accept it. Thank them, explain that you can only "
                    "book 17:00 today, then call end_conversation.")
        if self.operator_input_needed:
            return ("The manager wants guest details you do not have. Do not invent them and do not promise to send them "
                    "later. Say you do not have them and ask whether the booking can be held without them.")
        if problems:
            return "Do not confirm. Ask ONE short clarifying question that restates: a table for two at 17:00 today."
        if self.manager_agreed and self.restaurant_name:
            return "Everything is verified. Call confirm_booking now (no details are needed)."
        if self.manager_agreed:
            return "The manager agreed but has not named the restaurant. Ask: Could you please confirm the name of the restaurant?"
        if self.restaurant_name:
            return "The restaurant is known but availability is not confirmed. Ask about a table for two at 17:00 today."
        return "Nothing is confirmed yet. Reply politely and restate the request: a table for two at 17:00 today."

    # -- confirmation -----------------------------------------------------

    def can_confirm(self) -> tuple[bool, str]:
        """May the final confirmation be sent right now? (ok, reason if not)"""
        if self.is_terminal:
            return False, f"the conversation has already ended ({self.status.value})"
        if self.slot_expired():
            return False, "17:00 London time has already passed"
        if not self.restaurant_name:
            return False, "the restaurant name has not been stated by the manager"
        if not self.manager_agreed or self.availability is not Availability.AVAILABLE:
            return False, "the manager has not clearly agreed to a table for two at 17:00 today"
        return True, ""

    def final_confirmation_text(self) -> str:
        """The final message, built ONLY from verified state."""
        d = self.target_datetime()
        return (
            f"Thank you very much for confirming. To repeat the booking: a table for two today, "
            f"{d:%A}, {d.day} {d:%B %Y}, at 17:00 (London time) at {self.restaurant_name}. "
            "Many thanks for your help."
        )

    def summary(self) -> str:
        """One line of verified facts, shown to the model so it never relies on old history."""
        return (
            f"restaurant_verified={self.restaurant_name or 'NO'}; agreement_verified={'YES' if self.manager_agreed else 'NO'}; "
            f"availability={self.availability.value}; slot_expired={'YES' if self.slot_expired() else 'NO'}"
        )
