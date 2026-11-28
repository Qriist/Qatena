"""Full DDL from SPEC 2, made idempotent.

CREATE TABLE -> CREATE TABLE IF NOT EXISTS
CREATE INDEX -> CREATE INDEX IF NOT EXISTS
seed INSERTs   -> INSERT OR IGNORE

Column definitions, constraints, comments, and seed values are verbatim from
the spec (modulo the idempotency keywords).
"""

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS chain_headers (
  id      TEXT(36) NOT NULL PRIMARY KEY,           -- UUIDv7
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- alias
  created INTEGER NOT NULL,                        -- Unix ns
  updated INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS chain_intentions (
  id          TEXT(36) NOT NULL PRIMARY KEY,       -- UUIDv7
  name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  orientation TEXT    NOT NULL CHECK (orientation IN ('W', 'B')),
    -- W = mirrored (fast re-orientation), B = pulled on demand
  created     INTEGER NOT NULL,
  updated     INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS chain_intention_map (
  chain_id   TEXT(36) NOT NULL REFERENCES chain_headers(id)  ON DELETE CASCADE,
  intention_id TEXT(36) NOT NULL REFERENCES chain_intentions(id) ON DELETE CASCADE,
  PRIMARY KEY (chain_id, intention_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_csm_intention ON chain_intention_map (intention_id)",
    """CREATE TABLE IF NOT EXISTS chain_blades (
  id       TEXT(36) NOT NULL PRIMARY KEY,          -- UUIDv7
  chain_id TEXT(36) NOT NULL REFERENCES chain_headers(id) ON DELETE CASCADE,
  text     TEXT    NOT NULL,
  created  INTEGER NOT NULL,
  updated  INTEGER NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_blades_chain ON chain_blades (chain_id)",
    """CREATE TABLE IF NOT EXISTS blade_order (
  chain_id TEXT(36) NOT NULL REFERENCES chain_headers(id) ON DELETE CASCADE,
  blade_id TEXT(36) NOT NULL REFERENCES chain_blades(id)  ON DELETE CASCADE,
  pos      INTEGER NOT NULL,
  PRIMARY KEY (chain_id, pos),
  UNIQUE (chain_id, blade_id)
)""",
    """CREATE TABLE IF NOT EXISTS reblade_kinds (                        -- seeded register, extensible
  kind TEXT NOT NULL PRIMARY KEY
)""",
    """INSERT OR IGNORE INTO reblade_kinds (kind) VALUES
  ('name-ref'), ('supersedes'), ('part-of'), ('source'), ('see'),
  ('conflict'), ('depends-on')""",
    """CREATE TABLE IF NOT EXISTS reblades (
  src_uuid TEXT(36) NOT NULL,   -- endpoint, validated server-side (polymorphic)
  dst_uuid TEXT(36) NOT NULL,
  kind     TEXT    NOT NULL REFERENCES reblade_kinds(kind),
  note     TEXT,
  created  INTEGER NOT NULL,
  PRIMARY KEY (src_uuid, dst_uuid, kind)
)""",
    "CREATE INDEX IF NOT EXISTS idx_reblades_dst ON reblades (dst_uuid)",
    """CREATE TABLE IF NOT EXISTS logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- dense, monotonic, append-only; never a reblade endpoint
  created INTEGER NOT NULL,
  title   TEXT,
  area    TEXT,
  body    TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_logs_created ON logs (created)",
    """CREATE TABLE IF NOT EXISTS log_refs (
  log_id INTEGER   NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
  uuid   TEXT(36) NOT NULL,   -- deliberately NOT a foreign key: must outlive deleted entities
  op     TEXT      NOT NULL CHECK (op IN ('created', 'modified', 'deleted', 'referenced')),
  PRIMARY KEY (log_id, uuid, op)
)""",
    "CREATE INDEX IF NOT EXISTS idx_log_refs_uuid ON log_refs (uuid)",
    """CREATE TABLE IF NOT EXISTS open_item_states (
  name TEXT NOT NULL PRIMARY KEY
)""",
    "INSERT OR IGNORE INTO open_item_states (name) VALUES ('open'), ('shelved'), ('resolved')",
    """CREATE TABLE IF NOT EXISTS open_items (
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
)""",
    """CREATE TABLE IF NOT EXISTS ruling_states (
  name TEXT NOT NULL PRIMARY KEY
)""",
    "INSERT OR IGNORE INTO ruling_states (name) VALUES ('active'), ('retracted'), ('corrected'), ('final')",
    """CREATE TABLE IF NOT EXISTS rulings (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  number  TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- human alias, e.g. "R-014"
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  state   TEXT    NOT NULL REFERENCES ruling_states(name),
  body    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS terms (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  term    TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- the term *is* its name
  text    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS autoexport_registers (
  root_uuid        TEXT(36) NOT NULL,
  emit_path        TEXT     NOT NULL,
  emit_format      TEXT     NOT NULL,
  reblade_fidelity TEXT     NOT NULL DEFAULT 'out',  -- none | in | out | both
  last_sig         TEXT,                -- NULL until first successful export
  last_emitted     INTEGER,             -- Unix ns of last successful export
  created          INTEGER NOT NULL,
  PRIMARY KEY (root_uuid, emit_path)
)""",
    """CREATE TABLE IF NOT EXISTS deflected_writes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- dense trace like logs; NOT a UUID; never a reblade endpoint
  created     INTEGER NOT NULL,                   -- Unix ns
  group_id    TEXT(36),                           -- UUIDv7; shared by the ops of one failed batch; NULL for a single call
  op          TEXT    NOT NULL,                   -- verb name; for batch rows: the op's name
  args        TEXT    NOT NULL,                   -- canonical JSON (sort_keys, compact; lists keep order)
  error       TEXT,                               -- 'code: message'; NULL on batch siblings
  state       TEXT    NOT NULL CHECK (state IN ('pending', 'cleared')),
  cleared_at  INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS idx_deflected_writes_state ON deflected_writes (state)",
    """CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,   -- armory-level key (k='saga' = the human-facing name; the in-db copy is the source of truth, the armory ledger is a display cache)
  v TEXT NOT NULL
)""",
]


def _migrate_autoexport_registers(conn):
    """Idempotent migration for vambraces bootstrapped before reblade_fidelity
    existed: the table above was created without the column, so add it.
    SQLite allows ADD COLUMN with NOT NULL when the default is a constant."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(autoexport_registers)")}
    if "reblade_fidelity" not in cols:
        conn.execute(
            "ALTER TABLE autoexport_registers "
            "ADD COLUMN reblade_fidelity TEXT NOT NULL DEFAULT 'out'"
        )


def _migrate_open_items_freeform(conn):
    """Idempotent migration for vambraces bootstrapped before the seven
    freeform tracking columns existed: add any missing one to open_items
    (guarded per column, so a db missing any or all seven migrates safely).
    Fresh vambraces get all seven from the DDL above, so this no-ops there."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(open_items)")}
    for name, decl in (
        ("freeform_current", "INTEGER DEFAULT NULL"),
        ("freeform_pending", "INTEGER DEFAULT NULL"),
        ("freeform_total", "INTEGER DEFAULT NULL"),
        ("freeform_next", "TEXT DEFAULT NULL"),
        ("freeform_last", "TEXT DEFAULT NULL"),
        ("freeform_due", "TEXT DEFAULT NULL"),
        ("freeform_priority", "TEXT DEFAULT NULL"),
    ):
        if name not in cols:
            conn.execute("ALTER TABLE open_items ADD COLUMN %s %s" % (name, decl))


def _migrate_terminology(conn):
    """Idempotent terminology-tightening migration for pre-existing vambraces
    (the 2026-11-27 rename: intention/reblade/armory vocabulary).

    Renames the four tables (dropping their old-named indexes), renames the
    two affected columns, and carries the stored values along (register
    root_space, log areas). Each step is guarded, so fresh vambraces (the DDL
    in STATEMENTS already carries the new names) and already-migrated ones
    no-op. Table/column identifiers are static code constants; no
    user-supplied value ever reaches the SQL text.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for old, new in (
        ("chain_sections", "chain_intentions"),
        ("chain_section_map", "chain_intention_map"),
        ("pointers", "reblades"),
        ("pointer_kinds", "reblade_kinds"),
    ):
        if old in tables and new not in tables:
            conn.execute("ALTER TABLE %s RENAME TO %s" % (old, new))
            tables.discard(old)
            tables.add(new)
    # SQLite does not rename index objects when their table is renamed; drop
    # the old-named ones so the DDL in STATEMENTS creates the new-named ones.
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    for old_idx in ("idx_csm_section", "idx_pointers_dst"):
        if old_idx in indexes:
            conn.execute("DROP INDEX %s" % old_idx)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chain_intention_map)")}
    if "section_id" in cols:
        conn.execute("ALTER TABLE chain_intention_map "
                     "RENAME COLUMN section_id TO intention_id")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(autoexport_registers)")}
    if "pointer_fidelity" in cols:
        conn.execute("ALTER TABLE autoexport_registers "
                     "RENAME COLUMN pointer_fidelity TO reblade_fidelity")
    if "root_space" in cols:
        conn.execute("UPDATE autoexport_registers "
                     "SET root_space = 'intention' WHERE root_space = 'section'")
    if "logs" in tables:
        conn.execute(
            "UPDATE logs SET area = 'reblade_add' WHERE area = 'pointer_add'")
        conn.execute(
            "UPDATE logs SET area = 'reblade_remove' WHERE area = 'pointer_remove'")
        # log areas store the entity name ('pointer') for reblade writes
        conn.execute("UPDATE logs SET area = 'reblade' WHERE area = 'pointer'")


