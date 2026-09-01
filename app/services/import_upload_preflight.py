from __future__ import annotations

import lzma
import os
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from xml.etree import ElementTree
from xml.parsers import expat


OfficeExtension = Literal[".doc", ".docx", ".xls", ".xlsx", ".pptx"]
OfficePreflightCode = Literal[
    "encrypted_office_file",
    "file_signature_mismatch",
    "file_signature_unconfirmed",
]

OFFICE_EXTENSIONS = frozenset({".doc", ".docx", ".xls", ".xlsx", ".pptx"})

_READ_BUDGET_BYTES = 256 * 1024
_CONTENT_TYPES_LIMIT_BYTES = 64 * 1024
_MAX_ZIP_ENTRIES = 2_048
_MAX_ZIP_NAME_BYTES = 1_024

_CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_CFB_HEADER_BYTES = 512
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_DIFSECT = 0xFFFFFFFC
_MAXREGSECT = 0xFFFFFFFA
_NOSTREAM = 0xFFFFFFFF

_CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
_OOXML_MAIN_PARTS: dict[str, tuple[OfficeExtension, str]] = {
    "word/document.xml": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ),
    "xl/workbook.xml": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ),
    "ppt/presentation.xml": (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ),
}


@dataclass(frozen=True, slots=True)
class OfficeUploadRejection:
    code: OfficePreflightCode
    suggested_extension: OfficeExtension | None = None


class _UnconfirmedContainer(ValueError):
    pass


class _SourceIOError(Exception):
    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


class _BoundedReader:
    def __init__(self, raw: BinaryIO, *, file_size: int) -> None:
        self._raw = raw
        self._file_size = file_size
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        position = self.tell()
        available = max(0, self._file_size - position)
        requested = available if size < 0 else min(size, available)
        if requested > _READ_BUDGET_BYTES - self.bytes_read:
            raise _UnconfirmedContainer("container metadata exceeds the read budget")
        try:
            data = self._raw.read(requested)
        except OSError as exc:
            raise _SourceIOError(exc) from exc
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer: bytearray | memoryview) -> int:
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self.tell() + offset
        elif whence == os.SEEK_END:
            target = self._file_size + offset
        else:
            raise ValueError("invalid seek mode")
        if target < 0 or target > self._file_size:
            raise _UnconfirmedContainer("container seek is out of bounds")
        try:
            position = self._raw.seek(offset, whence)
        except OSError as exc:
            raise _SourceIOError(exc) from exc
        if position != target:
            raise _UnconfirmedContainer("container seek returned an invalid position")
        return position

    def tell(self) -> int:
        try:
            return self._raw.tell()
        except OSError as exc:
            raise _SourceIOError(exc) from exc

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self._file_size:
            raise _UnconfirmedContainer("container metadata is truncated")
        self.seek(offset)
        data = self.read(size)
        if len(data) != size:
            raise _UnconfirmedContainer("container metadata is truncated")
        return data

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


