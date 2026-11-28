"""Reads: get / search / list / reblades_of / follow / history (SPEC 3.12-3.17)."""
from qatena.db import now_ns, ns_to_iso
from qatena.uuid7 import new_uuid7

from helpers import QatenaTestCase, ISO_RE


def insert(case, chain, text, index=None):
    args = {"chain": chain, "text": text}
    if index is not None:
        args["index"] = index
    return case.verb("blade_insert", **args)


class Get(QatenaTestCase):
    def test_chain_composite_shape(self):
        cid = self.verb("chain_put", name="The Forge",
                        intentions=[{"name": "Work", "orientation": "W"}])["id"]
        b1 = insert(self, cid, "alpha line")["blade"]["id"]
        b2 = insert(self, cid, "beta line")["blade"]["id"]
        g = self.verb("get", id=cid)
        self.assertEqual(g["id"], cid)
        self.assertEqual(g["name"], "The Forge")
        self.assertRegex(g["created"], ISO_RE)
        self.assertRegex(g["updated"], ISO_RE)
        self.assertEqual(g["intentions"], ["Work"])
        self.assertEqual([(x["id"], x["pos"], x["text"]) for x in g["blades"]],
                         [(b1, 0, "alpha line"), (b2, 1, "beta line")])

    def test_blade_record_includes_chain_name(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        bid = insert(self, cid, "alpha line")["blade"]["id"]
        g = self.verb("get", id=bid)
        self.assertEqual(g["id"], bid)
        self.assertEqual(g["chain_id"], cid)
        self.assertEqual(g["chain_name"], "The Forge")
        self.assertEqual(g["text"], "alpha line")
        self.assertRegex(g["created"], ISO_RE)
        self.assertRegex(g["updated"], ISO_RE)

    def test_bare_records_for_other_spaces(self):
        tid = self.insert_term("anvil", "a heavy anvil")
        g = self.verb("get", id=tid)
        self.assertEqual(g["term"], "anvil")
        self.assertEqual(g["text"], "a heavy anvil")
        self.assertRegex(g["created"], ISO_RE)
        oid = self.insert_open_item("Task One", "open")
        g = self.verb("get", id=oid)
        self.assertEqual(g["name"], "Task One")
        self.assertEqual(g["state"], "open")
        sid = self.insert_section("Work")
        g = self.verb("get", id=sid)
        self.assertEqual(g["name"], "Work")
        self.assertEqual(g["orientation"], "B")
        rid = self.insert_ruling("R-001", "First Ruling")
        g = self.verb("get", id=rid)
        self.assertEqual(g["number"], "R-001")
        self.assertEqual(g["name"], "First Ruling")
        self.assertEqual(g["state"], "active")

    def test_not_found_and_validation(self):
        self.expect_error("not_found", "get", id=new_uuid7())
        self.expect_error("not_found", "get", id="not-a-uuid")
        self.expect_error("validation_failed", "get")


class Search(QatenaTestCase):
    def test_multi_probe_or_across_spaces(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        bid = insert(self, cid, "hammer strikes")["blade"]["id"]
        self.insert_term("anvil", "a heavy anvil")
        res = self.verb("search", probes=["forge", "hammer"],
                        spaces=["chain", "blade"])
        self.assertEqual(sorted((h["space"], h["id"]) for h in res["hits"]),
                         sorted([("chain", cid), ("blade", bid)]))
        by_space = {h["space"]: h for h in res["hits"]}
        self.assertEqual(by_space["chain"]["name"], "The Forge")
        self.assertIn("forge", by_space["chain"]["context"].casefold())
        self.assertEqual(by_space["blade"]["name"], "The Forge")  # blade = its chain's name
        self.assertIn("hammer", by_space["blade"]["context"].casefold())

    def test_default_spaces_include_logs(self):
        self.verb("chain_put", name="Zephyr")
        res = self.verb("search", probes=["zephyr"])
        spaces = {h["space"] for h in res["hits"]}
        self.assertIn("chain", spaces)
        self.assertIn("log", spaces)  # the chain_put log title mentions the name

    def test_case_insensitive_substring(self):
        self.verb("chain_put", name="The Forge")
        self.assertEqual(len(self.verb(
            "search", probes=["THE FORGE"], spaces=["chain"])["hits"]), 1)
        self.assertEqual(len(self.verb(
            "search", probes=["orge"], spaces=["chain"])["hits"]), 1)  # substring

    def test_context_window_bounds(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        insert(self, cid, "X" * 100 + "needle" + "Y" * 100)
        res = self.verb("search", probes=["needle"], spaces=["blade"], context=10)
        self.assertEqual(len(res["hits"]), 1)
        self.assertEqual(res["hits"][0]["context"], "X" * 10 + "needle" + "Y" * 10)
        res = self.verb("search", probes=["needle"], spaces=["blade"], context=0)
        self.assertEqual(res["hits"][0]["context"], "needle")

    def test_limit_caps_hits_and_order_is_pinned(self):
        ids = {self.verb("chain_put", name="Dup %d" % i)["id"] for i in range(3)}
        res = self.verb("search", probes=["dup"], spaces=["chain"])
        self.assertEqual(len(res["hits"]), 3)
        res = self.verb("search", probes=["dup"], spaces=["chain"], limit=2)
        self.assertEqual(len(res["hits"]), 2)
        self.assertTrue({h["id"] for h in res["hits"]} <= ids)
        keys = [(h["space"], h["id"]) for h in res["hits"]]
        self.assertEqual(keys, sorted(keys))  # SPEC-pinned sort by (space, id)

    def test_ruling_number_body_and_term_definition(self):
        self.insert_ruling("R-014", "The Verdict", "active", "body prose here")
        self.insert_term("quorum", "the quorum rule")
        self.assertEqual(len(self.verb(
            "search", probes=["R-014"], spaces=["ruling"])["hits"]), 1)
        self.assertEqual(len(self.verb(
            "search", probes=["body prose"], spaces=["ruling"])["hits"]), 1)
        self.assertEqual(len(self.verb(
            "search", probes=["quorum rule"], spaces=["term"])["hits"]), 1)

    def test_no_hits_and_validation(self):
        self.verb("chain_put", name="Alpha")
        self.assertEqual(self.verb("search", probes=["zzz"])["hits"], [])
        self.expect_error("validation_failed", "search", probes=[])
        self.expect_error("validation_failed", "search", probes=["x"],
                          spaces=["bogus"])
        self.expect_error("validation_failed", "search")


class List(QatenaTestCase):
    def test_chain_space_shape_and_include_blades(self):
        c1 = self.verb("chain_put", name="Alpha",
                       intentions=[{"name": "S1"}])["id"]
        c2 = self.verb("chain_put", name="Beta")["id"]
        insert(self, c1, "first line")
        res = self.verb("list", space="chain")
        self.assertEqual(res["count"], 2)
        by_name = {i["name"]: i for i in res["items"]}
        self.assertEqual(set(by_name), {"Alpha", "Beta"})
        self.assertEqual(by_name["Alpha"]["intentions"], ["S1"])
        self.assertNotIn("blades", by_name["Alpha"])  # default false
        self.assertRegex(by_name["Alpha"]["created"], ISO_RE)
        res = self.verb("list", space="chain", include_blades=True)
        by_name = {i["name"]: i for i in res["items"]}
        self.assertEqual(len(by_name["Alpha"]["blades"]), 1)
        self.assertEqual(by_name["Beta"]["blades"], [])

    def test_section_filter_and_name_prefix(self):
        self.verb("chain_put", name="Alpha",
                  intentions=[{"name": "S1", "orientation": "W"}])
        self.verb("chain_put", name="Alphabetic")
        res = self.verb("list", space="chain", filter={"intention": "S1"})
        self.assertEqual([i["name"] for i in res["items"]], ["Alpha"])
        res = self.verb("list", space="chain", filter={"name_prefix": "ALP"})
        self.assertEqual({i["name"] for i in res["items"]},
                         {"Alpha", "Alphabetic"})  # case-insensitive

    def test_name_prefix_escapes_like_wildcards(self):
        self.verb("chain_put", name="a_b")
        self.verb("chain_put", name="axb")
        res = self.verb("list", space="chain", filter={"name_prefix": "a_b"})
        self.assertEqual([i["name"] for i in res["items"]], ["a_b"])

    def test_section_space_orientation_filter(self):
        self.verb("chain_put", name="A", intentions=[{"name": "S1", "orientation": "W"}])
        self.verb("chain_put", name="B", intentions=[{"name": "S2"}])
        res = self.verb("list", space="intention", filter={"orientation": "W"})
        self.assertEqual([i["name"] for i in res["items"]], ["S1"])
        res = self.verb("list", space="intention")
        self.assertEqual({i["name"] for i in res["items"]}, {"S1", "S2"})
        self.assertEqual({i["orientation"] for i in res["items"]}, {"W", "B"})

    def test_open_item_and_ruling_state_filters(self):
        self.insert_open_item("Task One", "open")
        self.insert_open_item("Task Done", "resolved")
        self.insert_ruling("R-001", "First", "final")
        self.insert_ruling("R-002", "Second", "active")
        res = self.verb("list", space="open_item", filter={"state": "resolved"})
        self.assertEqual([i["name"] for i in res["items"]], ["Task Done"])
        res = self.verb("list", space="ruling", filter={"state": "final"})
        self.assertEqual([i["number"] for i in res["items"]], ["R-001"])

    def test_unknown_filter_key_warning(self):
        self.verb("chain_put", name="Alpha")
        res = self.verb("list", space="chain", filter={"bogus": "x"})
        self.assertIn("bogus", res["warning"])
        self.assertEqual(res["count"], 1)  # list still works
        res = self.verb("list", space="chain", filter={"state": "open"})
        self.assertIn("state", res["warning"])  # inapplicable to chains

    def test_unknown_space_validation_failed(self):
        self.expect_error("validation_failed", "list", space="blade")
        self.expect_error("validation_failed", "list")

    def test_limit_and_offset_cover_space(self):
        for i in range(3):
            self.verb("chain_put", name="Page %d" % i)
        res = self.verb("list", space="chain", limit=2)
        self.assertEqual(res["count"], 2)
        seen = set()
        for off in range(3):
            r = self.verb("list", space="chain", limit=1, offset=off)
            self.assertEqual(len(r["items"]), 1)
            seen.add(r["items"][0]["name"])
        self.assertEqual(seen, {"Page 0", "Page 1", "Page 2"})


class ListLogs(QatenaTestCase):
    def test_log_space_by_uuid_since_until(self):
        # Boundaries one full millisecond outside the write's millisecond so
        # the inclusion/exclusion is strict (the tool surface is ms-granular).
        t_before = ns_to_iso(now_ns() - 1_000_000)
        cid = self.verb("chain_put", name="Zephyr")["id"]
        t_after = ns_to_iso(now_ns() + 1_000_000)
        res = self.verb("list", space="log", filter={"uuid": cid})
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["items"][0]["id"], 1)  # first log in a fresh DB
        # until before the write: excluded (log created strictly after)
        res = self.verb("list", space="log",
                        filter={"uuid": cid, "until": t_before})
        self.assertEqual(res["count"], 0)
        # since before the write: included
        res = self.verb("list", space="log",
                        filter={"uuid": cid, "since": t_before})
        self.assertEqual(res["count"], 1)
        # since after the write: excluded
        res = self.verb("list", space="log",
                        filter={"uuid": cid, "since": t_after})
        self.assertEqual(res["count"], 0)
        self.expect_error("validation_failed", "list", space="log",
                          filter={"since": "yesterday"})


class RebladesOf(QatenaTestCase):
    def test_in_out_both_with_resolved_endpoint_names(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=cid, dst=tid, kind="source", note="prov")
        self.verb("reblade_add", src=tid, dst=cid, kind="see")
        out = self.verb("reblades_of", id=cid, direction="out")["edges"]
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual((e["src"], e["dst"], e["kind"], e["note"]),
                         (cid, tid, "source", "prov"))
        self.assertEqual((e["src_space"], e["src_name"]), ("chain", "The Forge"))
        self.assertEqual((e["dst_space"], e["dst_name"]), ("term", "anvil"))
        self.assertRegex(e["created"], ISO_RE)
        inn = self.verb("reblades_of", id=cid, direction="in")["edges"]
        self.assertEqual(len(inn), 1)
        self.assertEqual((inn[0]["src"], inn[0]["kind"]), (tid, "see"))
        both = self.verb("reblades_of", id=cid)["edges"]  # default: both
        self.assertEqual(len(both), 2)

    def test_kind_filter(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=cid, dst=tid, kind="source")
        self.verb("reblade_add", src=cid, dst=tid, kind="see")
        edges = self.verb("reblades_of", id=cid, kind="see")["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["kind"], "see")

    def test_blade_endpoint_named_by_its_chain(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        bid = insert(self, cid, "first line")["blade"]["id"]
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=tid, dst=bid, kind="source")
        e = self.verb("reblades_of", id=bid, direction="in")["edges"][0]
        self.assertEqual((e["dst_space"], e["dst_name"]), ("blade", "The Forge"))

    def test_unknown_kind_validation_failed(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.expect_error("validation_failed", "reblades_of", id=cid, kind="loves")

    def test_unresolved_id_returns_empty(self):
        self.assertEqual(self.verb("reblades_of", id=new_uuid7())["edges"], [])


class Follow(QatenaTestCase):
    def _graph(self):
        a = self.verb("chain_put", name="A1")["id"]
        b = self.verb("chain_put", name="B2")["id"]
        c = self.verb("chain_put", name="C3")["id"]
        self.verb("reblade_add", src=a, dst=b, kind="see")
        self.verb("reblade_add", src=b, dst=c, kind="source")
        return a, b, c

    def test_out_respects_max_hops(self):
        a, b, c = self._graph()
        res = self.verb("follow", **{"from": a, "direction": "out", "max_hops": 1})
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, b})
        depths = {n["uuid"]: n["depth"] for n in res["nodes"]}
        self.assertEqual(depths, {a: 0, b: 1})
        res = self.verb("follow", **{"from": a, "max_hops": 2})  # default direction: out
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, b, c})
        res = self.verb("follow", **{"from": a, "max_hops": 5})  # no more neighbors
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, b, c})

    def test_in_direction(self):
        a, b, c = self._graph()
        res = self.verb("follow", **{"from": c, "direction": "in", "max_hops": 2})
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, b, c})
        res = self.verb("follow", **{"from": c, "direction": "in", "max_hops": 1})
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {b, c})

    def test_both_directions_with_edge_kinds(self):
        a, b, c = self._graph()
        res = self.verb("follow", **{"from": b, "direction": "both", "max_hops": 1})
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, b, c})
        kinds = {n["uuid"]: n["edge_kind"] for n in res["nodes"]}
        self.assertEqual(kinds, {b: None, a: "see", c: "source"})

    def test_cycle_pruned(self):
        a = self.verb("chain_put", name="Loop A")["id"]
        b = self.verb("chain_put", name="Loop B")["id"]
        self.verb("reblade_add", src=a, dst=b, kind="see")
        self.verb("reblade_add", src=b, dst=a, kind="see")
        res = self.verb("follow", **{"from": a, "direction": "out", "max_hops": 5})
        self.assertEqual([n["uuid"] for n in res["nodes"]], [a, b])  # a not revisited

    def test_kinds_filter(self):
        a = self.verb("chain_put", name="A1")["id"]
        b = self.verb("chain_put", name="B2")["id"]
        c = self.verb("chain_put", name="C3")["id"]
        self.verb("reblade_add", src=a, dst=b, kind="source")
        self.verb("reblade_add", src=a, dst=c, kind="see")
        res = self.verb("follow", **{"from": a, "kinds": ["see"], "max_hops": 2})
        self.assertEqual({n["uuid"] for n in res["nodes"]}, {a, c})

    def test_max_hops_and_direction_validation(self):
        a = self.verb("chain_put", name="A1")["id"]
        self.expect_error("validation_failed", "follow", **{"from": a, "max_hops": 6})
        self.expect_error("validation_failed", "follow", **{"from": a, "max_hops": 0})
        self.expect_error("validation_failed", "follow", **{"from": a, "direction": "sideways"})

    def test_unresolvable_start_not_found(self):
        self.expect_error("not_found", "follow", **{"from": new_uuid7()})

    def test_expand_chains(self):
        a = self.verb("chain_put", name="A1")["id"]
        b1 = insert(self, a, "first line")["blade"]["id"]
        b2 = insert(self, a, "second line")["blade"]["id"]
        d = self.verb("chain_put", name="D4")["id"]
        self.verb("reblade_add", src=d, dst=a, kind="part-of")
        res = self.verb("follow", **{"from": d, "expand_chains": True, "max_hops": 1})
        node_a = [n for n in res["nodes"] if n["uuid"] == a][0]
        self.assertEqual(node_a["blades"], [b1, b2])
        node_d = [n for n in res["nodes"] if n["uuid"] == d][0]
        self.assertEqual(node_d["blades"], [])
        res = self.verb("follow", **{"from": d, "max_hops": 1})
        node_a = [n for n in res["nodes"] if n["uuid"] == a][0]
        self.assertNotIn("blades", node_a)


