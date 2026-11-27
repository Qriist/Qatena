# Qatena Spec — v1

**Status:** implementable contract for the settled design in `DESIGN.md`.
Resolutions the design left rough are marked **[resolution]** — the set of those is pending review.

**Runtime:** Python 3, stdlib `sqlite3`, a single stdio MCP server process (goose extension).
One database file per vambrace, created and owned by the server (§1.9, §3.18). The server is the **sole write
surface**: no other process or tool may open any vambrace file for writes — the one sanctioned exception is
the autoexport exporter child's single small per-export transaction (§3.18).

## 1. Conventions

### 1.1 Identity

- All IDs are UUIDv7, canonical lowercase hyphenated 36-char strings. The server mints them
  (`uuid.uuid7()`-equivalent); clients never generate IDs.
- Stored as `TEXT(36)` for v1. **Finalizing step (end of project):** convert all ID columns to
  `BLOB(16)`. Storage-only change; the tool surface keeps 36-char strings either way.
- The only non-UUID ID: `logs.id` (strictly sequential integer). Logs are never reblade endpoints.

### 1.2 Timestamps

- **Storage:** `INTEGER` — Unix epoch **nanoseconds** (Python `time.time_ns()`).
- **Tool surface:** rendered and accepted as ISO-8601 UTC with milliseconds,
  `2026-11-27T06:31:00.123Z`. (Raw storage is agent-irrelevant by design; precision outranks
  human readability of the raw value.)

### 1.3 Names and aliases

- Every named entity's alias column is `UNIQUE` **within its own table** (space).
- Uniqueness and matching are **case-insensitive** (`COLLATE NOCASE`) **[resolution e]**.
- **Cross-space collisions are allowed**: the same name may exist in multiple spaces (a name can
  carry real meaning at multiple strata). Consequence for auto-linking: an exact name match mints
  a `name-ref` to *every* entity bearing that name (at most one per space), and the response
  enumerates each minted reblade.

### 1.4 Transactions

- Every write verb runs in exactly one transaction: `BEGIN IMMEDIATE` → work → `COMMIT`.
  Any failure → `ROLLBACK`. Fail-closed: a partial write is never observable.
- `batch` is one transaction spanning N ops applied **in order**; later ops observe earlier
  ops' effects (including auto-linking against them).
- Reads (`get`, `search`, `list`, `reblades_of`, `follow`, `history`) are side-effect-free:
  they mint no logs and no `log_refs`.

### 1.5 System-minted logs **[resolution d]**

Every write transaction mints, in the same transaction:

- **One `logs` row**: `title` = short summary (e.g. `chain_put "The Forge"`),
  `area` = primary space (`chain` | `intention` | `blade` | `reblade` | `open_item` |
  `ruling` | `term` | `mixed` | `autoexport`), `body` = canonical JSON describing the change
  (per-op records: op, target UUID/name, fields changed with old→new where available).
- **`log_refs` rows**: one per UUID whose row was created / modified / deleted, or that new
  data in the transaction references without changing it:
  `op` ∈ `created` | `modified` | `deleted` | `referenced`.
  (`referenced` = e.g. the endpoints of a new reblade row.)

The settled verb surface has **no log-write verb**: the timeline is the mechanical trace of the
system's own footprint. Agent-authored narrative log entries would be a 19th verb — open, not
part of v1 (see §5).

### 1.6 Auto-linking

- **Triggers:** blade text writes — `blade_insert`, `blade_edit`, and their `batch` forms.
  (`chain_put` carries no body text, so names written there do not link.)
- **Exact matches:** each token of the written text equal to a registered alias
  (case-insensitive) mints a `name-ref` reblade from the written blade to that entity.
  Idempotent: an existing `(src, dst, kind)` is not duplicated.
- **Candidates:** registered aliases that do *not* exact-match but fuzzy-match some token —
  the alias begins with a token of ≥ 4 chars, or Levenshtein distance ≤ 2 to a token —
  are returned in the response (max 5, each with the token that triggered it).
  **Never linked silently.**
- **Reconciliation** (`blade_edit`): the blade's outgoing `name-ref` reblades are recomputed
  from scratch; stale ones are deleted, new ones minted. Both lists appear in the response.
- Registered alias set = the name columns of all six entity tables (chain names, intention names,
  open item names, ruling names, ruling numbers, terms).

### 1.7 Reblade endpoints

- A valid endpoint is any UUID resolving in `chain_headers`, `chain_blades`, `chain_intentions`,
  `open_items`, `rulings`, or `terms`. Reblades, logs, and state registers are **not** endpoints.
- `reblades.src_uuid` / `dst_uuid` are validated **by the server**, not by foreign keys —
  the endpoint space is polymorphic.
- Reblades carry **no UUID of their own**; identity is `(src_uuid, dst_uuid, kind)`
  **[resolution a]** — consistent with the settled endpoint enumeration.
