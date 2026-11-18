"""Configuration loader.

Reads environment variables from a .env file (if present) and from the
process environment, validates what the chosen mode needs and returns a
Config object.

The .env file is looked up next to this file, so the program works no
matter which folder you start it from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when a required environment variable is missing or invalid."""


# Needed when messages go to real Telegram (not --dry-run).
TELEGRAM_VARS = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "MANAGER_USERNAME",
)

# The app uses the OpenAI Python SDK. Either a native OpenAI key or a Gemini
# API key (through Google's OpenAI-compatible endpoint) can power the model.
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"


@dataclass(frozen=True)
class Config:
    """All settings the application needs, already parsed to the right types."""

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    openai_api_key: str
    manager_username: str
    test_manager_username: str | None
    enable_voice: bool
    follow_up_minutes: int
    max_follow_ups: int
    log_file: str
    openai_model: str
    openai_base_url: str | None
    session_file: str


def _parse_bool(value: str | None, default: bool) -> bool:
    """Convert strings like 'true', '1', 'yes' to a boolean."""
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(name: str, value: str | None, default: int) -> int:
    """Convert an env value to int, using the default when it is empty."""
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from exc


def validate_limits(follow_up_minutes: int, max_follow_ups: int) -> None:
    """Reject values that would make the follow-up logic meaningless or dangerous."""
    if follow_up_minutes < 1:
        raise ConfigError(f"FOLLOW_UP_MINUTES must be at least 1, got {follow_up_minutes}")
    if max_follow_ups < 0:
        raise ConfigError(f"MAX_FOLLOW_UPS cannot be negative, got {max_follow_ups}")


def _with_at(username: str | None) -> str | None:
    """Make sure a Telegram username starts with '@'."""
    if not username:
        return None
    return username if username.startswith("@") else "@" + username


def load_config(
    env_file: str | Path = DEFAULT_ENV_FILE,
    require_telegram: bool = True,
    require_openai: bool = True,
) -> Config:
    """Load .env (if it exists), validate what is needed and build Config.

    Args:
        env_file: path to the .env file.
        require_telegram: check the Telegram variables (off for --dry-run).
        require_openai: check that some model-provider key is configured (off for --mock).

    Raises:
        ConfigError: if a required variable is missing or a value is malformed.
    """
    # load_dotenv does nothing when the file is missing, so this is safe.
    load_dotenv(dotenv_path=env_file, override=False)

    required: list[str] = []
    if require_telegram:
        required += TELEGRAM_VARS
    missing = [name for name in required if not os.getenv(name, "").strip()]
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if require_openai and not (gemini_key or openai_key):
        missing.append("GEMINI_API_KEY or OPENAI_API_KEY")
    if missing:
        raise ConfigError(
            "Missing environment variables: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    follow_up_minutes = _parse_int("FOLLOW_UP_MINUTES", os.getenv("FOLLOW_UP_MINUTES"), 15)
    max_follow_ups = _parse_int("MAX_FOLLOW_UPS", os.getenv("MAX_FOLLOW_UPS"), 2)
    validate_limits(follow_up_minutes, max_follow_ups)

    manager = _with_at(os.getenv("MANAGER_USERNAME", "").strip()) or ""
    test_manager = _with_at(os.getenv("TEST_MANAGER_USERNAME", "").strip())
    if test_manager and manager and test_manager.casefold() == manager.casefold():
        raise ConfigError("TEST_MANAGER_USERNAME must not be the same as MANAGER_USERNAME.")

    gemini_base_url = os.getenv("GEMINI_BASE_URL", "").strip()
    openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    # An explicit Gemini key selects Gemini by default. Setting GEMINI_BASE_URL
    # also selects the Gemini key; otherwise the legacy OpenAI variables remain valid.
    use_gemini = bool(gemini_key or gemini_base_url or "generativelanguage.googleapis.com" in openai_base_url.lower())
    resolved_key = gemini_key or openai_key
    if gemini_key or gemini_base_url:
        resolved_base_url = gemini_base_url or DEFAULT_GEMINI_BASE_URL
    else:
        resolved_base_url = openai_base_url
    if use_gemini:
        # Do not inherit a stale OPENAI_MODEL (often gpt-4o-mini) when a Gemini key
        # is added to an existing .env. Use the Gemini default unless explicitly overridden.
        resolved_model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.1-flash-lite"
    else:
        resolved_model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"

    return Config(
        telegram_api_id=_parse_int("TELEGRAM_API_ID", os.getenv("TELEGRAM_API_ID"), 0),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
        telegram_phone=os.getenv("TELEGRAM_PHONE", "").strip(),
        openai_api_key=resolved_key,
        manager_username=manager,
        test_manager_username=test_manager,
        enable_voice=_parse_bool(os.getenv("ENABLE_VOICE"), default=False),
        follow_up_minutes=follow_up_minutes,
        max_follow_ups=max_follow_ups,
        log_file=os.getenv("LOG_FILE", "logs/agent.log").strip(),
        openai_model=resolved_model,
        openai_base_url=resolved_base_url or None,
        session_file=os.getenv("SESSION_FILE", "booking_agent.session").strip(),
    )
