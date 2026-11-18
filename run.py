"""Entry point of the booking agent.

Examples:
    python run.py --help
    python run.py --mock --dry-run --simulate-time 12:00   # no keys, no Telegram: offline check
    python run.py --dry-run --simulate-time 12:00          # real configured model, no Telegram
    python run.py --test                                   # real Telegram, sent to TEST_MANAGER_USERNAME
    python run.py                                          # real Telegram, sent to MANAGER_USERNAME

The booking is for 17:00 TODAY in London. If 17:00 has already passed the agent refuses to
start and sends nothing. --simulate-time pretends it is an earlier time, and is allowed only
with --dry-run or --test, never against the real manager.

Exit codes: 0 confirmed or declined; 1 failed; 2 bad configuration or arguments;
3 expired (17:00 passed) or no reply; 130 stopped by the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

from openai import AsyncOpenAI

import voice_utils
from agent_core import AgentCore
from booking_state import BookingState, Status
from config import Config, ConfigError, load_config, validate_limits
from decision_logger import DecisionLogger, TurnLog
from mock_llm import MockLLM
from prompts import FULL_SYSTEM_PROMPT
from provider_compat import is_gemini_compatible_url
from telegram_client import TelegramService, TelegramServiceError
from tools_registry import TOOL_SCHEMAS, ToolContext
from utils import LONDON, ensure_directory, get_current_time_london

VERSION = "1.1.1"
DECISIONS_FILE = "decisions.log"
VOICE_DIR = "temp_audio"

EXIT_CODES = {
    Status.CONFIRMED: 0,
    Status.DECLINED: 0,
    Status.FAILED: 1,
    Status.EXPIRED: 3,
    Status.NO_RESPONSE: 3,
    Status.CANCELLED: 130,
}

logger = logging.getLogger("booking_agent")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse the command-line flags."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="AI booking agent: books a table for two at 17:00 today in London via Telegram.",
    )
    parser.add_argument("--test", action="store_true", help="message TEST_MANAGER_USERNAME instead of MANAGER_USERNAME")
    parser.add_argument("--dry-run", action="store_true", help="no Telegram: print agent messages and type the manager's replies")
    parser.add_argument("--voice", action="store_true", help="enable voice messages (also needs ENABLE_VOICE=true in .env)")
    parser.add_argument("--follow-up-minutes", type=int, metavar="N", help="override FOLLOW_UP_MINUTES")
    parser.add_argument("--max-follow-ups", type=int, metavar="N", help="override MAX_FOLLOW_UPS")
    parser.add_argument("--reset", action="store_true", help="delete the saved session and log in again")
    parser.add_argument("--logout", action="store_true", help="log out of Telegram, delete the session, and exit")
    parser.add_argument("--list-tools", action="store_true", help="print the tool schemas and exit")
    parser.add_argument("--system-prompt", action="store_true", help="print the system prompt and exit")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--mock", action="store_true", help="use the offline simulated model instead of the configured API provider (no model key needed)")
    parser.add_argument("--simulate-time", metavar="HH:MM",
                        help="pretend it is this London time today (only with --dry-run or --test); lets you test after 17:00")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def make_clock(simulate: str | None) -> Callable[[], datetime]:
    """The clock used by the time gate: the real London time, or a simulated start time that keeps ticking."""
    if not simulate:
        return get_current_time_london
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", simulate.strip())
    if not m:
        raise ConfigError("--simulate-time must look like HH:MM, for example 12:00")
    real_start = get_current_time_london()
    sim_start = datetime.combine(real_start.date(), time(int(m.group(1)), int(m.group(2))), tzinfo=LONDON)
    return lambda: sim_start + (get_current_time_london() - real_start)


# ---------------------------------------------------------------------------
# Logging and session files
# ---------------------------------------------------------------------------

def setup_logging(log_file: str, debug: bool) -> None:
    """Send logs to the console and to a file."""
    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    ensure_directory(log_file)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def delete_session_files(session_base: Path) -> int:
    """Delete the Telethon session files. Returns how many were removed."""
    removed = 0
    for suffix in (".session", ".session-journal"):
        path = Path(str(session_base) + suffix)
        if path.exists():
            path.unlink()
            removed += 1
    return removed


async def do_logout(cfg: Config, dry_run: bool) -> int:
    """Log out of Telegram and remove the local session."""
    tg = TelegramService(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_phone, cfg.session_file, dry_run=dry_run)
    try:
        if not dry_run:
            await tg.start()  # logs in first if needed, so the logout can be sent
            await tg.log_out()
            logger.info("Logged out of Telegram.")
    except TelegramServiceError as exc:
        logger.error("%s", exc)
    finally:
        await tg.disconnect()
    removed = delete_session_files(Path(cfg.session_file).with_suffix(""))
    logger.info("Removed %d session file(s).", removed)
    return 0


# ---------------------------------------------------------------------------
# Dry-run console: you play the manager
# ---------------------------------------------------------------------------

async def console_manager_loop(agent: AgentCore, stop_event: asyncio.Event) -> None:
    """Read the manager's replies from the keyboard (dry-run only), in a daemon thread."""
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def reader() -> None:
        while True:
            try:
                line = input("manager> ")
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(lines.put_nowait, None)
                return
            loop.call_soon_threadsafe(lines.put_nowait, line)

    threading.Thread(target=reader, daemon=True).start()
    print("Dry run: type the manager's replies here. Type /quit to stop.")

    number = 0
    while not stop_event.is_set():
        line = await lines.get()
        if line is None or line.strip() == "/quit":
            stop_event.set()
            return
        if not line.strip():
            continue
        number += 1
        try:
            await agent.process_incoming_message(line.strip(), f"console-{number}")
        except Exception:
            logger.exception("Error while handling the typed reply.")
            stop_event.set()
            return


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace, cfg: Config) -> int:
    """Wire the parts together and run one conversation. Returns the exit code."""
    if args.logout:
        return await do_logout(cfg, args.dry_run)

    follow_up = args.follow_up_minutes if args.follow_up_minutes is not None else cfg.follow_up_minutes
    max_follow_ups = args.max_follow_ups if args.max_follow_ups is not None else cfg.max_follow_ups
    try:
        validate_limits(follow_up, max_follow_ups)
        if args.simulate_time and not (args.dry_run or args.test):
            raise ConfigError("--simulate-time is only allowed with --dry-run or --test, never against the real manager.")
        clock = make_clock(args.simulate_time)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.test:
        if not cfg.test_manager_username and not args.dry_run:
            print("Configuration error: --test needs TEST_MANAGER_USERNAME in .env.", file=sys.stderr)
            return 2
        target = cfg.test_manager_username or "(test account)"
    else:
        target = cfg.manager_username or "(not set)"

    # Time gate: 17:00 today (London) must still be ahead. A warning would not be enough:
    # nothing is sent and Telegram is not even contacted.
    booking = BookingState(clock=clock)
    if booking.expire_if_needed():
        now = clock().astimezone(LONDON)
        logger.error(
            "It is %s in London, so 17:00 today has already passed. Nothing was sent. "
            "Run again before 17:00 London time, or add --simulate-time HH:MM together with --dry-run or --test.",
            now.strftime("%H:%M"),
        )
        return EXIT_CODES[Status.EXPIRED]

    if args.reset:
        removed = delete_session_files(Path(cfg.session_file).with_suffix(""))
        logger.info("Session reset. Removed %d file(s).", removed)

    # Voice needs both the flag and ENABLE_VOICE=true.
    voice_on = args.voice and cfg.enable_voice
    if args.voice and not cfg.enable_voice:
        logger.warning("--voice given but ENABLE_VOICE is not true in .env; voice stays off.")
    if voice_on:
        missing = voice_utils.missing_dependencies()
        if missing:
            logger.warning("Voice is off because this is missing: %s. Using text only.", "; ".join(missing))
            voice_on = False
        else:
            logger.info("Voice is on: messages go out as voice notes with the text as caption.")

    provider_label = (
        "mock" if args.mock else
        "Gemini (OpenAI compatibility)" if is_gemini_compatible_url(cfg.openai_base_url) else
        "OpenAI-compatible provider" if cfg.openai_base_url else "OpenAI"
    )
    logger.info(
        "Mode: %s %s target=%s%s", provider_label, "dry-run" if args.dry_run else "telegram",
        target, f" simulated-time={args.simulate_time}" if args.simulate_time else "",
    )

    tg = TelegramService(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_phone, cfg.session_file, dry_run=args.dry_run)

    async def send_to_manager(text: str) -> None:
        """Send one message (as a voice note if voice is on). Errors propagate: the caller records the failure."""
        note = await voice_utils.make_voice_note(text, VOICE_DIR) if voice_on else None
        if note is not None:
            try:
                await tg.send_voice_message(str(note), caption=text)
            finally:
                voice_utils.delete_quietly(note)
        else:
            if voice_on:
                logger.warning("Voice note failed; sending plain text instead.")
            await tg.send_text_message(text)

    decisions = DecisionLogger(DECISIONS_FILE)
    turn_log = TurnLog(decisions, sent_direction="sent_dry_run" if args.dry_run else "sent")
    ctx = ToolContext(send_text=send_to_manager, turn_log=turn_log, booking=booking)

    llm: Any = MockLLM() if args.mock else AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url)
    agent = AgentCore(llm, "mock" if args.mock else cfg.openai_model, ctx,
                      follow_up_minutes=follow_up, max_follow_ups=max_follow_ups,
                      provider_compat="gemini" if is_gemini_compatible_url(cfg.openai_base_url) else "openai")

    stop_event = asyncio.Event()
    input_task: asyncio.Task | None = None

    async def on_manager_message(text: str, voice_path: str | None, message_id: str) -> None:
        """Handle one incoming Telegram message from the manager."""
        if voice_path is not None:
            transcript = await voice_utils.transcribe_audio(voice_path) if voice_on else None
            voice_utils.delete_quietly(voice_path)
            if transcript:
                text = f"[voice message, transcribed] {transcript}"
            else:
                text = "[voice message that could not be transcribed; please ask the manager to write instead]"
                logger.warning("Voice message could not be transcribed.")
        try:
            await agent.process_incoming_message(text, message_id)
        except Exception:
            logger.exception("Error while handling the manager's message.")
            stop_event.set()

    try:
        await tg.start()
        if not args.dry_run:
            # In Telegram mode, install the listener before the opening message so a
            # very fast manager reply cannot be missed.
            await tg.resolve_target(target)
            tg.listen_for_manager(on_manager_message)

        logger.info("Starting the conversation.")
        await agent.start_conversation()

        # In dry-run mode, do NOT start the keyboard reader until the opening
        # message has actually been sent. Otherwise a typed manager reply can race
        # with the opening turn and interleave with log output.
        if args.dry_run and agent.conversation_active:
            input_task = asyncio.create_task(console_manager_loop(agent, stop_event))

        while agent.conversation_active and not stop_event.is_set():
            await asyncio.sleep(0.5)

    except TelegramServiceError as exc:
        logger.error("%s", exc)
        booking.set_status(Status.FAILED, str(exc))
    finally:
        await agent.shutdown()
        if input_task is not None:
            input_task.cancel()
        await tg.disconnect()

    logger.info("Final status: %s%s", booking.status.value,
                f" ({booking.failed_reason or booking.end_reason})" if (booking.failed_reason or booking.end_reason) else "")
    if booking.operator_input_needed:
        logger.warning("The manager asked for details the agent does not have: %r", booking.operator_input_needed)
    logger.info("Decisions are in %s.", DECISIONS_FILE)
    return EXIT_CODES.get(booking.status, 1)


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns the exit code."""
    args = parse_args(argv)

    if args.list_tools:
        print(json.dumps(TOOL_SCHEMAS, indent=2, ensure_ascii=False))
        return 0
    if args.system_prompt:
        print(FULL_SYSTEM_PROMPT)
        return 0

    try:
        cfg = load_config(require_telegram=not args.dry_run, require_openai=not args.mock)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_file, args.debug)
    try:
        return asyncio.run(run(args, cfg))
    except KeyboardInterrupt:
        logger.info("Stopped by the user (Ctrl+C).")
        return 130


if __name__ == "__main__":
    sys.exit(main())
