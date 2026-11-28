"""The mythos snapshot: the whole-vambrace codec (draft scratch/
vambrace_actions_draft.md, sections 5.5 + 6; ratified 10.12).

One codec, two directions: ``compute_mythos`` reads a live vambrace and
produces the section-6.2 payload; ``render_mythos`` / ``parse_mythos`` move
it to and from a file in the four formats (json / md / csv / ndjson);
``write_mythos`` is the exact function phase 3's waylay calls inline --
open read-only, compute, render, atomic temp + os.replace (the autoexport
pattern). The vambrace is never written.

The mythos carries all eight entity spaces in the bare-record vocabulary
(NULLs omitted, the list rendering) plus the meta: ``key`` (the vambrace
key, passed in -- never read from the db), ``saga`` (meta table, omitted
when unset), ``exported_at`` (ISO-8601 UTC). Excluded by design:
``deflected_writes`` (declared memory, not truth) and
``autoexport_registers`` (machine-local config; an acquired vambrace starts
with none).

Mythos identification is uniform across formats: the presence of the meta
records. A stack-shaped payload (root/stack/counts/reblades, no meta) is
rejected -- never misread. Corrupt input is a clean ValueError, never a
crash.

Per-space record serializers share the list vocabulary: ``_bare`` (the
NULL-aware row rendering with ISO-8601 created/updated) and
``_intention_names`` come from qatena.verbs.
"""

import csv
import io
import json
import os
import re
import secrets
import sqlite3

from . import autoexport, db
from .verbs import _bare, _intention_names

ALLOWED_FORMATS = autoexport.ALLOWED_FORMATS

# The eight entity spaces, canonical order (draft 6.2).
SPACES = ("chains", "blades", "reblades", "intentions",
          "open_items", "rulings", "terms", "logs")

NOT_MYTHOS = (
    "not a whole-vambrace snapshot: missing meta key/exported_at -- "
    "a stack-shaped export has no meta; the mythos shape is required"
)

# The md field-comment shape: exactly one field per line, so values that
# contain colons or spaces (sagas, notes, bodies) stay machine-parseable.
_COMMENT_RE = re.compile(r"^<!-- (\w+): (.*?) -->$")
_INTENTIONS_RE = re.compile(r"^\*intentions: (.*)\*$")
_REBLADE_LINE_RE = re.compile(r"^(.+) \[(.+)\] -> (.+)$")


# ---------------------------------------------------------------- compute

def _rec(row):
    """A bare record in the list vocabulary (created/updated rendered
    ISO-8601) with NULLs omitted -- the mythos per-space record shape."""
    return {k: v for k, v in _bare(row).items() if v is not None}


