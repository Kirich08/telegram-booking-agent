"""Tool schemas (what the model may call) and their implementations.

Tools: send_message, record_manager_reply, log_decision, confirm_booking,
end_conversation, get_current_time.

record_manager_reply and end_conversation are additions to the four tools in the
task text. The first lets the model REPORT what it understood, so the program can
verify it against the real message. The second lets a declined conversation end.

The model never gets to decide what is true: confirm_booking builds its message
from the verified BookingState and refuses unless every invariant holds.
Every tool returns a JSON string (errors are returned, never raised, so the model can react).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from booking_state import INTENTS, PARTY_SIZE, Availability, BookingState, Status, mentioned_times, normalize
from decision_logger import TurnLog

ONE_MESSAGE_ERROR = (
    "You have already sent a message in this turn. Wait for the manager's reply before sending another."
)


@dataclass
class ToolContext:
    """Everything a tool needs. `sent_this_turn` is reset by AgentCore at the start of each turn."""

    send_text: Callable[[str], Awaitable[None]]
    turn_log: TurnLog
    booking: BookingState
    sent_this_turn: bool = False


# ---------------------------------------------------------------------------
# JSON schemas sent to OpenAI
# ---------------------------------------------------------------------------

def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "send_message",
        "Send ONE text message to the restaurant manager on Telegram. After sending, wait for the reply.",
        {"text": {"type": "string", "description": "The message, in polite British English."}},
        ["text"],
    ),
    _fn(
        "record_manager_reply",
        "Report what you understood from ONE real manager message. Call it before replying to every manager "
        "message. Evidence fields must be exact quotes copied from that message; the program checks them.",
        {
            "message_id": {"type": "string", "description": "The id shown with the manager's message."},
            "intent": {"type": "string", "enum": list(INTENTS)},
            "evidence": {"type": "string", "description": "Exact quote from the message that supports the intent."},
            "restaurant_name": {"type": "string", "description": "Only if the manager wrote it. Never guess."},
            "restaurant_evidence": {"type": "string", "description": "Exact quote containing the restaurant name."},
        },
        ["message_id", "intent"],
    ),
    _fn(
        "log_decision",
        "Add your interpretation to the decision log for a message that was sent or received. The program "
        "records the real text itself.",
        {
            "direction": {"type": "string", "enum": ["sent", "received"]},
            "message": {"type": "string", "description": "The text of the message."},
            "understood": {"type": "string", "description": "What you understood."},
            "decided": {"type": "string", "description": "What you decided to do."},
        },
        ["direction", "message", "understood", "decided"],
    ),
    _fn(
        "confirm_booking",
        "Send the final confirmation. Allowed only when the program has VERIFIED the manager's agreement and the "
        "restaurant name from real messages and the 17:00 slot has not passed. No arguments are needed: all details "
        "come from verified state. If you pass any, they must match, or the call is refused.",
        {
            "time": {"type": "string"},
            "date": {"type": "string"},
            "party_size": {"type": "integer"},
            "restaurant": {"type": "string"},
        },
        [],
    ),
    _fn(
        "end_conversation",
        "End the conversation after the manager declined or offered a different slot you cannot accept. "
        "Send your thank-you with send_message first.",
        {"reason": {"type": "string"}},
        ["reason"],
    ),
    _fn("get_current_time", "Return the current date and time in Europe/London as an ISO string.", {}, []),
]


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def _transport_send(ctx: ToolContext, text: str) -> None:
    """Send through the transport. Success is logged as sent, failure as send_failed."""
    try:
        await ctx.send_text(text)
    except Exception as exc:
        ctx.turn_log.add("send_failed", text, understood="Telegram send failed", decided=f"{type(exc).__name__}: {exc}")
        raise
    ctx.turn_log.add(ctx.turn_log.sent_direction, text)
    ctx.booking.last_outgoing_text = text
    ctx.sent_this_turn = True


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

async def tool_send_message(ctx: ToolContext, text: str) -> dict[str, Any]:
    """Send one message, unless the conversation is over, the slot has passed, or one was already sent."""
    b = ctx.booking
    text = (text or "").strip()
    if not text:
        return {"error": "Message text is empty."}
    if b.is_terminal:
        return {"error": f"The conversation has ended ({b.status.value}); nothing more can be sent."}
    if b.expire_if_needed():
        return {"error": "17:00 London time has passed; the conversation is closed."}
    if ctx.sent_this_turn:
        return {"error": ONE_MESSAGE_ERROR}
    await _transport_send(ctx, text)
    b.set_status(Status.WAITING_FOR_MANAGER)
    return {"result": "Message sent successfully."}


async def tool_record_manager_reply(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Validate the model's reading of a real manager message and update the verified state."""
    b = ctx.booking
    result = b.record_reply(
        str(args.get("message_id", "")),
        str(args.get("intent", "")),
        str(args.get("evidence", "") or ""),
        str(args.get("restaurant_name", "") or ""),
        str(args.get("restaurant_evidence", "") or ""),
    )
    if b.operator_input_needed:
        import logging
        logging.getLogger(__name__).warning("OPERATOR INPUT NEEDED: the manager asked: %r", b.operator_input_needed)
    return {
        "result": "recorded" if result.recorded else "rejected",
        "problems": result.problems,
        "restaurant_verified": result.restaurant_verified,
        "agreement_verified": result.agreement_verified,
        "availability": result.availability,
        "next_step": result.hint,
    }


