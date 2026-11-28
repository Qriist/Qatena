# Qatena Design

SQLite-based note storage for local AI agents, tailored to complex personal creative projects.

## Goal

Build **durable, itemizable, addressable, and malleable** note storage:

- **Durable** — SQLite-backed, local-first, crash-safe writes; safe for agents to touch repeatedly.
- **Itemizable** — notes decompose into individually addressable units (blades) that can be created, edited, moved, and deleted.
- **Addressable** — first-class cross-references between notes and blades that agents can create, follow, and traverse.
- **Malleable** — schema and structure are not frozen; notes can be split, merged, and restructured without migration pain.

## Terminology

### Core terms

The five nouns of the data model itself - the chain-weapon theme a note is built from: routing (vambrace), grouping (intention), content (chain/blade), and cross-reference (reblade).

| Term | Meaning |
|---|---|
| **vambrace** | the per-project routing key: the UUIDv7 minted by the `vambrace` verb (action: equip), naming the project's database file `<key>.db` in the armory dir - every routed call is addressed by it |
| **intention** | a category; the view layer of the note space. Many-to-many: a chain can belong to **any number of intentions** (including zero), and an intention can contain any number of chains - overlapping is the design. Membership is chain-only |
| **chain** | the unit of note (the source system's "pin") — a header plus ordered blades plus intention membership |
| **blade** | an individually addressable unit of a chain's body (the source system's "bullet") |
| **reblade** | a typed, UUID-addressed link between entities |

### Auxiliary terms

Everything else peculiar to the project: the armory and its lifecycle (saga, waylaid), the creation and export paths (equip, acquire, furnish, stack, mythos), and the failed-write cache (deflected write).

| Term | Meaning |
|---|---|
| **acquire** | the key-less second creation path: imports a mythos snapshot file as a NEW vambrace - the minted key is the armory's, the file's carried key is provenance; entity UUIDs preserved, one genesis `logs` row (area `acquire`) |
| **armory** | the storage dir for vambraces (env `QATENA_ARMORY`, default `./.qatena`): live `<key>.db` files at the top, the `<armory>/waylay/` subdir for waylaid ones, and `armory.json` - the sagas + waylays ledger (a rebuildable display cache; conflict checks never consult it); the `vambrace` tool is "the armory verb" - its 10 actions govern the dir |
| **deflected write** | a failed content-write call parked in the per-vambrace `deflected_writes` cache (verb + canonical args + error): memory for the next session, not an execution queue - `replay` is rejected; an identical successful write auto-clears its rows in-transaction; read via `list` (space `deflected_write`), erased by the `deflected_write` tool |
| **equip** | the key-less `vambrace` action that births a vambrace: mints a UUIDv7, instantiates `<key>.db` in the armory dir, bootstraps the schema, and returns the key; optional `saga` names it at birth |
| **furnish** | the one-shot export action: renders an export to a file immediately (one call, one file, no bookkeeping), opening the vambrace **read-only** - no register row, no log row, no stored signature; `root` selects the scope (a live stack root, or the literal `mythos`) |
| **mythos** | a whole-vambrace snapshot: all eight entity spaces + meta (saga when set), in the furnish/acquire codec (md/csv/json/ndjson); the special `furnish` root and the only shape `acquire` imports; deliberately excludes deflected_writes (memory, not truth) and autoexport_registers (machine-local config) |
| **saga** | the human-facing name of a vambrace; stored in the database itself (the meta `saga` row) — the source of truth, travels with the file; mirrored in the armory ledger as a display cache; conflict-checked case-insensitively across live + waylaid |
| **stack** | the downward closure of a live root over intention → chain → blade - the unit `furnish` exports and `register_autoexport` watches; its **endpoint set** `E` = the root UUID + every chain UUID + every blade UUID in the stack, the filter set for the reblade layer |
| **waylaid** | a vambrace suspended in the server-controlled `<armory>/waylay/` dir: not routable by routed verbs, exports inert; the file location IS the lifecycle state — live (armory top) / waylaid / gone (dismantle request 3) |

Themed after a chain weapon system in the project that prompted this design. The relational model (overlapping intentions, first-class blades, UUID reblades) intentionally goes beyond what the old markdown/filesystem system could express.

