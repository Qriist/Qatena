# Qatena Design

SQLite-based note storage for local AI agents, tailored to complex personal creative projects.

## Goal

Build **durable, bulletizable, pointerable, and malleable** note storage:

- **Durable** — SQLite-backed, local-first, crash-safe writes; safe for agents to touch repeatedly.
- **Bulletizable** — notes decompose into individually addressable units (blades) that can be created, edited, moved, and deleted.
- **Pointerable** — first-class cross-references between notes and blades that agents can create, follow, and traverse.
- **Malleable** — schema and structure are not frozen; notes can be split, merged, and restructured without migration pain.

## Terminology

| Term | Meaning |
|---|---|
| **chain** | the unit of note (the source system's "pin") — a header plus ordered blades plus section membership |
| **blade** | an individually addressable unit of a chain's body (the source system's "bullet") |
| **section** | a category; the view layer of the note space (membership is chain-only) |
| **pointer** | a typed, UUID-addressed link between entities |

Themed after a chain weapon system in the project that prompted this design. The relational model (overlapping sections, first-class blades, UUID pointers) intentionally goes beyond what the old markdown/filesystem system could express.

## Identity and addressing

- Immutable **UUIDv7 key IDs** are the identity for all tracked entities: chains, blades, sections, open items, rulings, terms. `logs` and internal lookup registers are the exceptions.
- UUIDv7 gives creation-time ordering and global uniqueness without a central counter.
- **Names are aliases, not identity**: every named entity carries a human-facing name that is queryable and renameable. Nothing structural references a name.
- Pointers reference immutable UUIDs only, so renames, splits, merges, and blade rewrites are **reference-neutral**.

## Interface layer

A **stdio MCP server** (goose extension). The tool surface is **verb-level**: a small number of well-shaped, composite, batch-capable operations — one logical operation is one call, not raw data access. Rationale: LLM round-trips dominate cost; tool count has recurring context cost (goose performs best under ~25 enabled tools); a resident server keeps DB state warm across calls.

## Multi-vault routing

One server process, many vaults: each project gets its own Qatena database file. The 18th verb,
`vambrace` (`action: "create"`), mints a UUIDv7, instantiates `<key>.db` in the vault dir
(env `QATENA_VAULTS`, default `./.qatena`), bootstraps its schema, and returns that UUIDv7 as
the vault's **routing key** — every other verb call carries that key in a required `vambrace`
argument, and the server routes the call to the right file.

Routing state is the files themselves — an existence check in the vault dir — so there is no
registry to persist and routing survives server restarts. The key rules:

- **Strict vambrace-or-nothing** — every call is routed by an explicit key.
- **No default vault, ever** — a default would let an un-oriented agent land in the wrong
  project's data.
- **Fail closed** — missing key → `missing_vambrace`; malformed key → `malformed_vambrace`;
  well-formed key with no file → `unknown_vambrace`. The server never silently creates or
  falls back.

## Autoexport

A register names a **stack** (section → chain → blade, rooted at a live UUID) and a
destination file + format (md / csv / json / ndjson). After every committed write, one hook
compares each register's **content-only stack signature** against its stored signature; on
change a detached exporter child (`python -m qatena.autoexport`) re-reads committed truth and
atomically rewrites the file — so the file is always the current truth while the tool return
stays fast: change identification stays in the server process, file writing in the side
process.

**Why signature-diff** (rather than extending the per-write transaction footprint): the
footprint would need every write handler to record every structurally-relevant UUID —
membership changes, blade re-parenting, membership unions in merges — fragile by
construction. A bounded read of the stack is *provably* correct for "did the stack change, in
any way" and costs nothing measurable at this scale. Timestamps are excluded, so no-op
upserts don't re-export; membership changes do (membership is content).

**Pointer fidelity.** The pointer layer is a registerable field per export:
`pointer_fidelity` (`none` | `in` | `out` | `both`, default `out`) selects which pointer rows
the file carries — rows with `src` inside the stack (`out`), rows entering it (`in`,
backlinks), either (`both`), or none. Rows are dead `{src, dst, kind}` identifiers, never
resolved: no mode can pull referenced content into the export, so nothing is recursive or
combinatorial in any mode. The mode is signed into the stack signature, so changing it
re-exports, and a pointer write re-exports exactly the registers whose mode includes the
row's direction.

The exporter child opens the vault read-only and then does exactly one small read-write
transaction (store `last_sig`/`last_emitted` + mint the export log row) — the one sanctioned
exception to the server-as-sole-write-surface rule. A failed export leaves the signature
stale, so the next changed write re-triggers: self-healing with no queue or retry
bookkeeping. Exported files are never deleted by the server; deleting a registered root
cascade-deletes its registers.

## Data model

### Core tables

| Table | Columns (rough) | Notes |
|---|---|---|
| `chain_headers` | id (UUIDv7), name, created, updated | Headers may be **stubs** (zero blades) |
| `chain_sections` | id (UUIDv7), name, orientation flag | W tier = mirrored for fast re-orientation; B tier = pulled on demand |
| `chain_section_map` | chain_id, section_id | M2M — independent and **overlapping** groups |
| `chain_blades` | id (UUIDv7), chain_id, text | |
| `blade_order` | chain_id, blade_id, index | One total order per chain |
| `pointers` | id, src_uuid, dst_uuid, kind, note?, created | Polymorphic over the whole UUID space; `UNIQUE(src, dst, kind)` |

**Invariants**

- `blade_order` indices are **gapless per chain**, re-established in the same transaction as any blade insert/delete/move.
- `pointers` endpoints are any UUID in the tracked-entity space (chains, blades, sections, open items, rulings, terms); endpoint *type* is implicit in the UUID, never stored as a column.
- Section membership is **chain-only** — sections are the view layer of the note space; operational entities are not filed into sections.

