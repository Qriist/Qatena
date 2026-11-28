"""Autoexport tests (SPEC 3.18) — register/deregister actions, sig-gated
re-export, cascade, renderer snapshots. All data synthetic.

Two drivers:
  * AutoexportTests — in-process: the real server handlers
    (``server._call_vambrace`` / ``server._call_routed``) against a real vambrace
    file, with QATENA_EXPORT_SYNC=1 (test seam) so the post-commit hook runs
    the exporter inline and files are deterministic.
  * AutoexportServerTests — the real stdio server (spawned), same env seam,
    to cover the wire path end to end.
"""
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from qatena import autoexport, db, schema, server
from qatena.uuid7 import new_uuid7
from helpers import ISO_RE, QatenaTestCase, REPO_ROOT, UUID7_RE


# ---------------------------------------------------------------------------
# in-process server-handler tests


class AutoexportTests(QatenaTestCase):
    def setUp(self):
        super().setUp()
        # a proper vambrace file (<key>.db) so the server's key validation passes
        self.conn.close()
        payload, is_err = server._call_vambrace(self._tmp, {"action": "equip"})
        self.assertFalse(is_err, payload)
        self._key = payload["vambrace"]
        self.db_path = os.path.join(self._tmp, self._key + ".db")
        self.conn = db.connect(self.db_path)
        schema.bootstrap(self.conn)
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

    def _register(self, root, emit_path, emit_format="json", with_key=True,
                  reblade_fidelity=None, key=None):
        args = {"action": "register_autoexport", "root": root,
                "emit_path": emit_path, "emit_format": emit_format}
        if reblade_fidelity is not None:
            args["reblade_fidelity"] = reblade_fidelity
        if with_key:
            args["vambrace"] = key if key is not None else self._key
        return server._call_vambrace(self._tmp, args)

    def _deregister(self, root, emit_path):
        return server._call_vambrace(self._tmp, {
            "action": "deregister_autoexport", "vambrace": self._key,
            "root": root, "emit_path": emit_path})

    def _write(self, verb, **args):
        return server._call_routed(self._tmp, verb, dict(args, vambrace=self._key))

    def _make_chain(self, name, blades=(), intentions=None):
        args = {"name": name}
        if intentions is not None:
            args["intentions"] = intentions
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

    def _export_logs(self):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM logs WHERE area = 'autoexport'").fetchone()["c"]

    def _register_row(self, root, emit_path):
        return self.conn.execute(
            "SELECT * FROM autoexport_registers WHERE root_uuid = ? AND emit_path = ?",
            (root, emit_path)).fetchone()

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    # -- validation ------------------------------------------------------------
    def test_register_validation_errors(self):
        cid = self._make_chain("Alpha", ("first",))
        tid = self.insert_term("Some Term")
        rid = self.insert_ruling("R-001", "A Ruling")
        out = os.path.join(self._tmp, "out.json")

        with self.subTest("emit_format outside the allowed set"):
            p, e = self._register(cid, out, emit_format="yaml")
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")

        with self.subTest("empty emit_path"):
            p, e = self._register(cid, "", emit_format="json")
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")

        with self.subTest("parent directory missing"):
            p, e = self._register(cid, os.path.join(self._tmp, "nodir", "x.md"))
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")

        with self.subTest("unknown root -> not_found"):
            p, e = self._register(new_uuid7(), out)
            self.assertTrue(e)
            self.assertEqual(self._code(p), "not_found")

        with self.subTest("dissolved (merged) root -> not_found"):
            other = self._make_chain("Other")
            self.verb("chain_merge", into=cid, fold=other)
            p, e = self._register(other, out)
            self.assertTrue(e)
            self.assertEqual(self._code(p), "not_found")

        with self.subTest("non-stackable root spaces"):
            for root in (tid, rid):
                p, e = self._register(root, out)
                self.assertTrue(e)
                self.assertEqual(self._code(p), "validation_failed")

        with self.subTest("missing routing key -> missing_vambrace"):
            p, e = self._register(cid, out, with_key=False)
            self.assertTrue(e)
            self.assertEqual(self._code(p), "missing_vambrace")

        self.assertIsNone(self._register_row(cid, out))  # nothing was stored
        self.assertEqual(self._export_logs(), 0)

    # -- register: file, state, logs -------------------------------------------
    def test_register_file_state_and_logs(self):
        cid = self._make_chain("Alpha", ("first line", "second line"))
        out = os.path.join(self._tmp, "out.json")
        p, e = self._register(cid, out)
        self.assertFalse(e, p)
        self.assertEqual(p["root"], cid)
        self.assertEqual(p["root_space"], "chain")
        self.assertEqual(p["emit_path"], out)
        self.assertEqual(p["emit_format"], "json")
        self.assertEqual(p["stack"], {"chains": 1, "blades": 2})
        self.assertTrue(p["exported"])
        self.assertRegex(p["exported_at"], ISO_RE)
        self.assertFalse(p["replaced"])
        self.assertIsInstance(p["log_id"], int)

        doc = json.loads(self._read(out))
        self.assertEqual(doc["root"], {"uuid": cid, "space": "chain", "name": "Alpha"})
        self.assertEqual(
            [b["text"] for b in doc["stack"]["chains"][0]["blades"]],
            ["first line", "second line"])

        row = self._register_row(cid, out)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["last_sig"])
        self.assertIsNotNone(row["last_emitted"])
        # register log + first-export log
        self.assertEqual(self._export_logs(), 2)
        # no torn/temp file left behind
        self.assertEqual([f for f in os.listdir(self._tmp) if ".tmp-" in f], [])

    # -- change detection -------------------------------------------------------
    def test_changed_write_reexports(self):
        cid = self._make_chain("Alpha", ("first line",))
        out = os.path.join(self._tmp, "out.json")
        p, e = self._register(cid, out)
        self.assertFalse(e, p)
        sig_before = self._register_row(cid, out)["last_sig"]
        logs_before = self._export_logs()

        p, e = self._write("blade_insert", chain=cid, text="second line")
        self.assertFalse(e, p)
        doc = json.loads(self._read(out))
        self.assertEqual(
            [b["text"] for b in doc["stack"]["chains"][0]["blades"]],
            ["first line", "second line"])
        self.assertEqual(self._export_logs(), logs_before + 1)
        self.assertNotEqual(self._register_row(cid, out)["last_sig"], sig_before)

    def test_unchanged_write_no_export(self):
        cid = self._make_chain("Alpha", ("first line",))
        bid = self._blade_id(cid)
        out = os.path.join(self._tmp, "out.json")
        # 'none': no reblade layer at all, so even a reblade write is a no-op
        p, e = self._register(cid, out, reblade_fidelity="none")
        self.assertFalse(e, p)
        file_before = self._read(out)
        sig_before = self._register_row(cid, out)["last_sig"]
        logs_before = self._export_logs()

        with self.subTest("no-op upsert of the registered chain"):
            p, e = self._write("chain_put", name="Alpha")
            self.assertFalse(e, p)

        with self.subTest("write to an unrelated chain"):
            p, e = self._write("chain_put", name="Unrelated")
            self.assertFalse(e, p)

        with self.subTest("reblade inside the stack (excluded in 'none' mode)"):
            p, e = self._write("reblade_add", src=cid, dst=bid, kind="see")
            self.assertFalse(e, p)

        self.assertEqual(self._read(out), file_before)
        self.assertEqual(self._register_row(cid, out)["last_sig"], sig_before)
        self.assertEqual(self._export_logs(), logs_before)

    # -- deregister ---------------------------------------------------------------
    def test_deregister(self):
        cid = self._make_chain("Alpha", ("first line",))
        out = os.path.join(self._tmp, "out.json")
        p, e = self._register(cid, out)
        self.assertFalse(e, p)

        p, e = self._deregister(cid, out)
        self.assertFalse(e, p)
        self.assertEqual(p, {"root": cid, "emit_path": out, "removed": True,
                             "log_id": p["log_id"]})
        self.assertIsNone(self._register_row(cid, out))
        self.assertTrue(os.path.exists(out))  # the server never deletes files
        self.assertEqual(self._export_logs(), 3)  # register + export + deregister

        file_before = self._read(out)
        logs_before = self._export_logs()
        p, e = self._write("blade_insert", chain=cid, text="after dereg")
        self.assertFalse(e, p)
        self.assertEqual(self._read(out), file_before)
        self.assertEqual(self._export_logs(), logs_before)

        p, e = self._deregister(cid, out)  # twice -> not_found
        self.assertTrue(e)
        self.assertEqual(self._code(p), "not_found")

    # -- cascade --------------------------------------------------------------------
    def test_root_deletion_cascades_registers(self):
        cid = self._make_chain("Alpha", ("first line",))
        bid = self._blade_id(cid)
        out1 = os.path.join(self._tmp, "chain.json")
        out2 = os.path.join(self._tmp, "blade.json")
        for out in (out1, out2):
            p, e = self._register(cid if out is out1 else bid, out)
            self.assertFalse(e, p)
        self.assertEqual(self._export_logs(), 4)

        p, e = self._write("chain_delete", id=cid)
        self.assertFalse(e, p)
        self.assertIsNone(self._register_row(cid, out1))
        self.assertIsNone(self._register_row(bid, out2))  # blade root too
        self.assertTrue(os.path.exists(out1))  # files stay on disk
        self.assertTrue(os.path.exists(out2))

    # -- batch: one sig check per transaction -----------------------------------------
    def test_batch_single_check(self):
        cid = self._make_chain("Alpha", ())
        out = os.path.join(self._tmp, "out.json")
        p, e = self._register(cid, out)
        self.assertFalse(e, p)
        logs_before = self._export_logs()

        p, e = self._write("batch", ops=[
            {"op": "blade_insert", "args": {"chain": cid, "text": "b1"}},
            {"op": "blade_insert", "args": {"chain": cid, "text": "b2"}},
        ])
        self.assertFalse(e, p)
        self.assertEqual(len(p["ops"]), 2)
        doc = json.loads(self._read(out))
        self.assertEqual(
            [b["text"] for b in doc["stack"]["chains"][0]["blades"]], ["b1", "b2"])
        # one transaction -> one sig check -> exactly one export
        self.assertEqual(self._export_logs(), logs_before + 1)

    # -- intention roots: membership is content -----------------------------------------
    def test_section_root_membership_reexport(self):
        c1 = self._make_chain("Alpha", ("a1",), intentions=[{"name": "Arc"}])
        c2 = self._make_chain("Beta", ("b1",), intentions=[{"name": "Arc"},
                                                         {"name": "Side"}])
        arc = self.conn.execute(
            "SELECT id FROM chain_intentions WHERE name = 'Arc'").fetchone()["id"]
        out_sec = os.path.join(self._tmp, "arc.md")
        out_chain = os.path.join(self._tmp, "beta.md")
        p, e = self._register(arc, out_sec, emit_format="md")
        self.assertFalse(e, p)
        self.assertEqual(p["root_space"], "intention")
        self.assertEqual(p["stack"], {"chains": 2, "blades": 2})
        p, e = self._register(c2, out_chain)
        self.assertFalse(e, p)

        self.assertIn("## Alpha", self._read(out_sec))
        self.assertIn("## Beta", self._read(out_sec))
        self.assertEqual(self._export_logs(), 4)

        # refile Beta out of Arc: membership is content -> both stacks change
        p, e = self._write("chain_put", id=c2, intentions=[])
        self.assertFalse(e, p)
        sec = self._read(out_sec)
        self.assertIn("## Alpha", sec)
        self.assertNotIn("## Beta", sec)
        chain_doc = json.loads(self._read(out_chain))
        self.assertEqual(chain_doc["stack"]["chains"][0]["intentions"], [])
        self.assertEqual(self._export_logs(), 6)  # one export per register

    # -- blade roots: signature is (uuid, text) only ------------------------------------
    def test_blade_root_semantics(self):
        cid = self._make_chain("Alpha", ("the text",))
        bid = self._blade_id(cid)
        out = os.path.join(self._tmp, "blade.json")
        p, e = self._register(bid, out)
        self.assertFalse(e, p)
        self.assertEqual(p["root_space"], "blade")
        self.assertEqual(p["stack"], {"chains": 0, "blades": 1})
        doc = json.loads(self._read(out))
        self.assertEqual(doc["root"]["name"], "the text")

        with self.subTest("blade_edit re-exports (text is in the signature)"):
            file_before = self._read(out)
            logs_before = self._export_logs()
            p, e = self._write("blade_edit", id=bid, text="new text")
            self.assertFalse(e, p)
            doc = json.loads(self._read(out))
            self.assertEqual(doc["root"]["name"], "new text")
            self.assertNotEqual(self._read(out), file_before)
            self.assertEqual(self._export_logs(), logs_before + 1)

        with self.subTest("cross-chain blade_move does NOT re-export"):
            c2 = self._make_chain("Beta")
            file_before = self._read(out)
            logs_before = self._export_logs()
            p, e = self._write("blade_move", id=bid, to_chain=c2)
            self.assertFalse(e, p)
            self.assertEqual(self._read(out), file_before)
            self.assertEqual(self._export_logs(), logs_before)

    # -- re-register: format replacement -------------------------------------------------
    def test_reregister_replaces_format(self):
        cid = self._make_chain("Alpha", ("first line",))
        out = os.path.join(self._tmp, "out.txt")
        p, e = self._register(cid, out, emit_format="json")
        self.assertFalse(e, p)
        self.assertFalse(p["replaced"])
        self.assertTrue(self._read(out).lstrip().startswith("{"))

        p, e = self._register(cid, out, emit_format="md")
        self.assertFalse(e, p)
        self.assertTrue(p["replaced"])
        self.assertTrue(self._read(out).startswith("# "))

        p, e = self._register(cid, out, emit_format="json")
        self.assertFalse(e, p)
        self.assertTrue(p["replaced"])
        self.assertEqual(self._register_row(cid, out)["emit_format"], "json")
        # one row, all along
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM autoexport_registers").fetchone()["c"], 1)

    # -- signatures -----------------------------------------------------------------------
    def test_signature_stable_and_sensitive(self):
        cid = self._make_chain("Alpha", ("first line",))
        bid = self._blade_id(cid)

        def sig():
            # 'none': content-only signature, reblades excluded by mode
            return autoexport.stack_signature(
                autoexport.compute_stack(self.conn, cid, "none"))

        s1, s2 = sig(), sig()
        self.assertEqual(s1, s2)
        self.assertEqual(len(s1), 64)  # sha256 hex

        self.verb("reblade_add", src=cid, dst=bid, kind="see")  # excluded in 'none'
        self.assertEqual(sig(), s1)

        self.verb("blade_edit", id=bid, text="changed")
        self.assertNotEqual(sig(), s1)

        with self.subTest("the same reblades ARE signed in 'out' mode"):
            o1 = autoexport.stack_signature(
                autoexport.compute_stack(self.conn, cid, "out"))
            self.verb("reblade_add", src=cid, dst=bid, kind="supersedes")
            o2 = autoexport.stack_signature(
                autoexport.compute_stack(self.conn, cid, "out"))
            self.assertNotEqual(o1, o2)

    # -- the exporter child, standalone -----------------------------------------------------
    def test_child_entrypoint_standalone(self):
        cid = self._make_chain("Gamma", ("a gamma line",))
        out = os.path.join(self._tmp, "child.csv")
        p, e = self._register(cid, out, emit_format="csv")
        self.assertFalse(e, p)
        logs_after_register = self._export_logs()  # register log + first-export log
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env.pop("QATENA_EXPORT_SYNC", None)
        proc = subprocess.run(
            [sys.executable, "-m", "qatena.autoexport", self.db_path, cid, out, "csv"],
            capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = self._read(out)
        self.assertIn("a gamma line", body)
        self.assertTrue(body.startswith(
            "space,uuid,name,src,dst,kind,pos,text,note\n"))
        # the child's one small write tx minted exactly one MORE log row
        self.assertEqual(self._export_logs(), logs_after_register + 1)

    def test_child_noop_without_register(self):
        # no register row for (root, path) -> the child exits 0, writes
        # nothing, mints no log (deregistered-in-flight contract)
        cid = self._make_chain("Gamma", ("a gamma line",))
        out = os.path.join(self._tmp, "nope.csv")
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        proc = subprocess.run(
            [sys.executable, "-m", "qatena.autoexport", self.db_path, cid, out, "csv"],
            capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(self._export_logs(), 0)

    def test_child_main_usage_error(self):
        self.assertEqual(autoexport.main([]), 2)
        self.assertEqual(autoexport.main(["only-one-arg"]), 2)

    # -- reblade fidelity modes (SPEC 3.18) ------------------------------------
    def test_register_fidelity_default_and_validation(self):
        cid = self._make_chain("Alpha", ("first",))
        out = os.path.join(self._tmp, "f.json")
        with self.subTest("omitted -> stored default 'out'"):
            p, e = self._register(cid, out)
            self.assertFalse(e, p)
            self.assertEqual(p["reblade_fidelity"], "out")
            self.assertEqual(self._register_row(cid, out)["reblade_fidelity"], "out")
        with self.subTest("explicit value is stored"):
            out2 = os.path.join(self._tmp, "f2.json")
            p, e = self._register(cid, out2, reblade_fidelity="none")
            self.assertFalse(e, p)
            self.assertEqual(p["reblade_fidelity"], "none")
            self.assertEqual(self._register_row(cid, out2)["reblade_fidelity"], "none")
        with self.subTest("invalid value -> validation_failed, nothing stored"):
            out3 = os.path.join(self._tmp, "f3.json")
            p, e = self._register(cid, out3, reblade_fidelity="sideways")
            self.assertTrue(e)
            self.assertEqual(self._code(p), "validation_failed")
            self.assertIsNone(self._register_row(cid, out3))

    def test_fidelity_modes_file_content(self):
        inside = self._make_chain("Inside", ("i one", "i two"))
        outside = self._make_chain("Outside", ("o one",))
        i1 = self._blade_id(inside, "i one")
        i2 = self._blade_id(inside, "i two")
        o1 = self._blade_id(outside, "o one")
        self.verb("reblade_add", src=inside, dst=o1, kind="see")
        self.verb("reblade_add", src=o1, dst=inside, kind="source")
        self.verb("reblade_add", src=i1, dst=i2, kind="part-of")

        def rows(path):
            doc = json.loads(self._read(path))
            return {(r["src"], r["dst"], r["kind"]) for r in doc["reblades"]}

        p, e = self._register(inside, os.path.join(self._tmp, "m-none.json"),
                              reblade_fidelity="none")
        self.assertFalse(e, p)
        self.assertEqual(rows(os.path.join(self._tmp, "m-none.json")), set())
        self._register(inside, os.path.join(self._tmp, "m-out.json"),
                       reblade_fidelity="out")
        self.assertEqual(rows(os.path.join(self._tmp, "m-out.json")),
                         {(inside, o1, "see"), (i1, i2, "part-of")})
        self._register(inside, os.path.join(self._tmp, "m-in.json"),
                       reblade_fidelity="in")
        self.assertEqual(rows(os.path.join(self._tmp, "m-in.json")),
                         {(o1, inside, "source"), (i1, i2, "part-of")})
        self._register(inside, os.path.join(self._tmp, "m-both.json"),
                       reblade_fidelity="both")
        self.assertEqual(rows(os.path.join(self._tmp, "m-both.json")),
                         {(inside, o1, "see"), (o1, inside, "source"),
                          (i1, i2, "part-of")})
        with self.subTest("md: Inbound block only in in/both modes"):
            self._register(inside, os.path.join(self._tmp, "m-in.md"),
                           emit_format="md", reblade_fidelity="in")
            body = self._read(os.path.join(self._tmp, "m-in.md"))
            self.assertIn("## Inbound", body)
            self.assertIn("- %s [source] -> %s" % (o1, inside), body)
            self._register(inside, os.path.join(self._tmp, "m-out.md"),
                           emit_format="md", reblade_fidelity="out")
            body = self._read(os.path.join(self._tmp, "m-out.md"))
            self.assertNotIn("## Inbound", body)
            self.assertIn("<!-- see -> %s -->" % o1, body)
            self.assertIn("<!-- part-of -> %s -->" % i2, body)

    def test_fidelity_trigger_matrix(self):
        inside = self._make_chain("Inside", ("i one",))
        outside = self._make_chain("Outside", ("o one",))
        o1 = self._blade_id(outside, "o one")

        def fresh(mode, tag):
            out = os.path.join(self._tmp, "t-%s-%s.json" % (mode, tag))
            p, e = self._register(inside, out, reblade_fidelity=mode)
            self.assertFalse(e, p)
            return out

        with self.subTest("none: reblade write touching E does not re-export"):
            out = fresh("none", "a")
            before = self._read(out)
            self._write("reblade_add", src=inside, dst=o1, kind="see")
            self.assertEqual(self._read(out), before)
        with self.subTest("out: own outbound reblade write re-exports"):
            out = fresh("out", "b")
            before = self._read(out)
            self._write("reblade_add", src=inside, dst=o1, kind="supersedes")
            self.assertNotEqual(self._read(out), before)
        with self.subTest("out: external inbound reblade write does not re-export"):
            out = fresh("out", "c")
            before = self._read(out)
            self._write("reblade_add", src=o1, dst=inside, kind="source")
            self.assertEqual(self._read(out), before)
        with self.subTest("in: external inbound reblade write re-exports"):
            out = fresh("in", "d")
            before = self._read(out)
            self._write("reblade_add", src=o1, dst=inside, kind="part-of")
            self.assertNotEqual(self._read(out), before)
        with self.subTest("both: either direction re-exports"):
            out = fresh("both", "e")
            before = self._read(out)
            self._write("reblade_add", src=o1, dst=inside, kind="see")
            self.assertNotEqual(self._read(out), before)

    def test_reregister_mode_change_reexports(self):
        inside = self._make_chain("Inside", ("i one",))
        outside = self._make_chain("Outside", ("o one",))
        o1 = self._blade_id(outside, "o one")
        self.verb("reblade_add", src=inside, dst=o1, kind="see")
        out = os.path.join(self._tmp, "mode.json")
        p, e = self._register(inside, out, reblade_fidelity="none")
        self.assertFalse(e, p)
        self.assertFalse(p["replaced"])
        self.assertEqual(json.loads(self._read(out))["reblades"], [])
        sig_none = self._register_row(inside, out)["last_sig"]
        with self.subTest("mode change is a replacement and re-exports"):
            p, e = self._register(inside, out, reblade_fidelity="out")
            self.assertFalse(e, p)
            self.assertTrue(p["replaced"])
            doc = json.loads(self._read(out))
            self.assertEqual(len(doc["reblades"]), 1)
            self.assertNotEqual(self._register_row(inside, out)["last_sig"], sig_none)
        with self.subTest("omitted on re-register keeps the stored value"):
            p, e = self._register(inside, out)
            self.assertFalse(e, p)
            self.assertFalse(p["replaced"])
            self.assertEqual(p["reblade_fidelity"], "out")
            self.assertEqual(self._register_row(inside, out)["reblade_fidelity"], "out")

    def test_bootstrap_migration_adds_reblade_fidelity(self):
        p = os.path.join(self._tmp, "legacy.db")
        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE autoexport_registers ("
            "root_uuid TEXT(36) NOT NULL, emit_path TEXT NOT NULL, "
            "emit_format TEXT NOT NULL, last_sig TEXT, last_emitted INTEGER, "
            "created INTEGER NOT NULL, PRIMARY KEY (root_uuid, emit_path))")
        conn.execute(
            "INSERT INTO autoexport_registers (root_uuid, emit_path, "
            "emit_format, created) VALUES (?, ?, ?, ?)",
            ("0192f6a0-0000-7000-8000-000000000000", "/x/y.md", "md", 1))
        conn.commit()
        schema.bootstrap(conn)  # migrates in place, idempotently
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(autoexport_registers)")}
        self.assertIn("reblade_fidelity", cols)
        row = conn.execute(
            "SELECT reblade_fidelity FROM autoexport_registers").fetchone()
        self.assertEqual(row["reblade_fidelity"], "out")  # default backfilled
        schema.bootstrap(conn)  # second run: no error, no duplicate column
        n = conn.execute(
            "SELECT COUNT(*) c FROM pragma_table_info('autoexport_registers') "
            "WHERE name = 'reblade_fidelity'").fetchone()["c"]
        self.assertEqual(n, 1)
        conn.close()


# ---------------------------------------------------------------------------
# renderer snapshots (pure — no DB)


SNAP_UUIDS = {
    "arc": "0192f6a0-0000-7abc-8def-000000000001",
    "alpha": "0192f6a0-0000-7abc-8def-000000000002",
    "beta": "0192f6a0-0000-7abc-8def-000000000003",
    "a1": "0192f6a0-0000-7abc-8def-000000000004",
    "a2": "0192f6a0-0000-7abc-8def-000000000005",
    "b1": "0192f6a0-0000-7abc-8def-000000000006",
    "ext1": "0192f6a0-0000-7abc-8def-000000000007",
    "ext2": "0192f6a0-0000-7abc-8def-000000000008",
}
SNAP_ISO = "2026-11-27T10:00:00.000Z"


def _snap_payloads():
    u = SNAP_UUIDS
    intention = {
        "root": {"uuid": u["arc"], "space": "intention", "name": "Arc"},
        "stack": {
            "intentions": [{"id": u["arc"], "name": "Arc"}],
            "chains": [
                {"id": u["alpha"], "name": "Alpha", "intentions": ["Arc"],
                 "blades": [{"id": u["a1"], "pos": 0, "text": "first line"},
                            {"id": u["a2"], "pos": 1, "text": "second line"}]},
                {"id": u["beta"], "name": "Beta", "intentions": ["Arc", "Side"],
                 "blades": [{"id": u["b1"], "pos": 0, "text": "beta blade"}]},
            ],
            "blades": [],
        },
        "reblade_fidelity": "both",
        "reblades": [
            {"src": u["alpha"], "dst": u["beta"], "kind": "see"},
            {"src": u["a1"], "dst": u["ext1"], "kind": "name-ref",
             "note": "exact alias match"},
            {"src": u["ext2"], "dst": u["b1"], "kind": "see"},
        ],
        "counts": {"chains": 2, "blades": 3, "reblades": 3},
    }
    chain = {
        "root": {"uuid": u["alpha"], "space": "chain", "name": "Alpha"},
        "stack": {
            "intentions": [],
            "chains": [
                {"id": u["alpha"], "name": "Alpha", "intentions": ["Arc"],
                 "blades": [{"id": u["a1"], "pos": 0, "text": "first line"},
                            {"id": u["a2"], "pos": 1, "text": "second line"}]},
            ],
            "blades": [],
        },
        "reblade_fidelity": "out",
        "reblades": [
            {"src": u["alpha"], "dst": u["beta"], "kind": "see"},
        ],
        "counts": {"chains": 1, "blades": 2, "reblades": 1},
    }
    blade = {
        "root": {"uuid": u["b1"], "space": "blade", "name": "beta blade"},
        "stack": {
            "intentions": [],
            "chains": [],
            "blades": [{"id": u["b1"], "pos": 0, "text": "beta blade",
                        "chain_id": u["beta"], "chain_name": "Beta"}],
        },
        "reblade_fidelity": "out",
        "reblades": [
            {"src": u["b1"], "dst": u["ext1"], "kind": "name-ref"},
        ],
        "counts": {"chains": 0, "blades": 1, "reblades": 1},
    }
    return intention, chain, blade


class RendererSnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.intention, cls.chain, cls.blade = _snap_payloads()

    # json: rendered == canonical dump of the hand-written expected document
    def test_json_section(self):
        u = SNAP_UUIDS
        expected_doc = {
            "root": {"uuid": u["arc"], "space": "intention", "name": "Arc"},
            "exported_at": SNAP_ISO,
            "reblade_fidelity": "both",
            "reblades": [
                {"src": u["alpha"], "dst": u["beta"], "kind": "see"},
                {"src": u["a1"], "dst": u["ext1"], "kind": "name-ref",
                 "note": "exact alias match"},
                {"src": u["ext2"], "dst": u["b1"], "kind": "see"},
            ],
            "stack": {
                "intentions": [{"id": u["arc"], "name": "Arc"}],
                "chains": [
                    {"id": u["alpha"], "name": "Alpha", "intentions": ["Arc"],
                     "blades": [{"id": u["a1"], "pos": 0, "text": "first line"},
                                {"id": u["a2"], "pos": 1, "text": "second line"}]},
                    {"id": u["beta"], "name": "Beta", "intentions": ["Arc", "Side"],
                     "blades": [{"id": u["b1"], "pos": 0, "text": "beta blade"}]},
                ],
                "blades": [],
            },
        }
        expected = json.dumps(expected_doc, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(autoexport.render(self.intention, "json", SNAP_ISO), expected)

    def test_json_blade(self):
        u = SNAP_UUIDS
        expected_doc = {
            "root": {"uuid": u["b1"], "space": "blade", "name": "beta blade"},
            "exported_at": SNAP_ISO,
            "reblade_fidelity": "out",
            "reblades": [
                {"src": u["b1"], "dst": u["ext1"], "kind": "name-ref"},
            ],
            "stack": {
                "intentions": [],
                "chains": [],
                "blades": [{"id": u["b1"], "pos": 0, "text": "beta blade",
                            "chain_id": u["beta"], "chain_name": "Beta"}],
            },
        }
        expected = json.dumps(expected_doc, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(autoexport.render(self.blade, "json", SNAP_ISO), expected)

    def test_ndjson(self):
        u = SNAP_UUIDS
        self.assertEqual(
            autoexport.render(self.intention, "ndjson", SNAP_ISO),
            '{"space":"intention","uuid":"%s","name":"Arc"}\n'
            '{"space":"chain","uuid":"%s","name":"Alpha"}\n'
            '{"space":"blade","uuid":"%s","name":"Alpha","pos":0,"text":"first line"}\n'
            '{"space":"blade","uuid":"%s","name":"Alpha","pos":1,"text":"second line"}\n'
            '{"space":"chain","uuid":"%s","name":"Beta"}\n'
            '{"space":"blade","uuid":"%s","name":"Beta","pos":0,"text":"beta blade"}\n'
            '{"space":"reblade","src":"%s","dst":"%s","kind":"see"}\n'
            '{"space":"reblade","src":"%s","dst":"%s","kind":"name-ref","note":"exact alias match"}\n'
            '{"space":"reblade","src":"%s","dst":"%s","kind":"see"}\n'
            % (u["arc"], u["alpha"], u["a1"], u["a2"], u["beta"], u["b1"],
               u["alpha"], u["beta"], u["a1"], u["ext1"], u["ext2"], u["b1"]))
        self.assertEqual(
            autoexport.render(self.chain, "ndjson", SNAP_ISO),
            '{"space":"chain","uuid":"%s","name":"Alpha"}\n'
            '{"space":"blade","uuid":"%s","name":"Alpha","pos":0,"text":"first line"}\n'
            '{"space":"blade","uuid":"%s","name":"Alpha","pos":1,"text":"second line"}\n'
            '{"space":"reblade","src":"%s","dst":"%s","kind":"see"}\n'
            % (u["alpha"], u["a1"], u["a2"], u["alpha"], u["beta"]))
        self.assertEqual(
            autoexport.render(self.blade, "ndjson", SNAP_ISO),
            '{"space":"blade","uuid":"%s","name":"Beta","pos":0,"text":"beta blade"}\n'
            '{"space":"reblade","src":"%s","dst":"%s","kind":"name-ref"}\n'
            % (u["b1"], u["b1"], u["ext1"]))

    def test_csv(self):
        u = SNAP_UUIDS
        self.assertEqual(
            autoexport.render(self.intention, "csv", SNAP_ISO),
            "space,uuid,name,src,dst,kind,pos,text,note\n"
            "intention,%s,Arc,,,,,,\n"
            "chain,%s,Alpha,,,,,,\n"
            "blade,%s,Alpha,,,,0,first line,\n"
            "blade,%s,Alpha,,,,1,second line,\n"
            "chain,%s,Beta,,,,,,\n"
            "blade,%s,Beta,,,,0,beta blade,\n"
            "reblade,,,%s,%s,see,,,\n"
            "reblade,,,%s,%s,name-ref,,,exact alias match\n"
            "reblade,,,%s,%s,see,,,\n"
            % (u["arc"], u["alpha"], u["a1"], u["a2"], u["beta"], u["b1"],
               u["alpha"], u["beta"], u["a1"], u["ext1"], u["ext2"], u["b1"]))
        self.assertEqual(
            autoexport.render(self.chain, "csv", SNAP_ISO),
            "space,uuid,name,src,dst,kind,pos,text,note\n"
            "chain,%s,Alpha,,,,,,\n"
            "blade,%s,Alpha,,,,0,first line,\n"
            "blade,%s,Alpha,,,,1,second line,\n"
            "reblade,,,%s,%s,see,,,\n"
            % (u["alpha"], u["a1"], u["a2"], u["alpha"], u["beta"]))
        self.assertEqual(
            autoexport.render(self.blade, "csv", SNAP_ISO),
            "space,uuid,name,src,dst,kind,pos,text,note\n"
            "blade,%s,Beta,,,,0,beta blade,\n"
            "reblade,,,%s,%s,name-ref,,,\n"
            % (u["b1"], u["b1"], u["ext1"]))

    def test_md(self):
        u = SNAP_UUIDS
        self.assertEqual(
            autoexport.render(self.intention, "md", SNAP_ISO),
            "# Arc\n"
            "<!-- uuid: %s -->\n"
            "\n"
            "## Alpha\n"
            "<!-- uuid: %s -->\n"
            "<!-- see -> %s -->\n"
            "*intentions: Arc*\n"
            "- first line <!-- uuid: %s -->\n"
            "<!-- name-ref -> %s -->\n"
            "<!-- note: exact alias match -->\n"
            "- second line <!-- uuid: %s -->\n"
            "\n"
            "## Beta\n"
            "<!-- uuid: %s -->\n"
            "*intentions: Arc, Side*\n"
            "- beta blade <!-- uuid: %s -->\n"
            "\n"
            "## Inbound\n"
            "- %s [see] -> %s\n"
            % (u["arc"], u["alpha"], u["beta"], u["a1"], u["ext1"], u["a2"],
               u["beta"], u["b1"], u["ext2"], u["b1"]))
        # the internal row (Alpha->Beta) renders exactly once, as an outgoing
        # comment under its src owner; it never enters the Inbound block
        body = autoexport.render(self.intention, "md", SNAP_ISO)
        self.assertEqual(body.count("<!-- see -> %s -->" % u["beta"]), 1)
        self.assertNotIn("- %s [" % u["alpha"], body)
        self.assertEqual(
            autoexport.render(self.chain, "md", SNAP_ISO),
            "# Alpha\n"
            "<!-- uuid: %s -->\n"
            "<!-- see -> %s -->\n"
            "*intentions: Arc*\n"
            "- first line <!-- uuid: %s -->\n"
            "- second line <!-- uuid: %s -->\n"
            % (u["alpha"], u["beta"], u["a1"], u["a2"]))
        self.assertEqual(
            autoexport.render(self.blade, "md", SNAP_ISO),
            "# beta blade\n"
            "<!-- uuid: %s -->\n"
            "\n"
            "*chain: Beta*\n"
            "- beta blade <!-- uuid: %s pos: 0 -->\n"
            "<!-- name-ref -> %s -->\n"
            % (u["b1"], u["b1"], u["ext1"]))

    def test_bad_format_rejected(self):
        with self.assertRaises(ValueError):
            autoexport.render(self.chain, "yaml", SNAP_ISO)


# ---------------------------------------------------------------------------
# real stdio server, QATENA_EXPORT_SYNC=1


class AutoexportServerTests(unittest.TestCase):
    DEADLINE = 20

    def setUp(self):
        self._armory = tempfile.mkdtemp(prefix="qatena-ae-armory-")
        self._outdir = tempfile.mkdtemp(prefix="qatena-ae-out-")
        self._proc, self._lines = self._spawn()
        self._next_id = 0

    def tearDown(self):
        if self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self._armory, ignore_errors=True)
        shutil.rmtree(self._outdir, ignore_errors=True)

    def _spawn(self):
        env = dict(os.environ)
        env.pop("QATENA_DB", None)
        env["PYTHONPATH"] = REPO_ROOT
        env["QATENA_ARMORY"] = self._armory
        env["QATENA_EXPORT_SYNC"] = "1"  # test seam: deterministic inline export
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "-m", "qatena"],
            cwd=REPO_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        lines = queue.Queue()

        def _pump():
            try:
                for line in proc.stdout:
                    lines.put(line)
            except Exception as e:  # noqa: BLE001
                lines.put(e)
            lines.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        return proc, lines

    def _readline(self):
        try:
            item = self._lines.get(timeout=self.DEADLINE)
        except queue.Empty:
            self.fail("server produced no line within %ss (hang)" % self.DEADLINE)
        if item is None:
            self.fail("server closed stdout early")
        if isinstance(item, Exception):
            self.fail("stdout read error: %r" % item)
        return item

    def _request(self, method, params=None):
        self._next_id += 1
        msg_id = self._next_id
        self._proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "method": method,
             "params": params or {}}) + "\n")
        self._proc.stdin.flush()
        line = self._readline()
        msg = json.loads(line)
        self.assertEqual(msg.get("id"), msg_id)
        return msg

    def _call_tool(self, name, arguments=None):
        res = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", res)
        return res["result"]

    def _body(self, result):
        self.assertFalse(result["isError"], "tool call failed: %r" % result)
        return json.loads(result["content"][0]["text"])

    def _code(self, result):
        self.assertTrue(result["isError"], "expected isError: %r" % result)
        return json.loads(result["content"][0]["text"])["error"]["code"]

    def _create_vambrace(self):
        body = self._body(self._call_tool("vambrace", {"action": "equip"}))
        key = body["vambrace"]
        self.assertRegex(key, UUID7_RE)
        return key

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_vambrace_tool_schema(self):
        res = self._request("tools/list")
        tools = {t["name"]: t for t in res["result"]["tools"]}
        t = tools["vambrace"]
        props = t["inputSchema"]["properties"]
        self.assertEqual(
            set(props["action"]["enum"]),
            {"equip", "inventory", "saga", "register_autoexport",
             "deregister_autoexport", "furnish"})
        self.assertEqual(t["inputSchema"]["required"], ["action"])
        for prop in ("root", "emit_path", "emit_format", "label", "saga"):
            self.assertIn(prop, props)
        self.assertEqual(
            set(props["emit_format"]["enum"]), {"md", "csv", "json", "ndjson"})
        # equip stays key-less: the key is an OPTIONAL schema property, never required
        self.assertEqual(props.get("vambrace", {}).get("type"), "string")
        self.assertNotIn("vambrace", t["inputSchema"]["required"])

    def test_register_reexport_deregister_over_stdio(self):
        key = self._create_vambrace()
        cid = self._body(
            self._call_tool("chain_put", {"vambrace": key, "name": "Forge"}))["id"]
        self._body(self._call_tool(
            "blade_insert", {"vambrace": key, "chain": cid, "text": "a line"}))
        out = os.path.join(self._outdir, "forge.json")

        with self.subTest("register: synchronous first export over the wire"):
            body = self._body(self._call_tool("vambrace", {
                "action": "register_autoexport", "vambrace": key,
                "root": cid, "emit_path": out, "emit_format": "json"}))
            self.assertTrue(body["exported"])
            self.assertFalse(body["replaced"])
            self.assertIn("a line", self._read(out))

        with self.subTest("changed write re-exports"):
            self._body(self._call_tool(
                "blade_insert",
                {"vambrace": key, "chain": cid, "text": "another line"}))
            self.assertIn("another line", self._read(out))

        with self.subTest("unchanged write does not re-export"):
            before = self._read(out)
            self._body(self._call_tool(
                "chain_put", {"vambrace": key, "name": "Forge"}))
            self.assertEqual(self._read(out), before)

        with self.subTest("deregister: file remains, no further exports"):
            body = self._body(self._call_tool("vambrace", {
                "action": "deregister_autoexport", "vambrace": key,
                "root": cid, "emit_path": out}))
            self.assertTrue(body["removed"])
            self.assertTrue(os.path.exists(out))
            before = self._read(out)
            self._body(self._call_tool(
                "blade_insert",
                {"vambrace": key, "chain": cid, "text": "after dereg"}))
            self.assertEqual(self._read(out), before)

        with self.subTest("bad action still unsupported_action"):
            r = self._call_tool("vambrace", {"action": "destroy"})
            self.assertEqual(self._code(r), "unsupported_action")


if __name__ == "__main__":
    unittest.main()
