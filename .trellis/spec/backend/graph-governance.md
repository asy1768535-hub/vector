# Graph Governance Actions

## 1. Scope / Trigger

Use this contract for internal graph fact creation, correction, review,
disable/restore, alias, merge, governance Publication planning, and activation.
It applies whenever a write could change the graph visible to retrieval.

The governing invariant is:

```text
submit or decide action -> projected Publication -> explicit activation
                                             |
                                             +-> mutate live facts only here
```

## 2. Signatures

Database revision:

```text
0038
graph_governance_actions
graph_governance_action_items
```

Environment:

```text
GRAPH_GOVERNANCE_ENABLED=false
ORGANIZATION_AUTHORIZATION_ENABLED=true  # required when governance is enabled
GRAPH_PUBLICATION_ENABLED=true           # required when governance is enabled
```

Service boundaries:

```python
submit_manual_entity(...)
submit_manual_relation(...)
submit_entity_correction(...)
submit_relation_correction(...)
submit_alias(...)
review_action(...)
review_relation(...)
stage_entity_status(...)
stage_relation_status(...)
stage_alias_disable(...)
stage_entity_merge(...)
cancel_governance_action(...)
plan_graph_governance_publication(...)
load_graph_governance_projection(...)
apply_loaded_graph_governance_projection(...)
release_graph_governance_publication(...)
```

Internal HTTP prefix:

```text
/libraries/{slug}/graph-governance
```

The surface provides action list/detail, entity/relation/correction/alias
submission, decisions, cancellation, disable/restore, merge, and
`POST /publications/plan`.

## 3. Contracts

- Governance routes are mounted but return `404` before Library lookup while
  disabled. Governance requires Organization authorization and graph
  Publication at startup.
- Submission requires Library `insert`. Decisions, disable/restore, merge, and
  Publication planning require Organization-admin or Library `admin` management
  authority. Governance HTTP operations are Cookie-only.
- An accepted command persists one immutable action and ordered effect items.
  Content may exist in the bounded action/effect payload, but audit targets may
  contain only IDs, kinds, statuses, reason codes, counts, and hashes.
- Every target is fenced by Library, Ontology, type where applicable, and a
  canonical expected-state hash. Services lock Library first, then action/fact
  rows in deterministic order, and never commit.
- Approval changes only action lifecycle. Live Entity, Relation, Alias, and
  Evidence state changes only in the same transaction that activates the exact
  governance Publication.
- A governance Publication binds canonical action IDs and their action-set
  hash in `plan_options` and the manifest. Ordinary Publications omit this
  field and preserve their existing manifest identity.
- Replanning actions already bound to a Publication is allowed only for an
  exact idempotent replay: same idempotency key, returned Publication ID,
  canonical action IDs, and action-set hash. A replay does not emit a second
  planned audit record. Dry-run never bypasses an existing binding.
- Merge is same-Library, same-Ontology, same-Entity-Type only. It chooses an
  explicit survivor, rewrites or disables related facts deterministically,
  preserves Evidence, and never physically deletes the loser.
- Rollback may project preserved `disabled` or `stale` facts back to `active`
  only when their remaining allowlisted state still matches the stored target
  Publication snapshot. Merge and alias history are not split automatically.

## 4. Validation & Error Matrix

| Condition | Stable result |
|---|---|
| Governance disabled | HTTP `404` on governance routes |
| Missing/hidden target or wrong Library scope | `graph_governance_not_found` |
| Missing insert/management authority | `graph_governance_forbidden` |
| Malformed command or action selection | `graph_governance_request_invalid` |
| Same key with different command identity | `graph_governance_idempotency_conflict` |
| Expected row/action state changed | `graph_governance_state_changed` |
| Action is bound to another Publication | `graph_governance_action_in_use` |
| Publication/action IDs or hashes drift | `graph_governance_publication_changed` |
| Cross-scope/type merge | `graph_governance_merge_incompatible` |
| Unresolved deterministic merge collision | `graph_governance_merge_conflict` |
| Stored invariant cannot be trusted | `graph_governance_unavailable` |

Request validation maps to `422`, authorization to `403`, hidden targets to
generic `404`, state/idempotency/merge conflicts to `409`, and invariant
failures to `503`.

## 5. Good / Base / Bad Cases

- Good: an admin stages an Entity disable; retrieval still sees the active
  Entity until the bound governance Publication is explicitly activated.
- Good: a client retries the same governance plan and receives the same
  Publication with `reused=true`, without rebinding actions or duplicating
  audit.
- Base: with governance disabled, ordinary Publication planning, manifest
  hashes, activation, rollback, graph retrieval, and Graph Catalog reads retain
  their prior behavior.
- Bad: changing a live fact during review, accepting an action bound to a
  different Publication, storing names/properties/Evidence text in audit, or
  committing inside a governance service is forbidden.
- Bad: Entity merge across Library, Ontology, or Entity Type boundaries, or
  deleting the loser/evidence physically, is forbidden.

## 6. Tests Required

- Assert ORM/migration parity, constraints, indexes, one Alembic head, and
  `0037 -> 0038 -> 0037` in a random PostgreSQL database.
- Test strict DTOs, default-off route behavior, Cookie-only authentication,
  insert versus management authority, stable error mapping, and API-owned
  commit/rollback.
- Test command hashes, expected-state fences, lifecycle transitions,
  idempotent command races, bounded audit targets, and no service commits.
- Test same-scope merge, deterministic conflicts, alias deduplication, Evidence
  preservation, no hard delete, and no pre-activation fact mutation.
- Test ordinary manifest compatibility, projected action-set identity, exact
  idempotent plan replay, atomic activation, failure rollback/unbinding, and
  preserved-status rollback.
- A PostgreSQL async engine and its pool must be created, used, and disposed in
  one event loop. Do not reuse it across multiple `asyncio.run()` calls.
- Run inherited graph extraction, Publication, retrieval, Graph Catalog, full
  backend, Ruff, compileall, import smoke, release safety, offline Alembic SQL,
  and `git diff --check`.

## 7. Wrong vs Correct

Wrong:

```python
action.status = "approved"
entity.status = "disabled"
await db.commit()
```

Correct:

```python
action.status = "approved"  # caller-owned transaction; live fact unchanged

result, action_set_hash = await plan_graph_governance_publication(db, command)
# A later explicit activation revalidates hashes, applies effects, switches the
# formal graph, marks action items applied, and commits atomically.
```

The correct flow keeps published retrieval stable until activation and makes
the exact action/effect set part of the Publication identity.
