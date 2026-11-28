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
vambrace; vambraces are created with the `vambrace` verb. The `vambrace` verb also runs the
vambrace lifecycle — `inventory` (both populations), `saga` (the human-facing name),
`furnish` (one-shot export; the `mythos` root snapshots the whole vambrace), `acquire`
(import a snapshot as a new vambrace), and `waylay` / `recover` / `dismantle` (suspend in the
server-controlled waylay dir; a three-step destroy) — the file location is the state.

## One armory, many vambraces

- Every project is its own **vambrace** — a single self-contained database file in one shared
  **armory** dir, addressed by a minted UUIDv7 key; every call lands on exactly that file.
- Projects are born with `equip`, and a **saga** gives each one a human-facing name that
  travels with the file.
- A project's lifecycle is a location, not a flag: **live** at the armory top, **waylaid**
  (suspended — not routable, exports inert), or gone after `dismantle`.
- A whole project moves as one file: `furnish` snapshots the entire vambrace (a **mythos**),
  and `acquire` imports that snapshot as a *new* vambrace — notes and links arrive intact.

## Malleable notes

- A note is a **chain** — the pin, expanded: a header over an ordered body of **blades**, the
  bullets themselves, now individually addressable, so any single one can be created, edited,
  moved, or deleted without touching the rest.
- Structure is data, not a fixed tree: split a set of blades out into a new chain, fold one
  chain into another, or move a blade across chains — restructure without migration, and every
  link travels with its note.
- **Intentions** are views, not folders: a chain can belong to any number of them at once
  (including zero), and an intention any number of chains — overlapping is the design.
- **Reblades** are typed cross-references between entities — the vault's link graph, followable
  hop by hop; a new blade that names an existing entity links itself on arrival.

## Summoning

- The point of the vault is in-scope context: an agent summons the slice it needs — one note or
  bullet by address, a filtered population, or every place a phrase appears — not the whole
  project.
- Retrieval runs three ways: **precise** (fetch any entity by its UUID with `get`), **browsed**
  (list a space, filtered by intention, name, or state), and **chased** (`search` unions
  probes across chains, blades, intentions, open items, rulings, terms, and logs in one call).
- Context can also be followed instead of found: `follow` walks the reblade graph hop by hop
  from any note, so related material arrives through the connections you actually made.
- Context can be protected at the edge of a summon: any read can be **counted** before
  it is fetched (a window-honest how many would come back) or **spilled** to a file
  (json/ndjson) — the result lands where it is needed, not in the agent's context.
- The one current gap: time-based queries — summoning a slice by *when* it happened, not just
  *what* it is — are a pending feature.

## Creative-project tracking

- The note space is joined by three tracking spaces: **open items** (the project's running list
  of loose ends — each with a state, an optional body, and freeform fields like due and
  priority), **rulings** (settled judgments, with their own state), and **terms** (the
  project's glossary).
- Open items are human-driven by design: the agent is a caretaker, and it works one item per
  call, no batch — the surface that must not get mangled gets no mangle path.
- Agent notes ride the same rails as everything else: a decision or a journal entry is just
  another note under a designated intention — summonable, linkable, and malleable like any
  other content, not a sidechannel.

## Furnish and autoexport

- Any part of the project — an intention, a single note, or one bullet — renders to a file on
  demand with `furnish`: one call, one file, the vault untouched, and you choose which layer
  of the link graph travels with it. The whole vambrace renders as a **mythos** snapshot —
  the shape `acquire` imports.
- `register_autoexport` makes a render live: it re-lands exactly when the stack changes and
  stays quiet while it doesn't — core worldbuilding kept current by a register is available
  to an agent from the first turn, not after re-summoning.
- A failed first export is not a dead end: the register is kept and the next write to the
  project retries; deregistering stops the watch and leaves the last file on disk.

## Semantic protections

- Every write lands all-or-nothing: one call, one transaction, and a failed batch rolls back
  entirely — the system would rather refuse a write than leave it half-landed, because a
  half-landed batch is easy to fix before it happens and nearly impossible after.
- Refusals leave memory, not a queue: a failed content write is parked in the per-vambrace
  **deflected write** cache for the next session — never auto-replayed, because creative work
  is not a financial record to be reconciled.
- The surface a confused agent can touch is deliberately small and hard-guarded: the 20 verbs
  are the whole write surface (raw SQL is never exposed), the invariants — gapless ordering,
  links that must resolve — are enforced server-side, and the human-driven open-items space is
  one item per call: a nameless write can never mint a new item, and a repeated name returns
  the existing one instead of duplicating it.
- Destructive paths carry escape hatches: a live vambrace cannot be dismantled directly — it
  must be waylaid first, and waylaying requires a complete **mythos** snapshot before
  anything moves; the dismantle itself runs three escalating steps, each naming `recover` as
  the way back; and uncertainty never destroys — a corrupt ledger defers the destroy, it
  doesn't take the chance.

## Repo layout

- `qatena/` — the package: `server.py` (stdio MCP loop + the 20 tool definitions),
  `verbs.py` (the 19 routed verbs), `schema.py` (DDL + seeded registers), `autolink.py`
  (auto-linking), `db.py` (connection + timestamp helpers), `armory.py` (the armory
  ledger, inventory, and saga management), `snapshot.py` (the mythos whole-vambrace
  snapshot codec), `autoexport.py` (register rendering + the child process), `uuid7.py`,
  `__main__.py`.
- `tests/` — stdlib `unittest` suite (no third-party dependencies).
- `SPEC.md` — the contract: conventions, DDL, the 20 tool schemas, error codes.
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

The 20 verbs are the only write surface; raw SQL is deliberately not exposed. The agent
gets named, shape-checked operations instead: every write runs in one all-or-nothing
transaction, the DB path is never advertised in any tool, and invariants (gapless blade
order, endpoint validation, system logs) are enforced server-side — so a bypassed write
leaves detectable inconsistency rather than silent corruption. A failed content write is
not lost: it parks in the per-vambrace `deflected_writes` cache (readable via `list`,
erasable via `deflected_write`) — memory for the next session, never auto-replayed.