## Identity and addressing

- Immutable **UUIDv7 key IDs** are the identity for all tracked entities: chains, blades, intentions, open items, rulings, terms. `logs` and internal lookup registers are the exceptions.
- UUIDv7 gives creation-time ordering and global uniqueness without a central counter.
- **Names are aliases, not identity**: every named entity carries a human-facing name that is queryable and renameable. Nothing structural references a name.
- Reblades reference immutable UUIDs only, so renames, splits, merges, and blade rewrites are **reference-neutral**.

## Interface layer

A **stdio MCP server** (goose extension). The tool surface is **verb-level**: a small number of well-shaped, composite, batch-capable operations — one logical operation is one call, not raw data access. Rationale: LLM round-trips dominate cost; tool count has recurring context cost (goose performs best under ~25 enabled tools); a resident server keeps DB state warm across calls. Where an entity's operations naturally form a set, they consolidate into **one verb with an `action` parameter** rather than separate verbs — armory control does this today (`vambrace`), and so do the `open_item` write surface (SPEC §3.19) and the `deflected_write` cache eraser (SPEC §3.20).

## Multi-vambrace routing

One server process, many vambraces: each project gets its own Qatena database file. The armory verb,
`vambrace` (`action: "equip"`), mints a UUIDv7, instantiates `<key>.db` in the armory dir
(env `QATENA_ARMORY`, default `./.qatena`), bootstraps its schema, and returns that UUIDv7 as the
vambrace's **key** — every other verb call carries that key in a required `vambrace`
argument, and the server routes the call to the right file. `action: "acquire"` is the
second creation path: it imports a mythos snapshot (a whole-vambrace snapshot file) as a NEW
vambrace — the minted key is the armory's; the file's carried key is provenance.

Routing state is the files themselves — an existence check in the armory dir — so there is no
registry to persist and routing survives server restarts. There are two locations, and the
location IS the state: the armory top level (LIVE, routable) and the `<armory>/waylay/` subdir
(WAYLAID: suspended — not routable by routed verbs, exports inert), managed by `waylay` /
`recover` / `dismantle`; no state column anywhere. The key rules:

- **Strict vambrace-or-nothing** — every call is routed by an explicit key.
- **No default vambrace, ever** — a default would let an un-oriented agent land in the wrong
  project's data.
- **Fail closed** — missing key → `missing_vambrace`; malformed key → `malformed_vambrace`;
  well-formed key with no file → `unknown_vambrace`. The server never silently creates or
  falls back.

## Furnish

The `furnish` vambrace action renders an export to a file immediately — the one-shot path:
one call, one file, no bookkeeping. The vambrace is opened **read-only**: no register row,
no log row, no stored signature is written, and the file lands atomically (temp file +
`os.replace`). `root` selects the scope: a live stack root, or the literal `mythos`.

**The stack.** A stack is the downward closure of a live root over intention → chain →
blade:

- root = **intention** — the intention + every member chain + every blade of those chains
- root = **chain** — the chain + its blades (in `blade_order`)
- root = **blade** — the blade itself (a leaf; its parent chain appears in the `md` output
  as presentation context only)

Four codecs render the same stack: `md` (human), `csv` (flat), `json` and `ndjson`
(machine).

**The reblade layer.** An optional `reblade_fidelity` (`none` | `in` | `out` | `both`,
default `out`) selects which reblade rows the file carries, against the stack's **endpoint
set** `E` = the root UUID + every chain UUID + every blade UUID in the stack. Rows are
carried as **dead identifiers** — `{src, dst, kind}` + `note` when present — fetched in one
query over `reblades`; endpoints are never resolved or traversed, so no mode can pull
referenced content into the export: every export is bounded and non-recursive by
construction.

