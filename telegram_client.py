"""Telegram client wrapper (Telethon, MTProto userbot).

Why Telethon and not the Bot API: a bot cannot start a conversation with a
user who has not written to it first. A userbot logs in as a normal account,
so it can message the manager first.

This module owns everything that talks to Telegram:
  - login (with 2FA password support),
  - finding the manager by username,
  - sending text and voice messages (with FloodWait retries),
  - listening for the manager's replies,
  - disconnecting.

In dry-run mode no connection is made: messages are printed to the console.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    UsernameNotOccupiedError,
)

logger = logging.getLogger(__name__)

MAX_FLOOD_RETRIES = 3

# Messages from the assignment spec, logged verbatim.
MSG_PRIVACY = (
    "Cannot message the manager due to privacy settings. Please ensure the manager "
    "accepts messages from strangers or use a different account."
)
MSG_USERNAME = "The manager username does not exist."
MSG_WRITE_FORBIDDEN = "You are not allowed to write to this chat."
MSG_PEER_FLOOD = (
    "Telegram limits this account from messaging people who have not written to it "
    "(PeerFloodError). Use an older account, or write to the target manually once."
)
MSG_BLOCKED = "The target user has blocked this account."

# Called with (text, voice_file_path, message_id) for every message from the manager.
# voice_file_path is None for ordinary text messages. message_id is stable per Telegram message.
IncomingCallback = Callable[[str, str | None, str], Awaitable[None]]


class TelegramServiceError(Exception):
    """A Telegram problem the program cannot recover from on its own."""


class TelegramService:
    """Thin, error-aware wrapper around TelegramClient."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_file: str,
        dry_run: bool = False,
        voice_dir: str = "temp_audio",
    ) -> None:
        self.voice_dir = voice_dir
        self.phone = phone
        self.dry_run = dry_run
        # Telethon adds ".session" itself, so the name must not include it.
        self.session_base = str(Path(session_file).with_suffix(""))
        self.target: Any = None
        self.client: TelegramClient | None = None
        if not dry_run:
            self.client = TelegramClient(self.session_base, api_id, api_hash)

    # ------------------------------------------------------------------
    # Connection and login
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect and log in. Asks for the code on the first run only."""
        if self.dry_run or self.client is None:
            logger.info("Dry run: Telegram is not used.")
            return

        await self.client.connect()
        if await self.client.is_user_authorized():
            logger.info("Logged in with the saved session.")
            return
        await self._interactive_login()

    async def _interactive_login(self) -> None:
        """First login: send a code, read it, handle 2FA and bad numbers."""
        assert self.client is not None
        logger.info("First login: Telegram is sending a code to %s.", self.phone)

        try:
            await self.client.send_code_request(self.phone)
        except PhoneNumberInvalidError as exc:
            raise TelegramServiceError(
                "TELEGRAM_PHONE is not valid. Use the full international format, "
                "e.g. +380501234567."
            ) from exc
        except PhoneNumberBannedError as exc:
            raise TelegramServiceError("This phone number is banned by Telegram.") from exc
        except PhoneNumberUnoccupiedError as exc:
            raise TelegramServiceError(
                "This phone number has no Telegram account. Register it in the "
                "Telegram app first."
            ) from exc

        code = await asyncio.to_thread(input, "Enter the login code from Telegram: ")
        try:
            await self.client.sign_in(self.phone, code.strip())
        except PhoneCodeInvalidError as exc:
            raise TelegramServiceError("The login code is wrong. Run the program again.") from exc
        except PhoneCodeExpiredError as exc:
            raise TelegramServiceError("The login code has expired. Run the program again.") from exc
        except SessionPasswordNeededError:
            # The account has two-step verification; the password is asked privately.
            password = await asyncio.to_thread(getpass.getpass, "Two-step verification password: ")
            await self.client.sign_in(password=password)

        logger.info("Logged in. The session is saved to %s.session", self.session_base)

    async def log_out(self) -> None:
        """Log out on Telegram's side (the caller deletes the session file)."""
        if self.client is not None:
            await self.client.log_out()

    async def disconnect(self) -> None:
        """Close the connection if it is open."""
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()
            logger.info("Disconnected from Telegram.")

    # ------------------------------------------------------------------
    # Target and listening
    # ------------------------------------------------------------------

    async def resolve_target(self, username: str) -> Any:
        """Find the manager (or test account) by username and remember it."""
        if self.dry_run or self.client is None:
            self.target = username
            return username

        try:
            entity = await self.client.get_entity(username)
        except (UsernameNotOccupiedError, ValueError) as exc:
            raise TelegramServiceError(MSG_USERNAME) from exc

        self.target = entity
        logger.info("Target resolved: %s", username)
        return entity

    def listen_for_manager(self, callback: IncomingCallback) -> None:
        """Call callback(text, voice_path, message_id) for every new message from the target.

        Only incoming messages from that one private chat are passed on. The agent's own
        messages and messages from other chats are ignored. Voice messages are downloaded
        to voice_dir first; voice_path is that file.
        """
        if self.dry_run or self.client is None:
            return
        if self.target is None:
            raise TelegramServiceError("resolve_target() must be called before listening.")

        async def _on_new_message(event: events.NewMessage.Event) -> None:
            if event.out or not event.is_private:
                return  # our own message, or a group/channel: never treated as the manager
            voice_path: str | None = None
            if event.message.voice:
                Path(self.voice_dir).mkdir(parents=True, exist_ok=True)
                try:
                    voice_path = await event.message.download_media(file=self.voice_dir + "/")
                except Exception as exc:
                    logger.warning("Could not download the voice message: %s", exc)
            await callback(event.raw_text or "", voice_path, f"tg-{event.message.id}")

        self.client.add_event_handler(
            _on_new_message,
            events.NewMessage(from_users=self.target, incoming=True),
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_text_message(self, text: str) -> None:
        """Send a text message to the target (printed in dry-run mode)."""
        if self.dry_run:
            print(f"\n[AGENT -> manager | dry run]\n{text}\n")
            return
        assert self.client is not None
        await self._send_with_retries(
            lambda: self.client.send_message(self.target, text),
            kind="text message",
        )

    async def send_voice_message(self, audio_path: str, caption: str | None = None) -> None:
        """Send an OGG Opus file as a voice note, with the text as its caption."""
        if not Path(audio_path).exists():
            raise TelegramServiceError(f"Voice file not found: {audio_path}")
        if self.dry_run:
            print(f"\n[AGENT -> manager | dry run] voice message: {audio_path}\n{caption or ''}\n")
            return
        assert self.client is not None
        await self._send_with_retries(
            lambda: self.client.send_file(
                self.target, audio_path, voice_note=True, caption=caption
            ),
            kind="voice message",
        )

    async def _send_with_retries(self, send: Callable[[], Awaitable[Any]], kind: str) -> None:
        """Run a send call, waiting out FloodWait errors up to MAX_FLOOD_RETRIES times."""
        if self.target is None:
            raise TelegramServiceError("No target has been resolved yet.")

        for attempt in range(1, MAX_FLOOD_RETRIES + 1):
            try:
                await send()
                return
            except FloodWaitError as exc:
                logger.warning(
                    "Telegram asks to wait %d s (attempt %d of %d).",
                    exc.seconds,
                    attempt,
                    MAX_FLOOD_RETRIES,
                )
                await asyncio.sleep(exc.seconds)
            except UserPrivacyRestrictedError as exc:
                raise TelegramServiceError(MSG_PRIVACY) from exc
            except ChatWriteForbiddenError as exc:
                raise TelegramServiceError(MSG_WRITE_FORBIDDEN) from exc
            except PeerFloodError as exc:
                raise TelegramServiceError(MSG_PEER_FLOOD) from exc
            except UserIsBlockedError as exc:
                raise TelegramServiceError(MSG_BLOCKED) from exc

        raise TelegramServiceError(
            f"Gave up sending the {kind} after {MAX_FLOOD_RETRIES} FloodWait waits."
        )
