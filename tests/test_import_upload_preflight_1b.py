from __future__ import annotations

import asyncio
import builtins
import io
import struct
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql

from app.api import admin_jobs
from app.api import import_uploads as import_uploads_api
from app.schemas.documents import ImportSessionCreate
from app.services.import_upload_preflight import inspect_office_upload
from app.services import import_uploads


_PREFLIGHT_PREFIX = "upload_preflight:v1:"


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        import_staging_dir=str(tmp_path),
        import_staging_max_file_bytes=1024 * 1024,
        import_selection_max_files=100,
        doc_conversion_max_bytes=1024 * 1024,
        import_upload_retry_after_seconds=2,
        import_upload_chunk_bytes=32,
        import_upload_claim_stale_seconds=300,
        import_upload_claim_heartbeat_seconds=30,
        import_upload_global_inflight_limit=10,
        import_upload_user_inflight_limit=1,
        import_worker_max_attempts=3,
    )


def _payload(file_name: str) -> ImportSessionCreate:
    return ImportSessionCreate(
        batch_id=uuid.uuid4(),
        file_name=file_name,
        relative_path=f"folder/{file_name}",
        size_bytes=16,
    )


def _preflight(path: Path, file_name: str) -> object:
    preflight = getattr(import_uploads, "preflight_completed_upload", None)
    assert callable(preflight), "1B shared completed-upload preflight owner is missing"
    return preflight(path, file_name)


def _suggested_extension(exc: import_uploads.ImportUploadError) -> str | None:
    suggestion = getattr(exc, "suggested_extension", None)
    if suggestion is not None:
        return suggestion
    details = getattr(exc, "details", None)
    return details.get("suggested_extension") if isinstance(details, dict) else None


def _write_ooxml(path: Path, *members: str) -> None:
    content_types = {
        "word/document.xml": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        ),
        "xl/workbook.xml": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet.main+xml"
        ),
        "ppt/presentation.xml": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml"
        ),
    }
    overrides = "".join(
        f'<Override PartName="/{member}" '
        f'ContentType="{content_types.get(member, "application/octet-stream")}" />'
        for member in members
        if member != "[Content_Types].xml"
    )
    entries = {member: b"<root />" for member in members}
    if "[Content_Types].xml" in entries:
        entries["[Content_Types].xml"] = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f"{overrides}</Types>"
        ).encode("ascii")
    _write_zip_entries(path, entries)


def _write_zip_entries(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for member, content in entries.items():
            archive.writestr(member, content)


def _first_zip_member_data_offset(content: bytearray) -> int:
    local_header = content.index(b"PK\x03\x04")
    name_size, extra_size = struct.unpack_from("<HH", content, local_header + 26)
    return local_header + 30 + name_size + extra_size


def _directory_entry(
    name: str,
    *,
    object_type: int,
    right_sibling: int = 0xFFFFFFFF,
    child: int = 0xFFFFFFFF,
) -> bytes:
    entry = bytearray(128)
    encoded_name = (name + "\x00").encode("utf-16le")
    assert len(encoded_name) <= 64
    entry[: len(encoded_name)] = encoded_name
    struct.pack_into("<H", entry, 64, len(encoded_name))
    entry[66] = object_type
    entry[67] = 1
    struct.pack_into("<III", entry, 68, 0xFFFFFFFF, right_sibling, child)
    struct.pack_into("<I", entry, 116, 0xFFFFFFFE)
    struct.pack_into("<Q", entry, 120, 0)
    return bytes(entry)


def _write_cfb(path: Path, *stream_names: str) -> None:
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 40, 0)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 0)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)
    struct.pack_into("<I", header, 72, 0)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4, 0xFFFFFFFF)
    struct.pack_into("<I", header, 76, 1)

    entries = [
        _directory_entry(
            "Root Entry",
            object_type=5,
            child=1 if stream_names else 0xFFFFFFFF,
        )
    ]
    for index, name in enumerate(stream_names, start=1):
        right_sibling = index + 1 if index < len(stream_names) else 0xFFFFFFFF
        entries.append(
            _directory_entry(
                name,
                object_type=2,
                right_sibling=right_sibling,
            )
        )
    directory_sector = b"".join(entries).ljust(512, b"\x00")
    assert len(directory_sector) == 512

    fat_sector = bytearray(b"\xff" * 512)
    struct.pack_into("<I", fat_sector, 0, 0xFFFFFFFE)
    struct.pack_into("<I", fat_sector, 4, 0xFFFFFFFD)
    path.write_bytes(bytes(header) + directory_sector + bytes(fat_sector))


