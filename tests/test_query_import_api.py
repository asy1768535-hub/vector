from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from fastapi import status

from app.main import app
from app.auth.backend import current_active_user
from app.db import get_db
from app.models.user import User
from app.models.library import Library
from app.schemas.documents import QueryRequest

# Mock user and library
mock_user = User(
    id="00000000-0000-0000-0000-000000000001",
    email="test@example.com",
    is_superuser=True,
    is_active=True,
)
mock_library = Library(
    id="00000000-0000-0000-0000-000000000002",
    slug="testlib",
    name="Test Library",
    qdrant_collection="testlib_col",
    embedding_model="bge-m3",
    embedding_base_url="http://mock-embeddings",
    embedding_dim=1024,
    chunk_size=1000,
    chunk_overlap=120,
)

async def override_user():
    return mock_user

async def override_db():
    return AsyncMock()

@pytest.fixture
def client():
    # Default mock user to superuser
    mock_user.is_superuser = True
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    
    with patch("app.deps.load_active_library", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = mock_library
        yield TestClient(app)
        
    app.dependency_overrides.clear()

def test_query_request_validation():
    # Test valid limit
    req = QueryRequest(query="test query", limit=10)
    assert req.query == "test query"
    assert req.limit == 10

    # Test limit validation constraints (min 1, max 20)
    with pytest.raises(Exception):
        QueryRequest(query="test", limit=0)
    with pytest.raises(Exception):
        QueryRequest(query="test", limit=21)
    with pytest.raises(Exception):
        QueryRequest(query="", limit=5)

@patch("app.services.embedding.embed_one", new_callable=AsyncMock)
@patch("app.services.qdrant.search", new_callable=AsyncMock)
def test_query_library_endpoint(mock_search, mock_embed, client):
    mock_embed.return_value = [0.1] * 1024
    mock_search.return_value = [
        {
            "id": "123",
            "score": 0.85,
            "payload": {
                "text": "matched chunk text",
                "document_id": "doc123",
                "chunk_id": "chunk123",
                "title": "doc title",
                "extra": "value"
            }
        }
    ]

    response = client.post(
        "/libraries/testlib/query",
        json={"query": "test query", "limit": 5}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "results" in data
    assert len(data["results"]) == 1
    assert data["results"][0]["text"] == "matched chunk text"
    assert data["results"][0]["similarity"] == 0.85
    assert data["results"][0]["document_id"] == "doc123"
    assert data["results"][0]["metadata"] == {"extra": "value"}

@patch("app.deps.has_permission")
def test_query_library_unauthorized(mock_has_perm, client):
    # Set user to non-superuser to trigger permission checks
    mock_user.is_superuser = False
    mock_has_perm.return_value = False

    response = client.post(
        "/libraries/testlib/query",
        json={"query": "test query", "limit": 5}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_txt_file(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000003"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 1, False)

    file_content = b"This is plain text content."
    files = {"file": ("test.txt", file_content, "text/plain")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 1
    assert data["documents"][0]["title"] == "test.txt"
    assert data["documents"][0]["chunk_count"] == 1
    
    mock_ingest.assert_called_once()
    kwargs = mock_ingest.call_args[1]
    assert kwargs["text"] == "This is plain text content."
    assert kwargs["title"] == "test.txt"

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_json_file_list(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000004"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 2, False)

    json_data = [
        {"title": "Doc 1", "text": "First doc text", "external_id": "ext1"},
        {"title": "Doc 2", "text": "Second doc text", "external_id": "ext2"}
    ]
    file_content = json.dumps(json_data).encode("utf-8")
    files = {"file": ("docs.json", file_content, "application/json")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 2
    assert data["documents"][0]["title"] == "Doc 1"
    assert data["documents"][1]["title"] == "Doc 2"
    assert mock_ingest.call_count == 2

@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
def test_import_csv_file(mock_ingest, client):
    mock_doc = AsyncMock()
    mock_doc.id = "00000000-0000-0000-0000-000000000005"
    mock_doc.status = "pending"
    mock_job = AsyncMock()
    mock_ingest.return_value = (mock_doc, mock_job, 3, False)

    csv_data = "title,text,external_id\nTitle A,Text A,extA\nTitle B,Text B,extB"
    file_content = csv_data.encode("utf-8")
    files = {"file": ("data.csv", file_content, "text/csv")}
    response = client.post("/libraries/testlib/import-file", files=files)
    
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["status"] == "success"
    assert data["imported_count"] == 2
    assert data["documents"][0]["title"] == "Title A"
    assert data["documents"][1]["title"] == "Title B"
    assert mock_ingest.call_count == 2
