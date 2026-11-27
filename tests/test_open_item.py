"""open_item: the action-driven write verb (SPEC freeform paragraph).

put = presence-masked create-or-update: address by id (precedence) or name,
idempotent name-addressed put with the always-present created marker on that
path only, hard nameless guard (creation always goes through a non-empty
name), explicit-null-clears, state validated against the open_item_states
register. delete = by id or name; the row is deleted and its incoming
reblades are preserved and reported.
"""
import time

from qatena.uuid7 import new_uuid7
from helpers import QatenaTestCase, ISO_RE, UUID7_RE


class PutMint(QatenaTestCase):
    def test_mint_by_name_defaults_and_marker(self):
        res = self.verb("open_item", action="put", name="Polish the blade")
        self.assertTrue(UUID7_RE.match(res["id"]))
        self.assertEqual(res["name"], "Polish the blade")
        self.assertEqual(res["state"], "open")  # default on mint only
        self.assertIs(res["created"], True)  # fresh mint: marker always present
        self.assertRegex(res["updated"], ISO_RE)
        self.assertIn("log_id", res)
        # non-supplied optional fields are ABSENT from the response (no zero-fill)
        for k in ("text", "freeform_current", "freeform_pending", "freeform_total",
                  "freeform_next", "freeform_last", "freeform_due", "freeform_priority"):
            self.assertNotIn(k, res)

    def test_mint_with_freeform_fields(self):
        res = self.verb("open_item", action="put", name="Track",
                        text="body", state="shelved",
                        current=3, pending=1, total=10,
                        next="ch.4", last="ch.2", due="2026-12-01", priority="high")
        self.assertIs(res["created"], True)
        self.assertEqual(res["text"], "body")
        self.assertEqual(res["state"], "shelved")
        self.assertEqual((res["freeform_current"], res["freeform_pending"],
                          res["freeform_total"]), (3, 1, 10))
        self.assertEqual((res["freeform_next"], res["freeform_last"],
                          res["freeform_due"], res["freeform_priority"]),
                         ("ch.4", "ch.2", "2026-12-01", "high"))

    def test_missing_action_validation_failed(self):
        self.expect_error("validation_failed", "open_item", name="X")

    def test_unknown_action_validation_failed(self):
        self.expect_error("validation_failed", "open_item",
                          action="rename", name="X")

    def test_neither_id_nor_name_validation_failed(self):
        self.expect_error("validation_failed", "open_item", action="put")

    def test_state_rejects_unknown_value(self):
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", state="closed")

    def test_state_rejects_explicit_null(self):
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", state=None)

    def test_int_fields_reject_string_and_bool(self):
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", current="3")
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", pending=True)

    def test_str_fields_reject_int(self):
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", due=1)

    def test_id_must_match_uuid_shape(self):
        self.expect_error("validation_failed", "open_item",
                          action="put", name="X", id="not-a-uuid")


