# Qatena Spec — v1

**Status:** implementable contract for the settled design in `DESIGN.md`.
Resolutions the design left rough are marked **[resolution]** — the set of those is pending review.

**Runtime:** Python 3, stdlib `sqlite3`, a single stdio MCP server process (goose extension).
One database file, created and owned by the server on first start. The server is the **sole write
surface**: no other process or tool may open the DB for writes.

## 1. Conventions

### 1.1 Identity

- All IDs are UUIDv7, canonical lowercase hyphenated 36-char strings. The server mints them
  (`uuid.uuid7()`-equivalent); clients never generate IDs.
- Stored as `TEXT(36)` for v1. **Finalizing step (end of project):** convert all ID columns to
  `BLOB(16)`. Storage-only change; the tool surface keeps 36-char strings either way.
- The only non-UUID ID: `logs.id` (strictly sequential integer). Logs are never pointer endpoints.

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
  enumerates each minted pointer.

### 1.4 Transactions

- Every write verb runs in exactly one transaction: `BEGIN IMMEDIATE` → work → `COMMIT`.
  Any failure → `ROLLBACK`. Fail-closed: a partial write is never observable.
- `batch` is one transaction spanning N ops applied **in order**; later ops observe earlier
  ops' effects (including auto-linking against them).
- Reads (`get`, `search`, `list`, `pointers_of`, `follow`, `history`) are side-effect-free:
  they mint no logs and no `log_refs`.

### 1.5 System-minted logs **[resolution d]**

Every write transaction mints, in the same transaction:

- **One `logs` row**: `title` = short summary (e.g. `chain_put "The Forge"`),
  `area` = primary space (`chain` | `section` | `blade` | `pointer` | `open_item` |
  `ruling` | `term` | `mixed`), `body` = canonical JSON describing the change
  (per-op records: op, target UUID/name, fields changed with old→new where available).
- **`log_refs` rows**: one per UUID whose row was created / modified / deleted, or that new
  data in the transaction references without changing it:
  `op` ∈ `created` | `modified` | `deleted` | `referenced`.
  (`referenced` = e.g. the endpoints of a new pointer row.)

The settled verb surface has **no log-write verb**: the timeline is the mechanical trace of the
system's own footprint. Agent-authored narrative log entries would be an 18th verb — open, not
part of v1 (see §5).

### 1.6 Auto-linking

- **Triggers:** blade text writes — `blade_insert`, `blade_edit`, and their `batch` forms.
  (`chain_put` carries no body text, so names written there do not link.)
- **Exact matches:** each token of the written text equal to a registered alias
  (case-insensitive) mints a `name-ref` pointer from the written blade to that entity.
  Idempotent: an existing `(src, dst, kind)` is not duplicated.
- **Candidates:** registered aliases that do *not* exact-match but fuzzy-match some token —
  the alias begins with a token of ≥ 4 chars, or Levenshtein distance ≤ 2 to a token —
  are returned in the response (max 5, each with the token that triggered it).
  **Never linked silently.**
- **Reconciliation** (`blade_edit`): the blade's outgoing `name-ref` pointers are recomputed
  from scratch; stale ones are deleted, new ones minted. Both lists appear in the response.
- Registered alias set = the name columns of all six entity tables (chain names, section names,
  open item names, ruling names, ruling numbers, terms).

### 1.7 Pointer endpoints

- A valid endpoint is any UUID resolving in `chain_headers`, `chain_blades`, `chain_sections`,
  `open_items`, `rulings`, or `terms`. Pointers, logs, and state registers are **not** endpoints.
- `pointers.src_uuid` / `dst_uuid` are validated **by the server**, not by foreign keys —
  the endpoint space is polymorphic.
- Pointers carry **no UUID of their own**; identity is `(src_uuid, dst_uuid, kind)`
  **[resolution a]** — consistent with the settled endpoint enumeration.
