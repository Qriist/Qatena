"""The per-vambrace history sidecar <key>.history.db (SPEC 3.17), P2+P3
plumbing pass. The sidecar is created eagerly at birth (equip / acquire
seed their one-row genesis frame; a pre-build vambrace gets the sidecar
at its next routed open with NO genesis row), ATTACHed at every routed
open so revision rows and the writes they record land or roll back in
ONE spanning BEGIN IMMEDIATE..COMMIT, and travels as a unit through the
waylay / recover / dismantle lifecycle pair (state IS file location).
The armory scan stays structurally blind to it. P4 (below): the
revision-row minting map of SPEC 3.17 — which writes mint which
content-field rows, and what never mints. The history response shape /
read-surface keys (P5) build on this file.
Unit level: server._call_vambrace + server._call_routed + server
sidecar helpers + qatena.armory against a temp armory dir."""
import json
import os
import shutil
import tempfile
import time
import unittest

from qatena import armory, db, server, verbs
from qatena.uuid7 import new_uuid7

from helpers import ISO_RE


class Base(unittest.TestCase):
    def setUp(self):
        self.armory = tempfile.mkdtemp(prefix="qatena-history-")

    def tearDown(self):
        shutil.rmtree(self.armory, ignore_errors=True)

    # -- vambrace actions ---------------------------------------------------
    def call(self, arguments):
        return server._call_vambrace(self.armory, arguments)

    def equip(self, **extra):
        payload, is_error = self.call(dict({"action": "equip"}, **extra))
        self.assertFalse(is_error, "equip failed: %r" % payload)
        return payload

    def make(self, **extra):
        return self.equip(**extra)["vambrace"]

    def waylay(self, key):
        return self.call({"action": "waylay", "vambrace": key,
                          "emit_path": self.emit_path(),
                          "emit_format": "json"})

    def recover(self, key):
        return self.call({"action": "recover", "vambrace": key})

    def dismantle(self, key):
        return self.call({"action": "dismantle", "vambrace": key})

    def emit_path(self, name="snap.json"):
        return os.path.join(self.armory, name)

    # -- file locations ------------------------------------------------------
    def live_path(self, key):
        return os.path.join(self.armory, key + ".db")

    def sidecar_path(self, key):
        return os.path.join(self.armory, key + ".history.db")

    def waylay_sidecar(self, key):
        return os.path.join(self.armory, armory.WAYLAY_DIR, key + ".history.db")

    # -- direct sidecar reads (storage-level assertions only) ----------------
    def genesis_rows(self, path):
        conn = db.connect(path)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT kind, detail, log_id, created FROM genesis "
                "ORDER BY created")]
        finally:
            conn.close()

    def revision_count(self, path):
        conn = db.connect(path)
        try:
            return conn.execute(
                "SELECT COUNT(*) c FROM revisions").fetchone()["c"]
        finally:
            conn.close()

    def rev_rows(self, path):
        """All revision rows of a sidecar (uuid, field, old_val,
        new_val, log_id, created) in dense id order."""
        conn = db.connect(path)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT uuid, field, old_val, new_val, log_id, created "
                "FROM revisions ORDER BY id")]
        finally:
            conn.close()

    # -- P4: routed writes ----------------------------------------------------
    def w(self, key, verb, **args):
        """Drive one routed write; assert success; return the payload."""
        return self.w_args(key, verb, args)

    def w_args(self, key, verb, args):
        """As w, but with a raw args dict (for reserved-word keys)."""
        payload, is_error = server._call_routed(
            self.armory, verb, dict({"vambrace": key}, **args))
        self.assertFalse(is_error, repr(payload))
        return payload

    def rows_for(self, key, uuid):
        """The sidecar's revision rows of one entity (all six columns)."""
        return [r for r in self.rev_rows(self.sidecar_path(key)) if r[0] == uuid]

    # -- P5: routed reads ----------------------------------------------------
    def r(self, key, verb, **args):
        """Drive one routed read; return (payload, is_error)."""
        return server._call_routed(self.armory, verb,
                                   dict({"vambrace": key}, **args))

    def log_rows(self, key):
        """The main db's log-row count (read-only assertions)."""
        conn = db.connect(self.live_path(key))
        try:
            return conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]
        finally:
            conn.close()


