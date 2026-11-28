"""Vambrace routing layer tests (SPEC 1.9, 3.18) — unit level, no subprocess.

Drives qatena.server._call_vambrace / _call_routed directly against a temp
armory dir; storage-level assertions go through qatena.db.connect. Restart
persistence is a property of the stdio server and lives in tests/test_server.py.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import unittest

from qatena import db, schema, server, uuid7
from qatena.verbs import execute

# SPEC 1.9 key format: 36-char lowercase hyphenated, version nibble 7.
KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
REBLADE_KINDS = {"name-ref", "supersedes", "part-of", "source", "see", "conflict", "depends-on"}
VAMBRACE_TABLES = {
    "chain_headers", "chain_intentions", "chain_intention_map", "chain_blades",
    "blade_order", "reblades", "reblade_kinds", "logs", "log_refs",
}


class VambraceTestCase(unittest.TestCase):
    def setUp(self):
        self.armory = tempfile.mkdtemp(prefix="qatena-vambrace-")

    def tearDown(self):
        shutil.rmtree(self.armory, ignore_errors=True)

    # -- routing helpers ------------------------------------------------------
    def equip(self, arguments=None):
        if arguments is None:
            arguments = {"action": "equip"}
        return server._call_vambrace(self.armory, arguments)

    def make_vambrace(self):
        payload, is_error = self.equip()
        self.assertFalse(is_error, "equip failed: %r" % payload)
        return payload["vambrace"]

    def routed(self, name, arguments):
        return server._call_routed(self.armory, name, arguments)

    def expect_routed_error(self, code, name, arguments):
        payload, is_error = self.routed(name, arguments)
        self.assertTrue(is_error,
                        "%r %r should fail closed, got payload %r"
                        % (name, arguments, payload))
        self.assertEqual(payload, {"error": {"code": code,
                                             "message": payload["error"]["message"]}},
                         "error payload shape: %r" % payload)
        self.assertIsInstance(payload["error"]["message"], str)
        return payload


class Equip(VambraceTestCase):
    def test_equip_returns_exact_payload_with_well_formed_uuidv7(self):
        payload, is_error = self.equip()
        self.assertFalse(is_error)
        self.assertEqual(list(payload), ["vambrace"],
                         "equip returns exactly the routing key: %r" % payload)
        key = payload["vambrace"]
        self.assertEqual(len(key), 36)
        self.assertRegex(key, KEY_RE)
        self.assertEqual(key[14], "7")  # version nibble

    def test_vambrace_file_exists_and_is_bootstrapped(self):
        key = self.make_vambrace()
        path = os.path.join(self.armory, key + ".db")
        self.assertTrue(os.path.exists(path))
        conn = db.connect(path)
        try:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(VAMBRACE_TABLES.issubset(tables),
                            "missing vambrace tables: %s" % (VAMBRACE_TABLES - tables))
            kinds = {r["kind"] for r in conn.execute("SELECT kind FROM reblade_kinds")}
            self.assertEqual(kinds, REBLADE_KINDS)
        finally:
            conn.close()

    def test_two_equips_are_distinct(self):
        k1, k2 = self.make_vambrace(), self.make_vambrace()
        self.assertNotEqual(k1, k2)
        self.assertTrue(os.path.exists(os.path.join(self.armory, k1 + ".db")))
        self.assertTrue(os.path.exists(os.path.join(self.armory, k2 + ".db")))

    def test_equip_ignores_stray_routing_key(self):
        # the vambrace verb takes no routing key of its own
        payload, is_error = self.equip(
            {"action": "equip", "vambrace": uuid7.new_uuid7()})
        self.assertFalse(is_error, "stray key must not block equip: %r" % payload)
        self.assertRegex(payload["vambrace"], KEY_RE)


class Actions(VambraceTestCase):
    def test_unsupported_action_variants(self):
        for action in ("list", "destroy", "CREATE"):
            payload, is_error = self.equip({"action": action})
            self.assertTrue(is_error)
            self.assertEqual(payload["error"]["code"], "unsupported_action")

    def test_missing_action_fails_closed(self):
        for arguments in ({}, {"action": None}):
            payload, is_error = self.equip(arguments)
            self.assertTrue(is_error, "arguments %r should fail" % arguments)
            self.assertEqual(payload["error"]["code"], "unsupported_action")


class FailClosed(VambraceTestCase):
    def test_missing_key_variants(self):
        for arguments in ({},                         # absent
                          {"vambrace": ""},           # empty string
                          {"vambrace": None},         # explicit null
                          {"vambrace": 42},           # non-string
                          {"vambrace": ["k"]}):       # non-string
            self.expect_routed_error("missing_vambrace", "chain_put", arguments)

    def test_malformed_key_variants(self):
        v7 = uuid7.new_uuid7()
        bad = (
            "not-a-uuid",             # garbage
            "abc",                    # too short
            v7.upper(),               # uppercase hex
            v7[:14] + "4" + v7[15:],  # version-4 nibble
            v7 + "0",                 # 37 chars
            v7[:9] + v7[10:],         # missing hyphen
        )
        for key in bad:
            self.expect_routed_error("malformed_vambrace", "chain_put",
                                     {"vambrace": key})

    def test_key_regex_matches_spec(self):
        self.assertIsNotNone(KEY_RE.match(uuid7.new_uuid7()))
        v4 = uuid7.new_uuid7()
        self.assertIsNone(KEY_RE.match(v4[:14] + "4" + v4[15:]))
        self.assertIsNone(KEY_RE.match(v4.upper()))

    def test_unknown_key(self):
        ghost = uuid7.new_uuid7()  # well-formed, but never created here
        self.assertFalse(os.path.exists(os.path.join(self.armory, ghost + ".db")))
        self.expect_routed_error("unknown_vambrace", "chain_put",
                                 {"vambrace": ghost})

    def test_routing_checked_before_verb_layer(self):
        # an unknown verb without a key fails at routing, not with not_found
        self.expect_routed_error("missing_vambrace", "definitely_not_a_verb", {})
        # with a valid key, routing passes and the verb layer rejects
        key = self.make_vambrace()
        self.expect_routed_error("not_found", "definitely_not_a_verb",
                                 {"vambrace": key})


class RoutedSuccess(VambraceTestCase):
    def test_chain_put_and_get_round_trip(self):
        key = self.make_vambrace()
        payload, is_error = self.routed("chain_put",
                                        {"vambrace": key, "name": "The Forge"})
        self.assertFalse(is_error, "chain_put failed: %r" % payload)
        self.assertEqual(payload["name"], "The Forge")
        self.assertIn("log_id", payload)
        payload, is_error = self.routed("get", {"vambrace": key, "id": payload["id"]})
        self.assertFalse(is_error)
        self.assertEqual(payload["name"], "The Forge")

    def test_verb_layer_never_sees_the_key(self):
        # after a routed call, the verb layer works on the same vambrace file
        # with no key at all — the key is stripped before execute()
        key = self.make_vambrace()
        self.routed("chain_put", {"vambrace": key, "name": "Routed"})
        conn = db.connect(os.path.join(self.armory, key + ".db"))
        try:
            payload = execute(conn, "chain_put", {"name": "No Key"})
            self.assertEqual(payload["name"], "No Key")
        finally:
            conn.close()

    def test_batch_routes_via_single_top_level_key(self):
        key = self.make_vambrace()
        payload, is_error = self.routed("batch", {
            "vambrace": key,
            "ops": [
                {"op": "chain_put", "args": {"name": "Batch A"}},
                {"op": "chain_put", "args": {"name": "Batch B"}},
            ],
        })
        self.assertFalse(is_error, "batch failed: %r" % payload)
        self.assertEqual(len(payload["ops"]), 2)
        self.assertIn("log_id", payload)  # one transaction, one minted log
        self.assertEqual([op["log_id"] for op in payload["ops"]],
                         [payload["log_id"]] * 2)
        payload, is_error = self.routed("list", {"vambrace": key, "space": "chain"})
        self.assertFalse(is_error)
        self.assertEqual(payload["count"], 2)

    def test_vambraces_are_independent(self):
        a, b = self.make_vambrace(), self.make_vambrace()
        payload, is_error = self.routed("chain_put",
                                        {"vambrace": a, "name": "OnlyInA"})
        self.assertFalse(is_error)
        cid = payload["id"]
        # the chain is visible in its own vambrace...
        payload, is_error = self.routed("list", {"vambrace": a, "space": "chain"})
        self.assertFalse(is_error)
        self.assertEqual(payload["count"], 1)
        # ...and absent from the other one
        payload, is_error = self.routed("list", {"vambrace": b, "space": "chain"})
        self.assertFalse(is_error)
        self.assertEqual(payload["count"], 0)
        self.expect_routed_error("not_found", "get", {"vambrace": b, "id": cid})

    def test_bootstrap_self_heals_hand_touched_vambrace_file(self):
        # a vambrace file lacking the schema (hand-touched) is bootstrapped on
        # the next routed call — bootstrap is idempotent
        key = uuid7.new_uuid7()
        conn = sqlite3.connect(os.path.join(self.armory, key + ".db"))
        conn.execute("CREATE TABLE stray (x INTEGER)")
        conn.commit()
        conn.close()
        payload, is_error = self.routed("list", {"vambrace": key, "space": "chain"})
        self.assertFalse(is_error, "self-heal failed: %r" % payload)
        self.assertEqual(payload["count"], 0)


if __name__ == "__main__":
    unittest.main()
