"""The mythos snapshot codec (qatena.snapshot) + the furnish mythos branch
(draft scratch/vambrace_actions_draft.md, sections 5.5 + 6).

Codec tests are direct module-level: compute_mythos / render_mythos /
parse_mythos / write_mythos against a temp armory db built with
schema.bootstrap + prepared INSERTs (data in every space). The server
tests drive the real _call_vambrace dispatch in-process (the same pattern
as tests/test_one_shot_export.py): furnish root='mythos' end-to-end,
mythos + reblade_fidelity rejected, malformed root rejected, missing root
rejected as before, register_autoexport mythos rejected.
"""
import json
import os
import shutil
import tempfile
import unittest

from qatena import db, schema, server, snapshot
from qatena.uuid7 import new_uuid7
from helpers import ISO_RE


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qatena-snapshot-")
        self.armory = os.path.join(self._tmp, "armory")
        os.makedirs(self.armory)
        payload, is_error = server._call_vambrace(self.armory, {"action": "equip"})
        self.assertFalse(is_error, "equip failed: %r" % payload)
        self.key = payload["vambrace"]
        self.db_path = os.path.join(self.armory, self.key + ".db")
        self.conn = db.connect(self.db_path)
        schema.bootstrap(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------
    def call(self, arguments):
        return server._call_vambrace(self.armory, arguments)

    def _code(self, payload):
        return payload["error"]["code"] if "error" in payload else None

    def _counts(self):
        c = self.conn
        return (
            c.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"],
            c.execute("SELECT COUNT(*) c FROM log_refs").fetchone()["c"],
            c.execute(
                "SELECT COUNT(*) c FROM autoexport_registers").fetchone()["c"],
        )

    def _read_bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _read_text(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def plant(self):
        """Fixture: data in every space (prepared statements only), plus the
        saga meta row. Returns the minted UUIDs."""
        now = db.now_ns()
        f = {
            "chain": new_uuid7(),
            "intention": new_uuid7(),
            "blade1": new_uuid7(),
            "blade2": new_uuid7(),
            "open_item": new_uuid7(),
            "ruling": new_uuid7(),
            "term": new_uuid7(),
        }
        conn = self.conn
        conn.execute(
            "INSERT INTO chain_headers (id, name, created, updated) "
            "VALUES (?,?,?,?)", (f["chain"], "Alpha", now, now))
        conn.execute(
            "INSERT INTO chain_intentions (id, name, orientation, "
            "created, updated) VALUES (?,?,?,?,?)",
            (f["intention"], "Sec", "B", now, now))
        conn.execute(
            "INSERT INTO chain_intention_map (chain_id, intention_id) "
            "VALUES (?,?)", (f["chain"], f["intention"]))
        conn.execute(
            "INSERT INTO chain_blades (id, chain_id, text, created, "
            "updated) VALUES (?,?,?,?,?)", (f["blade1"], f["chain"],
            "first blade", now, now))
        conn.execute(
            "INSERT INTO chain_blades (id, chain_id, text, created, "
            "updated) VALUES (?,?,?,?,?)", (f["blade2"], f["chain"],
            "second blade", now, now))
        conn.execute(
            "INSERT INTO blade_order (chain_id, blade_id, pos) "
            "VALUES (?,?,?)", (f["chain"], f["blade1"], 0))
        conn.execute(
            "INSERT INTO blade_order (chain_id, blade_id, pos) "
            "VALUES (?,?,?)", (f["chain"], f["blade2"], 1))
        conn.execute(
            "INSERT INTO reblades (src_uuid, dst_uuid, kind, note, "
            "created) VALUES (?,?,?,?,?)",
            (f["chain"], f["blade1"], "part-of", "inner", now))
        conn.execute(
            "INSERT INTO reblades (src_uuid, dst_uuid, kind, note, "
            "created) VALUES (?,?,?,?,?)",
            (f["blade2"], f["chain"], "source", "outer", now))
        conn.execute(
            "INSERT INTO open_items (id, name, state, text, created, "
            "updated, freeform_current, freeform_due) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f["open_item"], "A Task", "open", "task body", now, now,
             1, "2026-12-01"))
        conn.execute(
            "INSERT INTO rulings (id, number, name, state, body, created, "
            "updated) VALUES (?,?,?,?,?,?,?)",
            (f["ruling"], "R-001", "A Ruling", "active", "ruling body",
             now, now))
        conn.execute(
            "INSERT INTO terms (id, term, text, created, updated) "
            "VALUES (?,?,?,?,?)", (f["term"], "Some Term", "term body",
             now, now))
        conn.execute(
            "INSERT INTO logs (created, title, area, body) "
            "VALUES (?,?,?,?)", (now, "a log", "test", "log body"))
        f["log_id"] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO log_refs (log_id, uuid, op) VALUES (?,?,?)",
            (f["log_id"], f["chain"], "created"))
        conn.execute(
            "INSERT INTO log_refs (log_id, uuid, op) VALUES (?,?,?)",
            (f["log_id"], f["blade1"], "referenced"))
        conn.execute(
            "INSERT INTO meta (k, v) VALUES (?,?)", ("saga", "The Mythos"))
        conn.commit()
        return f


