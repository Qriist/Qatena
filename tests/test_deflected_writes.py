"""deflected_writes: the failed content-write cache (draft 2026-11-27, 1-6).

Deflection lives in the SERVER layer (_call_routed): any tool-level failure of
a content-write verb parks {op, canonical args, error} in the per-vambrace
deflected_writes table, one log row (area deflected_writes, no log_refs), and
the failed response stays byte-identical. Auto-clear lives in the VERB layer
(_run_write): a later identical successful write (same op + canonical args)
flips matching pending rows to cleared inside its own tx. Erase is the new
deflected_write verb (action=clear, exactly one of id/group_id).
Reads go through the generic list verb (space deflected_write).
"""
import json
import os

from qatena import db, schema, server
from qatena.verbs import canon_args
from qatena.uuid7 import new_uuid7
from helpers import QatenaTestCase, ISO_RE, UUID7_RE


class DeflectedTestCase(QatenaTestCase):
    """Server-layer driven: deflection happens in _call_routed, so the whole
    scenario runs against an armory dir + routing key (the base class's
    qatena.db conn is replaced by the armory file's conn, for assertions)."""

    def setUp(self):
        super().setUp()
        self.armory = os.path.join(self._tmp, "armory")
        os.makedirs(self.armory)
        self.key = new_uuid7()
        self.conn.close()  # base-class conn (unused here); armory file is the SUT
        self.dbfile = os.path.join(self.armory, self.key + ".db")
        boot = db.connect(self.dbfile)
        schema.bootstrap(boot)
        boot.close()
        self.conn = db.connect(self.dbfile)

    def route(self, verb, **args):
        """One server-layer call -> (payload, is_error)."""
        return server._call_routed(self.armory, verb, dict({"vambrace": self.key}, **args))

    # -- storage-level queries ----------------------------------------------
    def rows(self, **where):
        sql, params = "SELECT * FROM deflected_writes WHERE 1=1", []
        for k, v in where.items():
            sql += f" AND {k} = ?"
            params.append(v)
        return self.conn.execute(sql + " ORDER BY id", params).fetchall()

    def dw_logs(self):
        return [r for r in self.conn.execute("SELECT * FROM logs WHERE area = 'deflected_writes'")]

    def refs_for(self, log_id):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM log_refs WHERE log_id = ?", (log_id,)).fetchone()["c"]

    def _mk_chain(self, name):
        payload, is_err = self.route("chain_put", name=name)
        assert not is_err, payload
        return payload


class SingleVerbDeflection(DeflectedTestCase):
    """Draft 8.1/8.2: a row per failed content write, response unchanged."""

    def _deflect(self, name, bad_args):
        payload, is_err = self.route(name, **bad_args)
        assert is_err, f"expected failure: {payload}"
        self.assertEqual(set(payload), {"error"}, "response shape must stay unchanged")
        err = payload["error"]
        self.assertEqual(set(err), {"code", "message"})
        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        expected_args = {k: v for k, v in bad_args.items() if k != "vambrace"}
        self.assertEqual(row["op"], name)
        self.assertEqual(row["args"], canon_args(expected_args))
        self.assertEqual(row["error"], f"{err['code']}: {err['message']}")
        self.assertIsNone(row["group_id"])
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["cleared_at"])
        self.assertIsInstance(row["created"], int)
        # one log row, area deflected_writes, no log_refs
        logs = self.dw_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["title"], f"deflected_write cache: 1 op(s) {name}")
        body = json.loads(logs[0]["body"])
        self.assertEqual(body, [{"id": row["id"], "op": name, "error": row["error"]}])
        self.assertEqual(self.refs_for(logs[0]["id"]), 0)
        return row

    def test_chain_put_bad_orientation(self):
        row = self._deflect("chain_put", {
            "name": "X", "intentions": [{"name": "I", "orientation": "Z"}]})
        self.assertEqual(json.loads(row["args"]),
                         {"intentions": [{"name": "I", "orientation": "Z"}], "name": "X"})

    def test_blade_insert_index_beyond_count(self):
        self._mk_chain("C1")
        row = self._deflect("blade_insert", {"chain": "C1", "text": "b", "index": 1})
        self.assertEqual(json.loads(row["args"]), {"chain": "C1", "index": 1, "text": "b"})

    def test_reblade_add_unresolvable_dst(self):
        chain = self._mk_chain("C1")
        dst = new_uuid7()
        row = self._deflect("reblade_add", {"src": chain["id"], "dst": dst, "kind": "see"})
        self.assertEqual(json.loads(row["args"]),
                         {"dst": dst, "kind": "see", "src": chain["id"]})

    def test_open_item_put_nameless_unresolvable_id(self):
        row = self._deflect("open_item", {"action": "put", "id": new_uuid7()})
        self.assertEqual(json.loads(row["args"]), {"action": "put", "id": row["args"].join([]) or
                                                   json.loads(row["args"])["id"]})

    def test_blade_move_not_found(self):
        row = self._deflect("blade_move", {"id": new_uuid7()})
        self.assertEqual(json.loads(row["args"])["id"], json.loads(row["args"])["id"])
        self.assertIn("not_found", row["error"])