- Deleting a chain or blade cascade-deletes its **outgoing** reblades and **reports** incoming
  ones in the response (they may still be meaningful; retarget or remove explicitly).
- `chain_merge` retargets reblades aimed at the dissolved header onto the survivor; the
  dissolved UUID stops resolving (recoverable via its `log_refs` trace → `history`).

### 1.8 Invariants (enforced server-side, inside the write transaction)

- `blade_order.pos` is **gapless 0..n−1 per chain**; re-established in the same transaction as
  any blade insert / delete / move / split / merge.
- Intention membership writes and the owning chain's `updated` are always written together.
- Auto-log (§1.5) and auto-linking (§1.6) are part of the write transaction, not afterthoughts.
- `logs` is append-only: no verb updates or deletes a log row or its `log_refs`.

### 1.9 Vambrace routing (multi-vambrace)

- Every entity lives in a **vambrace** — one Qatena database file. The armory dir comes from the
  `QATENA_ARMORY` environment variable (default `./.qatena`); the server creates it at startup.
  A vambrace file is `<armory_dir>/<key>.db`.
- A vambrace is created only by the `vambrace` verb, `action: "equip"` (§3.18): it mints a UUIDv7,
  instantiates `<key>.db`, bootstraps its schema, and returns exactly
  `{"vambrace": "<UUIDv7>"}` — that UUIDv7 is the vambrace's **key** (its routing identity).
- Every other verb takes a **required** `vambrace` argument = the routing key. Key format:
  36-char lowercase hyphenated, version nibble 7 —
  `^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$`.
- **Fail closed; no default vambrace, ever.** Missing key → `missing_vambrace`; malformed key →
  `malformed_vambrace`; well-formed key with no file in the armory dir → `unknown_vambrace`.
  The server never silently creates a vambrace or falls back to another.
- Routing state is the files themselves — a scan / existence check in the armory dir. No in-memory
  registry: routing survives server restart. Each call opens a fresh connection to its vambrace.
- `QATENA_DB` (the old single-database env var, default `./.qatena/qatena.db`) is **retired** —
  replaced by `QATENA_ARMORY`.

## 2. DDL

Connection pragmas (server sets on every connection):

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;   -- safe under WAL, fast
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

### Core

```sql
CREATE TABLE chain_headers (
  id      TEXT(36) NOT NULL PRIMARY KEY,           -- UUIDv7
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- alias
  created INTEGER NOT NULL,                        -- Unix ns
  updated INTEGER NOT NULL
);

CREATE TABLE chain_intentions (
  id          TEXT(36) NOT NULL PRIMARY KEY,       -- UUIDv7
  name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  orientation TEXT    NOT NULL CHECK (orientation IN ('W', 'B')),
    -- W = mirrored (fast re-orientation), B = pulled on demand
  created     INTEGER NOT NULL,
  updated     INTEGER NOT NULL
);

CREATE TABLE chain_intention_map (
  chain_id   TEXT(36) NOT NULL REFERENCES chain_headers(id)  ON DELETE CASCADE,
  intention_id TEXT(36) NOT NULL REFERENCES chain_intentions(id) ON DELETE CASCADE,
  PRIMARY KEY (chain_id, intention_id)
);
CREATE INDEX idx_csm_intention ON chain_intention_map (intention_id);

CREATE TABLE chain_blades (
  id       TEXT(36) NOT NULL PRIMARY KEY,          -- UUIDv7
  chain_id TEXT(36) NOT NULL REFERENCES chain_headers(id) ON DELETE CASCADE,
  text     TEXT    NOT NULL,
  created  INTEGER NOT NULL,
  updated  INTEGER NOT NULL
);
CREATE INDEX idx_blades_chain ON chain_blades (chain_id);

-- One total order per chain. `pos` is the design's INDEX: 0-based, gapless **[resolution f]**.
CREATE TABLE blade_order (
  chain_id TEXT(36) NOT NULL REFERENCES chain_headers(id) ON DELETE CASCADE,
  blade_id TEXT(36) NOT NULL REFERENCES chain_blades(id)  ON DELETE CASCADE,
  pos      INTEGER NOT NULL,
  PRIMARY KEY (chain_id, pos),
  UNIQUE (chain_id, blade_id)
);

CREATE TABLE reblade_kinds (                        -- seeded register, extensible **[resolution b]**
  kind TEXT NOT NULL PRIMARY KEY
);
INSERT INTO reblade_kinds (kind) VALUES
  ('name-ref'), ('supersedes'), ('part-of'), ('source'), ('see');

CREATE TABLE reblades (
  src_uuid TEXT(36) NOT NULL,   -- endpoint, validated server-side (polymorphic)
  dst_uuid TEXT(36) NOT NULL,
  kind     TEXT    NOT NULL REFERENCES reblade_kinds(kind),
  note     TEXT,
  created  INTEGER NOT NULL,
  PRIMARY KEY (src_uuid, dst_uuid, kind)
);
CREATE INDEX idx_reblades_dst ON reblades (dst_uuid);
```