class ComputeMythos(Base):
    def test_shape_all_eight_spaces_and_meta(self):
        f = self.plant()
        p = snapshot.compute_mythos(self.conn, self.key)
        self.assertEqual(list(p), ["exported_at", "key", "saga", "spaces"])
        self.assertRegex(p["exported_at"], ISO_RE)
        self.assertEqual(p["key"], self.key)
        self.assertEqual(p["saga"], "The Mythos")
        self.assertEqual(list(p["spaces"]), list(snapshot.SPACES))
        ch = p["spaces"]["chains"]
        self.assertEqual(len(ch), 1)
        ch = ch[0]
        self.assertEqual(set(ch), {"id", "name", "created", "updated",
                                   "intentions"})
        self.assertEqual(ch["id"], f["chain"])
        self.assertEqual(ch["name"], "Alpha")
        self.assertEqual(ch["intentions"], ["Sec"])
        self.assertRegex(ch["created"], ISO_RE)
        blades = p["spaces"]["blades"]
        self.assertEqual([b["id"] for b in blades], [f["blade1"],
                                                     f["blade2"]])
        self.assertEqual([b["pos"] for b in blades], [0, 1])
        self.assertEqual(blades[0]["chain"], "Alpha")
        self.assertEqual(blades[0]["chain_id"], f["chain"])
        self.assertEqual(set(blades[0]), {"id", "chain_id", "chain", "text",
                                          "pos", "created", "updated"})
        self.assertEqual(
            {(r["src_uuid"], r["dst_uuid"], r["kind"])
             for r in p["spaces"]["reblades"]},
            {(f["chain"], f["blade1"], "part-of"),
             (f["blade2"], f["chain"], "source")})
        it = p["spaces"]["intentions"][0]
        self.assertEqual((it["name"], it["orientation"]), ("Sec", "B"))
        oi = p["spaces"]["open_items"][0]
        self.assertEqual((oi["name"], oi["state"]), ("A Task", "open"))
        self.assertEqual(oi["freeform_current"], 1)
        self.assertEqual(oi["freeform_due"], "2026-12-01")
        self.assertNotIn("freeform_pending", oi)  # NULLs omitted
        ru = p["spaces"]["rulings"][0]
        self.assertEqual((ru["number"], ru["name"]), ("R-001", "A Ruling"))
        self.assertEqual(ru["body"], "ruling body")
        t = p["spaces"]["terms"][0]
        self.assertEqual((t["term"], t["text"]), ("Some Term", "term body"))
        lg = p["spaces"]["logs"]
        self.assertEqual(len(lg), 1)
        lg = lg[0]
        self.assertEqual(lg["id"], f["log_id"])
        self.assertEqual((lg["title"], lg["area"]), ("a log", "test"))
        self.assertEqual(
            {(r["uuid"], r["op"]) for r in lg["refs"]},
            {(f["chain"], "created"), (f["blade1"], "referenced")})

    def test_bare_db_omits_saga_and_has_empty_spaces(self):
        payload, is_error = self.call({"action": "equip"})
        self.assertFalse(is_error, payload)
        key = payload["vambrace"]
        conn = db.connect(os.path.join(self.armory, key + ".db"))
        try:
            p = snapshot.compute_mythos(conn, key)
        finally:
            conn.close()
        self.assertEqual(list(p), ["exported_at", "key", "spaces"])
        self.assertNotIn("saga", p)
        for s in snapshot.SPACES:
            self.assertEqual(p["spaces"][s], [])


