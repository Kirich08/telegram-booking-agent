"""Optional voice support (the star task).

  - transcribe_audio(file_path)        speech -> text      (faster-whisper, runs locally)
  - generate_voice(text, output_path)  text -> MP3         (edge-tts, free online service)
  - convert_to_ogg_opus(in, out)       MP3 -> OGG Opus     (ffmpeg, installed separately)
  - make_voice_note(text, folder)      the three steps for an outgoing message

Nothing here uses the OpenAI API, so the OpenAI key needs no audio permission.

Every function handles its own errors and returns None / False instead of raising.
The caller then falls back to a normal text message, so voice problems never
stop the booking.

The imports of faster-whisper and edge-tts are lazy: the program starts and works
without them, and only complains when voice is actually requested.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WHISPER_MODEL_SIZE = "base"
TTS_VOICE = "en-GB-RyanNeural"
FFMPEG_TIMEOUT_SECONDS = 60

_whisper_model: Any = None  # loaded once, on first use (the download can take a while)


def missing_dependencies() -> list[str]:
    """Return what is needed for voice but not installed. Empty list = all good."""
    missing: list[str] = []
    for module, package in (("faster_whisper", "faster-whisper"), ("edge_tts", "edge-tts")):
        try:
            __import__(module)
        except ImportError:
            missing.append(f"python package {package}")
        except Exception as exc:  # a broken install can raise other errors
            missing.append(f"python package {package} (import failed: {exc})")
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg (install it and add it to PATH)")
    return missing


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------

def _transcribe_blocking(file_path: str) -> str:
    """Run faster-whisper. Blocking: call it from a thread."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        logger.info("Loading the Whisper '%s' model (the first run downloads it)...", WHISPER_MODEL_SIZE)
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")

    segments, _info = _whisper_model.transcribe(file_path, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_audio(file_path: str) -> str | None:
    """Turn a voice message into text. Returns None on any failure or empty speech."""
    if not Path(file_path).exists():
        logger.warning("Voice file not found: %s", file_path)
        return None
    try:
        text = await asyncio.to_thread(_transcribe_blocking, file_path)
    except ImportError:
        logger.warning("faster-whisper is not installed; cannot transcribe voice.")
        return None
    except Exception as exc:
        logger.warning("Transcription failed: %s", exc)
        return None
    if not text:
        logger.warning("Transcription returned no text.")
        return None
    return text


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

async def generate_voice(text: str, output_path: str) -> bool:
    """Create an MP3 file with a British voice. Returns True on success."""
    if not text.strip():
        return False
    try:
        import edge_tts

        await edge_tts.Communicate(text, TTS_VOICE).save(output_path)
    except ImportError:
        logger.warning("edge-tts is not installed; cannot generate voice.")
        return False
    except Exception as exc:
        logger.warning("Voice generation failed: %s", exc)
        return False

    path = Path(output_path)
    if not path.exists() or path.stat().st_size == 0:
        logger.warning("Voice generation produced an empty file.")
        return False
    return True


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

async def convert_to_ogg_opus(input_path: str, output_path: str) -> bool:
    """Convert an audio file to OGG Opus, the format Telegram shows as a voice note."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning("ffmpeg was not found; cannot convert audio.")
        return False

    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", input_path,
        "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
        output_path,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        logger.warning("ffmpeg timed out.")
        return False
    except Exception as exc:
        logger.warning("Could not run ffmpeg: %s", exc)
        return False

    if process.returncode != 0:
        logger.warning("ffmpeg failed: %s", stderr.decode("utf-8", "replace").strip())
        return False

    path = Path(output_path)
    return path.exists() and path.stat().st_size > 0


async def make_voice_note(text: str, folder: str | Path) -> Path | None:
    """Create a ready-to-send OGG Opus voice note for a text. None if any step fails."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex[:12]
    mp3 = folder / f"{name}.mp3"
    ogg = folder / f"{name}.ogg"

    try:
        if not await generate_voice(text, str(mp3)):
            return None
        if not await convert_to_ogg_opus(str(mp3), str(ogg)):
            return None
        return ogg
    finally:
        mp3.unlink(missing_ok=True)  # the MP3 is only an intermediate file


def delete_quietly(path: str | Path | None) -> None:
    """Remove a temporary file, ignoring errors."""
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
