"""Pure unit tests for the Gemini/OpenAI-compatible tool-message adapter."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from provider_compat import (
    gemini_tool_result_message,
    is_gemini_compatible_url,
    prepare_gemini_tool_round,
)


class ProviderDetectionTests(unittest.TestCase):
    def test_gemini_endpoint_is_detected(self):
        self.assertTrue(is_gemini_compatible_url("https://generativelanguage.googleapis.com/v1beta/openai/"))

    def test_non_gemini_and_missing_urls_are_not_detected(self):
        self.assertFalse(is_gemini_compatible_url("https://api.openai.com/v1"))
        self.assertFalse(is_gemini_compatible_url(None))


class GeminiToolRoundTests(unittest.TestCase):
    def test_empty_call_id_is_replaced_with_function_name(self):
        message = SimpleNamespace(role="assistant", content=None, provider_metadata={"thought": "keep"})
        calls = [SimpleNamespace(id="", type="function", function=SimpleNamespace(name="send_message", arguments='{"text":"Hello"}'))]
        assistant, normalized = prepare_gemini_tool_round(message, calls, 0)
        self.assertEqual(assistant["tool_calls"][0]["id"], "send_message")
        self.assertEqual(normalized, [("send_message", "send_message", '{"text":"Hello"}')])
        self.assertNotIn("content", assistant)

    def test_duplicate_missing_ids_get_unique_fallbacks(self):
        message = SimpleNamespace(content="calling tools")
        calls = [
            SimpleNamespace(id="", function=SimpleNamespace(name="log_decision", arguments="{}")),
            SimpleNamespace(id="", function=SimpleNamespace(name="log_decision", arguments="{}")),
        ]
        _, normalized = prepare_gemini_tool_round(message, calls, 3)
        self.assertEqual(normalized[0][0], "log_decision")
        self.assertEqual(normalized[1][0], "log_decision_3_2")

    def test_sdk_model_dump_metadata_is_preserved(self):
        class SDKMessage:
            def model_dump(self, exclude_none=True):
                return {"role": "assistant", "content": None, "thought_signature": "opaque-signature"}

        call = {"id": "provider-id-1", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}}
        assistant, normalized = prepare_gemini_tool_round(SDKMessage(), [call], 0)
        self.assertEqual(assistant["thought_signature"], "opaque-signature")
        self.assertEqual(normalized[0][0], "provider-id-1")

    def test_tool_result_includes_matching_id_function_name_and_content(self):
        result = gemini_tool_result_message("probe_tool", "probe_tool", '{"ok":true}')
        self.assertEqual(result, {
            "role": "tool",
            "tool_call_id": "probe_tool",
            "name": "probe_tool",
            "content": '{"ok":true}',
        })


if __name__ == "__main__":
    unittest.main()
