from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.document_import_job import DocumentImportJob


def test_doc_conversion_migration_extends_the_current_single_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = script.get_revision("0062")

    assert revision is not None
    assert revision.down_revision == "0061"
    assert script.get_heads() == ["0076"]


def test_document_import_job_owns_conversion_state_and_stages():
    columns = DocumentImportJob.__table__.columns
    assert columns["conversion_sha256"].nullable is True
    assert columns["converter_version"].nullable is True
    assert columns["conversion_attempt_count"].nullable is False
    constraints = "\n".join(str(item.sqltext) for item in DocumentImportJob.__table__.constraints if hasattr(item, "sqltext"))
    assert "converting" in constraints
    assert "conversion_ready" in constraints
    assert "conversion_attempt_count >= 0" in constraints


def test_import_limit_migration_allows_the_initial_import_profile():
    source = open("alembic/versions/0061_library_import_limits.py", encoding="utf-8").read()

    assert "BETWEEN 1048576 AND 53687091200" in source
    assert "BETWEEN 1 AND 100000" in source