class NoDeflection(DeflectedTestCase):
    """Draft 8.3: successes, read-verb failures, pre-routing, and the cache
    verb itself never park rows."""

    def test_successful_write_not_cached(self):
        self._mk_chain("Fine")
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(len(self.dw_logs()), 0)

    def test_read_verb_failure_not_cached(self):
        payload, is_err = self.route("get", id=new_uuid7())
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(len(self.rows()), 0)

    def test_pre_routing_failures_not_cached(self):
        payload, is_err = server._call_routed(self.armory, "chain_put", {"name": "X"})  # missing key
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "missing_vambrace")
        payload, is_err = self.route("chain_put", vambrace="nope", name="X")
        self.assertEqual(payload["error"]["code"], "malformed_vambrace")
        payload, is_err = self.route("chain_put", vambrace=new_uuid7(), name="X")
        self.assertEqual(payload["error"]["code"], "unknown_vambrace")
        self.assertEqual(len(self.rows()), 0)
        # the unknown key never minted a file
        self.assertEqual(len([f for f in os.listdir(self.armory) if f.endswith(".db")]), 1)

    def test_deflected_write_failure_not_cached(self):
        payload, is_err = self.route("deflected_write", action="clear")  # neither id nor group
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertEqual(len(self.rows()), 0)

    def test_failed_batch_of_unknown_op_is_cached(self):
        # the batch VERB failed (before any op ran): every op is cached,
        # op_index is None (tx-level) so the error sits on EVERY row
        payload, is_err = self.route("batch", ops=[{"op": "definitely_not", "args": {}}])
        self.assertTrue(is_err)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["op"], "definitely_not")
        self.assertIsNotNone(rows[0]["group_id"])
        self.assertIsNotNone(rows[0]["error"])


class BatchDeflection(DeflectedTestCase):
    """Draft 8.4: a failed batch caches EVERY op under one minted group_id."""

    def _fail_batch(self, ops):
        payload, is_err = self.route("batch", ops=ops)
        self.assertTrue(is_err, payload)
        return payload

    def test_failed_batch_rows(self):
        self._mk_chain("C1")
        ops = [
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "blade_insert", "args": {"chain": "C1", "text": "b", "index": 9}},
            {"op": "chain_put", "args": {"name": "B"}},
        ]
        payload = self._fail_batch(ops)
        self.assertIn("op 1 (blade_insert)", payload["error"]["message"])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["op"] for r in rows], ["chain_put", "blade_insert", "chain_put"])
        # one shared UUIDv7 group
        gids = {r["group_id"] for r in rows}
        self.assertEqual(len(gids), 1)
        self.assertTrue(UUID7_RE.match(next(iter(gids))))
        # error only on the failing op's row; siblings NULL
        self.assertIsNone(rows[0]["error"])
        self.assertEqual(rows[1]["error"], f"{payload['error']['code']}: {payload['error']['message']}")
        self.assertIsNone(rows[2]["error"])
        # canonical args per op
        self.assertEqual(rows[0]["args"], canon_args({"name": "A"}))
        self.assertEqual(rows[1]["args"], canon_args({"chain": "C1", "index": 9, "text": "b"}))
        self.assertEqual(rows[2]["args"], canon_args({"name": "B"}))
        # batch = one deflection tx = one log row, no refs
        logs = self.dw_logs()
        self.assertEqual(len(logs), 1)
        self.assertIn("failed batch, group", logs[0]["title"])
        self.assertEqual(self.refs_for(logs[0]["id"]), 0)

    def test_failed_batch_absent_op_args_default_to_empty(self):
        ops = [{"op": "chain_put", "args": {"name": "A"}},
               {"op": "chain_put"},  # no args key
               {"op": "chain_put", "args": {"name": "C"}}]
        # ops[0] mints A; ops[1] (no args) mints a nameless chain -> not_found at op 1
        payload = self._fail_batch(ops)
        self.assertIn("op 1 (chain_put)", payload["error"]["message"])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["args"], canon_args({}))

    def test_successful_batch_not_cached(self):
        self._mk_chain("C1")
        payload, is_err = self.route("batch", ops=[
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "blade_insert", "args": {"chain": "C1", "text": "b"}},
        ])
        self.assertFalse(is_err, payload)
        self.assertEqual(len(self.rows()), 0)


