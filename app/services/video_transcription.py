"""Video-to-text adapter for the existing import pipeline.

The configured ASR service can be the local FunASR ``POST /transcribe`` API or
an OpenAI-compatible ``POST /audio/transcriptions`` API. Video bytes remain
the source file; only the returned transcript enters the text indexing pipeline.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings, settings

VIDEO_IMPORT_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")
AUDIO_IMPORT_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")
MEDIA_IMPORT_EXTENSIONS = VIDEO_IMPORT_EXTENSIONS + AUDIO_IMPORT_EXTENSIONS


class VideoTranscriptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class VideoTranscript:
    text: str
    segments: tuple[TranscriptSegment, ...]


def is_configured(config: Settings = settings) -> bool:
    provider = str(getattr(config, "video_transcription_provider", "funasr"))
    return bool(
        getattr(config, "video_transcription_enabled", False)
        and provider in {"funasr", "openai_compatible"}
        and str(getattr(config, "video_transcription_base_url", "")).strip()
        and (
            provider == "funasr"
            or str(getattr(config, "video_transcription_model", "")).strip()
        )
    )


def _endpoint(base_url: str, provider: str) -> str:
    raw = base_url.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise VideoTranscriptionError("video transcription endpoint is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VideoTranscriptionError("video transcription endpoint is invalid")
    host = parsed.hostname.lower().rstrip(".")
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    if provider not in {"funasr", "openai_compatible"}:
        raise VideoTranscriptionError("video transcription provider is unsupported")
    suffix = "/transcribe" if provider == "funasr" else "/audio/transcriptions"
    if not path.endswith(suffix):
        path = f"{path}{suffix}"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _segments(payload: object) -> tuple[TranscriptSegment, ...]:
    raw_segments = payload if isinstance(payload, list) else []
    parsed: list[TranscriptSegment] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").replace("\x00", "").strip()
        if not text or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            continue
        parsed.append(TranscriptSegment(start, end, text))
    return tuple(parsed)


def _transcript_from_response(payload: object, *, max_chars: int) -> VideoTranscript:
    if not isinstance(payload, dict):
        raise VideoTranscriptionError("video transcription response is invalid")
    segments = _segments(payload.get("segments"))
    text = str(payload.get("text") or "").replace("\x00", "").strip()
    if not text:
        text = "\n".join(segment.text for segment in segments)
    if not text:
        raise VideoTranscriptionError("video contains no transcribable speech")
    if len(text) > max_chars:
        raise VideoTranscriptionError("video transcript exceeds the configured text limit")
    return VideoTranscript(text=text, segments=segments)


def _plain_text_transcript(text: str, *, max_chars: int) -> VideoTranscript:
    normalized = text.replace("\x00", "").strip()
    if not normalized:
        raise VideoTranscriptionError("video contains no transcribable speech")
    if len(normalized) > max_chars:
        raise VideoTranscriptionError("video transcript exceeds the configured text limit")
    return VideoTranscript(text=normalized, segments=())


def _extract_audio(path: Path, *, config: Settings) -> Path:
    descriptor, output_name = tempfile.mkstemp(prefix=".video-transcript-", suffix=".mp3")
    os.close(descriptor)
    output = Path(output_name)
    try:
        result = subprocess.run(
            [
                config.video_transcription_ffmpeg_binary,
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "32k",
                "-y",
                str(output),
            ],
            capture_output=True,
            check=False,
            timeout=config.video_transcription_extract_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        raise VideoTranscriptionError("video audio extraction failed") from exc
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise VideoTranscriptionError("video contains no extractable audio")
    if output.stat().st_size > config.video_transcription_max_audio_bytes:
        output.unlink(missing_ok=True)
        raise VideoTranscriptionError("extracted audio exceeds the transcription size limit")
    return output


def _transcribe_audio_path(
    audio_path: Path,
    *,
    upload_name: str,
    config: Settings,
) -> VideoTranscript:
    headers = {}
    api_key = config.video_transcription_api_key.get_secret_value()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(config.video_transcription_timeout_seconds, connect=10.0)
    try:
        with audio_path.open("rb") as source, httpx.Client(
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            request_kwargs = {
                "headers": headers,
                "files": {"file": (upload_name, source, "audio/mpeg")},
            }
            if config.video_transcription_provider == "openai_compatible":
                request_kwargs["data"] = {
                    "model": config.video_transcription_model,
                    "response_format": "verbose_json",
                }
            response = client.post(
                _endpoint(
                    config.video_transcription_base_url,
                    config.video_transcription_provider,
                ),
                **request_kwargs,
            )
    except (OSError, httpx.HTTPError) as exc:
        raise VideoTranscriptionError("video transcription service is unavailable") from exc
    if response.status_code != 200:
        raise VideoTranscriptionError(f"video transcription service returned {response.status_code}")
    if config.video_transcription_provider == "funasr":
        return _plain_text_transcript(
            response.text,
            max_chars=config.video_transcription_max_transcript_chars,
        )
    try:
        return _transcript_from_response(
            response.json(),
            max_chars=config.video_transcription_max_transcript_chars,
        )
    except ValueError as exc:
        raise VideoTranscriptionError("video transcription response is invalid") from exc


def transcribe_video(
    path: Path,
    *,
    file_name: str,
    config: Settings = settings,
) -> VideoTranscript:
    """Return transcript text and optional speech timestamps from one video."""
    if not is_configured(config):
        raise VideoTranscriptionError("video transcription is not configured")
    suffix = Path(file_name).suffix.lower()
    if suffix not in VIDEO_IMPORT_EXTENSIONS:
        raise VideoTranscriptionError("unsupported video file type")
    if not path.is_file() or path.is_symlink():
        raise VideoTranscriptionError("video source is unavailable")
    if path.stat().st_size > config.video_transcription_max_input_bytes:
        raise VideoTranscriptionError("video exceeds the transcription size limit")

    audio_path = _extract_audio(path, config=config)
    try:
        return _transcribe_audio_path(
            audio_path,
            upload_name=f"{Path(file_name).stem}.mp3",
            config=config,
        )
    finally:
        audio_path.unlink(missing_ok=True)


def transcribe_audio(
    path: Path,
    *,
    file_name: str,
    config: Settings = settings,
) -> VideoTranscript:
    """Transcribe a supported audio source without a needless ffmpeg round trip."""

    if not is_configured(config):
        raise VideoTranscriptionError("audio transcription is not configured")
    suffix = Path(file_name).suffix.lower()
    if suffix not in AUDIO_IMPORT_EXTENSIONS:
        raise VideoTranscriptionError("unsupported audio file type")
    if not path.is_file() or path.is_symlink():
        raise VideoTranscriptionError("audio source is unavailable")
    if path.stat().st_size > min(
        config.video_transcription_max_input_bytes,
        config.video_transcription_max_audio_bytes,
    ):
        raise VideoTranscriptionError("audio exceeds the transcription size limit")
    return _transcribe_audio_path(path, upload_name=file_name, config=config)