### Non-core (creative-project tracking)

```sql
CREATE TABLE logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- dense, monotonic, append-only; never a reblade endpoint
  created INTEGER NOT NULL,
  title   TEXT,
  area    TEXT,
  body    TEXT NOT NULL
);
CREATE INDEX idx_logs_created ON logs (created);

CREATE TABLE log_refs (
  log_id INTEGER   NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
  uuid   TEXT(36) NOT NULL,   -- deliberately NOT a foreign key: must outlive deleted entities
  op     TEXT      NOT NULL CHECK (op IN ('created', 'modified', 'deleted', 'referenced')),
  PRIMARY KEY (log_id, uuid, op)
);
CREATE INDEX idx_log_refs_uuid ON log_refs (uuid);

CREATE TABLE open_item_states (
  name TEXT NOT NULL PRIMARY KEY
);
INSERT INTO open_item_states (name) VALUES ('open'), ('shelved'), ('resolved');

CREATE TABLE open_items (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  state   TEXT    NOT NULL REFERENCES open_item_states(name),
  text    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  freeform_current  INTEGER DEFAULT NULL,
  freeform_pending  INTEGER DEFAULT NULL,
  freeform_total    INTEGER DEFAULT NULL,
  freeform_next     TEXT    DEFAULT NULL,
  freeform_last     TEXT    DEFAULT NULL,
  freeform_due      TEXT    DEFAULT NULL,
  freeform_priority TEXT    DEFAULT NULL
);

CREATE TABLE ruling_states (
  name TEXT NOT NULL PRIMARY KEY
);
INSERT INTO ruling_states (name) VALUES ('active'), ('retracted'), ('corrected'), ('final');

CREATE TABLE rulings (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  number  TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- human alias, e.g. "R-014"
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  state   TEXT    NOT NULL REFERENCES ruling_states(name),
  body    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);

CREATE TABLE terms (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  term    TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- the term *is* its name
  text    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);
```

**Freeform tracking (for when the open_item contract is built).** `open_items` carries seven NULL-defaulted freeform tracking columns — `freeform_current` / `freeform_pending` / `freeform_total` (INTEGER), `freeform_next` / `freeform_last` (TEXT), and `freeform_due` / `freeform_priority` (TEXT). When the open_item contract (write verb) is built, add seven OPTIONAL fields to the open_item contract: `current` / `pending` / `total` / `next` / `last` / `due` / `priority` (omitting the `freeform_` prefix). The first five are common-use columns for different types of tasks where you need to track a quantity or key-based position — beats parsing the item body all the time for one value; `due` / `priority` sit outside that pattern but are core to an open item's tracking needs (user-directed addition). No further additions planned: the remaining candidates are derivatives of the set (`done = total − pending`) or speculative.

### Autoexport

```sql
CREATE TABLE autoexport_registers (
  root_uuid    TEXT(36) NOT NULL,
  emit_path    TEXT     NOT NULL,
  emit_format  TEXT     NOT NULL,
  last_sig     TEXT,                -- NULL until first successful export
  last_emitted INTEGER,             -- Unix ns of last successful export
  created      INTEGER NOT NULL,
  PRIMARY KEY (root_uuid, emit_path)
);
```

**Indexing notes.** The UNIQUE alias columns give implicit indexes. Beyond PKs, only two
explicit indexes exist (`idx_csm_intention`, `idx_reblades_dst`, `idx_logs_created`,
`idx_log_refs_uuid`, `idx_blades_chain`); all tables are personal-project scale — no
speculative indexes. `search` is LIKE-probe based in v1 (FTS5 is a later optimization, §5).

## 3. Verb surface (18 MCP tools)

**Shared field definitions** (used throughout the schemas below):

- **uuid** — `string`, canonical hyphenated UUIDv7;
  pattern `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`
- **iso** — `string`, ISO-8601 UTC with milliseconds (`2026-11-27T06:31:00.123Z`)
- **kind enum** — `["name-ref", "supersedes", "part-of", "source", "see"]`
- **entity spaces** — `chain | intention | blade | open_item | ruling | term | log`
- **vambrace** — `string`, the routing key: the UUIDv7 returned by `vambrace`
  (`action: "equip"`, §3.18); pattern
  `^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$`. Required on every verb
  except `vambrace` itself (§1.9).

**Addressing.** Where a tool takes an entity address, `id` (uuid) takes precedence over `name`;
at least one must be supplied. If both are supplied, the name must equal the resolved entity's
current name — except `chain_put`, where `id` + a different name **is the rename** (per §3.1).
A name-only call whose case-variant already exists resolves to that chain (NOCASE alias):
an idempotent upsert, not an error.