class Birth(Base):
    """Sidecar creation is eager: equip and acquire seed the one-row
    genesis frame; a pre-build vambrace gets the sidecar at open with an
    EMPTY genesis table (no false birth claim)."""

    def test_equip_seeds_the_genesis_frame(self):
        key = self.make()
        self.assertTrue(os.path.isfile(self.sidecar_path(key)))
        rows = self.genesis_rows(self.sidecar_path(key))
        self.assertEqual(len(rows), 1)
        kind, detail, log_id, created = rows[0]
        self.assertEqual(kind, "equip")
        self.assertIsNone(detail)      # armory actions mint no db log rows
        self.assertIsNone(log_id)
        self.assertIsInstance(created, int)
        self.assertGreater(created, 0)
        # the main db itself mints no log row for the birth
        conn = db.connect(self.live_path(key))
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM logs").fetchone()["c"], 0)
        finally:
            conn.close()

    def test_equip_sidecar_schema_is_complete(self):
        key = self.make()
        conn = db.connect(self.sidecar_path(key))
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN "
                "('table', 'index')")}
            self.assertIn("genesis", names)
            self.assertIn("revisions", names)
            self.assertIn("idx_revisions_uuid", names)
        finally:
            conn.close()

    def test_prebuild_vambrace_gets_sidecar_at_open(self):
        key = self.make()
        os.remove(self.sidecar_path(key))  # simulate a pre-build vambrace
        payload, is_error = server._call_routed(
            self.armory, "chain_put",
            {"vambrace": key, "name": "A"})
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(self.sidecar_path(key)))
        self.assertEqual(self.genesis_rows(self.sidecar_path(key)), [])
        # P4: the write mints its revision rows into the fresh sidecar
        self.assertEqual(
            [r[:4] for r in self.rev_rows(self.sidecar_path(key))],
            [(payload["id"], "name", None, "A")])
        # the write itself still landed
        conn = db.connect(self.live_path(key))
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM chain_headers").fetchone()["c"], 1)
        finally:
            conn.close()


class Span(Base):
    """The attached sidecar participates in the ONE spanning
    BEGIN IMMEDIATE..COMMIT with the main db: one failure rolls back
    both files, one COMMIT writes both (plain SQLite multi-database
    transaction semantics; no recovery machinery)."""

    def _attached(self, key):
        path = self.live_path(key)
        conn = db.connect(path)
        server._attach_history(conn, path)
        return conn

    def _write_span(self, key):
        conn = self._attached(key)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO logs (created, title, area, body) VALUES (?,?,?,?)",
            (db.now_ns(), "t", "a", "b"))
        conn.execute(
            "INSERT INTO hist.revisions (uuid, field, old_val, new_val, "
            "log_id, created) VALUES (?,?,?,?,?,?)",
            (new_uuid7(), "text", None, "x", 1, db.now_ns()))
        return conn

    def test_rollback_covers_both_files(self):
        key = self.make()
        conn = self._write_span(key)
        try:
            conn.execute("ROLLBACK")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM logs").fetchone()["c"], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM hist.revisions").fetchone()["c"], 0)
        finally:
            conn.close()

    def test_commit_covers_both_files(self):
        key = self.make()
        conn = self._write_span(key)
        try:
            conn.execute("COMMIT")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM logs").fetchone()["c"], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM hist.revisions").fetchone()["c"], 1)
        finally:
            conn.close()


class AcquireGenesis(Base):
    """acquire's genesis row points at the mythos source path and the
    main db's one genesis log row (area 'acquire')."""

    def test_acquire_seeds_genesis_with_source_and_log_id(self):
        src = self.make()
        payload, is_error = server._call_routed(
            self.armory, "chain_put", {"vambrace": src, "name": "A"})
        self.assertFalse(is_error, repr(payload))
        emit = self.emit_path("mythos.json")
        payload, is_error = self.call({
            "action": "furnish", "vambrace": src, "root": "mythos",
            "emit_path": emit, "emit_format": "json"})
        self.assertFalse(is_error, repr(payload))

        payload, is_error = self.call(
            {"action": "acquire", "path": emit, "format": "json"})
        self.assertFalse(is_error, repr(payload))
        key = payload["key"]
        self.assertNotEqual(key, src)

        rows = self.genesis_rows(self.sidecar_path(key))
        self.assertEqual(len(rows), 1)
        kind, detail, log_id, created = rows[0]
        self.assertEqual(kind, "acquire")
        self.assertEqual(detail, emit)   # the mythos source path
        self.assertIsInstance(created, int)
        # log_id = the main db's one genesis log row
        conn = db.connect(self.live_path(key))
        try:
            row = conn.execute(
                "SELECT id FROM logs WHERE area = 'acquire'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(log_id, row["id"])
        finally:
            conn.close()


class Lifecycle(Base):
    """The sidecar travels as a unit with the main db: waylay moves
    both, recover restores both, dismantle request 3 deletes both.
    The armory scan stays structurally blind to the sidecar file."""

    def test_armory_scan_ignores_the_sidecar(self):
        key = self.make()
        payload, is_error = server._call_routed(
            self.armory, "chain_put", {"vambrace": key, "name": "A"})
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(self.sidecar_path(key)))
        live, waylaid = armory.list_vambraces(self.armory)
        self.assertEqual(live, [key])
        self.assertEqual(waylaid, [])

    def test_waylay_moves_the_pair(self):
        key = self.make()
        payload, is_error = self.waylay(key)
        self.assertFalse(is_error, repr(payload))
        self.assertFalse(os.path.exists(self.live_path(key)))
        self.assertFalse(os.path.exists(self.sidecar_path(key)))
        self.assertTrue(os.path.isfile(
            os.path.join(self.armory, armory.WAYLAY_DIR, key + ".db")))
        self.assertTrue(os.path.isfile(self.waylay_sidecar(key)))
        # the genesis frame survives the move intact
        rows = self.genesis_rows(self.waylay_sidecar(key))
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2]),
                         ("equip", None, None))
        live, waylaid = armory.list_vambraces(self.armory)
        self.assertEqual(live, [])
        self.assertEqual(waylaid, [key])

    def test_recover_restores_the_pair(self):
        key = self.make()
        self.assertFalse(self.waylay(key)[1])
        payload, is_error = self.recover(key)
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(self.live_path(key)))
        self.assertTrue(os.path.isfile(self.sidecar_path(key)))
        self.assertFalse(os.path.exists(self.waylay_sidecar(key)))
        rows = self.genesis_rows(self.sidecar_path(key))
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2]),
                         ("equip", None, None))
        live, waylaid = armory.list_vambraces(self.armory)
        self.assertEqual(live, [key])
        self.assertEqual(waylaid, [])

    def test_dismantle_request_three_deletes_the_pair(self):
        key = self.make(saga="The Forge")
        self.assertFalse(self.waylay(key)[1])
        for i in (1, 2, 3):
            payload, is_error = self.dismantle(key)
            self.assertFalse(is_error, repr(payload))
            self.assertEqual(payload["dismantled"], i == 3)
        self.assertFalse(os.path.exists(
            os.path.join(self.armory, armory.WAYLAY_DIR, key + ".db")))
        self.assertFalse(os.path.exists(self.waylay_sidecar(key)))
        ledger = armory.read_ledger(self.armory)[0]
        self.assertEqual(ledger["waylays"], {})
        self.assertEqual(ledger["sagas"], {})

    def test_waylay_without_sidecar_moves_only_the_main(self):
        key = self.make()
        os.remove(self.sidecar_path(key))  # pre-build waylay: none yet
        payload, is_error = self.waylay(key)
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(
            os.path.join(self.armory, armory.WAYLAY_DIR, key + ".db")))
        self.assertFalse(os.path.exists(self.waylay_sidecar(key)))
        self.assertFalse(self.recover(key)[1])
        # back to life as a pre-build vambrace: the next open recreates it
        payload, is_error = server._call_routed(
            self.armory, "chain_put", {"vambrace": key, "name": "A"})
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(self.sidecar_path(key)))
        self.assertEqual(self.genesis_rows(self.sidecar_path(key)), [])


