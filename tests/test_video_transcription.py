from __future__ import annotations

import pytest

from app.config import Settings
from app.services.video_transcription import (
    VideoTranscriptionError,
    _endpoint,
    _plain_text_transcript,
    _transcript_from_response,
    is_configured,
)


def test_transcription_endpoint_matches_supported_provider_contracts() -> None:
    assert _endpoint("http://asr:10095", "funasr") == "http://asr:10095/transcribe"
    assert _endpoint("http://asr:8000/v1", "openai_compatible") == (
        "http://asr:8000/v1/audio/transcriptions"
    )
    with pytest.raises(VideoTranscriptionError, match="unsupported"):
        _endpoint("http://asr", "unknown")


def test_funasr_plain_response_becomes_a_searchable_transcript() -> None:
    result = _plain_text_transcript(" 会议结论：下周安排培训。\n", max_chars=100)

    assert result.text == "会议结论：下周安排培训。"
    assert result.segments == ()


def test_openai_compatible_response_keeps_timestamp_segments() -> None:
    result = _transcript_from_response(
        {"segments": [{"start": 3, "end": 7.5, "text": "验收已经完成。"}]},
        max_chars=100,
    )

    assert result.text == "验收已经完成。"
    assert result.segments[0].start_seconds == 3
    assert result.segments[0].end_seconds == 7.5


def test_transcription_configuration_rejects_unknown_provider() -> None:
    config = Settings(
        _env_file=None,
        video_transcription_enabled=True,
        video_transcription_provider="unknown",
        video_transcription_base_url="http://asr",
        video_transcription_model="m",
    )

    assert is_configured(config) is False


def test_funasr_does_not_need_a_model_name() -> None:
    config = Settings(
        _env_file=None,
        video_transcription_enabled=True,
        video_transcription_provider="funasr",
        video_transcription_base_url="http://asr:10095",
    )

    assert is_configured(config) is True