**Response shapes.**

```jsonc
// write verbs:
{
  "log_id": 128,                          // the minted logs row
  "...": "changed entity echo — composite for chains (header + ordered blades + intention names), bare record otherwise",
  "minted":     [ {"src": "…", "dst": "…", "kind": "name-ref"} ],   // auto-linking ops only
  "candidates": [ {"name": "…", "space": "chain", "token": "…"} ]   // auto-linking ops only
}

// reads: the record(s), plainly

// failure (MCP isError = true):
{ "error": { "code": "duplicate_name", "message": "chain name 'The Forge' is taken (id 0192…)" } }
```

**Error codes:** `not_found` · `duplicate_name` · `unresolvable_endpoint` · `invalid_index` ·
`validation_failed` · `missing_vambrace` · `malformed_vambrace` · `unknown_vambrace` ·
`unsupported_action` · `not_implemented` · `db_error` (always fail-closed: rolled back, nothing written).

---

### 3.1 `chain_put`

Upsert a chain by UUID or name; fresh UUIDv7 if absent. Presence-mask: only supplied fields
change. Renames and refiling are just this.

```json
{
  "name": "chain_put",
  "description": "Create or update a chain (note). Address by id or name; if neither matches, a fresh UUIDv7 is minted and the name is required. Presence-mask: only supplied fields change. Intentions, if present, replace membership with exactly this set and upsert each intention by name (absent intentions are created; orientation defaults to 'B', or the value given; an existing intention's orientation updates only if supplied). Returns the chain composite and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Chain uuid. Takes precedence over name." },
      "name": { "type": "string", "minLength": 1, "description": "Chain name (alias). Unique within the chain space, case-insensitive." },
      "intentions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": { "type": "string", "minLength": 1 },
            "orientation": { "type": "string", "enum": ["W", "B"] }
          },
          "required": ["name"]
        },
        "description": "If present, replaces the chain's intention membership with exactly this set. Intentions are upserted by name."
      }
    },
    "minProperties": 1,
    "required": ["vambrace"]
  }
}
```

### 3.2 `chain_delete`

```json
{
  "name": "chain_delete",
  "description": "Delete a chain by id or name. Blades and intention memberships are cascade-deleted; outgoing reblades are cascade-deleted and incoming reblades are preserved and reported in the response (they may still be meaningful). Returns the list of incoming reblades and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "name": { "type": "string", "minLength": 1 }
    },
    "minProperties": 1,
    "required": ["vambrace"]
  }
}
```

### 3.3 `chain_split`

```json
{
  "name": "chain_split",
  "description": "Carve a set of blades out of a source chain into a new chain. Blade UUIDs and their reblades travel intact. Blades are removed from the source and appended to the new chain in the order listed. The new chain's intentions are set from the optional intentions param (upsert semantics, as in chain_put). Returns both chain composites and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "source": { "type": "string", "description": "Source chain id or name." },
      "blades": {
        "type": "array",
        "items": { "type": "string" },
        "minItems": 1,
        "description": "Blade UUIDs to move, in the order they should appear in the new chain."
      },
      "name": { "type": "string", "minLength": 1, "description": "Name for the new chain." },
      "intentions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": { "type": "string" },
            "orientation": { "type": "string", "enum": ["W", "B"] }
          },
          "required": ["name"]
        },
        "description": "Optional initial intention membership for the new chain."
      }
    },
    "required": ["vambrace", "source", "blades", "name"]
  }
}
```

### 3.4 `chain_merge`

```json
{
  "name": "chain_merge",
  "description": "Fold one chain into a survivor. The dissolved chain's blades are appended to the survivor in their order; intention memberships are unioned into the survivor; reblades aimed at the dissolved header are retargeted onto the survivor; the dissolved header's UUID stops resolving (recoverable via history on that UUID). Blade UUIDs and their reblades are unchanged. Returns the survivor composite, the retargeted reblades, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "into": { "type": "string", "description": "Surviving chain id or name." },
      "fold": { "type": "string", "description": "Chain to dissolve (id or name)." }
    },
    "required": ["vambrace", "into", "fold"]
  }
}
```

### 3.5 `blade_insert`

```json
{
  "name": "blade_insert",
  "description": "Insert a blade into a chain at a 0-based index (default: end). Runs auto-linking: exact name matches mint name-ref reblades; near-matches are returned as candidates (never linked). Returns the new blade UUID, its index, minted reblades, candidates, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "chain": { "type": "string", "description": "Target chain id or name." },
      "text": { "type": "string", "minLength": 1 },
      "index": { "type": "integer", "minimum": 0, "description": "0-based position; omitted = append. Must be <= current blade count." }
    },
    "required": ["vambrace", "chain", "text"]
  }
}
```

### 3.6 `blade_edit`

