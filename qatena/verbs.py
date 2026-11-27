"""The 17-verb surface of SPEC 3, behind one dispatch.

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
    """Scratch state of one write transaction: log refs + body records."""

    def __init__(self, now):
        self.now = now
        self.refs = {}
        self.records = []

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


def _run_write(handler, conn, args):
    tx = _Tx(now_ns())
    _begin(conn)
    try:
        result, title, area = handler(conn, args, tx)
        log_id = _mint_log(conn, tx, title, area)
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
        final_name = name if renamed else row["name"]
    if intentions is not None:
        _apply_intentions(conn, tx, cid, intentions)
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
    blade_ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM chain_blades WHERE chain_id = ?", (cid,))
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
    for b in blade_ids:
        tx.ref(b, "deleted")
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
            raise VerbError(e.code, f"op {i} ({name}): {e.message}") from None
        results.append(res)
    return {"ops": results}, f"batch ({len(ops)} ops)", "mixed"


# ----------------------------------------------------------------- read verbs

def _h_get(conn, args):
    uid = args.get("id")
    if not isinstance(uid, str) or not uid:
        raise VerbError("validation_failed", "id is required")
    if conn.execute("SELECT 1 FROM chain_headers WHERE id = ?", (uid,)).fetchone() is not None:
        return _chain_composite(conn, uid)
    row = conn.execute(
        "SELECT b.id, b.chain_id, b.text, b.created, b.updated, h.name AS chain_name "
        "FROM chain_blades b JOIN chain_headers h ON h.id = b.chain_id WHERE b.id = ?",
        (uid,),
    ).fetchone()
    if row is not None:
        return _bare(row)
    for table in ("chain_intentions", "open_items", "rulings", "terms"):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (uid,)).fetchone()
        if row is not None:
            return _bare(row)
    raise VerbError("not_found", f"no tracked entity with id {uid}")


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
    limit = args.get("limit", 50)
    _check_int(limit, "limit", 1)
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
    ordered = sorted(hits.values(), key=lambda h: (h["space"], h["id"]))
    return {"hits": ordered[:limit]}


_LIST_SPACES = ("chain", "intention", "open_item", "ruling", "term", "log")
_LIST_APPLICABLE = {
    "chain": {"intention", "name_prefix"},
    "intention": {"orientation", "name_prefix"},
    "open_item": {"state", "name_prefix"},
    "ruling": {"state", "name_prefix"},
    "term": {"name_prefix"},
    "log": {"uuid", "since", "until"},
}


def _h_list(conn, args):
    space = args.get("space")
    if space not in _LIST_SPACES:
        raise VerbError("validation_failed", f"space must be one of {list(_LIST_SPACES)}")
    filt = args.get("filter") or {}
    if not isinstance(filt, dict):
        raise VerbError("validation_failed", "filter must be an object")
    include_blades = bool(args.get("include_blades", False))
    limit = _check_int(args.get("limit", 50), "limit", 1)
    offset = _check_int(args.get("offset", 0), "offset", 0)
    ignored = sorted(k for k in filt if k not in _LIST_APPLICABLE[space])

    items = []
    if space == "chain":
        sql = "SELECT h.* FROM chain_headers h WHERE 1=1"
        params = []
        if "intention" in filt:
            sql += (
                " AND EXISTS (SELECT 1 FROM chain_intention_map m "
                "JOIN chain_intentions s ON s.id = m.intention_id "
                "WHERE m.chain_id = h.id AND s.name = ?)"
            )
            params.append(filt["intention"])
        if "name_prefix" in filt:
            sql += " AND lower(h.name) LIKE lower(?) ESCAPE '\\'"
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")
        sql += " ORDER BY h.created, h.id LIMIT ? OFFSET ?"
        for r in conn.execute(sql, (*params, limit, offset)):
            rec = _bare(r)
            rec["intentions"] = _intention_names(conn, r["id"])
            if include_blades:
                rec["blades"] = [
                    {"id": b["id"], "pos": b["pos"], "text": b["text"]}
                    for b in _ordered_blades(conn, r["id"])
                ]
            items.append(rec)
    elif space in ("intention", "open_item", "ruling"):
        table = {"intention": "chain_intentions", "open_item": "open_items", "ruling": "rulings"}[space]
        sql = f"SELECT * FROM {table} WHERE 1=1"
        params = []
        if space == "intention" and "orientation" in filt:
            sql += " AND orientation = ?"
            params.append(filt["orientation"])
        if "state" in filt and space != "intention":
            sql += " AND state = ?"
            params.append(filt["state"])
        if "name_prefix" in filt:
            sql += " AND lower(name) LIKE lower(?) ESCAPE '\\'"
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")
        sql += " ORDER BY created, id LIMIT ? OFFSET ?"
        items = [_bare(r) for r in conn.execute(sql, (*params, limit, offset))]
    elif space == "term":
        sql = "SELECT * FROM terms WHERE 1=1"
        params = []
        if "name_prefix" in filt:
            sql += " AND lower(term) LIKE lower(?) ESCAPE '\\'"
            params.append(_like_escaped(str(filt["name_prefix"])) + "%")
        sql += " ORDER BY created, id LIMIT ? OFFSET ?"
        items = [_bare(r) for r in conn.execute(sql, (*params, limit, offset))]
    else:  # log
        sql = "SELECT DISTINCT l.* FROM logs l WHERE 1=1"
        params = []
        if "uuid" in filt:
            sql += " AND EXISTS (SELECT 1 FROM log_refs lr WHERE lr.log_id = l.id AND lr.uuid = ?)"
            params.append(filt["uuid"])
        if "since" in filt:
            sql += " AND l.created >= ?"
            params.append(_iso_ns(filt["since"], "since"))
        if "until" in filt:
            sql += " AND l.created <= ?"
            params.append(_iso_ns(filt["until"], "until"))
        sql += " ORDER BY l.id LIMIT ? OFFSET ?"
        items = [_bare(r) for r in conn.execute(sql, (*params, limit, offset))]

    result = {"items": items, "count": len(items)}
    if ignored:
        result["warning"] = f"filter keys inapplicable to space '{space}' ignored: {', '.join(ignored)}"
    return result


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
    return {"edges": edges}


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
    return {"nodes": nodes}


def _h_history(conn, args):
    uid = args.get("id")
    if not isinstance(uid, str) or not uid:
        raise VerbError("validation_failed", "id is required")
    limit = _check_int(args.get("limit", 20), "limit", 1)
    rows = conn.execute(
        "SELECT l.id AS log_id, lr.op AS op, l.title AS title, l.created AS created "
        "FROM log_refs lr JOIN logs l ON l.id = lr.log_id "
        "WHERE lr.uuid = ? ORDER BY l.id DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    return {
        "entries": [
            {"log_id": r["log_id"], "op": r["op"], "title": r["title"], "created": ns_to_iso(r["created"])}
            for r in rows
        ],
        "trace_level": "coarse",
    }


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
}

_READ_HANDLERS = {
    "get": _h_get,
    "search": _h_search,
    "list": _h_list,
    "reblades_of": _h_reblades_of,
    "follow": _h_follow,
    "history": _h_history,
}

# 11 write + 6 read = 17 verbs
ALL_VERBS = tuple(_WRITE_DISPATCH) + tuple(_READ_HANDLERS)


def execute(conn, verb, args):
    """Dispatch one of the 17 SPEC 3 verbs. Pure JSON-serializable dict, or VerbError."""
    if not isinstance(args, dict):
        args = {}
    read = _READ_HANDLERS.get(verb)
    if read is not None:
        try:
            return read(conn, args)
        except sqlite3.Error as e:
            raise VerbError("db_error", str(e)) from None
    if verb in _WRITE_DISPATCH:
        return _run_write(_WRITE_DISPATCH[verb], conn, args)
    raise VerbError("not_found", f"unknown verb: {verb!r}")
