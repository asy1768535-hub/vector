# Graph Exploration Console Contracts

## Scenario: Bounded Published Graph Exploration

### 1. Scope / Trigger

Use this contract when changing the `explore` tab under
`/knowledge-governance/graph` (legacy `/knowledge-graph`), its
seed search, published traversal client, radial canvas, equivalent tables, or
Entity/Relation/Evidence drill-down. Exploration is a secondary desktop
workspace. It reuses existing Graph Catalog, v0.6 traversal, and Catalog
Evidence truth and does not add another backend graph service.

### 2. Signatures

Route:

```text
/knowledge-governance/graph?tab=explore
                 &organization=<uuid>
                 &libraries=<slug,slug>
```

Traversal client:

```javascript
queryPublishedGraph(librarySlug, {
    ontology_version_id,
    expected_publication_id,
    seeds: [{ entity_id }],
    direction,                 // inbound | outbound | both
    relation_type_keys,        // at most 8 exact keys
    max_hops,                  // 1 | 2 in this UI
    max_nodes,                 // 1..100
    max_relations,             // 1..200
    include_evidence_locators: true,
})
```

Backend endpoint:

```text
POST /libraries/{slug}/v06/graph/query
```

Pure frontend boundaries live in `graph_exploration_ui.js`, including seed
identity, response validation, fixed errors, truncation projection, radial
layout, and hit testing.

### 3. Contracts

- Reuse Organization and 1..20 readable-Library scope from the parent graph
  workspace. Effective Library `read` is required; platform superuser alone is
  not customer-content authority.
- Search seeds through Graph Catalog. Retain Library, Ontology, Entity type,
  publication state, and Publication identity. Select at most four unique
  `(library_id, entity_id)` seeds.
- Only a seed backed by an active published Publication is queryable. Execute
  one request per seed using its exact Library slug, Ontology version, Entity
  ID, and expected Publication ID.
- Keep every result in its own Library/seed group. Same-name Entities from
  different Libraries are never merged, and the frontend never synthesizes a
  cross-Library relation.
- The API builder allowlists request fields, caps controls within v0.6 limits,
  and always requests Evidence locators. It must not spread component state or
  caller extras into the request.
- Accept a response only when contract, Library request context, Ontology,
  Publication, seed, counts, unique IDs, relation endpoints, hop depths,
  bounds, item hashes, Manifest hashes, and Evidence locator identities all
  match the submitted request.
- The canvas uses deterministic radial layout: seed at the center, one-hop
  Entities on the inner ring, and two-hop Entities on the outer ring. It has no
  force physics, 3D, authoring, or cross-group edges.
- Canvas interactions and the equivalent semantic tables emit strict existing
  Entity, Relation, and Evidence identities to the parent. Do not duplicate
  drawers, source readers, or Evidence rendering.
- Render only allowlisted traversal fields. Never render arbitrary properties,
  raw errors, source text, prompts, credentials, storage locators, or provider
  payloads.
- M5 is desktop-focused. Use stable desktop sizing plus the existing 1199px and
  899px console breakpoints for reduced desktop windows. Do not add a dedicated
  `520px` exploration layout or a separate mobile page.

### 4. Validation & Error Matrix

| Condition | Console behavior |
|---|---|
| No effective Library read | Reject the scope through the parent route guard |
| Seed lacks an active Publication | Show it as unavailable and do not query it |
| Runtime disabled or unavailable | Show a fixed unavailable state for that group |
| Publication missing or degraded | Show a fixed Publication state for that group |
| Expected Publication changed | Discard that group result and require a new explicit query |
| Invalid relation keys or bounds | Show a fixed invalid-query state; do not loosen controls |
| Timeout | Show a fixed timeout state for that group |
| `403` or `404` | Show fixed scoped forbidden/not-found state |
| Malformed or mismatched response | Discard the payload and show a fixed malformed state |
| One seed request fails | Preserve every successful sibling group |
| Server reports truncation | Show exact node/relation/Evidence truncation indicators |

### 5. Good / Base / Bad Cases

- Good: two same-name seeds from different Libraries produce two independent
  groups with separate Publication and Ontology identity.
- Good: a two-hop result renders three bounded Entities, two Relations, and
  exact Evidence controls in both canvas and tables.
- Base: a seed has no relations; its successful group still shows the seed and
  authoritative zero relation count.
- Bad: joining nodes by normalized name creates false cross-Library facts.
- Bad: accepting counts or endpoints without validating returned membership can
  render stale or fabricated relationships.
- Bad: replacing v0.6 traversal with frontend table queries duplicates backend
  truth and loses Publication recheck semantics.

### 6. Tests Required

- API tests assert the exact v0.6 path, request allowlist, defaults, bounds, and
  forced Evidence locator request.
- Pure tests cover seed eligibility/deduplication, same-name Library separation,
  exact Publication and Ontology identity, counts, endpoint membership, hops,
  Evidence locators, truncation, fixed errors, deterministic layout, and hit
  testing.
- View tests assert the fifth non-default tab, independent request sequences,
  partial-failure retention, existing detail/Evidence event reuse, accessible
  tables, privacy exclusions, internal scrolling, and no dedicated 520px
  exploration layout.
- Desktop browser QA asserts nonblank canvas dimensions/pixels, exact one-hop
  and two-hop counts, click drill-down, same-name separation, truncation,
  partial failure, no page overflow, and no console errors.
- Run every `admin-ui/*.test.mjs` test, inherited v0.6 and v0.9 backend focused
  tests, full non-frozen backend regression when backend truth changes, Ruff,
  compileall, release safety, and `git diff --check`.

### 7. Wrong vs Correct

Wrong:

```javascript
const merged = new Map(nodes.map((node) => [node.canonical_name, node]));
await api.queryPublishedGraph(seed.library_slug, { ...controls, ...seed });
```

Correct:

```javascript
const seedKey = `${seed.library_id}:${seed.entity_id}`;
const result = await api.queryPublishedGraph(seed.library_slug, {
    ontology_version_id: seed.ontology_version_id,
    expected_publication_id: seed.publication.id,
    seeds: [{ entity_id: seed.entity_id }],
    direction: controls.direction,
    relation_type_keys: controls.relationTypeKeys,
    max_hops: controls.maxHops,
    max_nodes: controls.maxNodes,
    max_relations: controls.maxRelations,
    include_evidence_locators: true,
});
if (!graphTraversalResponseMatches(result, requestIdentity)) discardGroup(seedKey);
```

The correct flow preserves Library identity, delegates traversal truth to v0.6,
and renders only an exact, bounded published result.