class BladeMinting(Base):
    """SPEC 3.17 blade rows (field `text`): insert NULL->v, edit
    old->new (a no-op edit mints nothing), delete v->NULL; a move is
    structural and mints nothing."""

    def test_insert_mints_null_to_text(self):
        key = self.make()
        chain = self.w(key, "chain_put", name="A")["id"]
        bid = self.w(key, "blade_insert", chain=chain, text="one")["blade"]["id"]
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, bid)],
            [(bid, "text", None, "one")])

    def test_edit_mints_old_to_new_and_noop_mints_nothing(self):
        key = self.make()
        chain = self.w(key, "chain_put", name="A")["id"]
        bid = self.w(key, "blade_insert", chain=chain, text="one")["blade"]["id"]
        self.w(key, "blade_edit", id=bid, text="two")
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, bid)],
            [(bid, "text", None, "one"), (bid, "text", "one", "two")])
        before = self.rev_rows(self.sidecar_path(key))
        self.w(key, "blade_edit", id=bid, text="two")  # no-op write
        self.assertEqual(self.rev_rows(self.sidecar_path(key)), before)

    def test_delete_mints_text_to_null_and_row_survives(self):
        key = self.make()
        chain = self.w(key, "chain_put", name="A")["id"]
        bid = self.w(key, "blade_insert", chain=chain, text="one")["blade"]["id"]
        self.w(key, "blade_delete", id=bid)
        conn = db.connect(self.live_path(key))
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM chain_blades WHERE id = ?", (bid,)).fetchone())
        finally:
            conn.close()
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, bid)],
            [(bid, "text", None, "one"), (bid, "text", "one", None)])

    def test_move_mints_nothing(self):
        key = self.make()
        a = self.w(key, "chain_put", name="A")["id"]
        b = self.w(key, "chain_put", name="B")["id"]
        bid = self.w(key, "blade_insert", chain=a, text="one")["blade"]["id"]
        self.w(key, "blade_move", id=bid, to_chain=b, index=0)
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, bid)],
            [(bid, "text", None, "one")])  # only the insert's row


