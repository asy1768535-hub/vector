from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.main import handle_request_validation_error
from app.schemas.documents import ImportSessionCreate


def test_import_session_validation_logs_only_safe_field_metadata(caplog):
    test_app = FastAPI()
    test_app.add_exception_handler(
        RequestValidationError,
        handle_request_validation_error,
    )

    @test_app.post("/libraries/{slug}/import-sessions")
    async def create_session(slug: str, body: ImportSessionCreate):
        return {"slug": slug, "file_name": body.file_name}

    secret_file_name = "客户名单-不要写入日志.xlsx"
    secret_extra_key = "SECRET_TOKEN_MUST_NOT_BE_LOGGED"
    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(test_app).post(
            "/libraries/finance/import-sessions",
            json={
                "batch_id": "not-a-uuid",
                "file_name": secret_file_name,
                "size_bytes": 0,
                secret_extra_key: "private-payload-must-not-be-logged",
            },
        )

    assert response.status_code == 422
    assert "body.batch_id" in caplog.text
    assert "body.size_bytes" in caplog.text
    assert "uuid_parsing" in caplog.text
    assert "greater_than" in caplog.text
    assert secret_file_name not in caplog.text
    assert "not-a-uuid" not in caplog.text
    assert secret_extra_key not in caplog.text
    assert "private-payload-must-not-be-logged" not in caplog.text
    assert "body.unknown_field" in caplog.text


def test_other_route_validation_is_not_logged_as_an_upload_failure(caplog):
    test_app = FastAPI()
    test_app.add_exception_handler(
        RequestValidationError,
        handle_request_validation_error,
    )

    @test_app.post("/other")
    async def other(body: ImportSessionCreate):
        return body

    with caplog.at_level(logging.WARNING, logger="app.main"):
        response = TestClient(test_app).post("/other", json={})

    assert response.status_code == 422
    assert "import_session_validation_failed" not in caplog.text
