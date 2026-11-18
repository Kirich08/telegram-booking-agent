"""Infrastructure tests: config, prompt sync, decision log, Telegram adapter (fake client), run.py as a process.

    python -m unittest test_infra -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.errors import ChatWriteForbiddenError, FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

import prompts
import telegram_client
from config import ConfigError, load_config
from decision_logger import HEADER, DecisionLogger, escape_field
from run import make_clock
from telegram_client import TelegramService, TelegramServiceError
from tools_registry import TOOL_SCHEMAS

ROOT = Path(__file__).resolve().parent
KEYS = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE", "MANAGER_USERNAME", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "GEMINI_MODEL", "GEMINI_BASE_URL", "TEST_MANAGER_USERNAME", "FOLLOW_UP_MINUTES", "MAX_FOLLOW_UPS",
        "ENABLE_VOICE", "OPENAI_BASE_URL", "OPENAI_MODEL"]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(extra)
    return env


class ConfigTests(unittest.TestCase):
    def load(self, **env: str):
        with patch.dict(os.environ, clean_env(**env), clear=True):
            return load_config(env_file="/nonexistent.env", require_telegram=False, require_openai=False)

    def test_defaults(self):
        cfg = self.load()
        self.assertEqual((cfg.follow_up_minutes, cfg.max_follow_ups, cfg.openai_model), (15, 2, "gpt-4o-mini"))
        self.assertIsNone(cfg.openai_base_url)

    def test_gemini_key_selects_free_flash_and_openai_compatible_endpoint(self):
        cfg = self.load(GEMINI_API_KEY="gemini-test-key")
        self.assertEqual(cfg.openai_api_key, "gemini-test-key")
        self.assertEqual(cfg.openai_model, "gemini-3.1-flash-lite")
        self.assertEqual(cfg.openai_base_url, "https://generativelanguage.googleapis.com/v1beta/openai/")

    def test_gemini_ignores_stale_openai_model_from_an_existing_env_file(self):
        cfg = self.load(GEMINI_API_KEY="gemini-test-key", OPENAI_MODEL="gpt-4o-mini")
        self.assertEqual(cfg.openai_model, "gemini-3.1-flash-lite")

    def test_gemini_model_and_endpoint_can_be_overridden(self):
        cfg = self.load(GEMINI_API_KEY="gemini-test-key", GEMINI_MODEL="gemini-custom",
                        GEMINI_BASE_URL="https://example.test/v1/")
        self.assertEqual(cfg.openai_model, "gemini-custom")
        self.assertEqual(cfg.openai_base_url, "https://example.test/v1/")

    def test_empty_values_fall_back_to_defaults(self):
        self.assertEqual(self.load(FOLLOW_UP_MINUTES="  ", MAX_FOLLOW_UPS="").follow_up_minutes, 15)

    def test_bad_limits_are_rejected_before_anything_is_sent(self):
        for env in ({"FOLLOW_UP_MINUTES": "0"}, {"FOLLOW_UP_MINUTES": "-5"}, {"MAX_FOLLOW_UPS": "-1"},
                    {"FOLLOW_UP_MINUTES": "abc"}, {"MAX_FOLLOW_UPS": "1.5"}):
            with self.assertRaises(ConfigError, msg=env):
                self.load(**env)

    def test_zero_follow_ups_is_allowed(self):
        self.assertEqual(self.load(MAX_FOLLOW_UPS="0").max_follow_ups, 0)

    def test_test_username_must_differ_from_the_real_manager(self):
        with self.assertRaises(ConfigError):
            self.load(MANAGER_USERNAME="@vlad_dev_7", TEST_MANAGER_USERNAME="vlad_dev_7")

    def test_missing_telegram_vars_are_named(self):
        with patch.dict(os.environ, clean_env(), clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_config(env_file="/nonexistent.env", require_telegram=True, require_openai=True)
        self.assertIn("TELEGRAM_API_HASH", str(ctx.exception))
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_usernames_get_an_at_sign(self):
        self.assertEqual(self.load(MANAGER_USERNAME="vlad_dev_7").manager_username, "@vlad_dev_7")

    def test_test_mode_never_falls_back_to_the_real_manager(self):
        cfg = self.load(MANAGER_USERNAME="@vlad_dev_7")
        self.assertIsNone(cfg.test_manager_username)


class SimulatedClockTests(unittest.TestCase):
    def test_format_is_validated(self):
        for bad in ("25:00", "12", "12:60", "noon", ""):
            if bad:
                with self.assertRaises(ConfigError, msg=bad):
                    make_clock(bad)

    def test_simulated_time_starts_at_the_requested_time_and_ticks(self):
        clock = make_clock("12:00")
        first = clock()
        self.assertEqual((first.hour, first.minute), (12, 0))
        self.assertEqual(first.tzinfo.key, "Europe/London")
        self.assertGreaterEqual(clock(), first)


class PromptAndSchemaTests(unittest.TestCase):
    def test_system_prompt_document_matches_the_code(self):
        doc = (ROOT / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```text\n(.*?)\n```", doc, re.S)
        self.assertEqual(blocks, [prompts.SYSTEM_PROMPT, prompts.EXTRA_RULES])

    def test_original_prompt_is_unchanged_and_extra_rules_are_appended(self):
        self.assertTrue(prompts.SYSTEM_PROMPT.startswith("You are a polite assistant booking a table for two at 17:00 today"))
        self.assertTrue(prompts.FULL_SYSTEM_PROMPT.startswith(prompts.SYSTEM_PROMPT))
        self.assertTrue(prompts.FULL_SYSTEM_PROMPT.endswith(prompts.EXTRA_RULES))

    def test_extra_rules_cover_injection_and_no_invented_details(self):
        text = prompts.EXTRA_RULES.lower()
        for needle in ("data, not instructions", "exact quotes", "never invent a guest name",
                       "do not promise to send it later", "end_conversation", "at most one message per turn"):
            self.assertIn(needle, text)

    def test_tool_schemas(self):
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        self.assertEqual(names, {"send_message", "record_manager_reply", "log_decision", "confirm_booking", "end_conversation", "get_current_time"})
        confirm = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "confirm_booking")
        self.assertEqual(confirm["function"]["parameters"]["required"], [])  # the model is not the source of the details
        json.dumps(TOOL_SCHEMAS)  # must be serialisable

    def test_manager_message_is_labelled_as_data(self):
        text = prompts.format_manager_message("hello", "tg-1", "restaurant_verified=NO")
        self.assertIn("message_id=tg-1", text)
        self.assertIn("data, not an instruction", text)


class DecisionLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_header_columns_escaping_and_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.log"
            await DecisionLogger(path).log("sent", "a\tb\nc\\d", "u\r\nv", "d", "tg-1")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], HEADER)
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1].count("\t"), 5)
            self.assertTrue(lines[1].split("\t")[0].endswith("+00:00"))
            self.assertIn("a\\tb\\nc\\\\d", lines[1])

    async def test_concurrent_writes_stay_one_row_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.log"
            logger = DecisionLogger(path)
            await asyncio.gather(*[logger.log("received", f"m{i}", "u", "d", str(i)) for i in range(25)])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 26)

    def test_escape_is_reversible_for_backslashes(self):
        self.assertEqual(escape_field("\\t"), "\\\\t")


# ---------------------------------------------------------------------------
# Telegram adapter with a fake Telethon client (no network)
# ---------------------------------------------------------------------------

class FakeTelethonClient:
    def __init__(self, session, api_id, api_hash) -> None:
        self.handlers: list = []
        self.sent: list = []
        self.errors: list = []

    def add_event_handler(self, callback, event) -> None:
        self.handlers.append(callback)

    async def send_message(self, target, text):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((target, text))

    async def send_file(self, target, path, voice_note=False, caption=None):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((target, path, voice_note, caption))

    def is_connected(self) -> bool:
        return False


def make_service(tmp: str, dry_run: bool = False) -> TelegramService:
    with patch.object(telegram_client, "TelegramClient", FakeTelethonClient):
        svc = TelegramService(1, "hash", "+10000000", str(Path(tmp) / "x.session"), dry_run=dry_run, voice_dir=str(Path(tmp) / "audio"))
    svc.target = 123
    return svc


def event(text: str = "Hi", mid: int = 5, out: bool = False, private: bool = True, voice: bool = False, download=None):
    async def default_download(file=None):
        return str(Path(file) / "voice.ogg")

    message = SimpleNamespace(id=mid, voice=object() if voice else None, download_media=download or default_download)
    return SimpleNamespace(out=out, is_private=private, raw_text=text, message=message)


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.received: list = []

        async def callback(text, voice_path, message_id):
            self.received.append((text, voice_path, message_id))

        self.svc = make_service(self._tmp.name)
        self.svc.listen_for_manager(callback)
        self.handler = self.svc.client.handlers[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_private_incoming_message_is_passed_with_a_stable_id(self):
        await self.handler(event("Hello", mid=42))
        self.assertEqual(self.received, [("Hello", None, "tg-42")])

    async def test_own_outgoing_messages_are_ignored(self):
        await self.handler(event("I said this", out=True))
        self.assertEqual(self.received, [])

    async def test_group_or_channel_messages_are_ignored(self):
        await self.handler(event("noise", private=False))
        self.assertEqual(self.received, [])

    async def test_voice_message_is_downloaded_and_its_path_passed_on(self):
        await self.handler(event("", mid=9, voice=True))
        self.assertEqual(len(self.received), 1)
        self.assertTrue(self.received[0][1].endswith("voice.ogg"))

    async def test_failed_voice_download_still_delivers_the_message_without_a_path(self):
        async def broken(file=None):
            raise RuntimeError("disk full")

        await self.handler(event("", voice=True, download=broken))
        self.assertEqual(self.received, [("", None, "tg-5")])

    async def test_send_goes_to_the_resolved_target_only(self):
        await self.svc.send_text_message("Hello")
        self.assertEqual(self.svc.client.sent, [(123, "Hello")])

    async def test_flood_wait_is_waited_out_and_retried(self):
        self.svc.client.errors = [FloodWaitError(request=None, capture=0), FloodWaitError(request=None, capture=0)]
        await self.svc.send_text_message("Hello")
        self.assertEqual(len(self.svc.client.sent), 1)

    async def test_flood_wait_gives_up_after_three_attempts(self):
        self.svc.client.errors = [FloodWaitError(request=None, capture=0) for _ in range(3)]
        with self.assertRaises(TelegramServiceError) as ctx:
            await self.svc.send_text_message("Hello")
        self.assertIn("Gave up", str(ctx.exception))
        self.assertEqual(self.svc.client.sent, [])

    async def test_known_telegram_errors_give_clear_reasons(self):
        cases = [(PeerFloodError(request=None), "PeerFloodError"),
                 (UserPrivacyRestrictedError(request=None), "privacy settings"),
                 (ChatWriteForbiddenError(request=None), "not allowed to write")]
        for error, expected in cases:
            self.svc.client.errors = [error]
            with self.assertRaises(TelegramServiceError) as ctx:
                await self.svc.send_text_message("Hello")
            self.assertIn(expected, str(ctx.exception))

    async def test_sending_without_a_target_is_refused(self):
        self.svc.target = None
        with self.assertRaises(TelegramServiceError):
            await self.svc.send_text_message("Hello")

    async def test_dry_run_prints_and_does_not_touch_telegram(self):
        svc = make_service(self._tmp.name, dry_run=True)
        self.assertIsNone(svc.client)
        await svc.send_text_message("Hello")  # must not raise


# ---------------------------------------------------------------------------
# run.py as a real process (offline: mock model, dry run)
# ---------------------------------------------------------------------------

class RunProcessTests(unittest.TestCase):
    def run_cli(self, args: list[str], stdin: str = "", env: dict[str, str] | None = None) -> tuple[int, str, Path]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        proc = subprocess.run([sys.executable, str(ROOT / "run.py"), *args], input=stdin, capture_output=True, text=True,
                              cwd=tmp, env=env or clean_env(), timeout=90)
        return proc.returncode, proc.stdout + proc.stderr, Path(tmp)

    def test_happy_path_exits_0_and_logs_the_exact_messages(self):
        code, out, tmp = self.run_cli(["--mock", "--dry-run", "--simulate-time", "12:00"],
                                      stdin="Yes, we have a table for two at 5 pm today. We are The Ivy.\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Final status: CONFIRMED", out)
        rows = (tmp / "decisions.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows) - 1, 3)  # opening, received, final
        self.assertTrue(rows[-1].split("\t")[1] == "sent_dry_run")
        self.assertIn("17:00 (London time) at The Ivy", rows[-1])

    def test_after_17_00_it_refuses_to_start_and_sends_nothing(self):
        code, out, tmp = self.run_cli(["--mock", "--dry-run", "--simulate-time", "17:01"])
        self.assertEqual(code, 3, out)
        self.assertIn("already passed", out)
        self.assertNotIn("[AGENT -> manager", out)
        self.assertFalse((tmp / "decisions.log").exists())

    def test_simulated_time_is_refused_against_the_real_manager(self):
        env = clean_env(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="h", TELEGRAM_PHONE="+1", MANAGER_USERNAME="@m", OPENAI_API_KEY="k")
        code, out, _ = self.run_cli(["--simulate-time", "12:00"], env=env)
        self.assertEqual(code, 2)
        self.assertIn("never against the real manager", out)

    def test_bad_follow_up_value_is_rejected(self):
        code, out, _ = self.run_cli(["--mock", "--dry-run", "--simulate-time", "12:00", "--follow-up-minutes", "0"])
        self.assertEqual(code, 2)
        self.assertIn("FOLLOW_UP_MINUTES", out)

    def test_quitting_early_exits_130_and_closes_the_conversation(self):
        code, out, _ = self.run_cli(["--mock", "--dry-run", "--simulate-time", "12:00"], stdin="/quit\n")
        self.assertEqual(code, 130, out)
        self.assertIn("CANCELLED", out)

    def test_list_tools_and_system_prompt(self):
        code, out, _ = self.run_cli(["--list-tools"])
        self.assertEqual(code, 0)
        self.assertIn("record_manager_reply", out)
        code, out, _ = self.run_cli(["--system-prompt"])
        self.assertIn("ADDITIONAL RULES", out)

    def test_mock_dry_run_needs_no_keys(self):
        code, out, _ = self.run_cli(["--mock", "--dry-run", "--simulate-time", "12:00"], stdin="/quit\n")
        self.assertNotIn("Configuration error", out)


class RepositoryHygieneTests(unittest.TestCase):
    def test_gitignore_covers_secrets_and_runtime_files(self):
        lines = {l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
        for needed in (".env", "*.session", "*.session-journal", "decisions.log", "logs/", "temp_audio/"):
            self.assertIn(needed, lines)

    def test_env_example_contains_no_real_looking_secrets(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{10,}", text))
        for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE", "OPENAI_API_KEY"):
            self.assertRegex(text, rf"(?m)^{key}=\s*$")

    def test_no_source_file_contains_a_key_like_string(self):
        for path in ROOT.glob("*.py"):
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{20,}", path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