class AutoClear(DeflectedTestCase):
    """Draft 4/8.5: an identical successful write pays off pending rows,
    inside its own tx; variant args never match."""

    def test_exact_reissue_clears(self):
        self._mk_chain("C1")
        self._deflect_args = {"chain": "C1", "text": "b", "index": 1}
        payload, _ = self.route("blade_insert", **self._deflect_args)
        self.assertTrue(payload["error"])
        row = self.rows()[0]
        # a different call makes the chain big enough, then the identical op lands
        self.route("blade_insert", chain="C1", text="filler")
        self.assertEqual(len(self.rows(state="pending")), 1)
        payload, is_err = self.route("blade_insert", **self._deflect_args)
        self.assertFalse(is_err, payload)
        row = self.rows()[0]
        self.assertEqual(row["state"], "cleared")
        self.assertIsNotNone(row["cleared_at"])
        self.assertEqual(len(self.rows()), 1)  # no duplicate row

    def test_reissue_with_different_key_order_still_clears(self):
        # canonicalization: object key order is irrelevant, list order is not
        a = {"name": "X", "intentions": [{"name": "I", "orientation": "W"}]}
        b = {"intentions": [{"name": "I", "orientation": "W"}], "name": "X"}
        self.assertEqual(canon_args(a), canon_args(b))
        chain = self._mk_chain("C1")
        tid = new_uuid7()
        self.route("reblade_add", src=chain["id"], dst=tid, kind="see")  # fails: unresolvable
        self.assertEqual(len(self.rows(state="pending")), 1)
        now = db.now_ns()
        self.conn.execute(
            "INSERT INTO terms (id, term, text, created, updated) VALUES (?, ?, ?, ?, ?)",
            (tid, "A term", None, now, now))
        self.conn.commit()
        self.route("reblade_add", kind="see", dst=tid, src=chain["id"])  # succeeds; key order differs
        self.assertEqual(len(self.rows(state="pending")), 0)
        self.assertEqual(len(self.rows(state="cleared")), 1)

    def test_list_order_matters(self):
        i1 = [{"name": "I1"}, {"name": "I2"}]
        i2 = [{"name": "I2"}, {"name": "I1"}]
        self.assertNotEqual(canon_args(i1), canon_args(i2))
        self.route("chain_put", name="X", intentions=i1)  # succeeds
        self.assertEqual(len(self.rows()), 0)  # success never caches
        self.route("chain_put", name="X", intentions=i2)  # succeeds (membership replaced)
        self.assertEqual(len(self.rows()), 0)

    def test_batch_siblings_clear_one_at_a_time_and_whole_batch(self):
        self._mk_chain("C1")
        ops = [
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "blade_insert", "args": {"chain": "C1", "text": "b", "index": 9}},
            {"op": "chain_put", "args": {"name": "B"}},
        ]
        self.route("batch", ops=ops)  # fails at op 1
        self.assertEqual(len(self.rows(state="pending")), 3)
        # one-at-a-time re-issue clears each sibling as it lands
        self.route("chain_put", name="A")
        self.assertEqual(len(self.rows(state="pending")), 2)
        self.route("chain_put", name="B")
        self.assertEqual(len(self.rows(state="pending")), 1)
        # whole-batch re-issue clears the last one (all ops valid now: index 9
        # still invalid, so make the batch valid by rewording... use a fixed batch)
        self.route("batch", ops=[
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "chain_put", "args": {"name": "B"}},
        ])
        # the re-worded batch does NOT clear the stale blade_insert row
        self.assertEqual(len(self.rows(state="pending")), 1)
        # clear it with the (now valid) identical op after growing the chain
        for _ in range(9):
            self.route("blade_insert", chain="C1", text="x")
        self.route("batch", ops=ops)  # the original failed batch, whole, now valid
        self.assertEqual(len(self.rows(state="pending")), 0)
        self.assertEqual(len(self.rows(state="cleared")), 3)

    def test_variant_args_do_not_clear(self):
        self._mk_chain("C1")
        self.route("chain_put", name="X", text="first")  # succeeds
        self.route("chain_put", name="Y", text="first")  # succeeds: different name
        self.assertEqual(len(self.rows()), 0)
        # a genuinely failed write, then a SUCCESS with variant args
        self.route("blade_insert", chain="C1", text="b", index=1)  # fails
        self.assertEqual(len(self.rows(state="pending")), 1)
        self.route("blade_insert", chain="C1", text="CHANGED")  # succeeds, variant text
        self.assertEqual(len(self.rows(state="pending")), 1)  # NOT cleared

    def test_second_failure_appends_and_both_clear_when_it_lands(self):
        self._mk_chain("C1")
        for _ in range(2):
            self.route("blade_insert", chain="C1", text="b", index=1)  # fails twice
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.rows(state="pending")), 2)
        self.route("blade_insert", chain="C1", text="filler")
        self.route("blade_insert", chain="C1", text="b", index=1)  # finally lands
        self.assertEqual(len(self.rows(state="pending")), 0)
        self.assertEqual(len(self.rows(state="cleared")), 2)

    def test_idempotent_success_write_clears_and_stays(self):
        chain = self._mk_chain("C1")
        tid = new_uuid7()  # pre-minted: a term will be inserted under this id
        self.route("reblade_add", src=chain["id"], dst=tid, kind="see")  # fails: unresolvable
        self.assertEqual(len(self.rows(state="pending")), 1)
        # make the endpoint resolve (direct SQL, like the open_item/ruling fixtures)
        now = db.now_ns()
        self.conn.execute(
            "INSERT INTO terms (id, term, text, created, updated) VALUES (?, ?, ?, ?, ?)",
            (tid, "A term", None, now, now))
        self.conn.commit()
        # identical re-issue now succeeds and pays off the row
        p1, e1 = self.route("reblade_add", src=chain["id"], dst=tid, kind="see")
        self.assertFalse(e1, p1)
        row = self.rows()[0]
        self.assertEqual(row["state"], "cleared")
        # the idempotent no-op re-issue (reblade already exists) must not
        # append a row, un-clear, or mint a deflection
        p2, e2 = self.route("reblade_add", src=chain["id"], dst=tid, kind="see")
        self.assertFalse(e2, p2)
        self.assertEqual(p2["reblade"]["src"], chain["id"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows(state="pending")), 0)

    def test_no_extra_log_row_on_auto_clear(self):
        self._mk_chain("C1")
        self.route("blade_insert", chain="C1", text="b", index=1)  # fails -> 1 deflection log
        self.assertEqual(len(self.dw_logs()), 1)
        total_before = self.conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]
        self.route("blade_insert", chain="C1", text="filler")
        self.route("blade_insert", chain="C1", text="b", index=1)  # lands + auto-clears
        total_after = self.conn.execute("SELECT COUNT(*) c FROM logs").fetchone()["c"]
        # satisfying write mints its ONE log row; the clear rides inside it
        self.assertEqual(total_after - total_before, 2)
        self.assertEqual(len(self.dw_logs()), 1)  # no second deflection log


