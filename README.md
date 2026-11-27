# Qatena
SQLite-based note storage for local AI agents, tailored to complex personal projects.

## What it is

Durable, itemizable, addressable, and malleable note storage for local AI agents.
Notes (**chains**) decompose into individually addressable units (**blades**).
Cross-references (**reblades**) are first-class and traversable.
Chains can be filed under any number of **intentions** (view-level categories,
overlapping by design). Structure can be split, merged, and refiled without breaking
references. It is a set of SQLite databases (one **vambrace** per project) behind a
stdio MCP server that exposes a small, composite, batch-capable verb surface —
one logical operation per call — with every invariant enforced server-side. Every
call is routed by a **vambrace** key (a vambrace's UUIDv7 identity) to its project's
vambrace; vambraces are created with the `vambrace` verb.

## Repo layout

- `qatena/` — the package: `server.py` (stdio MCP loop + the 19 tool definitions),
  `verbs.py` (the 18-verb surface), `schema.py` (DDL + seeded registers), `autolink.py`
  (auto-linking), `db.py` (connection + timestamp helpers), `uuid7.py`, `__main__.py`.
- `tests/` — stdlib `unittest` suite (no third-party dependencies).
- `SPEC.md` — the contract: conventions, DDL, the 19 tool schemas, error codes.
  `DESIGN.md` — the settled design it implements. `AGENTS.md` — local agent orientation
  (gitignored).

## Running the server

```
python -m qatena
```

The armory dir comes from the `QATENA_ARMORY` environment variable (default `./.qatena`);
the directory is auto-created at start, and each vambrace is a file `<key>.db` inside it,
created by the `vambrace` verb. The server speaks newline-delimited JSON-RPC 2.0 (MCP) on stdin/stdout until
stdin closes; all diagnostics go to stderr, never stdout.

## Running the tests

From the repo root:

```
python -m unittest discover -s tests -v
```

Stdlib only — no pytest, no new dependencies.

## Using it in goose

Add to `~/.config/goose/config.yaml`:

```yaml
extensions:
  qatena:
    name: Qatena
    cmd: python
    args: ["-m", "qatena"]
    enabled: true
    envs:
      PYTHONPATH: "F:/Projects/Qatena"
      QATENA_ARMORY: "F:/Projects/Qatena/.qatena"
    type: stdio
    timeout: 300
```

## Write surface

The 19 verbs are the only write surface; raw SQL is deliberately not exposed. The agent
gets named, shape-checked operations instead: every write runs in one all-or-nothing
transaction, the DB path is never advertised in any tool, and invariants (gapless blade
order, endpoint validation, system logs) are enforced server-side — so a bypassed write
leaves detectable inconsistency rather than silent corruption.