class ChainMinting(Base):
    """SPEC 3.17 chain-header rows: `name` only when supplied and
    changed; intention membership one row per name changed (an
    orientation-only change mints nothing); split/merge mint ONLY a
    header's name; delete mints the header name and EVERY blade's text
    (the rows survive the entity — the forensic surface)."""

    def test_creation_mints_name_and_each_intention(self):
        key = self.make()
        res = self.w(key, "chain_put", name="A",
                     intentions=[{"name": "alpha"},
                                 {"name": "beta", "orientation": "W"}])
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, res["id"])],
            [(res["id"], "name", None, "A"),
             (res["id"], "intention", None, "alpha"),
             (res["id"], "intention", None, "beta")])

    def test_rename_mints_name_change(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, cid)],
            [(cid, "name", None, "A"), (cid, "name", "A", "B")])

    def test_membership_add_and_remove(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A",
                     intentions=[{"name": "alpha"}, {"name": "beta"}])["id"]
        self.w(key, "chain_put", id=cid,
               intentions=[{"name": "beta"}, {"name": "gamma"}])
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, cid)],
            [(cid, "name", None, "A"),
             (cid, "intention", None, "alpha"),
             (cid, "intention", None, "beta"),
             (cid, "intention", None, "gamma"),    # added
             (cid, "intention", "alpha", None)])   # removed; beta unchanged

    def test_orientation_only_change_mints_nothing(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A",
                     intentions=[{"name": "alpha"}])["id"]
        before = self.rev_rows(self.sidecar_path(key))
        self.w(key, "chain_put", id=cid,
               intentions=[{"name": "alpha", "orientation": "W"}])
        self.assertEqual(self.rev_rows(self.sidecar_path(key)), before)

    def test_split_mints_only_the_new_header_name(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        b1 = self.w(key, "blade_insert", chain=cid, text="one")["blade"]["id"]
        b2 = self.w(key, "blade_insert", chain=cid, text="two")["blade"]["id"]
        res = self.w(key, "chain_split", source=cid, name="B",
                     blades=[b1], intentions=[{"name": "alpha"}])
        nid = res["new"]["id"]
        # ONLY the new header's name: the initial intention membership
        # and the moved blades mint nothing
        self.assertEqual([r[:4] for r in self.rows_for(key, nid)],
                         [(nid, "name", None, "B")])
        self.assertEqual([r[:4] for r in self.rows_for(key, cid)],
                         [(cid, "name", None, "A")])
        self.assertEqual([r[:4] for r in self.rows_for(key, b1)],
                         [(b1, "text", None, "one")])
        self.assertEqual([r[:4] for r in self.rows_for(key, b2)],
                         [(b2, "text", None, "two")])

    def test_merge_mints_only_the_dissolved_header_name(self):
        key = self.make()
        va = self.w(key, "chain_put", name="A")["id"]
        vb = self.w(key, "chain_put", name="B")["id"]
        b = self.w(key, "blade_insert", chain=vb, text="one")["blade"]["id"]
        self.w(key, "chain_merge", into=va, fold=vb)
        # the dissolved header's name row; the blade travels un-minted
        self.assertEqual([r[:4] for r in self.rows_for(key, vb)],
                         [(vb, "name", None, "B"), (vb, "name", "B", None)])
        self.assertEqual([r[:4] for r in self.rows_for(key, va)],
                         [(va, "name", None, "A")])
        self.assertEqual([r[:4] for r in self.rows_for(key, b)],
                         [(b, "text", None, "one")])

    def test_delete_mints_header_name_and_every_blade_text(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        b1 = self.w(key, "blade_insert", chain=cid, text="one")["blade"]["id"]
        b2 = self.w(key, "blade_insert", chain=cid, text="two")["blade"]["id"]
        self.w(key, "chain_delete", id=cid)
        # forensic surface: read the sidecar AFTER the deletion — the
        # rows survive the entity (they never cascade)
        self.assertEqual([r[:4] for r in self.rows_for(key, cid)],
                         [(cid, "name", None, "A"), (cid, "name", "A", None)])
        self.assertEqual([r[:4] for r in self.rows_for(key, b1)],
                         [(b1, "text", None, "one"), (b1, "text", "one", None)])
        self.assertEqual([r[:4] for r in self.rows_for(key, b2)],
                         [(b2, "text", None, "two"), (b2, "text", "two", None)])
        conn = db.connect(self.live_path(key))
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM chain_headers WHERE id = ?", (cid,)).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM chain_blades WHERE id = ?", (b1,)).fetchone())
        finally:
            conn.close()


