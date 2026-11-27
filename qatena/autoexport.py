"""Autoexport: stack computation, content-only signatures, renderers, spawn.

A register (autoexport_registers row) names a *stack* — the root plus its
descendants over section -> chain -> blade only — and a destination file +
format. The server's post-commit hook compares the current stack signature
against the stored last_sig; on change it spawns a detached exporter child
(``python -m qatena.autoexport <vault> <root> <path> <format>``) that
re-reads committed truth and atomically rewrites the file (SPEC 3.18).

The child always exports the latest committed state — no data passes over
IPC. It opens the vault READ-ONLY, renders to a temp file, os.replace()s it
into place, then runs exactly one small read-write transaction (store
last_sig/last_emitted + mint the export log row). A failed export leaves
last_sig stale, so the next changed write re-triggers: self-healing with no
queue or retry bookkeeping.

Test seam: env QATENA_EXPORT_SYNC=1 on the server makes the hook call
``export`` inline (same code path, no spawn) so tests can assert on files
deterministically. Never set in live sessions.
"""

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys

from . import db

ALLOWED_FORMATS = ("md", "csv", "json", "ndjson")
POINTER_FIDELITIES = ("none", "in", "out", "both")


# ------------------------------------------------------------- stack compute

def _chain_stack(conn, chain_id):
    """One chain entry: header + sorted section names + blades in pos order."""
    h = conn.execute(
        "SELECT id, name FROM chain_headers WHERE id = ?", (chain_id,)
    ).fetchone()
    secs = [
        r["name"]
        for r in conn.execute(
            "SELECT s.name FROM chain_section_map m "
            "JOIN chain_sections s ON s.id = m.section_id "
            "WHERE m.chain_id = ? ORDER BY s.name COLLATE NOCASE",
            (chain_id,),
        )
    ]
    blades = [
        {"id": b["id"], "pos": b["pos"], "text": b["text"]}
        for b in conn.execute(
            "SELECT b.id AS id, b.text AS text, o.pos AS pos "
            "FROM chain_blades b "
            "JOIN blade_order o ON o.chain_id = b.chain_id AND o.blade_id = b.id "
            "WHERE b.chain_id = ? ORDER BY o.pos",
            (chain_id,),
        )
    ]
    return {"id": h["id"], "name": h["name"], "sections": secs, "blades": blades}


def _endpoint_set(payload):
    """Endpoint set E for a computed stack (SPEC 3.18): the root uuid itself
    (section, chain, and blade roots all count — sections are a valid pointer
    endpoint space per SPEC 1.7) + every chain uuid + every blade uuid."""
    e = {payload["root"]["uuid"]}
    for ch in payload["stack"]["chains"]:
        e.add(ch["id"])
        for b in ch["blades"]:
            e.add(b["id"])
    for b in payload["stack"]["blades"]:
        e.add(b["id"])
    return e


def _pointer_layer(conn, payload, fidelity):
    """The register's pointer layer P, filtered by mode (SPEC 3.18).

    One query over `pointers`; skipped entirely for `none`:
      none -> []
      out  -> rows where src_uuid IN E
      in   -> rows where dst_uuid IN E
      both -> rows where src_uuid IN E OR dst_uuid IN E
    Each row appears exactly once, as {"src","dst","kind"} plus "note" only
    when non-NULL, sorted by (src, dst, kind, note or "").
    """
    if fidelity == "none":
        return []
    e = _endpoint_set(payload)
    ph = ",".join("?" * len(e))
    if fidelity == "out":
        sql = f"SELECT src_uuid, dst_uuid, kind, note FROM pointers WHERE src_uuid IN ({ph})"
        args = list(e)
    elif fidelity == "in":
        sql = f"SELECT src_uuid, dst_uuid, kind, note FROM pointers WHERE dst_uuid IN ({ph})"
        args = list(e)
    else:  # both
        sql = (f"SELECT src_uuid, dst_uuid, kind, note FROM pointers "
               f"WHERE src_uuid IN ({ph}) OR dst_uuid IN ({ph})")
        args = list(e) * 2
    rows = conn.execute(sql, args).fetchall()
    layer = []
    for r in rows:
        d = {"src": r["src_uuid"], "dst": r["dst_uuid"], "kind": r["kind"]}
        if r["note"] is not None:
            d["note"] = r["note"]
        layer.append(d)
    layer.sort(key=lambda d: (d["src"], d["dst"], d["kind"], d.get("note", "")))
    return layer


