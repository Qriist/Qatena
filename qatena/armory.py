"""Armory-level state: the armory ledger, inventory, and saga management.

Phase 1 of the vambrace lifecycle (draft: scratch/vambrace_actions_draft.md,
sections 4.2-4.3, 5.1-5.3). The in-db `meta` table (k='saga') is the SOURCE
OF TRUTH for a vambrace's saga -- the human-facing name; it lives in the
database and travels with the file. The armory ledger at <armory>/armory.json
(a deliberately non-hidden, human-facing file) is a LISTING CACHE: the sagas
map is derivable from the dbs, while the waylays bookkeeping (phase 3:
waylay/recover/dismantle) is deliberately NOT derivable and is kept out of
the candidate db so a corrupt or vanished candidate cannot fake its own
dismantle count.

Fail-closed invariants (draft 4.3): a missing or corrupt ledger never blocks
an action; count uncertainty never destroys -- it only delays. Armory-level
actions (inventory, saga, equip's saga mirror) mint NO db log rows: the
ledger + the responses are their record (draft, ratified 10.8).

This module is transport-free: it returns plain data and warning strings and
never raises into the caller for armory-state problems; the server layer
shapes the (payload, is_error) tool responses.
"""

import json
import os
import re
import secrets
import sqlite3
import sys

from . import autoexport, db

# The UUIDv7 routing-key shape (SPEC 1.9) -- same pattern as the server's
# _VAMBRACE_KEY_RE (kept local so this module does not import the server).
KEY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")

LEDGER_NAME = "armory.json"   # deliberately non-hidden: human-facing
WAYLAY_DIR = "waylay"
SAGA_KEY = "saga"
DISMANTLE_TOTAL = 3           # request 3 destroys (phase 3); display here


# ------------------------------------------------------------- the ledger

def ledger_path(armory_dir):
    return os.path.join(armory_dir, LEDGER_NAME)


def _empty_ledger():
    return {"sagas": {}, "waylays": {}}


def read_ledger(armory_dir):
    """(ledger, warning) for <armory>/armory.json; never raises.

    Missing -> empty ledger + a warning (a fresh armory has no ledger; the
    callers decide whether that is worth surfacing). Corrupt (unreadable /
    not JSON / wrong top-level shape) -> empty ledger + a warning: the sagas
    portion is re-derivable from the dbs, and the waylays counts zero out
    fail-closed (uncertainty never destroys -- it only delays). Well-formed
    files with malformed individual entries keep the good entries.
    """
    path = ledger_path(armory_dir)
    if not os.path.exists(path):
        return _empty_ledger(), f"armory ledger {LEDGER_NAME} is missing; treating as empty"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return _empty_ledger(), (
            f"armory ledger {LEDGER_NAME} is unreadable ({type(e).__name__}); "
            "treating as empty")
    if not isinstance(data, dict) or not isinstance(data.get("sagas"), dict) \
            or not isinstance(data.get("waylays"), dict):
        return _empty_ledger(), (
            f"armory ledger {LEDGER_NAME} has an unexpected shape; "
            "treating as empty")
    sagas = {k: v for k, v in data["sagas"].items()
             if isinstance(k, str) and isinstance(v, str)}
    waylays = {k: v for k, v in data["waylays"].items()
               if isinstance(k, str) and isinstance(v, dict)}
    return {"sagas": sagas, "waylays": waylays}, None


def write_ledger(armory_dir, ledger):
    """Atomically replace <armory>/armory.json (temp file + os.replace -- the
    autoexport pattern): a crash never leaves a torn ledger. Returns a
    warning string on failure (never raises); a failed mirror never fails the
    action, the in-db saga stays truth. sort_keys keeps the bytes comparable;
    the file is human-facing, so it stays indented."""
    path = ledger_path(armory_dir)
    tmp = f"{path}.tmp-{secrets.token_hex(8)}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(ledger, sort_keys=True, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return (f"armory ledger {LEDGER_NAME} write failed ({type(e).__name__}); "
                "the in-db saga is authoritative, the ledger may be stale")
    return None