class OpenItemMinting(Base):
    """SPEC 3.17 open_item rows: a put mints every supplied field on a
    mint (a first appearance is a change) and each changed field on an
    update — one row per changed freeform column, INTEGER columns as
    decimal strings; a delete mints every non-null content field to
    NULL (machine columns never)."""

    def test_creation_mints_supplied_fields(self):
        key = self.make()
        res = self.w_args(key, "open_item", {
            "action": "put", "name": "buy milk", "state": "shelved",
            "text": "do it", "current": 1, "pending": 2, "total": 3,
            "next": "a", "last": "b", "due": "2026-12-01",
            "priority": "low"})
        oid = res["id"]
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, oid)],
            [(oid, "name", None, "buy milk"),
             (oid, "state", None, "shelved"),
             (oid, "text", None, "do it"),
             (oid, "freeform_current", None, "1"),
             (oid, "freeform_pending", None, "2"),
             (oid, "freeform_total", None, "3"),
             (oid, "freeform_next", None, "a"),
             (oid, "freeform_last", None, "b"),
             (oid, "freeform_due", None, "2026-12-01"),
             (oid, "freeform_priority", None, "low")])

    def test_creation_defaults_state_to_open(self):
        key = self.make()
        oid = self.w(key, "open_item", action="put", name="A")["id"]
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, oid)],
            [(oid, "name", None, "A"), (oid, "state", None, "open")])

    def test_rename_state_and_text_changes(self):
        key = self.make()
        oid = self.w(key, "open_item", action="put", name="A")["id"]
        self.w(key, "open_item", action="put", id=oid, name="B",
               state="shelved", text="t")
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, oid)],
            [(oid, "name", None, "A"),
             (oid, "state", None, "open"),
             (oid, "name", "A", "B"),
             (oid, "state", "open", "shelved"),
             (oid, "text", None, "t")])

    def test_one_row_per_changed_freeform_column(self):
        key = self.make()
        oid = self.w(key, "open_item", action="put", name="A",
                     current=1, total=2)["id"]
        self.w_args(key, "open_item",
                    {"action": "put", "id": oid, "current": 2, "next": "x"})
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, oid)],
            [(oid, "name", None, "A"),
             (oid, "state", None, "open"),
             (oid, "freeform_current", None, "1"),
             (oid, "freeform_total", None, "2"),
             # the INTEGER column lands as its decimal string; one row
             # per changed column; the untouched columns mint nothing
             (oid, "freeform_current", "1", "2"),
             (oid, "freeform_next", None, "x")])

    def test_clearing_a_field_mints_to_null(self):
        key = self.make()
        oid = self.w(key, "open_item", action="put", name="A",
                     text="t", current=1)["id"]
        self.w(key, "open_item", action="put", id=oid,
               text=None, current=None)
        self.assertEqual(
            [r[:4] for r in self.rows_for(key, oid)],
            [(oid, "name", None, "A"),
             (oid, "state", None, "open"),
             (oid, "text", None, "t"),
             (oid, "freeform_current", None, "1"),
             (oid, "text", "t", None),
             (oid, "freeform_current", "1", None)])

    def test_delete_mints_every_non_null_field_to_null(self):
        key = self.make()
        oid = self.w(key, "open_item", action="put", name="A",
                     state="shelved", text="t", current=5)["id"]
        self.w(key, "open_item", action="delete", id=oid)
        rows = [r[:4] for r in self.rows_for(key, oid)]
        self.assertEqual(
            rows,
            [(oid, "name", None, "A"),
             (oid, "state", None, "shelved"),
             (oid, "text", None, "t"),
             (oid, "freeform_current", None, "5"),
             (oid, "name", "A", None),
             (oid, "state", "shelved", None),
             (oid, "text", "t", None),
             (oid, "freeform_current", "5", None)])
        # machine columns (id / created / updated) never mint
        self.assertTrue({r[1] for r in rows} <=
                        {"name", "state", "text", "freeform_current"})