class _NoDatabaseWork:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, *_args, **_kwargs):
        self.calls.append("execute")
        return self

    def scalars(self):
        return self

    def first(self):
        return None

    def scalar_one(self) -> int:
        return 0

    def add(self, *_args, **_kwargs) -> None:
        self.calls.append("add")

    async def flush(self) -> None:
        self.calls.append("flush")


@pytest.mark.parametrize(
    ("file_name", "expected_code"),
    [
        ("._report.docx", "metadata_file"),
        ("~$budget.xlsx", "office_lock_file"),
    ],
)
def test_ignored_filename_is_rejected_before_database_or_staging(
    file_name: str,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _NoDatabaseWork()
    staging_calls: list[bool] = []

    def unexpected_staging(*_args, **_kwargs):
        staging_calls.append(True)
        raise AssertionError("ignored files must not reserve staging")

    monkeypatch.setattr(import_uploads, "staging_root", unexpected_staging)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.create_session(
                db,
                library=SimpleNamespace(
                    id=uuid.uuid4(),
                    import_max_file_bytes=None,
                    import_max_files_per_selection=None,
                ),
                user=SimpleNamespace(id=uuid.uuid4()),
                payload=_payload(file_name),
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == expected_code
    assert db.calls == []
    assert staging_calls == []


@pytest.mark.parametrize(
    ("extension", "kind"),
    [
        (".docx", "docx"),
        (".xlsx", "xlsx"),
        (".pptx", "pptx"),
        (".doc", "doc"),
        (".xls", "xls"),
    ],
)
def test_completed_upload_preflight_accepts_matching_office_container(
    extension: str,
    kind: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"matching{extension}"
    if kind == "docx":
        _write_ooxml(path, "[Content_Types].xml", "word/document.xml")
    elif kind == "xlsx":
        _write_ooxml(path, "[Content_Types].xml", "xl/workbook.xml")
    elif kind == "pptx":
        _write_ooxml(path, "[Content_Types].xml", "ppt/presentation.xml")
    elif kind == "doc":
        _write_cfb(path, "WordDocument")
    else:
        _write_cfb(path, "Workbook")

    _preflight(path, path.name)


@pytest.mark.parametrize(
    ("actual_kind", "claimed_extension", "suggested_extension"),
    [
        ("docx", ".xlsx", ".docx"),
        ("xlsx", ".docx", ".xlsx"),
        ("pptx", ".docx", ".pptx"),
        ("doc", ".xlsx", ".doc"),
        ("xls", ".doc", ".xls"),
    ],
)
def test_completed_upload_preflight_reports_deterministic_signature_mismatch(
    actual_kind: str,
    claimed_extension: str,
    suggested_extension: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"wrong{claimed_extension}"
    if actual_kind == "docx":
        _write_ooxml(path, "[Content_Types].xml", "word/document.xml")
    elif actual_kind == "xlsx":
        _write_ooxml(path, "[Content_Types].xml", "xl/workbook.xml")
    elif actual_kind == "pptx":
        _write_ooxml(path, "[Content_Types].xml", "ppt/presentation.xml")
    elif actual_kind == "doc":
        _write_cfb(path, "WordDocument")
    else:
        _write_cfb(path, "Workbook")

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_mismatch"
    assert _suggested_extension(exc_info.value) == suggested_extension
    assert "扩展名" in str(exc_info.value)
    assert suggested_extension in str(exc_info.value)

    with pytest.raises(HTTPException) as http_exc_info:
        import_uploads_api._raise_upload_error(exc_info.value)
    assert http_exc_info.value.detail["code"] == "file_signature_mismatch"
    assert http_exc_info.value.detail["suggested_extension"] == suggested_extension


def test_office_container_disguised_as_another_allowed_type_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "disguised.pdf"
    _write_ooxml(path, "[Content_Types].xml", "xl/workbook.xml")

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_mismatch"
    assert rejection.suggested_extension == ".xlsx"


def test_utf16_ooxml_doctype_is_rejected_before_entity_expansion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entity.xlsx"
    content_types = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE Types ['
        '<!ENTITY main "application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml">'
        ']>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/xl/workbook.xml" ContentType="&main;" />'
        '</Types>'
    ).encode("utf-16")
    _write_zip_entries(
        path,
        {
            "[Content_Types].xml": content_types,
            "xl/workbook.xml": b"<root />",
        },
    )

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


def test_malformed_content_types_xml_is_unconfirmed(tmp_path: Path) -> None:
    path = tmp_path / "malformed.xlsx"
    _write_zip_entries(
        path,
        {
            "[Content_Types].xml": b"<Types",
            "xl/workbook.xml": b"<root />",
        },
    )

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


def test_corrupt_ooxml_compressed_metadata_is_unconfirmed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.xlsx"
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        b"</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", b"<root />")

    data = bytearray(path.read_bytes())
    local_header = data.index(b"PK\x03\x04")
    name_size, extra_size = struct.unpack_from("<HH", data, local_header + 26)
    compressed_data = local_header + 30 + name_size + extra_size
    data[compressed_data + 2] ^= 0xFF
    path.write_bytes(data)

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


@pytest.mark.parametrize(
    "compression",
    [
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    ],
)
def test_supported_ooxml_compressions_preserve_valid_workbooks(
    compression: int,
    tmp_path: Path,
) -> None:
    path = tmp_path / "valid.xlsx"
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        b"</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", b"<root />")

    assert inspect_office_upload(path, path.name) is None


@pytest.mark.parametrize(
    ("compression", "damage_offset", "damage"),
    [
        (zipfile.ZIP_BZIP2, 0, b"\x00\x00\x00"),
        (zipfile.ZIP_LZMA, 2, b"\x00"),
    ],
)
def test_corrupt_ooxml_alternate_compression_is_unconfirmed(
    compression: int,
    damage_offset: int,
    damage: bytes,
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt.xlsx"
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        b"</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", b"<root />")

    content = bytearray(path.read_bytes())
    start = _first_zip_member_data_offset(content) + damage_offset
    content[start : start + len(damage)] = damage
    path.write_bytes(content)

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


def test_ooxml_negative_member_offset_from_corrupt_eocd_is_unconfirmed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-eocd.xlsx"
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        b"</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", b"<root />")

    content = bytearray(path.read_bytes())
    eocd = content.rindex(b"PK\x05\x06")
    central_directory_offset = struct.unpack_from("<I", content, eocd + 16)[0]
    struct.pack_into("<I", content, eocd + 16, central_directory_offset + 10_000)
    path.write_bytes(content)

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


def test_cfb_with_nonzero_header_clsid_is_unconfirmed(tmp_path: Path) -> None:
    path = tmp_path / "malformed.doc"
    _write_cfb(path, "WordDocument")
    content = bytearray(path.read_bytes())
    content[8] = 1
    path.write_bytes(content)

    rejection = inspect_office_upload(path, path.name)

    assert rejection is not None
    assert rejection.code == "file_signature_unconfirmed"


def test_preflight_io_error_is_not_misclassified_as_permanent_format_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"placeholder")

    def fail_open(*_args, **_kwargs):
        raise PermissionError("temporary read failure")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(PermissionError, match="temporary read failure"):
        inspect_office_upload(path, path.name)