- `none` — no reblade rows
- `out` — rows with `src` in E (the stack's outbound references) — the default
- `in` — rows with `dst` in E (backlinks: outside → inside)
- `both` — rows with either endpoint in E

Each row appears exactly once, sorted by `(src, dst, kind, note)`. Per format: `json` gains
top-level `reblade_fidelity` + `reblades`; `ndjson` appends `{"space": "reblade", "src",
"dst", "kind"(, "note")}` lines after the entity lines; `csv` is
`space,uuid,name,src,dst,kind,pos,text,note` (reblade rows leave `uuid`/`name` blank); `md`
renders outgoing rows as `<!-- kind -> dst -->` comment lines under the owning entity (plus
`<!-- note: ... -->`), and `in` / `both` add a trailing `## Inbound` block of
`- src [kind] -> dst` rows — in `both`, a row with both endpoints inside the stack renders
exactly once (under its `src` owner), never duplicated into the block. Liveness caveat: the
file carries reblade identifiers, not endpoint liveness — a deleted external endpoint leaves
its UUID in the file until the reblade row itself changes.

**The mythos root.** `root` may instead be the literal `mythos`: the whole-vambrace
snapshot, payload `{exported_at, key, saga?, spaces: {chains, blades, reblades, intentions,
open_items, rulings, terms, logs}}` — all eight entity spaces in the bare-record vocabulary
(NULLs omitted), plus the meta: the `key` is the source vambrace's key (provenance, not
identity — `acquire` mints a new one) and the `saga` from meta (omitted when unset).
Excluded by design: `deflected_writes` (declared memory, not truth) and
`autoexport_registers` (machine-local config; an acquired vambrace starts with none). One
codec, two directions: the same `md` / `csv` / `json` / `ndjson` render serves `furnish`
(root = `'mythos'`) out and `acquire` in; a mythos round-trip is identical in all four
formats except `exported_at`, the new key, and the acquired db's genesis log row.
`reblade_fidelity` is inapplicable to mythos (the whole db is the scope) and is rejected if
supplied.

Autoexport (below) is this same render, kept current by the server.

## Autoexport

Autoexport builds on furnish: a **register** names the same furnish export — a stack root,
a destination file, a format, a reblade layer — plus a stored signature, and the server
keeps the file current instead of rendering once.

After every committed write (one post-commit check in the server's routed write path;
`batch` = one check for the whole transaction; read verbs and the register / deregister
actions themselves are excluded), each active register's **stack signature** is compared
against its stored `last_sig`. The signature is a stable content-only hash over the ordered
stack and the register's reblade layer: `(root uuid, root space, root name, fidelity mode)`
+ per chain `(uuid, name, sorted intention names, [(blade uuid, text)] in pos order)` + the
sorted reblade rows (SPEC 3.18). On change, a detached child process
(`python -m qatena.autoexport <vambrace> <root> <path> <format>`) re-reads committed truth
and re-lands the file with the exact furnish render — so the file is always the current
truth while the tool return stays fast: change identification stays in the server process,
file writing in the side process.

**Why signature-diff** (rather than extending the per-write transaction footprint): the
footprint would need every write handler to record every structurally-relevant UUID —
membership changes, blade re-parenting, membership unions in merges — fragile by
construction. A bounded read of the stack is *provably* correct for "did the stack change,
in any way" and costs nothing measurable at this scale. Timestamps, logs, and intention
orientation are excluded, so no-op upserts don't re-export — but a chain refiled into / out
of a registered intention does (membership is content), and the chain's own register
re-exports too (its intention list is in its signature). A blade root's stack is
`(uuid, text)` only, so moving a registered blade between chains does not re-export it (a
move changes no reblade row).

**The fidelity mode is signed.** Changing the register's `reblade_fidelity` re-exports, and
a reblade write re-exports exactly the registers whose mode includes the row's direction —
an outside → inside write never re-exports an `out` register; an inside → outside write
never re-exports an `in` one. Renaming an external endpoint re-exports nothing (no endpoint
name is carried).

Registration does a **synchronous first export** (the response reports the initial write);
a failed first export keeps the register with `last_sig` NULL, so the next write to the
vambrace re-exports. The child process opens the vambrace **read-only** (re-reading its own
register row first — absent, i.e. deregistered in flight, is a clean no-op: no file write,
no log), then does exactly one small read-write transaction (store `last_sig` /
`last_emitted` + mint the export log row, area `autoexport`) — the one sanctioned exception
to the server-as-sole-write-surface rule. A failed export leaves the signature stale, so the
next changed write re-triggers: self-healing with no queue or retry bookkeeping.