def compute_stack(conn, root_uuid, fidelity="out"):
    """Compute the stack rooted at root_uuid, or None if it does not resolve.

    Stack definition (SPEC 3.18):
      root = section: the section + every member chain + every blade of those
      root = chain:   the chain + its blades (in blade_order)
      root = blade:   the blade itself (a leaf; its chain is shown as
                      presentation context only, never part of the signature)

    fidelity (none | in | out | both) selects the pointer layer P carried in
    the payload (key "pointers", counted in "counts"["pointers"], and signed
    into the stack signature).
    """
    if fidelity not in POINTER_FIDELITIES:
        raise ValueError(
            f"unknown pointer_fidelity {fidelity!r} (allowed: {list(POINTER_FIDELITIES)})"
        )
    base = _compute_stack_base(conn, root_uuid)
    if base is None:
        return None
    base["pointer_fidelity"] = fidelity
    base["pointers"] = _pointer_layer(conn, base, fidelity)
    base["counts"]["pointers"] = len(base["pointers"])
    return base


def _compute_stack_base(conn, root_uuid):
    """The stack payload without the pointer layer (see compute_stack)."""
    row = conn.execute(
        "SELECT id, name FROM chain_sections WHERE id = ?", (root_uuid,)
    ).fetchone()
    if row is not None:
        chain_ids = [
            r["chain_id"]
            for r in conn.execute(
                "SELECT chain_id FROM chain_section_map WHERE section_id = ? "
                "ORDER BY chain_id",
                (root_uuid,),
            )
        ]
        chains = [_chain_stack(conn, cid) for cid in chain_ids]
        return {
            "root": {"uuid": row["id"], "space": "section", "name": row["name"]},
            "stack": {
                "sections": [{"id": row["id"], "name": row["name"]}],
                "chains": chains,
                "blades": [],
            },
            "counts": {
                "chains": len(chains),
                "blades": sum(len(c["blades"]) for c in chains),
            },
        }

    row = conn.execute(
        "SELECT id, name FROM chain_headers WHERE id = ?", (root_uuid,)
    ).fetchone()
    if row is not None:
        chain = _chain_stack(conn, row["id"])
        return {
            "root": {"uuid": row["id"], "space": "chain", "name": row["name"]},
            "stack": {"sections": [], "chains": [chain], "blades": []},
            "counts": {"chains": 1, "blades": len(chain["blades"])},
        }

    row = conn.execute(
        "SELECT b.id AS id, b.text AS text, b.chain_id AS chain_id, "
        "o.pos AS pos, h.name AS chain_name "
        "FROM chain_blades b "
        "JOIN blade_order o ON o.chain_id = b.chain_id AND o.blade_id = b.id "
        "JOIN chain_headers h ON h.id = b.chain_id "
        "WHERE b.id = ?",
        (root_uuid,),
    ).fetchone()
    if row is not None:
        blade = {
            "id": row["id"],
            "pos": row["pos"],
            "text": row["text"],
            "chain_id": row["chain_id"],
            "chain_name": row["chain_name"],
        }
        # root name = the blade's own text: the blade carries no alias
        return {
            "root": {"uuid": row["id"], "space": "blade", "name": row["text"]},
            "stack": {"sections": [], "chains": [], "blades": [blade]},
            "counts": {"chains": 0, "blades": 1},
        }

    return None


# --------------------------------------------------------------- signatures