@pytest.mark.parametrize("error_type", [OSError, PermissionError])
def test_ooxml_backing_store_error_is_not_misclassified(
    error_type: type[OSError],
    tmp_path: Path,
) -> None:
    path = tmp_path / "report.xlsx"
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
        b"</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", b"<root />")

    content = bytearray(path.read_bytes())
    failure_offset = _first_zip_member_data_offset(content)

    class FaultingReader:
        def __init__(self) -> None:
            self.raw = io.BytesIO(content)

        def read(self, size: int = -1) -> bytes:
            if self.raw.tell() == failure_offset:
                raise error_type("backing store read failed")
            return self.raw.read(size)

        def seek(self, offset: int, whence: int = 0) -> int:
            return self.raw.seek(offset, whence)

        def tell(self) -> int:
            return self.raw.tell()

    with pytest.raises(error_type, match="backing store read failed"):
        inspect_office_upload(path, path.name, handle=FaultingReader())


def test_completed_upload_preflight_reports_encrypted_office_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "encrypted.xlsx"
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "encrypted_office_file"
    message = str(exc_info.value)
    assert "本地" in message
    assert "解密" in message
    assert "重新上传" in message


@pytest.mark.parametrize(
    "container_kind",
    ["unknown_zip", "ambiguous_zip", "unknown_cfb", "ambiguous_cfb"],
)
def test_completed_upload_preflight_never_guesses_ambiguous_or_unknown_container(
    container_kind: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "unconfirmed.docx"
    if container_kind == "unknown_zip":
        _write_ooxml(path, "[Content_Types].xml", "custom/data.bin")
    elif container_kind == "ambiguous_zip":
        _write_ooxml(
            path,
            "[Content_Types].xml",
            "word/document.xml",
            "xl/workbook.xml",
        )
    elif container_kind == "unknown_cfb":
        _write_cfb(path, "UnknownStream")
    else:
        _write_cfb(path, "WordDocument", "Workbook")

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"
    assert _suggested_extension(exc_info.value) is None
    assert "无法确认" in str(exc_info.value)


@pytest.mark.parametrize(
    "damage",
    [
        "truncated",
        "invalid_sector_size",
        "directory_sid_out_of_bounds",
        "directory_chain_cycle",
    ],
)
def test_malformed_cfb_is_unconfirmed_instead_of_guessed(
    damage: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed.doc"
    _write_cfb(path, "WordDocument")
    content = bytearray(path.read_bytes())
    if damage == "truncated":
        content = content[:700]
    elif damage == "invalid_sector_size":
        struct.pack_into("<H", content, 30, 15)
    elif damage == "directory_sid_out_of_bounds":
        struct.pack_into("<I", content, 48, 9999)
    else:
        struct.pack_into("<I", content, 1024, 0)
    path.write_bytes(content)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"
    assert _suggested_extension(exc_info.value) is None


@pytest.mark.parametrize("stream_name", ["EncryptionInfo", "EncryptedPackage"])
def test_single_encryption_stream_is_not_enough_to_claim_encryption(
    stream_name: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "single-encryption-stream.xlsx"
    _write_cfb(path, stream_name)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"
    assert exc_info.value.code != "encrypted_office_file"


@pytest.mark.parametrize(
    "content",
    [
        bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504,
        b"not a container: WordDocument Workbook EncryptionInfo EncryptedPackage",
        b"PK\x03\x04word/document.xml xl/workbook.xml [Content_Types].xml",
    ],
    ids=["ole_magic_only", "forged_stream_names", "damaged_zip"],
)
def test_magic_or_payload_strings_without_valid_container_are_unconfirmed(
    content: bytes,
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged.docx"
    path.write_bytes(content)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"
    assert _suggested_extension(exc_info.value) is None


@pytest.mark.parametrize(
    ("family", "claimed_extension", "content_type"),
    [
        (
            "word",
            ".docx",
            "application/vnd.ms-word.document.macroEnabled.main+xml",
        ),
        (
            "xl",
            ".xlsx",
            "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        ),
        (
            "ppt",
            ".pptx",
            "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
        ),
    ],
)
def test_macro_enabled_ooxml_is_never_accepted_as_macro_free_ooxml(
    family: str,
    claimed_extension: str,
    content_type: str,
    tmp_path: Path,
) -> None:
    main_part = {
        "word": "word/document.xml",
        "xl": "xl/workbook.xml",
        "ppt": "ppt/presentation.xml",
    }[family]
    macro_part = {
        "word": "word/vbaProject.bin",
        "xl": "xl/vbaProject.bin",
        "ppt": "ppt/vbaProject.bin",
    }[family]
    path = tmp_path / f"macro-disguised{claimed_extension}"
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{main_part}" ContentType="{content_type}" />'
        "</Types>"
    ).encode("ascii")
    _write_zip_entries(
        path,
        {
            "[Content_Types].xml": content_types,
            main_part: b"<root />",
            macro_part: b"macro",
        },
    )

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"
    assert _suggested_extension(exc_info.value) is None


class _BudgetedReader:
    def __init__(self, raw, *, maximum_bytes: int) -> None:
        self.raw = raw
        self.maximum_bytes = maximum_bytes
        self.bytes_read = 0

    def read(self, size: int = -1):
        assert size >= 0, "preflight must not request an unbounded read"
        data = self.raw.read(size)
        self.bytes_read += len(data)
        assert self.bytes_read <= self.maximum_bytes, "preflight read budget exceeded"
        return data

    def readinto(self, buffer) -> int:
        read = self.raw.readinto(buffer)
        self.bytes_read += int(read or 0)
        assert self.bytes_read <= self.maximum_bytes, "preflight read budget exceeded"
        return read

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        return self.raw.__exit__(*args)

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


def test_cfb_preflight_has_a_bounded_read_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "large-unknown.doc"
    _write_cfb(path, "UnknownStream")
    with path.open("r+b") as handle:
        handle.seek(8 * 1024 * 1024)
        handle.write(b"\x00")

    maximum_bytes = 256 * 1024
    real_path_open = Path.open
    real_builtin_open = builtins.open

    def wrap(raw, candidate) -> object:
        try:
            is_target = Path(candidate).resolve() == path.resolve()
        except TypeError:
            is_target = False
        return (
            _BudgetedReader(raw, maximum_bytes=maximum_bytes)
            if is_target
            else raw
        )

    def path_open(candidate: Path, *args, **kwargs):
        return wrap(real_path_open(candidate, *args, **kwargs), candidate)

    def builtin_open(candidate, *args, **kwargs):
        return wrap(real_builtin_open(candidate, *args, **kwargs), candidate)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(builtins, "open", builtin_open)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        _preflight(path, path.name)

    assert exc_info.value.code == "file_signature_unconfirmed"


class _ScalarResult:
    def __init__(self, value: uuid.UUID | None) -> None:
        self.value = value
        self.rowcount = 1 if value is not None else 0

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self.value


def _updated_value(statement, key: str):
    params = statement.compile().params
    if key in params:
        return params[key]
    for name, value in params.items():
        if name.startswith(f"{key}_m"):
            return value
    return None


class _CompletionDb:
    def __init__(
        self,
        *,
        job_id: uuid.UUID,
        staging_path: Path,
        reject_failed_transition: bool = False,
        fail_cleanup_complete_commit: bool = False,
    ) -> None:
        self.job_id = job_id
        self.staging_path = staging_path
        self.reject_failed_transition = reject_failed_transition
        self.fail_cleanup_complete_commit = fail_cleanup_complete_commit
        self.events: list[tuple[str, object, object, object, bool]] = []
        self.pending_status = None
        self.pending_stage = None
        self.pending_error = None

    async def execute(self, statement):
        status = _updated_value(statement, "status")
        current_stage = _updated_value(statement, "current_stage")
        last_error = _updated_value(statement, "last_error")
        self.pending_status = status
        self.pending_stage = current_stage
        self.pending_error = last_error
        self.events.append(
            (
                "update",
                status,
                current_stage,
                last_error,
                self.staging_path.exists(),
            )
        )
        if status == "failed" and self.reject_failed_transition:
            return _ScalarResult(None)
        return _ScalarResult(self.job_id)

    async def commit(self) -> None:
        self.events.append(
            (
                "commit",
                self.pending_status,
                self.pending_stage,
                self.pending_error,
                self.staging_path.exists(),
            )
        )
        if (
            self.fail_cleanup_complete_commit
            and isinstance(self.pending_error, str)
            and self.pending_error.startswith(
                f"{_PREFLIGHT_PREFIX}cleanup_complete:"
            )
        ):
            raise RuntimeError("injected cleanup_complete commit failure")
        self.pending_status = None
        self.pending_stage = None
        self.pending_error = None

    async def rollback(self) -> None:
        self.events.append(
            ("rollback", None, None, None, self.staging_path.exists())
        )


class _NoopLease:
    def __init__(self) -> None:
        import threading

        self.thread_stop_event = threading.Event()
        self._lost_error = None

    def ensure_current(self) -> None:
        return None

    async def stop_renewal(self) -> None:
        return None


@asynccontextmanager
async def _noop_claim_lease(*_args, **_kwargs):
    yield _NoopLease()


def _complete_claim(tmp_path: Path) -> import_uploads.UploadOperationClaim:
    job_id = uuid.uuid4()
    return import_uploads.UploadOperationClaim(
        job_id=job_id,
        owner_token=f"upload:{uuid.uuid4().hex}:complete:{uuid.uuid4().hex}",
        operation="complete",
        staging_key=f"{job_id.hex}.upload",
        file_name="encrypted.xlsx",
        size_bytes=1536,
        upload_offset=1536,
    )


def test_complete_rejection_commits_failed_then_unlinks_then_marks_cleanup_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _complete_claim(tmp_path)
    path = tmp_path / claim.staging_key
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")
    db = _CompletionDb(job_id=claim.job_id, staging_path=path)
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == "encrypted_office_file"
    updates = [event for event in db.events if event[0] == "update"]
    pending = next(
        event
        for event in updates
        if isinstance(event[3], str)
        and event[3].startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_pending:encrypted_office_file:"
        )
    )
    assert pending[1] == "failed"
    assert pending[2] == "completed"
    pending_commit_index = next(
        index
        for index, event in enumerate(db.events)
        if event[0] == "commit" and event[3] == pending[3]
    )
    complete_update_index = next(
        index
        for index, event in enumerate(db.events)
        if event[0] == "update"
        and isinstance(event[3], str)
        and event[3].startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_complete:encrypted_office_file:"
        )
    )
    assert db.events[pending_commit_index][4] is True
    assert complete_update_index > pending_commit_index
    assert db.events[complete_update_index][4] is False
    assert path.exists() is False


def test_complete_rejection_does_not_unlink_when_failed_transition_loses_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _complete_claim(tmp_path)
    path = tmp_path / claim.staging_key
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")
    db = _CompletionDb(
        job_id=claim.job_id,
        staging_path=path,
        reject_failed_transition=True,
    )
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == "upload_claim_lost"
    assert any(event[0] == "update" and event[1] == "failed" for event in db.events)
    assert path.exists() is True


def test_complete_rejection_keeps_cleanup_pending_when_staging_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _complete_claim(tmp_path)
    path = tmp_path / claim.staging_key
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")
    db = _CompletionDb(job_id=claim.job_id, staging_path=path)
    cleanup_calls: list[str] = []

    async def fail_cleanup(staging_key: str, config=None) -> bool:
        cleanup_calls.append(staging_key)
        return False

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)
    monkeypatch.setattr(import_uploads, "remove_staging_file", fail_cleanup)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == "encrypted_office_file"
    assert cleanup_calls == [claim.staging_key]
    stored_errors = [
        event[3]
        for event in db.events
        if event[0] == "update" and isinstance(event[3], str)
    ]
    assert any(
        error.startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_pending:encrypted_office_file:"
        )
        for error in stored_errors
    )
    assert not any(
        error.startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_complete:encrypted_office_file:"
        )
        for error in stored_errors
    )
    assert path.exists() is True