# ------------------------------------------------------------- enumeration

def list_vambraces(armory_dir):
    """(live, waylaid): the <key>.db files of each population, TOP LEVEL of
    each directory only, sorted by key. The waylay dir may not exist (->
    empty) and reads never create it. Strays -- armory.json, any other file,
    WAL sidecars -- are skipped silently."""
    def _keys(directory):
        keys = []
        try:
            entries = os.listdir(directory)
        except OSError:
            return keys
        for name in entries:
            if name.endswith(".db") and KEY_RE.match(name[:-3]) \
                    and os.path.isfile(os.path.join(directory, name)):
                keys.append(name[:-3])
        return sorted(keys)

    live = _keys(armory_dir)
    waylaid = _keys(os.path.join(armory_dir, WAYLAY_DIR))
    return live, waylaid


# ------------------------------------------------------------- the sagas

def read_db_saga(path):
    """(saga, warning): the in-db saga -- meta WHERE k='saga' -- read-only.
    (None, None) when unset; (None, warning) when the file is unreadable,
    corrupt, or lacks the meta table (a hand-touched db): it is SKIPPED,
    never blocks the action."""
    try:
        conn = autoexport.open_readonly(path)
    except (sqlite3.Error, OSError) as e:
        return None, (f"cannot open {os.path.basename(path)} read-only "
                      f"({type(e).__name__})")
    try:
        row = conn.execute(
            "SELECT v FROM meta WHERE k = ?", (SAGA_KEY,)).fetchone()
    except sqlite3.Error as e:
        return None, (f"cannot read saga of {os.path.basename(path)} "
                      f"({type(e).__name__})")
    finally:
        conn.close()
    return (row["v"] if row is not None else None), None


def known_sagas(armory_dir, exclude=None):
    """The conflict-check ground truth (draft 4.3/5.1/5.3): the meta 'saga'
    row of EVERY live and waylaid db, read-only -- the db is the source of
    truth, the ledger is a display cache and is never consulted here.
    Returns ({key: saga}, [warnings]); unreadable dbs are skipped with a
    warning and never block. ``exclude`` drops one key (self-exclusion)."""
    live, waylaid = list_vambraces(armory_dir)
    sagas, warnings = {}, []
    for key in live:
        if key != exclude:
            saga, warn = read_db_saga(os.path.join(armory_dir, key + ".db"))
            if warn:
                warnings.append(warn)
            if saga is not None:
                sagas[key] = saga
    for key in waylaid:
        if key != exclude:
            saga, warn = read_db_saga(os.path.join(
                armory_dir, WAYLAY_DIR, key + ".db"))
            if warn:
                warnings.append(warn)
            if saga is not None:
                sagas[key] = saga
    return sagas, warnings


def _warn_stderr(warnings):
    """Non-fatal diagnostics go to stderr, the server's established channel
    for warnings that must not shape the response (the autoexport-check and
    deflection-cache pattern)."""
    for warn in warnings:
        print(f"qatena: saga scan: {warn}", file=sys.stderr)


def find_saga_conflict(armory_dir, saga, exclude=None):
    """The key of ANOTHER vambrace already carrying ``saga`` (case-
    insensitive across live + waylaid), or None. Warnings for unreadable dbs
    go to stderr and never block the action."""
    sags, warnings = known_sagas(armory_dir, exclude=exclude)
    _warn_stderr(warnings)
    for key, s in sorted(sags.items()):
        if s.lower() == saga.lower():
            return key
    return None