def stack_signature(payload):
    """Stable content-only hash over the ordered stack + pointer layer.

    (root uuid, root space, root name, MODE) + per chain (uuid, name, sorted
    section names, [(blade uuid, text)] in pos order) + the sorted pointer
    rows. The mode is deliberately signed so that changing the mode on a
    re-register re-exports. Excluded: timestamps, logs, section
    orientation/note.
    """
    root = payload["root"]
    parts = [root["uuid"], root["space"], root["name"], payload["pointer_fidelity"]]
    for ch in payload["stack"]["chains"]:
        parts.append([
            "chain", ch["id"], ch["name"], ch["sections"],
            [[b["id"], b["text"]] for b in ch["blades"]],
        ])
    for b in payload["stack"]["blades"]:
        parts.append(["blade", b["id"], b["text"]])
    parts.append(payload["pointers"])  # already sorted (src, dst, kind, note or "")
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- renderers

def _render_json(payload, exported_at):
    doc = {
        "root": payload["root"],
        "exported_at": exported_at,
        "pointer_fidelity": payload["pointer_fidelity"],
        "pointers": payload["pointers"],
        "stack": {
            "sections": payload["stack"]["sections"],
            "chains": payload["stack"]["chains"],
            "blades": payload["stack"]["blades"],
        },
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _ndjson_lines(payload):
    lines = []
    for s in payload["stack"]["sections"]:
        lines.append({"space": "section", "uuid": s["id"], "name": s["name"]})
    for ch in payload["stack"]["chains"]:
        lines.append({"space": "chain", "uuid": ch["id"], "name": ch["name"]})
        for b in ch["blades"]:
            lines.append({
                "space": "blade", "uuid": b["id"], "name": ch["name"],
                "pos": b["pos"], "text": b["text"],
            })
    for b in payload["stack"]["blades"]:
        lines.append({
            "space": "blade", "uuid": b["id"], "name": b["chain_name"],
            "pos": b["pos"], "text": b["text"],
        })
    for r in payload["pointers"]:
        line = {"space": "pointer", "src": r["src"], "dst": r["dst"],
                "kind": r["kind"]}
        if "note" in r:
            line["note"] = r["note"]
        lines.append(line)
    return lines


def _render_ndjson(payload, exported_at):
    return "".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        for line in _ndjson_lines(payload)
    )


def _render_csv(payload, exported_at):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["space", "uuid", "name", "src", "dst", "kind",
                     "pos", "text", "note"])
    for line in _ndjson_lines(payload):
        if line["space"] == "pointer":
            writer.writerow([line["space"], "", "", line["src"], line["dst"],
                             line["kind"], "", "", line.get("note", "")])
        else:
            writer.writerow([line["space"], line["uuid"], line["name"],
                             "", "", "", line.get("pos", ""),
                             line.get("text", ""), ""])
    return buf.getvalue()


def _md_out_lines(payload, uuid):
    """The md comment lines for one entity's OUTGOING pointers (modes
    out/both only). Rows are already sorted, so per-src order is stable."""
    if payload["pointer_fidelity"] not in ("out", "both"):
        return []
    lines = []
    for r in payload["pointers"]:
        if r["src"] == uuid:
            lines.append(f"<!-- {r['kind']} -> {r['dst']} -->")
            if "note" in r:
                lines.append(f"<!-- note: {r['note']} -->")
    return lines


def _md_inbound_rows(payload):
    """The md Inbound block rows (modes in/both only). A row whose src is
    already in the stack is rendered exactly once under its src owner
    (out/both), so it is excluded here; mode `in` keeps every row."""
    if payload["pointer_fidelity"] not in ("in", "both"):
        return []
    e = _endpoint_set(payload)
    if payload["pointer_fidelity"] == "in":
        return list(payload["pointers"])
    return [r for r in payload["pointers"]
            if r["dst"] in e and r["src"] not in e]


