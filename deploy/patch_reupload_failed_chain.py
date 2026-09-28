"""Apply the re-upload cancellation overlay to the active API image."""

from __future__ import annotations

import sys
from pathlib import Path


target = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("/app/app/services/import_uploads.py")
source = target.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global source
    if source.count(old) != 1:
        raise SystemExit(f"expected {label} exactly once")
    source = source.replace(old, new)


replace_once(
    """    relative_path: str | None = None
    content_type: str | None = None
""",
    """    relative_path: str | None = None
    content_type: str | None = None
    external_id: str | None = None
    replace_document_id: uuid.UUID | None = None
""",
    "UploadOperationClaim source identity fields",
)

claim_fields = """            relative_path=job.relative_path,
            content_type=job.content_type,
"""
replace_once(
    claim_fields,
    """            relative_path=job.relative_path,
            content_type=job.content_type,
            external_id=getattr(job, "external_id", None),
            replace_document_id=getattr(job, "replace_document_id", None),
""",
    "already-queued upload claim source identity",
)
replace_once(
    """        relative_path=job.relative_path,
        content_type=job.content_type,
""",
    """        relative_path=job.relative_path,
        content_type=job.content_type,
        external_id=getattr(job, "external_id", None),
        replace_document_id=getattr(job, "replace_document_id", None),
""",
    "active upload claim source identity",
)

replace_once(
    """                sha256=sha256,
                lease=lease,
            ):
""",
    """                sha256=sha256,
                lease=lease,
                config=config,
            ):
""",
    "duplicate-completion config forwarding",
)

replace_once(
    """                    await db.commit()
                    raise

            await lease.stop_renewal()
""",
    """                    prior_staging_keys = await cancel_prior_failed_uploads_for_reupload(
                        db, claim=claim
                    )
                    await db.commit()
                    await remove_reuploaded_failed_staging(
                        prior_staging_keys, config=config
                    )
                    raise

            await lease.stop_renewal()
""",
    "preflight failure cancellation",
)

replace_once(
    """            await db.commit()
    except ObjectStorageError as exc:
""",
    """            prior_staging_keys = await cancel_prior_failed_uploads_for_reupload(
                db, claim=claim
            )
            await db.commit()
            await remove_reuploaded_failed_staging(prior_staging_keys, config=config)
    except ObjectStorageError as exc:
""",
    "normal completion cancellation",
)

helper = '''async def cancel_prior_failed_uploads_for_reupload(
    db: AsyncSession,
    *,
    claim: UploadOperationClaim,
) -> tuple[str, ...]:
    """Cancel a replacement upload's older failed processing chain."""
    if claim.library_id is None or claim.uploaded_by_user_id is None:
        return ()

    if claim.relative_path is not None:
        source_conditions = (
            DocumentImportJob.relative_path == claim.relative_path,
            DocumentImportJob.file_name == claim.file_name,
        )
    elif claim.external_id is not None:
        source_conditions = (
            DocumentImportJob.relative_path.is_(None),
            DocumentImportJob.external_id == claim.external_id,
        )
    else:
        source_conditions = (
            DocumentImportJob.relative_path.is_(None),
            DocumentImportJob.external_id.is_(None),
            DocumentImportJob.file_name == claim.file_name,
        )
    replacement_match = (
        DocumentImportJob.replace_document_id == claim.replace_document_id
        if claim.replace_document_id is not None
        else DocumentImportJob.replace_document_id.is_(None)
    )
    failed_embedding = exists(
        select(EmbeddingJob.id).where(
            EmbeddingJob.id == DocumentImportJob.embedding_job_id,
            EmbeddingJob.status == "failed",
        )
    )
    failed_graph = exists(
        select(GraphExtractionJob.id).where(
            GraphExtractionJob.document_revision_id
            == DocumentImportJob.document_revision_id,
            GraphExtractionJob.status == "failed",
        )
    )
    failed_processing_chain = and_(
        DocumentImportJob.status == "processing",
        DocumentImportJob.current_stage.in_(("embedding", "graph")),
        or_(failed_embedding, failed_graph),
    )
    source_match = (
        DocumentImportJob.id != claim.job_id,
        DocumentImportJob.library_id == claim.library_id,
        DocumentImportJob.requested_by_user_id == claim.uploaded_by_user_id,
        replacement_match,
        *source_conditions,
    )
    prior_rows = (
        await db.execute(
            select(
                DocumentImportJob.id,
                DocumentImportJob.staging_key,
                DocumentImportJob.embedding_job_id,
                DocumentImportJob.document_revision_id,
            ).where(
                *source_match,
                or_(
                    DocumentImportJob.status == "failed",
                    failed_processing_chain,
                ),
            )
        )
    ).all()
    if not prior_rows:
        return ()

    prior_job_ids = tuple(row.id for row in prior_rows)
    prior_staging_keys = tuple(row.staging_key for row in prior_rows)
    embedding_job_ids = tuple(
        job_id for row in prior_rows if (job_id := row.embedding_job_id) is not None
    )
    document_revision_ids = tuple(
        revision_id
        for row in prior_rows
        if (revision_id := row.document_revision_id) is not None
    )
    now = datetime.now(timezone.utc)
    await db.execute(
        update(DocumentImportJob)
        .where(DocumentImportJob.id.in_(prior_job_ids))
        .values(
            status="cancelled",
            worker_id=None,
            claimed_at=None,
            finished_at=now,
        )
    )
    if embedding_job_ids:
        await db.execute(
            update(EmbeddingJob)
            .where(
                EmbeddingJob.id.in_(embedding_job_ids),
                EmbeddingJob.status == "failed",
            )
            .values(
                status="superseded",
                worker_id=None,
                claimed_at=None,
                finished_at=now,
            )
        )
    if document_revision_ids:
        await db.execute(
            update(GraphExtractionJob)
            .where(
                GraphExtractionJob.document_revision_id.in_(document_revision_ids),
                GraphExtractionJob.status == "failed",
            )
            .values(status="cancelled", finished_at=now)
        )
    return prior_staging_keys


async def remove_reuploaded_failed_staging(
    staging_keys: tuple[str, ...],
    *,
    config: Settings = settings,
) -> None:
    for staging_key in staging_keys:
        if not await remove_staging_file(staging_key, config):
            log.warning(
                "failed to remove superseded import staging file key=%s",
                staging_key,
            )


'''
replace_once(
    """async def _duplicate_upload_target(
""",
    helper + """async def _duplicate_upload_target(
""",
    "re-upload cancellation helpers",
)

replace_once(
    """    sha256: str,
    lease: UploadClaimLease,
) -> bool:
""",
    """    sha256: str,
    lease: UploadClaimLease,
    config: Settings = settings,
) -> bool:
""",
    "duplicate helper signature",
)

replace_once(
    """        await db.commit()
        return True

    target = await _duplicate_upload_target(db, job=job)
""",
    """        prior_staging_keys = await cancel_prior_failed_uploads_for_reupload(
            db, claim=claim
        )
        await db.commit()
        await remove_reuploaded_failed_staging(prior_staging_keys, config=config)
        return True

    target = await _duplicate_upload_target(db, job=job)
""",
    "duplicate-source cancellation",
)

replace_once(
    """    await db.commit()
    return True


async def skip_duplicate_upload(
""",
    """    prior_staging_keys = await cancel_prior_failed_uploads_for_reupload(
        db, claim=claim
    )
    await db.commit()
    await remove_reuploaded_failed_staging(prior_staging_keys, config=config)
    return True


async def skip_duplicate_upload(
""",
    "duplicate target cancellation",
)

target.write_text(source, encoding="utf-8")