```json
{
  "name": "blade_edit",
  "description": "Replace a blade's text by UUID. Reconciles the blade's outgoing name-ref reblades: stale ones are deleted, new exact matches minted; near-matches returned as candidates. Returns the updated blade, minted/deleted reblades, candidates, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." },
      "text": { "type": "string", "minLength": 1 }
    },
    "required": ["vambrace", "id", "text"]
  }
}
```

### 3.7 `blade_move`

```json
{
  "name": "blade_move",
  "description": "Move a blade to a new position within its chain, or to another chain. blade_order is re-established gapless in the same transaction. The blade's UUID and reblades are unchanged. Returns the blade's new chain and index and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." },
      "to_chain": { "type": "string", "description": "Destination chain id or name; omitted = same chain." },
      "index": { "type": "integer", "minimum": 0, "description": "0-based destination position, measured after removal; omitted = end." }
    },
    "required": ["vambrace", "id"]
  }
}
```

### 3.8 `blade_delete`

```json
{
  "name": "blade_delete",
  "description": "Delete a blade by UUID. Its outgoing reblades are cascade-deleted; incoming reblades are preserved and reported. blade_order is re-established gapless in the same transaction. Returns the incoming reblades and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." }
    },
    "required": ["vambrace", "id"]
  }
}
```

### 3.9 `batch`

```json
{
  "name": "batch",
  "description": "Execute a list of write ops (chain_put, chain_delete, chain_split, chain_merge, blade_insert, blade_edit, blade_move, blade_delete, reblade_add, reblade_remove) atomically: one transaction, all-or-nothing, applied in order. Later ops observe earlier ops' effects, including auto-linking. Returns per-op results (with minted/candidates where applicable) and the single minted log id. Any op failure rolls back the whole batch.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "ops": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "properties": {
            "op": { "type": "string", "enum": ["chain_put", "chain_delete", "chain_split", "chain_merge", "blade_insert", "blade_edit", "blade_move", "blade_delete", "reblade_add", "reblade_remove"] },
            "args": { "type": "object", "description": "Arguments for this op, with the shape of the standalone verb's input, except the routing key, which is supplied once as this batch's vambrace argument." }
          },
          "required": ["op", "args"]
        }
      }
    },
    "required": ["vambrace", "ops"]
  }
}
```

### 3.10 `reblade_add`

```json
{
  "name": "reblade_add",
  "description": "Create an explicit typed link between two UUIDs. kind is the semantic relationship, never the endpoint type (that is implicit in the UUID). Endpoints must resolve in the tracked entity space (logs and registers are not endpoints). Idempotent on (src, dst, kind): an existing reblade is returned, not duplicated. Returns the reblade and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "src": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "dst": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] },
      "note": { "type": "string" }
    },
    "required": ["vambrace", "src", "dst", "kind"]
  }
}
```

### 3.11 `reblade_remove`

```json
{
  "name": "reblade_remove",
  "description": "Remove the reblade identified by (src, dst, kind). Errors with not_found if absent. Returns the removed reblade and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "src": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "dst": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] }
    },
    "required": ["vambrace", "src", "dst", "kind"]
  }
}
```

### 3.12 `get`

```json
{
  "name": "get",
  "description": "Fetch any tracked entity by UUID. Chains return the composite: header, ordered blades (UUID, index, text), and intention names. Blades return the blade plus its chain's name. Intentions, open items, rulings, and terms return their bare records. A dissolved (merged) UUID returns not_found; the dissolving merge is recoverable via history on that UUID.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" }
    },
    "required": ["vambrace", "id"]
  }
}
```

### 3.13 `search`

```json
{
  "name": "search",
  "description": "Multi-probe union search across entity spaces: chain names, blade text, intention names, open item names/text, ruling numbers/names/bodies, term names/definitions, log titles/bodies. Probes are OR'd (substring, case-insensitive). Results are unioned, each with its entity space, UUID, name, and a context window around the match. Returns up to limit hits.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "probes": {
        "type": "array",
        "items": { "type": "string", "minLength": 1 },
        "minItems": 1,
        "description": "Search terms; unioned across spaces."
      },
      "spaces": {
        "type": "array",
        "items": { "type": "string", "enum": ["chain", "blade", "intention", "open_item", "ruling", "term", "log"] },
        "description": "Restrict to these entity spaces; omitted = all."
      },
      "context": { "type": "integer", "minimum": 0, "description": "Characters of context on each side of a match. Default 120." },
      "limit": { "type": "integer", "minimum": 1, "description": "Maximum hits. Default 50." }
    },
    "required": ["vambrace", "probes"]
  }
}
```

### 3.14 `list`