def inspect_office_upload(
    path: Path,
    file_name: str,
    *,
    handle: BinaryIO | None = None,
) -> OfficeUploadRejection | None:
    """Return a stable rejection for an Office upload without reading document bodies."""
    claimed_extension = Path(file_name).suffix.lower()
    claimed_as_office = claimed_extension in OFFICE_EXTENSIONS

    try:
        actual_extension = _classify_office_container(path, handle=handle)
    except (
        ElementTree.ParseError,
        EOFError,
        NotImplementedError,
        RuntimeError,
        UnicodeError,
        ValueError,
        expat.ExpatError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        actual_extension = None

    if actual_extension == "encrypted":
        return OfficeUploadRejection("encrypted_office_file")
    if actual_extension is None:
        return (
            OfficeUploadRejection("file_signature_unconfirmed")
            if claimed_as_office
            else None
        )
    if actual_extension != claimed_extension:
        return OfficeUploadRejection(
            "file_signature_mismatch",
            suggested_extension=actual_extension,
        )
    return None


def _classify_office_container(
    path: Path,
    *,
    handle: BinaryIO | None = None,
) -> OfficeExtension | Literal["encrypted"] | None:
    if handle is not None:
        position = handle.tell()
        try:
            return _classify_office_stream(handle)
        finally:
            handle.seek(position)
    with path.open("rb") as raw:
        return _classify_office_stream(raw)


def _classify_office_stream(
    raw: BinaryIO,
) -> OfficeExtension | Literal["encrypted"] | None:
    raw.seek(0, os.SEEK_END)
    file_size = raw.tell()
    raw.seek(0)
    reader = _BoundedReader(raw, file_size=file_size)
    try:
        header = reader.read(min(_CFB_HEADER_BYTES, file_size))
        if header.startswith(_CFB_MAGIC):
            return _classify_cfb(reader, header, file_size=file_size)
        if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return _classify_ooxml(reader)
        return None
    except _SourceIOError as exc:
        raise exc.error from exc


def _classify_ooxml(reader: _BoundedReader) -> OfficeExtension | None:
    with zipfile.ZipFile(reader, mode="r") as archive:
        entries = archive.infolist()
        if not entries or len(entries) > _MAX_ZIP_ENTRIES:
            raise _UnconfirmedContainer("invalid OOXML entry count")

        by_name: dict[str, zipfile.ZipInfo] = {}
        folded_names: set[str] = set()
        for entry in entries:
            name = entry.filename
            if (
                not name
                or len(name.encode("utf-8", errors="surrogatepass")) > _MAX_ZIP_NAME_BYTES
                or "\\" in name
                or "\x00" in name
                or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
                or entry.flag_bits & 0x1
            ):
                raise _UnconfirmedContainer("unsafe OOXML entry")
            folded_name = name.casefold()
            if folded_name in folded_names:
                raise _UnconfirmedContainer("duplicate OOXML entry")
            folded_names.add(folded_name)
            by_name[name] = entry

        content_types_entry = by_name.get("[Content_Types].xml")
        if content_types_entry is None or content_types_entry.is_dir():
            return None
        if content_types_entry.file_size > _CONTENT_TYPES_LIMIT_BYTES:
            raise _UnconfirmedContainer("OOXML content types metadata is too large")

        try:
            with archive.open(content_types_entry, mode="r") as source:
                content_types = source.read(_CONTENT_TYPES_LIMIT_BYTES + 1)
        except (lzma.LZMAError, OSError) as exc:
            raise _UnconfirmedContainer("invalid OOXML compressed metadata") from exc
        if len(content_types) > _CONTENT_TYPES_LIMIT_BYTES:
            raise _UnconfirmedContainer("OOXML content types metadata is too large")
        def reject_doctype(*_args) -> None:
            raise _UnconfirmedContainer("OOXML content types metadata contains a DTD")

        xml_guard = expat.ParserCreate()
        xml_guard.StartDoctypeDeclHandler = reject_doctype
        xml_guard.EntityDeclHandler = reject_doctype
        xml_guard.ExternalEntityRefHandler = reject_doctype
        xml_guard.Parse(content_types, True)

        root = ElementTree.fromstring(content_types)
        if root.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Types":
            raise _UnconfirmedContainer("invalid OOXML content types root")

        overrides: dict[str, str] = {}
        for element in root:
            if element.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Override":
                continue
            part_name = element.attrib.get("PartName", "")
            content_type = element.attrib.get("ContentType", "")
            if not part_name.startswith("/") or not content_type:
                raise _UnconfirmedContainer("invalid OOXML content type override")
            if part_name in overrides:
                raise _UnconfirmedContainer("duplicate OOXML content type override")
            overrides[part_name] = content_type

        if any("macroenabled" in value.casefold() for value in overrides.values()):
            return None
        if any(
            name.casefold() == "vbaproject.bin" or name.casefold().endswith("/vbaproject.bin")
            for name in by_name
        ):
            return None

        candidates: list[tuple[OfficeExtension, zipfile.ZipInfo]] = []
        for main_part, (extension, expected_content_type) in _OOXML_MAIN_PARTS.items():
            entry = by_name.get(main_part)
            if (
                entry is not None
                and not entry.is_dir()
                and overrides.get(f"/{main_part}") == expected_content_type
            ):
                candidates.append((extension, entry))
        if len(candidates) != 1:
            return None

        extension, main_entry = candidates[0]
        # Opening validates the main part's local header without consuming its body.
        with archive.open(main_entry, mode="r"):
            pass
        return extension


def _classify_cfb(
    reader: _BoundedReader,
    header: bytes,
    *,
    file_size: int,
) -> OfficeExtension | Literal["encrypted"] | None:
    if len(header) != _CFB_HEADER_BYTES:
        raise _UnconfirmedContainer("truncated CFB header")

    minor_version, major_version, byte_order, sector_shift, mini_sector_shift = struct.unpack_from(
        "<HHHHH", header, 24
    )
    del minor_version
    if (
        header[8:24] != b"\x00" * 16
        or byte_order != 0xFFFE
        or mini_sector_shift != 6
        or (major_version, sector_shift) not in {(3, 9), (4, 12)}
        or header[34:40] != b"\x00" * 6
    ):
        raise _UnconfirmedContainer("invalid CFB header")

    sector_size = 1 << sector_shift
    if file_size < sector_size * 2 or file_size % sector_size:
        raise _UnconfirmedContainer("invalid CFB file size")
    sector_count = file_size // sector_size - 1
    if sector_count <= 0 or sector_count >= _MAXREGSECT:
        raise _UnconfirmedContainer("invalid CFB sector count")

    directory_sector_count = _u32(header, 40)
    fat_sector_count = _u32(header, 44)
    first_directory_sid = _u32(header, 48)
    mini_stream_cutoff = _u32(header, 56)
    first_mini_fat_sid = _u32(header, 60)
    mini_fat_sector_count = _u32(header, 64)
    first_difat_sid = _u32(header, 68)
    difat_sector_count = _u32(header, 72)
    if (
        not fat_sector_count
        or fat_sector_count > sector_count
        or mini_stream_cutoff != 4096
        or (major_version == 3 and directory_sector_count != 0)
    ):
        raise _UnconfirmedContainer("invalid CFB allocation metadata")

    fat_sids, difat_sids = _load_difat(
        reader,
        header,
        sector_size=sector_size,
        sector_count=sector_count,
        fat_sector_count=fat_sector_count,
        first_difat_sid=first_difat_sid,
        difat_sector_count=difat_sector_count,
    )
    fat = _load_fat(
        reader,
        fat_sids,
        sector_size=sector_size,
        sector_count=sector_count,
    )
    for sid in fat_sids:
        if fat[sid] != _FATSECT:
            raise _UnconfirmedContainer("invalid CFB FAT marker")
    for sid in difat_sids:
        if fat[sid] != _DIFSECT:
            raise _UnconfirmedContainer("invalid CFB DIFAT marker")

    directory_sids = _walk_sector_chain(
        first_directory_sid,
        fat,
        sector_count=sector_count,
    )
    if major_version == 4 and len(directory_sids) != directory_sector_count:
        raise _UnconfirmedContainer("invalid CFB directory sector count")

    if mini_fat_sector_count:
        mini_fat_sids = _walk_sector_chain(
            first_mini_fat_sid,
            fat,
            sector_count=sector_count,
        )
        if len(mini_fat_sids) != mini_fat_sector_count:
            raise _UnconfirmedContainer("invalid CFB mini FAT sector count")
    elif first_mini_fat_sid != _ENDOFCHAIN:
        raise _UnconfirmedContainer("invalid CFB mini FAT start")
    else:
        mini_fat_sids = []

    allocation_sets = [set(fat_sids), set(difat_sids), set(directory_sids), set(mini_fat_sids)]
    if sum(len(items) for items in allocation_sets) != len(set().union(*allocation_sets)):
        raise _UnconfirmedContainer("overlapping CFB allocation chains")

    directory = b"".join(
        _read_cfb_sector(reader, sid, sector_size=sector_size, sector_count=sector_count)
        for sid in directory_sids
    )
    stream_names = _reachable_cfb_stream_names(directory)
    has_doc = "worddocument" in stream_names
    has_xls = bool({"workbook", "book"} & stream_names)
    has_encryption_info = "encryptioninfo" in stream_names
    has_encrypted_package = "encryptedpackage" in stream_names

    if has_encryption_info and has_encrypted_package and not has_doc and not has_xls:
        return "encrypted"
    if has_encryption_info or has_encrypted_package:
        return None
    if has_doc == has_xls:
        return None
    return ".doc" if has_doc else ".xls"


def _load_difat(
    reader: _BoundedReader,
    header: bytes,
    *,
    sector_size: int,
    sector_count: int,
    fat_sector_count: int,
    first_difat_sid: int,
    difat_sector_count: int,
) -> tuple[list[int], list[int]]:
    header_difat = struct.unpack_from("<109I", header, 76)
    fat_sids: list[int] = []
    padding_started = False
    for sid in header_difat:
        if sid == _FREESECT:
            padding_started = True
            continue
        if padding_started or len(fat_sids) >= fat_sector_count:
            raise _UnconfirmedContainer("invalid CFB header DIFAT")
        _validate_regular_sid(sid, sector_count)
        fat_sids.append(sid)

    if not difat_sector_count:
        if first_difat_sid != _ENDOFCHAIN:
            raise _UnconfirmedContainer("invalid CFB DIFAT start")
        if len(fat_sids) != fat_sector_count or len(set(fat_sids)) != len(fat_sids):
            raise _UnconfirmedContainer("incomplete CFB DIFAT")
        return fat_sids, []

    current_sid = first_difat_sid
    difat_sids: list[int] = []
    entries_per_sector = sector_size // 4 - 1
    for index in range(difat_sector_count):
        _validate_regular_sid(current_sid, sector_count)
        if current_sid in difat_sids:
            raise _UnconfirmedContainer("cyclic CFB DIFAT")
        difat_sids.append(current_sid)
        sector = _read_cfb_sector(
            reader,
            current_sid,
            sector_size=sector_size,
            sector_count=sector_count,
        )
        values = struct.unpack(f"<{entries_per_sector + 1}I", sector)
        padding_started = False
        for sid in values[:-1]:
            if sid == _FREESECT:
                padding_started = True
                continue
            if padding_started or len(fat_sids) >= fat_sector_count:
                raise _UnconfirmedContainer("invalid CFB DIFAT sector")
            _validate_regular_sid(sid, sector_count)
            fat_sids.append(sid)
        next_sid = values[-1]
        if index + 1 == difat_sector_count:
            if next_sid != _ENDOFCHAIN:
                raise _UnconfirmedContainer("unterminated CFB DIFAT")
        else:
            current_sid = next_sid

    if len(fat_sids) != fat_sector_count or len(set(fat_sids)) != len(fat_sids):
        raise _UnconfirmedContainer("invalid CFB FAT sector list")
    return fat_sids, difat_sids


def _load_fat(
    reader: _BoundedReader,
    fat_sids: list[int],
    *,
    sector_size: int,
    sector_count: int,
) -> tuple[int, ...]:
    raw_fat = b"".join(
        _read_cfb_sector(reader, sid, sector_size=sector_size, sector_count=sector_count) for sid in fat_sids
    )
    fat = struct.unpack(f"<{len(raw_fat) // 4}I", raw_fat)
    if len(fat) < sector_count:
        raise _UnconfirmedContainer("CFB FAT does not cover the file")
    return fat


def _walk_sector_chain(
    first_sid: int,
    fat: tuple[int, ...],
    *,
    sector_count: int,
) -> list[int]:
    _validate_regular_sid(first_sid, sector_count)
    chain: list[int] = []
    seen: set[int] = set()
    current_sid = first_sid
    while True:
        _validate_regular_sid(current_sid, sector_count)
        if current_sid in seen:
            raise _UnconfirmedContainer("cyclic CFB sector chain")
        seen.add(current_sid)
        chain.append(current_sid)
        next_sid = fat[current_sid]
        if next_sid == _ENDOFCHAIN:
            return chain
        if len(chain) >= sector_count:
            raise _UnconfirmedContainer("oversized CFB sector chain")
        current_sid = next_sid


def _read_cfb_sector(
    reader: _BoundedReader,
    sid: int,
    *,
    sector_size: int,
    sector_count: int,
) -> bytes:
    _validate_regular_sid(sid, sector_count)
    return reader.read_at((sid + 1) * sector_size, sector_size)


def _validate_regular_sid(sid: int, sector_count: int) -> None:
    if sid >= _MAXREGSECT or sid >= sector_count:
        raise _UnconfirmedContainer("CFB sector is out of bounds")


def _reachable_cfb_stream_names(directory: bytes) -> set[str]:
    if not directory or len(directory) % 128:
        raise _UnconfirmedContainer("invalid CFB directory stream")
    entry_count = len(directory) // 128
    root = _parse_cfb_directory_entry(directory, 0)
    if (
        root.object_type != 5
        or root.name != "Root Entry"
        or root.color != 1
        or root.left_sibling != _NOSTREAM
        or root.right_sibling != _NOSTREAM
    ):
        raise _UnconfirmedContainer("invalid CFB root entry")

    stack = [] if root.child == _NOSTREAM else [root.child]
    visited: set[int] = set()
    stream_names: set[str] = set()
    while stack:
        entry_id = stack.pop()
        if entry_id >= entry_count or entry_id in visited:
            raise _UnconfirmedContainer("invalid CFB directory tree")
        visited.add(entry_id)
        entry = _parse_cfb_directory_entry(directory, entry_id)
        if entry.object_type not in {1, 2} or entry.color not in {0, 1}:
            raise _UnconfirmedContainer("invalid reachable CFB directory entry")
        for sibling in (entry.left_sibling, entry.right_sibling):
            if sibling != _NOSTREAM:
                stack.append(sibling)
        if entry.object_type == 1:
            if entry.child != _NOSTREAM:
                stack.append(entry.child)
        elif entry.child != _NOSTREAM:
            raise _UnconfirmedContainer("CFB stream entry has a child")
        else:
            stream_names.add(entry.name.casefold())
    return stream_names


@dataclass(frozen=True, slots=True)
class _CfbDirectoryEntry:
    name: str
    object_type: int
    color: int
    left_sibling: int
    right_sibling: int
    child: int


def _parse_cfb_directory_entry(directory: bytes, entry_id: int) -> _CfbDirectoryEntry:
    offset = entry_id * 128
    entry = directory[offset : offset + 128]
    if len(entry) != 128:
        raise _UnconfirmedContainer("truncated CFB directory entry")
    name_bytes = struct.unpack_from("<H", entry, 64)[0]
    if (
        name_bytes < 2
        or name_bytes > 64
        or name_bytes % 2
        or entry[name_bytes - 2 : name_bytes] != b"\x00\x00"
    ):
        raise _UnconfirmedContainer("invalid CFB directory entry name")
    name = entry[: name_bytes - 2].decode("utf-16le")
    if not name or "\x00" in name:
        raise _UnconfirmedContainer("invalid CFB directory entry name")
    left_sibling, right_sibling, child = struct.unpack_from("<III", entry, 68)
    return _CfbDirectoryEntry(
        name=name,
        object_type=entry[66],
        color=entry[67],
        left_sibling=left_sibling,
        right_sibling=right_sibling,
        child=child,
    )


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]