async def tool_log_decision(ctx: ToolContext, direction: str, message: str, understood: str, decided: str) -> dict[str, Any]:
    """Attach the model's interpretation to a real logged message (never creates a message)."""
    if direction not in ("sent", "received"):
        return {"error": "direction must be 'sent' or 'received'."}
    ctx.turn_log.annotate(direction, understood, decided, claimed_text=message)
    return {"result": "Decision logged."}


async def tool_confirm_booking(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Send the final confirmation, built from verified state, and only if every invariant holds."""
    b = ctx.booking
    if ctx.sent_this_turn:
        return {"error": ONE_MESSAGE_ERROR}

    ok, reason = b.can_confirm()
    if not ok:
        if b.slot_expired():
            b.expire_if_needed()
        return {"error": f"Cannot confirm: {reason}.", "next_step": b.next_step_hint()}

    # Anything the model passes is only a claim to cross-check, never a source of truth.
    if args.get("time") and mentioned_times(str(args["time"])) != {(17, 0)} and normalize(str(args["time"])) != "17:00":
        return {"error": "Cannot confirm: the time argument differs from 17:00."}
    if args.get("date") and str(args["date"]).strip() != b.target_date.isoformat():
        return {"error": f"Cannot confirm: the date argument differs from {b.target_date.isoformat()}."}
    if args.get("party_size") not in (None, "") and int(args["party_size"]) != PARTY_SIZE:
        return {"error": f"Cannot confirm: the party size argument differs from {PARTY_SIZE}."}
    if args.get("restaurant") and normalize(str(args["restaurant"])) != normalize(b.restaurant_name or ""):
        return {"error": "Cannot confirm: the restaurant argument differs from the name the manager stated."}

    text = b.final_confirmation_text()
    await _transport_send(ctx, text)  # raises if the send fails: the state is then NOT confirmed
    b.confirmed_at = b.clock()
    b.set_status(Status.CONFIRMED, "final confirmation sent")
    return {"result": "Booking confirmed and conversation ended."}


async def tool_end_conversation(ctx: ToolContext, reason: str) -> dict[str, Any]:
    """End the chat, but only after a real decline or an alternative the agent cannot accept."""
    b = ctx.booking
    if b.is_terminal:
        return {"error": f"The conversation has already ended ({b.status.value})."}
    if b.availability not in (Availability.UNAVAILABLE, Availability.ALTERNATIVE):
        return {"error": "The manager has not declined. Continue the conversation instead of ending it."}
    b.end_reason = reason or "manager declined"
    b.set_status(Status.DECLINED, b.end_reason)
    return {"result": f"Conversation ended. Reason: {b.end_reason}."}


def tool_get_current_time(ctx: ToolContext) -> dict[str, Any]:
    """Return the current London time from the same clock the time gate uses."""
    return {"result": ctx.booking.clock().isoformat()}


async def execute_tool(ctx: ToolContext, name: str, arguments_json: str) -> str:
    """Dispatch one tool call from the model. Always returns a JSON string."""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return json.dumps({"error": "Arguments were not valid JSON."})
    if not isinstance(args, dict):
        return json.dumps({"error": "Arguments must be a JSON object."})

    try:
        if name == "send_message":
            result = await tool_send_message(ctx, str(args.get("text", "")))
        elif name == "record_manager_reply":
            result = await tool_record_manager_reply(ctx, args)
        elif name == "log_decision":
            result = await tool_log_decision(
                ctx, str(args.get("direction", "")), str(args.get("message", "")),
                str(args.get("understood", "")), str(args.get("decided", "")),
            )
        elif name == "confirm_booking":
            result = await tool_confirm_booking(ctx, args)
        elif name == "end_conversation":
            result = await tool_end_conversation(ctx, str(args.get("reason", "")))
        elif name == "get_current_time":
            result = tool_get_current_time(ctx)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except (TypeError, ValueError) as exc:
        result = {"error": f"Bad arguments: {exc}"}
    return json.dumps(result, ensure_ascii=False)