def compute_mythos(conn, key):
    """The section-6.2 payload for a whole vambrace, from an OPEN
    connection (this function never writes).

    ``key`` is the vambrace key, passed in -- NOT read from anywhere in the
    db (the file name IS the key). ``saga`` comes from the meta table and
    is omitted when unset. ``deflected_writes`` and ``autoexport_registers``
    are never read (draft 6.1: excluded by design).
    """
    saga = None
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", ("saga",)).fetchone()
    except sqlite3.Error:
        row = None  # a hand-touched db without the meta table: no saga
    payload = {"exported_at": db.ns_to_iso(db.now_ns()), "key": key}
    if row is not None:
        payload["saga"] = row["v"]

    spaces = {}
    # chains: the composite as `list` renders it -- the bare header plus
    # the sorted intention names (blades ride their own space, with pos)
    spaces["chains"] = [
        dict(_rec(r), intentions=_intention_names(conn, r["id"]))
        for r in conn.execute("SELECT * FROM chain_headers ORDER BY created, id")
    ]
    blades = []
    for r in conn.execute(
        "SELECT b.id AS id, b.chain_id AS chain_id, b.text AS text, "
        "b.created AS created, b.updated AS updated, o.pos AS pos, "
        "h.name AS chain_name "
        "FROM chain_blades b "
        "JOIN blade_order o ON o.chain_id = b.chain_id AND o.blade_id = b.id "
        "JOIN chain_headers h ON h.id = b.chain_id "
        "ORDER BY h.created, h.id, o.pos"
    ):
        rec = _rec(r)
        rec["chain"] = rec.pop("chain_name")
        blades.append(rec)
    spaces["blades"] = blades
    spaces["reblades"] = [
        _rec(r) for r in conn.execute(
            "SELECT * FROM reblades ORDER BY src_uuid, dst_uuid, kind")
    ]
    spaces["intentions"] = [
        _rec(r) for r in conn.execute(
            "SELECT * FROM chain_intentions ORDER BY created, id")
    ]
    spaces["open_items"] = [
        _rec(r) for r in conn.execute("SELECT * FROM open_items ORDER BY created, id")
    ]
    spaces["rulings"] = [
        _rec(r) for r in conn.execute("SELECT * FROM rulings ORDER BY created, id")
    ]
    spaces["terms"] = [
        _rec(r) for r in conn.execute("SELECT * FROM terms ORDER BY created, id")
    ]
    logs = []
    for r in conn.execute("SELECT * FROM logs ORDER BY id"):
        rec = _rec(r)
        # the ref UUIDs inline (log_refs deliberately outlive deleted
        # entities, so the refs land intact in an acquired vambrace)
        rec["refs"] = [
            {"uuid": rr["uuid"], "op": rr["op"]}
            for rr in conn.execute(
                "SELECT uuid, op FROM log_refs WHERE log_id = ? ORDER BY uuid, op",
                (r["id"],),
            )
        ]
        logs.append(rec)
    spaces["logs"] = logs

    payload["spaces"] = spaces
    return payload


# ----------------------------------------------------------------- render

def _meta_doc(payload, exported_at):
    """The meta records: key always, saga when set, exported_at always."""
    meta = {"key": payload["key"], "exported_at": exported_at}
    if "saga" in payload:
        meta["saga"] = payload["saga"]
    return meta


def _canon(rec):
    """Canonical JSON for one record (sort_keys, compact separators)."""
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render_json(payload):
    doc = {"exported_at": payload["exported_at"], "key": payload["key"]}
    if "saga" in payload:
        doc["saga"] = payload["saga"]
    doc["spaces"] = {s: payload["spaces"][s] for s in SPACES}
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _field_lines(rec, fields):
    """One HTML comment per machine field (the mythos md convention)."""
    return [f"<!-- {k}: {rec[k]} -->" for k in fields if k in rec]


def _render_md(payload):
    out = [f"# {payload.get('saga') or payload['key']}", ""]
    out.append("## meta")
    out.append(f"<!-- key: {payload['key']} -->")
    if "saga" in payload:
        out.append(f"<!-- saga: {payload['saga']} -->")
    out.append(f"<!-- exported_at: {payload['exported_at']} -->")

    spaces = payload["spaces"]
    out += ["", "## chains"]
    for ch in spaces["chains"]:
        out.append(f"- {ch['name']} <!-- uuid: {ch['id']} -->")
        out.extend(_field_lines(ch, ("created", "updated")))
        if ch["intentions"]:
            out.append("*intentions: " + ", ".join(ch["intentions"]) + "*")
    out += ["", "## blades"]
    for b in spaces["blades"]:
        out.append(f"- {b['text']} <!-- uuid: {b['id']} pos: {b['pos']} -->")
        out.extend(_field_lines(b, ("chain_id", "chain", "created", "updated")))
    out += ["", "## reblades"]
    for r in spaces["reblades"]:
        out.append(f"- {r['src_uuid']} [{r['kind']}] -> {r['dst_uuid']}")
        out.extend(_field_lines(r, ("note", "created")))
    out += ["", "## intentions"]
    for s in spaces["intentions"]:
        out.append(f"- {s['name']} <!-- uuid: {s['id']} -->")
        out.extend(_field_lines(s, ("orientation", "created", "updated")))
    out += ["", "## open_items"]
    for o in spaces["open_items"]:
        out.append(f"- {o['name']} <!-- uuid: {o['id']} -->")
        out.extend(_field_lines(o, ("state", "text", "freeform_current",
                                    "freeform_pending", "freeform_total",
                                    "freeform_next", "freeform_last",
                                    "freeform_due", "freeform_priority",
                                    "created", "updated")))
    out += ["", "## rulings"]
    for r in spaces["rulings"]:
        out.append(f"- {r['name']} <!-- uuid: {r['id']} -->")
        out.extend(_field_lines(r, ("number", "state", "body", "created", "updated")))
    out += ["", "## terms"]
    for t in spaces["terms"]:
        out.append(f"- {t['term']} <!-- uuid: {t['id']} -->")
        out.extend(_field_lines(t, ("text", "created", "updated")))
    out += ["", "## logs"]
    for lg in spaces["logs"]:
        out.append(f"- {lg.get('title', '')} <!-- id: {lg['id']} -->")
        out.extend(_field_lines(lg, ("area", "body", "created")))
        out.append(f"<!-- refs: {_canon(lg['refs'])} -->")
    return "\n".join(out) + "\n"


