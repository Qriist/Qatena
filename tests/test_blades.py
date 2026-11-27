"""Blades: insert / edit / move / delete, gapless order, auto-linking (SPEC 1.6, 3.5-3.8)."""
from qatena.uuid7 import new_uuid7

from helpers import QatenaTestCase, UUID_RE


def insert(case, chain, text, index=None):
    args = {"chain": chain, "text": text}
    if index is not None:
        args["index"] = index
    return case.verb("blade_insert", **args)


class Insert(QatenaTestCase):
    def test_default_index_appends_gapless(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        r1 = insert(self, cid, "first line")
        r2 = insert(self, cid, "second line")
        self.assertEqual((r1["blade"]["pos"], r2["blade"]["pos"]), (0, 1))
        self.assertEqual(r1["blade"]["chain_id"], cid)
        self.assertRegex(r1["blade"]["id"], UUID_RE)
        self.assertEqual(self.positions(cid), [r1["blade"]["id"], r2["blade"]["id"]])
        self.assert_gapless(cid)
        self.assertEqual(r1["minted"], [])
        self.assertEqual(r1["candidates"], [])

    def test_insert_at_0_middle_end(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        a = insert(self, cid, "first line")["blade"]
        c = insert(self, cid, "third line")["blade"]
        b = insert(self, cid, "second line", index=1)["blade"]
        self.assertEqual(b["pos"], 1)
        self.assertEqual(self.positions(cid), [a["id"], b["id"], c["id"]])
        self.assert_gapless(cid)
        # composite reflects the same order
        g = self.verb("get", id=cid)
        self.assertEqual([x["id"] for x in g["blades"]], [a["id"], b["id"], c["id"]])

    def test_insert_index_above_count_invalid_nothing_persisted(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        a = insert(self, cid, "first line")["blade"]["id"]
        b = insert(self, cid, "second line")["blade"]["id"]
        logs = self.log_count()
        self.expect_error("invalid_index", "blade_insert",
                          chain=cid, text="third line", index=3)
        self.assertEqual(self.positions(cid), [a, b])
        self.assert_gapless(cid)
        self.assertEqual(self.log_count(), logs)

    def test_insert_negative_index_validation_failed(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.expect_error("validation_failed", "blade_insert",
                          chain=cid, text="x", index=-1)

    def test_insert_unknown_chain_not_found(self):
        self.expect_error("not_found", "blade_insert", chain="Missing", text="x")


class Edit(QatenaTestCase):
    def test_edit_replaces_text_and_reports_pos(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        bid = insert(self, cid, "first line")["blade"]["id"]
        res = self.verb("blade_edit", id=bid, text="first line edited")
        self.assertEqual(res["blade"]["id"], bid)
        self.assertEqual(res["blade"]["text"], "first line edited")
        self.assertEqual(res["blade"]["pos"], 0)
        self.assertEqual(res["blade"]["chain_id"], cid)
        self.assertIn("log_id", res)
        self.assertEqual(res["deleted"], [])  # SPEC 1.6: both lists in the response
        self.assertEqual(self.verb("get", id=bid)["text"], "first line edited")
        self.assert_gapless(cid)

    def test_edit_unknown_not_found(self):
        self.expect_error("not_found", "blade_edit", id=new_uuid7(), text="x")

    def test_edit_missing_text_validation_failed(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        bid = insert(self, cid, "first line")["blade"]["id"]
        self.expect_error("validation_failed", "blade_edit", id=bid)

    def test_edit_reconciliation_deleted_list_in_response_SPEC_1_6(self):
        # SPEC 1.6: reconciliation returns BOTH lists — stale refs as `deleted`
        # reblade dicts (same shape as `minted`), new refs as `minted`.
        cid = self.verb("chain_put", name="Alpha")["id"]
        tid = self.insert_term("anvil")
        bid = insert(self, cid, "the anvil glows")["blade"]["id"]
        self.assertEqual(self.pkeys(), {(bid, tid, "name-ref")})
        res = self.verb("blade_edit", id=bid, text="the hammer glows")
        self.assertEqual(res["deleted"],
                         [{"src": bid, "dst": tid, "kind": "name-ref"}])
        self.assertEqual(res["minted"], [])
        self.assertEqual(self.pkeys(), set())
        res = self.verb("blade_edit", id=bid, text="anvil again")
        self.assertEqual(res["deleted"], [])
        self.assertEqual(res["minted"],
                         [{"src": bid, "dst": tid, "kind": "name-ref"}])


class Move(QatenaTestCase):
    def test_move_within_chain(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        a = insert(self, cid, "first line")["blade"]["id"]
        b = insert(self, cid, "second line")["blade"]["id"]
        c = insert(self, cid, "third line")["blade"]["id"]
        # index is measured after removal: [a, b, c] -> move c to 0 -> [c, a, b]
        res = self.verb("blade_move", id=c, index=0)
        self.assertEqual(res["blade"]["id"], c)
        self.assertEqual(res["blade"]["pos"], 0)
        self.assertEqual(res["blade"]["chain_id"], cid)
        self.assertEqual(self.positions(cid), [c, a, b])
        self.assert_gapless(cid)

    def test_move_across_chains_gapless_both_uuid_and_reblades_stable(self):
        c1 = self.verb("chain_put", name="Alpha")["id"]
        c2 = self.verb("chain_put", name="Beta")["id"]
        a = insert(self, c1, "first line")["blade"]["id"]
        m = insert(self, c1, "second line")["blade"]["id"]
        b = insert(self, c2, "third line")["blade"]["id"]
        self.verb("reblade_add", src=a, dst=b, kind="see")
        before = self.pkeys()
        res = self.verb("blade_move", id=m, to_chain=c2, index=0)
        self.assertEqual(res["blade"]["chain_id"], c2)
        self.assertEqual(res["blade"]["pos"], 0)
        self.assertEqual(self.positions(c1), [a])
        self.assertEqual(self.positions(c2), [m, b])
        self.assert_gapless(c1)
        self.assert_gapless(c2)
        # blade UUID and its reblades are unchanged
        self.assertEqual(self.pkeys(), before)

    def test_move_invalid_index_nothing_persisted(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        a = insert(self, cid, "first line")["blade"]["id"]
        b = insert(self, cid, "second line")["blade"]["id"]
        logs = self.log_count()
        # after removal the chain has 1 blade: valid indices 0..1
        self.expect_error("invalid_index", "blade_move", id=a, index=2)
        self.assertEqual(self.positions(cid), [a, b])
        self.assertEqual(self.log_count(), logs)

    def test_move_unknown_not_found(self):
        self.expect_error("not_found", "blade_move", id=new_uuid7())


class Delete(QatenaTestCase):
    def test_delete_gapless_outgoing_cascade_incoming_reported(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        a = insert(self, cid, "first line")["blade"]["id"]
        b = insert(self, cid, "second line")["blade"]["id"]
        c = insert(self, cid, "third line")["blade"]["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=a, dst=b, kind="see")       # outgoing from a
        self.verb("reblade_add", src=tid, dst=a, kind="source")  # incoming to a
        res = self.verb("blade_delete", id=a)
        self.assertIn("log_id", res)
        self.assertEqual(self.positions(cid), [b, c])
        self.assert_gapless(cid)
        # outgoing cascade-deleted; incoming preserved and reported
        self.assertEqual(self.pkeys(), {(tid, a, "source")})
        self.assertEqual(len(res["incoming"]), 1)
        inc = res["incoming"][0]
        self.assertEqual((inc["src"], inc["dst"], inc["kind"]), (tid, a, "source"))
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops, {a: "deleted", cid: "modified"})

    def test_delete_unknown_not_found(self):
        self.expect_error("not_found", "blade_delete", id=new_uuid7())


class AutoLink(QatenaTestCase):
    def test_exact_multiword_chain_name_mints_one(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        res = insert(self, cid, "visit the forge at dawn")
        self.assertEqual(res["blade"]["pos"], 0)
        self.assertEqual(res["minted"],
                         [{"src": res["blade"]["id"], "dst": cid, "kind": "name-ref"}])
        self.assertEqual(res["candidates"], [])
        self.assertIn((res["blade"]["id"], cid, "name-ref"), self.pkeys())

    def test_near_miss_is_candidate_not_linked(self):
        cid = self.verb("chain_put", name="Forge")["id"]
        res = insert(self, cid, "the forges gleam")
        self.assertEqual(res["minted"], [])
        self.assertLessEqual(len(res["candidates"]), 5)
        self.assertIn({"name": "Forge", "space": "chain", "token": "forges"},
                      res["candidates"])
        self.assertEqual(self.pkeys(), set())

    def test_prefix_of_alias_is_candidate(self):
        self.insert_term("blacksmith")
        cid = self.verb("chain_put", name="Alpha")["id"]
        res = insert(self, cid, "set the black on stone")
        self.assertEqual(res["minted"], [])
        self.assertIn({"name": "blacksmith", "space": "term", "token": "black"},
                      res["candidates"])
        self.assertEqual(self.pkeys(), set())

    def test_cross_space_exact_mints_one_per_space(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        tid = self.insert_term("the forge")
        res = insert(self, cid, "the forge is open")
        self.assertEqual(len(res["minted"]), 2)
        self.assertEqual({m["dst"] for m in res["minted"]}, {cid, tid})
        self.assertTrue(all(m["kind"] == "name-ref"
                            and m["src"] == res["blade"]["id"]
                            for m in res["minted"]))

    def test_edit_reconciliation_stale_gone_new_minted(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        tid = self.insert_term("anvil")
        bid = insert(self, cid, "the anvil glows")["blade"]["id"]
        self.assertEqual(self.pkeys(), {(bid, tid, "name-ref")})
        # old match drops out -> stale ref deleted
        res = self.verb("blade_edit", id=bid, text="the hammer glows")
        self.assertEqual(res["minted"], [])
        self.assertEqual(self.pkeys(), set())
        # a match reappears -> ref minted again
        res = self.verb("blade_edit", id=bid, text="anvil again")
        self.assertEqual(res["minted"],
                         [{"src": bid, "dst": tid, "kind": "name-ref"}])
        self.assertEqual(self.pkeys(), {(bid, tid, "name-ref")})

    def test_edit_same_text_keeps_ref_without_remint(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        tid = self.insert_term("anvil")
        bid = insert(self, cid, "the anvil glows")["blade"]["id"]
        res = self.verb("blade_edit", id=bid, text="the anvil glows")
        self.assertEqual(res["minted"], [])
        self.assertEqual(self.pkeys(), {(bid, tid, "name-ref")})

    def test_chain_put_does_not_autolink(self):
        self.insert_term("anvil")
        res = self.verb("chain_put", name="Anvil")
        self.assertNotIn("minted", res)
        self.assertNotIn("candidates", res)
        self.assertEqual(self.pkeys(), set())

    def test_candidate_cap_five(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        for i in range(6):
            self.insert_term("base%d" % i)
        res = insert(self, cid, "a base of operations")
        self.assertEqual(res["minted"], [])
        self.assertEqual(len(res["candidates"]), 5)  # 6 near-misses, capped at 5
        for c in res["candidates"]:
            self.assertIn(c["name"], {"base%d" % i for i in range(6)})
