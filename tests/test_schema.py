"""Schema bootstrap, seeded registers, UUIDv7 format/ordering (SPEC 1.1, 2)."""
import time

from qatena import schema
from qatena.uuid7 import new_uuid7
from helpers import QatenaTestCase, UUID7_RE


class Bootstrap(QatenaTestCase):
    def test_bootstrap_idempotent(self):
        schema.bootstrap(self.conn)
        schema.bootstrap(self.conn)  # second run: no error
        tables = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "chain_headers", "chain_sections", "chain_section_map", "chain_blades",
            "blade_order", "pointers", "pointer_kinds", "logs", "log_refs",
            "open_item_states", "open_items", "ruling_states", "rulings", "terms"}
        self.assertTrue(expected.issubset(tables))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM chain_headers").fetchone()["c"], 0)


class SeededRegisters(QatenaTestCase):
    def test_pointer_kinds_exact(self):
        got = {r["kind"] for r in self.conn.execute("SELECT kind FROM pointer_kinds")}
        self.assertEqual(got, {"name-ref", "supersedes", "part-of", "source", "see"})

    def test_open_item_states_exact(self):
        got = {r["name"] for r in self.conn.execute("SELECT name FROM open_item_states")}
        self.assertEqual(got, {"open", "shelved", "resolved"})

    def test_ruling_states_exact(self):
        got = {r["name"] for r in self.conn.execute("SELECT name FROM ruling_states")}
        self.assertEqual(got, {"active", "retracted", "corrected", "final"})


class Uuid7(QatenaTestCase):
    def test_format(self):
        for _ in range(25):
            u = new_uuid7()
            self.assertEqual(len(u), 36)
            self.assertRegex(u, UUID7_RE)
            self.assertEqual(u[14], "7")      # version nibble
            self.assertIn(u[19], "89ab")      # variant nibble

    def test_ordered_2ms_apart(self):
        # Only cross-generation ordering at a 2ms gap is asserted (not
        # within-millisecond ordering, which the layout does not guarantee).
        a = new_uuid7()
        time.sleep(0.002)
        b = new_uuid7()
        self.assertLess(a, b)
