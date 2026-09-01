# MCP Knowledge Adapter

The v0.9 MCP adapter exposes the accepted public knowledge API as MCP tools and
resources. It is a separate, default-off process. Read tools use public v1;
optional file upload delegates to the existing permission-checked Library import
API. The adapter never accesses the database or implements authorization, scope
resolution, retrieval, graph traversal, import parsing, or Publication logic.

## Prerequisites

1. Run the main application with `PUBLIC_API_V1_ENABLED=true`.
2. Issue an API key for a user bound to one active Organization.
3. Grant that user `read` permission on only the required Libraries.
4. Install the project dependencies, including the official MCP Python SDK.

Grant `insert` on a Library only when MCP upload is enabled for that process.

Do not put the API key in MCP tool arguments, resource URIs, checked-in client
configuration, or command-line arguments. Supply it through the MCP process
environment.

## Configuration

```dotenv
MCP_ADAPTER_ENABLED=true
MCP_ADAPTER_BASE_URL=http://127.0.0.1:8000
MCP_ADAPTER_API_KEY=<organization-bound-api-key>
MCP_ADAPTER_TIMEOUT_SECONDS=60
MCP_ADAPTER_TRANSPORT=stdio
MCP_ADAPTER_UPLOAD_ENABLED=false
MCP_ADAPTER_MAX_UPLOAD_BYTES=10485760
```

`MCP_ADAPTER_BASE_URL` may include `/api/v1`; the adapter normalizes it exactly
once. Redirects are disabled so the Bearer credential cannot cross an
unexpected redirect boundary.

The adapter refuses to start when disabled or when enabled without a key.
Timeouts are bounded from 1 through 300 seconds.

## Stdio Clients

Configure a Codex-, Claude-, or Cursor-compatible client to start:

```powershell
C:\path\to\vectorDatabase\.venv\Scripts\python.exe -m app.mcp_adapter
```

Set the working directory to the repository root and provide the
`MCP_ADAPTER_*` environment variables in the client's secret/environment
configuration. On initialization the client discovers these tools:

```text
list_libraries
list_permissions
validate_scope
get_document
get_entity
get_relation
get_evidence
search_entities
search_relations
retrieve
search_knowledge
answer
```

`search_knowledge` is the single-Library compatibility entry point. It returns
the same bounded text and published graph context as `retrieve`.

When `MCP_ADAPTER_UPLOAD_ENABLED=true`, discovery also includes:

```text
upload_file
```

Search, retrieval, and answer calls require exactly one bounded scope:
`library_slugs` or `scope_id`. The tool schemas preserve the public v1 limits.

## Resources

Clients can list or read:

```text
vector-kb://libraries
vector-kb://libraries/{slug}/documents/{document_id}
vector-kb://libraries/{slug}/entities/{entity_id}
vector-kb://libraries/{slug}/relations/{relation_id}
vector-kb://libraries/{slug}/evidence/{evidence_id}
```

Each resource calls the same public endpoint as its equivalent tool. There is
no adapter cache or second query path.

## Controlled Upload

`upload_file` accepts `library_slug`, a basename-only `filename`, and standard
Base64 `content_base64`. Supported extensions are `.txt`, `.md`, `.markdown`,
`.json`, `.csv`, `.pdf`, `.docx`, and `.xlsx`. Empty, malformed, path-like,
unsupported, or oversized inputs are rejected before the upstream request.

The configured service user must have `insert` permission on the target
Library. The existing import API remains responsible for authentication,
permission checks, parsing, deduplication, revision storage, chunking, and
embedding job creation. An accepted upload is not searchable until the
Embedding Worker completes its job.

## Streamable HTTP

For a controlled network deployment:

```dotenv
MCP_ADAPTER_TRANSPORT=streamable-http
MCP_ADAPTER_HTTP_HOST=127.0.0.1
MCP_ADAPTER_HTTP_PORT=8001
```

Start the same module. The official SDK serves stateless JSON Streamable HTTP at
`http://127.0.0.1:8001/mcp`.

The adapter rejects non-loopback binds. A reverse proxy must reach it through
loopback and provide authenticated TLS externally. The upstream Organization
API key is process-wide, so do not expose this adapter as a multi-tenant public
endpoint. Non-loopback upstream APIs must use HTTPS.

For systemd, run the adapter independently from the API and workers:

```ini
[Unit]
Description=Vector KB MCP Adapter
After=vector-kb-api.service

[Service]
User=vkb
WorkingDirectory=/opt/vector-kb
EnvironmentFile=/opt/vector-kb/.env
ExecStart=/opt/vector-kb/.venv/bin/python -m app.mcp_adapter
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Errors And Cancellation

Validated public v1 errors retain only their stable error code, request ID, HTTP
status, and bounded reason codes. Raw bodies, URLs, credentials, questions,
answers, source text, and exception details are not included.

Client cancellation propagates to the upstream HTTP request. `/api/v1` remains
responsible for releasing answer leases and recording content-free operation
metadata.

## Rollback

Stop the adapter process or set `MCP_ADAPTER_ENABLED=false`. To remove only the
write surface while retaining reads, set:

```dotenv
MCP_ADAPTER_UPLOAD_ENABLED=false
```

The adapter has no migration or local persistent state. Disabling it does not
alter `/api/v1`, imported documents, or existing clients.
