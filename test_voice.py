"""Offline tests for voice_utils: real ffmpeg, stubbed text-to-speech, fallbacks.

Real edge-tts and faster-whisper need internet and a model download, so they are
NOT tested here. Test them by hand (see VOICE.md).

    python -m unittest test_voice -v
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import voice_utils

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def make_test_mp3(path: str) -> None:
    """Create a 1-second sine-wave MP3 with ffmpeg (stands in for edge-tts output)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", path],
        check=True,
    )


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg is not installed")
class FfmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_convert_produces_ogg_opus(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp3, ogg = str(Path(tmp) / "a.mp3"), str(Path(tmp) / "a.ogg")
            make_test_mp3(mp3)
            self.assertTrue(await voice_utils.convert_to_ogg_opus(mp3, ogg))
            self.assertGreater(Path(ogg).stat().st_size, 0)
            self.assertEqual(Path(ogg).read_bytes()[:4], b"OggS")  # Ogg container header

    async def test_convert_fails_cleanly_on_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.mp3"
            bad.write_bytes(b"not audio")
            self.assertFalse(await voice_utils.convert_to_ogg_opus(str(bad), str(Path(tmp) / "o.ogg")))

    async def test_make_voice_note_with_stubbed_tts(self):
        async def fake_tts(text: str, output_path: str) -> bool:
            make_test_mp3(output_path)
            return True

        with tempfile.TemporaryDirectory() as tmp, patch.object(voice_utils, "generate_voice", fake_tts):
            note = await voice_utils.make_voice_note("Hello", tmp)
            self.assertIsNotNone(note)
            self.assertEqual(note.suffix, ".ogg")
            self.assertEqual([p.suffix for p in Path(tmp).iterdir()], [".ogg"])  # the mp3 was removed


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_failure_returns_none(self):
        async def failing_tts(text: str, output_path: str) -> bool:
            return False

        with tempfile.TemporaryDirectory() as tmp, patch.object(voice_utils, "generate_voice", failing_tts):
            self.assertIsNone(await voice_utils.make_voice_note("Hello", tmp))

    async def test_missing_ffmpeg_returns_false(self):
        with patch.object(voice_utils.shutil, "which", return_value=None):
            self.assertFalse(await voice_utils.convert_to_ogg_opus("a.mp3", "a.ogg"))
            self.assertIn("ffmpeg (install it and add it to PATH)", voice_utils.missing_dependencies())

    async def test_empty_text_is_not_spoken(self):
        self.assertFalse(await voice_utils.generate_voice("   ", "x.mp3"))

    async def test_transcribe_missing_file_returns_none(self):
        self.assertIsNone(await voice_utils.transcribe_audio("/nonexistent/file.ogg"))

    async def test_transcribe_failure_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f, patch.object(
            voice_utils, "_transcribe_blocking", side_effect=RuntimeError("model broken")
        ):
            self.assertIsNone(await voice_utils.transcribe_audio(f.name))

    async def test_transcribe_returns_text(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f, patch.object(
            voice_utils, "_transcribe_blocking", return_value="Yes, we have a table"
        ):
            self.assertEqual(await voice_utils.transcribe_audio(f.name), "Yes, we have a table")


if __name__ == "__main__":
    unittest.main()