class PutAddressing(QatenaTestCase):
    def test_name_addressed_idempotent_found(self):
        self.verb("open_item", action="put", name="Alpha", text="body", current=5)
        # case-insensitive name match: no clobber, no error, full row as it is
        res = self.verb("open_item", action="put", name="alpha")
        self.assertIs(res["created"], False)
        self.assertEqual(res["text"], "body")
        self.assertEqual(res["freeform_current"], 5)
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM open_items WHERE name=?", ("Alpha",)).fetchall()), 1)

    def test_name_addressed_found_applies_supplied_fields(self):
        self.verb("open_item", action="put", name="Alpha")
        res = self.verb("open_item", action="put", name="Alpha", total=9)
        self.assertIs(res["created"], False)
        self.assertEqual(res["freeform_total"], 9)
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM open_items WHERE name=?", ("Alpha",)).fetchall()), 1)

    def test_id_addressed_has_no_created_marker(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", id=oid, state="shelved")
        # id-addressed path: no created-vs-found marker (never a bool)
        self.assertNotIsInstance(res.get("created"), bool)
        self.assertEqual(res["state"], "shelved")
        self.assertEqual(res["id"], oid)

    def test_nameless_resolvable_id_succeeds_as_update(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", id=oid, state="resolved")
        self.assertEqual(res["state"], "resolved")
        self.assertEqual(res["name"], "Alpha")
        self.assertNotIsInstance(res.get("created"), bool)

    def test_nameless_unresolvable_id_not_found(self):
        self.expect_error("not_found", "open_item",
                          action="put", id=new_uuid7())

    def test_name_with_unresolvable_id_not_found(self):
        self.expect_error("not_found", "open_item",
                          action="put", name="New", id=new_uuid7())

    def test_empty_name_behaves_as_missing(self):
        # hard guard: empty name + unresolvable id -> not_found (not validation)
        self.expect_error("not_found", "open_item",
                          action="put", name="", id=new_uuid7())
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", name="", id=oid, text="set")
        self.assertEqual(res["text"], "set")
        self.assertEqual(res["name"], "Alpha")  # name unchanged


class PutRename(QatenaTestCase):
    def test_id_and_new_name_is_rename(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", id=oid, name="Beta")
        self.assertEqual((res["id"], res["name"]), (oid, "Beta"))
        self.assertEqual(
            self.conn.execute("SELECT id FROM open_items WHERE name=?",
                              ("Alpha",)).fetchall(), [])

    def test_rename_to_taken_name_duplicate(self):
        self.verb("open_item", action="put", name="Alpha")
        oid = self.verb("open_item", action="put", name="Beta")["id"]
        # case-insensitive duplicate check, as chain_put
        self.expect_error("duplicate_name", "open_item",
                          action="put", id=oid, name="alpha")
        self.assertEqual(self.conn.execute(
            "SELECT id FROM open_items WHERE name=?", ("Beta",)).fetchall()[0]["id"],
            oid)

    def test_case_variant_of_own_name_is_not_a_rename(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", id=oid, name="alpha")
        self.assertEqual(res["name"], "Alpha")
        self.assertEqual(len(self.conn.execute(
            "SELECT id FROM open_items").fetchall()), 1)


class PutClearing(QatenaTestCase):
    def test_explicit_null_clears_stored_fields(self):
        oid = self.verb("open_item", action="put", name="Alpha", text="body",
                        current=3, due="2026-12-01")["id"]
        res = self.verb("open_item", action="put", id=oid,
                        text=None, current=None, due=None)
        self.assertNotIn("text", res)
        self.assertNotIn("freeform_current", res)
        self.assertNotIn("freeform_due", res)
        row = self.conn.execute(
            "SELECT text, freeform_current, freeform_due FROM open_items WHERE id=?",
            (oid,)).fetchone()
        self.assertEqual((row["text"], row["freeform_current"], row["freeform_due"]),
                         (None, None, None))

    def test_absent_fields_left_unchanged(self):
        oid = self.verb("open_item", action="put", name="Alpha",
                        text="body", total=7)["id"]
        res = self.verb("open_item", action="put", id=oid, state="shelved")
        self.assertEqual(res["text"], "body")
        self.assertEqual(res["freeform_total"], 7)
        self.assertEqual(res["state"], "shelved")

    def test_explicit_null_on_already_null_field_is_noop(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="put", id=oid, text=None)
        # nothing changed: no modified ref, no updated bump
        self.assertEqual(self.ref_ops(res["log_id"]), [])


class PutNoOp(QatenaTestCase):
    def test_noop_put_does_not_bump_updated(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        before = self.conn.execute(
            "SELECT updated FROM open_items WHERE id=?", (oid,)).fetchone()["updated"]
        res = self.verb("open_item", action="put", id=oid)  # true no-op
        after = self.conn.execute(
            "SELECT updated FROM open_items WHERE id=?", (oid,)).fetchone()["updated"]
        self.assertEqual(before, after)
        # the put is still recorded (one log row), but with no log_refs rows
        self.assertEqual(self.log_count(), 2)
        self.assertEqual(self.ref_ops(res["log_id"]), [])

    def test_field_change_bumps_updated_and_refs_modified(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        before = self.conn.execute(
            "SELECT updated FROM open_items WHERE id=?", (oid,)).fetchone()["updated"]
        time.sleep(0.002)  # ensure a strictly later nanosecond
        res = self.verb("open_item", action="put", id=oid, state="shelved")
        after = self.conn.execute(
            "SELECT updated FROM open_items WHERE id=?", (oid,)).fetchone()["updated"]
        self.assertGreater(after, before)
        self.assertEqual(self.ref_ops(res["log_id"]),
                         [(res["log_id"], oid, "modified")])


class Delete(QatenaTestCase):
    def _item_with_incoming(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        cid = self.verb("chain_put", name="Forge")["id"]
        self.verb("reblade_add", src=cid, dst=oid, kind="see")  # incoming
        return oid, cid

    def test_delete_by_id_reports_incoming(self):
        oid, _cid = self._item_with_incoming()
        res = self.verb("open_item", action="delete", id=oid)
        self.assertEqual(res["id"], oid)
        self.assertEqual(res["name"], "Alpha")
        self.assertEqual(len(res["incoming"]), 1)
        self.assertEqual(res["incoming"][0]["kind"], "see")
        self.assertIn("log_id", res)
        self.expect_error("not_found", "get", id=oid)  # row gone
        self.assertEqual(len(self.reblades(dst_uuid=oid)), 1)  # preserved

    def test_delete_by_name_case_insensitive(self):
        oid, _cid = self._item_with_incoming()
        res = self.verb("open_item", action="delete", name="alpha")
        self.assertEqual(res["id"], oid)
        self.expect_error("not_found", "get", id=oid)

    def test_delete_removes_outgoing_keeps_incoming(self):
        oid, cid = self._item_with_incoming()
        other = self.verb("chain_put", name="Voyager")["id"]
        self.verb("reblade_add", src=oid, dst=other, kind="source")  # outgoing
        self.verb("open_item", action="delete", id=oid)
        self.assertEqual(self.pkeys(), {(cid, oid, "see")})

    def test_delete_unresolvable_not_found(self):
        self.expect_error("not_found", "open_item",
                          action="delete", id=new_uuid7())
        self.expect_error("not_found", "open_item",
                          action="delete", name="Nobody")
        self.expect_error("not_found", "open_item", action="delete")


class Logs(QatenaTestCase):
    def test_mint_log_refs_created(self):
        before = self.log_count()
        res = self.verb("open_item", action="put", name="Alpha")
        self.assertEqual(self.log_count(), before + 1)
        log_id = res["log_id"]
        self.assertEqual(self.ref_ops(log_id), [(log_id, res["id"], "created")])
        row = self.conn.execute(
            "SELECT title, area FROM logs WHERE id=?", (log_id,)).fetchone()
        self.assertEqual(row["title"], 'open_item put "Alpha"')
        self.assertEqual(row["area"], "open_item")

    def test_delete_log_refs_deleted(self):
        oid = self.verb("open_item", action="put", name="Alpha")["id"]
        res = self.verb("open_item", action="delete", id=oid)
        self.assertEqual(self.ref_ops(res["log_id"]),
                         [(res["log_id"], oid, "deleted")])
        row = self.conn.execute(
            "SELECT title, area FROM logs WHERE id=?", (res["log_id"],)).fetchone()
        self.assertEqual(row["title"], 'open_item delete "Alpha"')
        self.assertEqual(row["area"], "open_item")


class ReadsRoundTrip(QatenaTestCase):
    def test_get_list_search_see_open_item(self):
        res = self.verb("open_item", action="put", name="Anvil work",
                        text="hammer time")
        g = self.verb("get", id=res["id"])
        self.assertEqual(g["name"], "Anvil work")
        self.assertEqual(g["text"], "hammer time")
        lst = self.verb("list", space="open_item")
        self.assertEqual([r["name"] for r in lst["items"]], ["Anvil work"])
        s = self.verb("search", probes=["Anvil"], spaces=["open_item"])["hits"]
        self.assertEqual([(h["space"], h["id"]) for h in s],
                         [("open_item", res["id"])])
        # a deleted item leaves all read spaces
        self.verb("open_item", action="delete", id=res["id"])
        self.assertEqual(self.verb("list", space="open_item")["items"], [])
