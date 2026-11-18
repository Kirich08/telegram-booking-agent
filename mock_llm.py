"""Offline stand-in for the model provider, used with --mock and in scenario tests.

It has the same shape as AsyncOpenAI for the one method AgentCore calls
(chat.completions.create) and it works through the SAME tools as the real model:
first it calls record_manager_reply (reporting what it understood), then it reads
the program's verdict and chooses send_message / confirm_booking / end_conversation.

What it is NOT: it is not intelligent, and it knows no facts in advance. The restaurant
name is pulled out of the manager's actual text with a regular expression; it is never
passed in from the command line. It is deliberately a little naive (for example it takes
"Yes, we do have a restaurant" for an agreement), because the point of the system is that
booking_state.py catches such mistakes. Use it to test the plumbing and the safeguards,
not the quality of real language understanding. That needs the real model.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from booking_state import _AFFIRM, _ALTERNATIVE, _REFUSAL, normalize
from prompts import FOLLOW_UP_INSTRUCTION, NUDGE_INSTRUCTION, START_INSTRUCTION

OPENING = (
    "Good afternoon. I would like to book a table for two at 17:00 today, please. "
    "Could you kindly let me know whether you have availability?"
)
FOLLOW_UP = (
    "Good afternoon again. I wanted to follow up on my request for a table for two at 17:00 today. "
    "Would that be possible, please?"
)
ASK_NAME = "Could you please confirm the name of the restaurant?"
THANKS_DECLINE = "Thank you very much for letting me know. I appreciate your time and wish you a pleasant evening."
NO_ALTERNATIVE = (
    "Thank you kindly for the suggestion. I am afraid I can only book a table for two at 17:00 today, "
    "so I will not be able to accept another time. I appreciate your help."
)
CLARIFY = "Thank you. To be sure I understand: could you kindly confirm whether you have a table for two at 17:00 today?"
NO_GUEST_DETAILS = (
    "Thank you. I am afraid I do not have that information to hand. "
    "Could the booking for two at 17:00 today be held without it, please?"
)
ASK_AVAILABILITY = "Thank you. Could you kindly confirm whether you have a table for two at 17:00 today?"
INJECTION_REPLY = (
    "I am afraid I am not able to help with that. Could you kindly let me know "
    "whether a table for two at 17:00 today is available?"
)
ACK = "Thank you for your reply. Could you kindly confirm whether a table for two at 17:00 today is available?"

_MANAGER_LINE = re.compile(r"^\[message_id=(?P<id>[^\]]+)\] Manager replied[^:]*: (?P<text>.*?)\n\[verified facts:", re.S)
_INJECTION = re.compile(r"ignore (?:all )?(?:previous|prior)|api key|password|secret|environment variable|system prompt")
_GUEST_DETAILS = re.compile(
    r"name for the (?:reservation|booking|table)|your name|guest name|contact (?:number|details)|phone|telephone|e-?mail"
)
_NAME_AFTER = re.compile(
    # the lead-in words may be in any case; the restaurant name itself must start with a capital
    r"(?i:\bwe(?:'|\u2019)re|\bwe are|\bthis is|\byou(?:'|\u2019)re speaking with|\bwelcome to|\bhere at|"
    r"\byou(?:'|\u2019)ve reached)\s+"
    r"((?:the\s+)?[A-Z][\w'\u2019&\-]*(?:\s+(?:of\s+|&\s+)?[A-Z][\w'\u2019&\-]*){0,4})"
)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _sentence_matching(text: str, pattern: re.Pattern[str]) -> str:
    """The sentence of `text` that matches `pattern` (or the whole text)."""
    return next((s for s in _sentences(text) if pattern.search(normalize(s))), text.strip())


def interpret(text: str) -> dict[str, str]:
    """Naive reading of a manager message: intent, evidence quote, restaurant name and quote."""
    t = normalize(text)
    name = evidence_for_name = ""
    m = _NAME_AFTER.search(text)
    if m:
        name = m.group(1).strip()
        evidence_for_name = next((s for s in _sentences(text) if name in s), text.strip())

    flag = ""
    if _INJECTION.search(t):
        intent, evidence, flag = "other", "", "injection"
    elif _GUEST_DETAILS.search(t):
        intent, evidence = "requests_guest_details", _sentence_matching(text, _GUEST_DETAILS)
    elif _ALTERNATIVE.search(t):
        intent, evidence = "alternative_offered", _sentence_matching(text, _ALTERNATIVE)
    elif _REFUSAL.search(t):
        intent, evidence = "availability_declined", _sentence_matching(text, _REFUSAL)
    elif _AFFIRM.search(t):
        intent, evidence = "availability_confirmed", _sentence_matching(text, _AFFIRM)
    elif name:
        intent, evidence = "restaurant_identified", ""
    elif "?" in text:
        intent, evidence = "clarification_requested", ""
    else:
        intent, evidence = "other", ""
    return {"intent": intent, "evidence": evidence, "restaurant_name": name, "restaurant_evidence": evidence_for_name, "flag": flag}


class MockLLM:
    """Scripted replacement for AsyncOpenAI. Use it only with --mock and in tests."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)
        self._counter = 0

    async def create(self, model: str, messages: list[dict[str, Any]], tools: Any = None, tool_choice: Any = None, **kwargs: Any):
        return self._response(self._decide(messages))

    def _response(self, actions: list[tuple[str, dict[str, Any]]]) -> SimpleNamespace:
        """Build an object shaped like a chat completion with tool calls (unique ids)."""
        calls = []
        for name, args in actions:
            self._counter += 1
            calls.append(SimpleNamespace(id=f"mock_{self._counter}", type="function",
                                         function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=calls or None))])

    # ------------------------------------------------------------------

    def _decide(self, messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        last = messages[-1]
        if last["role"] == "user":
            content = str(last["content"])
            if content == START_INSTRUCTION:
                return _say(OPENING, "Nothing received yet", "Open with a request for a table for two at 17:00 today")
            if content == FOLLOW_UP_INSTRUCTION:
                return _say(FOLLOW_UP, "No reply yet", "Send one polite follow-up")
            if content == NUDGE_INSTRUCTION:
                return _say(ACK, "No clear reading", "Ask again about availability")
            m = _MANAGER_LINE.match(content)
            if m:
                reading = interpret(m.group("text"))
                return [("record_manager_reply", {"message_id": m.group("id"), **{k: v for k, v in reading.items() if v}})]
            return []

        # The previous round was tool calls: decide the next step from the program's verdict.
        assistant = next(msg for msg in reversed(messages) if msg["role"] == "assistant")
        calls = assistant.get("tool_calls") or []
        names = [c["function"]["name"] for c in calls]
        if "record_manager_reply" not in names or any(n in names for n in ("send_message", "confirm_booking", "end_conversation")):
            return []  # already acted: end the turn

        record = next(c for c in calls if c["function"]["name"] == "record_manager_reply")
        reading = json.loads(record["function"]["arguments"])
        verdict = json.loads(next(msg for msg in messages if msg.get("tool_call_id") == record["id"])["content"])
        return self._reply_actions(reading, verdict)

    def _reply_actions(self, reading: dict[str, Any], verdict: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        intent = reading.get("intent", "other")
        understood = f"Manager message read as: {intent}"
        availability = verdict.get("availability")

        if availability == "unavailable":
            return _say(THANKS_DECLINE, understood, "Thank the manager and end the conversation", received=True) + [
                ("end_conversation", {"reason": "manager declined"})
            ]
        if availability == "alternative":
            return _say(NO_ALTERNATIVE, understood, "Do not accept another time; thank and end", received=True) + [
                ("end_conversation", {"reason": "alternative time not accepted"})
            ]
        if verdict.get("agreement_verified") and verdict.get("restaurant_verified"):
            return [
                ("confirm_booking", {}),
                ("log_decision", {"direction": "received", "message": "", "understood": understood, "decided": "Everything verified: confirm the booking"}),
                ("log_decision", {"direction": "sent", "message": "", "understood": understood, "decided": "Sent the final confirmation"}),
            ]
        if verdict.get("agreement_verified"):
            return _say(ASK_NAME, "Manager agreed but has not named the restaurant", "Ask for the restaurant name", received=True)
        if intent == "requests_guest_details":
            return _say(NO_GUEST_DETAILS, understood, "Do not invent details; ask if the booking can be held without them", received=True)
        if reading.get("flag") == "injection":
            return _say(INJECTION_REPLY, "Message contained an instruction unrelated to the booking", "Ignore it; steer back to the booking", received=True)
        if verdict.get("problems") or availability == "ambiguous":
            return _say(CLARIFY, "Agreement was not proven", "Ask one clarifying question", received=True)
        if verdict.get("restaurant_verified"):
            return _say(ASK_AVAILABILITY, "Restaurant named, availability not confirmed", "Ask about availability", received=True)
        return _say(ACK, understood, "Restate the request", received=True)


def _say(message: str, understood: str, decided: str, received: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """send_message plus the decision-log annotations for the message just handled."""
    calls: list[tuple[str, dict[str, Any]]] = [("send_message", {"text": message})]
    if received:
        calls.append(("log_decision", {"direction": "received", "message": "", "understood": understood, "decided": decided}))
    calls.append(("log_decision", {"direction": "sent", "message": message, "understood": understood, "decided": decided}))
    return calls