- Deleting a chain or blade cascade-deletes its **outgoing** pointers and **reports** incoming
  ones in the response (they may still be meaningful; retarget or remove explicitly).
- `chain_merge` retargets pointers aimed at the dissolved header onto the survivor; the
  dissolved UUID stops resolving (recoverable via its `log_refs` trace → `history`).

### 1.8 Invariants (enforced server-side, inside the write transaction)

- `blade_order.pos` is **gapless 0..n−1 per chain**; re-established in the same transaction as
  any blade insert / delete / move / split / merge.
- Section membership writes and the owning chain's `updated` are always written together.
- Auto-log (§1.5) and auto-linking (§1.6) are part of the write transaction, not afterthoughts.
- `logs` is append-only: no verb updates or deletes a log row or its `log_refs`.

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

CREATE TABLE chain_sections (
  id          TEXT(36) NOT NULL PRIMARY KEY,       -- UUIDv7
  name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  orientation TEXT    NOT NULL CHECK (orientation IN ('W', 'B')),
    -- W = mirrored (fast re-orientation), B = pulled on demand
  created     INTEGER NOT NULL,
  updated     INTEGER NOT NULL
);

CREATE TABLE chain_section_map (
  chain_id   TEXT(36) NOT NULL REFERENCES chain_headers(id)  ON DELETE CASCADE,
  section_id TEXT(36) NOT NULL REFERENCES chain_sections(id) ON DELETE CASCADE,
  PRIMARY KEY (chain_id, section_id)
);
CREATE INDEX idx_csm_section ON chain_section_map (section_id);

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

CREATE TABLE pointer_kinds (                        -- seeded register, extensible **[resolution b]**
  kind TEXT NOT NULL PRIMARY KEY
);
INSERT INTO pointer_kinds (kind) VALUES
  ('name-ref'), ('supersedes'), ('part-of'), ('source'), ('see');

CREATE TABLE pointers (
  src_uuid TEXT(36) NOT NULL,   -- endpoint, validated server-side (polymorphic)
  dst_uuid TEXT(36) NOT NULL,
  kind     TEXT    NOT NULL REFERENCES pointer_kinds(kind),
  note     TEXT,
  created  INTEGER NOT NULL,
  PRIMARY KEY (src_uuid, dst_uuid, kind)
);
CREATE INDEX idx_pointers_dst ON pointers (dst_uuid);
```

### Non-core (creative-project tracking)

```sql
CREATE TABLE logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- dense, monotonic, append-only; never a pointer endpoint
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
  updated INTEGER NOT NULL
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

**Indexing notes.** The UNIQUE alias columns give implicit indexes. Beyond PKs, only two
explicit indexes exist (`idx_csm_section`, `idx_pointers_dst`, `idx_logs_created`,
`idx_log_refs_uuid`, `idx_blades_chain`); all tables are personal-project scale — no
speculative indexes. `search` is LIKE-probe based in v1 (FTS5 is a later optimization, §5).

## 3. Verb surface (17 MCP tools)

**Shared field definitions** (used throughout the schemas below):

- **uuid** — `string`, canonical hyphenated UUIDv7;
  pattern `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`
- **iso** — `string`, ISO-8601 UTC with milliseconds (`2026-11-27T06:31:00.123Z`)
- **kind enum** — `["name-ref", "supersedes", "part-of", "source", "see"]`
- **entity spaces** — `chain | section | blade | open_item | ruling | term | log`

**Addressing.** Where a tool takes an entity address, `id` (uuid) takes precedence over `name`;
at least one must be supplied. If both are supplied, the name must equal the resolved entity's
current name (a rename is a name-only call).

**Response shapes.**

