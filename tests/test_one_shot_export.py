"""One-shot export action (SPEC 3.18) -- the vambrace ``export`` action.

Unlike register_autoexport, export performs NO database mutations: no
register row, no log row, no log_refs, no stored signature. The real
server handlers drive a real vault file (same in-process pattern as
test_autoexport.py); QATENA_EXPORT_SYNC=1 is set so the sig-gated
register re-export path in one test also runs inline and deterministically.
"""
import json
import os
import unittest

from qatena import db, server
from qatena.uuid7 import new_uuid7
from helpers import ISO_RE, QatenaTestCase


class OneShotExportTests(QatenaTestCase):
    def setUp(self):
        super().setUp()
        # a proper vault file (<key>.db) so the server's key validation passes
        self.conn.close()
        payload, is_err = server._call_vambrace(self._tmp, {"action": "create"})
        self.assertFalse(is_err, payload)
        self._key = payload["vambrace"]
        self.db_path = os.path.join(self._tmp, self._key + ".db")
        self.conn = db.connect(self.db_path)
        self._old_sync = os.environ.get("QATENA_EXPORT_SYNC")
        os.environ["QATENA_EXPORT_SYNC"] = "1"  # test seam: inline exporter

    def tearDown(self):
        if self._old_sync is None:
            os.environ.pop("QATENA_EXPORT_SYNC", None)
        else:
            os.environ["QATENA_EXPORT_SYNC"] = self._old_sync
        super().tearDown()

    # -- helpers -------------------------------------------------------------
    def _code(self, payload):
        return payload["error"]["code"] if "error" in payload else None

    def _export(self, root, emit_path, emit_format="json",
                pointer_fidelity=None):
        args = {"action": "export", "root": root,
                "emit_path": emit_path, "emit_format": emit_format}
        if pointer_fidelity is not None:
            args["pointer_fidelity"] = pointer_fidelity
        args["vambrace"] = self._key
        return server._call_vambrace(self._tmp, args)

    def _register(self, root, emit_path, emit_format="json",
                  pointer_fidelity=None):
        args = {"action": "register_autoexport", "vambrace": self._key,
                "root": root, "emit_path": emit_path,
                "emit_format": emit_format}
        if pointer_fidelity is not None:
            args["pointer_fidelity"] = pointer_fidelity
        return server._call_vambrace(self._tmp, args)

    def _write(self, verb, **args):
        return server._call_routed(self._tmp, verb, dict(args, vambrace=self._key))

    def _make_chain(self, name, blades=(), sections=None):
        args = {"name": name}
        if sections is not None:
            args["sections"] = sections
        res = self.verb("chain_put", **args)
        for text in blades:
            self.verb("blade_insert", chain=res["id"], text=text)
        return res["id"]

    def _blade_id(self, chain_id, text=None):
        sql = "SELECT id FROM chain_blades WHERE chain_id = ?"
        args = [chain_id]
        if text is not None:
            sql += " AND text = ?"
            args.append(text)
        return self.conn.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()["id"]

    def _db_counts(self):
        """(logs, log_refs, autoexport_registers) row counts."""
        return (
            self.conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"],
            self.conn.execute("SELECT COUNT(*) c FROM log_refs").fetchone()["c"],
            self.conn.execute(
                "SELECT COUNT(*) c FROM autoexport_registers").fetchone()["c"],
        )

    def _autoexport_logs(self):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM logs WHERE area = 'autoexport'").fetchone()["c"]

    def _register_row(self, root, emit_path):
        return self.conn.execute(
            "SELECT * FROM autoexport_registers WHERE root_uuid = ? AND emit_path = ?",
            (root, emit_path)).fetchone()

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    # -- happy path ------------------------------------------------------------
    def test_export_chain_json(self):
        cid = self._make_chain("Alpha", ("first", "second"))
        out = os.path.join(self._tmp, "one-shot.json")
        p, e = self._export(cid, out)
        self.assertFalse(e, p)
        with self.subTest("response: no mutation markers"):
            self.assertNotIn("log_id", p)
            self.assertNotIn("replaced", p)
            self.assertEqual(p["root"], cid)
            self.assertEqual(p["root_space"], "chain")
            self.assertEqual(p["emit_path"], out)
            self.assertEqual(p["emit_format"], "json")
            self.assertEqual(p["pointer_fidelity"], "out")
            self.assertEqual(p["stack"], {"chains": 1, "blades": 2})
            self.assertTrue(p["exported"])
            self.assertRegex(p["exported_at"], ISO_RE)
        with self.subTest("file: json with root/pointer_fidelity/pointers/stack"):
            self.assertTrue(os.path.exists(out))
            doc = json.loads(self._read(out))
            self.assertEqual(doc["root"]["uuid"], cid)
            self.assertEqual(doc["root"]["space"], "chain")
            self.assertEqual(doc["pointer_fidelity"], "out")
            self.assertIn("pointers", doc)
            self.assertEqual(doc["stack"]["chains"][0]["id"], cid)
            self.assertEqual(len(doc["stack"]["chains"][0]["blades"]), 2)
            self.assertEqual(doc["stack"]["blades"], [])

    def test_export_no_db_mutation(self):
        cid = self._make_chain("Alpha", ("first",))
        out = os.path.join(self._tmp, "mut.json")
        before = self._db_counts()
        p, e = self._export(cid, out)
        self.assertFalse(e, p)
        self.assertEqual(self._db_counts(), before)
        self.assertIsNone(self._register_row(cid, out))
        self.assertEqual(self._autoexport_logs(), 0)

    def test_export_sig_untouched(self):
        cid = self._make_chain("Alpha", ("first",))
        out = os.path.join(self._tmp, "sig.json")
        p, e = self._register(cid, out)
        self.assertFalse(e, p)
        # registration mints TWO autoexport log rows: the register row
        # (_register_tx) and the synchronous first export (child export())
        self.assertEqual(self._autoexport_logs(), 2)
        row = self._register_row(cid, out)
        self.assertIsNotNone(row["last_sig"])
        sig_before = row["last_sig"]
        emitted_before = row["last_emitted"]
        with self.subTest("one-shot to the same path: file re-lands, sig untouched"):
            p, e = self._export(cid, out)
            self.assertFalse(e, p)
            doc = json.loads(self._read(out))
            self.assertEqual(doc["root"]["uuid"], cid)
            self.assertEqual(len(doc["stack"]["chains"][0]["blades"]), 1)
            row = self._register_row(cid, out)
            self.assertEqual(row["last_sig"], sig_before)
            self.assertEqual(row["last_emitted"], emitted_before)
            self.assertEqual(self._autoexport_logs(), 2)  # one-shot mints none
        with self.subTest("a write re-exports via the register normally"):
            self._write("blade_insert", chain=cid, text="second")
            row = self._register_row(cid, out)
            self.assertNotEqual(row["last_sig"], sig_before)
            self.assertIsNotNone(row["last_emitted"])
            doc = json.loads(self._read(out))
            self.assertEqual(len(doc["stack"]["chains"][0]["blades"]), 2)
            self.assertEqual(self._autoexport_logs(), 3)  # + re-export row

    def test_export_fidelity_modes(self):
        inside = self._make_chain("Inside", ("i one", "i two"))
        outside = self._make_chain("Outside", ("o one",))
        i1 = self._blade_id(inside, "i one")
        i2 = self._blade_id(inside, "i two")
        o1 = self._blade_id(outside, "o one")
        self.verb("pointer_add", src=inside, dst=o1, kind="see")
        self.verb("pointer_add", src=o1, dst=inside, kind="source")
        self.verb("pointer_add", src=i1, dst=i2, kind="part-of")

        def rows(path):
            doc = json.loads(self._read(path))
            return {(r["src"], r["dst"], r["kind"]) for r in doc["pointers"]}

        cases = {
            "none": set(),
            "out": {(inside, o1, "see"), (i1, i2, "part-of")},
            "in": {(o1, inside, "source"), (i1, i2, "part-of")},
            "both": {(inside, o1, "see"), (o1, inside, "source"),
                     (i1, i2, "part-of")},
        }
        for mode, want in cases.items():
            with self.subTest(mode):
                out = os.path.join(self._tmp, "f-%s.json" % mode)
                p, e = self._export(inside, out, pointer_fidelity=mode)
                self.assertFalse(e, p)
                self.assertEqual(p["pointer_fidelity"], mode)
                self.assertEqual(rows(out), want)

    def test_export_default_fidelity(self):
        cid = self._make_chain("Alpha", ("first",))
        out = os.path.join(self._tmp, "def.json")
        p, e = self._export(cid, out)  # pointer_fidelity omitted
        self.assertFalse(e, p)
        self.assertEqual(p["pointer_fidelity"], "out")

    # -- validation ------------------------------------------------------------
    def test_export_validation(self):
        cid = self._make_chain("Alpha", ("first",))
        out = os.path.join(self._tmp, "val.json")
        with self.subTest("bad emit_format"):
            p, e = self._export(cid, out, emit_format="yaml")
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
        with self.subTest("bad pointer_fidelity"):
            p, e = self._export(cid, out, pointer_fidelity="sideways")
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
        with self.subTest("missing root"):
            p, e = server._call_vambrace(self._tmp, {
                "action": "export", "vambrace": self._key,
                "emit_path": out, "emit_format": "json"})
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
        with self.subTest("missing emit_path"):
            p, e = server._call_vambrace(self._tmp, {
                "action": "export", "vambrace": self._key, "root": cid})
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
        with self.subTest("parent directory missing"):
            p, e = self._export(cid, os.path.join(self._tmp, "nodir", "x.md"))
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
        with self.subTest("missing routing key"):
            p, e = server._call_vambrace(self._tmp, {
                "action": "export", "root": cid,
                "emit_path": out, "emit_format": "json"})
            self.assertTrue(e)
            self.assertEqual(self._code(p), "missing_vambrace")
        self.assertFalse(os.path.exists(out))
        self.assertIsNone(self._register_row(cid, out))
        self.assertEqual(self._autoexport_logs(), 0)

    def test_export_root_errors(self):
        out = os.path.join(self._tmp, "err.json")
        with self.subTest("fresh uuid -> not_found"):
            p, e = self._export(new_uuid7(), out)
            self.assertTrue(e)
            self.assertEqual(self._code(p), "not_found")
        with self.subTest("non-stackable root -> validation_failed"):
            for root in (self.insert_term("Some Term"),
                         self.insert_ruling("R-001", "A Ruling"),
                         self.insert_open_item("A Task")):
                p, e = self._export(root, out)
                self.assertTrue(e)
                self.assertEqual(self._code(p), "validation_failed")
        self.assertFalse(os.path.exists(out))

    # -- formats + root spaces ---------------------------------------------------
    def test_export_formats(self):
        cid = self._make_chain("Alpha", ("first",))
        for fmt in ("md", "csv", "ndjson"):
            with self.subTest(fmt):
                out = os.path.join(self._tmp, "fmt.%s" % fmt)
                p, e = self._export(cid, out, emit_format=fmt)
                self.assertFalse(e, p)
                self.assertTrue(os.path.exists(out))
                body = self._read(out)
                if fmt == "md":
                    self.assertEqual(body.splitlines()[0], "# Alpha")
                elif fmt == "csv":
                    self.assertEqual(
                        body.splitlines()[0],
                        "space,uuid,name,src,dst,kind,pos,text,note")
                else:
                    first = json.loads(body.splitlines()[0])
                    self.assertEqual(first["space"], "chain")

    def test_export_root_spaces(self):
        self._make_chain("Alpha", ("first", "second"),
                         sections=[{"name": "Sec"}])
        cid = self.chain_ids("Alpha")[0]
        bid = self._blade_id(cid, "first")
        sid = self.conn.execute(
            "SELECT id FROM chain_sections WHERE name = ?", ("Sec",)
        ).fetchone()["id"]
        with self.subTest("section root"):
            out = os.path.join(self._tmp, "sec.json")
            p, e = self._export(sid, out)
            self.assertFalse(e, p)
            self.assertEqual(p["root_space"], "section")
            self.assertEqual(p["stack"], {"chains": 1, "blades": 2})
            doc = json.loads(self._read(out))
            self.assertEqual(doc["root"]["space"], "section")
            self.assertEqual(len(doc["stack"]["chains"]), 1)
        with self.subTest("blade root"):
            out = os.path.join(self._tmp, "blade.json")
            p, e = self._export(bid, out)
            self.assertFalse(e, p)
            self.assertEqual(p["root_space"], "blade")
            self.assertEqual(p["stack"], {"chains": 0, "blades": 1})
            doc = json.loads(self._read(out))
            self.assertEqual(doc["root"]["space"], "blade")
            self.assertEqual(len(doc["stack"]["blades"]), 1)


if __name__ == "__main__":
    unittest.main()