def _chain_block(out, ch, top_level, md_out):
    """One chain's lines; top-level chains are '##', the root chain itself
    renders directly under the root heading."""
    if top_level:
        out.extend(md_out(ch["id"]))
    else:
        out.append("")
        out.append(f"## {ch['name']}")
        out.append(f"<!-- uuid: {ch['id']} -->")
        out.extend(md_out(ch["id"]))
    if ch["sections"]:
        out.append("*sections: " + ", ".join(ch["sections"]) + "*")
    for b in ch["blades"]:
        out.append(f"- {b['text']} <!-- uuid: {b['id']} -->")
        out.extend(md_out(b["id"]))


def _render_md(payload, exported_at):
    root = payload["root"]
    out = [f"# {root['name']}", f"<!-- uuid: {root['uuid']} -->"]
    if root["space"] == "section":
        # the section root line is the section's own line; a chain root's
        # outgoing pointers render at the top-level chain block, a blade
        # root's at its blade line below
        out.extend(_md_out_lines(payload, root["uuid"]))
    # stack.sections is non-empty only for a section root, where it is the
    # root itself — already the root heading above
    for ch in payload["stack"]["chains"]:
        _chain_block(out, ch,
                     top_level=(root["space"] == "chain" and ch["id"] == root["uuid"]),
                     md_out=lambda uid: _md_out_lines(payload, uid))
    for b in payload["stack"]["blades"]:
        # presentation context only — not part of the signature (SPEC 3.18)
        out.append("")
        out.append(f"*chain: {b['chain_name']}*")
        out.append(f"- {b['text']} <!-- uuid: {b['id']} pos: {b['pos']} -->")
        out.extend(_md_out_lines(payload, b["id"]))
    inbound = _md_inbound_rows(payload)
    if inbound:
        out.append("")
        out.append("## Inbound")
        for r in inbound:
            out.append(f"- {r['src']} [{r['kind']}] -> {r['dst']}")
            if "note" in r:
                out.append(f"<!-- note: {r['note']} -->")
    return "\n".join(out) + "\n"


_RENDERERS = {
    "json": _render_json,
    "ndjson": _render_ndjson,
    "csv": _render_csv,
    "md": _render_md,
}


def render(payload, emit_format, exported_at):
    """Render a computed stack for export. Raises ValueError on bad format."""
    if emit_format not in _RENDERERS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})"
        )
    return _RENDERERS[emit_format](payload, exported_at)


# -------------------------------------------------------------- export flow

def open_readonly(vault_path):
    """Open a vault file read-only (URI mode=ro); the child never writes reads."""
    path = os.path.abspath(vault_path)
    if os.name == "nt":
        # forward slashes keep the URI parse unambiguous on Windows
        path = path.replace("\\", "/")
    uri = "file:" + path + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, autocommit=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def write_export(conn, root_uuid, emit_path, emit_format, fidelity):
    """Render one export and atomically write it to ``emit_path``.

    The one-shot path (the vambrace ``export`` action): takes an OPEN
    read-only connection, computes the stack, renders, and writes the file
    with the same temp + os.replace mechanism as ``export``. No database
    writes happen here at all -- the vault is never mutated.

    Returns ``(exported_at, counts)`` (counts = the payload's
    ``{"chains", "blades", "pointers"}`` dict) or ``None`` if the stack does
    not resolve (root is not a live section / chain / blade).
    """
    payload = compute_stack(conn, root_uuid, fidelity)
    if payload is None:
        return None
    now = db.now_ns()
    exported_at = db.ns_to_iso(now)
    rendered = render(payload, emit_format, exported_at)
    tmp = f"{emit_path}.tmp-{secrets.token_hex(8)}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(rendered)
        os.replace(tmp, emit_path)  # atomic: a crash never leaves a torn file
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return exported_at, payload["counts"]