_CSV_COLUMNS = ["space", "uuid", "name", "src", "dst", "kind", "pos",
                "text", "note", "extra"]


def _render_csv(payload):
    """Extends the stack-csv framing (space column) with the added spaces:
    the human-readable core columns stay as the stack csv writes them, and
    ``extra`` carries the full record as canonical JSON so the codec stays
    lossless (draft 6.4). Meta rows first."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_CSV_COLUMNS)
    meta = _meta_doc(payload, payload["exported_at"])
    for k in ("key", "saga", "exported_at"):
        if k in meta:
            w.writerow(["meta", k, meta[k]] + [""] * 7)
    spaces = payload["spaces"]
    for ch in spaces["chains"]:
        w.writerow(["chain", ch["id"], ch["name"], "", "", "", "", "",
                    "", _canon(ch)])
    for b in spaces["blades"]:
        w.writerow(["blade", b["id"], b["chain"], "", "", "", str(b["pos"]),
                    b["text"], "", _canon(b)])
    for r in spaces["reblades"]:
        w.writerow(["reblade", "", "", r["src_uuid"], r["dst_uuid"], r["kind"],
                    "", "", r.get("note", ""), _canon(r)])
    for s in spaces["intentions"]:
        w.writerow(["intention", s["id"], s["name"], "", "", "", "", "",
                    "", _canon(s)])
    for o in spaces["open_items"]:
        w.writerow(["open_item", o["id"], o["name"], "", "", "", "",
                    o.get("text", ""), "", _canon(o)])
    for r in spaces["rulings"]:
        w.writerow(["ruling", r["id"], r["name"], "", "", "", "",
                    r.get("body", ""), "", _canon(r)])
    for t in spaces["terms"]:
        w.writerow(["term", t["id"], t["term"], "", "", "", "",
                    t.get("text", ""), "", _canon(t)])
    for lg in spaces["logs"]:
        w.writerow(["log", str(lg["id"]), lg.get("title", ""), "", "", "", "",
                    lg.get("body", ""), "", _canon(lg)])
    return buf.getvalue()


def _render_ndjson(payload):
    """One record per line, {"space": ..., "record": ...}; meta lines first."""
    lines = []
    meta = _meta_doc(payload, payload["exported_at"])
    for k in ("key", "saga", "exported_at"):
        if k in meta:
            lines.append({"space": "meta", "record": {k: meta[k]}})
    for s in SPACES:
        for rec in payload["spaces"][s]:
            lines.append({"space": s, "record": rec})
    return "".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        for line in lines
    )


def render_mythos(payload, emit_format, exported_at=None):
    """Render a mythos payload in one of the four formats (draft 6.3).

    ``exported_at`` overrides the payload's when given (the write path
    stamps the current time there). Raises ValueError on an unknown format.
    """
    if emit_format not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})")
    if exported_at is not None and exported_at != payload.get("exported_at"):
        payload = dict(payload)
        payload["exported_at"] = exported_at
    return {
        "json": _render_json,
        "md": _render_md,
        "csv": _render_csv,
        "ndjson": _render_ndjson,
    }[emit_format](payload)


# ------------------------------------------------------------------- parse

def _finish_parse(meta, sections):
    """The uniform mythos gate: the meta records (key + exported_at) must
    be present, else the payload is a stack-shaped export (draft 6.2)."""
    if "key" not in meta or "exported_at" not in meta:
        raise ValueError(NOT_MYTHOS)
    payload = {"exported_at": meta["exported_at"], "key": meta["key"]}
    if "saga" in meta:
        payload["saga"] = meta["saga"]
    payload["spaces"] = {s: sections[s] for s in SPACES}
    return payload


def _parse_json(text):
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"corrupt json snapshot: {e}") from None
    if (not isinstance(doc, dict) or "key" not in doc
            or "exported_at" not in doc
            or not isinstance(doc.get("spaces"), dict)):
        raise ValueError(NOT_MYTHOS)
    spaces = doc["spaces"]
    for s in SPACES:
        if not isinstance(spaces.get(s), list):
            raise ValueError(
                f"not a whole-vambrace snapshot: spaces.{s} missing or not a list")
    meta = {"key": doc["key"], "exported_at": doc["exported_at"]}
    if "saga" in doc:
        meta["saga"] = doc["saga"]
    return _finish_parse(meta, {s: spaces[s] for s in SPACES})


def _md_record(section, cur):
    """Rebuild one record from its md content line + field comments."""
    f = cur["fields"]
    if section == "chains":
        rec = {"id": f["uuid"], "name": cur["name"],
               "created": f["created"], "updated": f["updated"],
               "intentions": cur.get("intentions", [])}
        return rec
    if section == "blades":
        return {"id": f["uuid"], "chain_id": f["chain_id"],
                "chain": f["chain"], "text": cur["name"],
                "pos": int(f["pos"]), "created": f["created"],
                "updated": f["updated"]}
    if section == "reblades":
        m = _REBLADE_LINE_RE.match(cur["name"])
        if m is None:
            raise ValueError(f"corrupt md snapshot: bad reblade line {cur['name']!r}")
        rec = {"src_uuid": m.group(1), "dst_uuid": m.group(3), "kind": m.group(2)}
        if "note" in f:
            rec["note"] = f["note"]
        rec["created"] = f["created"]
        return rec
    if section == "intentions":
        return {"id": f["uuid"], "name": cur["name"],
                "orientation": f["orientation"], "created": f["created"],
                "updated": f["updated"]}
    if section == "open_items":
        rec = {"id": f["uuid"], "name": cur["name"], "state": f["state"],
               "created": f["created"], "updated": f["updated"]}
        for k in ("text", "freeform_next", "freeform_last", "freeform_due",
                  "freeform_priority"):
            if k in f:
                rec[k] = f[k]
        for k in ("freeform_current", "freeform_pending", "freeform_total"):
            if k in f:
                rec[k] = int(f[k])
        return rec
    if section == "rulings":
        rec = {"id": f["uuid"], "number": f["number"], "name": cur["name"],
               "state": f["state"], "created": f["created"],
               "updated": f["updated"]}
        if "body" in f:
            rec["body"] = f["body"]
        return rec
    if section == "terms":
        rec = {"id": f["uuid"], "term": cur["name"],
               "created": f["created"], "updated": f["updated"]}
        if "text" in f:
            rec["text"] = f["text"]
        return rec
    if section == "logs":
        rec = {"id": int(f["id"]), "title": cur["name"],
               "created": f["created"], "refs": json.loads(f["refs"])}
        if "area" in f:
            rec["area"] = f["area"]
        if "body" in f:
            rec["body"] = f["body"]
        return rec
    raise ValueError(f"corrupt md snapshot: unknown section {section!r}")


def _parse_md(text):
    meta = {}
    sections = {s: [] for s in SPACES}
    section = None
    cur = None

    def flush():
        nonlocal cur
        if cur is not None and section in sections:
            sections[section].append(_md_record(section, cur))
        cur = None

    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("## "):
            flush()
            section = line[3:].strip()
            continue
        if line.startswith("# "):
            continue  # the title (saga or key): the meta block carries both
        if section == "meta":
            m = _COMMENT_RE.match(line)
            if m:
                meta[m.group(1)] = m.group(2)
            continue
        if section not in sections:
            continue  # an unknown section (a stack md has none of ours)
        m = _COMMENT_RE.match(line)
        if m:
            if cur is None:
                cur = {"name": "", "fields": {}}
            cur["fields"][m.group(1)] = m.group(2)
            continue
        m = _INTENTIONS_RE.match(line)
        if m:
            if cur is not None and m.group(1).strip():
                cur["intentions"] = [s.strip() for s in m.group(1).split(",")]
            continue
        if line.startswith("- "):
            flush()
            cur = {"name": "", "fields": {}}
            content = line[2:]
            cm = re.search(r"<!-- (.*) -->\s*$", content)
            if cm:
                content = content[:cm.start()].rstrip()
                for fm in re.finditer(r"(\w+): (\S+)", cm.group(1)):
                    cur["fields"][fm.group(1)] = fm.group(2)
            cur["name"] = content
            continue
        # blank lines: ignored
    flush()
    return _finish_parse(meta, sections)


# The csv space column carries the stack framing's SINGULAR values (the
# mythos csv extends the stack csv, draft 6.3); the payload's spaces use the
# canonical plural names, so the rows are mapped, not matched directly.
_CSV_SPACE = {
    "chain": "chains",
    "blade": "blades",
    "reblade": "reblades",
    "intention": "intentions",
    "open_item": "open_items",
    "ruling": "rulings",
    "term": "terms",
    "log": "logs",
}


def _parse_csv(text):
    meta = {}
    sections = {s: [] for s in SPACES}
    for row in csv.DictReader(io.StringIO(text)):
        space = (row.get("space") or "").strip()
        if space == "meta":
            k = (row.get("uuid") or "").strip()
            if k:
                meta[k] = (row.get("name") or "").strip()
            continue
        target = _CSV_SPACE.get(space)
        if target is None:
            continue
        extra = (row.get("extra") or "").strip()
        if extra:
            sections[target].append(json.loads(extra))
    return _finish_parse(meta, sections)


def _parse_ndjson(text):
    meta = {}
    sections = {s: [] for s in SPACES}
    for n, line in enumerate(text.split("\n"), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise ValueError(
                f"corrupt ndjson snapshot: line {n}: {e}") from None
        space = obj.get("space") if isinstance(obj, dict) else None
        if space == "meta":
            rec = obj.get("record")
            if isinstance(rec, dict):
                meta.update(rec)
        elif space in sections:
            sections[space].append(obj.get("record"))
    return _finish_parse(meta, sections)


def parse_mythos(text, emit_format):
    """The inverse of render_mythos in all four formats.

    Raises ValueError (never crashes) on corrupt input or on a payload that
    is not the mythos shape: a stack-shaped export (no meta records) is
    rejected with the not-a-whole-vambrace-snapshot error (draft 6.2).
    """
    if emit_format not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})")
    if not isinstance(text, str):
        raise ValueError(f"corrupt {emit_format} snapshot: not text")
    try:
        return {
            "json": _parse_json,
            "md": _parse_md,
            "csv": _parse_csv,
            "ndjson": _parse_ndjson,
        }[emit_format](text)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            f"corrupt {emit_format} snapshot: {type(e).__name__}: {e}") from None


# -------------------------------------------------------------- write path

def _atomic_write(rendered, emit_path):
    """temp + os.replace (the autoexport pattern): a crash never leaves a
    torn file. Returns the bytes written."""
    tmp = f"{emit_path}.tmp-{secrets.token_hex(8)}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(rendered)
        os.replace(tmp, emit_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return os.path.getsize(emit_path)


def write_mythos(vambrace_path, key, emit_path, emit_format):
    """The inline whole-vambrace furnish (draft 5.5): open the vambrace
    READ-ONLY, compute, render, atomic write. Zero mutation -- no register
    row, no log row, no stored signature: the file is opened read-only and
    never written. Returns the bytes written. This is the exact function
    phase 3's waylay calls (draft 5.6, step 1)."""
    if emit_format not in ALLOWED_FORMATS:
        raise ValueError(
            f"unknown emit format {emit_format!r} (allowed: {list(ALLOWED_FORMATS)})")
    conn = autoexport.open_readonly(vambrace_path)
    try:
        payload = compute_mythos(conn, key)
    finally:
        conn.close()
    return _atomic_write(render_mythos(payload, emit_format), emit_path)
