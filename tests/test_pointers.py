"""pointers: endpoint resolution, idempotency, removal, log_refs (SPEC 1.5, 1.7, 3.10-3.11)."""
from qatena.uuid7 import new_uuid7

from helpers import QatenaTestCase, ISO_RE


def entities(case):
    cid = case.verb("chain_put", name="The Forge")["id"]
    bid = case.verb("blade_insert", chain=cid, text="plain line")["blade"]["id"]
    tid = case.insert_term("anvil")
    return cid, bid, tid


class Add(QatenaTestCase):
    def test_add_resolves_endpoints_and_persists(self):
        cid, bid, tid = entities(self)
        res = self.verb("pointer_add", src=bid, dst=cid, kind="part-of", note="n")
        p = res["pointer"]
        self.assertEqual((p["src"], p["dst"], p["kind"], p["note"]),
                         (bid, cid, "part-of", "n"))
        self.assertRegex(p["created"], ISO_RE)
        self.assertIn("log_id", res)
        self.assertIn((bid, cid, "part-of"), self.pkeys())
        # endpoints are polymorphic: chain -> term also resolves server-side
        res2 = self.verb("pointer_add", src=cid, dst=tid, kind="source")
        self.assertIn((cid, tid, "source"), self.pkeys())
        self.assertIsNone(res2["pointer"]["note"])

    def test_add_idempotent_on_src_dst_kind(self):
        cid, bid, tid = entities(self)
        r1 = self.verb("pointer_add", src=bid, dst=cid, kind="see")
        logs = self.log_count()
        r2 = self.verb("pointer_add", src=bid, dst=cid, kind="see")
        self.assertEqual(r1["pointer"], r2["pointer"])  # returned, not duplicated
        self.assertEqual(len(self.pointers(src_uuid=bid)), 1)
        self.assertEqual(self.log_count(), logs + 1)  # still a write transaction

    def test_add_unresolvable_src(self):
        cid, bid, tid = entities(self)
        self.expect_error("unresolvable_endpoint", "pointer_add",
                          src=new_uuid7(), dst=cid, kind="see")

    def test_add_unresolvable_dst_fresh_uuid7(self):
        # logs ids are integers, not UUIDs at all; a fresh never-registered uuid7
        # is the cleanest unresolvable endpoint (SPEC 1.7)
        cid, bid, tid = entities(self)
        self.expect_error("unresolvable_endpoint", "pointer_add",
                          src=cid, dst=new_uuid7(), kind="see")

    def test_add_unknown_kind_validation_failed(self):
        cid, bid, tid = entities(self)
        self.expect_error("validation_failed", "pointer_add",
                          src=cid, dst=tid, kind="loves")

    def test_add_missing_endpoint_validation_failed(self):
        cid, bid, tid = entities(self)
        self.expect_error("validation_failed", "pointer_add", src=cid, kind="see")
        self.expect_error("validation_failed", "pointer_add", dst=tid, kind="see")


class Remove(QatenaTestCase):
    def test_remove_then_not_found_other_kind_untouched(self):
        cid, bid, tid = entities(self)
        self.verb("pointer_add", src=bid, dst=cid, kind="see", note="keep")
        self.verb("pointer_add", src=bid, dst=cid, kind="part-of")
        res = self.verb("pointer_remove", src=bid, dst=cid, kind="see")
        p = res["pointer"]
        self.assertEqual((p["src"], p["dst"], p["kind"], p["note"]),
                         (bid, cid, "see", "keep"))
        self.assertIn("log_id", res)
        self.assertEqual(self.pkeys(), {(bid, cid, "part-of")})
        self.expect_error("not_found", "pointer_remove",
                          src=bid, dst=cid, kind="see")
        # the other kind on the same pair survived
        self.assertEqual(self.pkeys(), {(bid, cid, "part-of")})

    def test_remove_absent_pointer_not_found(self):
        cid, bid, tid = entities(self)
        self.expect_error("not_found", "pointer_remove",
                          src=cid, dst=new_uuid7(), kind="see")


class LogRefs(QatenaTestCase):
    def test_add_stamps_referenced_on_both_endpoints(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        tid = self.insert_term("anvil")
        res = self.verb("pointer_add", src=cid, dst=tid, kind="see")
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops, {cid: "referenced", tid: "referenced"})

    def test_remove_stamps_referenced_on_both_endpoints(self):
        cid = self.verb("chain_put", name="The Forge")["id"]
        tid = self.insert_term("anvil")
        self.verb("pointer_add", src=cid, dst=tid, kind="see")
        res = self.verb("pointer_remove", src=cid, dst=tid, kind="see")
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops, {cid: "referenced", tid: "referenced"})