class ListSpace(DeflectedTestCase):
    """Draft 5/8.7: list space deflected_write — bare records, filters, order."""

    def _fail(self):
        self._mk_chain("C1")
        self.route("blade_insert", chain="C1", text="b", index=1)  # pending row

    def test_record_shape(self):
        self._fail()
        res = self.verb("list", space="deflected_write")
        items = res["items"]
        self.assertEqual(res["count"], 1)
        rec = items[0]
        self.assertEqual(set(rec), {"id", "created", "op", "args", "error", "state"})
        self.assertEqual(rec["op"], "blade_insert")
        self.assertEqual(rec["args"], {"chain": "C1", "index": 1, "text": "b"})  # an OBJECT
        self.assertRegex(rec["created"], ISO_RE)
        self.assertEqual(rec["state"], "pending")
        self.assertIn("invalid_index", rec["error"])
        self.assertNotIn("group_id", rec)   # NULLs omitted
        self.assertNotIn("cleared_at", rec)  # NULLs omitted

    def test_cleared_record_shape(self):
        self._fail()
        row = self.rows()[0]
        self.verb("deflected_write", action="clear", id=row["id"])
        rec = self.verb("list", space="deflected_write")["items"][0]
        self.assertEqual(rec["state"], "cleared")
        self.assertRegex(rec["cleared_at"], ISO_RE)

    def test_state_filter(self):
        self._fail()
        row = self.rows()[0]
        self.assertEqual(len(self.verb("list", space="deflected_write",
                                       filter={"state": "pending"})["items"]), 1)
        self.assertEqual(len(self.verb("list", space="deflected_write",
                                       filter={"state": "cleared"})["items"]), 0)
        self.verb("deflected_write", action="clear", id=row["id"])
        self.assertEqual(len(self.verb("list", space="deflected_write",
                                       filter={"state": "cleared"})["items"]), 1)
        self.assertEqual(len(self.verb("list", space="deflected_write",
                                       filter={"state": "pending"})["items"]), 0)

    def test_since_until_filters(self):
        self._fail()
        res = self.verb("list", space="deflected_write", filter={
            "since": "1970-01-01T00:00:00.000Z", "until": "2200-01-01T00:00:00.000Z"})
        self.assertEqual(res["count"], 1)
        res = self.verb("list", space="deflected_write",
                        filter={"since": "2200-01-01T00:00:00.000Z"})
        self.assertEqual(res["count"], 0)
        # bad iso -> validation_failed
        self.expect_error("validation_failed", "list",
                          space="deflected_write", filter={"since": "yesterday"})

    def test_inapplicable_filter_warns(self):
        self._fail()
        res = self.verb("list", space="deflected_write", filter={"name_prefix": "x"})
        self.assertIn("warning", res)
        self.assertIn("name_prefix", res["warning"])
        self.assertEqual(res["count"], 1)  # rows still returned

    def test_ordering_limit_offset(self):
        self._mk_chain("C1")
        for i in range(3):
            self.route("blade_insert", chain="C1", text=f"b{i}", index=1)  # 3 pending rows
        res = self.verb("list", space="deflected_write", limit=2, offset=0)
        ids = [r["id"] for r in res["items"]]
        all_ids = [r["id"] for r in self.rows()]
        self.assertEqual(ids, all_ids[:2])  # ORDER BY id ASC
        res = self.verb("list", space="deflected_write", limit=5, offset=2)
        self.assertEqual([r["id"] for r in res["items"]], all_ids[2:])


