"""The 19-verb surface of SPEC 3, behind one dispatch.

    execute(conn, verb, args) -> dict   (pure JSON-serializable result)

Raises VerbError(code, message) with code in:
    not_found | duplicate_name | unresolvable_endpoint | invalid_index |
    validation_failed | not_implemented | db_error

Structural rules implemented here (SPEC 1):
  * one transaction per write verb: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on
    any failure (SPEC 1.4)
  * system-minted log on every write: one logs row + log_refs rows, same
    transaction (SPEC 1.5)
  * auto-linking on blade text writes, incl. reconciliation on blade_edit
    (SPEC 1.6)
  * reblade endpoints validated server-side (SPEC 1.7)
  * gapless blade_order re-established inside the write transaction on every
    insert / delete / move / split / merge (SPEC 1.8)
  * intention membership writes and the owning chain's updated always together
    (SPEC 1.8)
"""

import json
import os
import re
import secrets
import sqlite3

from .autolink import load_aliases, analyze, mint_name_refs
from .db import now_ns, ns_to_iso, iso_to_ns
from .uuid7 import new_uuid7


# --------------------------------------------------------------------- errors

class VerbError(Exception):
    """A verb failed. ``code`` is one of the SPEC 3 error codes."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def __str__(self):
        return f"{self.code}: {self.message}"


# ------------------------------------------------------- endpoint resolution

# Endpoint space (SPEC 1.7): logs and state registers are NOT endpoints.
_ENDPOINTS = (
    ("chain", "chain_headers", "name"),
    ("intention", "chain_intentions", "name"),
    ("blade", "chain_blades", None),   # no name column; reported as its chain's name
    ("open_item", "open_items", "name"),
    ("ruling", "rulings", "name"),
    ("term", "terms", "term"),
)


def _resolve_endpoint(conn, uuid):
    """Resolve a polymorphic endpoint uuid -> {"space","name"} or None."""
    for space, table, name_col in _ENDPOINTS:
        if space == "blade":
            row = conn.execute(
                "SELECT h.name AS name FROM chain_blades b "
                "JOIN chain_headers h ON h.id = b.chain_id WHERE b.id = ?",
                (uuid,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {name_col} AS name FROM {table} WHERE id = ?", (uuid,)
            ).fetchone()
        if row is not None:
            return {"space": space, "name": row["name"]}
    return None


# ------------------------------------------------------------ small helpers

# The id property pattern of the tool schemas: a 36-char lowercase UUID.
# Qatena mints UUIDv7; this check validates the SHAPE only, so a well-formed
# but unknown uuid is not_found, never validation_failed.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _need_str(value, what):
    if not isinstance(value, str) or not value:
        raise VerbError("validation_failed", f"{what} must be a non-empty string")


def _check_index(value, what="index"):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VerbError("validation_failed", f"{what} must be a non-negative integer")
    return value


def _check_int(value, what, minimum):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise VerbError("validation_failed", f"{what} must be an integer >= {minimum}")
    return value


def _iso_ns(value, what):
    try:
        return iso_to_ns(value)
    except ValueError:
        raise VerbError("validation_failed", f"{what} must be ISO-8601 UTC millis (e.g. 2026-11-27T06:31:00.123Z)") from None


def _like_escaped(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bare(row):
    """All columns of a row; created/updated rendered as ISO-8601 (SPEC 1.2)."""
    rec = {}
    for k in row.keys():
        v = row[k]
        if k in ("created", "updated") and isinstance(v, int):
            v = ns_to_iso(v)
        rec[k] = v
    return rec


def canon_args(args):
    """Canonical JSON for a write's args (the deflected_writes string rule).

    Object keys sorted (callers, especially LLMs, do not re-emit keys in a
    stable order), compact separators. Lists keep their order — membership
    order (e.g. chain_put.intentions) is semantically significant; no list
    normalization, ever. Used at deflection and at auto-clear matching only,
    never at any read.
    """
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def _pd(src, dst, kind, note, created):
    return {"src": src, "dst": dst, "kind": kind, "note": note, "created": ns_to_iso(created)}


def _reblade_dict(row):
    return _pd(row["src_uuid"], row["dst_uuid"], row["kind"], row["note"], row["created"])


def _ordered_blades(conn, chain_id):
    return conn.execute(
        "SELECT b.id AS id, b.text AS text, o.pos AS pos "
        "FROM chain_blades b "
        "JOIN blade_order o ON o.chain_id = b.chain_id AND o.blade_id = b.id "
        "WHERE b.chain_id = ? ORDER BY o.pos",
        (chain_id,),
    ).fetchall()


def _rebuild_order(conn, chain_id, blade_ids):
    """Re-establish gapless 0..n-1 order for a chain (SPEC 1.8)."""
    conn.execute("DELETE FROM blade_order WHERE chain_id = ?", (chain_id,))
    for pos, bid in enumerate(blade_ids):
        conn.execute(
            "INSERT INTO blade_order (chain_id, blade_id, pos) VALUES (?, ?, ?)",
            (chain_id, bid, pos),
        )


def _intention_names(conn, chain_id):
    return [
        r["name"]
        for r in conn.execute(
            "SELECT s.name FROM chain_intention_map m "
            "JOIN chain_intentions s ON s.id = m.intention_id "
            "WHERE m.chain_id = ? ORDER BY s.name COLLATE NOCASE",
            (chain_id,),
        )
    ]


def _chain_composite(conn, chain_id):
    h = conn.execute("SELECT * FROM chain_headers WHERE id = ?", (chain_id,)).fetchone()
    return {
        "id": h["id"],
        "name": h["name"],
        "created": ns_to_iso(h["created"]),
        "updated": ns_to_iso(h["updated"]),
        "intentions": _intention_names(conn, chain_id),
        "blades": [
            {"id": b["id"], "pos": b["pos"], "text": b["text"]}
            for b in _ordered_blades(conn, chain_id)
        ],
    }


def _resolve_chain_ref(conn, ref):
    """Resolve a single string that is a chain id or name (spec 3.3-3.7)."""
    if not isinstance(ref, str) or not ref:
        raise VerbError("validation_failed", "chain reference must be a non-empty string")
    row = conn.execute("SELECT * FROM chain_headers WHERE id = ?", (ref,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM chain_headers WHERE name = ?", (ref,)).fetchone()
    if row is None:
        raise VerbError("not_found", f"chain {ref!r} not found")
    return row


def _find_chain(conn, args, id_key, name_key):
    """Addressing rule: id takes precedence; both -> name must match current."""
    i = args.get(id_key)
    n = args.get(name_key)
    if i is not None and not isinstance(i, str):
        raise VerbError("validation_failed", f"{id_key} must be a uuid string")
    if i:
        row = conn.execute("SELECT * FROM chain_headers WHERE id = ?", (i,)).fetchone()
        if row is None:
            raise VerbError("not_found", f"chain {i} not found")
        if n is not None:
            _need_str(n, name_key)
            if row["name"].casefold() != n.casefold():
                raise VerbError(
                    "validation_failed",
                    f"name {n!r} does not match chain {i} (current name {row['name']!r})",
                )
        return row
    if n is not None:
        _need_str(n, name_key)
        return conn.execute("SELECT * FROM chain_headers WHERE name = ?", (n,)).fetchone()
    return None


# -------------------------------------------------- write-transaction plumbing

_OP_PRIORITY = {"created": 0, "modified": 1, "deleted": 2, "referenced": 3}


class _Tx:
    """Scratch state of one write transaction: log refs, body records, and
    the content-field revision rows (SPEC 3.17) the handlers collected."""

    def __init__(self, now):
        self.now = now
        self.refs = {}
        self.records = []
        self.revisions = []  # (uuid, field, old, new) tuples, mint order

    def ref(self, uuid, op):
        cur = self.refs.get(uuid)
        if cur is None or _OP_PRIORITY[op] < _OP_PRIORITY[cur]:
            self.refs[uuid] = op

    def record(self, rec):
        self.records.append(rec)


def _mint_log(conn, tx, title, area):
    """Mint the system log row + log_refs (SPEC 1.5). Inside the tx."""
    body = json.dumps(tx.records, sort_keys=True, separators=(",", ":"))
    cur = conn.execute(
        "INSERT INTO logs (created, title, area, body) VALUES (?, ?, ?, ?)",
        (tx.now, title, area, body),
    )
    log_id = cur.lastrowid
    for uuid, op in tx.refs.items():
        conn.execute(
            "INSERT OR IGNORE INTO log_refs (log_id, uuid, op) VALUES (?, ?, ?)",
            (log_id, uuid, op),
        )
    return log_id


def _begin(conn):
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:
        raise VerbError("db_error", f"begin: {e}") from None


def _rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _rev(tx, uuid, field, old, new):
    """Mint one revision row (SPEC 3.17): content fields only; a no-op
    write (old == new, incl. None == None) mints nothing."""
    if old != new:
        tx.revisions.append((uuid, field, old, new))


def _hist_attached(conn):
    """True when the history sidecar is ATTACHed as `hist` — every
    routed open (server, P3); a bare unit-level connection carries no
    sidecar and mints no revision rows."""
    return any(r["name"] == "hist" for r in conn.execute("PRAGMA database_list"))


def _run_write(handler, conn, args, name):
    tx = _Tx(now_ns())
    _begin(conn)
    try:
        result, title, area = handler(conn, args, tx)
        log_id = _mint_log(conn, tx, title, area)
        # revision rows (SPEC 3.17): the handler collected the content-field
        # changes on tx.revisions; every row of a batch shares the batch's
        # single log_id, and a rolled-back write rolls its rows back with it.
        # A bare unit-level connection has no `hist` attached and mints none.
        if tx.revisions and _hist_attached(conn):
            for uuid, field, old, new in tx.revisions:
                conn.execute(
                    "INSERT INTO hist.revisions (uuid, field, old_val, new_val, "
                    "log_id, created) VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid, field, old, new, log_id, tx.now))
        result = dict(result)
        result["log_id"] = log_id
        ops = result.get("ops")
        if isinstance(ops, list):  # batch: every per-op result carries the one log_id
            for opres in ops:
                if isinstance(opres, dict):
                    opres["log_id"] = log_id
        # autoexport cascade (SPEC 3.18): a deleted registered root can never
        # re-trigger — remove its register rows in the same transaction
        deleted = [u for u, op in tx.refs.items() if op == "deleted"]
        if deleted:
            conn.execute(
                "DELETE FROM autoexport_registers WHERE root_uuid IN ("
                + ",".join("?" * len(deleted)) + ")",
                deleted,
            )
        # deflected-writes auto-clear: a later IDENTICAL successful write pays
        # off the pending rows claiming (op, canonical args). Same tx, before
        # COMMIT — a rollback undoes the clear (the clear can never outlive its
        # write). No extra log row: the satisfying write's own log row records it.
        if name != "deflected_write":  # the cache verb never deflects itself
            if name == "batch":
                for op in args.get("ops") or []:
                    if not isinstance(op, dict):
                        continue
                    oargs = op.get("args")
                    if oargs is None:
                        oargs = {}
                    conn.execute(
                        "UPDATE deflected_writes SET state = 'cleared', cleared_at = ? "
                        "WHERE state = 'pending' AND op = ? AND args = ?",
                        (tx.now, op.get("op"), canon_args(oargs)),
                    )
            else:
                conn.execute(
                    "UPDATE deflected_writes SET state = 'cleared', cleared_at = ? "
                    "WHERE state = 'pending' AND op = ? AND args = ?",
                    (tx.now, name, canon_args(args)),
                )
        conn.execute("COMMIT")
        return result
    except sqlite3.Error as e:
        _rollback(conn)
        raise VerbError("db_error", str(e)) from None
    except VerbError:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise


# --------------------------------------------------------------- write verbs

def _apply_intentions(conn, tx, chain_id, intentions):
    """chain_put.intentions / chain_split.intentions upsert semantics (SPEC 3.1).

    Replace membership with exactly the given set; absent intentions are created
    (orientation default "B"); an existing intention's orientation updates only
    if supplied. The owning chain's updated moves in the same transaction.
    """
    if intentions is None:
        return
    if not isinstance(intentions, list):
        raise VerbError("validation_failed", "intentions must be an array")
    ids = []
    for entry in intentions:
        if not isinstance(entry, dict):
            raise VerbError("validation_failed", "each intention entry must be an object with 'name'")
        name = entry.get("name")
        _need_str(name, "intention name")
        orientation = entry.get("orientation")
        if orientation is not None and orientation not in ("W", "B"):
            raise VerbError("validation_failed", f"intention orientation must be 'W' or 'B', got {orientation!r}")
        row = conn.execute(
            "SELECT id, orientation FROM chain_intentions WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            sid = new_uuid7()
            conn.execute(
                "INSERT INTO chain_intentions (id, name, orientation, created, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, name, orientation or "B", tx.now, tx.now),
            )
            tx.ref(sid, "created")
        else:
            sid = row["id"]
            if orientation is not None and row["orientation"] != orientation:
                conn.execute(
                    "UPDATE chain_intentions SET orientation = ?, updated = ? WHERE id = ?",
                    (orientation, tx.now, sid),
                )
                tx.ref(sid, "modified")
        ids.append(sid)
    conn.execute("DELETE FROM chain_intention_map WHERE chain_id = ?", (chain_id,))
    for sid in ids:
        conn.execute(
            "INSERT OR IGNORE INTO chain_intention_map (chain_id, intention_id) VALUES (?, ?)",
            (chain_id, sid),
        )
    conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, chain_id))
    tx.ref(chain_id, "modified")


def _autolink(conn, tx, src_blade_id, text):
    """Run SPEC 1.6 auto-linking for a written blade.

    Returns (expected, minted, candidates): expected is the full set of
    entity ids the text exactly matches (the name-refs that should exist
    after the write); minted lists only the refs created now.
    """
    exact, candidates = analyze(text, load_aliases(conn))
    minted = mint_name_refs(conn, src_blade_id, exact, tx.now)
    for m in minted:
        tx.ref(m["dst"], "referenced")
    return {h["id"] for h in exact}, minted, candidates


def _h_chain_put(conn, args, tx):
    name = args.get("name")
    if name is not None:
        _need_str(name, "name")
    intentions = args.get("intentions")
    i = args.get("id")
    if i is not None and not isinstance(i, str):
        raise VerbError("validation_failed", "id must be a uuid string")
    row = None
    if i:
        row = conn.execute("SELECT * FROM chain_headers WHERE id = ?", (i,)).fetchone()
        if row is None:
            raise VerbError("not_found", f"chain {i} not found")
        # SPEC 3.1: renames are chain_put(id, new name) — a name that differs
        # from the resolved name is a rename, not a validation failure.
    elif name is not None:
        row = conn.execute("SELECT * FROM chain_headers WHERE name = ?", (name,)).fetchone()
    if row is None:
        if name is None:
            raise VerbError("validation_failed", "name is required to create a chain")
        cid = new_uuid7()
        conn.execute(
            "INSERT INTO chain_headers (id, name, created, updated) VALUES (?, ?, ?, ?)",
            (cid, name, tx.now, tx.now),
        )
        tx.ref(cid, "created")
        _rev(tx, cid, "name", None, name)  # a first appearance is a change
        final_name = name
    else:
        cid = row["id"]
        renamed = name is not None and row["name"].casefold() != name.casefold()
        if renamed:
            dup = conn.execute(
                "SELECT id FROM chain_headers WHERE name = ? AND id <> ?", (name, cid)
            ).fetchone()
            if dup is not None:
                raise VerbError("duplicate_name", f"chain name {name!r} is taken (id {dup['id']})")
            conn.execute(
                "UPDATE chain_headers SET name = ?, updated = ? WHERE id = ?",
                (name, tx.now, cid),
            )
            tx.ref(cid, "modified")
            _rev(tx, cid, "name", row["name"], name)
        final_name = name if renamed else row["name"]
    if intentions is not None:
        old_names = _intention_names(conn, cid)
        old_set = set(old_names)
        _apply_intentions(conn, tx, cid, intentions)
        # SPEC 3.17: intention membership is one revision row per changed
        # name (added NULL->name, removed name->NULL); an orientation-only
        # change mints nothing (membership rows are by name).
        supplied_set = set()
        added = []
        for e in intentions:
            n = e["name"]
            if n not in supplied_set:
                supplied_set.add(n)
                if n not in old_set:
                    added.append(n)
        removed = [n for n in old_names if n not in supplied_set]
        for n in added:
            _rev(tx, cid, "intention", None, n)
        for n in removed:
            _rev(tx, cid, "intention", n, None)
    rec = {"op": "chain_put", "chain": cid, "name": final_name}
    if intentions is not None:
        rec["intentions"] = [e.get("name") if isinstance(e, dict) else None for e in intentions]
    tx.record(rec)
    return _chain_composite(conn, cid), f'chain_put "{final_name}"', "chain"


def _h_chain_delete(conn, args, tx):
    row = _find_chain(conn, args, "id", "name")
    if row is None:
        raise VerbError("not_found", "chain not found (id or name required)")
    cid, cname = row["id"], row["name"]
    incoming = conn.execute("SELECT * FROM reblades WHERE dst_uuid = ?", (cid,)).fetchall()
    # (id, text) pairs: the texts feed the forensic revision rows (SPEC 3.17).
    blade_rows = [
        (r["id"], r["text"])
        for r in conn.execute("SELECT id, text FROM chain_blades WHERE chain_id = ?", (cid,))
    ]
    conn.execute("DELETE FROM reblades WHERE src_uuid = ?", (cid,))  # outgoing (header)
    # Blades cascade-delete with the header, but reblades hold polymorphic
    # UUIDs (no FK cascade) — remove the blades' outgoing refs too (SPEC 3.2).
    conn.execute(
        "DELETE FROM reblades WHERE src_uuid IN "
        "(SELECT id FROM chain_blades WHERE chain_id = ?)",
        (cid,),
    )
    conn.execute("DELETE FROM chain_headers WHERE id = ?", (cid,))   # blades + map cascade
    tx.ref(cid, "deleted")
    _rev(tx, cid, "name", cname, None)  # survives the entity (the forensic surface)
    for bid, btext in blade_rows:
        tx.ref(bid, "deleted")
        _rev(tx, bid, "text", btext, None)
    tx.record({"op": "chain_delete", "chain": cid, "name": cname, "incoming": len(incoming)})
    return {"incoming": [_reblade_dict(r) for r in incoming]}, f'chain_delete "{cname}"', "chain"


def _h_chain_split(conn, args, tx):
    src = _resolve_chain_ref(conn, args.get("source"))
    sid = src["id"]
    blades_arg = args.get("blades")
    if not isinstance(blades_arg, list) or not blades_arg:
        raise VerbError("validation_failed", "blades must be a non-empty array of uuids")
    name = args.get("name")
    _need_str(name, "name")
    dup = conn.execute("SELECT id FROM chain_headers WHERE name = ?", (name,)).fetchone()
    if dup is not None:
        raise VerbError("duplicate_name", f"chain name {name!r} is taken (id {dup['id']})")
    by_id = {b["id"]: b for b in _ordered_blades(conn, sid)}
    moved = []
    for bid in blades_arg:
        if not isinstance(bid, str) or bid not in by_id:
            existing = conn.execute(
                "SELECT chain_id FROM chain_blades WHERE id = ?",
                ((bid,) if isinstance(bid, str) else (None,)),
            ).fetchone()
            if existing is None:
                raise VerbError("not_found", f"blade {bid!r} not found")
            raise VerbError("validation_failed", f"blade {bid} does not belong to source chain {sid}")
        if bid in moved:
            raise VerbError("validation_failed", f"blade {bid} listed more than once")
        moved.append(bid)
    nid = new_uuid7()
    conn.execute(
        "INSERT INTO chain_headers (id, name, created, updated) VALUES (?, ?, ?, ?)",
        (nid, name, tx.now, tx.now),
    )
    tx.ref(nid, "created")
    _rev(tx, nid, "name", None, name)  # ONLY the new header's name (SPEC 3.17)
    if args.get("intentions") is not None:
        _apply_intentions(conn, tx, nid, args["intentions"])
    moved_set = set(moved)
    for bid in moved:  # blade UUIDs and their reblades travel intact
        conn.execute(
            "UPDATE chain_blades SET chain_id = ?, updated = ? WHERE id = ?",
            (nid, tx.now, bid),
        )
        tx.ref(bid, "modified")
    _rebuild_order(conn, sid, [b["id"] for b in by_id.values() if b["id"] not in moved_set])
    _rebuild_order(conn, nid, moved)
    tx.record({"op": "chain_split", "source": sid, "new": nid, "name": name, "blades": moved})
    return (
        {"source": _chain_composite(conn, sid), "new": _chain_composite(conn, nid)},
        f'chain_split "{src["name"]}"',
        "mixed",
    )


def _h_chain_merge(conn, args, tx):
    survivor = _resolve_chain_ref(conn, args.get("into"))
    dissolved = _resolve_chain_ref(conn, args.get("fold"))
    vid, did = survivor["id"], dissolved["id"]
    if vid == did:
        raise VerbError("validation_failed", "cannot merge a chain into itself")
    vid_ids = [b["id"] for b in _ordered_blades(conn, vid)]
    did_ids = [b["id"] for b in _ordered_blades(conn, did)]
    for bid in did_ids:  # appended in the dissolved chain's order
        conn.execute(
            "UPDATE chain_blades SET chain_id = ?, updated = ? WHERE id = ?",
            (vid, tx.now, bid),
        )
        tx.ref(bid, "modified")
    _rebuild_order(conn, vid, vid_ids + did_ids)
    tx.ref(vid, "modified")
    # intention memberships are unioned into the survivor
    fold_secs = [
        r["intention_id"]
        for r in conn.execute(
            "SELECT intention_id FROM chain_intention_map WHERE chain_id = ?", (did,)
        )
    ]
    for sec_id in fold_secs:
        conn.execute(
            "INSERT OR IGNORE INTO chain_intention_map (chain_id, intention_id) VALUES (?, ?)",
            (vid, sec_id),
        )
        tx.ref(sec_id, "referenced")
    # reblades aimed at the dissolved header are retargeted onto the survivor
    incoming = conn.execute("SELECT * FROM reblades WHERE dst_uuid = ?", (did,)).fetchall()
    retargeted = []
    for p in incoming:
        clash = conn.execute(
            "SELECT 1 FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
            (p["src_uuid"], vid, p["kind"]),
        ).fetchone()
        if clash is not None:  # (src, survivor, kind) already exists: dedupe
            conn.execute(
                "DELETE FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
                (p["src_uuid"], did, p["kind"]),
            )
        else:
            conn.execute(
                "UPDATE reblades SET dst_uuid = ? WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
                (vid, p["src_uuid"], did, p["kind"]),
            )
            retargeted.append(
                _pd(p["src_uuid"], vid, p["kind"], p["note"], p["created"])
            )
    conn.execute("DELETE FROM reblades WHERE src_uuid = ?", (did,))  # its outgoing
    conn.execute("DELETE FROM chain_headers WHERE id = ?", (did,))   # stops resolving
    tx.ref(did, "deleted")
    _rev(tx, did, "name", dissolved["name"], None)  # ONLY the dissolved name (SPEC 3.17)
    tx.record({
        "op": "chain_merge", "into": vid, "fold": did,
        "blades": did_ids, "intentions": fold_secs, "retargeted": len(retargeted),
    })
    return (
        {"survivor": _chain_composite(conn, vid), "retargeted": retargeted},
        f'chain_merge "{dissolved["name"]}" -> "{survivor["name"]}"',
        "mixed",
    )


def _h_blade_insert(conn, args, tx):
    chain = _resolve_chain_ref(conn, args.get("chain"))
    cid = chain["id"]
    text = args.get("text")
    _need_str(text, "text")
    index = _check_index(args.get("index"), "index")
    ordered = _ordered_blades(conn, cid)
    count = len(ordered)
    if index is None:
        index = count
    if index > count:
        raise VerbError(
            "invalid_index",
            f"index {index} out of range: chain {chain['name']!r} has {count} blades (valid 0..{count})",
        )
    bid = new_uuid7()
    conn.execute(
        "INSERT INTO chain_blades (id, chain_id, text, created, updated) VALUES (?, ?, ?, ?, ?)",
        (bid, cid, text, tx.now, tx.now),
    )
    tx.ref(bid, "created")
    _rev(tx, bid, "text", None, text)
    ids = [b["id"] for b in ordered]
    ids.insert(index, bid)
    _rebuild_order(conn, cid, ids)
    conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, cid))
    tx.ref(cid, "modified")
    _, minted, candidates = _autolink(conn, tx, bid, text)
    tx.record({
        "op": "blade_insert", "chain": cid, "blade": bid,
        "index": index, "text": text, "minted": minted,
    })
    return (
        {
            "blade": {"id": bid, "pos": index, "text": text, "chain_id": cid},
            "minted": minted,
            "candidates": candidates,
        },
        f'blade_insert "{chain["name"]}"',
        "blade",
    )


def _h_blade_edit(conn, args, tx):
    bid = args.get("id")
    if not isinstance(bid, str) or not bid:
        raise VerbError("validation_failed", "id is required (blade uuid)")
    row = conn.execute("SELECT * FROM chain_blades WHERE id = ?", (bid,)).fetchone()
    if row is None:
        raise VerbError("not_found", f"blade {bid} not found")
    text = args.get("text")
    _need_str(text, "text")
    cid = row["chain_id"]
    # Reconciliation (SPEC 1.6): outgoing name-refs recomputed from scratch.
    old_dsts = [
        r["dst_uuid"]
        for r in conn.execute(
            "SELECT dst_uuid FROM reblades WHERE src_uuid = ? AND kind = 'name-ref'", (bid,)
        )
    ]
    expected, minted, candidates = _autolink(conn, tx, bid, text)
    deleted = [
        {"src": bid, "dst": d, "kind": "name-ref"}
        for d in old_dsts
        if d not in expected
    ]
    for p in deleted:
        conn.execute(
            "DELETE FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = 'name-ref'",
            (p["src"], p["dst"]),
        )
    conn.execute("UPDATE chain_blades SET text = ?, updated = ? WHERE id = ?", (text, tx.now, bid))
    tx.ref(bid, "modified")
    _rev(tx, bid, "text", row["text"], text)  # a no-op edit mints nothing
    conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, cid))
    tx.ref(cid, "modified")
    pos = conn.execute(
        "SELECT pos FROM blade_order WHERE chain_id = ? AND blade_id = ?", (cid, bid)
    ).fetchone()["pos"]
    tx.record({
        "op": "blade_edit", "blade": bid, "chain": cid,
        "text": text, "minted": minted, "deleted": deleted,
    })
    return (
        {
            "blade": {"id": bid, "pos": pos, "text": text, "chain_id": cid},
            "minted": minted,
            "deleted": deleted,
            "candidates": candidates,
        },
        f"blade_edit {bid}",
        "blade",
    )


def _h_blade_move(conn, args, tx):
    bid = args.get("id")
    if not isinstance(bid, str) or not bid:
        raise VerbError("validation_failed", "id is required (blade uuid)")
    row = conn.execute("SELECT * FROM chain_blades WHERE id = ?", (bid,)).fetchone()
    if row is None:
        raise VerbError("not_found", f"blade {bid} not found")
    src_cid = row["chain_id"]
    to = args.get("to_chain")
    if to is None:
        dst_cid = src_cid
    else:
        dst_cid = _resolve_chain_ref(conn, to)["id"]
    index = _check_index(args.get("index"), "index")
    if dst_cid == src_cid:
        limit = len([b for b in _ordered_blades(conn, src_cid) if b["id"] != bid])
    else:
        limit = len(_ordered_blades(conn, dst_cid))
    if index is None:
        index = limit
    if index > limit:
        raise VerbError(
            "invalid_index",
            f"index {index} out of range: destination has {limit} blades after removal (valid 0..{limit})",
        )
    if dst_cid != src_cid:
        conn.execute(
            "UPDATE chain_blades SET chain_id = ?, updated = ? WHERE id = ?",
            (dst_cid, tx.now, bid),
        )
        tx.ref(bid, "modified")
        src_ids = [b["id"] for b in _ordered_blades(conn, src_cid)]
        dst_ids = [b["id"] for b in _ordered_blades(conn, dst_cid)]
        conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, src_cid))
        tx.ref(src_cid, "modified")
        _rebuild_order(conn, src_cid, src_ids)
    else:
        dst_ids = [b["id"] for b in _ordered_blades(conn, src_cid) if b["id"] != bid]
    dst_ids.insert(index, bid)
    _rebuild_order(conn, dst_cid, dst_ids)
    conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, dst_cid))
    tx.ref(dst_cid, "modified")
    tx.record({
        "op": "blade_move", "blade": bid,
        "from_chain": src_cid, "to_chain": dst_cid, "index": index,
    })
    return (
        {"blade": {"id": bid, "pos": index, "text": row["text"], "chain_id": dst_cid}},
        f"blade_move {bid}",
        "blade",
    )


def _h_blade_delete(conn, args, tx):
    bid = args.get("id")
    if not isinstance(bid, str) or not bid:
        raise VerbError("validation_failed", "id is required (blade uuid)")
    row = conn.execute("SELECT * FROM chain_blades WHERE id = ?", (bid,)).fetchone()
    if row is None:
        raise VerbError("not_found", f"blade {bid} not found")
    cid = row["chain_id"]
    incoming = conn.execute("SELECT * FROM reblades WHERE dst_uuid = ?", (bid,)).fetchall()
    conn.execute("DELETE FROM reblades WHERE src_uuid = ?", (bid,))  # outgoing
    conn.execute("DELETE FROM chain_blades WHERE id = ?", (bid,))
    _rev(tx, bid, "text", row["text"], None)
    remaining = [b["id"] for b in _ordered_blades(conn, cid)]
    _rebuild_order(conn, cid, remaining)
    conn.execute("UPDATE chain_headers SET updated = ? WHERE id = ?", (tx.now, cid))
    tx.ref(bid, "deleted")
    tx.ref(cid, "modified")
    tx.record({"op": "blade_delete", "blade": bid, "chain": cid, "incoming": len(incoming)})
    return {"incoming": [_reblade_dict(r) for r in incoming]}, f"blade_delete {bid}", "blade"


def _check_kind(conn, kind):
    if not isinstance(kind, str) or conn.execute(
        "SELECT 1 FROM reblade_kinds WHERE kind = ?", (kind,)
    ).fetchone() is None:
        known = [r["kind"] for r in conn.execute("SELECT kind FROM reblade_kinds ORDER BY kind")]
        raise VerbError("validation_failed", f"unknown reblade kind {kind!r} (known: {known})")


def _h_reblade_add(conn, args, tx):
    src, dst = args.get("src"), args.get("dst")
    kind, note = args.get("kind"), args.get("note")
    for label, v in (("src", src), ("dst", dst)):
        if not isinstance(v, str) or not v:
            raise VerbError("validation_failed", f"{label} is required (uuid)")
    _check_kind(conn, kind)
    if note is not None and not isinstance(note, str):
        raise VerbError("validation_failed", "note must be a string")
    if _resolve_endpoint(conn, src) is None:
        raise VerbError("unresolvable_endpoint", f"src {src} does not resolve in any entity space")
    if _resolve_endpoint(conn, dst) is None:
        raise VerbError("unresolvable_endpoint", f"dst {dst} does not resolve in any entity space")
    existing = conn.execute(
        "SELECT * FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
        (src, dst, kind),
    ).fetchone()
    if existing is not None:  # idempotent: returned, not duplicated
        tx.record({"op": "reblade_add", "src": src, "dst": dst, "kind": kind, "created": False})
        return {"reblade": _reblade_dict(existing)}, f"reblade_add {src} -> {dst}", "reblade"
    conn.execute(
        "INSERT INTO reblades (src_uuid, dst_uuid, kind, note, created) VALUES (?, ?, ?, ?, ?)",
        (src, dst, kind, note, tx.now),
    )
    tx.ref(src, "referenced")
    tx.ref(dst, "referenced")
    tx.record({"op": "reblade_add", "src": src, "dst": dst, "kind": kind, "created": True})
    return (
        {"reblade": _pd(src, dst, kind, note, tx.now)},
        f"reblade_add {src} -> {dst}",
        "reblade",
    )


def _h_reblade_remove(conn, args, tx):
    src, dst, kind = args.get("src"), args.get("dst"), args.get("kind")
    for label, v in (("src", src), ("dst", dst)):
        if not isinstance(v, str) or not v:
            raise VerbError("validation_failed", f"{label} is required (uuid)")
    _check_kind(conn, kind)
    row = conn.execute(
        "SELECT * FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
        (src, dst, kind),
    ).fetchone()
    if row is None:
        raise VerbError("not_found", f"reblade ({src}, {dst}, {kind}) not found")
    conn.execute(
        "DELETE FROM reblades WHERE src_uuid = ? AND dst_uuid = ? AND kind = ?",
        (src, dst, kind),
    )
    tx.ref(src, "referenced")
    tx.ref(dst, "referenced")
    tx.record({"op": "reblade_remove", "src": src, "dst": dst, "kind": kind})
    return {"reblade": _reblade_dict(row)}, f"reblade_remove {src} -> {dst}", "reblade"


_BATCH_OPS = (
    "chain_put", "chain_delete", "chain_split", "chain_merge",
    "blade_insert", "blade_edit", "blade_move", "blade_delete",
    "reblade_add", "reblade_remove",
)


def _h_batch(conn, args, tx):
    """One transaction spanning N ops, applied in order (SPEC 1.4/3.9)."""
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        raise VerbError("validation_failed", "ops must be a non-empty array")
    results = []
    for i, op in enumerate(ops):
        name = op.get("op") if isinstance(op, dict) else None
        if name not in _BATCH_OPS:
            raise VerbError("not_found", f"op {i}: unknown op {name!r}")
        oargs = op.get("args")
        if oargs is None:
            oargs = {}
        if not isinstance(oargs, dict):
            raise VerbError("validation_failed", f"op {i}: args must be an object")
        try:
            res, _title, _area = _WRITE_DISPATCH[name](conn, oargs, tx)
        except VerbError as e:
            wrapped = VerbError(e.code, f"op {i} ({name}): {e.message}")
            wrapped.op_index = i  # internal: batch failure attribution for the deflection cache
            raise wrapped from None
        results.append(res)
    return {"ops": results}, f"batch ({len(ops)} ops)", "mixed"


# -------------------------------------------------------------- open_item

# Contract (SPEC freeform paragraph): all fields except the addressing key are
# OPTIONAL and presence-masked; explicit null clears a stored field, absent
# leaves it unchanged. Column map: contract field -> storage column.
_OI_INT_FIELDS = ("current", "pending", "total")      # freeform_<f> INTEGER
_OI_STR_FIELDS = ("next", "last", "due", "priority")  # freeform_<f> TEXT
_OI_INT_COLUMNS = tuple("freeform_" + f for f in _OI_INT_FIELDS)


def _oi_column(field):
    return field if field == "text" else f"freeform_{field}"


def _oi_rev(tx, oid, col, old, new):
    """One open_item revision row (SPEC 3.17): INTEGER freeform columns
    normalize to their decimal string form so old/new compare and store
    consistently; a no-op change mints nothing (via _rev)."""
    if col in _OI_INT_COLUMNS:
        old = None if old is None else str(int(old))
        new = None if new is None else str(int(new))
    _rev(tx, oid, col, old, new)


def _oi_check_state(conn, state):
    """state validates against the open_item_states register (not a
    hardcoded set); state is NOT NULL, so an explicit null is rejected."""
    if state is None:
        raise VerbError("validation_failed", "state is not clearable (explicit null rejected)")
    if not isinstance(state, str):
        raise VerbError("validation_failed", "state must be a string")
    if conn.execute("SELECT 1 FROM open_item_states WHERE name = ?", (state,)).fetchone() is None:
        known = [r["name"] for r in conn.execute("SELECT name FROM open_item_states ORDER BY name")]
        raise VerbError("validation_failed", f"unknown state {state!r} (known: {known})")
    return state


def _oi_validate_fields(conn, args):
    """Validate the supplied optional fields. Returns (state, {field: value})
    with only the supplied fields; state is None when absent (default applies
    on mint only)."""
    state = _oi_check_state(conn, args["state"]) if "state" in args else None
    sets = {}
    if "text" in args:
        v = args["text"]
        if v is not None and not isinstance(v, str):
            raise VerbError("validation_failed", "text must be a string")
        sets["text"] = v
    for f in _OI_INT_FIELDS:
        if f in args:
            v = args[f]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
                raise VerbError("validation_failed", f"{f} must be an integer")
            sets[f] = v
    for f in _OI_STR_FIELDS:
        if f in args:
            v = args[f]
            if v is not None and not isinstance(v, str):
                raise VerbError("validation_failed", f"{f} must be a string")
            sets[f] = v
    return state, sets


def _oi_render(row):
    """The item's row as all non-null fields (NULLs omitted, not zero-filled);
    created/updated rendered as ISO-8601 (as _bare does)."""
    out = {}
    for k in row.keys():
        v = row[k]
        if v is None:
            continue
        out[k] = ns_to_iso(v) if k in ("created", "updated") else v
    return out


def _h_open_item(conn, args, tx):
    """open_item: the single action-driven write verb for the open_item space.

    put = create-or-update (presence-masked, idempotent by name, hard
    nameless guard); delete = by id or name, incoming reblades reported.
    """
    action = args.get("action")
    if action not in ("put", "delete"):
        raise VerbError("validation_failed", f"open_item action must be 'put' or 'delete', got {action!r}")
    if action == "put":
        return _h_open_item_put(conn, args, tx)
    return _h_open_item_delete(conn, args, tx)


def _h_open_item_put(conn, args, tx):
    """Presence-masked create-or-update (SPEC freeform paragraph).

    Addressing: id (shape-checked) takes precedence over name; neither
    supplied -> validation_failed. HARD GUARD: a missing or empty name never
    mints -- a nameless put is an update of a resolvable id or not_found.
    A name-addressed put whose name resolves to an existing item is
    idempotent: no clobber, no error, supplied fields applied as an update.

    No-op put: the put is still recorded in the log body, but nothing changes
    -- no 'modified' ref (the log row simply carries no log_refs rows) and
    updated is NOT bumped.
    """
    name = args.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise VerbError("validation_failed", "name must be a string")
        if not name:
            name = None  # hard guard: an empty name is a missing name
    i = args.get("id")
    if i is not None and (not isinstance(i, str) or not _UUID_RE.match(i)):
        raise VerbError("validation_failed", "id must be a uuid string")
    if name is None and i is None:
        raise VerbError("validation_failed", "open_item put requires id or name")

    row = None
    if i is not None:
        row = conn.execute("SELECT * FROM open_items WHERE id = ?", (i,)).fetchone()
        if row is None:
            # hard guard: an id that does not resolve to an open item is
            # rejected, with or without a name (creation needs a non-empty
            # name; an id-addressed put never mints).
            raise VerbError("not_found", f"id {i} does not resolve to an open item")
    elif name is not None:
        row = conn.execute("SELECT * FROM open_items WHERE name = ?", (name,)).fetchone()

    state, sets = _oi_validate_fields(conn, args)

    if row is None:
        # Mint: always through a non-empty name.
        oid = new_uuid7()
        final_state = state if state is not None else "open"
        conn.execute(
            "INSERT INTO open_items (id, name, state, text, created, updated, "
            "freeform_current, freeform_pending, freeform_total, "
            "freeform_next, freeform_last, freeform_due, freeform_priority) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, name, final_state,
             sets.get("text"), tx.now, tx.now,
             sets.get("current"), sets.get("pending"), sets.get("total"),
             sets.get("next"), sets.get("last"), sets.get("due"), sets.get("priority")),
        )
        tx.ref(oid, "created")
        # A first appearance is a change: every supplied field mints (SPEC 3.17).
        _rev(tx, oid, "name", None, name)
        _rev(tx, oid, "state", None, final_state)
        for f, v in sets.items():
            _oi_rev(tx, oid, _oi_column(f), None, v)
        final_name, created = name, True
    else:
        oid = row["id"]
        created = False
        changed = []
        renamed = name is not None and row["name"].casefold() != name.casefold()
        if renamed:
            # id-addressed put with a differing name = rename (as chain_put).
            dup = conn.execute(
                "SELECT id FROM open_items WHERE name = ? AND id <> ?", (name, oid)
            ).fetchone()
            if dup is not None:
                raise VerbError("duplicate_name", f"open item name {name!r} is taken (id {dup['id']})")
            changed.append(("name", name))
        if state is not None and row["state"] != state:
            changed.append(("state", state))
        for f, v in sets.items():
            if row[_oi_column(f)] != v:
                changed.append((_oi_column(f), v))
        if changed:
            cols = ", ".join(c + " = ?" for c, _ in changed)
            conn.execute(
                f"UPDATE open_items SET {cols}, updated = ? WHERE id = ?",
                [v for _, v in changed] + [tx.now, oid],
            )
            tx.ref(oid, "modified")
            for col, v in changed:
                _oi_rev(tx, oid, col, row[col], v)
        # A no-op put changes nothing: no 'modified' ref, updated untouched.
        final_name = name if renamed else row["name"]

    fresh = conn.execute("SELECT * FROM open_items WHERE id = ?", (oid,)).fetchone()
    out = _oi_render(fresh)
    if i is None:
        # Name-addressed call (the mint path: fresh mint or found-existing):
        # the created-vs-found marker is ALWAYS present on this path only.
        # It carries the key "created", so on a name-addressed response it
        # stands in for the row's created timestamp (recoverable via get).
        out["created"] = created
    tx.record({"op": "open_item_put", "item": oid, "name": final_name, "created": created})
    return out, f'open_item put "{final_name}"', "open_item"


def _find_open_item(conn, args):
    """open_item delete addressing: id takes precedence (as _find_chain)."""
    i = args.get("id")
    n = args.get("name")
    if i is not None:
        if not isinstance(i, str) or not _UUID_RE.match(i):
            raise VerbError("validation_failed", "id must be a uuid string")
        row = conn.execute("SELECT * FROM open_items WHERE id = ?", (i,)).fetchone()
        if row is None:
            raise VerbError("not_found", f"open item {i} not found")
        if n is not None:
            _need_str(n, "name")
            if row["name"].casefold() != n.casefold():
                raise VerbError(
                    "validation_failed",
                    f"name {n!r} does not match open item {i} (current name {row['name']!r})",
                )
        return row
    if n is not None:
        _need_str(n, "name")
        row = conn.execute("SELECT * FROM open_items WHERE name = ?", (n,)).fetchone()
        if row is None:
            raise VerbError("not_found", f"open item {n!r} not found")
        return row
    raise VerbError("not_found", "open_item delete requires id or name")


def _h_open_item_delete(conn, args, tx):
    row = _find_open_item(conn, args)
    oid, oname = row["id"], row["name"]
    incoming = conn.execute("SELECT * FROM reblades WHERE dst_uuid = ?", (oid,)).fetchall()
    conn.execute("DELETE FROM reblades WHERE src_uuid = ?", (oid,))  # outgoing
    conn.execute("DELETE FROM open_items WHERE id = ?", (oid,))
    tx.ref(oid, "deleted")
    # every non-null content field -> NULL; machine columns (id, created,
    # updated) mint nothing. The rows survive the entity (SPEC 3.17).
    for col in row.keys():
        if col in ("id", "created", "updated"):
            continue
        v = row[col]
        if v is not None:
            _oi_rev(tx, oid, col, v, None)
    tx.record({"op": "open_item_delete", "item": oid, "name": oname})
    return (
        {"id": oid, "name": oname, "incoming": [_reblade_dict(r) for r in incoming]},
        f'open_item delete "{oname}"',
        "open_item",
    )


# ----------------------------------------------------------------- read verbs

# ------------------------------------------------------- read-surface keys (P5)
# SPEC, "Read-surface keys" (the section after 3.17): count / emit_path /
# emit_format on the six read verbs. A result can be counted before it is
# fetched, or spilled to a file instead of the agent's context. No new
# verbs -- the tool surface stays 20.

# The window a fetch renders: the verb's own default (the normal branch,
# limit omitted), no cap (the emit branch, limit omitted), or an explicit
# limit int.
_DEFAULT = object()
_FULL = object()


def _emit_file(emit_path, emit_format, payload, ndjson_records):
    """Write the bare payload to emit_path (atomic temp + os.replace, the
    snapshot pattern). The parent directory must exist. json = one
    document; ndjson = one record per line -- a non-list payload (get's
    record, history's object) is the single line. No envelope, no
    exported_at: provenance lives in the tool response. Returns the bytes
    written."""
    parent = os.path.dirname(emit_path)
    if parent and not os.path.isdir(parent):
        raise VerbError("validation_failed",
                        f"emit_path parent directory does not exist: {parent!r}")
    if ndjson_records:
        rendered = "".join(
            json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
            for rec in payload)
    elif emit_format == "json":
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        rendered = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")) + "\n"
    tmp = f"{emit_path}.tmp-{secrets.token_hex(8)}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(rendered)
        os.replace(tmp, emit_path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise VerbError("db_error", f"emit write failed: {type(e).__name__}: {e}") from None
    return os.path.getsize(emit_path)


def _read_surface(args, fetch, count_total, wrap, ndjson_records=False):
    """Branch one of the six read verbs over the read-surface keys.

    fetch(window) -> (records, count): the bare result-set records for
    the window (_DEFAULT | _FULL | an explicit limit int) plus their
    window-honest count ("would have shown up", including limit/offset/
    window effects) -- a list of records (list / search / reblades_of /
    follow), the single record (get), or the {genesis, revisions, trace}
    object (history). count_total() -> (count, total) for the count
    branch; total is the unwindowed count (the genesis frame is never
    counted; per-verb, by construction). wrap(records) -> the
    normal-branch body: the container verbs wrap their records in their
    one keyed field; get and history are identity. The emit branch
    writes the bare records -- never the wrap, never an envelope.

    count + emit_path = validation_failed (a record set and a count are
    mutually exclusive; the emit receipt already carries count); a lone
    emit_format is validation_failed too. The emit branch is read-only
    against the armory: no log row, no signature, no register, no
    vambrace mutation."""
    count = args.get("count")
    if count is not None and not isinstance(count, bool):
        raise VerbError("validation_failed", "count must be a boolean")
    emit_path = args.get("emit_path")
    if emit_path is not None and (not isinstance(emit_path, str) or not emit_path):
        raise VerbError("validation_failed", "emit_path must be a non-empty string")
    emit_format = args.get("emit_format")
    if emit_format is not None and emit_format not in ("json", "ndjson"):
        raise VerbError(
            "validation_failed",
            "emit_format must be 'json' or 'ndjson' (md/csv stay with furnish "
            "stack renders)")
    if count and emit_path is not None:
        raise VerbError(
            "validation_failed",
            "count and emit_path are mutually exclusive (the emit receipt "
            "already carries count)")
    if emit_format is not None and emit_path is None:
        raise VerbError("validation_failed", "emit_format requires emit_path")
    limit = args.get("limit")
    if limit is not None:
        limit = _check_int(limit, "limit", 1)
    if count:
        c, t = count_total()
        return {"count": c, "total": t}
    if emit_path is not None:
        payload, n_full = fetch(_FULL)
        if limit is None:
            c = n_full
        else:
            payload, c = fetch(limit)
        nbytes = _emit_file(emit_path, emit_format, payload, ndjson_records)
        return {
            "emitted": True,
            "emit_path": emit_path,
            "emit_format": emit_format,
            "count": c,
            "bytes": nbytes,
            "truncated": limit is not None and c < n_full,
        }
    return wrap(fetch(_DEFAULT if limit is None else limit)[0])


def _h_get(conn, args):
    uid = args.get("id")
    if not isinstance(uid, str) or not uid:
        raise VerbError("validation_failed", "id is required")
    if conn.execute("SELECT 1 FROM chain_headers WHERE id = ?", (uid,)).fetchone() is not None:
        rec = _chain_composite(conn, uid)
        size = len(rec["blades"])
    else:
        row = conn.execute(
            "SELECT b.id, b.chain_id, b.text, b.created, b.updated, h.name AS chain_name "
            "FROM chain_blades b JOIN chain_headers h ON h.id = b.chain_id WHERE b.id = ?",
            (uid,),
        ).fetchone()
        if row is not None:
            rec, size = _bare(row), 1
        else:
            rec, size = None, 0
            for table in ("chain_intentions", "open_items", "rulings", "terms"):
                row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (uid,)).fetchone()
                if row is not None:
                    rec, size = _bare(row), 1
                    break

    def fetch(window):
        if rec is None:
            raise VerbError("not_found", f"no tracked entity with id {uid}")
        return rec, size

    def count_total():
        # the count branch answers an unresolved UUID with 0 (not not_found)
        return size, size

    return _read_surface(args, fetch, count_total, lambda p: p,
                         ndjson_records=False)


_SEARCH_SPACES = ("chain", "blade", "intention", "open_item", "ruling", "term", "log")
# space -> (table, value column, name column) probes
_SEARCH_SOURCES = {
    "chain": [("chain_headers", "name", "name")],
    "blade": [("chain_blades", "text", "chain_name")],
    "intention": [("chain_intentions", "name", "name")],
    "open_item": [("open_items", "name", "name"), ("open_items", "text", "name")],
    "ruling": [("rulings", "number", "name"), ("rulings", "name", "name"), ("rulings", "body", "name")],
    "term": [("terms", "term", "term"), ("terms", "text", "term")],
    "log": [("logs", "title", "title"), ("logs", "body", "title")],
}


def _search_rows(conn, space, probes):
    """id -> {"v": matched text, "n": entity name} for one space (OR'd probes)."""
    out = {}
    for table, vcol, ncol in _SEARCH_SOURCES[space]:
        if space == "blade":
            base = (
                f"SELECT b.id AS id, b.{vcol} AS v, h.name AS n "
                "FROM chain_blades b JOIN chain_headers h ON h.id = b.chain_id"
            )
            where = f"lower(b.{vcol}) LIKE lower('%' || ? || '%') ESCAPE '\\'"
        elif ncol == vcol:
            base = f"SELECT id, {vcol} AS v, {vcol} AS n FROM {table}"
            where = f"lower({vcol}) LIKE lower('%' || ? || '%') ESCAPE '\\'"
        else:
            base = f"SELECT id, {vcol} AS v, {ncol} AS n FROM {table}"
            where = f"lower({vcol}) LIKE lower('%' || ? || '%') ESCAPE '\\'"
        for probe in probes:
            for r in conn.execute(base + " WHERE " + where, (_like_escaped(probe),)):
                out.setdefault(r["id"], {"v": r["v"], "n": r["n"]})
    return out


def _window(text, probe, ctx):
    pos = text.casefold().find(probe.casefold())
    if pos < 0:
        return ""
    lo = max(0, pos - ctx)
    hi = min(len(text), pos + len(probe) + ctx)
    return text[lo:hi]


def _h_search(conn, args):
    probes = args.get("probes")
    if (
        not isinstance(probes, list)
        or not probes
        or not all(isinstance(p, str) and p for p in probes)
    ):
        raise VerbError("validation_failed", "probes must be a non-empty array of non-empty strings")
    spaces = args.get("spaces")
    if spaces is None:
        active = list(_SEARCH_SPACES)
    else:
        if not isinstance(spaces, list) or any(s not in _SEARCH_SPACES for s in spaces):
            raise VerbError("validation_failed", f"spaces must be a subset of {list(_SEARCH_SPACES)}")
        active = list(dict.fromkeys(spaces))
    context = args.get("context", 120)
    _check_int(context, "context", 0)
    cache = {}

    def ordered():
        if "hits" not in cache:
            hits = {}
            for space in active:
                for uid, info in _search_rows(conn, space, probes).items():
                    text = info["v"] or ""
                    probe = next((p for p in probes if p.casefold() in text.casefold()), "")
                    hits[(space, uid)] = {
                        "space": space,
                        "id": uid,
                        "name": info["n"] or "",
                        "context": _window(text, probe, context) if probe else "",
                    }
            cache["hits"] = sorted(hits.values(), key=lambda h: (h["space"], h["id"]))
        return cache["hits"]

    def fetch(window):
        if window is _FULL:
            lim = None
        elif window is _DEFAULT:
            lim = 50
        else:
            lim = window
        page = ordered() if lim is None else ordered()[:lim]
        return page, len(page)

    def count_total():
        all_hits = ordered()
        lim = args.get("limit")
        lim = 50 if lim is None else lim
        return len(all_hits[:lim]), len(all_hits)

    return _read_surface(args, fetch, count_total,
                         lambda p: {"hits": p}, ndjson_records=True)


_LIST_SPACES = ("chain", "intention", "open_item", "ruling", "term", "log", "deflected_write")
_LIST_APPLICABLE = {
    "chain": {"intention", "name_prefix"},
    "intention": {"orientation", "name_prefix"},
    "open_item": {"state", "name_prefix"},
    "ruling": {"state", "name_prefix"},
    "term": {"name_prefix"},
    "log": {"uuid", "since", "until"},
    "deflected_write": {"state", "since", "until"},
}


def _h_list(conn, args):
    space = args.get("space")
    if space not in _LIST_SPACES:
        raise VerbError("validation_failed", f"space must be one of {list(_LIST_SPACES)}")
    filt = args.get("filter") or {}
    if not isinstance(filt, dict):
        raise VerbError("validation_failed", "filter must be an object")
    include_blades = bool(args.get("include_blades", False))
    offset = _check_int(args.get("offset", 0), "offset", 0)
    ignored = sorted(k for k in filt if k not in _LIST_APPLICABLE[space])

    where_parts = ["1=1"]
    params = []
    if space == "chain":
        select = "SELECT h.* FROM chain_headers h WHERE "
        from_where = "FROM chain_headers h WHERE "
        order = "ORDER BY h.created, h.id"
        if "intention" in filt:
            where_parts.append(
                "EXISTS (SELECT 1 FROM chain_intention_map m "
                "JOIN chain_intentions s ON s.id = m.intention_id "
                "WHERE m.chain_id = h.id AND s.name = ?)")
            params.append(filt["intention"])
        if "name_prefix" in filt:
            where_parts.append("lower(h.name) LIKE lower(?) ESCAPE '\\'")
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")

        def render(r):
            rec = _bare(r)
            rec["intentions"] = _intention_names(conn, r["id"])
            if include_blades:
                rec["blades"] = [
                    {"id": b["id"], "pos": b["pos"], "text": b["text"]}
                    for b in _ordered_blades(conn, r["id"])
                ]
            return rec
    elif space in ("intention", "open_item", "ruling"):
        table = {"intention": "chain_intentions", "open_item": "open_items", "ruling": "rulings"}[space]
        select = f"SELECT * FROM {table} WHERE "
        from_where = f"FROM {table} WHERE "
        order = "ORDER BY created, id"
        if space == "intention" and "orientation" in filt:
            where_parts.append("orientation = ?")
            params.append(filt["orientation"])
        if "state" in filt and space != "intention":
            where_parts.append("state = ?")
            params.append(filt["state"])
        if "name_prefix" in filt:
            where_parts.append("lower(name) LIKE lower(?) ESCAPE '\\'")
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")
        render = _bare
    elif space == "term":
        select = "SELECT * FROM terms WHERE "
        from_where = "FROM terms WHERE "
        order = "ORDER BY created, id"
        if "name_prefix" in filt:
            where_parts.append("lower(term) LIKE lower(?) ESCAPE '\\'")
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")
        render = _bare
    elif space == "deflected_write":
        select = "SELECT * FROM deflected_writes WHERE "
        from_where = "FROM deflected_writes WHERE "
        order = "ORDER BY id"
        if "state" in filt:
            where_parts.append("state = ?")
            params.append(filt["state"])
        if "since" in filt:
            where_parts.append("created >= ?")
            params.append(_iso_ns(filt["since"], "since"))
        if "until" in filt:
            where_parts.append("created <= ?")
            params.append(_iso_ns(filt["until"], "until"))
        render = _deflected_record
    else:  # log
        select = "SELECT DISTINCT l.* FROM logs l WHERE "
        from_where = "FROM logs l WHERE "
        order = "ORDER BY l.id"
        if "uuid" in filt:
            where_parts.append(
                "EXISTS (SELECT 1 FROM log_refs lr WHERE lr.log_id = l.id AND lr.uuid = ?)")
            params.append(filt["uuid"])
        if "since" in filt:
            where_parts.append("l.created >= ?")
            params.append(_iso_ns(filt["since"], "since"))
        if "until" in filt:
            where_parts.append("l.created <= ?")
            params.append(_iso_ns(filt["until"], "until"))
        render = _bare

    where_sql = " AND ".join(where_parts)

    def fetch(window):
        if window is _FULL:
            lim = None
        elif window is _DEFAULT:
            lim = 50
        else:
            lim = window
        sql = select + where_sql + " " + order
        ps = list(params)
        if lim is not None:
            sql += " LIMIT ? OFFSET ?"
            ps.extend((lim, offset))
        items = [render(r) for r in conn.execute(sql, ps)]
        return items, len(items)

    def wrap(records):
        out = {"items": records, "count": len(records)}
        if ignored:
            out["warning"] = (f"filter keys inapplicable to space "
                              f"'{space}' ignored: {', '.join(ignored)}")
        return out

    def count_total():
        total = conn.execute(
            "SELECT COUNT(*) c " + from_where + where_sql, params).fetchone()["c"]
        lim = args.get("limit")
        lim = 50 if lim is None else lim
        return min(lim, max(0, total - offset)), total

    return _read_surface(args, fetch, count_total, wrap, ndjson_records=True)


def _h_reblades_of(conn, args):
    uid = args.get("id")
    if not isinstance(uid, str) or not uid:
        raise VerbError("validation_failed", "id is required")
    direction = args.get("direction", "both")
    if direction not in ("in", "out", "both"):
        raise VerbError("validation_failed", "direction must be 'in', 'out', or 'both'")
    kind = args.get("kind")
    if kind is not None:
        _check_kind(conn, kind)
    rows = []
    for col in (["src_uuid", "dst_uuid"] if direction == "both" else [direction == "out" and "src_uuid" or "dst_uuid"]):
        sql = f"SELECT * FROM reblades WHERE {col} = ?"
        params = [uid]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        rows.extend(conn.execute(sql, params))
    seen = set()
    edges = []
    for r in rows:
        key = (r["src_uuid"], r["dst_uuid"], r["kind"])
        if key in seen:
            continue
        seen.add(key)
        s = _resolve_endpoint(conn, r["src_uuid"])
        d = _resolve_endpoint(conn, r["dst_uuid"])
        e = _reblade_dict(r)
        e["src_space"] = s["space"] if s else None
        e["src_name"] = s["name"] if s else None
        e["dst_space"] = d["space"] if d else None
        e["dst_name"] = d["name"] if d else None
        edges.append(e)

    def fetch(window):
        return edges, len(edges)

    def count_total():
        # no limit today, so count = total
        return len(edges), len(edges)

    return _read_surface(args, fetch, count_total,
                         lambda p: {"edges": p}, ndjson_records=True)


def _neighbors(conn, u, direction, kinds):
    """(neighbor uuid, edge kind) pairs one hop from u."""
    if kinds is not None and not kinds:
        return []
    out = []
    kind_sql = ""
    params = []
    if kinds is not None:
        kind_sql = " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        params = list(kinds)
    if direction in ("out", "both"):
        for r in conn.execute(
            f"SELECT dst_uuid, kind FROM reblades WHERE src_uuid = ?{kind_sql}", (u, *params)
        ):
            out.append((r["dst_uuid"], r["kind"]))
    if direction in ("in", "both"):
        for r in conn.execute(
            f"SELECT src_uuid, kind FROM reblades WHERE dst_uuid = ?{kind_sql}", (u, *params)
        ):
            out.append((r["src_uuid"], r["kind"]))
    return out


def _h_follow(conn, args):
    start = args.get("from")
    if not isinstance(start, str) or not start:
        raise VerbError("validation_failed", "from is required")
    if _resolve_endpoint(conn, start) is None:
        raise VerbError("not_found", f"start {start} does not resolve in any entity space")
    direction = args.get("direction", "out")
    if direction not in ("out", "in", "both"):
        raise VerbError("validation_failed", "direction must be 'out', 'in', or 'both'")
    kinds = args.get("kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
            raise VerbError("validation_failed", "kinds must be an array of kind strings")
    max_hops = args.get("max_hops", 2)
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 1 or max_hops > 5:
        raise VerbError("validation_failed", "max_hops must be an integer between 1 and 5")
    expand = bool(args.get("expand_chains", False))

    def _node(uuid, depth, edge_kind):
        res = _resolve_endpoint(conn, uuid)
        n = {
            "uuid": uuid,
            "name": res["name"] if res else None,
            "space": res["space"] if res else None,
            "depth": depth,
            "edge_kind": edge_kind,
        }
        if expand and res is not None and res["space"] == "chain":
            n["blades"] = [b["id"] for b in _ordered_blades(conn, uuid)]
        return n

    nodes = [_node(start, 0, None)]
    visited = {start}
    frontier = [start]
    for depth in range(1, max_hops + 1):
        nxt = []
        for u in frontier:
            for v, ek in _neighbors(conn, u, direction, kinds):
                if v not in visited:  # cycle-pruned: first visit wins
                    visited.add(v)
                    nodes.append(_node(v, depth, ek))
                    nxt.append(v)
        frontier = nxt

    def fetch(window):
        return nodes, len(nodes)

    def count_total():
        # no window today, so count = total
        return len(nodes), len(nodes)

    return _read_surface(args, fetch, count_total,
                         lambda p: {"nodes": p}, ndjson_records=True)


def _h_history(conn, args):
    """history (SPEC 3.17): the full {genesis, revisions, trace} shape.

    genesis = the sidecar's one-row birth frame (or null for a pre-build
    vambrace; a frame, present even for dissolved entities, and never
    counted by count / total). revisions = the limit-governed page of
    field rows (default 20), newest first, each joined with its log's
    resolved op + title; orphan rows (a log_id that no longer resolves)
    are excluded by the inner join. trace = the FULL log rows (op, title,
    created, body, refs) touching the UUID via log_refs: the same time
    window as the revisions page when the page is non-empty, else default
    20 -- a log row with no revisions in the window is a trace-only entry
    (a gap). No live state in the response.

    Bare-connection degradation (P4): with the sidecar not attached,
    genesis is null and revisions empty, and the trace is the plain
    log_refs query."""
    uid = args.get("id")
    if not isinstance(uid, str) or not uid:
        raise VerbError("validation_failed", "id is required")
    attached = _hist_attached(conn)
    if attached:
        g = conn.execute(
            "SELECT kind, detail, log_id, created FROM hist.genesis "
            "ORDER BY created LIMIT 1").fetchone()
        genesis = None if g is None else {
            "kind": g["kind"],
            "detail": g["detail"],
            "log_id": g["log_id"],
            "created": ns_to_iso(g["created"]),
        }
    else:
        genesis = None

    def _revisions(limit):
        if not attached:
            return []
        sql = (
            "SELECT r.field AS field, r.old_val AS old, r.new_val AS new, "
            "r.created AS created, lr.op AS op, l.title AS title "
            "FROM hist.revisions r "
            "JOIN logs l ON l.id = r.log_id "
            "JOIN log_refs lr ON lr.log_id = l.id AND lr.uuid = r.uuid "
            "WHERE r.uuid = ? ORDER BY r.created DESC, r.id DESC")
        ps = [uid]
        if limit is not None:
            sql += " LIMIT ?"
            ps.append(limit)
        return conn.execute(sql, ps).fetchall()

    def _trace(rows):
        if rows:
            lo = min(r["created"] for r in rows)
            hi = max(r["created"] for r in rows)
            trows = conn.execute(
                "SELECT l.id AS id, l.title AS title, l.created AS created, "
                "l.body AS body, lr.op AS op "
                "FROM log_refs lr JOIN logs l ON l.id = lr.log_id "
                "WHERE lr.uuid = ? AND l.created BETWEEN ? AND ? "
                "ORDER BY l.id DESC",
                (uid, lo, hi)).fetchall()
        else:
            trows = conn.execute(
                "SELECT l.id AS id, l.title AS title, l.created AS created, "
                "l.body AS body, lr.op AS op "
                "FROM log_refs lr JOIN logs l ON l.id = lr.log_id "
                "WHERE lr.uuid = ? ORDER BY l.id DESC LIMIT 20",
                (uid,)).fetchall()
        trace = []
        for r in trows:
            refs = [
                {"uuid": x["uuid"], "op": x["op"]}
                for x in conn.execute(
                    "SELECT uuid, op FROM log_refs WHERE log_id = ? ORDER BY uuid",
                    (r["id"],))
            ]
            trace.append({
                "op": r["op"],
                "title": r["title"],
                "created": ns_to_iso(r["created"]),
                "body": r["body"],
                "refs": refs,
            })
        return trace

    def fetch(window):
        if window is _FULL:
            lim = None
        elif window is _DEFAULT:
            lim = 20
        else:
            lim = window
        rows = _revisions(lim)
        payload = {
            "genesis": genesis,
            "revisions": [
                {
                    "field": r["field"],
                    "old": r["old"],
                    "new": r["new"],
                    "created": ns_to_iso(r["created"]),
                    "op": r["op"],
                    "title": r["title"],
                }
                for r in rows
            ],
            "trace": _trace(rows),
        }
        return payload, len(rows)

    def count_total():
        lim = args.get("limit")
        lim = 20 if lim is None else lim
        rows = _revisions(lim)
        total = (
            conn.execute(
                "SELECT COUNT(*) c FROM hist.revisions r "
                "JOIN logs l ON l.id = r.log_id "
                "JOIN log_refs lr ON lr.log_id = l.id AND lr.uuid = r.uuid "
                "WHERE r.uuid = ?",
                (uid,)).fetchone()["c"]
            if attached else 0)
        return len(rows), total

    return _read_surface(args, fetch, count_total, lambda p: p,
                         ndjson_records=False)


# ---------------------------------------------------------- deflected writes

def _deflected_record(row):
    """A deflected_writes row as a bare record: NULLs omitted, ns rendered
    ISO-8601 at the surface, args re-parsed from canonical JSON to an object."""
    rec = {}
    for k in row.keys():
        v = row[k]
        if v is None:
            continue
        if k in ("created", "cleared_at"):
            v = ns_to_iso(v)
        elif k == "args":
            try:
                v = json.loads(v)
            except (TypeError, ValueError):
                pass  # leave the raw canonical string (should never happen)
        rec[k] = v
    return rec


def _h_deflected_write(conn, args, tx):
    """deflected_write: erase from the deflected-writes cache (20th tool).

    action=clear: exactly one of id (a single row) or group_id (a whole
    deflected batch). Idempotent on already-cleared rows (the rows are
    returned, no error); not_found if no row matches at all. The cache is
    MEMORY, not an execution queue (SPEC deflected_writes 6): re-issue
    deflected writes deliberately — replay is not a verb, and no target means
    no implicit bulk wipe.
    """
    action = args.get("action")
    if action != "clear":
        raise VerbError("validation_failed", f"deflected_write action must be 'clear', got {action!r}")
    row_id = args.get("id")
    group_id = args.get("group_id")
    if row_id is not None and (not isinstance(row_id, int) or isinstance(row_id, bool) or row_id < 1):
        raise VerbError("validation_failed", "id must be an integer >= 1")
    if group_id is not None and (not isinstance(group_id, str) or not group_id):
        raise VerbError("validation_failed", "group_id must be a uuid string")
    if (row_id is None) == (group_id is None):
        raise VerbError("validation_failed", "deflected_write clear requires exactly one of id or group_id")
    if row_id is not None:
        cond, where = "id = ?", (row_id,)
        target = f"id {row_id}"
    else:
        cond, where = "group_id = ?", (group_id,)
        target = f"group {group_id}"
    any_rows = conn.execute(f"SELECT * FROM deflected_writes WHERE {cond}", where).fetchall()
    if not any_rows:
        raise VerbError("not_found", f"no deflected write matches {target}")
    pending = [r for r in any_rows if r["state"] == "pending"]
    if pending:
        conn.execute(
            f"UPDATE deflected_writes SET state = 'cleared', cleared_at = ? "
            f"WHERE state = 'pending' AND {cond}",
            (tx.now, *where),
        )
        any_rows = conn.execute(f"SELECT * FROM deflected_writes WHERE {cond}", where).fetchall()
    n = len(any_rows)
    tx.record({"op": "deflected_write_clear", "count": n, "id": row_id, "group_id": group_id})
    return (
        {"cleared": [_deflected_record(r) for r in any_rows], "count": n},
        f"deflected_write clear {n} row(s)",
        "deflected_writes",
    )


# ------------------------------------------------------------------ dispatch

_WRITE_DISPATCH = {
    "chain_put": _h_chain_put,
    "chain_delete": _h_chain_delete,
    "chain_split": _h_chain_split,
    "chain_merge": _h_chain_merge,
    "blade_insert": _h_blade_insert,
    "blade_edit": _h_blade_edit,
    "blade_move": _h_blade_move,
    "blade_delete": _h_blade_delete,
    "reblade_add": _h_reblade_add,
    "reblade_remove": _h_reblade_remove,
    "batch": _h_batch,
    "open_item": _h_open_item,
    "deflected_write": _h_deflected_write,
}

_READ_HANDLERS = {
    "get": _h_get,
    "search": _h_search,
    "list": _h_list,
    "reblades_of": _h_reblades_of,
    "follow": _h_follow,
    "history": _h_history,
}

# 13 write + 6 read = 19 verbs
ALL_VERBS = tuple(_WRITE_DISPATCH) + tuple(_READ_HANDLERS)


def execute(conn, verb, args):
    """Dispatch one of the 19 SPEC 3 verbs. Pure JSON-serializable dict, or VerbError."""
    if not isinstance(args, dict):
        args = {}
    read = _READ_HANDLERS.get(verb)
    if read is not None:
        try:
            return read(conn, args)
        except sqlite3.Error as e:
            raise VerbError("db_error", str(e)) from None
    if verb in _WRITE_DISPATCH:
        return _run_write(_WRITE_DISPATCH[verb], conn, args, verb)
    raise VerbError("not_found", f"unknown verb: {verb!r}")
