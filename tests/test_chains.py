"""chain_put: create, presence-mask, rename, intention upsert/replace, uniqueness."""
from helpers import QatenaTestCase, ISO_RE, UUID_RE


class Create(QatenaTestCase):
    def test_create_fresh_uuid7(self):
        res = self.verb("chain_put", name="The Forge")
        self.assertTrue(UUID_RE.match(res["id"]))
        self.assertEqual(res["id"][14], "7")
        self.assertEqual(res["name"], "The Forge")
        self.assertIn("log_id", res)
        self.assertEqual(res["intentions"], [])
        self.assertEqual(res["blades"], [])
        self.assertRegex(res["created"], ISO_RE)
        self.assertRegex(res["updated"], ISO_RE)

    def test_presence_mask_absent_fields_untouched(self):
        cid = self.verb("chain_put", name="Alpha",
                        intentions=[{"name": "Work"}])["id"]
        res = self.verb("chain_put", name="Alpha")  # no intentions supplied
        self.assertEqual(res["id"], cid)
        self.assertEqual(res["intentions"], ["Work"])
        self.assertEqual(self.section_names(cid), ["Work"])

    def test_id_and_new_name_is_rename(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        res = self.verb("chain_put", id=cid, name="Beta")
        self.assertEqual((res["id"], res["name"]), (cid, "Beta"))
        self.assertEqual(self.chain_ids("Alpha"), [])
        self.assertEqual(self.chain_ids("Beta"), [cid])
        self.assertEqual(self.verb("chain_put", name="Beta")["id"], cid)

    def test_id_and_same_name_noop(self):
        cid = self.verb("chain_put", name="Alpha")["id"]
        self.assertEqual(self.verb("chain_put", id=cid, name="alpha")["name"], "Alpha")

    def test_unknown_id_not_found(self):
        from qatena.uuid7 import new_uuid7
        self.expect_error("not_found", "chain_put", id=new_uuid7(), name="X")


class Uniqueness(QatenaTestCase):
    def test_alias_case_insensitive_upsert(self):
        # SPEC 3.1: chain_put upserts by name; matching is case-insensitive,
        # so a second chain can never exist under a case variant.
        cid = self.verb("chain_put", name="Forge")["id"]
        res = self.verb("chain_put", name="forge")
        self.assertEqual(res["id"], cid)
        self.assertEqual(res["name"], "Forge")
        self.assertEqual(len(self.chain_ids()), 1)

    def test_rename_to_taken_case_insensitive_duplicate(self):
        self.verb("chain_put", name="Forge")
        vid = self.verb("chain_put", name="Voyager")["id"]
        self.expect_error("duplicate_name", "chain_put", id=vid, name="forge")
        self.assertEqual(self.chain_ids("Voyager"), [vid])
        self.assertEqual(len(self.chain_ids("Forge")), 1)

    def test_split_to_taken_case_insensitive_duplicate(self):
        self.verb("chain_put", name="Forge")
        cid = self.verb("chain_put", name="Voyager")["id"]
        bid = self.verb("blade_insert", chain="Voyager", text="x")["blade"]["id"]
        self.expect_error("duplicate_name", "chain_split",
                          source="Voyager", blades=[bid], name="FORGE")


class Intentions(QatenaTestCase):
    def test_replace_semantics(self):
        cid = self.verb("chain_put", name="Alpha",
                        intentions=[{"name": "A"}, {"name": "B"}])["id"]
        self.assertEqual(self.section_names(cid), ["A", "B"])
        res = self.verb("chain_put", id=cid, intentions=[{"name": "C"}])
        self.assertEqual(res["intentions"], ["C"])
        self.assertEqual(self.section_names(cid), ["C"])

    def test_absent_section_created_default_B(self):
        cid = self.verb("chain_put", name="Alpha",
                        intentions=[{"name": "Smithy"}])["id"]
        self.assertEqual(self.section_orientation("Smithy"), "B")
        self.assertEqual(self.section_names(cid), ["Smithy"])

    def test_supplied_orientation_honored_at_creation(self):
        cid = self.verb("chain_put", name="Alpha",
                        intentions=[{"name": "Anvil", "orientation": "W"}])["id"]
        self.assertEqual(self.section_orientation("Anvil"), "W")

    def test_existing_orientation_updates_only_if_supplied(self):
        cid = self.verb("chain_put", name="Alpha",
                        intentions=[{"name": "Smithy"}])["id"]
        self.verb("chain_put", id=cid,
                  intentions=[{"name": "Smithy", "orientation": "W"}])
        self.assertEqual(self.section_orientation("Smithy"), "W")
        self.verb("chain_put", id=cid, intentions=[{"name": "Smithy"}])
        self.assertEqual(self.section_orientation("Smithy"), "W")

    def test_shared_section_orientation_untouched(self):
        cid1 = self.verb("chain_put", name="A",
                         intentions=[{"name": "Shared"}])["id"]
        cid2 = self.verb("chain_put", name="B",
                         intentions=[{"name": "Shared", "orientation": "W"}])["id"]
        self.assertEqual(self.section_orientation("Shared"), "W")
        self.assertEqual(self.section_names(cid1), ["Shared"])

    def test_bad_orientation_validation_failed(self):
        self.expect_error("validation_failed", "chain_put", name="Alpha",
                          intentions=[{"name": "X", "orientation": "S"}])

    def test_sections_only_validation_failed(self):
        self.expect_error("validation_failed", "chain_put",
                          intentions=[{"name": "X"}])


class Delete(QatenaTestCase):
    def test_delete_cascades_header_and_blade_outgoing_reblades_SPEC_3_2(self):
        # SPEC 3.2: outgoing reblades are cascade-deleted — the header's AND
        # the deleted blades' (reblades hold polymorphic UUIDs; no FK cascade).
        cid = self.verb("chain_put", name="Alpha")["id"]
        other = self.verb("chain_put", name="Beta")["id"]
        a = self.verb("blade_insert", chain=cid, text="first line")["blade"]["id"]
        b = self.verb("blade_insert", chain=cid, text="second line")["blade"]["id"]
        self.verb("reblade_add", src=a, dst=other, kind="source")   # outgoing from a blade
        self.verb("reblade_add", src=cid, dst=b, kind="part-of")    # outgoing from header
        tid = self.insert_term("anvil")
        self.verb("reblade_add", src=tid, dst=cid, kind="see")      # incoming: preserved
        res = self.verb("chain_delete", id=cid)
        self.assertEqual(len(res["incoming"]), 1)
        self.assertEqual(self.pkeys(), {(tid, cid, "see")})
        ops = {u: o for _, u, o in self.ref_ops(res["log_id"])}
        self.assertEqual(ops, {cid: "deleted", a: "deleted", b: "deleted"})
