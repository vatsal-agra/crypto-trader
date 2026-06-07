"""Voice transcription using OpenAI Whisper."""

import logging
import os

logger = logging.getLogger(__name__)


async def transcribe_voice(audio_path: str) -> str:
    """Transcribe an audio file (OGG/MP3/WAV) to text via OpenAI Whisper.

    Raises:
        RuntimeError: if OPENAI_API_KEY is not set or the API call fails.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)

    try:
        with open(audio_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )
        text = transcript.strip() if isinstance(transcript, str) else str(transcript).strip()
        logger.info("Whisper transcribed: %s", text)
        return text
    except Exception as exc:
        logger.error("Whisper transcription failed: %s", exc)
        raise RuntimeError(f"Transcription failed: {exc}") from exc