def test_complete_rejection_keeps_cleanup_pending_when_final_marker_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _complete_claim(tmp_path)
    path = tmp_path / claim.staging_key
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")
    db = _CompletionDb(
        job_id=claim.job_id,
        staging_path=path,
        fail_cleanup_complete_commit=True,
    )
    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _noop_claim_lease)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.complete_claimed_upload(
                db,
                claim=claim,
                config=_config(tmp_path),
            )
        )

    assert exc_info.value.code == "encrypted_office_file"
    assert path.exists() is False
    assert any(
        event[0] == "commit"
        and isinstance(event[3], str)
        and event[3].startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_pending:encrypted_office_file:"
        )
        and event[4] is True
        for event in db.events
    )
    assert any(
        event[0] == "commit"
        and isinstance(event[3], str)
        and event[3].startswith(
            f"{_PREFLIGHT_PREFIX}cleanup_complete:encrypted_office_file:"
        )
        and event[4] is False
        for event in db.events
    )
    assert db.events[-1][0] == "rollback"


def test_legacy_complete_upload_uses_the_same_content_preflight(
    tmp_path: Path,
) -> None:
    job_id = uuid.uuid4()
    staging_key = f"{job_id.hex}.upload"
    path = tmp_path / staging_key
    _write_cfb(path, "EncryptionInfo", "EncryptedPackage")
    job = SimpleNamespace(
        id=job_id,
        status="uploading",
        current_stage="uploading",
        upload_offset=path.stat().st_size,
        size_bytes=path.stat().st_size,
        staging_key=staging_key,
        file_name="encrypted.xlsx",
        sha256=None,
        upload_completed_at=None,
    )

    class Db:
        async def flush(self) -> None:
            return None

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(import_uploads.complete_upload(Db(), job=job, config=_config(tmp_path)))

    assert exc_info.value.code == "encrypted_office_file"
    assert "本地" in str(exc_info.value)
    assert "解密" in str(exc_info.value)


