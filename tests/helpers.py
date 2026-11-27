"""Shared fixtures for the Qatena v1 test suite (stdlib unittest only).

Fresh temp-DB fixture per test class (setUp/tearDown, temp dir cleaned up).
Direct SQL is used only for (a) setup of spaces that have no v1 write verb
(open items, rulings, terms — SPEC 3 has none) and (b) asserting storage-level
invariants (blade_order gaplessness, log counts) — never to drive behavior
under test.
"""
import os
import re
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from qatena import db, schema                      # noqa: E402
from qatena.verbs import VerbError, execute        # noqa: E402
from qatena.uuid7 import new_uuid7                 # noqa: E402

# Tool-surface ISO-8601 UTC with milliseconds (SPEC 1.2).
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
REF_OPS = {"created", "modified", "deleted", "referenced"}


class QatenaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qatena-test-")
        self.conn = db.connect(os.path.join(self._tmp, "qatena.db"))
        schema.bootstrap(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- verb helpers -------------------------------------------------------
    def verb(self, v, **args):
        return execute(self.conn, v, args)

    def expect_error(self, code, v, **args):
        with self.assertRaises(VerbError) as ctx:
            execute(self.conn, v, args)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    # -- storage-level queries ----------------------------------------------
    def positions(self, chain_id):
        return [r["blade_id"] for r in self.conn.execute(
            "SELECT blade_id FROM blade_order WHERE chain_id=? ORDER BY pos", (chain_id,))]

    def assert_gapless(self, chain_id):
        pos = [r["pos"] for r in self.conn.execute(
            "SELECT pos FROM blade_order WHERE chain_id=? ORDER BY pos", (chain_id,))]
        self.assertEqual(pos, list(range(len(pos))))

    def log_count(self):
        return self.conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]

    def ref_ops(self, log_id=None):
        sql, args = "SELECT log_id, uuid, op FROM log_refs", ()
        if log_id is not None:
            sql, args = sql + " WHERE log_id=?", (log_id,)
        return [tuple(r) for r in self.conn.execute(sql + " ORDER BY log_id, uuid", args)]

    def pointers(self, **where):
        sql, args = "SELECT src_uuid, dst_uuid, kind, note, created FROM pointers WHERE 1=1", []
        for col in ("src_uuid", "dst_uuid", "kind"):
            if col in where:
                sql += " AND %s=?" % col
                args.append(where[col])
        return [tuple(r) for r in self.conn.execute(sql + " ORDER BY 1,2,3", tuple(args))]

    def pkeys(self, **where):
        return {(p[0], p[1], p[2]) for p in self.pointers(**where)}

    def chain_ids(self, name=None):
        sql, args = "SELECT id FROM chain_headers", ()
        if name is not None:
            sql, args = sql + " WHERE name=?", (name,)
        return [r["id"] for r in self.conn.execute(sql, args)]

    def section_names(self, chain_id):
        return [r["name"] for r in self.conn.execute(
            "SELECT s.name FROM chain_section_map m JOIN chain_sections s ON s.id=m.section_id "
            "WHERE m.chain_id=? ORDER BY s.name COLLATE NOCASE", (chain_id,))]

    def section_orientation(self, name):
        r = self.conn.execute(
            "SELECT orientation FROM chain_sections WHERE name=?", (name,)).fetchone()
        return r["orientation"] if r else None

    # -- direct inserts for spaces with no v1 write verb ---------------------
    def _now(self):
        return db.now_ns()

    def insert_term(self, term, text=None):
        tid, now = new_uuid7(), self._now()
        self.conn.execute(
            "INSERT INTO terms (id,term,text,created,updated) VALUES (?,?,?,?,?)",
            (tid, term, text, now, now))
        return tid

    def insert_open_item(self, name, state="open", text=None):
        oid, now = new_uuid7(), self._now()
        self.conn.execute(
            "INSERT INTO open_items (id,name,state,text,created,updated) VALUES (?,?,?,?,?,?)",
            (oid, name, state, text, now, now))
        return oid

    def insert_ruling(self, number, name, state="active", body=None):
        rid, now = new_uuid7(), self._now()
        self.conn.execute(
            "INSERT INTO rulings (id,number,name,state,body,created,updated) "
            "VALUES (?,?,?,?,?,?,?)",
            (rid, number, name, state, body, now, now))
        return rid

    def insert_section(self, name, orientation="B"):
        sid, now = new_uuid7(), self._now()
        self.conn.execute(
            "INSERT INTO chain_sections (id,name,orientation,created,updated) "
            "VALUES (?,?,?,?,?)",
            (sid, name, orientation, now, now))
        return sid