def export(vault_path, root_uuid, emit_path, emit_format):
    """Run one full export. Raises on failure — the caller self-heals.

    0. re-read this register's own row (root_uuid, emit_path) from the
       read-only connection; if it is absent (deregistered in flight),
       return a no-op WITHOUT writing the file or minting a log;
    1. re-read committed state READ-ONLY, compute stack + signature (using
       the register row's pointer_fidelity);
    2. write_export: re-derive the same committed stack, render, and
       atomically write <emit_path>.tmp-<random> -> os.replace (no DB
       writes in that step);
    3. one small RW transaction: store last_sig/last_emitted + mint the
       export log row (area "autoexport", root ref op "referenced").

    Returns {"exported_at": iso, "sig": str, "log_id": int} or a no-op
    {"exported_at": None, "sig": None, "log_id": None, "noop": ...}.
    """
    if emit_format not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})"
        )
    ro = open_readonly(vault_path)
    try:
        reg = ro.execute(
            "SELECT pointer_fidelity FROM autoexport_registers "
            "WHERE root_uuid = ? AND emit_path = ?",
            (root_uuid, emit_path),
        ).fetchone()
        if reg is None:
            # deregistered in flight: nothing to do, no file write, no log
            return {"exported_at": None, "sig": None, "log_id": None,
                    "noop": "deregistered"}
        fidelity = reg["pointer_fidelity"]
        payload = compute_stack(ro, root_uuid, fidelity)
        if payload is None:
            # root no longer resolves (deleted after the hook fired):
            # nothing to do; the register row is cascade-deleted with it
            return {"exported_at": None, "sig": None, "log_id": None,
                    "noop": "root_gone"}
        # render + atomic file write (write_export does no DB writes)
        written = write_export(ro, root_uuid, emit_path, emit_format, fidelity)
    finally:
        ro.close()
    if written is None:
        # the root vanished between the stack read and the write: same no-op
        return {"exported_at": None, "sig": None, "log_id": None,
                "noop": "root_gone"}
    exported_at = written[0]
    sig = stack_signature(payload)
    now = db.now_ns()

    # exactly one small read-write transaction (SPEC 3.18)
    conn = db.connect(vault_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE autoexport_registers SET last_sig = ?, last_emitted = ? "
                "WHERE root_uuid = ? AND emit_path = ?",
                (sig, now, root_uuid, emit_path),
            )
            title = f"autoexport {payload['root']['space']} {root_uuid} -> {emit_path}"
            body = json.dumps(
                {
                    "op": "autoexport",
                    "root": root_uuid,
                    "space": payload["root"]["space"],
                    "emit_path": emit_path,
                    "emit_format": emit_format,
                    "sig": sig,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            cur = conn.execute(
                "INSERT INTO logs (created, title, area, body) VALUES (?, ?, ?, ?)",
                (now, title, "autoexport", body),
            )
            log_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO log_refs (log_id, uuid, op) VALUES (?, ?, ?)",
                (log_id, root_uuid, "referenced"),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"exported_at": exported_at, "sig": sig, "log_id": log_id}


def spawn_exporter(vault_path, root_uuid, emit_path, emit_format):
    """Detach the exporter child; the server does nothing but spawn.

    CREATE_NO_WINDOW + devnull streams: no console flash, no output
    interleaving with the MCP stdio transport.
    """
    cmd = [
        sys.executable, "-m", "qatena.autoexport",
        vault_path, root_uuid, emit_path, emit_format,
    ]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)


# ------------------------------------------------------------- child entry

def main(argv=None):
    """Child entrypoint: `python -m qatena.autoexport <vault> <root> <path> <fmt>`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4:
        print(
            "usage: python -m qatena.autoexport <vault_path> <root_uuid> "
            "<emit_path> <emit_format>",
            file=sys.stderr,
        )
        return 2
    vault_path, root_uuid, emit_path, emit_format = argv
    try:
        export(vault_path, root_uuid, emit_path, emit_format)
    except Exception as e:  # failure is self-healing: stale sig re-triggers later
        print(f"qatena.autoexport: export failed: {e!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