class History(QatenaTestCase):
    def test_full_rows_newest_first(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.verb("chain_put", id=cid, name="Beta")  # rename
        res = self.verb("history", id=cid)
        self.assertEqual(sorted(res), ["genesis", "revisions", "trace"])
        self.assertEqual(len(res["trace"]), 2)
        e0, e1 = res["trace"]
        # newest first; same-millisecond writes render identical ISO stamps,
        # so the strict ordering is pinned by the ops below (log id is not
        # surfaced in the trace row shape)
        self.assertGreaterEqual(e0["created"], e1["created"])
        self.assertEqual(e1["op"], "created")
        self.assertEqual(e0["op"], "modified")
        self.assertTrue(e0["title"].startswith("chain_put"))
        self.assertRegex(e0["created"], ISO_RE)

    def test_limit_and_unknown_uuid_empty(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.verb("chain_put", id=cid, name="Beta")
        self.verb("chain_put", id=cid, name="Gamma")
        res = self.verb("history", id=cid, limit=2)
        # bare-connection degradation: no sidecar attached, so nothing was
        # minted -- genesis null, revisions empty; the trace still carries
        # the FULL log rows for the uuid
        self.assertEqual(res["revisions"], [])
        self.assertIsNone(res["genesis"])
        self.assertEqual(len(res["trace"]), 3)
        self.assertEqual(res["trace"][0]["title"], 'chain_put "Gamma"')
        self.assertEqual(res["trace"][0]["op"], "modified")
        empty = self.verb("history", id=new_uuid7())
        self.assertEqual(empty["revisions"], [])
        self.assertEqual(empty["trace"], [])
        self.assertIsNone(empty["genesis"])