class Exclusions(Base):
    def test_deflected_writes_and_registers_neither_read_nor_present(self):
        f = self.plant()
        now = db.now_ns()
        self.conn.execute(
            "INSERT INTO deflected_writes (created, op, args, state) "
            "VALUES (?,?,?,?)",
            (now, "chain_put", '{"marker": "DEFLECTED-MARKER"}', "pending"))
        self.conn.execute(
            "INSERT INTO autoexport_registers (root_uuid, emit_path, "
            "emit_format, reblade_fidelity, created) "
            "VALUES (?,?,?,?,?)",
            (f["chain"], "REGISTERED-EMIT-MARKER.md", "json", "out", now))
        self.conn.commit()
        text = snapshot.render_mythos(
            snapshot.compute_mythos(self.conn, self.key), "json")
        self.assertNotIn("DEFLECTED-MARKER", text)
        self.assertNotIn("REGISTERED-EMIT-MARKER", text)
        self.assertNotIn("deflected", text)
        self.assertNotIn("autoexport", text)


class RoundTrip(Base):
    def test_parse_render_identity_all_four_formats(self):
        self.plant()
        payload = snapshot.compute_mythos(self.conn, self.key)
        for fmt in snapshot.ALLOWED_FORMATS:
            with self.subTest(fmt):
                self.assertEqual(
                    snapshot.parse_mythos(
                        snapshot.render_mythos(payload, fmt), fmt),
                    payload)

    def test_csv_header_and_meta_rows_first(self):
        self.plant()
        text = snapshot.render_mythos(
            snapshot.compute_mythos(self.conn, self.key), "csv")
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "space,uuid,name,src,dst,kind,pos,text,note,extra")
        self.assertTrue(lines[1].startswith("meta,key," + self.key))
        self.assertTrue(lines[2].startswith("meta,saga,The Mythos"))
        self.assertTrue(lines[3].startswith("meta,exported_at,"))


