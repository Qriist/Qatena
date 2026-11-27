"""Split / merge / delete policies, batch atomicity, system-minted logs
(SPEC 1.4, 1.5, 1.7, 3.2-3.4, 3.9)."""
from qatena.uuid7 import new_uuid7

from helpers import QatenaTestCase, REF_OPS, ISO_RE, UUID_RE


def insert(case, chain, text, index=None):
    args = {"chain": chain, "text": text}
    if index is not None:
        args["index"] = index
    return case.verb("blade_insert", **args)


class Split(QatenaTestCase):
    def test_split_blades_and_reblades_travel(self):
        sid = self.verb("chain_put", name="Source")["id"]
        b1 = insert(self, sid, "first line")["blade"]["id"]
        b2 = insert(self, sid, "second line")["blade"]["id"]
        b3 = insert(self, sid, "third line")["blade"]["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=b1, dst=b3, kind="see")      # src stays, dst travels
        self.verb("reblade_add", src=tid, dst=b2, kind="source")  # dst travels
        before = self.pkeys()
        res = self.verb("chain_split", source=sid, blades=[b3, b2], name="Offcut")
        nid = res["new"]["id"]
        self.assertRegex(nid, UUID_RE)
        self.assertNotEqual(nid, sid)
        self.assertIn("log_id", res)
        # new chain: blades in listed order; source: remainder in original order
        self.assertEqual([b["id"] for b in res["new"]["blades"]], [b3, b2])
        self.assertEqual([b["id"] for b in res["source"]["blades"]], [b1])
        self.assertEqual(res["source"]["id"], sid)
        self.assertEqual(res["new"]["name"], "Offcut")
        # storage agrees with the composites
        self.assertEqual(self.positions(sid), [b1])
        self.assertEqual(self.positions(nid), [b3, b2])
        self.assert_gapless(sid)
        self.assert_gapless(nid)
        # blade UUIDs and their reblades travel intact (same rows)
        self.assertEqual(self.pkeys(), before)
        self.assertEqual(self.verb("get", id=nid)["name"], "Offcut")
        self.assertEqual([b["id"] for b in self.verb("get", id=sid)["blades"]], [b1])
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops[nid], "created")
        self.assertEqual(ops[b2], "modified")
        self.assertEqual(ops[b3], "modified")
        self.assertNotIn(b1, ops)

    def test_split_sections_option_upserted(self):
        sid = self.verb("chain_put", name="Source")["id"]
        b = insert(self, sid, "first line")["blade"]["id"]
        res = self.verb("chain_split", source=sid, blades=[b], name="Offcut",
                        intentions=[{"name": "Core", "orientation": "W"}])
        self.assertEqual(res["new"]["intentions"], ["Core"])
        self.assertEqual(self.section_orientation("Core"), "W")
        self.assertEqual(res["source"]["intentions"], [])

    def test_split_unknown_blade_not_found_nothing_persisted(self):
        sid = self.verb("chain_put", name="Source")["id"]
        insert(self, sid, "first line")
        chains, logs = len(self.chain_ids()), self.log_count()
        self.expect_error("not_found", "chain_split",
                          source=sid, blades=[new_uuid7()], name="Offcut")
        self.assertEqual(len(self.chain_ids()), chains)
        self.assertEqual(self.log_count(), logs)

    def test_split_foreign_blade_validation_failed(self):
        s1 = self.verb("chain_put", name="One")["id"]
        s2 = self.verb("chain_put", name="Two")["id"]
        b = insert(self, s2, "first line")["blade"]["id"]
        self.expect_error("validation_failed", "chain_split",
                          source=s1, blades=[b], name="X")

    def test_split_blade_listed_twice_validation_failed(self):
        sid = self.verb("chain_put", name="Source")["id"]
        b = insert(self, sid, "first line")["blade"]["id"]
        self.expect_error("validation_failed", "chain_split",
                          source=sid, blades=[b, b], name="X")

    def test_split_name_taken_duplicate_name(self):
        self.verb("chain_put", name="Offcut")
        sid = self.verb("chain_put", name="Source")["id"]
        b = insert(self, sid, "first line")["blade"]["id"]
        self.expect_error("duplicate_name", "chain_split",
                          source=sid, blades=[b], name="offcut")  # case-insensitive

    def test_split_blank_blades_validation_failed(self):
        sid = self.verb("chain_put", name="Source")["id"]
        self.expect_error("validation_failed", "chain_split",
                          source=sid, blades=[], name="X")


