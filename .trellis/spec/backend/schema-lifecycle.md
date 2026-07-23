# Schema Lifecycle

## Scenario: Versioned Ontology Drafting And Activation

### 1. Scope / Trigger

Use this contract when reading Ontology versions, cloning an active Schema,
editing or validating a draft, previewing its impact, or activating it. The
existing Ontology tables remain authoritative; lifecycle actions coordinate
permission, state fencing, idempotency, audit, and rollback.

The central invariant is:

```text
active version -> independent draft -> validate -> explicit activation
       |                                           |
       +------ historical facts stay bound --------+
```

### 2. Signatures

Database revision and rollout:

```text
0039: schema_lifecycle_actions
SCHEMA_LIFECYCLE_ENABLED=false
ORGANIZATION_AUTHORIZATION_ENABLED=true  # required when enabled
```

Internal HTTP prefix:

```text
/libraries/{slug}/schema-lifecycle
```

Service boundaries:

```python
list_schema_versions(...)
load_schema_version_bundle(...)
clone_schema_version(...)
apply_schema_item_command(...)
validate_schema_draft_bundle(...)
preview_schema_impact(...)
activate_schema_version(...)
```

Every mutation carries `expected_version_state_hash` and `idempotency_key`.
Activation also carries `expected_active_version_id` and the fixed
`activate_schema_version` confirmation.

### 3. Contracts

- Routes return `404` before Library lookup while disabled. Reads and writes
  require an Organization administrator or explicit Library `admin` through
  `resolve_loaded_library_management`; platform superuser is not sufficient.
- Version detail loads entity types, relation types, attributes, and relation
  constraints as one bounded projection. Responses repeat Library, version,
  child ownership, endpoint identity, counts, and canonical state hashes.
- Clone locks one `(library_id, version_key)` family, creates the next version,
  uses deterministic IDs, copies non-deleted children, and remaps every owner
  and constraint endpoint to cloned children. Source rows never change.
- Create, update, and disable operate only on a draft. Dependencies must exist,
  be enabled, and belong to the same Library and Ontology version before a row
  is written.
- Validation is complete, read-only, bounded, and stably ordered. Activation
  invokes the same validator and refuses a draft with any issue.
- Impact compares the draft with its parent, counts extraction and Publication
  references, and discloses compatibility only for actor-readable Libraries in
  the same Organization.
- Activation locks the version family and deactivates only the active member of
  the same `version_key`. Other active version families remain active. It
  activates draft children and the draft atomically without rewriting facts,
  Evidence, extraction snapshots, Publications, or Publication items.
- Lifecycle action and audit rows are written in the caller-owned transaction.
  Payloads contain identifiers, hashes, kinds, versions, and counts only. A
  failed mutation rolls back Schema, action, and audit together.
- Idempotent replay requires the same canonical command hash. Reusing a key for
  another command fails; clients never receive raw exception or stored content.

### 4. Validation & Error Matrix

| Condition | Stable result |
|---|---|
| Feature disabled | HTTP `404` before customer-data query |
| Missing management authority | `schema_lifecycle_forbidden` / `403` |
| Hidden or cross-Library identity | generic not found / `404` |
| Strict request or stored shape invalid | `schema_lifecycle_request_invalid` / `422` |
| Version hash or active identity changed | `schema_lifecycle_state_changed` / `409` |
| Same key, different command hash | `schema_lifecycle_idempotency_conflict` / `409` |
| Enabled dependency is absent or cross-version | `schema_lifecycle_dependency_conflict` / `409` |
| Complete draft validation fails | `schema_lifecycle_invalid_draft` / `409` |
| Stored invariant cannot be trusted | `schema_lifecycle_unavailable` / `503` |

### 5. Good / Base / Bad Cases

- Good: an administrator clones `enterprise v1`, adds a type and constraint,
  previews references, then activates `enterprise v2`; old facts and the
  unrelated active `compliance` family are unchanged.
- Good: two identical clone requests race and converge on the same deterministic
  draft and action result.
- Base: a Library has one active version and no draft. Inspection works, active
  children are read-only, and the console offers clone rather than edit.
- Bad: cloning attributes with source owner IDs, editing an active row, disabling
  a referenced type, deactivating every active Ontology family, or migrating
  historical facts during activation is forbidden.

### 6. Tests Required

- Assert ORM/migration parity, enum and hash checks, FKs, indexes, one Alembic
  head, and `0038 -> 0039 -> 0038` against disposable PostgreSQL.
- Test default-off behavior, startup dependency validation, Cookie-only
  management authorization, cross-Library denial, strict DTOs, fixed errors,
  and unknown-exception sanitization.
- Test complete clone remapping, deterministic replay, command-hash collision,
  draft-only writes, dependency conflicts, transaction rollback, and bounded
  audit payloads.
- Test validation ordering, parent diff, reference counts, authorized
  compatibility projection, stale fencing, concurrent activation, version-key
  isolation, and historical Entity/Publication preservation.
- Run focused v0.9 regressions, full non-frozen backend tests, Ruff, compileall,
  import smoke, release safety, offline migration SQL, and `git diff --check`.

### 7. Wrong vs Correct

Wrong:

```python
for version in active_versions:
    version.status = "disabled"
draft.status = "active"
await db.commit()
```

Correct:

```python
family = await lock_version_family(db, library_id, draft.version_key)
assert_expected_active(family, command.expected_active_version_id)
validate_schema_draft_bundle(bundle)
disable_active_family_member(family)
activate_draft_bundle(bundle)
await record_action_and_audit(db, command)  # caller commits once
```

The correct flow isolates one Schema family, revalidates under lock, and keeps
the Schema mutation, action record, and audit record atomic.
