"""Schema bootstrap, seeded registers, UUIDv7 format/ordering (SPEC 1.1, 2)."""
import os
import time

from qatena import db, schema
from qatena.uuid7 import new_uuid7
from helpers import QatenaTestCase, UUID7_RE


class Bootstrap(QatenaTestCase):
    def test_bootstrap_idempotent(self):
        schema.bootstrap(self.conn)
        schema.bootstrap(self.conn)  # second run: no error
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "chain_headers", "chain_intentions", "chain_intention_map", "chain_blades",
            "blade_order", "reblades", "reblade_kinds", "logs", "log_refs",
            "open_item_states", "open_items", "ruling_states", "rulings", "terms"}
        self.assertTrue(expected.issubset(tables))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM chain_headers").fetchone()["c"], 0)


class SeededRegisters(QatenaTestCase):
    def test_reblade_kinds_exact(self):
        got = {r["kind"] for r in self.conn.execute("SELECT kind FROM reblade_kinds")}
        self.assertEqual(got, {"name-ref", "supersedes", "part-of", "source", "see", "conflict", "depends-on"})

    def test_open_item_states_exact(self):
        got = {r["name"] for r in self.conn.execute("SELECT name FROM open_item_states")}
        self.assertEqual(got, {"open", "shelved", "resolved"})

    def test_ruling_states_exact(self):
        got = {r["name"] for r in self.conn.execute("SELECT name FROM ruling_states")}
        self.assertEqual(got, {"active", "retracted", "corrected", "final"})


class Uuid7(QatenaTestCase):
    def test_format(self):
        for _ in range(25):
            u = new_uuid7()
            self.assertEqual(len(u), 36)
            self.assertRegex(u, UUID7_RE)
            self.assertEqual(u[14], "7")      # version nibble
            self.assertIn(u[19], "89ab")      # variant nibble

    def test_ordered_2ms_apart(self):
        # Only cross-generation ordering at a 2ms gap is asserted (not
        # within-millisecond ordering, which the layout does not guarantee).
        a = new_uuid7()
        time.sleep(0.002)
        b = new_uuid7()
        self.assertLess(a, b)


class OpenItemsFreeform(QatenaTestCase):
    """open_items freeform tracking columns (SPEC non-core DDL + bootstrap
    migration for pre-freeform vambraces)."""

    FREEFORM = (
        ("freeform_current", "INTEGER"),
        ("freeform_pending", "INTEGER"),
        ("freeform_total", "INTEGER"),
        ("freeform_next", "TEXT"),
        ("freeform_last", "TEXT"),
        ("freeform_due", "TEXT"),
        ("freeform_priority", "TEXT"),
    )

    # Pre-change open_items DDL, verbatim from git show HEAD:qatena/schema.py
    # (HEAD 7227558, before the freeform columns landed).
    LEGACY_OPEN_ITEMS = """CREATE TABLE open_items (
  id      TEXT(36) NOT NULL PRIMARY KEY,
  name    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  state   TEXT    NOT NULL REFERENCES open_item_states(name),
  text    TEXT,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
)"""

    def _columns(self, conn):
        return {r["name"]: r["type"] for r in conn.execute(
            "PRAGMA table_info(open_items)")}

    def test_fresh_vambrace_freeform_columns(self):
        cols = self._columns(self.conn)
        for name, decl in self.FREEFORM:
            self.assertEqual(cols.get(name), decl)

    def test_insert_without_freeform_defaults_null(self):
        self.insert_open_item("Task One")  # helper omits the seven columns
        row = self.conn.execute(
            "SELECT freeform_current, freeform_pending, freeform_total, "
            "freeform_next, freeform_last, freeform_due, "
            "freeform_priority FROM open_items").fetchone()
        self.assertEqual(
            tuple(row), (None, None, None, None, None, None, None))

    def test_legacy_db_migrates_in_place(self):
        # A db whose open_items predates the freeform columns: bootstrap
        # must add all seven in place, and a second run must no-op.
        con = db.connect(os.path.join(self._tmp, "legacy.db"))
        con.execute(
            "CREATE TABLE open_item_states (name TEXT NOT NULL PRIMARY KEY)")
        con.execute(
            "INSERT INTO open_item_states (name) "
            "VALUES ('open'), ('shelved'), ('resolved')")
        con.execute(self.LEGACY_OPEN_ITEMS)
        self.assertEqual(
            [c for c in self._columns(con)],
            ["id", "name", "state", "text", "created", "updated"])
        schema.bootstrap(con)
        cols = self._columns(con)
        for name, decl in self.FREEFORM:
            self.assertEqual(cols.get(name), decl)
        schema.bootstrap(con)  # second run: idempotent, no error
        self.assertEqual(self._columns(con), cols)
        con.close()

    def test_post_five_db_gains_due_priority(self):
        # A db migrated at the five-column point: it has the original five
        # freeform columns but not due/priority; bootstrap must add the
        # two in place, and a second run must no-op.
        con = db.connect(os.path.join(self._tmp, "post_five.db"))
        con.execute(
            "CREATE TABLE open_item_states (name TEXT NOT NULL PRIMARY KEY)")
        con.execute(
            "INSERT INTO open_item_states (name) "
            "VALUES ('open'), ('shelved'), ('resolved')")
        con.execute(self.LEGACY_OPEN_ITEMS)
        for name, decl in self.FREEFORM[:5]:
            con.execute(
                "ALTER TABLE open_items ADD COLUMN %s %s DEFAULT NULL"
                % (name, decl))
        cols = self._columns(con)
        self.assertNotIn("freeform_due", cols)
        self.assertNotIn("freeform_priority", cols)
        schema.bootstrap(con)
        cols = self._columns(con)
        for name, decl in self.FREEFORM:
            self.assertEqual(cols.get(name), decl)
        schema.bootstrap(con)  # second run: idempotent, no error
        self.assertEqual(self._columns(con), cols)
        con.close()


class OpenItemBodyOptional(QatenaTestCase):
    """The open item body (text) is OPTIONAL (SPEC freeform note, user-directed):
    a pure tracking row (name + state + freeform fields, no body) is a legitimate
    shape, so a single incremental freeform update never has to re-supply a
    massive body that tracks a single number."""

    def test_text_column_is_nullable(self):
        cols = {r["name"]: r["notnull"] for r in self.conn.execute(
            "PRAGMA table_info(open_items)")}
        self.assertEqual(cols["text"], 0)

    def test_bodyless_item_round_trips(self):
        oid = self.insert_open_item("Counter", text=None)
        # The use case: one incremental freeform update, no body involved.
        self.conn.execute("UPDATE open_items SET freeform_current=? WHERE id=?", (7, oid))
        self.conn.commit()
        got = self.verb("get", id=oid)
        self.assertEqual(got["name"], "Counter")
        self.assertIsNone(got["text"])
        self.assertEqual(got["freeform_current"], 7)
        rec = [r for r in self.verb("list", space="open_item")["items"] if r["id"] == oid][0]
        self.assertIsNone(rec["text"])
        hits = [h for h in self.verb("search", probes=["Counter"])["hits"] if h["id"] == oid]
        self.assertEqual([h["space"] for h in hits], ["open_item"])