class Merge(QatenaTestCase):
    def test_merge_append_union_retargests(self):
        vid = self.verb("chain_put", name="Main",
                        intentions=[{"name": "Core"}])["id"]
        did = self.verb("chain_put", name="Fold",
                        intentions=[{"name": "Extra"}, {"name": "Core"}])["id"]
        v1 = insert(self, vid, "first line")["blade"]["id"]
        f1 = insert(self, did, "second line")["blade"]["id"]
        f2 = insert(self, did, "third line")["blade"]["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=tid, dst=did, kind="source")  # aimed at dissolved
        self.verb("reblade_add", src=did, dst=tid, kind="see")     # dissolved's outgoing
        res = self.verb("chain_merge", into=vid, fold=did)
        self.assertIn("log_id", res)
        # blades appended in the dissolved chain's order
        self.assertEqual([b["id"] for b in res["survivor"]["blades"]], [v1, f1, f2])
        self.assertEqual(self.positions(vid), [v1, f1, f2])
        self.assert_gapless(vid)
        # intention memberships unioned
        self.assertEqual(self.section_names(vid), ["Core", "Extra"])
        # reblade aimed at the dissolved header is retargeted onto the survivor
        self.assertEqual(len(res["retargeted"]), 1)
        self.assertEqual((res["retargeted"][0]["src"], res["retargeted"][0]["dst"],
                          res["retargeted"][0]["kind"]), (tid, vid, "source"))
        edges = self.verb("reblades_of", id=vid, direction="in")["edges"]
        self.assertIn((tid, vid, "source"),
                      {(e["src"], e["dst"], e["kind"]) for e in edges})
        # dissolved header stops resolving; its reblades are gone
        self.expect_error("not_found", "get", id=did)
        self.assertEqual(self.pkeys(src_uuid=did), set())
        self.assertEqual(self.pkeys(dst_uuid=did), set())
        # ...but its log_refs trace survives in history
        h = self.verb("history", id=did)
        self.assertEqual(h["trace_level"], "coarse")
        self.assertTrue(any(e["op"] == "deleted" for e in h["entries"]))
        self.assertTrue(any(e["title"].startswith("chain_merge") for e in h["entries"]))
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops[vid], "modified")
        self.assertEqual(ops[did], "deleted")
        self.assertEqual(ops[f1], "modified")

    def test_merge_dedupe_when_target_reblade_exists(self):
        vid = self.verb("chain_put", name="Main")["id"]
        did = self.verb("chain_put", name="Fold")["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=tid, dst=vid, kind="source")  # already exists
        self.verb("reblade_add", src=tid, dst=did, kind="source")  # would clash
        res = self.verb("chain_merge", into=vid, fold=did)
        self.assertEqual(res["retargeted"], [])  # deduped: deleted, not retargeted
        self.assertEqual(self.pkeys(src_uuid=tid), {(tid, vid, "source")})

    def test_merge_into_self_validation_failed(self):
        vid = self.verb("chain_put", name="Main")["id"]
        self.expect_error("validation_failed", "chain_merge", into=vid, fold=vid)

    def test_merge_fold_not_found(self):
        vid = self.verb("chain_put", name="Main")["id"]
        self.expect_error("not_found", "chain_merge", into=vid, fold=new_uuid7())


class ChainDelete(QatenaTestCase):
    def test_delete_cascades_blades_and_reports_incoming(self):
        cid = self.verb("chain_put", name="Doomed")["id"]
        b = insert(self, cid, "first line")["blade"]["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=cid, dst=tid, kind="see")      # outgoing
        self.verb("reblade_add", src=tid, dst=cid, kind="source")   # incoming
        res = self.verb("chain_delete", id=cid)
        self.assertIn("log_id", res)
        self.assertEqual(self.chain_ids("Doomed"), [])
        blades = self.conn.execute(
            "SELECT COUNT(*) c FROM chain_blades WHERE chain_id=?", (cid,)).fetchone()["c"]
        self.assertEqual(blades, 0)
        # outgoing cascade-deleted; incoming preserved and reported
        self.assertEqual(self.pkeys(), {(tid, cid, "source")})
        self.assertEqual(len(res["incoming"]), 1)
        self.assertEqual(res["incoming"][0]["src"], tid)
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops, {cid: "deleted", b: "deleted"})

    def test_delete_by_name_case_insensitive(self):
        self.verb("chain_put", name="Doomed")
        self.verb("chain_delete", name="doomed")
        self.assertEqual(self.chain_ids(), [])

    def test_delete_not_found(self):
        self.expect_error("not_found", "chain_delete", id=new_uuid7())
        self.expect_error("not_found", "chain_delete", name="Missing")


class Batch(QatenaTestCase):
    def test_ops_in_order_later_observes_earlier_incl_autolink(self):
        res = self.verb("batch", ops=[
            {"op": "chain_put", "args": {"name": "Anvil"}},
            {"op": "blade_insert", "args": {"chain": "Anvil",
                                            "text": "the anvil glows"}},
        ])
        self.assertIn("log_id", res)
        self.assertEqual(len(res["ops"]), 2)
        r1, r2 = res["ops"]
        self.assertEqual(r1["log_id"], res["log_id"])
        self.assertEqual(r2["log_id"], res["log_id"])
        cid = r1["id"]
        self.assertEqual(r2["blade"]["chain_id"], cid)
        # op 2 auto-links against op 1's creation
        self.assertEqual(r2["minted"],
                         [{"src": r2["blade"]["id"], "dst": cid, "kind": "name-ref"}])
        self.assertEqual(self.verb("get", id=cid)["name"], "Anvil")

    def test_failing_op_rolls_back_whole_batch(self):
        self.verb("chain_put", name="Taken")
        vid = self.verb("chain_put", name="Voyager")["id"]
        chains = len(self.chain_ids())
        blades = self.conn.execute(
            "SELECT COUNT(*) c FROM chain_blades").fetchone()["c"]
        logs, refs = self.log_count(), len(self.ref_ops())
        self.expect_error("duplicate_name", "batch", ops=[
            {"op": "chain_put", "args": {"name": "FreshBatch"}},
            {"op": "blade_insert", "args": {"chain": "FreshBatch",
                                            "text": "first line"}},
            # rename to a case variant of a taken name -> duplicate_name
            {"op": "chain_put", "args": {"id": vid, "name": "taken"}},
        ])
        self.assertEqual(len(self.chain_ids()), chains)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM chain_blades").fetchone()["c"], blades)
        self.assertEqual(self.log_count(), logs)
        self.assertEqual(len(self.ref_ops()), refs)
        self.assertEqual(self.chain_ids("FreshBatch"), [])  # op 1 rolled back
        self.assertEqual(self.chain_ids("Voyager"), [vid])

    def test_three_ops_all_results_single_log_id(self):
        tid = self.insert_term("anvil")
        cid = self.verb("chain_put", name="Alpha")["id"]
        bid = insert(self, cid, "first line")["blade"]["id"]
        self.verb("reblade_add", src=cid, dst=tid, kind="see")
        res = self.verb("batch", ops=[
            {"op": "blade_edit", "args": {"id": bid, "text": "first line edited"}},
            {"op": "blade_move", "args": {"id": bid, "index": 0}},
            {"op": "reblade_remove", "args": {"src": cid, "dst": tid, "kind": "see"}},
        ])
        self.assertEqual(len(res["ops"]), 3)
        for o in res["ops"]:
            self.assertEqual(o["log_id"], res["log_id"])
        self.assertEqual(self.verb("get", id=bid)["text"], "first line edited")
        self.assertEqual(self.pkeys(), set())

    def test_unknown_op_not_found(self):
        self.expect_error("not_found", "batch",
                          ops=[{"op": "get", "args": {"id": "x"}}])

    def test_empty_ops_validation_failed(self):
        self.expect_error("validation_failed", "batch", ops=[])

    def test_op_error_wrapped_with_index_and_rolled_back(self):
        exc = self.expect_error("not_found", "batch", ops=[
            {"op": "chain_put", "args": {"name": "OK"}},
            {"op": "chain_delete", "args": {"name": "missing"}},
        ])
        self.assertIn("op 1", exc.message)
        self.assertEqual(self.chain_ids("OK"), [])  # nothing persisted


class SystemLogs(QatenaTestCase):
    def test_each_write_mints_exactly_one_log(self):
        before = self.log_count()
        self.verb("chain_put", name="Alpha")
        self.assertEqual(self.log_count(), before + 1)
        self.verb("blade_insert", chain="Alpha", text="first line")
        self.assertEqual(self.log_count(), before + 2)
        self.verb("reblade_add", src=self.chain_ids("Alpha")[0],
                  dst=self.insert_term("anvil"), kind="see")
        self.assertEqual(self.log_count(), before + 3)

    def test_titles_start_with_verb_name(self):
        tid = self.insert_term("anvil")
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.verb("reblade_add", src=cid, dst=tid, kind="see")
        bid = insert(self, cid, "first line")["blade"]["id"]
        self.verb("blade_edit", id=bid, text="second line")
        titles = [r["title"] for r in self.conn.execute(
            "SELECT title FROM logs ORDER BY id")]
        self.assertEqual(len(titles), 4)
        self.assertTrue(titles[0].startswith("chain_put"))
        self.assertTrue(titles[1].startswith("reblade_add"))
        self.assertTrue(titles[2].startswith("blade_insert"))
        self.assertTrue(titles[3].startswith("blade_edit"))

    def test_log_ids_strictly_increasing(self):
        ids = [self.verb("chain_put", name="Chain %d" % i)["log_id"] for i in range(3)]
        self.assertTrue(all(a < b for a, b in zip(ids, ids[1:])))

    def test_reads_mint_zero_logs(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        bid = insert(self, cid, "hello world")["blade"]["id"]
        n = self.log_count()
        self.verb("get", id=cid)
        self.verb("get", id=bid)
        self.verb("search", probes=["hello"])
        self.verb("list", space="chain")
        self.verb("reblades_of", id=cid)
        self.verb("follow", **{"from": cid})
        self.verb("history", id=cid)
        self.assertEqual(self.log_count(), n)

    def test_ref_ops_all_valid_and_each_write_has_refs(self):
        tid = self.insert_term("anvil")
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.verb("blade_insert", chain=cid, text="first line")
        self.verb("reblade_add", src=cid, dst=tid, kind="see")
        for _, _, op in self.ref_ops():
            self.assertIn(op, REF_OPS)
        logs_with_refs = {r[0] for r in self.ref_ops()}
        self.assertEqual(len(logs_with_refs), 3)

    def test_log_row_shape_and_area(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        res = self.verb("list", space="log", filter={"uuid": cid})
        self.assertEqual(res["count"], 1)
        item = res["items"][0]
        self.assertIsInstance(item["id"], int)
        self.assertRegex(item["created"], ISO_RE)
        self.assertEqual(item["area"], "chain")
        self.assertTrue(item["title"].startswith("chain_put"))
        import json as _json
        _json.loads(item["body"])  # canonical JSON