**Lifecycle.** Upsert on identity `(root, emit_path)`: re-registering the same root + path
with a different format or fidelity REPLACES it; fidelity is presence-masked (omitted at
creation → `out`, omitted on re-register → keep the stored value). Deregister removes the
register row only — **the exported file is left on disk**; the server never deletes user
files. Deleting a registered root (`chain_delete` / `blade_delete`) cascade-deletes its
register rows in the same transaction — a dead root can never re-trigger, and UUIDs are
never reused.

## Data model

### Core tables

| Table | Columns (rough) | Notes |
|---|---|---|
| `chain_headers` | id (UUIDv7), name, created, updated | Headers may be **stubs** (zero blades) |
| `chain_intentions` | id (UUIDv7), name, orientation flag | W tier = mirrored for fast re-orientation; B tier = pulled on demand |
| `chain_intention_map` | chain_id, intention_id | M2M — independent and **overlapping** groups |
| `chain_blades` | id (UUIDv7), chain_id, text | |
| `blade_order` | chain_id, blade_id, index | One total order per chain |
| `reblades` | id, src_uuid, dst_uuid, kind, note?, created | Polymorphic over the whole UUID space; `UNIQUE(src, dst, kind)` |

**Invariants**

- `blade_order` indices are **gapless per chain**, re-established in the same transaction as any blade insert/delete/move.
- `reblades` endpoints are any UUID in the tracked-entity space (chains, blades, intentions, open items, rulings, terms); endpoint *type* is implicit in the UUID, never stored as a column.
- Intention membership is **chain-only** — intentions are the view layer of the note space; operational entities are not filed into intentions. Membership is unconstrained in degree: a chain can belong to any number of intentions (including zero), and an intention can contain any number of chains — groups are independent and overlapping by design.

### Non-core tables (creative-project tracking)

| Table | Columns (rough) | Notes |
|---|---|---|
| `logs` | id (strictly sequential integer), created, title?, area?, body | Append-only timeline; **no UUIDv7** — the sequence must be dense and monotonic, which UUIDs cannot guarantee; not a reblade endpoint |
| `open_items` | id (UUIDv7), name, state, text, created, updated | state → `open_item_states` |
| `open_item_states` | name (small seeded register) | e.g. open / shelved / resolved |
| `rulings` | id (UUIDv7), number (human-facing alias), name, state, body?, created, updated | state → `ruling_states` |
| `ruling_states` | name (small seeded register) | load-bearing markers (active / retracted / corrected / final) are **state values, not prose** |
| `terms` | id (UUIDv7), term (name), text?, created, updated | naming register |
| `log_refs` | log_id, uuid, op | system-minted junction (never prose): write verbs record their transaction's own footprint in the same transaction; op ∈ created / modified / deleted / referenced |

The unified UUID/reblade space dissolves the source system's `ruling_tags` and `open_items.refs` prose-reference columns: those relationships become ordinary reblade rows. Likewise `log_refs` replaces any free-text "affected items" list: logs are queryable by UUID, and the rows are minted by the system from the transaction's own footprint, so they cannot drift.

`open_items` carries seven NULL-defaulted freeform tracking columns — `freeform_current` / `freeform_pending` / `freeform_total` (INTEGER) and `freeform_next` / `freeform_last` / `freeform_due` / `freeform_priority` (TEXT) — for quantity / key-position / due / priority tracking. The future open_item contract exposes them as seven OPTIONAL fields without the `freeform_` prefix (see SPEC open_items note).

## Reblade semantics

`kind` expresses the reblade's **semantic relationship** — never the endpoint type.

Seeded kinds: `name-ref` (mentions by name; the kind auto-linking mints at write time) · `supersedes` · `part-of` · `source` (provenance) · `see` (loose relatedness) · `conflict` (the endpoints contradict) · `depends-on` (one gates the other). The set is extensible as needs surface.

**Auto-linking:** when text is written (blade insert/edit, chain put), exact name matches mint `name-ref` reblades; anything less than exact returns *candidates* in the response — the server never links silently.

## Revision tracking