```json
{
  "name": "list",
  "description": "List an entity space with filters. Returns bare records (chains as headers plus intention names; blade bodies only if include_blades). Filters are space-specific keys, ignored (with a warning in the response) where inapplicable: chains: intention (name), name_prefix; intentions: orientation; open items / rulings: state; logs: uuid (via log_refs), since, until (iso); any space: name_prefix where the space has a name/alias field.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "space": { "type": "string", "enum": ["chain", "intention", "open_item", "ruling", "term", "log"] },
      "filter": {
        "type": "object",
        "properties": {
          "intention": { "type": "string" },
          "orientation": { "type": "string", "enum": ["W", "B"] },
          "state": { "type": "string" },
          "uuid": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
          "since": { "type": "string", "description": "iso, inclusive" },
          "until": { "type": "string", "description": "iso, inclusive" },
          "name_prefix": { "type": "string" }
        }
      },
      "include_blades": { "type": "boolean", "description": "For space=chain: include ordered blades in each record. Default false." },
      "limit": { "type": "integer", "minimum": 1, "description": "Default 50." },
      "offset": { "type": "integer", "minimum": 0, "description": "Default 0." }
    },
    "required": ["vambrace", "space"]
  }
}
```

### 3.15 `reblades_of`

```json
{
  "name": "reblades_of",
  "description": "Incoming and/or outgoing reblades for a UUID, filterable by kind. Each edge is returned as {src, dst, kind, note, created} with both endpoints resolved: their entity space and name.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "direction": { "type": "string", "enum": ["in", "out", "both"], "description": "Default both." },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] }
    },
    "required": ["vambrace", "id"]
  }
}
```

### 3.16 `follow`

```json
{
  "name": "follow",
  "description": "Multi-hop traversal from a UUID through the reblade graph. Returns visited nodes {uuid, name, space} with the edge kinds traversed, up to max_hops. Cycles are detected and pruned. Chain nodes include blades only if expand_chains.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "from": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "direction": { "type": "string", "enum": ["out", "in", "both"], "description": "Default out." },
      "kinds": {
        "type": "array",
        "items": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] },
        "description": "Restrict traversal to these kinds; omitted = any kind."
      },
      "max_hops": { "type": "integer", "minimum": 1, "description": "Default 2, maximum 5." },
      "expand_chains": { "type": "boolean", "description": "Include blades for chain nodes. Default false." }
    },
    "required": ["vambrace", "from"]
  }
}
```

### 3.17 `history`

```json
{
  "name": "history",
  "description": "Reserved revision surface: contract now, mechanism deferred. Currently returns the coarse trace — log rows touching this UUID via log_refs, with op codes, titles, and timestamps, newest first. The field-level old→new history this contract carries is not yet implemented; clients may build against the contract shape.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace." },
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "limit": { "type": "integer", "minimum": 1, "description": "Default 20." }
    },
    "required": ["vambrace", "id"]
  }
}
```

### 3.18 `vambrace`

Create and route vambraces. `action: "equip"` mints a UUIDv7, instantiates an empty Qatena database
file named `<UUIDv7>.db` in the armory dir, bootstraps its schema, and returns exactly
`{"vambrace": "<UUIDv7>"}` — that UUIDv7 is the vambrace's key, the required `vambrace`
argument of every other verb. `equip` is the only key-less action.

`register_autoexport`, `deregister_autoexport`, and `export` are **key-bearing** actions: alongside
`action` they take the vambrace's key as the `vambrace` argument, validated fail-closed
exactly like a routed verb (§1.9 — `missing_vambrace` / `malformed_vambrace` /
`unknown_vambrace`), plus the fields below. Any other action fails with `unsupported_action`.

**Autoexport.** A register names a *stack* — root + descendants, over intention → chain → blade
only — and a destination file + format:

- root = **intention**: the intention + every member chain + every blade of those chains
- root = **chain**: the chain + its blades (in `blade_order`)
- root = **blade**: the blade itself (a leaf; its parent chain appears in the `md` output as
  presentation context only)

Every committed write is followed by one post-commit check (one hook in the server's routed
write path; `batch` = one check for the whole transaction; read verbs and the register /
deregister actions themselves are excluded). For each active register the server compares the
**stack signature** — a stable content-only hash over the ordered stack and the register's
**reblade layer**: `(root uuid, root space, root name, fidelity mode)` + per chain `(uuid,
name, sorted intention names, [(blade uuid, text)] in pos order)` + the sorted reblade rows —
against the stored `last_sig`. On change, the exporter
child (`python -m qatena.autoexport <vambrace> <root> <path> <format>`) is spawned detached; it
re-reads committed truth, renders the format, and atomically rewrites the file. Timestamps,
logs, and intention orientation are excluded, so no-op upserts do not re-export — but a chain
refiled into / out of a registered intention does (membership is content), and the chain's own
register re-exports too (its intention list is in its signature). A blade root's stack is
`(uuid, text)` only, so moving a registered blade between chains does not re-export it (a
move changes no reblade row).