def _preflight_failed_job(*, code: str, message: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        file_name="encrypted.xlsx",
        relative_path="folder/encrypted.xlsx",
        size_bytes=1536,
        upload_offset=1536,
        status="failed",
        current_stage="completed",
        attempt_count=0,
        last_error=(
            f"{_PREFLIGHT_PREFIX}cleanup_complete:{code}:{message}"
        ),
        result_operation=None,
        document_id=None,
        document_revision_id=None,
        embedding_job_id=None,
        created_at=now,
        updated_at=now,
        upload_completed_at=None,
        claimed_at=None,
        finished_at=now,
        worker_id=None,
        staging_key=f"{uuid.uuid4().hex}.upload",
        conversion_sha256=None,
        graph_extraction_requested=False,
    )


def test_complete_retry_restores_original_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Office 文件已加密，请在本地解密后重新上传"
    job = _preflight_failed_job(code="encrypted_office_file", message=message)

    async def no_lock(_db) -> None:
        return None

    async def get_job(*_args, **_kwargs):
        return job

    monkeypatch.setattr(import_uploads, "_lock_upload_claim_capacity", no_lock)
    monkeypatch.setattr(import_uploads, "get_owned_job", get_job)

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.claim_upload_operation(
                SimpleNamespace(),
                library_id=job.library_id,
                job_id=job.id,
                user=SimpleNamespace(id=job.requested_by_user_id, is_superuser=False),
                operation="complete",
                config=_config(Path(".")),
            )
        )

    assert exc_info.value.code == "encrypted_office_file"
    assert str(exc_info.value) == message