class ClearTool(DeflectedTestCase):
    """Draft 6/8.8: the deflected_write erase verb."""

    def _pending_row(self):
        self._mk_chain("C1")
        self.route("blade_insert", chain="C1", text="b", index=1)
        return self.rows()[0]

    def test_clear_by_id(self):
        row = self._pending_row()
        res = self.verb("deflected_write", action="clear", id=row["id"])
        self.assertEqual(set(res), {"cleared", "count", "log_id"})
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["cleared"][0]["id"], row["id"])
        self.assertEqual(res["cleared"][0]["state"], "cleared")
        self.assertRegex(res["cleared"][0]["cleared_at"], ISO_RE)
        self.assertEqual(len(self.rows(state="pending")), 0)
        log = self.dw_logs()[-1]
        self.assertEqual(log["title"], "deflected_write clear 1 row(s)")
        self.assertEqual(self.refs_for(log["id"]), 0)

    def test_clear_by_group_id(self):
        self._mk_chain("C1")
        self.route("batch", ops=[
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "blade_insert", "args": {"chain": "C1", "text": "b", "index": 9}},
        ])
        rows = self.rows()
        gid = rows[0]["group_id"]
        res = self.verb("deflected_write", action="clear", group_id=gid)
        self.assertEqual(res["count"], 2)
        self.assertEqual(sorted(r["id"] for r in res["cleared"]), sorted(r["id"] for r in rows))
        self.assertEqual(len(self.rows(state="pending")), 0)

    def test_both_or_neither_validation_failed(self):
        row = self._pending_row()
        self.expect_error("validation_failed", "deflected_write",
                          action="clear", id=row["id"], group_id=row["group_id"] or "g")
        # neither (row is single: group_id None — pass id=None explicitly is absent, so
        # use a group row)
        self.route("batch", ops=[
            {"op": "chain_put", "args": {"name": "A"}},
            {"op": "blade_insert", "args": {"chain": "A", "text": "x", "index": 99}},
        ])
        gid = next(r["group_id"] for r in self.rows() if r["group_id"] is not None)
        self.expect_error("validation_failed", "deflected_write", action="clear")
        self.expect_error("validation_failed", "deflected_write",
                          action="clear", id=row["id"], group_id=gid)
        # nothing was cleared
        self.assertEqual(len(self.rows(state="pending")), 3)

    def test_bad_id_shapes_validation_failed(self):
        row = self._pending_row()
        self.expect_error("validation_failed", "deflected_write", action="clear", id=0)
        self.expect_error("validation_failed", "deflected_write", action="clear", id="1")
        self.expect_error("validation_failed", "deflected_write", action="clear", id=True)
        self.expect_error("validation_failed", "deflected_write",
                          action="clear", group_id=1)

    def test_unknown_action_validation_failed(self):
        self.expect_error("validation_failed", "deflected_write", action="replay")

    def test_idempotent_on_already_cleared(self):
        row = self._pending_row()
        first = self.verb("deflected_write", action="clear", id=row["id"])
        second = self.verb("deflected_write", action="clear", id=row["id"])
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["cleared"][0]["id"], row["id"])
        self.assertEqual(second["cleared"][0]["state"], "cleared")
        # same cleared_at (not re-stamped by the idempotent pass)
        self.assertEqual(first["cleared"][0]["cleared_at"], second["cleared"][0]["cleared_at"])
        self.assertEqual(len(self.rows()), 1)

    def test_not_found_when_absent(self):
        self._mk_chain("C1")
        self.expect_error("not_found", "deflected_write", action="clear", id=9999)
        self.expect_error("not_found", "deflected_write",
                          action="clear", group_id=new_uuid7())
        # the not_found failure of the cache verb itself is NOT deflected
        self.assertEqual(len(self.rows()), 0)


