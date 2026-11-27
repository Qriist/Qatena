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


def compute_stack(conn, root_uuid):
    """Compute the stack rooted at root_uuid, or None if it does not resolve.

    Stack definition (SPEC 3.18):
      root = section: the section + every member chain + every blade of those
      root = chain:   the chain + its blades (in blade_order)
      root = blade:   the blade itself (a leaf; its chain is shown as
                      presentation context only, never part of the signature)
    """
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
    """Stable content-only hash over the ordered stack (SPEC 3.18).

    (root uuid, root space, root name) + per chain (uuid, name, sorted
    section names, [(blade uuid, text)] in pos order). Excluded: pointers,
    timestamps, logs, section orientation/note.
    """
    root = payload["root"]
    parts = [root["uuid"], root["space"], root["name"]]
    for ch in payload["stack"]["chains"]:
        parts.append([
            "chain", ch["id"], ch["name"], ch["sections"],
            [[b["id"], b["text"]] for b in ch["blades"]],
        ])
    for b in payload["stack"]["blades"]:
        parts.append(["blade", b["id"], b["text"]])
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- renderers

def _render_json(payload, exported_at):
    doc = {
        "root": payload["root"],
        "exported_at": exported_at,
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
    return lines


def _render_ndjson(payload, exported_at):
    return "".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        for line in _ndjson_lines(payload)
    )


def _render_csv(payload, exported_at):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["space", "uuid", "name", "pos", "text"])
    for line in _ndjson_lines(payload):
        writer.writerow([
            line["space"], line["uuid"], line["name"],
            line.get("pos", ""), line.get("text", ""),
        ])
    return buf.getvalue()


def _chain_block(out, ch, top_level):
    """One chain's lines; top-level chains are '##', the root chain itself
    renders directly under the root heading."""
    if not top_level:
        out.append("")
        out.append(f"## {ch['name']}")
        out.append(f"<!-- uuid: {ch['id']} -->")
    if ch["sections"]:
        out.append("*sections: " + ", ".join(ch["sections"]) + "*")
    for b in ch["blades"]:
        out.append(f"- {b['text']} <!-- uuid: {b['id']} -->")


def _render_md(payload, exported_at):
    root = payload["root"]
    out = [f"# {root['name']}", f"<!-- uuid: {root['uuid']} -->"]
    # stack.sections is non-empty only for a section root, where it is the
    # root itself — already the root heading above
    for ch in payload["stack"]["chains"]:
        _chain_block(out, ch, top_level=(root["space"] == "chain" and ch["id"] == root["uuid"]))
    for b in payload["stack"]["blades"]:
        # presentation context only — not part of the signature (SPEC 3.18)
        out.append("")
        out.append(f"*chain: {b['chain_name']}*")
        out.append(f"- {b['text']} <!-- uuid: {b['id']} pos: {b['pos']} -->")
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

def _open_readonly(vault_path):
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


def export(vault_path, root_uuid, emit_path, emit_format):
    """Run one full export. Raises on failure — the caller self-heals.

    1. re-read committed state READ-ONLY, compute stack + signature;
    2. render to <emit_path>.tmp-<random>, then os.replace (atomic);
    3. one small RW transaction: store last_sig/last_emitted + mint the
       export log row (area "autoexport", root ref op "referenced").

    Returns {"exported_at": iso, "sig": str, "log_id": int}.
    """
    if emit_format not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})"
        )
    ro = _open_readonly(vault_path)
    try:
        payload = compute_stack(ro, root_uuid)
    finally:
        ro.close()
    if payload is None:
        # root no longer resolves (deleted after the hook fired): nothing to do;
        # the register row is cascade-deleted with it
        return {"exported_at": None, "sig": None, "log_id": None}

    now = db.now_ns()
    exported_at = db.ns_to_iso(now)
    sig = stack_signature(payload)
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