def test_preflight_failure_is_not_exposed_as_retryable_import() -> None:
    safe_message = "文件签名与扩展名不匹配，请改为 .xlsx 后重新上传"
    job = _preflight_failed_job(
        code="file_signature_mismatch",
        message=safe_message,
    )

    projection = import_uploads.session_projection(job, config=_config(Path(".")))
    assert projection["retry_target_type"] is None
    assert projection["retry_target_id"] is None
    assert projection["last_error"] == safe_message
    assert _PREFLIGHT_PREFIX not in projection["last_error"]

    class ReadOnlyDb:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("terminal preflight projection needs no query")

    job_read = asyncio.run(import_uploads.job_projection(ReadOnlyDb(), job))
    assert job_read["retry_target_type"] is None
    assert job_read["retry_target_id"] is None
    assert job_read["last_error"] == safe_message
    assert _PREFLIGHT_PREFIX not in job_read["last_error"]

    class Db:
        async def flush(self) -> None:
            raise AssertionError("preflight rejection must not be requeued")

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(import_uploads.retry_job(Db(), job=job, config=_config(Path("."))))

    assert exc_info.value.code == "job_not_retryable"


def test_task_monitor_hides_preflight_storage_prefix_and_disables_retry() -> None:
    safe_message = "文件已加密，请在本地解密后重新上传"
    job = _preflight_failed_job(
        code="encrypted_office_file",
        message=safe_message,
    )

    row = admin_jobs._import_monitor_row(job)

    assert row.last_error == safe_message
    assert _PREFLIGHT_PREFIX not in str(row.model_dump())
    assert row.retryable is False
    assert row.retry_capability != "supported"


