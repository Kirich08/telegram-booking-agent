"""Check that the configured Gemini key supports a multi-turn tool round-trip.

This makes two small model requests and does not connect to Telegram or send messages.
Run it after setting GEMINI_API_KEY in .env:
    python check_provider.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from openai import AsyncOpenAI, APIError

from config import ConfigError, load_config
from provider_compat import (
    gemini_tool_result_message,
    is_gemini_compatible_url,
    prepare_gemini_tool_round,
)


PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "provider_probe",
        "description": "Return a harmless test result. This is only a provider connectivity check.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}]


async def main() -> int:
    try:
        cfg = load_config(require_telegram=False, require_openai=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not is_gemini_compatible_url(cfg.openai_base_url):
        print("Gemini endpoint is not configured. Set GEMINI_API_KEY (or GEMINI_BASE_URL).", file=sys.stderr)
        return 2

    client = AsyncOpenAI(
        api_key=cfg.openai_api_key,
        base_url=cfg.openai_base_url,
        timeout=30.0,
        max_retries=0,
    )
    messages = [
        {"role": "system", "content": "This is a technical API test. Follow the user's instruction exactly."},
        {"role": "user", "content": "Call provider_probe exactly once with value set to 'round_trip_test'. Do not answer in plain text."},
    ]

    try:
        first = await client.chat.completions.create(
            model=cfg.openai_model,
            messages=messages,
            tools=PROBE_TOOL,
            tool_choice="auto",
        )
        assistant_message = first.choices[0].message
        calls = assistant_message.tool_calls or []
        if not calls:
            print("FAIL: the model returned no function call.")
            print(f"Model response: {assistant_message.content!r}")
            return 1

        assistant_entry, normalized = prepare_gemini_tool_round(assistant_message, calls, 0)
        messages.append(assistant_entry)
        for call_id, function_name, arguments in normalized:
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                parsed = {}
            if function_name != "provider_probe" or parsed.get("value") != "round_trip_test":
                print(f"FAIL: unexpected probe call: {function_name} {parsed!r}")
                return 1
            messages.append(gemini_tool_result_message(
                call_id,
                function_name,
                json.dumps({"ok": True, "received": parsed["value"]}),
            ))

        messages.append({
            "role": "user",
            "content": "The test function has completed successfully. Reply with exactly PROVIDER_ROUND_TRIP_OK.",
        })
        second = await client.chat.completions.create(
            model=cfg.openai_model,
            messages=messages,
        )
        final_text = (second.choices[0].message.content or "").strip()
        if "PROVIDER_ROUND_TRIP_OK" not in final_text:
            print("FAIL: the second request completed, but the final marker was not returned.")
            print(f"Model response: {final_text!r}")
            return 1

        print(f"PASS: {cfg.openai_model} completed a multi-turn tool-call round-trip.")
        print("Telegram was not contacted and no booking message was sent.")
        return 0
    except APIError as exc:
        print("FAIL: Gemini API request failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