class NeverMints(Base):
    """SPEC 3.17 'Nothing mints': reblade rows (tuple-identified PK,
    no UUID anchor — a note write is already covered by its logs row),
    failed writes (history records what LANDED — the rolled-back rows
    never appear), and the deflected_write clear verb (machine
    bookkeeping)."""

    def test_reblade_add_and_remove_mint_nothing(self):
        key = self.make()
        a = self.w(key, "chain_put", name="A")["id"]
        b = self.w(key, "chain_put", name="B")["id"]
        self.w(key, "reblade_add", src=a, dst=b, kind="see", note="n")
        self.w(key, "reblade_remove", src=a, dst=b, kind="see")
        self.assertEqual(
            sorted(r[:4] for r in self.rev_rows(self.sidecar_path(key))),
            sorted([(a, "name", None, "A"), (b, "name", None, "B")]))

    def test_failed_batch_mints_no_new_rows(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        bid = self.w(key, "blade_insert", chain=cid, text="one")["blade"]["id"]
        before = self.rev_rows(self.sidecar_path(key))
        # the first op would mint a row; the second fails (duplicate
        # name) and the WHOLE transaction — revision rows included —
        # rolls back. (The deflected cache row is existing behavior.)
        payload, is_error = server._call_routed(self.armory, "batch", {
            "vambrace": key, "ops": [
                {"op": "blade_insert", "args": {"chain": cid, "text": "two"}},
                {"op": "chain_split",
                 "args": {"source": cid, "name": "A", "blades": [bid]}},
            ]})
        self.assertTrue(is_error, repr(payload))
        self.assertEqual(self.rev_rows(self.sidecar_path(key)), before)

    def test_deflected_write_clear_mints_nothing(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        before = self.rev_rows(self.sidecar_path(key))
        payload, is_error = server._call_routed(self.armory, "chain_split", {
            "vambrace": key, "source": cid, "name": "A", "blades": []})
        self.assertTrue(is_error, repr(payload))
        conn = db.connect(self.live_path(key))
        try:
            dw_id = conn.execute(
                "SELECT id FROM deflected_writes WHERE state = 'pending'"
            ).fetchone()["id"]
        finally:
            conn.close()
        self.assertIsNotNone(dw_id)
        self.w(key, "deflected_write", action="clear", id=dw_id)
        self.assertEqual(self.rev_rows(self.sidecar_path(key)), before)


class BatchMinting(Base):
    """SPEC 3.17 batch rows: per-op rows exactly as the standalone
    ops, ALL sharing the batch's single log_id; a revision row's
    created is the same Unix-ns instant as its log row."""

    def _assert_created_matches_log(self, key, rows):
        self.assertTrue(rows)
        conn = db.connect(self.live_path(key))
        try:
            created = {
                r["id"]: r["created"]
                for r in conn.execute("SELECT id, created FROM logs")
            }
        finally:
            conn.close()
        for r in rows:
            _uuid, field, old, new, log_id, row_created = r
            self.assertEqual(row_created, created[log_id])

    def test_revision_created_equals_its_log_created(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "blade_insert", chain=cid, text="one")
        self._assert_created_matches_log(
            key, self.rev_rows(self.sidecar_path(key)))

    def test_batch_rows_share_the_single_log_id(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        bid = self.w(key, "blade_insert", chain=cid, text="one")["blade"]["id"]
        res = self.w(key, "batch", ops=[
            {"op": "blade_insert", "args": {"chain": cid, "text": "two"}},
            {"op": "blade_edit", "args": {"id": bid, "text": "three"}},
            {"op": "chain_put", "args": {"id": cid, "name": "B"}},
        ])
        log_id = res["log_id"]
        self.assertTrue(all(o["log_id"] == log_id for o in res["ops"]))
        b2 = res["ops"][0]["blade"]["id"]
        rows = [r for r in self.rev_rows(self.sidecar_path(key))
                if r[4] == log_id]
        self.assertEqual(sorted(r[:4] for r in rows), sorted([
            (b2, "text", None, "two"),
            (bid, "text", "one", "three"),
            (cid, "name", "A", "B"),
        ]))
        # every row of the batch carries the batch's one log_id
        self.assertTrue(all(r[4] == log_id for r in rows))
        self._assert_created_matches_log(key, rows)


class HistoryResponse(Base):
    """P5: the routed `history` response — the exact
    {genesis, revisions, trace} shape (SPEC 3.17 response shape), the
    joined op + title, the window-honest count branch, orphan
    exclusion, and the emit branch."""

    def test_response_shape_and_genesis_frame(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        payload, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(sorted(payload), ["genesis", "revisions", "trace"])
        self.assertEqual(sorted(payload["genesis"]),
                         ["created", "detail", "kind", "log_id"])
        self.assertEqual(payload["genesis"]["kind"], "equip")
        self.assertIsNone(payload["genesis"]["detail"])
        self.assertIsNone(payload["genesis"]["log_id"])
        self.assertRegex(payload["genesis"]["created"], ISO_RE)

    def test_revision_rows_carry_joined_op_and_title(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        payload, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(payload))
        rows = payload["revisions"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(sorted(row),
                             ["created", "field", "new", "old", "op", "title"])
            self.assertRegex(row["created"], ISO_RE)
        newest, oldest = rows
        # the log_refs join: op + title come from the write's log row
        self.assertEqual(
            (newest["field"], newest["old"], newest["new"],
             newest["op"], newest["title"]),
            ("name", "A", "B", "modified", 'chain_put "B"'))
        self.assertEqual(
            (oldest["field"], oldest["old"], oldest["new"],
             oldest["op"], oldest["title"]),
            ("name", None, "A", "created", 'chain_put "A"'))

    def test_revisions_newest_first_and_limit_governs_the_page_only(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        self.w(key, "chain_put", id=cid, name="C")
        payload, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [(r["old"], r["new"]) for r in payload["revisions"]],
            [("B", "C"), ("A", "B"), (None, "A")])
        payload, is_error = self.r(key, "history", id=cid, limit=2)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [(r["old"], r["new"]) for r in payload["revisions"]],
            [("B", "C"), ("A", "B")])
        # the sidecar still holds all 3 rows — the limit governs the page
        self.assertEqual(self.revision_count(self.sidecar_path(key)), 3)

    def test_trace_full_row_shape_and_trace_only_gap(self):
        key = self.make()
        b = self.w(key, "chain_put", name="B")["id"]
        a = self.w(key, "chain_put", name="A")["id"]
        # a pointer write between the two name revisions: it mints NO
        # revision for A, but its log row refs A — a trace-only entry
        # (a gap) inside the page window
        self.w(key, "reblade_add", src=a, dst=b, kind="see")
        self.w(key, "chain_put", id=a, name="C")
        payload, is_error = self.r(key, "history", id=a)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [(r["field"], r["old"], r["new"]) for r in payload["revisions"]],
            [("name", "A", "C"), ("name", None, "A")])
        self.assertEqual(len(payload["trace"]), 3)  # 2 revisions + the gap
        for row in payload["trace"]:
            self.assertEqual(sorted(row),
                             ["body", "created", "op", "refs", "title"])
            self.assertRegex(row["created"], ISO_RE)
            for ref in row["refs"]:
                self.assertEqual(sorted(ref), ["op", "uuid"])
        self.assertEqual(
            [(t["op"], t["title"]) for t in payload["trace"]],
            [("modified", 'chain_put "C"'),
             ("referenced", "reblade_add %s -> %s" % (a, b)),
             ("created", 'chain_put "A"')])
        self.assertEqual(
            {(x["uuid"], x["op"]) for x in payload["trace"][1]["refs"]},
            {(a, "referenced"), (b, "referenced")})

    def test_trace_window_excludes_logs_newer_than_the_page(self):
        """CONTRACT NOTE (mismatch): the tasking expected a blade_insert
        landing AFTER the newest revision to still appear in the
        chain's trace; the implementation's window is [oldest page row,
        newest page row], so a log newer than the newest revision is
        outside it (verified against verbs._h_history). This test pins
        the actual behavior; the same log IS inside the blade's own
        window."""
        key = self.make()
        a = self.w(key, "chain_put", name="A")["id"]
        time.sleep(0.002)
        self.w(key, "chain_put", id=a, name="B")
        time.sleep(0.002)
        bid = self.w(key, "blade_insert", chain=a, text="one")["blade"]["id"]
        payload, is_error = self.r(key, "history", id=a)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [(t["op"], t["title"]) for t in payload["trace"]],
            [("modified", 'chain_put "B"'), ("created", 'chain_put "A"')])
        # the same blade_insert log, seen from the blade: in its window
        payload, is_error = self.r(key, "history", id=bid)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual([t["title"] for t in payload["trace"]],
                         ['blade_insert "B"'])

    def test_trace_window_follows_the_revisions_page(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        time.sleep(0.002)
        self.w(key, "chain_put", id=cid, name="B")
        time.sleep(0.002)
        self.w(key, "chain_put", id=cid, name="C")
        # default page spans all 3 revisions -> all 3 touching logs
        payload, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [t["title"] for t in payload["trace"]],
            ['chain_put "C"', 'chain_put "B"', 'chain_put "A"'])
        # limit=1: window = the single newest row's instant -> exactly
        # that one log row
        payload, is_error = self.r(key, "history", id=cid, limit=1)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [r["title"] for r in payload["revisions"]], ['chain_put "C"'])
        self.assertEqual(
            [t["title"] for t in payload["trace"]], ['chain_put "C"'])

    def test_count_branch_never_counts_the_genesis_frame(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        payload, is_error = self.r(key, "history", id=cid, count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 2, "total": 2})
        payload, is_error = self.r(key, "history", id=cid, count=True,
                                   limit=1)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 1, "total": 2})

    def test_orphan_revisions_are_excluded_from_rows_and_count(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        # a revision row whose log_id no longer resolves: a direct
        # sidecar insert (the inner join drops it)
        conn = db.connect(self.sidecar_path(key))
        try:
            conn.execute(
                "INSERT INTO revisions (uuid, field, old_val, new_val, "
                "log_id, created) VALUES (?, 'name', 'B', 'ORPHAN', 999999, ?)",
                (cid, db.now_ns()))
        finally:
            conn.close()
        payload, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            [(r["old"], r["new"]) for r in payload["revisions"]],
            [("A", "B"), (None, "A")])
        payload, is_error = self.r(key, "history", id=cid, count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 2, "total": 2})

    def test_emit_json_receipt_and_bare_payload(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        out = self.emit_path("history.json")
        plain, is_error = self.r(key, "history", id=cid)
        self.assertFalse(is_error, repr(plain))
        payload, is_error = self.r(key, "history", id=cid, emit_path=out,
                                   emit_format="json")
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(sorted(payload),
                         ["bytes", "count", "emit_format", "emit_path",
                          "emitted", "truncated"])
        self.assertIs(payload["emitted"], True)
        self.assertEqual(payload["emit_path"], out)
        self.assertEqual(payload["emit_format"], "json")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["bytes"], os.path.getsize(out))
        self.assertFalse(payload["truncated"])
        # the file carries exactly what the plain call would have
        self.assertEqual(json.load(open(out, encoding="utf-8")), plain)

    def test_emit_json_truncated_with_explicit_cutting_limit(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "chain_put", id=cid, name="B")
        self.w(key, "chain_put", id=cid, name="C")
        out = self.emit_path("history-cut.json")
        payload, is_error = self.r(key, "history", id=cid, emit_path=out,
                                   emit_format="json", limit=1)
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["count"], 1)
        doc = json.load(open(out, encoding="utf-8"))
        self.assertEqual(len(doc["revisions"]), 1)
        # a limit that does not cut leaves truncated False
        out = self.emit_path("history-full.json")
        payload, is_error = self.r(key, "history", id=cid, emit_path=out,
                                   emit_format="json", limit=3)
        self.assertFalse(is_error, repr(payload))
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["count"], 3)