**Reblade layer (`reblade_fidelity`).** Each register selects which reblade rows the export
carries, against the stack's **endpoint set** `E` = the root UUID + every chain UUID + every
blade UUID in the stack (all live reblade endpoints; intentions count). Rows are carried as
**dead identifiers** — `{src, dst, kind}` + `note` when present — fetched in one query over
`reblades`; endpoints are never resolved or traversed, so no mode can pull referenced content
into the export: every export is bounded and non-recursive by construction.

- `none` — no reblade rows (the pre-fidelity reblade-excluded behavior)
- `out` — rows with `src` in E (the stack's outbound references) — **default**, outward-facing
- `in` — rows with `dst` in E (backlinks: outside → inside)
- `both` — rows with either endpoint in E

Each row appears exactly once, sorted by `(src, dst, kind, note)`. Because the layer is
signed, `reblade_add` / `reblade_remove` re-export exactly the registers whose mode includes
the row's direction: an outside → inside write never re-exports an `out` register; an inside
→ outside write never re-exports an `in` one. Renaming an external endpoint re-exports
nothing (no endpoint name is carried). Liveness caveat: the file carries reblade identifiers,
not endpoint liveness — a deleted external endpoint leaves its UUID in the file until the
reblade row itself changes.

Renderers: `json` gains top-level `reblade_fidelity` + `reblades`; `ndjson` appends
`{"space": "reblade", "src", "dst", "kind"(, "note")}` lines after the entity lines; `csv`
`space,uuid,name,src,dst,kind,pos,text,note` (reblade rows leave `uuid`/`name` blank); `md`
renders outgoing rows as `<!-- kind -> dst -->` comment lines under the owning entity
(plus `<!-- note: ... -->`), and modes `in` / `both` add a trailing `## Inbound` block of
`- src [kind] -> dst` rows — in `both`, a row with both endpoints inside the stack renders
exactly once (under its `src` owner), never duplicated into the block.

`register_autoexport` (key-bearing):

- `root` — must resolve to a LIVE `intention` / `chain` / `blade`. Other entity spaces →
  `validation_failed`; unknown / dissolved UUID → `not_found`.
- `emit_path` — used exactly as given; non-empty, parent directory must exist
  (`validation_failed` otherwise).
- `emit_format` — one of `md` | `csv` | `json` | `ndjson` (`validation_failed` otherwise).
- `reblade_fidelity` (optional) — one of `none` | `in` | `out` | `both`
  (`validation_failed` otherwise); see the reblade layer above. **Presence-masked**: omitted
  at creation → `out`; omitted on re-register → keep the stored value.
- Upsert on identity `(root_uuid, emit_path)`: re-registering the same root + path with a
  different format **or** fidelity REPLACES it (`replaced: true` in the response).
- **Synchronous first export** at registration (the response reports the initial write):
  `exported: false` + `export_error` on failure — the register row is kept and the export
  self-heals on the next changed write.
- Mints one log row (area `autoexport`). Response:
  `{root, root_space, emit_path, emit_format, reblade_fidelity, stack: {chains, blades},
  exported, exported_at (iso), replaced, log_id}`.

`deregister_autoexport` (key-bearing): deletes the register row for `(root, emit_path)`
(`not_found` if absent), mints one log row (area `autoexport`), and returns
`{root, emit_path, removed: true, log_id}`. **The exported file is left on disk** — the server
never deletes user files.

`export` (key-bearing): one-shot export — the same fields as `register_autoexport`
(`root`, `emit_path`, `emit_format`, optional `reblade_fidelity`), executed immediately with
**no vambrace mutation**: the vambrace opens **read-only**, and no register row, log row, `log_refs`,
or stored signature is written. `reblade_fidelity` here is the explicit reblade layer for this
one render (default `out`) — there is **no presence-masking**: the register row is never
consulted. The file lands via the same atomic mechanism as the exporter child (shared
`write_export`: temp + `os.replace`). Response:
`{root, root_space, emit_path, emit_format, reblade_fidelity, stack: {chains, blades},
exported: true, exported_at (iso)}` — deliberately **no `replaced`, no `log_id`** (nothing was
registered, nothing was logged). Errors: unresolvable root → `not_found`; root space not
stackable (intention/chain/blade) → `validation_failed`; file-write failure → `export_failed`;
sqlite → `db_error`.

**Export lifecycle (convention).** Re-exports are sig-gated: the file is rewritten only when
the stack signature actually changed. Self-healing: any failed export (disk full, path lost,
crash) leaves `last_sig` stale, so the next changed write re-triggers automatically — no queue,
no retry bookkeeping. The exporter child is the one sanctioned exception to the sole-write
surface: it opens the vambrace **read-only** (first re-reading its own register row — absent,
i.e. deregistered in flight, is a clean no-op: no file write, no log), then does exactly one
small read-write transaction (store `last_sig` / `last_emitted`, mint the export log row —
area `autoexport`, title `autoexport <root-space> <root-uuid> -> <emit_path>`, root ref op
`referenced`). Cascade: deleting a registered root (`chain_delete` / `blade_delete`) deletes
its register rows in the same transaction — a dead root can never re-trigger, and UUIDs are
never reused. Pre-existing vambraces (bootstrapped before `reblade_fidelity`) are migrated in
place by `schema.bootstrap` on next open: the column is added with default `out`,
idempotently.

```json
{
  "name": "vambrace",
  "description": "Armory control. action=equip mints a UUIDv7, instantiates an empty Qatena database file named <UUIDv7>.db in the armory dir, bootstraps its schema, and returns exactly {\"vambrace\": \"<UUIDv7>\"} — the vambrace's key, required on every other verb's call. register_autoexport, deregister_autoexport, and export are key-bearing vambrace actions: alongside 'action' they take the vambrace's key as the 'vambrace' argument (validated fail-closed exactly like every routed verb — missing_vambrace / malformed_vambrace / unknown_vambrace), plus 'root' and 'emit_path', and 'emit_format' for register and export. export is one-shot: it exports a live intention/chain/blade stack to emit_path immediately, read-only against the vambrace (no register row, no log, no stored signature). See SPEC 3.18.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "action": { "type": "string", "enum": ["equip", "register_autoexport", "deregister_autoexport", "export"], "description": "equip: key-less — mints a new vambrace. register_autoexport / deregister_autoexport / export: key-bearing — also pass 'vambrace' (the vambrace's key), 'root', and 'emit_path'; register_autoexport and export additionally require 'emit_format'. export is one-shot: immediate export of a live intention/chain/blade stack to emit_path, read-only against the vambrace (no register row, no log, no signature); reblade_fidelity selects the reblade layer for this render (default out)." },
      "vambrace": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "For register_autoexport / deregister_autoexport / export: the vambrace's key (returned by action=equip). Not used by equip." },
      "root": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "For register_autoexport / deregister_autoexport / export: the stack root — a LIVE intention, chain, or blade (register / export); the registered root (deregister). Not used by equip." },
      "emit_path": { "type": "string", "minLength": 1, "description": "For register_autoexport / deregister_autoexport / export: destination file path, used exactly as given; the parent directory must exist. Not used by equip." },
      "emit_format": { "type": "string", "enum": ["md", "csv", "json", "ndjson"], "description": "For register_autoexport and export only: export format. register: re-registering the same root+path with a different format replaces it. Not used by equip or deregister." },
      "reblade_fidelity": { "type": "string", "enum": ["none", "in", "out", "both"], "description": "For register_autoexport and export only: reblade layer (none|in|out|both). register: 'out' at creation, presence-masked on re-register, signed into the stack signature. export: the mode for this one-off render; omitted = 'out'. Not used by equip or deregister." }
    },
    "required": ["action"]
  }
}
```

The key-bearing actions' `vambrace` argument is the routing key itself (§1.9); the schema
carries it as an optional property (UUIDv7 pattern) — `equip` takes no key, so it is omitted
there — and it is validated fail-closed exactly like every routed verb.

## 4. Migration notes (source → Qatena)

- Migration output lands in a vambrace created by the `vambrace` verb (`action: "equip"`); there is
  no separate migration database path, and `QATENA_DB` is retired (§1.9).
- Source pins → `chain_headers` (+ blades from split points; migration script's problem, not the schema's).
- Source `pins` prose references → `reblades` rows (`name-ref` for bare mentions; `source` /
  `part-of` where the prose is structural).
- Source `open_items.refs` and `ruling_tags` → dissolved into `reblades` (the one-graph design).
- Source log rows → `logs` with `log_refs` backfilled where the prose identifies affected
  entities; unidentifiable entries get no refs.
- State registers are seeded as in §2; unknown source state strings are a migration error, not a silent passthrough.

## 5. Open items

1. **UUID storage → `BLOB(16)`** — the project's finalizing step (storage-only; tool surface unchanged).
2. **`history` mechanism** — field-level old→new audit (tables/triggers), contract reserved.
3. **Agent-authored log entries** — a 19th verb, if wanted; v1's `logs` are system-minted only.
4. **FTS5 for `search`** — only if LIKE-probe latency ever matters at this scale.
5. **`SPEC.md` resolutions (a)–(f) pending review** — reblades carry no UUID (§1.7) · `reblade_kinds` register (§2) · intentions upserted via `chain_put.intentions` (§3.1) · system-minted logs (§1.5) · case-insensitive aliases (§1.3) · `blade_order.pos` naming (§2).
6. **`vambrace` actions beyond `equip`** — list / destroy / label, etc.; deliberately deferred until the project is close to finalizing. (`register_autoexport` / `deregister_autoexport` are built, §3.18.)