```jsonc
// write verbs:
{
  "log_id": 128,                          // the minted logs row
  "...": "changed entity echo — composite for chains (header + ordered blades + section names), bare record otherwise",
  "minted":     [ {"src": "…", "dst": "…", "kind": "name-ref"} ],   // auto-linking ops only
  "candidates": [ {"name": "…", "space": "chain", "token": "…"} ]   // auto-linking ops only
}

// reads: the record(s), plainly

// failure (MCP isError = true):
{ "error": { "code": "duplicate_name", "message": "chain name 'The Forge' is taken (id 0192…)" } }
```

**Error codes:** `not_found` · `duplicate_name` · `unresolvable_endpoint` · `invalid_index` ·
`validation_failed` · `not_implemented` · `db_error` (always fail-closed: rolled back, nothing written).

---

### 3.1 `chain_put`

Upsert a chain by UUID or name; fresh UUIDv7 if absent. Presence-mask: only supplied fields
change. Renames and refiling are just this.

```json
{
  "name": "chain_put",
  "description": "Create or update a chain (note). Address by id or name; if neither matches, a fresh UUIDv7 is minted and the name is required. Presence-mask: only supplied fields change. Sections, if present, replace membership with exactly this set and upsert each section by name (absent sections are created; orientation defaults to 'B', or the value given; an existing section's orientation updates only if supplied). Returns the chain composite and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Chain uuid. Takes precedence over name." },
      "name": { "type": "string", "minLength": 1, "description": "Chain name (alias). Unique within the chain space, case-insensitive." },
      "sections": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": { "type": "string", "minLength": 1 },
            "orientation": { "type": "string", "enum": ["W", "B"] }
          },
          "required": ["name"]
        },
        "description": "If present, replaces the chain's section membership with exactly this set. Sections are upserted by name."
      }
    },
    "minProperties": 1
  }
}
```

### 3.2 `chain_delete`

```json
{
  "name": "chain_delete",
  "description": "Delete a chain by id or name. Blades and section memberships are cascade-deleted; outgoing pointers are cascade-deleted and incoming pointers are preserved and reported in the response (they may still be meaningful). Returns the list of incoming pointers and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "name": { "type": "string", "minLength": 1 }
    },
    "minProperties": 1
  }
}
```

### 3.3 `chain_split`

```json
{
  "name": "chain_split",
  "description": "Carve a set of blades out of a source chain into a new chain. Blade UUIDs and their pointers travel intact. Blades are removed from the source and appended to the new chain in the order listed. The new chain's sections are set from the optional sections param (upsert semantics, as in chain_put). Returns both chain composites and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "source": { "type": "string", "description": "Source chain id or name." },
      "blades": { "type": "array", "items": { "type": "string" }, "minItems": 1, "description": "Blade UUIDs to move, in the order they should appear in the new chain." },
      "name": { "type": "string", "minLength": 1, "description": "Name for the new chain." },
      "sections": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": { "type": "string" },
            "orientation": { "type": "string", "enum": ["W", "B"] }
          },
          "required": ["name"]
        },
        "description": "Optional initial section membership for the new chain."
      }
    },
    "required": ["source", "blades", "name"]
  }
}
```

### 3.4 `chain_merge`

```json
{
  "name": "chain_merge",
  "description": "Fold one chain into a survivor. The dissolved chain's blades are appended to the survivor in their order; section memberships are unioned into the survivor; pointers aimed at the dissolved header are retargeted onto the survivor; the dissolved header's UUID stops resolving (recoverable via history on that UUID). Blade UUIDs and their pointers are unchanged. Returns the survivor composite, the retargeted pointers, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "into": { "type": "string", "description": "Surviving chain id or name." },
      "fold": { "type": "string", "description": "Chain to dissolve (id or name)." }
    },
    "required": ["into", "fold"]
  }
}
```

### 3.5 `blade_insert`

```json
{
  "name": "blade_insert",
  "description": "Insert a blade into a chain at a 0-based index (default: end). Runs auto-linking: exact name matches mint name-ref pointers; near-matches are returned as candidates (never linked). Returns the new blade UUID, its index, minted pointers, candidates, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "chain": { "type": "string", "description": "Target chain id or name." },
      "text": { "type": "string", "minLength": 1 },
      "index": { "type": "integer", "minimum": 0, "description": "0-based position; omitted = append. Must be <= current blade count." }
    },
    "required": ["chain", "text"]
  }
}
```

