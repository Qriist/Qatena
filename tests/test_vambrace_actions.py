"""Phase 1 of the vambrace lifecycle (draft scratch/vambrace_actions_draft.md,
section 8, phase-1 rows). The in-db meta table is the saga source of truth;
the armory.json ledger is a display cache (sagas) + non-derivable waylays
bookkeeping (phase 3). Covers: ledger write/read/atomic + rebuild +
fail-closed display; saga set/clear/no-change/self-exclusion/
conflict-names-offender/waylaid-target/ledger-warning; inventory both
populations/strays/counts/rebuild flag; equip saga conflict pre-mint with no
file litter + response shape. Phase 3 rows: waylay (snapshot + verify +
move, all rejections with zero side effects, suspended-register info,
occupied target, ledger-warning), recover (move back, count reset,
occupied location, register note), dismantle (1-2-3 state machine,
escalating warnings, live-key 'waylay first', fail-closed counts,
ledger cleanup at destruction), and the inventory across every transition.
Unit level: server._call_vambrace + qatena.armory against a temp armory dir;
the stdio schema pin lives in tests/test_server.py.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from qatena import armory, db, schema, server, snapshot
from qatena.uuid7 import new_uuid7
from helpers import ISO_RE


class Base(unittest.TestCase):
    def setUp(self):
        self.armory = tempfile.mkdtemp(prefix="qatena-actions-")

    def tearDown(self):
        shutil.rmtree(self.armory, ignore_errors=True)

    def call(self, arguments):
        return server._call_vambrace(self.armory, arguments)

    def equip(self, **extra):
        payload, is_error = self.call(dict({"action": "equip"}, **extra))
        self.assertFalse(is_error, "equip failed: %r" % payload)
        return payload

    def make(self, **extra):
        return self.equip(**extra)["vambrace"]

    def live_path(self, key):
        return os.path.join(self.armory, key + ".db")

    def waylay_path(self, key):
        return os.path.join(self.armory, armory.WAYLAY_DIR, key + ".db")

    def _dbpath(self, key):
        path = self.live_path(key)
        return path if os.path.exists(path) else self.waylay_path(key)

    def waylaid(self, key):
        """Phase 3 moves the file via the waylay action; phase 1 tests need
        the file-location state directly."""
        os.makedirs(os.path.join(self.armory, armory.WAYLAY_DIR), exist_ok=True)
        os.replace(self.live_path(key), self.waylay_path(key))

    def db_files(self):
        return sorted(n for n in os.listdir(self.armory) if n.endswith(".db"))

    def read_ledger(self):
        path = os.path.join(self.armory, armory.LEDGER_NAME)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def corrupt_ledger(self, content="{not json"):
        with open(os.path.join(self.armory, armory.LEDGER_NAME), "w",
                  encoding="utf-8") as f:
            f.write(content)

    def db_saga(self, key):
        conn = db.connect(self._dbpath(key))
        try:
            row = conn.execute(
                "SELECT v FROM meta WHERE k = ?", (armory.SAGA_KEY,)).fetchone()
        finally:
            conn.close()
        return row["v"] if row is not None else None

    def db_log_count(self, key):
        conn = db.connect(self._dbpath(key))
        try:
            return conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]
        finally:
            conn.close()

    def emit_path(self, name="snap.json"):
        return os.path.join(self.armory, name)

    def plant_register(self, key):
        """One autoexport register row (no write verb covers it): the
        waylay/recover suspended-register paths."""
        conn = db.connect(self._dbpath(key))
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO autoexport_registers "
                "(root_uuid, emit_path, emit_format, created) "
                "VALUES (?, ?, ?, ?)",
                (key, os.path.join(self.armory, "x.md"), "md", db.now_ns()))
            conn.execute("COMMIT")
        finally:
            conn.close()


class Ledger(Base):
    def test_read_missing_is_empty(self):
        ledger, warning = armory.read_ledger(self.armory)
        self.assertEqual(ledger, {"sagas": {}, "waylays": {}})
        self.assertIsInstance(warning, str)  # surfaced only when it matters

    def test_write_then_read_round_trip(self):
        ledger = {"sagas": {"b-key": "Beta", "a-key": "Alpha"},
                  "waylays": {"c-key": {"waylaid_at": "2026-01-01T00:00:00.000Z",
                                        "dismantle_requests": []}}}
        self.assertIsNone(armory.write_ledger(self.armory, ledger))
        self.assertEqual(armory.read_ledger(self.armory), (ledger, None))
        with open(os.path.join(self.armory, armory.LEDGER_NAME),
                  "r", encoding="utf-8") as f:
            self.assertEqual(f.read(),
                             json.dumps(ledger, sort_keys=True, indent=2) + "\n")

    def test_write_is_atomic_with_no_tmp_litter(self):
        armory.write_ledger(self.armory, {"sagas": {}, "waylays": {}})
        armory.write_ledger(self.armory, {"sagas": {"k": "s"}, "waylays": {}})
        self.assertEqual([n for n in os.listdir(self.armory) if ".tmp-" in n], [])
        self.assertEqual(self.read_ledger(), {"sagas": {"k": "s"}, "waylays": {}})

    def test_write_failure_returns_warning_never_raises(self):
        blocker = os.path.join(self.armory, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        warning = armory.write_ledger(blocker, {"sagas": {}, "waylays": {}})
        self.assertIsInstance(warning, str)
        self.assertIn("write failed", warning)
        self.assertFalse(os.path.exists(os.path.join(blocker, armory.LEDGER_NAME)))

    def test_read_corrupt_is_empty_with_warning(self):
        self.corrupt_ledger()
        ledger, warning = armory.read_ledger(self.armory)
        self.assertEqual(ledger, {"sagas": {}, "waylays": {}})
        self.assertIsInstance(warning, str)

    def test_read_wrong_shape_is_empty_with_warning(self):
        self.corrupt_ledger('{"sagas": "nope"}')
        ledger, warning = armory.read_ledger(self.armory)
        self.assertEqual(ledger, {"sagas": {}, "waylays": {}})
        self.assertIsInstance(warning, str)


class Saga(Base):
    def test_set_persists_to_db_and_mirrors_to_ledger(self):
        key = self.make()
        payload, is_error = self.call({"action": "saga", "vambrace": key,
                                       "label": "The Forge"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"key": key, "saga": "The Forge"})
        self.assertEqual(self.db_saga(key), "The Forge")
        self.assertEqual(self.read_ledger(),
                         {"sagas": {key: "The Forge"}, "waylays": {}})
        self.assertEqual(self.db_log_count(key), 0)  # no log row minted

    def test_clear_deletes_row_and_ledger_entry(self):
        key = self.make(saga="The Forge")
        payload, is_error = self.call({"action": "saga", "vambrace": key,
                                       "label": ""})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"key": key, "saga": None})
        self.assertIsNone(self.db_saga(key))
        self.assertEqual(self.read_ledger(), {"sagas": {}, "waylays": {}})

    def test_omitted_label_is_no_change(self):
        key = self.make(saga="The Forge")
        payload, is_error = self.call({"action": "saga", "vambrace": key})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"key": key, "saga": "The Forge"})
        self.assertEqual(self.db_saga(key), "The Forge")
        bare = self.make()
        payload, is_error = self.call({"action": "saga", "vambrace": bare})
        self.assertFalse(is_error)
        self.assertEqual(payload, {"key": bare, "saga": None})

    def test_self_is_excluded_from_the_conflict_check(self):
        key = self.make(saga="The Forge")
        payload, is_error = self.call({"action": "saga", "vambrace": key,
                                       "label": "the FORGE"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"key": key, "saga": "the FORGE"})

    def test_conflict_names_the_offender(self):
        offender = self.make(saga="The Forge")
        victim = self.make(saga="Other Story")
        payload, is_error = self.call({"action": "saga", "vambrace": victim,
                                       "label": "the forge"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "duplicate_name")
        self.assertIn(offender, payload["error"]["message"])
        self.assertEqual(self.db_saga(victim), "Other Story")  # untouched

    def test_conflict_ignores_ledger_and_reads_the_dbs(self):
        # the db is the source of truth: the ledger is gone, the conflict
        # still resolves from the meta rows
        offender = self.make(saga="The Forge")
        os.remove(os.path.join(self.armory, armory.LEDGER_NAME))
        victim = self.make(saga="Other")
        payload, is_error = self.call({"action": "saga", "vambrace": victim,
                                       "label": "THE FORGE"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "duplicate_name")
        self.assertIn(offender, payload["error"]["message"])

    def test_conflict_includes_waylaid_vambraces(self):
        offender = self.make(saga="The Forge")
        self.waylaid(offender)
        victim = self.make(saga="Other")
        payload, is_error = self.call({"action": "saga", "vambrace": victim,
                                       "label": "the FORGE"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "duplicate_name")
        self.assertIn(offender, payload["error"]["message"])

    def test_works_on_a_waylaid_key(self):
        key = self.make(saga="Retired Story")
        self.waylaid(key)
        payload, is_error = self.call({"action": "saga", "vambrace": key,
                                       "label": "Reinstated"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"key": key, "saga": "Reinstated"})
        self.assertEqual(self.db_saga(key), "Reinstated")
        self.assertEqual(self.read_ledger()["sagas"], {key: "Reinstated"})

    def test_unknown_key_is_not_found(self):
        payload, is_error = self.call(
            {"action": "saga", "vambrace": new_uuid7()})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_fail_closed_routing_validation(self):
        for args, code in (
                ({}, "missing_vambrace"),
                ({"vambrace": ""}, "missing_vambrace"),
                ({"vambrace": 42}, "missing_vambrace"),
                ({"vambrace": "not-a-uuid"}, "malformed_vambrace")):
            payload, is_error = self.call(dict({"action": "saga"}, **args))
            self.assertTrue(is_error, repr(args))
            self.assertEqual(payload["error"]["code"], code, repr(args))

    def test_non_string_label_is_validation_failed(self):
        key = self.make()
        payload, is_error = self.call(
            {"action": "saga", "vambrace": key, "label": 42})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIsNone(self.db_saga(key))

    def test_failed_ledger_mirror_warns_but_does_not_fail(self):
        key = self.make()
        with mock.patch.object(
                armory, "write_ledger",
                return_value="armory ledger armory.json write failed (simulated); the in-db saga is authoritative, the ledger may be stale") as w:
            payload, is_error = self.call(
                {"action": "saga", "vambrace": key, "label": "The Forge"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["key"], key)
        self.assertEqual(payload["saga"], "The Forge")
        self.assertIn("ledger_warning", payload)
        self.assertEqual(self.db_saga(key), "The Forge")  # the db is truth
        self.assertFalse(self.read_ledger())              # the mirror never landed
        w.assert_called_once()

    def test_corrupt_ledger_mirror_warns_and_rebuilds(self):
        key = self.make(saga="Old Name")
        self.corrupt_ledger()
        payload, is_error = self.call({"action": "saga", "vambrace": key,
                                       "label": "New Name"})
        self.assertFalse(is_error, repr(payload))
        self.assertIn("ledger_warning", payload)
        self.assertEqual(self.read_ledger(),
                         {"sagas": {key: "New Name"}, "waylays": {}})


class Inventory(Base):
    def test_empty_armory(self):
        payload, is_error = self.call({"action": "inventory"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {"live": [], "waylaid": []})

    def test_both_populations_strays_and_shape(self):
        a = self.make(saga="Alpha")
        b = self.make()                       # no saga
        w = self.make(saga="Waylaid")
        self.waylaid(w)
        ledger = self.read_ledger()
        ledger["waylays"][w] = {
            "waylaid_at": "2026-01-02T03:04:05.000Z",
            "dismantle_requests": ["2026-01-03T00:00:00.000Z",
                                   "2026-01-04T00:00:00.000Z"]}
        armory.write_ledger(self.armory, ledger)
        # strays: the ledger itself, other files, non-UUID dbs, a WAL sidecar
        with open(os.path.join(self.armory, "notes.txt"), "w",
                  encoding="utf-8") as f:
            f.write("not a vambrace")
        with open(os.path.join(self.armory, "not-a-key.db"), "w",
                  encoding="utf-8") as f:
            f.write("x")
        with open(self.live_path(a) + "-wal", "w", encoding="utf-8") as f:
            f.write("wal")
        payload, is_error = self.call({"action": "inventory"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(set(payload), {"live", "waylaid"})
        self.assertEqual([e["key"] for e in payload["live"]], sorted([a, b]))
        by_key = {e["key"]: e for e in payload["live"]}
        self.assertEqual(list(by_key[a]), ["key", "saga", "size_bytes",
                                           "modified"])
        self.assertEqual(by_key[a]["saga"], "Alpha")
        self.assertIsNone(by_key[b]["saga"])            # null when unknown
        for e in payload["live"]:
            self.assertGreaterEqual(e["size_bytes"], 0)
            self.assertRegex(e["modified"], ISO_RE)
        self.assertEqual([e["key"] for e in payload["waylaid"]], [w])
        e = payload["waylaid"][0]
        self.assertEqual(list(e), ["key", "saga", "waylaid_at", "size_bytes",
                                   "modified", "dismantle_requests",
                                   "dismantles_until_destroy"])
        self.assertEqual(e["saga"], "Waylaid")
        self.assertEqual(e["waylaid_at"], "2026-01-02T03:04:05.000Z")
        self.assertEqual(e["dismantle_requests"], 2)
        self.assertEqual(e["dismantles_until_destroy"], 1)
        self.assertRegex(e["modified"], ISO_RE)

    def test_missing_ledger_rebuilds_sagas_from_dbs(self):
        a = self.make(saga="Alpha")
        b = self.make()
        os.remove(os.path.join(self.armory, armory.LEDGER_NAME))
        payload, is_error = self.call({"action": "inventory"})
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(payload["ledger_rebuilt"])
        self.assertEqual({e["key"]: e["saga"] for e in payload["live"]},
                         {a: "Alpha", b: None})
        self.assertEqual(self.read_ledger(),
                         {"sagas": {a: "Alpha"}, "waylays": {}})

    def test_corrupt_ledger_rebuilds_and_zeroes_counts(self):
        w = self.make(saga="Waylaid")
        self.waylaid(w)
        armory.write_ledger(self.armory, {
            "sagas": {w: "Waylaid"},
            "waylays": {w: {"waylaid_at": "2026-01-02T03:04:05.000Z",
                            "dismantle_requests": ["t1", "t2"]}}})
        payload, _ = self.call({"action": "inventory"})
        self.assertEqual(payload["waylaid"][0]["dismantle_requests"], 2)
        self.assertEqual(payload["waylaid"][0]["dismantles_until_destroy"], 1)
        # fail-closed display: a corrupt ledger zeroes the counts (uncertainty
        # never destroys -- it only delays) and rebuilds the sagas from the dbs
        self.corrupt_ledger()
        payload, is_error = self.call({"action": "inventory"})
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(payload["ledger_rebuilt"])
        e = payload["waylaid"][0]
        self.assertIsNone(e["waylaid_at"])
        self.assertEqual(e["dismantle_requests"], 0)
        self.assertEqual(e["dismantles_until_destroy"], 3)
        self.assertEqual(e["saga"], "Waylaid")         # restored from the db


class EquipSaga(Base):
    def test_response_shape_with_saga(self):
        payload = self.equip(saga="The Forge")
        self.assertEqual(list(payload), ["vambrace", "saga"])
        key = payload["vambrace"]
        self.assertEqual(payload["saga"], "The Forge")
        self.assertEqual(self.db_saga(key), "The Forge")
        self.assertEqual(self.read_ledger(),
                         {"sagas": {key: "The Forge"}, "waylays": {}})

    def test_omitted_saga_keeps_the_exact_shape(self):
        payload = self.equip()
        self.assertEqual(list(payload), ["vambrace"])
        self.assertIsNone(self.db_saga(payload["vambrace"]))
        self.assertIsNone(self.read_ledger())          # no mirror, no file

    def test_empty_saga_is_no_saga(self):
        payload = self.equip(saga="")
        self.assertEqual(list(payload), ["vambrace"])
        self.assertIsNone(self.db_saga(payload["vambrace"]))
        self.assertIsNone(self.read_ledger())

    def test_non_string_saga_rejects_before_minting(self):
        before = self.db_files()
        payload, is_error = self.call({"action": "equip", "saga": 42})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertEqual(self.db_files(), before)      # no file litter

    def test_conflict_rejects_pre_mint_with_no_litter(self):
        offender = self.make(saga="The Forge")
        before = self.db_files()
        payload, is_error = self.call({"action": "equip", "saga": "the FORGE"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "duplicate_name")
        self.assertIn(offender, payload["error"]["message"])
        self.assertEqual(self.db_files(), before)      # nothing minted
        self.assertEqual(self.read_ledger()["sagas"], {offender: "The Forge"})

    def test_conflict_against_a_waylaid_vambrace(self):
        offender = self.make(saga="The Forge")
        self.waylaid(offender)
        before = self.db_files()
        payload, is_error = self.call({"action": "equip", "saga": "the FORGE"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "duplicate_name")
        self.assertIn(offender, payload["error"]["message"])
        self.assertEqual(self.db_files(), before)

    def test_distinct_sagas_coexist(self):
        k1, k2 = self.make(saga="Alpha"), self.make(saga="Beta")
        payload, is_error = self.call({"action": "inventory"})
        self.assertFalse(is_error, repr(payload))
        self.assertEqual({e["key"]: e["saga"] for e in payload["live"]},
                         {k1: "Alpha", k2: "Beta"})


class MetaSchema(Base):
    def test_meta_ddl_is_in_the_schema(self):
        self.assertTrue(any(
            "CREATE TABLE IF NOT EXISTS meta" in s for s in schema.STATEMENTS))

    def test_bootstrap_self_heals_a_db_missing_meta(self):
        key = self.make()
        conn = db.connect(self.live_path(key))
        try:
            conn.execute("DROP TABLE meta")          # simulate a pre-phase-1 db
            schema.bootstrap(conn)                   # in-place idempotent migration
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?)",
                (armory.SAGA_KEY, "X"))
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.assertEqual(self.db_saga(key), "X")


class Waylay(Base):
    """Phase 3 (draft 5.6): waylay — suspend a live vambrace: inline mythos
    snapshot, verified non-empty, then the db moves to the waylay dir.
    All-or-nothing against the armory; no db log row."""

    def waylay(self, key, **extra):
        return self.call(dict({"action": "waylay", "vambrace": key,
                               "emit_path": self.emit_path(),
                               "emit_format": "json"}, **extra))

    def test_happy_path_moves_file_and_lands_snapshot(self):
        key = self.make(saga="The Forge")
        with open(self.live_path(key), "rb") as f:
            before = f.read()
        emit = self.emit_path()
        payload, is_error = self.waylay(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload, {
            "key": key,
            "waylaid": True,
            "export": {"path": emit, "bytes": os.path.getsize(emit)},
            "suspended_autoexport_registers": 0,
        })
        self.assertFalse(os.path.exists(self.live_path(key)))
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        with open(self.waylay_path(key), "rb") as f:
            self.assertEqual(f.read(), before)  # the db was never written
        with open(emit, "r", encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["key"], key)
        self.assertEqual(snap["saga"], "The Forge")
        self.assertEqual(set(snap["spaces"]),
                         {"chains", "blades", "reblades", "intentions",
                          "open_items", "rulings", "terms", "logs"})
        ledger = self.read_ledger()
        self.assertEqual(ledger["sagas"], {key: "The Forge"})
        entry = ledger["waylays"][key]
        self.assertRegex(entry["waylaid_at"], ISO_RE)
        self.assertEqual(entry["dismantle_requests"], [])
        self.assertEqual(self.db_log_count(key), 0)  # no log row minted

    def test_suspended_registers_are_reported_with_note(self):
        key = self.make()
        self.plant_register(key)
        payload, is_error = self.waylay(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["suspended_autoexport_registers"], 1)
        self.assertIn("note", payload)
        self.assertIn("1 autoexport register", payload["note"])
        # the register row rides with the file
        conn = db.connect(self.waylay_path(key))
        try:
            c = conn.execute(
                "SELECT COUNT(*) c FROM autoexport_registers").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(c, 1)

    def test_already_waylaid_is_validation_failed(self):
        key = self.make()
        self.waylaid(key)
        payload, is_error = self.waylay(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIn("already waylaid", payload["error"]["message"])
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        self.assertFalse(os.path.exists(self.emit_path()))

    def test_unknown_key_is_not_found(self):
        payload, is_error = self.waylay(new_uuid7())
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_bad_emit_params_reject_with_zero_side_effects(self):
        key = self.make(saga="The Forge")
        with open(self.live_path(key), "rb") as f:
            before = f.read()
        ledger_before = self.read_ledger()
        for extra in (
                {"emit_path": None},
                {"emit_path": ""},
                {"emit_path": 42},
                {"emit_format": "json"},
                {"emit_path": self.emit_path()},
                {"emit_path": self.emit_path(), "emit_format": "docx"},
                {"emit_path": self.emit_path(), "emit_format": None}):
            payload, is_error = self.call(dict(
                {"action": "waylay", "vambrace": key}, **extra))
            self.assertTrue(is_error, repr(extra))
            self.assertEqual(payload["error"]["code"], "validation_failed",
                             repr(extra))
        with open(self.live_path(key), "rb") as f:
            self.assertEqual(f.read(), before)  # byte-identical, untouched
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        self.assertEqual(self.read_ledger(), ledger_before)  # unchanged

    def test_missing_parent_rejects_with_zero_side_effects(self):
        key = self.make()
        with open(self.live_path(key), "rb") as f:
            before = f.read()
        payload, is_error = self.call({
            "action": "waylay", "vambrace": key,
            "emit_path": os.path.join(self.armory, "no-such-dir", "s.json"),
            "emit_format": "json"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIn("parent directory", payload["error"]["message"])
        with open(self.live_path(key), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        self.assertFalse(os.path.exists(
            os.path.join(self.armory, "no-such-dir")))  # never created

    def test_snapshot_failure_stays_live(self):
        key = self.make()
        with mock.patch.object(snapshot, "write_mythos",
                               side_effect=OSError("disk full")):
            payload, is_error = self.waylay(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "export_failed")
        self.assertIn("waylay snapshot failed", payload["error"]["message"])
        self.assertTrue(os.path.isfile(self.live_path(key)))
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        self.assertIsNone(self.read_ledger())

    def test_verification_failure_stays_live_without_cleanup(self):
        key = self.make()
        emit = self.emit_path()
        with open(emit, "w", encoding="utf-8") as f:
            f.write("pre-existing")

        def wipe(path, k, p, fmt):
            with open(p, "w", encoding="utf-8") as f:
                f.write("")
            return 0

        with mock.patch.object(snapshot, "write_mythos", side_effect=wipe):
            payload, is_error = self.call({
                "action": "waylay", "vambrace": key,
                "emit_path": emit, "emit_format": "json"})
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "export_failed")
        self.assertIn("verification failed", payload["error"]["message"])
        self.assertTrue(os.path.isfile(self.live_path(key)))
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        self.assertTrue(os.path.exists(emit))  # never cleaned up
        self.assertEqual(os.path.getsize(emit), 0)

    def test_occupied_target_rejects_the_impossible_state(self):
        key = self.make()
        os.makedirs(os.path.join(self.armory, armory.WAYLAY_DIR),
                    exist_ok=True)
        with open(self.waylay_path(key), "wb") as f:
            f.write(b"occupant")
        payload, is_error = self.waylay(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIn("occupied", payload["error"]["message"])
        self.assertTrue(os.path.isfile(self.live_path(key)))
        with open(self.waylay_path(key), "rb") as f:
            self.assertEqual(f.read(), b"occupant")

    def test_failed_ledger_write_warns_but_never_fails(self):
        key = self.make()
        with mock.patch.object(
                armory, "write_ledger",
                return_value="armory ledger armory.json write failed "
                             "(simulated)") as w:
            payload, is_error = self.waylay(key)
        self.assertFalse(is_error, repr(payload))
        self.assertTrue(os.path.isfile(self.waylay_path(key)))  # truth
        self.assertIn("ledger_warning", payload)
        w.assert_called_once()

    def test_fail_closed_routing_validation(self):
        for args, code in (
                ({}, "missing_vambrace"),
                ({"vambrace": ""}, "missing_vambrace"),
                ({"vambrace": 42}, "missing_vambrace"),
                ({"vambrace": "not-a-uuid"}, "malformed_vambrace")):
            payload, is_error = self.call(dict({"action": "waylay"}, **args))
            self.assertTrue(is_error, repr(args))
            self.assertEqual(payload["error"]["code"], code, repr(args))


class Recover(Base):
    """Phase 3 (draft 5.7): recover — move a waylaid vambrace back; the
    dismantle count resets (the intent chain broke)."""

    def waylaid_with(self, key, requests=()):
        self.waylaid(key)
        ledger = self.read_ledger() or {"sagas": {}, "waylays": {}}
        ledger["waylays"][key] = {
            "waylaid_at": "2026-01-01T00:00:00.000Z",
            "dismantle_requests": list(requests)}
        armory.write_ledger(self.armory, ledger)

    def recover(self, key):
        return self.call({"action": "recover", "vambrace": key})

    def test_happy_path_moves_back_and_resets(self):
        key = self.make(saga="Waylaid")
        self.waylaid_with(key, ["t1", "t2"])
        with open(self.waylay_path(key), "rb") as f:
            before = f.read()
        payload, is_error = self.recover(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            payload, {"key": key, "recovered": True, "count_reset": True})
        self.assertTrue(os.path.isfile(self.live_path(key)))
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        with open(self.live_path(key), "rb") as f:
            self.assertEqual(f.read(), before)
        ledger = self.read_ledger()
        self.assertEqual(ledger["waylays"], {})      # count reset
        self.assertEqual(ledger["sagas"], {key: "Waylaid"})
        self.assertEqual(self.db_log_count(key), 0)

    def test_no_requests_gives_count_reset_false(self):
        key = self.make()
        self.waylaid_with(key)
        payload, is_error = self.recover(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            payload, {"key": key, "recovered": True, "count_reset": False})

    def test_missing_ledger_entry_never_blocks_the_move(self):
        key = self.make()
        self.waylaid(key)               # file moved, no ledger entry at all
        payload, is_error = self.recover(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(
            payload, {"key": key, "recovered": True, "count_reset": False})
        self.assertTrue(os.path.isfile(self.live_path(key)))
        self.assertIsNone(self.read_ledger())  # nothing to rewrite

    def test_live_key_is_not_found(self):
        key = self.make()
        payload, is_error = self.recover(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertTrue(os.path.isfile(self.live_path(key)))

    def test_unknown_key_is_not_found(self):
        payload, is_error = self.recover(new_uuid7())
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_occupied_live_location_rejects(self):
        key = self.make()
        self.waylaid_with(key)
        with open(self.live_path(key), "wb") as f:
            f.write(b"occupant")
        with open(self.waylay_path(key), "rb") as f:
            waylaid_before = f.read()
        payload, is_error = self.recover(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIn("occupied", payload["error"]["message"])
        with open(self.waylay_path(key), "rb") as f:
            self.assertEqual(f.read(), waylaid_before)  # untouched
        with open(self.live_path(key), "rb") as f:
            self.assertEqual(f.read(), b"occupant")

    def test_register_note_on_recover(self):
        key = self.make()
        self.plant_register(key)
        self.waylaid(key)
        payload, is_error = self.recover(key)
        self.assertFalse(is_error, repr(payload))
        self.assertIn("note", payload)
        self.assertIn("1 autoexport register", payload["note"])
        self.assertIn("resume", payload["note"])

    def test_fail_closed_routing_validation(self):
        for args, code in (
                ({}, "missing_vambrace"),
                ({"vambrace": ""}, "missing_vambrace"),
                ({"vambrace": 42}, "missing_vambrace"),
                ({"vambrace": "not-a-uuid"}, "malformed_vambrace")):
            payload, is_error = self.call(dict({"action": "recover"}, **args))
            self.assertTrue(is_error, repr(args))
            self.assertEqual(payload["error"]["code"], code, repr(args))


class Dismantle(Base):
    """Phase 3 (draft 5.8): dismantle — the multi-step destruction process.
    Requests 1-2 succeed with escalating warnings; request 3 destroys."""

    def waylaid_via(self, key, requests=()):
        payload, is_error = self.call({
            "action": "waylay", "vambrace": key,
            "emit_path": self.emit_path(), "emit_format": "json"})
        self.assertFalse(is_error, repr(payload))
        if requests:
            ledger = self.read_ledger()
            ledger["waylays"][key]["dismantle_requests"] = list(requests)
            armory.write_ledger(self.armory, ledger)

    def dismantle(self, key):
        return self.call({"action": "dismantle", "vambrace": key})

    def test_state_machine_warns_then_destroys(self):
        key = self.make(saga="The Forge")
        self.waylaid_via(key)
        p1, e1 = self.dismantle(key)
        self.assertFalse(e1, repr(p1))
        self.assertEqual(p1, {
            "key": key, "dismantled": False, "request": 1, "of": 3,
            "warning": (
                "request 1 of 3 for 'The Forge': nothing destroyed. The "
                "next request will still only warn; the THIRD request "
                "destroys the waylaid database permanently. To stop, "
                "recover this vambrace (action=recover, "
                f"vambrace={key}).")})
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        self.assertEqual(
            len(self.read_ledger()["waylays"][key]["dismantle_requests"]), 1)
        p2, e2 = self.dismantle(key)
        self.assertFalse(e2, repr(p2))
        self.assertEqual((p2["key"], p2["dismantled"], p2["request"],
                          p2["of"]), (key, False, 2, 3))
        self.assertEqual(p2["warning"],
                         "request 2 of 3 for 'The Forge': nothing destroyed "
                         "yet. The NEXT request will destroy the waylaid "
                         "database PERMANENTLY. To stop, recover this "
                         f"vambrace (action=recover, vambrace={key}).")
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        self.assertEqual(
            len(self.read_ledger()["waylays"][key]["dismantle_requests"]), 2)
        p3, e3 = self.dismantle(key)
        self.assertFalse(e3, repr(p3))
        self.assertEqual(p3, {"key": key, "dismantled": True})
        self.assertFalse(os.path.exists(self.waylay_path(key)))
        self.assertFalse(os.path.exists(self.live_path(key)))
        ledger = self.read_ledger()
        self.assertEqual(ledger["waylays"], {})
        self.assertEqual(ledger["sagas"], {})  # the ghost saga is removed

    def test_warning_names_the_key_without_a_saga(self):
        key = self.make()
        self.waylaid_via(key)
        p1, e1 = self.dismantle(key)
        self.assertFalse(e1, repr(p1))
        self.assertTrue(
            p1["warning"].startswith(f"request 1 of 3 for key {key}: "))

    def test_live_key_rejected_waylay_first(self):
        key = self.make(saga="The Forge")
        payload, is_error = self.dismantle(key)
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertIn("waylay", payload["error"]["message"])
        self.assertTrue(os.path.isfile(self.live_path(key)))

    def test_unknown_key_is_not_found(self):
        payload, is_error = self.dismantle(new_uuid7())
        self.assertTrue(is_error)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_corrupt_ledger_zeroes_the_count_fail_closed(self):
        key = self.make(saga="The Forge")
        self.waylaid_via(key, ["t1", "t2"])   # one more request would destroy
        self.corrupt_ledger()
        payload, is_error = self.dismantle(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["dismantled"], False)  # never destroys
        self.assertEqual(payload["request"], 1)
        self.assertEqual(payload["of"], 3)
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        entry = self.read_ledger()["waylays"][key]
        self.assertEqual(len(entry["dismantle_requests"]), 1)
        self.assertRegex(entry["waylaid_at"], ISO_RE)  # refreshed

    def test_tampered_count_is_zeroed_with_a_ledger_warning(self):
        key = self.make(saga="The Forge")
        self.waylaid_via(key)
        ledger = self.read_ledger()
        ledger["waylays"][key]["dismantle_requests"] = ["a", "b", "c"]
        armory.write_ledger(self.armory, ledger)
        payload, is_error = self.dismantle(key)
        self.assertFalse(is_error, repr(payload))
        self.assertEqual(payload["request"], 1)
        self.assertIn("anomalous", payload["ledger_warning"])
        self.assertIn("fail-closed", payload["ledger_warning"])
        self.assertTrue(os.path.isfile(self.waylay_path(key)))
        self.assertEqual(
            len(self.read_ledger()["waylays"][key]["dismantle_requests"]), 1)

    def test_malformed_request_shapes_zero_the_count(self):
        for bad in ("two", ["t1", 42], ["t1", ""], None):
            key = self.make()
            self.waylaid_via(key)
            ledger = self.read_ledger()
            ledger["waylays"][key]["dismantle_requests"] = bad
            armory.write_ledger(self.armory, ledger)
            payload, is_error = self.dismantle(key)
            self.assertFalse(is_error, repr(bad))
            self.assertEqual(payload["request"], 1, repr(bad))
            self.assertNotIn("ledger_warning", payload)
            self.assertTrue(os.path.isfile(self.waylay_path(key)),
                            repr(bad))

    def test_recover_resets_the_count_for_the_next_waylay(self):
        key = self.make(saga="The Forge")
        self.waylaid_via(key)
        self.dismantle(key)
        self.dismantle(key)
        pr, er = self.call({"action": "recover", "vambrace": key})
        self.assertFalse(er, repr(pr))
        self.assertTrue(pr["count_reset"])
        p2, e2 = self.call({
            "action": "waylay", "vambrace": key,
            "emit_path": self.emit_path("snap2.json"),
            "emit_format": "json"})
        self.assertFalse(e2, repr(p2))
        p3, e3 = self.dismantle(key)
        self.assertFalse(e3, repr(p3))
        self.assertEqual(p3["request"], 1)  # fresh count

    def test_fail_closed_routing_validation(self):
        for args, code in (
                ({}, "missing_vambrace"),
                ({"vambrace": ""}, "missing_vambrace"),
                ({"vambrace": 42}, "missing_vambrace"),
                ({"vambrace": "not-a-uuid"}, "malformed_vambrace")):
            payload, is_error = self.call(
                dict({"action": "dismantle"}, **args))
            self.assertTrue(is_error, repr(args))
            self.assertEqual(payload["error"]["code"], code, repr(args))


class LifecycleInventory(Base):
    """The phase-1 inventory action reflects every phase-3 transition."""

    def test_inventory_tracks_waylay_dismantle_and_destroy(self):
        key = self.make(saga="The Forge")
        p, e = self.call({"action": "inventory"})
        self.assertFalse(e, repr(p))
        self.assertEqual([x["key"] for x in p["live"]], [key])
        self.assertEqual(p["waylaid"], [])
        p, e = self.call({"action": "waylay", "vambrace": key,
                          "emit_path": self.emit_path(),
                          "emit_format": "json"})
        self.assertFalse(e, repr(p))
        p, e = self.call({"action": "inventory"})
        self.assertFalse(e, repr(p))
        self.assertEqual(p["live"], [])
        w = p["waylaid"][0]
        self.assertEqual(w["key"], key)
        self.assertEqual(w["saga"], "The Forge")
        self.assertRegex(w["waylaid_at"], ISO_RE)
        self.assertEqual(w["dismantle_requests"], 0)
        self.assertEqual(w["dismantles_until_destroy"], 3)
        self.call({"action": "dismantle", "vambrace": key})
        self.call({"action": "dismantle", "vambrace": key})
        p, e = self.call({"action": "inventory"})
        self.assertFalse(e, repr(p))
        w = p["waylaid"][0]
        self.assertEqual(w["dismantle_requests"], 2)
        self.assertEqual(w["dismantles_until_destroy"], 1)
        p, e = self.call({"action": "dismantle", "vambrace": key})
        self.assertFalse(e, repr(p))
        self.assertTrue(p["dismantled"])
        p, e = self.call({"action": "inventory"})
        self.assertEqual(p, {"live": [], "waylaid": []})
        self.assertEqual(self.read_ledger(), {"sagas": {}, "waylays": {}})


class Acquire(Base):
    """Phase 4 (draft 5.4): acquire -- key-less import of a whole-vambrace
    (mythos) snapshot file into a NEW vambrace. Entity UUIDs are preserved
    verbatim (reblades, log refs, and the graph land intact), value
    registers are upserted from the imported rows, one genesis log row
    (area 'acquire') is minted; a rejected or failed acquire leaves nothing
    behind (no file litter)."""

    def routed(self, key, verb, **args):
        return server._call_routed(self.armory, verb,
                                   dict({"vambrace": key}, **args))

    def set_saga_direct(self, key, saga):
        """The in-db saga written directly: the actions' own conflict check
        would reject a colliding name, and the harness drives the db the
        same way it drives the file location."""
        conn = db.connect(self._dbpath(key))
        try:
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (armory.SAGA_KEY, saga))
        finally:
            conn.close()

    def seed_ruling_term(self, key):
        """One ruling + one term (no routed verb covers those spaces): the
        register-upsert paths."""
        conn = db.connect(self.live_path(key))
        try:
            now = db.now_ns()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO rulings (id, number, name, state, body, "
                "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_uuid7(), "R-001", "First Ruling", "active",
                 "ruling body", now, now))
            conn.execute(
                "INSERT INTO terms (id, term, text, created, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_uuid7(), "Vambrace", "the key", now, now))
            conn.execute("COMMIT")
        finally:
            conn.close()

    def seed_content(self, key):
        """All eight spaces via routed verbs + the ruling/term seeds;
        returns the chain uuid (a stack root)."""
        chain = self.routed(key, "chain_put", name="A",
                            intentions=[{"name": "I1"}])[0]["id"]
        self.routed(key, "blade_insert", chain=chain, text="b1")
        self.routed(key, "reblade_add", src=chain, dst=chain, kind="see",
                    note="n")
        self.routed(key, "open_item", action="put", name="oi1",
                    state="open", text="t")
        self.seed_ruling_term(key)
        return chain

    def furnish_mythos(self, key, fmt="json", name=None):
        emit = self.emit_path(name or f"file1.{fmt}")
        payload, is_error = self.call({
            "action": "furnish", "vambrace": key, "root": "mythos",
            "emit_path": emit, "emit_format": fmt})
        self.assertFalse(is_error, repr(payload))
        return emit

    def clear_saga(self, key):
        """The saga action's '' = clear: after a furnish, the file carries
        the saga, and the live source must drop it -- a file-carried saga
        conflict-checks against the source itself (acquire has no
        self-exclusion: the new key does not exist yet)."""
        payload, is_error = self.call(
            {"action": "saga", "vambrace": key, "label": ""})
        self.assertFalse(is_error, repr(payload))

    def acquire(self, path, **extra):
        return self.call(dict({"action": "acquire", "path": path}, **extra))

    def parse(self, path, fmt):
        with open(path, "r", encoding="utf-8") as f:
            return snapshot.parse_mythos(f.read(), fmt)

    def _minimal_mythos(self, kind, ostate, rstate):
        iso = db.ns_to_iso(db.now_ns())
        return json.dumps({
            "exported_at": iso,
            "key": new_uuid7(),
            "spaces": {
                "chains": [],
                "blades": [],
                "reblades": [{
                    "src_uuid": new_uuid7(), "dst_uuid": new_uuid7(),
                    "kind": kind, "created": iso,
                }],
                "intentions": [],
                "open_items": [{
                    "id": new_uuid7(), "name": "Stuck Item", "state": ostate,
                    "created": iso, "updated": iso,
                }],
                "rulings": [{
                    "id": new_uuid7(), "number": "R-X", "name": "Stale One",
                    "state": rstate, "created": iso, "updated": iso,
                }],
                "terms": [],
                "logs": [],
            },
        })

    def test_round_trip_all_four_formats(self):
        key = self.make()
        self.seed_content(key)
        for fmt in ("md", "csv", "json", "ndjson"):
            file1 = self.furnish_mythos(key, fmt, f"file1.{fmt}")
            p, e = self.acquire(file1, format=fmt)
            self.assertFalse(e, repr(p))
            key2 = p["key"]
            self.assertNotEqual(key2, key, fmt)
            p1 = self.parse(file1, fmt)
            # the import counts are the seeded content, read from the file
            self.assertEqual(
                p["imported"],
                {s: len(p1["spaces"][s]) for s in snapshot.SPACES}, fmt)
            for s in ("chains", "blades", "reblades", "intentions",
                      "open_items", "rulings", "terms"):
                self.assertEqual(p["imported"][s], 1, fmt)
            self.assertIsInstance(p["log_id"], int, fmt)
            # the acquired vambrace re-snapshots identically (the genesis
            # row is the only difference, at the max log id + 1)
            file2 = self.furnish_mythos(key2, fmt, f"file2.{fmt}")
            p2 = self.parse(file2, fmt)
            for s in snapshot.SPACES:
                if s != "logs":
                    self.assertEqual(p2["spaces"][s], p1["spaces"][s],
                                     (fmt, s))
            logs1 = p1["spaces"]["logs"]
            logs2 = p2["spaces"]["logs"]
            self.assertEqual(logs2[:len(logs1)], logs1, fmt)
            extra = logs2[len(logs1):]
            self.assertEqual(len(extra), 1, fmt)
            gen = extra[0]
            self.assertEqual(gen["area"], "acquire", fmt)
            self.assertEqual(gen["title"], "acquire: from " + file1, fmt)
            self.assertEqual(gen["refs"], [], fmt)
            self.assertEqual(gen["id"], max(l["id"] for l in logs1) + 1, fmt)
            self.assertRegex(p1["exported_at"], ISO_RE, fmt)
            self.assertRegex(p2["exported_at"], ISO_RE, fmt)
            self.assertEqual(p1["key"], key, fmt)
            self.assertEqual(p2["key"], key2, fmt)
            # pop exported_at before the whole-payload comparison: file2 is
            # file1 + the genesis row, re-keyed
            p1.pop("exported_at")
            p2.pop("exported_at")
            expected2 = dict(p1)
            expected2["key"] = key2
            expected2["spaces"] = dict(p1["spaces"])
            expected2["spaces"]["logs"] = logs1 + [gen]
            self.assertEqual(p2, expected2, fmt)

    def test_round_trip_preserves_saga(self):
        key = self.make(saga="My Saga")
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        self.clear_saga(key)  # the file carries "My Saga"; the source drops it
        p, e = self.acquire(file1, format="json")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        self.assertEqual(p["saga"], "My Saga")
        self.assertEqual(self.db_saga(key2), "My Saga")
        self.assertEqual(self.read_ledger()["sagas"], {key2: "My Saga"})

    def test_saga_supplied_beats_file(self):
        key = self.make(saga="FromFile")
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        p, e = self.acquire(file1, format="json", saga="Supplied")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        self.assertEqual(p["saga"], "Supplied")
        self.assertEqual(self.db_saga(key2), "Supplied")
        self.assertEqual(self.read_ledger()["sagas"][key2], "Supplied")

    def test_saga_empty_string_is_deliberate_none(self):
        key = self.make(saga="FromFile")
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        p, e = self.acquire(file1, format="json", saga="")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        self.assertNotIn("saga", p)
        self.assertIsNone(self.db_saga(key2))
        self.assertEqual(self.read_ledger()["sagas"], {key: "FromFile"})

    def test_saga_none_everywhere(self):
        key = self.make()
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        p, e = self.acquire(file1, format="json")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        self.assertNotIn("saga", p)
        self.assertIsNone(self.db_saga(key2))
        self.assertIsNone(self.read_ledger())  # nothing to mirror

    def test_conflict_supplied_names_offender(self):
        offender = self.make(saga="Taken")
        key = self.make()
        self.seed_content(key)
        file1 = self.furnish_mythos(key)  # the file carries no saga
        before = self.db_files()
        p, e = self.acquire(file1, format="json", saga="taken")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "duplicate_name")
        self.assertIn(offender, p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_conflict_file_carried_names_offender(self):
        src = self.make(saga="Taken")
        self.seed_content(src)
        file1 = self.furnish_mythos(src)  # the file carries "Taken"
        self.clear_saga(src)  # only the file carries it now
        offender = self.make()
        self.set_saga_direct(offender, "taken")
        before = self.db_files()
        p, e = self.acquire(file1, format="json")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "duplicate_name")
        self.assertIn(offender, p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_conflict_against_waylaid(self):
        a = self.make(saga="Taken")
        self.waylaid(a)  # the waylaid db holds "Taken"
        c = self.make()
        self.seed_content(c)
        self.set_saga_direct(c, "taken")
        file1 = self.furnish_mythos(c)  # the file carries "taken"
        self.clear_saga(c)  # live c drops it: only waylaid a holds "Taken"
        before = self.db_files()
        p, e = self.acquire(file1, format="json")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "duplicate_name")
        self.assertIn(a, p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_stack_shaped_rejected(self):
        key = self.make()
        chain = self.seed_content(key)
        emit = self.emit_path("stack.json")
        p, e = self.call({
            "action": "furnish", "vambrace": key, "root": chain,
            "emit_path": emit, "emit_format": "json"})
        self.assertFalse(e, repr(p))
        before = self.db_files()
        p, e = self.acquire(emit, format="json")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "validation_failed")
        self.assertIn("not a whole-vambrace snapshot", p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_corrupt_file_rejected(self):
        path = self.emit_path("corrupt.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json {")
        before = self.db_files()
        p, e = self.acquire(path, format="json")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "validation_failed")
        self.assertIn("corrupt json snapshot", p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_missing_file(self):
        before = self.db_files()
        p, e = self.acquire(os.path.join(self.armory, "nope.json"),
                            format="json")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "not_found")
        self.assertIn("acquire source not found", p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_bad_format_and_missing_format(self):
        key = self.make()
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        before = self.db_files()
        p, e = self.acquire(file1, format="doc")
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "validation_failed")
        for fmt in ("md", "csv", "json", "ndjson"):
            self.assertIn(fmt, p["error"]["message"])
        p, e = self.acquire(file1)
        self.assertTrue(e)
        self.assertEqual(p["error"]["code"], "validation_failed")
        self.assertIn("must be one of", p["error"]["message"])
        self.assertEqual(self.db_files(), before)  # no litter

    def test_register_upserts_from_imported_rows(self):
        path = self.emit_path("minimal.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._minimal_mythos("wibble", "stuck", "stale"))
        p, e = self.acquire(path, format="json")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        self.assertEqual(p["imported"]["reblades"], 1)
        self.assertEqual(p["imported"]["open_items"], 1)
        self.assertEqual(p["imported"]["rulings"], 1)
        self.assertEqual(p["imported"]["chains"], 0)
        conn = db.connect(self.live_path(key2))
        try:
            def one(table, col, val):
                return conn.execute(
                    f"SELECT COUNT(*) c FROM {table} WHERE {col} = ?",
                    (val,)).fetchone()["c"] == 1
            self.assertTrue(one("reblade_kinds", "kind", "wibble"))
            self.assertTrue(one("open_item_states", "name", "stuck"))
            self.assertTrue(one("ruling_states", "name", "stale"))
            # the rows landed (the FKs resolved against the upserted rows)
            for table in ("reblades", "open_items", "rulings"):
                self.assertEqual(
                    conn.execute(
                        f"SELECT COUNT(*) c FROM {table}").fetchone()["c"], 1)
        finally:
            conn.close()

    def test_genesis_row(self):
        path = self.emit_path("minimal2.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._minimal_mythos("name-ref", "open", "active"))
        p, e = self.acquire(path, format="json")
        self.assertFalse(e, repr(p))
        key2 = p["key"]
        conn = db.connect(self.live_path(key2))
        try:
            rows = conn.execute(
                "SELECT id, title, area, body FROM logs "
                "WHERE area = 'acquire'").fetchall()
            self.assertEqual(len(rows), 1)
            gen = rows[0]
            self.assertEqual(gen["id"], p["log_id"])
            self.assertEqual(gen["title"], "acquire: from " + path)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) c FROM log_refs WHERE log_id = ?",
                    (gen["id"],)).fetchone()["c"], 0)
            self.assertEqual(json.loads(gen["body"]), [{
                "imported": p["imported"], "op": "acquire", "path": path}])
        finally:
            conn.close()

    def test_acquired_is_routable(self):
        key = self.make()
        self.seed_content(key)
        file1 = self.furnish_mythos(key)
        p, e = self.acquire(file1, format="json")
        self.assertFalse(e, repr(p))
        out, is_error = self.routed(p["key"], "list", space="chain")
        self.assertFalse(is_error, repr(out))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["name"], "A")


if __name__ == "__main__":
    unittest.main()