@pytest.mark.parametrize(
    "marker",
    (
        import_uploads.EXPIRED_UPLOAD_CLEANUP_PENDING,
        import_uploads.EXPIRED_UPLOAD_CLEANUP_COMPLETE,
    ),
)
def test_expired_cleanup_marker_is_hidden_from_import_projections(marker: str) -> None:
    job = _preflight_failed_job(
        code="encrypted_office_file",
        message="unused",
    )
    job.status = "cancelled"
    job.current_stage = "uploading"
    job.last_error = marker

    projection = import_uploads.session_projection(job, config=_config(Path(".")))
    assert projection["last_error"] == "上传未完成且已过期"
    assert "staging_cleanup:v1:" not in str(projection)

    row = admin_jobs._import_monitor_row(job)
    assert row.last_error == "上传未完成且已过期"
    assert "staging_cleanup:v1:" not in str(row.model_dump())


def test_task_monitor_stats_excludes_preflight_failures_from_retryable_count() -> None:
    statement = admin_jobs._monitor_stats_select(library_id=None)
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "upload_preflight:v1:%" in sql
    assert "document_import_jobs.last_error NOT LIKE" in sql
    assert "preflight_code" in sql
    for code in (
        "metadata_file",
        "office_lock_file",
        "encrypted_office_file",
        "file_signature_mismatch",
        "file_signature_unconfirmed",
    ):
        assert f":{code}:" in sql