### 3.6 `blade_edit`

```json
{
  "name": "blade_edit",
  "description": "Replace a blade's text by UUID. Reconciles the blade's outgoing name-ref pointers: stale ones are deleted, new exact matches minted; near-matches returned as candidates. Returns the updated blade, minted/deleted pointers, candidates, and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." },
      "text": { "type": "string", "minLength": 1 }
    },
    "required": ["id", "text"]
  }
}
```

### 3.7 `blade_move`

```json
{
  "name": "blade_move",
  "description": "Move a blade to a new position within its chain, or to another chain. blade_order is re-established gapless in the same transaction. The blade's UUID and pointers are unchanged. Returns the blade's new chain and index and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." },
      "to_chain": { "type": "string", "description": "Destination chain id or name; omitted = same chain." },
      "index": { "type": "integer", "minimum": 0, "description": "0-based destination position, measured after removal; omitted = end." }
    },
    "required": ["id"]
  }
}
```

### 3.8 `blade_delete`

```json
{
  "name": "blade_delete",
  "description": "Delete a blade by UUID. Its outgoing pointers are cascade-deleted; incoming pointers are preserved and reported. blade_order is re-established gapless in the same transaction. Returns the incoming pointers and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "description": "Blade UUID." }
    },
    "required": ["id"]
  }
}
```

### 3.9 `batch`

```json
{
  "name": "batch",
  "description": "Execute a list of write ops (chain_put, chain_delete, chain_split, chain_merge, blade_insert, blade_edit, blade_move, blade_delete, pointer_add, pointer_remove) atomically: one transaction, all-or-nothing, applied in order. Later ops observe earlier ops' effects, including auto-linking. Returns per-op results (with minted/candidates where applicable) and the single minted log id. Any op failure rolls back the whole batch.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "ops": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "properties": {
            "op": {
              "type": "string",
              "enum": ["chain_put", "chain_delete", "chain_split", "chain_merge", "blade_insert", "blade_edit", "blade_move", "blade_delete", "pointer_add", "pointer_remove"]
            },
            "args": {
              "type": "object",
              "description": "Arguments for this op, with the exact shape of the standalone verb's input."
            }
          },
          "required": ["op", "args"]
        }
      }
    },
    "required": ["ops"]
  }
}
```

### 3.10 `pointer_add`

```json
{
  "name": "pointer_add",
  "description": "Create an explicit typed link between two UUIDs. kind is the semantic relationship, never the endpoint type (that is implicit in the UUID). Endpoints must resolve in the tracked entity space (logs and registers are not endpoints). Idempotent on (src, dst, kind): an existing pointer is returned, not duplicated. Returns the pointer and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "src": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "dst": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] },
      "note": { "type": "string" }
    },
    "required": ["src", "dst", "kind"]
  }
}
```

### 3.11 `pointer_remove`

```json
{
  "name": "pointer_remove",
  "description": "Remove the pointer identified by (src, dst, kind). Errors with not_found if absent. Returns the removed pointer and the minted log id.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "src": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "dst": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] }
    },
    "required": ["src", "dst", "kind"]
  }
}
```

### 3.12 `get`

```json
{
  "name": "get",
  "description": "Fetch any tracked entity by UUID. Chains return the composite: header, ordered blades (UUID, index, text), and section names. Blades return the blade plus its chain's name. Sections, open items, rulings, and terms return their bare records. A dissolved (merged) UUID returns not_found; the dissolving merge is recoverable via history on that UUID.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" }
    },
    "required": ["id"]
  }
}
```

### 3.13 `search`

