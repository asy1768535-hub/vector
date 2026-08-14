# Graph Exploration Console Contracts

## Scenario: Bounded Published Graph Exploration

### 1. Scope / Trigger

Use this contract when changing the `explore` tab or the relationship display
inside the default `browse` workspace under
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

/knowledge-governance/graph?tab=browse
                 &organization=<uuid>
                 &libraries=<slug,slug>
                 &entity=<uuid>
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
- The default browser requests one selected Entity at a time and renders no
  graph until the user switches from `directory` or `content` to `graph`.
  Switching display modes does not create another route or backend service.
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
- The radial canvas uses locally vendored Cytoscape and a self-contained
  d3-force bundle. Simulation node and link objects retain stable ID-based
  identity across data refreshes. Dragging temporarily fixes only the grabbed
  node in model coordinates, reheats the simulation, and releases it back into
  soft link, collision, repulsion, and forceX/forceY centering forces. Data-only
  refreshes, viewport resize, pan, and zoom never recreate the graph or restart
  the simulation; explicit relayout is separate from fit-to-view. The canvas
  remains presentation-only and has no 3D, authoring, or cross-group edges.
  Radial Relations use Chinese presentation labels without mutating Schema
  keys. Unfocused nodes and edges remain visibly traceable while their text is
  hidden; selection, hover, or dragging reveals the focused Entity, its direct
  neighbor names, and related Relation labels.
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
- Good: the default browser keeps directory, content, and relationship graph
  mutually exclusive and preserves the selected Entity while switching.
- Base: a seed has no relations; its successful group still shows the seed and
  authoritative zero relation count.
- Bad: joining nodes by normalized name creates false cross-Library facts.
- Bad: accepting counts or endpoints without validating returned membership can
  render stale or fabricated relationships.
- Bad: replacing v0.6 traversal with frontend table queries duplicates backend
  truth and loses Publication recheck semantics.
- Bad: querying traversal during initial directory load or mounting all three
  browser displays at once wastes work and creates an overcrowded console.

### 6. Tests Required

- API tests assert the exact v0.6 path, request allowlist, defaults, bounds, and
  forced Evidence locator request.
- Pure tests cover seed eligibility/deduplication, same-name Library separation,
  exact Publication and Ontology identity, counts, endpoint membership, hops,
  Evidence locators, truncation, fixed errors, stable simulation identity,
  model-coordinate dragging and release, edge-driven panning, graph diff, and
  refresh state retention.
- View tests assert the preserved non-default advanced tab, independent request sequences,
  partial-failure retention, existing detail/Evidence event reuse, accessible
  tables, privacy exclusions, internal scrolling, and no dedicated 520px
  exploration layout.
- Browser-workspace tests additionally assert three mutually exclusive button
  modes, directory-first behavior, lazy traversal, and selected-Entity reuse.
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

## Scenario: Chat Citation Graph Context

### 1. Signatures

```text
引用片段 -> [复制片段] [知识图谱] -> 独立引用知识图谱弹窗

GET /libraries/{slug}/chat/graph-context?chunk_id=<uuid>
```

The response is either a successful empty context or one existing v0.6
`GraphRetrievalQueryResponse`. The graph request is server-owned and fixed to
one hop, 30 nodes, 50 relations, and included Evidence locators.

### 2. Contracts

- Require effective Library `read`. The cited Chunk must belong to the
  Library, a non-deleted ready Document, and that Document's current ready
  Revision.
- Resolve exact Evidence IDs from the Chunk and its active Entity Mention and
  Relation Evidence links. Select seeds only from active items in an active
  or degraded current Publication whose frozen `support_evidence_ids` contain
  those exact IDs.
- Reuse v0.6 published traversal and its Publication invariant checks. Never
  generate a relation with the LLM or scan Graph Catalog rows in the browser.
- Choose the Publication and seeds deterministically. A degraded selected
  Publication fails closed; no matching published item returns a successful
  empty context with `该引用暂未关联已发布图谱`.
- Clicking `知识图谱` closes the citation dialog before opening the graph
  dialog. Loading, empty, fixed error, graph, and selected-fact states are
  mutually exclusive. A closed or superseded request cannot update the dialog.
- Reuse `GraphCanvas`, not the directory/content/graph browser workspace. Its
  citation variant uses locally vendored Cytoscape with deterministic,
  irregular radial coordinates followed by an animated preset layout. Render
  one focused connected component at a time: one purple center Entity and its
  authoritative connected network grow outward. Other disconnected components
  remain in memory and are exposed through stable previous/next paging, but are
  not scattered across the same canvas. Changing the center inside a component
  must not reorder those pages. This layout is presentation-only and must not
  synthesize an edge.
- Render every Entity name and use Chinese presentation labels for Relations;
  this must not mutate Relation Type Schema keys or labels. Single-clicking an
  Entity or Relation opens its contextual inspector without changing viewport
  scale. Double-clicking a non-center Entity makes it the new center and starts
  its bounded expansion. Dragging the center translates the entire visible
  connected network, dragging another Entity moves only that Entity, dragging
  empty space pans the viewport, and the wheel zooms around the pointer.
- The initial citation response is prepared as exploration depth zero/one. An
  unexpanded selected Entity issues one existing v0.6 published traversal with
  the exact current Ontology version and expected Publication, `both`
  direction, one hop, 30 nodes, 50 Relations, and Evidence locators. Validate
  the full response through `graphTraversalResponseMatches` before merging.
- Merge expansion results only by authoritative Entity and Relation IDs. Keep
  the original citation seeds, shortest display depth, and exact Relation
  endpoints. Ignore dangling Relations, suppress duplicate Entity requests,
  and cap the accumulated view at 80 Entities / 160 Relations. Closing or
  superseding the dialog invalidates every in-flight expansion.
- Scale visible node diameter by degree in the accumulated subgraph. Show every
  Entity label; Relation labels remain hidden until hover or selection. Existing
  nodes retain their positions while new nodes begin near the selected expansion
  pivot and animate outward. The citation canvas uses a white, unframed stage,
  thin light straight edges, gray neighbor nodes, one purple center, and one
  top-right settings entry. Wheel zoom replaces permanent +/- controls; fit and
  relayout commands live inside the settings menu.

### 3. Tests Required

- Backend tests cover Library read authorization, hidden/current Chunk scope,
  published-only exact Evidence selection, deterministic candidate ordering,
  successful empty context, degraded/invariant errors, and fixed limits.
- Frontend tests cover conditional action visibility, the exact API path,
  separate-dialog behavior, all states, stale request invalidation, and
  deterministic finite starting coordinates without coincident multi-seeds.
  They also cover stable connected-component page order, page retention when
  changing center, and varied same-depth radii rather than a perfect ring.
  Pure expansion tests cover request fences, immutable citation seeds,
  deduplication, dangling-edge rejection, shortest display depth, degree
  projection, accumulated bounds, and consecutive expansion.
- Browser QA covers desktop and mobile dialog bounds, no page overflow, all
  returned nodes occupying finite preset positions, fact-list selection,
  component paging, center dragging, blank-canvas panning, pointer-centered
  wheel zoom, the settings menu, nonblank canvas pixels, and no console errors.
