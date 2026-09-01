from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, settings
from app.services.import_uploads import ImportUploadError, staging_path


OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
OLE_HEADER_BYTES = 512


class DocConversionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DocConversionArtifact:
    path: Path
    sha256: str
    converter_version: str


def converted_staging_path(
    staging_key: str,
    config: Settings = settings,
) -> Path:
    try:
        source = staging_path(staging_key, config)
    except ImportUploadError as exc:
        raise DocConversionError("invalid DOC staging key") from exc
    return source.with_name(f"{source.name.removesuffix('.upload')}.converted.docx")


def validate_doc_source(path: Path, *, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocConversionError("DOC source is unavailable") from exc
    if size > max_bytes:
        raise DocConversionError(f"DOC file exceeds {max_bytes} bytes")
    if size < OLE_HEADER_BYTES:
        raise DocConversionError("DOC OLE header is truncated")
    try:
        with path.open("rb") as handle:
            header = handle.read(OLE_HEADER_BYTES)
    except OSError as exc:
        raise DocConversionError("DOC source is unavailable") from exc
    if header[:8] != OLE_MAGIC:
        raise DocConversionError("DOC file does not have an OLE compound-file signature")
    if header[0x1C:0x1E] != b"\xfe\xff":
        raise DocConversionError("DOC OLE byte order is invalid")
    if int.from_bytes(header[0x1E:0x20], "little") not in {9, 12}:
        raise DocConversionError("DOC OLE sector size is invalid")


def validate_converted_docx(path: Path, *, max_bytes: int | None = None) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocConversionError("LibreOffice did not produce a valid DOCX") from exc
    if max_bytes is not None and size > max_bytes:
        raise DocConversionError(f"converted DOCX exceeds {max_bytes} bytes")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocConversionError("LibreOffice did not produce a valid DOCX") from exc
    if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
        raise DocConversionError("LibreOffice did not produce a valid DOCX")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def libreoffice_version(config: Settings = settings) -> str:
    try:
        result = subprocess.run(
            [config.doc_converter_binary, "--headless", "--version"],
            capture_output=True,
            text=True,
            timeout=min(30, config.doc_conversion_timeout_seconds),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocConversionError("LibreOffice version is unavailable") from exc
    version = (result.stdout or result.stderr or "").strip().splitlines()
    if result.returncode != 0 or not version:
        raise DocConversionError("LibreOffice version is unavailable")
    return version[0][:128]


def convert_doc(
    source: Path,
    staging_key: str,
    *,
    config: Settings = settings,
) -> DocConversionArtifact:
    validate_doc_source(source, max_bytes=config.doc_conversion_max_bytes)
    destination = converted_staging_path(staging_key, config)
    try:
        destination.unlink(missing_ok=True)
    except OSError as exc:
        raise DocConversionError("stale converted DOCX cannot be removed") from exc

    root = destination.parent
    try:
        with tempfile.TemporaryDirectory(prefix="doc-convert-", dir=root) as temporary:
            workdir = Path(temporary)
            input_path = workdir / "source.doc"
            profile_path = workdir / "profile"
            profile_path.mkdir()
            shutil.copyfile(source, input_path)
            argv = [
                config.doc_converter_binary,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                f"-env:UserInstallation={profile_path.resolve().as_uri()}",
                "--convert-to",
                "docx:Office Open XML Text",
                "--outdir",
                str(workdir),
                str(input_path),
            ]
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=config.doc_conversion_timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DocConversionError("DOC conversion timed out") from exc
            except OSError as exc:
                raise DocConversionError("LibreOffice cannot be started") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "conversion failed").strip()
                raise DocConversionError(detail[:1000])
            generated = workdir / "source.docx"
            validate_converted_docx(
                generated,
                max_bytes=config.doc_conversion_max_bytes,
            )
            os.replace(generated, destination)
    except DocConversionError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise DocConversionError("DOC conversion staging failed") from exc

    try:
        converter_version = libreoffice_version(config)
        digest = sha256_file(destination)
    except DocConversionError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise DocConversionError("converted DOCX cannot be verified") from exc
    return DocConversionArtifact(
        path=destination,
        sha256=digest,
        converter_version=converter_version,
    )
