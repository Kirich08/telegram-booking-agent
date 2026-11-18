"""Core agent logic: runs the conversation turn by turn.

The model decides what to say; this module and booking_state.py decide what is allowed.

Liveness rule: an active conversation always has a way forward. After every turn it is
either (a) waiting for the manager with a follow-up timer running, or (b) in a terminal
status with an explained reason. It never stays "active" doing nothing:
  - no tool call from the model  -> the model is reminded (bounded), then FAILED;
  - temporary API error          -> bounded retries with back-off, then FAILED;
  - wrong key / no credits       -> FAILED immediately with a clear reason;
  - tool loop too long           -> treated like "no action";
  - manager silent               -> follow-ups, then NO_RESPONSE.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from openai import APIError

from booking_state import Status
from prompts import (
    FOLLOW_UP_INSTRUCTION,
    FULL_SYSTEM_PROMPT,
    NUDGE_INSTRUCTION,
    START_INSTRUCTION,
    format_manager_message,
)
from tools_registry import TOOL_SCHEMAS, ToolContext, execute_tool
from provider_compat import (
    gemini_tool_result_message,
    is_gemini_compatible_url,
    prepare_gemini_tool_round,
)

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ROUNDS = 6
RETRY_DELAYS = (1.0, 2.0, 4.0)  # seconds between attempts after a temporary API error
MAX_NUDGES = 2  # how many times a silent model is reminded before the turn fails


class ModelError(Exception):
    """The model API cannot be used: a fatal error, or temporary errors that did not go away."""


class AgentCore:
    """Runs the booking conversation with one manager."""

    def __init__(
        self,
        client: Any,
        model: str,
        ctx: ToolContext,
        follow_up_minutes: float = 15,
        max_follow_ups: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        max_nudges: int = MAX_NUDGES,
        provider_compat: str | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.ctx = ctx
        self.follow_up_minutes = follow_up_minutes
        self.max_follow_ups = max_follow_ups
        self._sleep = sleep  # used for retry back-off (tests pass a fast fake)
        self.retry_delays = retry_delays
        self.max_nudges = max_nudges
        detected_gemini = is_gemini_compatible_url(str(getattr(client, "base_url", "")))
        self.provider_compat = provider_compat or ("gemini" if detected_gemini else "openai")

        self.history: list[dict[str, Any]] = []
        self._turn_lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None
        self._follow_ups_sent = 0
        self._replies_received = 0

    # ------------------------------------------------------------------
    # State shortcuts
    # ------------------------------------------------------------------

    @property
    def booking(self):
        return self.ctx.booking

    @property
    def status(self) -> Status:
        return self.ctx.booking.status

    @property
    def conversation_active(self) -> bool:
        """True until the state reaches a terminal status."""
        return not self.ctx.booking.is_terminal

    @property
    def failed_reason(self) -> str | None:
        return self.ctx.booking.failed_reason

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_conversation(self) -> None:
        """Let the model write the opening message. Refuses if 17:00 has already passed."""
        await self._run_turn(user_text=START_INSTRUCTION)

    async def process_incoming_message(self, text: str, message_id: str | None = None) -> None:
        """Handle one real message from the manager."""
        b = self.booking
        text = (text or "").strip()
        if not text:
            return
        mid = message_id or f"local-{len(b.incoming) + 1}"
        if not b.record_incoming(mid, text):
            logger.info("Duplicate message %s ignored.", mid)
            return
        if b.is_terminal:
            logger.info("Conversation already ended (%s); message %s ignored.", b.status.value, mid)
            await self.ctx.turn_log.logger.log(
                "received", text, "", f"(ignored: the conversation had already ended: {b.status.value})", mid
            )
            return

        self._cancel_timer()
        self._replies_received += 1
        self._follow_ups_sent = 0
        await self._run_turn(incoming=(mid, text))

    async def shutdown(self) -> None:
        """Cancel timers and close the conversation if it is still open."""
        self._cancel_timer()
        if not self.booking.is_terminal:
            self.booking.set_status(Status.CANCELLED, "stopped by the operator")

    # ------------------------------------------------------------------
    # One turn
    # ------------------------------------------------------------------

    async def _run_turn(self, user_text: str | None = None, incoming: tuple[str, str] | None = None) -> None:
        """Run one model turn under a lock; always write the log and decide what happens next."""
        async with self._turn_lock:
            b = self.booking
            self.ctx.sent_this_turn = False
            if incoming is not None:
                # Transport fact: the real message is logged by the program, exactly as received.
                self.ctx.turn_log.add("received", incoming[1], incoming[0])
            try:
                if not b.is_terminal and not b.expire_if_needed():
                    if incoming is not None:
                        user_text = format_manager_message(incoming[1], incoming[0], b.summary())
                    b.set_status(Status.PROCESSING)
                    self.history.append({"role": "user", "content": user_text})
                    await self._drive_model()
                elif b.status is Status.EXPIRED:
                    logger.error("17:00 London time has passed; no message is sent.")
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}")
                raise
            finally:
                await self.ctx.turn_log.flush()
                self._trim_history()
                self._after_turn()

    async def _drive_model(self) -> None:
        """Make the model take a valid action, or end the turn in a clear state."""
        nudges = 0
        while True:
            try:
                await self._llm_loop()
            except ModelError as exc:
                if self.ctx.sent_this_turn or self.booking.is_terminal:
                    logger.warning("Model problem after the action was already done: %s", exc)
                else:
                    self._fail(str(exc))
                return

            if self.booking.is_terminal or self.ctx.sent_this_turn:
                return
            if nudges >= self.max_nudges:
                self._fail(f"The model took no action after {nudges} reminders.")
                return
            nudges += 1
            logger.warning("The model took no action; reminding it (%d/%d).", nudges, self.max_nudges)
            self.history.append({"role": "user", "content": NUDGE_INSTRUCTION})

    async def _llm_loop(self) -> None:
        """Call the model and run its tool calls until it stops calling tools."""
        for round_number in range(MAX_TOOL_ROUNDS):
            response = await self._call_model()
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            if self.provider_compat == "gemini":
                if not tool_calls:
                    # Do not put an empty assistant message into future Gemini history:
                    # the compatibility endpoint may reject empty content on replay.
                    if message.content:
                        self.history.append({"role": "assistant", "content": message.content})
                        logger.warning("Model returned text without a tool call; it is not sent.")
                    else:
                        logger.warning("Model returned an empty response without a tool call.")
                    return

                # Preserve the complete provider response (including any extra metadata),
                # repair missing function-call IDs, and attach names to tool results.
                entry, normalized_calls = prepare_gemini_tool_round(message, tool_calls, round_number)
                self.history.append(entry)
                for call_id, function_name, arguments_json in normalized_calls:
                    result = await self.execute_tool(function_name, arguments_json)
                    self.history.append(gemini_tool_result_message(call_id, function_name, result))
            else:
                entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if tool_calls:
                    entry["tool_calls"] = [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ]
                self.history.append(entry)

                if not tool_calls:
                    # Plain text is never sent: every message must go through send_message.
                    if message.content:
                        logger.warning("Model returned text without a tool call; it is not sent.")
                    return

                for tc in tool_calls:
                    result = await self.execute_tool(tc.function.name, tc.function.arguments)
                    self.history.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            if self.booking.is_terminal:
                return
        logger.warning("Stopped after %d tool rounds.", MAX_TOOL_ROUNDS)

    async def execute_tool(self, tool_name: str, arguments_json: str) -> str:
        """Run a tool through the registry and return its JSON result."""
        return await execute_tool(self.ctx, tool_name, arguments_json)

    # ------------------------------------------------------------------
    # Model calls with bounded retries
    # ------------------------------------------------------------------

    async def _call_model(self) -> Any:
        """One chat-completion call. Retries temporary errors with back-off; fails on fatal ones."""
        messages = [{"role": "system", "content": FULL_SYSTEM_PROMPT}] + self.history
        for attempt in range(len(self.retry_delays) + 1):
            try:
                return await self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=TOOL_SCHEMAS, tool_choice="auto"
                )
            except APIError as exc:
                hint, fatal = self._classify(exc)
                logger.error("Model API error: %s | %s", hint, exc)
                if fatal:
                    raise ModelError(hint) from exc
                if attempt >= len(self.retry_delays):
                    raise ModelError(f"Temporary model errors did not go away after {attempt} retries: {exc}") from exc
                delay = self.retry_delays[attempt]
                logger.warning("Retrying in %.1f s (retry %d of %d).", delay, attempt + 1, len(self.retry_delays))
                await self._sleep(delay)
        raise ModelError("unreachable")  # pragma: no cover

    @staticmethod
    def _classify(exc: APIError) -> tuple[str, bool]:
        """Return (human hint, is_fatal) for a model API error."""
        text = str(exc)
        status = getattr(exc, "status_code", None)
        if "insufficient_quota" in text or "no credits" in text.lower():
            return "The model provider reports no credits remaining or that the free quota is exhausted. Check its usage limits, or use another compatible provider.", True
        if status == 401:
            return "The model-provider API key was rejected. Check the key in your .env file.", True
        if status == 403:
            return "The API key does not have permission to use this model or endpoint. Check provider access settings.", True
        if status is not None and 400 <= status < 500 and status not in (408, 409, 429):
            return f"The model-provider request was rejected (HTTP {status}). Check the model ID, API base URL, and tool compatibility. Provider details: {text[:500]}", True
        return "Temporary model error.", False

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """Keep the last MAX_HISTORY_MESSAGES messages, cutting only at a user message.

        This runs between turns, so a tool call is never separated from its result.
        Business facts do not live here: they are in BookingState.
        """
        if len(self.history) <= MAX_HISTORY_MESSAGES:
            return
        cut = len(self.history) - MAX_HISTORY_MESSAGES
        while cut < len(self.history) and self.history[cut]["role"] != "user":
            cut += 1
        self.history = self.history[cut:]

    # ------------------------------------------------------------------
    # After a turn, and the timer
    # ------------------------------------------------------------------

    def _fail(self, reason: str) -> None:
        """End the conversation as FAILED, with the reason kept for the operator."""
        logger.error("Conversation failed: %s", reason)
        self.booking.set_status(Status.FAILED, reason)

    def _after_turn(self) -> None:
        """Make sure the conversation has a way forward: a timer if it is waiting, nothing if it ended."""
        if self.booking.status is Status.WAITING_FOR_MANAGER:
            self._schedule_timer()
        else:
            self._cancel_timer()

    def _schedule_timer(self) -> None:
        """Start the timer that follows up, or closes the chat after the last follow-up."""
        self._cancel_timer()
        give_up = self._follow_ups_sent >= self.max_follow_ups
        self._timer_task = asyncio.create_task(self._timer(self._replies_received, give_up))

    def _cancel_timer(self) -> None:
        """Cancel the pending timer, unless it is the task we are running in."""
        task = self._timer_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._timer_task = None

    async def _timer(self, replies_at_schedule: int, give_up: bool) -> None:
        """Sleep, then send a follow-up or close the conversation."""
        try:
            await asyncio.sleep(self.follow_up_minutes * 60)
        except asyncio.CancelledError:
            return

        b = self.booking
        if b.is_terminal or self._replies_received != replies_at_schedule:
            return  # finished, or the manager replied: this timer is stale
        if b.expire_if_needed():
            logger.error("17:00 London time passed while waiting; the conversation is closed.")
            self._cancel_timer()
            return
        if give_up:
            b.set_status(Status.NO_RESPONSE, f"no reply after {self._follow_ups_sent} follow-up(s)")
            self._cancel_timer()  # nothing is pending any more; drop the reference to this finished task
            return

        self._follow_ups_sent += 1
        logger.info("No reply after %s min; follow-up %d of %d.", self.follow_up_minutes, self._follow_ups_sent, self.max_follow_ups)
        try:
            await self._run_turn(user_text=FOLLOW_UP_INSTRUCTION)
        except Exception:
            # Nobody is waiting on this background task; _run_turn has already set FAILED.
            logger.exception("Follow-up failed.")
