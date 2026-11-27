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
    """CREATE TABLE IF NOT EXISTS chain_sections (
  id          TEXT(36) NOT NULL PRIMARY KEY,       -- UUIDv7
  name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  orientation TEXT    NOT NULL CHECK (orientation IN ('W', 'B')),
    -- W = mirrored (fast re-orientation), B = pulled on demand
  created     INTEGER NOT NULL,
  updated     INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS chain_section_map (
  chain_id   TEXT(36) NOT NULL REFERENCES chain_headers(id)  ON DELETE CASCADE,
  section_id TEXT(36) NOT NULL REFERENCES chain_sections(id) ON DELETE CASCADE,
  PRIMARY KEY (chain_id, section_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_csm_section ON chain_section_map (section_id)",
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
    """CREATE TABLE IF NOT EXISTS pointer_kinds (                        -- seeded register, extensible **[resolution b]**
  kind TEXT NOT NULL PRIMARY KEY
)""",
    """INSERT OR IGNORE INTO pointer_kinds (kind) VALUES
  ('name-ref'), ('supersedes'), ('part-of'), ('source'), ('see')""",
    """CREATE TABLE IF NOT EXISTS pointers (
  src_uuid TEXT(36) NOT NULL,   -- endpoint, validated server-side (polymorphic)
  dst_uuid TEXT(36) NOT NULL,
  kind     TEXT    NOT NULL REFERENCES pointer_kinds(kind),
  note     TEXT,
  created  INTEGER NOT NULL,
  PRIMARY KEY (src_uuid, dst_uuid, kind)
)""",
    "CREATE INDEX IF NOT EXISTS idx_pointers_dst ON pointers (dst_uuid)",
    """CREATE TABLE IF NOT EXISTS logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- dense, monotonic, append-only; never a pointer endpoint
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
  updated INTEGER NOT NULL
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
  root_uuid    TEXT(36) NOT NULL,
  emit_path    TEXT     NOT NULL,
  emit_format  TEXT     NOT NULL,
  last_sig     TEXT,                -- NULL until first successful export
  last_emitted INTEGER,             -- Unix ns of last successful export
  created      INTEGER NOT NULL,
  PRIMARY KEY (root_uuid, emit_path)
)""",
]


def bootstrap(conn):
    """Create the full schema on a fresh DB; no-op on an existing one.

    Idempotent: safe to run on every server start.
    """
    for stmt in STATEMENTS:
        conn.execute(stmt)
