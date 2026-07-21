from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import status
from fastapi.testclient import TestClient

from app.api import documents as documents_api
from app.auth.backend import current_active_user
from app.db import get_db
from app.main import app
from tests.test_query_import_api import make_db_mock, mock_library, override_user


def _client() -> TestClient:
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = lambda: make_db_mock()
    return TestClient(app)


def test_legacy_doc_and_xls_are_not_in_strict_import_whitelist():
    """Phase 2 rule: unsupported legacy Office formats are outside the whitelist."""
    assert ".doc" not in documents_api._SUPPORTED_IMPORT_SUFFIXES
    assert ".xls" not in documents_api._SUPPORTED_IMPORT_SUFFIXES


def test_import_legacy_doc_and_xls_return_415():
    client = _client()
    try:
        with patch("app.deps.load_active_library", new_callable=AsyncMock) as load_lib:
            load_lib.return_value = mock_library
            doc_resp = client.post(
                "/libraries/testlib/import-file",
                files={"file": ("legacy.doc", b"\xd0\xcf\x11\xe0fake-doc", "application/msword")},
            )
            xls_resp = client.post(
                "/libraries/testlib/import-file",
                files={"file": ("legacy.xls", b"\xd0\xcf\x11\xe0fake-xls", "application/vnd.ms-excel")},
            )
        assert doc_resp.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        assert xls_resp.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    finally:
        app.dependency_overrides.clear()