### Non-core tables (creative-project tracking)

| Table | Columns (rough) | Notes |
|---|---|---|
| `logs` | id (strictly sequential integer), created, title?, area?, body | Append-only timeline; **no UUIDv7** — the sequence must be dense and monotonic, which UUIDs cannot guarantee; not a pointer endpoint |
| `open_items` | id (UUIDv7), name, state, text, created, updated | state → `open_item_states` |
| `open_item_states` | name (small seeded register) | e.g. open / shelved / resolved |
| `rulings` | id (UUIDv7), number (human-facing alias), name, state, body?, created, updated | state → `ruling_states` |
| `ruling_states` | name (small seeded register) | load-bearing markers (active / retracted / corrected / final) are **state values, not prose** |
| `terms` | id (UUIDv7), term (name), text?, created, updated | naming register |
| `log_refs` | log_id, uuid, op | system-minted junction (never prose): write verbs record their transaction's own footprint in the same transaction; op ∈ created / modified / deleted / referenced |

The unified UUID/pointer space dissolves the source system's `ruling_tags` and `open_items.refs` prose-reference columns: those relationships become ordinary pointer rows. Likewise `log_refs` replaces any free-text "affected items" list: logs are queryable by UUID, and the rows are minted by the system from the transaction's own footprint, so they cannot drift.

## Pointer semantics

`kind` expresses the pointer's **semantic relationship** — never the endpoint type.

v1 kinds: `name-ref` (mentions by name; the kind auto-linking mints at write time) · `supersedes` · `part-of` · `source` (provenance) · `see` (loose relatedness). The set is extensible as needs surface.

**Auto-linking:** when text is written (blade insert/edit, chain put), exact name matches mint `name-ref` pointers; anything less than exact returns *candidates* in the response — the server never links silently.

## Revision tracking

Placeholder surface only. A verb-level contract is reserved: field-level change history for any UUID entity (field, old → new, timestamp). The mechanism (audit tables, triggers) is deliberately deferred until there is something to build against; the design may assume write verbs leave an auditable trace. A coarse per-entity trace (which log touched which UUID, with an op code) is available via `logs` + `log_refs`; field-level old→new stays deferred.

## Verb surface (18 tools)

**Vault**
- `vambrace` — `action: "create"` mints a UUIDv7 and instantiates an empty vault; the returned
  UUIDv7 is the routing key required on every other verb (multi-vault routing, above).
  `action: "register_autoexport"` / `"deregister_autoexport"` are key-bearing vault actions:
  register a stack (section/chain/blade root) for automatic sig-gated export to a file
  (Autoexport, above; optional `pointer_fidelity` selects the pointer layer carried, default
  `out`), or remove the register

**Chain writes**
- `chain_put` — upsert by UUID or name; presence-mask fields (`name`, `sections`, …); fresh UUIDv7 if absent. Renames and refiling are just this.
- `chain_delete`
- `chain_split` — move a named set of blades into a new chain; blade UUIDs (and their pointers) travel intact
- `chain_merge` — fold one chain into another; blades appended in order, sections unioned

**Blade writes**
- `blade_insert` — into a chain at an index (or end); returns the new blade UUID; runs auto-linking on the text
- `blade_edit` — replace text by UUID; reconciles that blade's `name-ref` pointers
- `blade_move` — within or across chains; gapless `blade_order` re-established in the same transaction
- `blade_delete`
- `batch` — a list of any of the above, one transaction, all-or-nothing

**Pointer writes**
- `pointer_add` — explicit src/dst UUID + `kind` + optional note
- `pointer_remove`

**Reads**
- `get` — any UUID; chains return composite (header + ordered blades + section names), others bare
- `search` — multi-probe union across all entity spaces with context windows
- `list` — an entity space with filters (chains by section, items by state, rulings by state, …)
- `pointers_of` — incoming/outgoing edges for a UUID, filterable by `kind`
- `follow` — multi-hop traversal with a `kind` filter
- `history` — the reserved revision surface (contract now, mechanism deferred)

**Surface decisions**

- **Delete policy** — deleting a chain or blade cascade-deletes its *outgoing* pointers and *reports* incoming ones (they may still be meaningful).
- **Merge retargeting** — `chain_merge` retargets pointers aimed at the dissolved chain's header onto the survivor; the dissolved header's UUID stops resolving.
- **Auto-link strictness** — exact name match only mints pointers; near-matches are returned as candidates, never linked silently.

## Concretization (spec pass, 2026-11-27)

- **`logs.id`** — `INTEGER PRIMARY KEY AUTOINCREMENT`: the strongest sequential promise; append-only so the cost is nil.
- **Timestamps** — stored as Unix epoch **nanoseconds** (`INTEGER`); the tool surface renders and accepts ISO-8601 UTC. Precision outranks human readability of the raw value.
- **UUID storage** — canonical `TEXT(36)` for v1; conversion to `BLOB(16)` is a **finalizing step** at project end (storage-only, tool surface unchanged).
- **Name uniqueness** — `UNIQUE` per table (per space) on all alias fields; **cross-space collisions allowed** — a name may carry real meaning at multiple strata. Consequence: auto-linking exact matches mint `name-ref` to every entity bearing the name (≤ 1 per space), each enumerated in the response.
- **Implementation** — Python 3, stdlib `sqlite3`, single stdio MCP server process (the sole write surface).

## Open items

- Full DDL + MCP tool schemas — drafted in `SPEC.md` (tracked, uncommitted), pending review.
- UUID storage → `BLOB(16)` (finalizing step).
- `history` mechanism (field-level audit; contract reserved).
- Agent-authored log entries (a possible 19th verb) — open.