def test_staging_quota_excludes_only_cleanup_complete_preflight_failures() -> None:
    engine = create_engine("sqlite://")
    connection = engine.connect()
    library_id = uuid.uuid4()
    try:
        connection.exec_driver_sql(
            "CREATE TABLE document_import_jobs ("
            "library_id CHAR(32), file_name TEXT, size_bytes BIGINT, "
            "status TEXT, current_stage TEXT, last_error TEXT)"
        )
        rows = [
            (
                library_id.hex,
                "pending.txt",
                11,
                "failed",
                "completed",
                f"{_PREFLIGHT_PREFIX}cleanup_pending:encrypted_office_file:pending",
            ),
            (
                library_id.hex,
                "complete.txt",
                13,
                "failed",
                "completed",
                f"{_PREFLIGHT_PREFIX}cleanup_complete:encrypted_office_file:complete",
            ),
            (
                library_id.hex,
                "ordinary.txt",
                19,
                "failed",
                "parsing",
                "ordinary parser failure",
            ),
            (library_id.hex, "uploading.txt", 17, "uploading", "uploading", None),
        ]
        for row in rows:
            connection.exec_driver_sql(
                "INSERT INTO document_import_jobs VALUES (?, ?, ?, ?, ?, ?)", row
            )

        class Db:
            async def execute(self, statement):
                return connection.execute(statement)

        retained_bytes = asyncio.run(
            import_uploads._active_staging_bytes(Db(), library_id=library_id)
        )
    finally:
        connection.close()
        engine.dispose()

    assert retained_bytes == 11 + 19 + 17


def test_1a_complete_claim_and_offset_mismatch_remain_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        status="queued",
        staging_key=f"{uuid.uuid4().hex}.upload",
        file_name="sample.txt",
        size_bytes=7,
        upload_offset=7,
    )

    async def no_lock(_db) -> None:
        return None

    async def get_job(*_args, **_kwargs):
        return job

    monkeypatch.setattr(import_uploads, "_lock_upload_claim_capacity", no_lock)
    monkeypatch.setattr(import_uploads, "get_owned_job", get_job)
    claim = asyncio.run(
        import_uploads.claim_upload_operation(
            SimpleNamespace(),
            library_id=job.library_id,
            job_id=job.id,
            user=SimpleNamespace(id=job.requested_by_user_id, is_superuser=False),
            operation="complete",
            config=_config(Path(".")),
        )
    )
    assert claim.already_queued is True

    job.status = "uploading"
    job.upload_offset = 4
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.claim_upload_operation(
                SimpleNamespace(),
                library_id=job.library_id,
                job_id=job.id,
                user=SimpleNamespace(id=job.requested_by_user_id, is_superuser=False),
                operation="content",
                expected_offset=2,
                config=_config(Path(".")),
            )
        )

    assert exc_info.value.code == "upload_offset_mismatch"
    assert exc_info.value.upload_offset == 4