def bootstrap(conn):
    """Create the full schema on a fresh DB; no-op on an existing one.

    Idempotent: safe to run on every server start. Pre-existing vambraces are
    migrated in place (terminology rename first, then autoexport_registers
    gains reblade_fidelity if it predates that column too, then open_items
    gains the seven freeform tracking columns if it predates any of them).
    """
    _migrate_terminology(conn)
    for stmt in STATEMENTS:
        conn.execute(stmt)
    _migrate_autoexport_registers(conn)
    _migrate_open_items_freeform(conn)


# The per-vambrace history sidecar <key>.history.db (SPEC 3.17): a
# separate SQLite file beside <key>.db, ATTACHed at every routed open so
# a revision row and the write it record land or roll back in ONE
# spanning BEGIN IMMEDIATE..COMMIT (SPEC 1.4). All-scalar, no JSON, no
# reason column: the write's logs row joined by log_id is the "why".

SIDECAR_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS genesis (
  kind    TEXT    NOT NULL CHECK (kind IN ('equip', 'acquire')),
  detail  TEXT,               -- acquire: the mythos source path; equip: NULL
  log_id  INTEGER,            -- acquire: the main db's genesis log row; equip: NULL
  created INTEGER NOT NULL    -- Unix ns
)""",
    """CREATE TABLE IF NOT EXISTS revisions (
  id      INTEGER PRIMARY KEY,  -- dense, append-only; never a reblade endpoint
  uuid    TEXT(36) NOT NULL,    -- the entity the field belongs to
  field   TEXT    NOT NULL,     -- the content field name ('intention' for membership)
  old_val TEXT,                 -- NULL = absent before
  new_val TEXT,                 -- NULL = absent after
  log_id  INTEGER NOT NULL,     -- the write's logs row; joined, not duplicated
  created INTEGER NOT NULL      -- Unix ns
)""",
    "CREATE INDEX IF NOT EXISTS idx_revisions_uuid ON revisions (uuid, field, created)",
]


def bootstrap_sidecar(conn):
    """Create the history sidecar schema on a fresh <key>.history.db;
    no-op on an existing one. CREATE IF NOT EXISTS makes re-bootstrapping
    idempotent, so pre-existing sidecars survive (mirroring bootstrap for
    the main db)."""
    for stmt in SIDECAR_STATEMENTS:
        conn.execute(stmt)