class ParseRejections(Base):
    def test_stack_shaped_payload_rejected(self):
        for text, fmt in (
                (json.dumps({
                    "root": {"uuid": new_uuid7(), "space": "chain",
                             "name": "A"},
                    "reblades": [], "stack": {"chains": [], "blades": []},
                    "counts": {"chains": 0, "blades": 0},
                    "reblade_fidelity": "out",
                    "exported_at": "2026-11-27T00:00:00.000Z"}), "json"),
                ("space,uuid,name,src,dst,kind,pos,text,note\n"
                 "chain,c1,Alpha,,,,,,\n"
                 "blade,b1,Alpha,,,0,first,,\n", "csv")):
            with self.subTest(fmt):
                with self.assertRaises(ValueError) as ctx:
                    snapshot.parse_mythos(text, fmt)
                self.assertIn("not a whole-vambrace snapshot",
                              str(ctx.exception))

    def test_corrupt_input_is_a_clean_error(self):
        with self.assertRaises(ValueError) as ctx:
            snapshot.parse_mythos("this is not json", "json")
        self.assertIn("corrupt json snapshot", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            snapshot.parse_mythos("this is not json", "ndjson")
        self.assertIn("corrupt ndjson snapshot", str(ctx.exception))
        with self.assertRaises(ValueError):
            snapshot.parse_mythos(42, "csv")
        with self.assertRaises(ValueError):
            snapshot.parse_mythos("x", "yaml")
        with self.assertRaises(ValueError):
            snapshot.render_mythos(
                {"exported_at": "2026-11-27T00:00:00.000Z",
                 "key": new_uuid7(),
                 "spaces": {s: [] for s in snapshot.SPACES}}, "yaml")


class WriteMythos(Base):
    def test_atomic_read_only_returns_bytes(self):
        f = self.plant()
        now = db.now_ns()
        self.conn.execute(
            "INSERT INTO autoexport_registers (root_uuid, emit_path, "
            "emit_format, reblade_fidelity, last_sig, last_emitted, "
            "created) VALUES (?,?,?,?,?,?,?)",
            (f["chain"], "reg.md", "json", "out", "sig-abc", now, now))
        self.conn.commit()
        counts_before = self._counts()
        file_before = self._read_bytes(self.db_path)
        out = os.path.join(self._tmp, "mythos.json")
        n = snapshot.write_mythos(self.db_path, self.key, out, "json")
        self.assertEqual(n, os.path.getsize(out))
        self.assertEqual(n, len(self._read_bytes(out)))
        doc = json.loads(self._read_text(out))
        self.assertEqual(doc["key"], self.key)
        self.assertEqual(doc["saga"], "The Mythos")
        self.assertRegex(doc["exported_at"], ISO_RE)
        self.assertEqual(set(doc["spaces"]), set(snapshot.SPACES))
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(self._read_bytes(self.db_path), file_before)
        self.assertEqual([x for x in os.listdir(self._tmp)
                          if ".tmp-" in x], [])


class FurnishMythos(Base):
    def _furnish(self, root, emit_path, emit_format="json", **extra):
        args = {"action": "furnish", "vambrace": self.key, "root": root,
                "emit_path": emit_path, "emit_format": emit_format}
        args.update(extra)
        return self.call(args)

    def test_mythos_end_to_end(self):
        self.plant()
        out = os.path.join(self._tmp, "mythos.json")
        file_before = self._read_bytes(self.db_path)
        counts_before = self._counts()
        p, e = self._furnish("mythos", out)
        self.assertFalse(e, p)
        self.assertEqual(list(p), ["root", "emit_path", "emit_format",
                                   "bytes", "exported"])
        self.assertEqual(p["root"], "mythos")
        self.assertEqual(p["emit_path"], out)
        self.assertEqual(p["emit_format"], "json")
        self.assertTrue(p["exported"])
        self.assertEqual(p["bytes"], os.path.getsize(out))
        self.assertNotIn("log_id", p)  # no mutation markers
        doc = json.loads(self._read_text(out))
        self.assertEqual(doc["key"], self.key)
        self.assertEqual(doc["saga"], "The Mythos")
        self.assertRegex(doc["exported_at"], ISO_RE)
        self.assertEqual(set(doc["spaces"]), set(snapshot.SPACES))
        self.assertEqual(len(doc["spaces"]["chains"]), 1)
        self.assertEqual(len(doc["spaces"]["blades"]), 2)
        self.assertEqual(self._counts(), counts_before)
        self.assertEqual(self._read_bytes(self.db_path), file_before)

    def test_mythos_lands_in_all_four_formats(self):
        self.plant()
        payload = snapshot.compute_mythos(self.conn, self.key)
        # exported_at is stamped per render (wall clock) — not comparable
        # across calls; compare the content around it.
        payload.pop("exported_at")
        for fmt in snapshot.ALLOWED_FORMATS:
            with self.subTest(fmt):
                out = os.path.join(self._tmp, "mythos.%s" % fmt)
                p, e = self._furnish("mythos", out, emit_format=fmt)
                self.assertFalse(e, p)
                self.assertEqual(p["bytes"], os.path.getsize(out))
                parsed = snapshot.parse_mythos(self._read_text(out), fmt)
                self.assertRegex(parsed["exported_at"], ISO_RE)
                parsed.pop("exported_at")
                self.assertEqual(parsed, payload)

    def test_mythos_rejects_reblade_fidelity(self):
        self.plant()
        out = os.path.join(self._tmp, "fid.json")
        counts_before = self._counts()
        for mode in ("none", "in", "out", "both"):
            with self.subTest(mode):
                p, e = self._furnish("mythos", out,
                                     reblade_fidelity=mode)
                self.assertTrue(e)
                self.assertEqual(self._code(p), "validation_failed")
                self.assertIn("mythos", p["error"]["message"])
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self._counts(), counts_before)

    def test_garbage_root_is_malformed(self):
        self.plant()
        out = os.path.join(self._tmp, "bad.json")
        p, e = self._furnish("not-a-uuid", out)
        self.assertTrue(e)
        self.assertEqual(self._code(p), "malformed")
        self.assertIn("not-a-uuid", p["error"]["message"])
        self.assertFalse(os.path.exists(out))

    def test_uuid_shaped_unresolved_root_stacks_not_found(self):
        self.plant()
        out = os.path.join(self._tmp, "nf.json")
        p, e = self._furnish(new_uuid7(), out)
        self.assertTrue(e)
        self.assertEqual(self._code(p), "not_found")
        # an unresolvable uppercase UUID is still UUID-shaped: the stack
        # path decides (not_found), the malformed gate does not overreach
        cid = self.conn.execute(
            "SELECT id FROM chain_headers LIMIT 1").fetchone()["id"]
        p, e = self._furnish(cid.upper(), out)
        self.assertTrue(e)
        self.assertEqual(self._code(p), "not_found")
        self.assertFalse(os.path.exists(out))

    def test_missing_or_empty_root_rejected_as_before(self):
        out = os.path.join(self._tmp, "no-root.json")
        for args in ({}, {"root": ""}, {"root": 42}):
            with self.subTest(args):
                p, e = self.call({
                    "action": "furnish", "vambrace": self.key,
                    "emit_path": out, "emit_format": "json", **args})
                self.assertTrue(e)
                self.assertEqual(self._code(p), "validation_failed")
        self.assertFalse(os.path.exists(out))

    def test_register_autoexport_rejects_mythos(self):
        self.plant()
        out = os.path.join(self._tmp, "reg.json")
        counts_before = self._counts()
        p, e = self.call({
            "action": "register_autoexport", "vambrace": self.key,
            "root": "mythos", "emit_path": out, "emit_format": "json"})
        self.assertTrue(e)
        self.assertEqual(self._code(p), "validation_failed")
        self.assertIn("mythos", p["error"]["message"])
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self._counts(), counts_before)

    def test_stack_mode_via_furnish_keeps_the_old_shape(self):
        f = self.plant()
        out = os.path.join(self._tmp, "stack.json")
        p, e = self._furnish(f["chain"], out)
        self.assertFalse(e, p)
        self.assertEqual(p["root"], f["chain"])
        self.assertEqual(p["root_space"], "chain")
        self.assertEqual(p["reblade_fidelity"], "out")
        self.assertEqual(p["stack"], {"chains": 1, "blades": 2})
        self.assertTrue(p["exported"])
        self.assertRegex(p["exported_at"], ISO_RE)
        self.assertNotIn("log_id", p)
        self.assertNotIn("replaced", p)
        doc = json.loads(self._read_text(out))
        self.assertEqual(doc["root"]["uuid"], f["chain"])
        self.assertEqual(doc["root"]["space"], "chain")


if __name__ == "__main__":
    unittest.main()
