# First Principles Analysis: Chat Citation Graph

## Axioms

1. A graph is useful only when a user can identify one current center and follow its real neighbors without guessing; otherwise it does not communicate knowledge.
2. The citation graph may render only authoritative published entities and relations returned by the backend; visual grouping must never invent facts.
3. The result is bounded to one hop and at most 30 nodes / 50 relations, so a client-side force layout and direct manipulation are sufficient.

## Problem Essence

Core problem: the current circle-only radial view obscures names, directions, and fact structure, so users cannot locate the cited knowledge point.

Success criteria:

- Every visible node exposes its type and a readable canonical name.
- Every Entity name is readable and Relation display text is Chinese.
- Exactly one center remains visually distinct from its connected neighbors.
- Single click inspects, double click changes center, center drag moves the network, blank drag pans, and the wheel zooms.
- No frontend-generated entity or relation is introduced.

## Assumptions Challenged

| Assumption | Challenge | Axiom | Verdict |
|---|---|---|---|
| Knowledge graphs should use circles | Shape is decorative; long legal and organizational names need width | A1 | Discard |
| A fixed layout is clearer because it is predictable | Fixed columns turn exploration into a static audit chart and preserve avoidable crossings | A1, A3 | Discard |
| Labels below nodes are sufficient | Labels are truncated and detached from their type and relationship context | A1 | Discard |
| A legend below the canvas explains the graph | It explains depth but not actual facts or direction | A1 | Modify |
| The frontend may add a citation node to clarify context | That would be a synthetic graph fact | A2 | Discard |
| Canvas interaction alone is enough | A canvas is hard to scan and is not a semantic fact list | A1 | Discard |
| Every cited seed must be drawn simultaneously | Disconnected seeds produce several unrelated chains and destroy the single-center mental model | A1, A2 | Modify: retain data but render one connected component |
| Clicking a node should automatically fit its neighborhood | Viewport jumps make click and navigation indistinguishable | A1 | Discard |
| A perfect ring is the clearest radial layout | Equal distance and angle look synthetic and collapse a network into several spokes | A1 | Discard: keep radial growth but vary distance and angle deterministically |
| Hidden components only need a count | A count does not let the user inspect the remaining authoritative graph | A1, A2 | Discard: page each real connected component without inventing links |

## Ground Truths

1. Current published chat context can return several disconnected seed components plus their one-hop neighbors, all bounded to 30 nodes and 50 relations.
2. Entity type labels and relation type labels are already present in the response.
3. The existing GraphCanvas is shared with governance views and its default behavior must remain compatible.
4. The dialog must work within the existing no-build Vue 3 and Element Plus frontend.
5. The inspected Obsidian 1.12.7 local graph uses a white unframed canvas, a purple center, smaller dark-gray neighbors, always-visible light-gray node names, very thin straight edges, irregular radial distances, and one top-right settings control.

## Reasoning Chain

- GT1 + GT2 -> choose one deterministic seed as the center and project only its authoritative connected component; keep other components without drawing fake links.
- GT1 + A1 -> use an animated irregular radial layout, draggable nodes, and explicit focus navigation instead of fixed columns or unconstrained force drift.
- GT3 -> add a citation-specific GraphCanvas variant and preserve the default radial variant.
- GT4 + A1 -> separate single-click inspection from double-click center navigation and preserve the viewport on inspection.
- GT1 + A1 -> translate every visible node by the same delta when dragging the center, while blank dragging remains viewport panning.
- GT1 + GT5 -> expose stable previous/next component paging and one settings entry; use the wheel for zoom instead of a permanent four-button toolbar.
- GT5 + A1 -> vary radius and angle by a stable entity-ID hash so the graph reads as a growing web while remaining reproducible and testable.
- A2 -> all grouping, colors, and layout remain presentation-only; returned node and relation identities drive every interaction.

## Validation

- Every conclusion traces to a ground truth: yes.
- Every ground truth is covered: yes.
- No analysis phase was skipped: yes.
- Inversion stress test: the design would fail if it behaved like a static image, showed every relation label at once, permanently consumed canvas space with lists, or synthesized a citation node; the implementation explicitly prevents all four.
