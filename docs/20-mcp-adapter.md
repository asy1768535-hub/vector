# MCP Adapter

This project exposes an MCP adapter for vector retrieval and controlled file
upload. The adapter runs as a separate process and calls the existing APIs with
one configured service API key. It does not read the database directly, bypass
Casbin, delete documents, or rebuild indexes.

## Runtime Shape

```text
MCP client -> Streamable HTTP /mcp -> MCP adapter -> existing vector service APIs
```

Default endpoint:

```text
http://127.0.0.1:8001/mcp
```

## Configuration

```dotenv
MCP_ADAPTER_BASE_URL=http://127.0.0.1:8100
MCP_ADAPTER_API_KEY=vk_xxx
MCP_ADAPTER_TRANSPORT=streamable-http
MCP_ADAPTER_HTTP_HOST=127.0.0.1
MCP_ADAPTER_HTTP_PORT=8001
MCP_ADAPTER_ALLOWED_HOSTS=["127.0.0.1","localhost"]
MCP_ADAPTER_MAX_UPLOAD_BYTES=10485760
```

Start it from the repository root:

```powershell
.\.venv\Scripts\python.exe -m app.mcp_adapter
```

For an intranet server, keep the adapter bound to loopback and expose it through
an authenticated reverse proxy. If you intentionally bind it to a LAN address,
also set:

```dotenv
MCP_ADAPTER_ALLOW_NON_LOOPBACK_BIND=true
MCP_ADAPTER_HTTP_HOST=0.0.0.0
MCP_ADAPTER_ALLOWED_HOSTS=["your.internal.host","127.0.0.1","localhost"]
```

Do not expose this adapter directly to the public internet. The configured API
key is process-wide, so every MCP caller gets the same library permissions.

## Client Setup

Point MCP clients at the Streamable HTTP endpoint:

```text
http://your.internal.host:8001/mcp
```

After connection, the client should discover:

```text
search_knowledge
list_permissions
upload_file
```

Typical use from an AI client:

```text
Use search_knowledge with knowledge_id="legal", query="contract risk", top_k=5.
```

For upload, the MCP client reads the local file, base64-encodes its bytes, and
calls `upload_file`. The configured service key must have `insert` permission
on the target library.

The caller does not pass the vector service API key in tool arguments. The MCP
adapter owns that credential through `MCP_ADAPTER_API_KEY`.

## Tools

`search_knowledge`

Searches one authorized library.

Arguments:

- `knowledge_id`: existing library slug.
- `query`: search text.
- `top_k`: 1-100, defaults to 5.
- `score_threshold`: 0.0-1.0, defaults to 0.0.
- `metadata_condition`: optional Dify-compatible metadata filter.

`list_permissions`

Returns the libraries and actions visible to the configured API key. For a
superuser key, this follows the existing `/me/permissions` behavior and may
return an empty list.

`upload_file`

Uploads one file through the existing
`POST /libraries/{knowledge_id}/import-file` API.

Arguments:

- `knowledge_id`: target library slug.
- `filename`: basename ending in `.txt`, `.json`, or `.csv`.
- `content_base64`: standard Base64 encoding of the UTF-8 file bytes.

The adapter rejects empty files, malformed Base64, path-like filenames,
unsupported extensions, and decoded content larger than
`MCP_ADAPTER_MAX_UPLOAD_BYTES` (10 MiB by default, configurable up to 50 MiB).
The vector service still performs authentication, `insert` permission checks,
parsing, deduplication, chunking, and embedding job creation.

For deployments behind a reverse proxy, also set its request-body limit close
to the Base64 payload ceiling (about 14 MiB for the default 10 MiB decoded
limit). This rejects oversized requests before the MCP process parses them.

## Rollback

Stop the MCP adapter process. No migration or persistent state is created.