def set_db_saga(conn, saga):
    """One small RW transaction on an OPEN connection: upsert the meta
    'saga' row, or delete it when ``saga`` is None (clear). NO log row is
    minted (armory-level actions leave no db trace; draft, ratified 10.8).
    Returns the effective saga read back from the db (None when cleared)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if saga is None:
            conn.execute("DELETE FROM meta WHERE k = ?", (SAGA_KEY,))
        else:
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (SAGA_KEY, saga),
            )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    row = conn.execute(
        "SELECT v FROM meta WHERE k = ?", (SAGA_KEY,)).fetchone()
    return row["v"] if row is not None else None


def mirror_saga(armory_dir, key, saga):
    """The ledger mirror, AFTER the in-db write succeeded: read-modify-write
    the sagas map (None clears the entry). Returns a warning when the mirror
    write failed, or when it rewrote a CORRUPT ledger (the non-derivable
    waylays section was lost -- fail-closed: uncertainty never destroys, it
    only delays); None on a clean mirror (a mirror over a MISSING ledger
    simply created it: nothing was lost). A failed mirror never fails the
    action -- the db is truth."""
    ledger, read_warn = read_ledger(armory_dir)
    pre_existed = os.path.exists(ledger_path(armory_dir))
    if saga is None:
        ledger["sagas"].pop(key, None)
    else:
        ledger["sagas"][key] = saga
    write_warn = write_ledger(armory_dir, ledger)
    if write_warn is not None:
        return write_warn
    if read_warn is not None and pre_existed:
        return read_warn
    return None


# ------------------------------------------------------------- inventory

def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _file_modified(path):
    """ISO-8601 UTC (the tool-surface form) of the file's mtime, or None."""
    try:
        return db.ns_to_iso(int(os.path.getmtime(path) * 1_000_000_000))
    except OSError:
        return None


def inventory(armory_dir):
    """The inventory (draft 5.2): both populations -- live
    (<armory>/<key>.db) and waylaid (<armory>/waylay/<key>.db); strays are
    skipped silently. Entries: {key, saga, size_bytes, modified} plus, for
    waylaid, waylaid_at / dismantle_requests / dismantles_until_destroy from
    the ledger. Sagas come from the ledger (a display cache); a missing or
    corrupt ledger rebuilds the sagas portion by reading each db's meta (the
    db is truth), zeroes the waylay counts (fail-closed), rewrites the
    ledger, and flags ledger_rebuilt."""
    live, waylaid = list_vambraces(armory_dir)
    ledger, read_warn = read_ledger(armory_dir)
    rebuilt = False
    write_warn = None
    if read_warn is not None:
        # Unreadable ledger: rebuild the sagas portion from the dbs (the db
        # is truth), zero the waylay counts (not derivable -> fail-closed),
        # and rewrite the ledger -- but only when there is something to
        # record (a pre-existing file, or a non-empty armory); a fresh empty
        # armory stays file-free.
        pre_existed = os.path.exists(ledger_path(armory_dir))
        sagas, warnings = known_sagas(armory_dir)
        _warn_stderr(warnings)
        ledger = {"sagas": sagas, "waylays": {}}
        if pre_existed or sagas or waylaid:
            rebuilt = True
            write_warn = write_ledger(armory_dir, ledger)
    live_entries = []
    for key in live:
        path = os.path.join(armory_dir, key + ".db")
        live_entries.append({
            "key": key,
            "saga": ledger["sagas"].get(key),
            "size_bytes": _file_size(path),
            "modified": _file_modified(path),
        })
    waylaid_entries = []
    for key in waylaid:
        path = os.path.join(armory_dir, WAYLAY_DIR, key + ".db")
        entry = ledger["waylays"].get(key)
        if not isinstance(entry, dict):
            entry = {}
        requests = entry.get("dismantle_requests")
        n = len(requests) if isinstance(requests, list) else 0
        waylaid_at = entry.get("waylaid_at")
        waylaid_entries.append({
            "key": key,
            "saga": ledger["sagas"].get(key),
            "waylaid_at": waylaid_at if isinstance(waylaid_at, str) else None,
            "size_bytes": _file_size(path),
            "modified": _file_modified(path),
            "dismantle_requests": n,
            "dismantles_until_destroy": max(DISMANTLE_TOTAL - n, 0),
        })
    payload = {"live": live_entries, "waylaid": waylaid_entries}
    if rebuilt:
        payload["ledger_rebuilt"] = True
    if write_warn:
        payload["ledger_warning"] = write_warn
    return payload