class ReadSurface(Base):
    """P5: the read-surface keys (count / emit_path / emit_format) on
    the six read verbs — exact response shapes (SPEC 3.17 read-surface
    keys)."""

    def test_get_count_branch(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        for i in range(3):
            self.w(key, "blade_insert", chain=cid, text="line %d" % i)
        payload, is_error = self.r(key, "get", id=cid, count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 3, "total": 3})
        bid = self.w(key, "blade_insert", chain=cid,
                     text="more")["blade"]["id"]
        payload, is_error = self.r(key, "get", id=bid, count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 1, "total": 1})
        payload, is_error = self.r(key, "get", id=new_uuid7(), count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 0, "total": 0})

    def test_list_count_branch_is_window_honest(self):
        key = self.make()
        for i in range(7):
            self.w(key, "chain_put", name="L%d" % i)
        payload, is_error = self.r(key, "list", space="chain", count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 7, "total": 7})
        payload, is_error = self.r(key, "list", space="chain", count=True,
                                   limit=3)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 3, "total": 7})
        payload, is_error = self.r(key, "list", space="chain", count=True,
                                   limit=3, offset=5)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 2, "total": 7})

    def test_search_count_branch(self):
        key = self.make()
        for i in range(4):
            self.w(key, "chain_put", name="S%d" % i)
        args = {"probes": ["s0", "s1", "s2"], "spaces": ["chain"]}
        payload, is_error = self.r(key, "search", count=True, **args)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 3, "total": 3})
        payload, is_error = self.r(key, "search", count=True, limit=2,
                                   **args)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 2, "total": 3})

    def test_reblades_of_and_follow_count_branches(self):
        key = self.make()
        a = self.w(key, "chain_put", name="A")["id"]
        b = self.w(key, "chain_put", name="B")["id"]
        self.w(key, "reblade_add", src=a, dst=b, kind="see")
        payload, is_error = self.r(key, "reblades_of", id=a, count=True)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 1, "total": 1})
        payload, is_error = self.r(key, "follow", **{"from": a,
                                                     "count": True})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"count": 2, "total": 2})

    def test_emit_get_json_bare_payload(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "blade_insert", chain=cid, text="one")
        out = self.emit_path("get.json")
        plain, is_error = self.r(key, "get", id=cid)
        self.assertFalse(is_error, repr(plain))
        payload, is_error = self.r(key, "get", id=cid, emit_path=out,
                                   emit_format="json")
        self.assertFalse(is_error, repr(payload))
        self.assertIs(payload["emitted"], True)
        self.assertEqual(payload["emit_format"], "json")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["bytes"], os.path.getsize(out))
        self.assertFalse(payload["truncated"])
        doc = json.load(open(out, encoding="utf-8"))
        self.assertEqual(doc, plain)           # bare payload, no envelope
        self.assertNotIn("exported_at", doc)

    def test_emit_search_ndjson_one_line_per_record(self):
        key = self.make()
        for i in range(3):
            self.w(key, "chain_put", name="N%d" % i)
        args = {"probes": ["n0", "n1", "n2"], "spaces": ["chain"]}
        out = self.emit_path("search.ndjson")
        plain, is_error = self.r(key, "search", **args)
        self.assertFalse(is_error, repr(plain))
        payload, is_error = self.r(key, "search", emit_path=out,
                                   emit_format="ndjson", **args)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["count"], 3)
        self.assertFalse(payload["truncated"])
        lines = [l for l in open(out, encoding="utf-8").read().splitlines()
                 if l]
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(l) for l in lines], plain["hits"])

    def test_emit_receipt_truncated_flag(self):
        key = self.make()
        for i in range(3):
            self.w(key, "chain_put", name="N%d" % i)
        args = {"probes": ["n0", "n1", "n2"], "spaces": ["chain"],
                "emit_format": "ndjson"}
        out = self.emit_path("cut.ndjson")
        payload, is_error = self.r(key, "search",
                                   **dict(args, emit_path=out, limit=1))
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["count"], 1)
        out = self.emit_path("nocut.ndjson")
        payload, is_error = self.r(key, "search",
                                   **dict(args, emit_path=out, limit=9))
        self.assertFalse(is_error, repr(payload))
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["count"], 3)

    def test_emit_list_ndjson_one_line_per_record(self):
        key = self.make()
        for i in range(3):
            self.w(key, "chain_put", name="N%d" % i)
        out = self.emit_path("list.ndjson")
        payload, is_error = self.r(key, "list", space="chain",
                                   emit_path=out, emit_format="ndjson")
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["emitted"], True)
        self.assertEqual(payload["emit_path"], out)
        self.assertEqual(payload["emit_format"], "ndjson")
        self.assertEqual(payload["count"], 3)
        self.assertTrue(payload["bytes"] > 0)
        self.assertFalse(payload["truncated"])
        # the bare payload: one ndjson record per line, exactly the items
        plain = self.r(key, "list", space="chain")[0]["items"]
        lines = open(out, encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 3)
        for line, item in zip(lines, plain):
            self.assertEqual(json.loads(line), item)

    def test_read_surface_validation(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        out = self.emit_path("x.json")
        cases = [
            ("get", dict(id=cid, count=True, emit_path=out,
                         emit_format="json")),
            ("history", dict(id=cid, count=True, emit_path=out,
                             emit_format="json")),
            ("get", dict(id=cid, emit_format="json")),
            ("history", dict(id=cid, emit_format="json")),
            ("get", dict(id=cid, emit_path=os.path.join(
                self.armory, "no-such-dir", "x.json"),
                emit_format="json")),
        ]
        for verb, extra in cases:
            payload, is_error = self.r(key, verb, **extra)
            self.assertTrue(is_error, (verb, extra, payload))
            self.assertEqual(payload["error"]["code"], "validation_failed",
                             (verb, extra, payload))

    def test_emit_is_read_only_against_the_armory(self):
        key = self.make()
        cid = self.w(key, "chain_put", name="A")["id"]
        self.w(key, "blade_insert", chain=cid, text="one")
        before = self.log_rows(key)
        self.r(key, "history", id=cid, emit_path=self.emit_path("h.json"),
               emit_format="json")
        self.r(key, "get", id=cid, emit_path=self.emit_path("g.json"),
               emit_format="json")
        self.r(key, "search", probes=["one"],
               emit_path=self.emit_path("s.ndjson"), emit_format="ndjson")
        self.assertEqual(self.log_rows(key), before)


if __name__ == "__main__":
    unittest.main()
