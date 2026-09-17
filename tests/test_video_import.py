from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.schemas.documents import ImportSessionCreate
from app.services import import_parsing, import_uploads
from app.services.video_transcription import TranscriptSegment, VideoTranscript

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def _library() -> SimpleNamespace:
    return SimpleNamespace(
        chunk_size=200,
        chunk_overlap=20,
        ocr_enabled=False,
        docx_table_aware=False,
    )


def test_media_formats_are_saved_even_when_transcription_is_not_configured() -> None:
    disabled = import_uploads.import_configuration(Settings(_env_file=None))
    enabled = import_uploads.import_configuration(
        Settings(
            _env_file=None,
            video_transcription_enabled=True,
            video_transcription_base_url="http://asr.internal/v1",
            video_transcription_model="whisper-large-v3",
        )
    )

    assert VIDEO_EXTENSIONS.issubset(disabled["allowed_extensions"])
    assert AUDIO_EXTENSIONS.issubset(disabled["allowed_extensions"])
    assert VIDEO_EXTENSIONS.issubset(enabled["allowed_extensions"])
    assert enabled["video_max_file_bytes"] == 500 * 1024 * 1024


def test_unconfigured_media_is_saved_without_sending_it_to_processing() -> None:
    config = Settings(_env_file=None)

    assert import_uploads.media_processing_required("meeting.mp3", 1024, config) is False
    assert import_uploads.media_processing_required("meeting.mp4", 1024, config) is False


def test_archive_planning_skips_videos_when_transcription_is_not_enabled() -> None:
    import zipfile

    video = zipfile.ZipInfo("recording.mp4")
    video.file_size = 20
    video.compress_size = 10
    planned, skipped = import_uploads._validated_archive_entries(
        [video],
        archive_folder="bundle [ZIP]",
        max_files=100,
        max_file_bytes=1024,
        allowed_extensions=frozenset(import_uploads.ALLOWED_IMPORT_EXTENSIONS)
        - frozenset(VIDEO_EXTENSIONS),
    )

    assert planned == []
    assert skipped == ["recording.mp4"]


def test_large_video_is_saved_but_not_sent_to_the_transcription_worker() -> None:
    config = Settings(
        _env_file=None,
        video_transcription_enabled=True,
        video_transcription_base_url="http://asr",
        video_transcription_model="funasr",
        video_transcription_max_input_bytes=2 * 1024 * 1024,
        document_storage_max_read_bytes=1024 * 1024,
    )
    payload = ImportSessionCreate(
        batch_id="11111111-1111-4111-8111-111111111111",
        file_name="meeting.mp4",
        size_bytes=1024 * 1024 + 1,
    )

    assert import_uploads._validate_payload(payload, config) is None
    assert import_uploads.media_processing_required(
        payload.file_name, payload.size_bytes, config
    ) is False


def test_video_parser_turns_timecoded_transcript_into_searchable_segments(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "meeting.mp4"
    path.write_bytes(b"video-fixture")
    monkeypatch.setattr(
        import_parsing.video_transcription,
        "transcribe_video",
        lambda *_args, **_kwargs: VideoTranscript(
            text="项目进度已经完成验收。\n下周安排现场培训。",
            segments=(
                TranscriptSegment(0.0, 4.5, "项目进度已经完成验收。"),
                TranscriptSegment(4.5, 9.0, "下周安排现场培训。"),
            ),
        ),
    )

    result = import_parsing.parse_import_file(path, _library())

    assert "项目进度已经完成验收" in result.normalized_text
    assert result.chunks
    assert result.segments[0]["source_kind"] == "video"
    assert result.segments[0]["location"] == {
        "type": "video",
        "start_seconds": 0.0,
        "end_seconds": 4.5,
    }
    assert result.segments[1]["parser_unit"]["source"]["timestamp"] == {
        "start_seconds": 4.5,
        "end_seconds": 9.0,
    }


def test_video_parser_uses_timecoded_text_as_its_citable_source(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "meeting.webm"
    path.write_bytes(b"video-fixture")
    monkeypatch.setattr(
        import_parsing.video_transcription,
        "transcribe_video",
        lambda *_args, **_kwargs: VideoTranscript(
            text="语音服务提供的整段文本可能含有不可定位的额外内容。",
            segments=(TranscriptSegment(12.0, 15.0, "可定位的会议结论。"),),
        ),
    )

    result = import_parsing.parse_import_file(path, _library())

    assert result.normalized_text == "可定位的会议结论。"
    text_source = result.segments[0]["parser_unit"]["source"]["text"]
    assert text_source["start"] == 0
    assert text_source["end"] == len("可定位的会议结论。")
    assert text_source["ranges"][0]["start"] == 0
    assert text_source["ranges"][0]["end"] == len("可定位的会议结论。")


def test_audio_parser_uses_the_same_transcript_pipeline_without_ffmpeg(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "meeting.mp3"
    path.write_bytes(b"ID3audio-fixture")
    monkeypatch.setattr(
        import_parsing.video_transcription,
        "transcribe_audio",
        lambda *_args, **_kwargs: VideoTranscript(
            text="会议录音：下周安排培训。",
            segments=(),
        ),
    )

    result = import_parsing.parse_import_file(path, _library())

    assert result.normalized_text == "会议录音：下周安排培训。"
    assert result.segments[0]["source_kind"] == "audio"
