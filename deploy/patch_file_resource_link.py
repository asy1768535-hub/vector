"""Apply the narrow file-resource FK hotfix inside a release-overlay image."""

from pathlib import Path


target = Path("/app/app/services/import_uploads.py")
source = target.read_text(encoding="utf-8")

sqlalchemy_import = "from sqlalchemy import and_, case, exists, func, or_, select, text, update\n"
if source.count(sqlalchemy_import) != 1:
    raise SystemExit("expected SQLAlchemy import exactly once")
source = source.replace(
    sqlalchemy_import,
    sqlalchemy_import + "from sqlalchemy.dialects.postgresql import insert as pg_insert\n",
)

if source.count("    build_file_resource,\n") != 1:
    raise SystemExit("expected existing file-resource builder import exactly once")

old_link = """            resource.storage_error_code = None

            # Store the final, verified resource in the same transaction that
            # links it to the import job.  The initial ``storing`` record is
            # intentionally committed before writing to object storage, but a
            # missing record must not turn a successfully stored original into
            # a foreign-key failure at the task-link step.
            resource = await db.merge(
                build_file_resource(
                    prepared,
                    library_id=claim.library_id,
                    uploaded_by_user_id=claim.uploaded_by_user_id,
                    file_name=claim.file_name,
                    relative_path=claim.relative_path,
                    resource_id=resource_id,
                )
            )
            await db.flush()

            resource_link_values = {
"""
new_link = """            resource.storage_error_code = None

            # Store the final, verified resource in the same transaction that
            # links it to the import job.  The initial ``storing`` record is
            # intentionally committed before writing to object storage, but a
            # missing record must not turn a successfully stored original into
            # a foreign-key failure at the task-link step.  This is an atomic
            # Postgres upsert, not ORM merge: an identity-map entry can make
            # merge emit only UPDATE even when the database row was deleted.
            resource = build_file_resource(
                prepared,
                library_id=claim.library_id,
                uploaded_by_user_id=claim.uploaded_by_user_id,
                file_name=claim.file_name,
                relative_path=claim.relative_path,
                resource_id=resource_id,
            )
            resource_values = {
                "id": resource.id,
                "library_id": resource.library_id,
                "uploaded_by_user_id": resource.uploaded_by_user_id,
                "file_name": resource.file_name,
                "relative_path": resource.relative_path,
                "content_type": resource.content_type,
                "size_bytes": resource.size_bytes,
                "sha256": resource.sha256,
                "storage_path": resource.storage_path,
                "storage_provider": resource.storage_provider,
                "endpoint_ref": resource.endpoint_ref,
                "bucket": resource.bucket,
                "object_key": resource.object_key,
                "object_version": resource.object_version,
                "etag": resource.etag,
                "immutability_mode": resource.immutability_mode,
                "storage_status": resource.storage_status,
                "storage_verified_at": resource.storage_verified_at,
                "storage_error_code": resource.storage_error_code,
            }
            await db.execute(
                pg_insert(FileResource)
                .values(**resource_values)
                .on_conflict_do_update(
                    index_elements=(FileResource.id,),
                    set_={
                        **{
                            key: value
                            for key, value in resource_values.items()
                            if key != "id"
                        },
                        "updated_at": func.now(),
                    },
                )
            )
            await db.flush()

            resource_link_values = {
"""
if source.count(old_link) != 1:
    raise SystemExit("expected verified-resource link marker exactly once")
target.write_text(source.replace(old_link, new_link), encoding="utf-8")