Settled, built this pass: every content write mints **revision rows** (field, old → new, timestamp) into a per-vambrace sidecar `<key>.history.db`, joined to the write's `logs` row — which is the why, one truth. `history` returns the revision rows with the entity's FULL log trace (op / title / created / body / refs), so field-level old→new and the coarse per-entity trace arrive in one response. Content fields only — blade `text`; chain `name` + intention membership (one row per name); open item fields; ruling / term fields when their write verbs land. Machine columns, moves, and reblades mint nothing; a no-op write mints nothing. Revision rows never cascade with the entity — a dissolved header stays recoverable via history. Mechanism, minting map, retention, response shape: SPEC §3.17.

## Verb surface (20 tools)

**Armory**

The armory verb `vambrace` (multi-vambrace routing, above) hosts **10 actions**:

| action | key? | what it does |
| --- | --- | --- |
| `equip` | — | mint + optional `saga` |
| `acquire` | — | import a mythos snapshot as a NEW vambrace |
| `saga` | live or waylaid | the human-addressable name |
| `inventory` | — | reports all existing vambraces, including waylaid |
| `furnish` | live | one-shot: a live root's stack, or root `mythos` = whole-vambrace snapshot; read-only |
| `waylay` | live | stages an active vambrace for dismantling; **requires** an inline `mythos` snapshot before the move happens |
| `recover` | waylaid | moves a waylaid vambrace back into active use; resets the `dismantle` countdown |
| `dismantle` | waylaid | requests 1-2 warn, request 3 destroys |
| `register_autoexport` | live | register a live stack root for autoexport; kept current by every signature-changing write |
| `deregister_autoexport` | live | remove a registered autoexport; the exported file is left on disk |

- Armory-level invariants: the file location IS the lifecycle state (live at the armory top /
  waylaid / gone). Actions mint no db log rows (the armory ledger, `armory.json`, + the
  responses are their record) — `acquire`'s genesis row is the one exception — and `equip` /
  `acquire` each seed the sidecar's one genesis row (the `history` birth frame)

**Chain writes**
- `chain_put` — upsert by UUID or name; presence-mask fields (`name`, `intentions`, …); fresh UUIDv7 if absent. Renames and refiling are just this.
- `chain_delete`
- `chain_split` — move a named set of blades into a new chain; blade UUIDs (and their reblades) travel intact
- `chain_merge` — fold one chain into another; blades appended in order, intentions unioned

**Blade writes**
- `blade_insert` — into a chain at an index (or end); returns the new blade UUID; runs auto-linking on the text
- `blade_edit` — replace text by UUID; reconciles that blade's `name-ref` reblades
- `blade_move` — within or across chains; gapless `blade_order` re-established in the same transaction
- `blade_delete`
- `batch` — a list of any of the above, one transaction, all-or-nothing

**Reblade writes**
- `reblade_add` — explicit src/dst UUID + `kind` + optional note
- `reblade_remove`

**Open items**
- `open_item` — the single `action`-driven verb for the open_item space. `action: "put"`:
  presence-masked create-or-update; `id` takes precedence over `name` (id + different name =
  rename; id-addressed puts never mint — a nameless put with an unresolvable id is `not_found`);
  name-addressed responses always carry the `created` marker; `state` is a field, defaulting to
  `open` on mint only. `action: "delete"`: by id or name; incoming reblades preserved and
  reported. Reads stay on the generic `get` / `list` / `search` verbs.

**Deflected (Failed) writes**
- `deflected_write` — the eraser for the failed content-write cache: `action: "clear"` by
  `id` (one row) or `group_id` (a whole failed batch), exactly one required. Every failed
  content-write call (verb + canonical args) is parked per-vambrace in the `deflected_writes`
  table — read through `list` (space `deflected_write`), auto-cleared in-transaction by an
  identical successful write. Memory, not an execution queue: `replay` is rejected (SPEC §3.20).