```json
{
  "name": "search",
  "description": "Multi-probe union search across entity spaces: chain names, blade text, section names, open item names/text, ruling numbers/names/bodies, term names/definitions, log titles/bodies. Probes are OR'd (substring, case-insensitive). Results are unioned, each with its entity space, UUID, name, and a context window around the match. Returns up to limit hits.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "probes": { "type": "array", "items": { "type": "string", "minLength": 1 }, "minItems": 1, "description": "Search terms; unioned across spaces." },
      "spaces": {
        "type": "array",
        "items": { "type": "string", "enum": ["chain", "blade", "section", "open_item", "ruling", "term", "log"] },
        "description": "Restrict to these entity spaces; omitted = all."
      },
      "context": { "type": "integer", "minimum": 0, "description": "Characters of context on each side of a match. Default 120." },
      "limit": { "type": "integer", "minimum": 1, "description": "Maximum hits. Default 50." }
    },
    "required": ["probes"]
  }
}
```

### 3.14 `list`

```json
{
  "name": "list",
  "description": "List an entity space with filters. Returns bare records (chains as headers plus section names; blade bodies only if include_blades). Filters are space-specific keys, ignored (with a warning in the response) where inapplicable: chains: section (name), name_prefix; sections: orientation; open items / rulings: state; logs: uuid (via log_refs), since, until (iso); any space: name_prefix where the space has a name/alias field.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "space": { "type": "string", "enum": ["chain", "section", "open_item", "ruling", "term", "log"] },
      "filter": {
        "type": "object",
        "properties": {
          "section": { "type": "string" },
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
    "required": ["space"]
  }
}
```

### 3.15 `pointers_of`

```json
{
  "name": "pointers_of",
  "description": "Incoming and/or outgoing pointers for a UUID, filterable by kind. Each edge is returned as {src, dst, kind, note, created} with both endpoints resolved: their entity space and name.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "direction": { "type": "string", "enum": ["in", "out", "both"], "description": "Default both." },
      "kind": { "type": "string", "enum": ["name-ref", "supersedes", "part-of", "source", "see"] }
    },
    "required": ["id"]
  }
}
```

### 3.16 `follow`

```json
{
  "name": "follow",
  "description": "Multi-hop traversal from a UUID through the pointer graph. Returns visited nodes {uuid, name, space} with the edge kinds traversed, up to max_hops. Cycles are detected and pruned. Chain nodes include blades only if expand_chains.",
  "inputSchema": {
    "type": "object",
    "properties": {
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
    "required": ["from"]
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
      "id": { "type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" },
      "limit": { "type": "integer", "minimum": 1, "description": "Default 20." }
    },
    "required": ["id"]
  }
}
```

## 4. Migration notes (source → Qatena)

- Source pins → `chain_headers` (+ blades from split points; migration script's problem, not the schema's).
- Source `pins` prose references → `pointers` rows (`name-ref` for bare mentions; `source` /
  `part-of` where the prose is structural).
- Source `open_items.refs` and `ruling_tags` → dissolved into `pointers` (the one-graph design).
- Source log rows → `logs` with `log_refs` backfilled where the prose identifies affected
  entities; unidentifiable entries get no refs.
- State registers are seeded as in §2; unknown source state strings are a migration error, not a silent passthrough.

## 5. Open items

1. **UUID storage → `BLOB(16)`** — the project's finalizing step (storage-only; tool surface unchanged).
2. **`history` mechanism** — field-level old→new audit (tables/triggers), contract reserved.
3. **Agent-authored log entries** — an 18th verb, if wanted; v1's `logs` are system-minted only.
4. **FTS5 for `search`** — only if LIKE-probe latency ever matters at this scale.
5. **`SPEC.md` resolutions (a)–(f) pending review** — pointers carry no UUID (§1.7) · `pointer_kinds` register (§2) · sections upserted via `chain_put.sections` (§3.1) · system-minted logs (§1.5) · case-insensitive aliases (§1.3) · `blade_order.pos` naming (§2).