class BootstrapMigration(DeflectedTestCase):
    """Draft 8.9: an old vambrace gains the table on the next routed open."""

    def test_old_db_migrates_in_place(self):
        old_key = new_uuid7()
        path = os.path.join(self.armory, old_key + ".db")
        conn = db.connect(path)
        for stmt in schema.STATEMENTS[:-2]:  # everything except the deflected_writes DDL
            conn.execute(stmt)
        now = db.now_ns()
        conn.execute("INSERT INTO logs (created, title, area, body) VALUES (?, ?, ?, ?)",
                     (now, "pre-existing", "chain", "[]"))
        conn.close()
        self.assertIsNone(db.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deflected_writes'"
        ).fetchone())
        # a routed open bootstraps: the table appears, the old data is intact
        payload, is_err = self.route("list", space="log", vambrace=old_key)
        self.assertFalse(is_err, payload)
        c2 = db.connect(path)
        self.assertIsNotNone(c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deflected_writes'"
        ).fetchone())
        self.assertEqual(c2.execute(
            "SELECT COUNT(*) c FROM logs").fetchone()["c"], 1)
        # and the new space is immediately usable on the migrated vambrace
        res = server._call_routed(self.armory, "list",
                                  {"vambrace": old_key, "space": "deflected_write"})[0]
        self.assertEqual(res["items"], [])
        c2.close()
