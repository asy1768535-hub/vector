from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Literal, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from pydantic import BaseModel

from app.mcp_adapter.client import MCPAdapterError, PublicV1Client
from app.schemas.public_v1 import (
    PublicAnswerRequest,
    PublicAnswerResponse,
    PublicDocumentResponse,
    PublicEntityResponse,
    PublicEntitySearchRequest,
    PublicEntitySearchResponse,
    PublicEvidenceResponse,
    LibrarySlug,
    PublicLibrariesResponse,
    PublicRelationResponse,
    PublicRelationSearchRequest,
    PublicRelationSearchResponse,
    PublicRetrievalRequest,
    PublicRetrievalResponse,
    PublicScopeSelection,
    PublicScopeValidationResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


async def _tool_result(result: Awaitable[ModelT]) -> ModelT:
    try:
        return await result
    except MCPAdapterError as exc:
        raise ToolError(str(exc)) from None


async def _resource_result(result: Awaitable[ModelT]) -> str:
    try:
        model = await result
    except MCPAdapterError as exc:
        raise ResourceError(str(exc)) from None
    return model.model_dump_json()


def _resource_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ResourceError("resource_identifier_invalid") from None


def create_mcp_server(client: PublicV1Client) -> FastMCP:
    server = FastMCP(
        name="Vector Knowledge",
        instructions=(
            "Read authorized knowledge libraries through the stable public v1 "
            "contracts. Select an explicit Library scope or a saved scope for "
            "search, retrieval, and answer operations."
        ),
        stateless_http=True,
        json_response=True,
    )

    @server.tool()
    async def list_libraries() -> PublicLibrariesResponse:
        """List knowledge libraries visible to the configured credential."""
        return await _tool_result(client.list_libraries())

    @server.tool()
    async def validate_scope(
        scope: PublicScopeSelection,
        channels: list[Literal["text", "graph"]] = ["text", "graph"],
    ) -> PublicScopeValidationResponse:
        """Validate one bounded Library scope and its channel compatibility."""
        return await _tool_result(
            client.validate_scope(scope=scope, channels=list(channels))
        )

    @server.tool()
    async def get_document(
        library_slug: LibrarySlug,
        document_id: uuid.UUID,
    ) -> PublicDocumentResponse:
        """Read one authorized document and current Revision metadata."""
        return await _tool_result(client.get_document(library_slug, document_id))

    @server.tool()
    async def get_entity(
        library_slug: LibrarySlug,
        entity_id: uuid.UUID,
    ) -> PublicEntityResponse:
        """Read one Library-scoped entity with bounded Evidence links."""
        return await _tool_result(client.get_entity(library_slug, entity_id))

    @server.tool()
    async def get_relation(
        library_slug: LibrarySlug,
        relation_id: uuid.UUID,
    ) -> PublicRelationResponse:
        """Read one Library-scoped relation with bounded Evidence links."""
        return await _tool_result(client.get_relation(library_slug, relation_id))

    @server.tool()
    async def get_evidence(
        library_slug: LibrarySlug,
        evidence_id: uuid.UUID,
    ) -> PublicEvidenceResponse:
        """Read one Evidence record and its authorized source locator."""
        return await _tool_result(client.get_evidence(library_slug, evidence_id))

    @server.tool()
    async def search_entities(
        request: PublicEntitySearchRequest,
    ) -> PublicEntitySearchResponse:
        """Search entities in one explicit or saved authorized scope."""
        return await _tool_result(client.search_entities(request))

    @server.tool()
    async def search_relations(
        request: PublicRelationSearchRequest,
    ) -> PublicRelationSearchResponse:
        """Search relations in one explicit or saved authorized scope."""
        return await _tool_result(client.search_relations(request))

    @server.tool()
    async def retrieve(
        request: PublicRetrievalRequest,
    ) -> PublicRetrievalResponse:
        """Retrieve bounded source chunks and published graph context."""
        return await _tool_result(client.retrieve(request))

    @server.tool()
    async def answer(
        request: PublicAnswerRequest,
    ) -> PublicAnswerResponse:
        """Generate one grounded answer with bounded sources and graph context."""
        return await _tool_result(client.answer(request))

    @server.resource("vector-kb://libraries")
    async def libraries_resource() -> str:
        """List libraries visible to the configured credential."""
        return await _resource_result(client.list_libraries())

    @server.resource(
        "vector-kb://libraries/{slug}/documents/{document_id}",
        mime_type="application/json",
    )
    async def document_resource(slug: str, document_id: str) -> str:
        """Read an authorized document resource."""
        return await _resource_result(
            client.get_document(slug, _resource_uuid(document_id))
        )

    @server.resource(
        "vector-kb://libraries/{slug}/entities/{entity_id}",
        mime_type="application/json",
    )
    async def entity_resource(slug: str, entity_id: str) -> str:
        """Read an authorized entity resource."""
        return await _resource_result(client.get_entity(slug, _resource_uuid(entity_id)))

    @server.resource(
        "vector-kb://libraries/{slug}/relations/{relation_id}",
        mime_type="application/json",
    )
    async def relation_resource(slug: str, relation_id: str) -> str:
        """Read an authorized relation resource."""
        return await _resource_result(
            client.get_relation(slug, _resource_uuid(relation_id))
        )

    @server.resource(
        "vector-kb://libraries/{slug}/evidence/{evidence_id}",
        mime_type="application/json",
    )
    async def evidence_resource(slug: str, evidence_id: str) -> str:
        """Read an authorized Evidence resource."""
        return await _resource_result(
            client.get_evidence(slug, _resource_uuid(evidence_id))
        )

    return server
