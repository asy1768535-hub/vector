# External Graph Fact Synchronization

M10 synchronizes structured Entity and relation facts from an existing
Library `SyncSource`. It preserves external identity and provenance while
staging every new fact for review. It never publishes automatically.

## Enablement

The feature is default-off and has fail-closed dependencies:

```dotenv
ENABLE_SYNC_SOURCE_API=true
ORGANIZATION_AUTHORIZATION_ENABLED=true
GRAPH_PUBLICATION_ENABLED=true
GRAPH_GOVERNANCE_ENABLED=true
EXTERNAL_GRAPH_SYNC_ENABLED=true
EXTERNAL_GRAPH_SYNC_MAX_BATCH_ITEMS=100
EXTERNAL_GRAPH_SYNC_MAX_ITEM_BYTES=65536
```

Enabling without every dependency fails application startup. Disabling the
feature returns 404 before Library lookup.

## Source Policy

Create the ordinary SyncSource first, then configure graph authority:

```http
PUT /libraries/{slug}/external-graph-sync/sources/{source_key}/policy
Content-Type: application/json

{
  "authority_rank": 20,
  "status": "active",
  "stale_after_seconds": 86400
}
```

Lower ranks are stronger. Manual product edits have implicit rank 0, imported
sources use 1 through 999, and extracted facts have implicit rank 1000. Equal
rank divergence always creates a conflict; arrival order never breaks ties.

Policy writes and conflict reads require Library management authority.

## Batch Contract

```http
POST /libraries/{slug}/external-graph-sync/sources/{source_key}/batches
```

A batch contains 1 through 100 unique external identities and an
`idempotency_key`. Entity items are processed before relation items, so a
relation may reference Entity mappings created in the same batch.

The durable identity is:

```text
(Library, SyncSource, fact_kind, external_type, external_id)
```

The same key and request returns the recorded result. A changed request with the
same key fails with `graph_sync_idempotency_conflict`. When `source_event_id` is
present, it is also unique per Library/source; replaying that event under a new
key returns the original operation.

New facts use `source_type=imported` and `status=pending_review`. An active,
published, manual, stronger-source, or equal-source fact is not overwritten.
Instead the response reports `conflict` and a bounded review record is stored.

## Relations

A relation identifies source and target through active Entity mappings in the
same SyncSource. Missing endpoints fail the transaction. Relation upsert never
creates implicit Entities and never invents cross-Library endpoints.

## Evidence And Structured Provenance

An item may bind to an existing active same-Library `evidence_id`. Without
document Evidence, it may carry a bounded `source_locator` object. Locator keys
for credentials, tokens, passwords, raw text, and quotes are rejected.

Graph Entity/relation detail responses include bounded `external_mappings`.
Public v1 details inherit that additive provenance contract.

## Delete And Snapshot Behavior

Delete tombstones a mapping and may mark only an unpublished imported draft as
deleted. It never hard-deletes a fact. Active/published facts and Entities with
live relation dependencies produce reviewable conflicts and remain unchanged.

For complete snapshots, set both `snapshot_id` and `complete_snapshot=true`.
Mappings not observed in that source snapshot become `stale` and receive an open
`source_snapshot_stale` conflict. Snapshot completion never deletes a fact.

## Read Endpoints

```text
GET /libraries/{slug}/external-graph-sync/sources/{source_key}/mappings
GET /libraries/{slug}/external-graph-sync/conflicts
PATCH /libraries/{slug}/external-graph-sync/conflicts/{conflict_id}
```

List endpoints are bounded to 100 rows. Mapping reads require Library `read`;
conflict reads and explicit `resolved`/`dismissed` decisions require management
authority. Decisions classify the sync conflict only; they do not mutate or
publish the target fact.

## Rollback

Set `EXTERNAL_GRAPH_SYNC_ENABLED=false` to stop all sync routes. Existing facts,
mappings, conflicts, and operations remain available for audit after
re-enablement.

Migration `0041 -> 0040` removes M10 metadata only. Before downgrade, operators
must resolve or explicitly retire any imported drafts created by accepted sync
operations. Downgrade never silently edits those facts.