**Reads**
- `get` — any UUID; chains return composite (header + ordered blades + intention names), others bare
- `search` — multi-probe union across all entity spaces with context windows
- `list` — an entity space with filters (chains by intention, items by state, rulings by state, …)
- `reblades_of` — incoming/outgoing edges for a UUID, filterable by `kind`
- `follow` — multi-hop traversal with a `kind` filter
- `history` — field-level old→new history for any UUID: revision rows (old→new per
  content field, joined to their log's op + title) plus the FULL log trace
  (SPEC §3.17)
- All six take the optional **read-surface keys** (the section after §3.17) — agent
  context protection: `count` returns a window-honest `{count, total}` instead of
  the record set (a result counted before it is fetched); `emit_path` +
  `emit_format` (`json` | `ndjson` only) spill the result set to a file instead
  of the agent's context (bare payload, no envelope; read-only against the
  armory; atomic temp + `os.replace`); `count` + `emit_path` together is rejected

**Surface decisions**

- **Delete policy** — deleting a chain or blade cascade-deletes its *outgoing* reblades and *reports* incoming ones (they may still be meaningful).
- **Merge retargeting** — `chain_merge` retargets reblades aimed at the dissolved chain's header onto the survivor; the dissolved header's UUID stops resolving.
- **Auto-link strictness** — exact name match only mints reblades; near-matches are returned as candidates, never linked silently.
- **Deflected writes are memory, not an execution queue** — a failed content write parks in the per-vambrace `deflected_writes` cache (readable via `list`, erasable via `deflected_write`); an identical successful write auto-clears its rows in-transaction; `replay` is rejected — re-issuing is a deliberate act (SPEC §3.20)

## Concretization (spec pass, 2026-11-27)

- **`logs.id`** — `INTEGER PRIMARY KEY AUTOINCREMENT`: the strongest sequential promise; append-only so the cost is nil.
- **Timestamps** — stored as Unix epoch **nanoseconds** (`INTEGER`); the tool surface renders and accepts ISO-8601 UTC. Precision outranks human readability of the raw value.
- **UUID storage** — canonical `TEXT(36)` for v1; conversion to `BLOB(16)` is a **finalizing step** at project end (storage-only, tool surface unchanged).
- **Name uniqueness** — `UNIQUE` per table (per space) on all alias fields; **cross-space collisions allowed** — a name may carry real meaning at multiple strata. Consequence: auto-linking exact matches mint `name-ref` to every entity bearing the name (≤ 1 per space), each enumerated in the response.
- **Implementation** — Python 3, stdlib `sqlite3`, single stdio MCP server process (the sole write surface).

## Open items

- `history` mechanism — settled, built this pass: the field-level old→new audit is SPEC §3.17 (per-vambrace sidecar `<key>.history.db`: `revisions` rows joined to the write's log row + a `genesis` frame at equip/acquire); the companion read-surface keys (`count` / `emit_path` / `emit_format`) on the six Reads-group verbs are the section after it.
- UUID storage → `BLOB(16)` (finalizing step).
- Seeding agent-specific intentions: the possibility of seeding the intention space with a few useful agent-specific entries (e.g. a designated agent-notes intention) so the notes-as-blades pattern is available from the start.
- Autoexport root clarification (docs): state explicitly that `register_autoexport` / `furnish` may be rooted at any part of the stack — a live intention, a chain within it, or a single blade — with the exported stack extending downward from the root (currently only implicit: SPEC 3.18 names the validation rule, the DESIGN autoexport intro just says "rooted at a live UUID").
- Autoexport use case example (docs): add a use case example to the Autoexport section — core worldbuilding information kept up to date via a register, immediately available for agent orientation.
- Make setup pip-able: package Qatena so it installs via pip (no `setup.py`/`pyproject.toml` yet — it currently runs as `python -m qatena` from the repo checkout).
- Make Qatena available to goose with a sensible default `QATENA_ARMORY` (same pass as make setup pip-able): a goose extension setup where Qatena works out of the box — with no env set, `qatena/__main__.py` falls back to CWD-relative `./.qatena`, so the default must be stable/absolute. (PYTHONPATH probably stops mattering once pip-able.)
- Sensible filters: the notes-as-blades pattern (agent notes under a designated intention) has no time-window query today — chain-list filters are `intention`/`name_prefix` only. Settle which list/search filters make on-demand in-scope summoning practical (e.g. `since`/`until` over `created`).