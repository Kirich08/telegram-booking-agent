"""Small compatibility helpers for Gemini's OpenAI-compatible Chat Completions API.

Google's compatibility endpoint can return tool calls without OpenAI-style IDs and
requires a function name to be present on tool-result messages in some cases. It can
also reject an empty assistant content field when that assistant message contains
function calls. These helpers normalize those cases while preserving extra response
metadata (including provider-specific fields) when the SDK exposes it.
"""

from __future__ import annotations

import json
from typing import Any


def is_gemini_compatible_url(base_url: str | None) -> bool:
    return bool(base_url and "generativelanguage.googleapis.com" in base_url.lower())


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    dump = getattr(value, "dict", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def prepare_gemini_tool_round(
    assistant_message: Any,
    tool_calls: list[Any],
    round_number: int,
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Return an assistant history message and normalized (id, name, args) calls.

    The complete SDK message/tool-call dictionaries are retained where possible so
    provider-specific response metadata is not silently discarded.
    """
    assistant = _as_dict(assistant_message)
    assistant["role"] = "assistant"

    # Google's endpoint has had compatibility cases where empty content alongside
    # tool_calls causes a 400. Omit the field rather than inventing assistant prose.
    if assistant.get("content") in (None, ""):
        assistant.pop("content", None)

    normalized_calls: list[tuple[str, str, str]] = []
    serialized_calls: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for index, call in enumerate(tool_calls):
        item = _as_dict(call)
        function_obj = getattr(call, "function", None)
        function_data = _as_dict(function_obj) if function_obj is not None else dict(item.get("function") or {})
        name = str(function_data.get("name") or "").strip()
        if not name:
            raise ValueError("Gemini returned a tool call without a function name.")

        call_id = str(item.get("id") or "").strip() or name
        if call_id in used_ids:
            call_id = f"{name}_{round_number}_{index + 1}"
        used_ids.add(call_id)

        arguments = function_data.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        function_data["arguments"] = arguments

        item["id"] = call_id
        item["type"] = "function"
        item["function"] = function_data
        serialized_calls.append(item)
        normalized_calls.append((call_id, name, arguments))

    assistant["tool_calls"] = serialized_calls
    return assistant, normalized_calls


def gemini_tool_result_message(call_id: str, function_name: str, content: str) -> dict[str, str]:
    """Build a named tool result in the standard OpenAI tool-message shape."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": function_name,
        "content": content,
    }
