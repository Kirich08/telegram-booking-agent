"""Shared helpers for the tests: a fake clock, a scripted model, and an agent harness."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_core import AgentCore
from booking_state import BookingState
from decision_logger import DecisionLogger, TurnLog
from tools_registry import ToolContext
from utils import LONDON

OPENING_TEXT = "Good afternoon. I would like to book a table for two at 17:00 today, please."


class FakeClock:
    """A controllable London clock (aware datetimes). Never touches the machine's real time."""

    def __init__(self, hour: int = 12, minute: int = 0, second: int = 0, year: int = 2026, month: int = 11, day: int = 18) -> None:
        self.now = datetime(year, month, day, hour, minute, second, tzinfo=LONDON)

    def __call__(self) -> datetime:
        return self.now

    def set(self, hour: int, minute: int = 0, second: int = 0) -> None:
        self.now = self.now.replace(hour=hour, minute=minute, second=second)


def _tool_call(i: int, name: str, args: Any) -> SimpleNamespace:
    import json
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=f"f{i}", type="function", function=SimpleNamespace(name=name, arguments=raw))


class FakeModel:
    """Replays a queue of model responses, one per chat.completions.create call.

    Each queue item is: a list of (tool_name, args) tuples; an Exception to raise;
    or a str (plain text with no tool call). An empty queue yields "no tool calls".
    NOTE: after a round of tool calls AgentCore calls the model again, so every turn
    normally ends with an empty round `[]`. Use `turn(...)` to add it.
    """

    def __init__(self, *items: Any) -> None:
        self.queue: list[Any] = list(items)
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0
        self._n = 0

    @staticmethod
    def turn(*rounds: Any) -> list[Any]:
        """The rounds of one turn followed by the empty round that ends it."""
        return [*rounds, []]

    async def create(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        item = self.queue.pop(0) if self.queue else []
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item, tool_calls=None))])
        calls = []
        for name, args in item:
            self._n += 1
            calls.append(_tool_call(self._n, name, args))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=calls or None))])


async def no_sleep(_seconds: float) -> None:
    """Fast stand-in for the retry back-off delay."""


class AgentTestCase(unittest.IsolatedAsyncioTestCase):
    """Builds an agent around a model, a fake clock, a fake transport and a temp decision log."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self._tmp.name) / "decisions.log"
        self.sent: list[str] = []
        self.clock = FakeClock()
        self._agents: list[AgentCore] = []

    async def asyncTearDown(self) -> None:
        for agent in self._agents:
            agent._cancel_timer()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_agent(self, model: Any, follow_up_minutes: float = 60, max_follow_ups: int = 2, send_error: Exception | None = None,
                   sent_direction: str = "sent") -> AgentCore:
        self.send_error = send_error  # can be changed by a test while the agent runs

        async def send(text: str) -> None:
            if self.send_error is not None:
                raise self.send_error
            self.sent.append(text)

        self.booking = BookingState(clock=self.clock)
        self.turn_log = TurnLog(DecisionLogger(self.log_path), sent_direction=sent_direction)
        self.ctx = ToolContext(send_text=send, turn_log=self.turn_log, booking=self.booking)
        agent = AgentCore(model, "test-model", self.ctx, follow_up_minutes=follow_up_minutes, max_follow_ups=max_follow_ups,
                          sleep=no_sleep, retry_delays=(0, 0, 0))
        self._agents.append(agent)
        return agent

    def rows(self) -> list[dict[str, str]]:
        """The decision log as a list of dicts (header row excluded)."""
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:]]

    async def settle(self) -> None:
        """Let background tasks (timers) run."""
        for _ in range(5):
            await asyncio.sleep(0)
