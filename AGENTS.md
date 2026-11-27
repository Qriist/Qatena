# Qatena

SQLite-based note storage for local AI agents, tailored to complex personal projects.

## Goal

Build durable, bulletizable, pointerable, and *malleable* note storage.

- **Durable** — SQLite-backed, local-first, crash-safe writes; safe for agents to touch repeatedly.
- **Bulletizable** — notes decompose into individually addressable units (bullets) that can be created, edited, moved, and deleted.
- **Pointerable** — first-class cross-references between notes and bullets that agents can create, follow, and traverse.
- **Malleable** — schema and structure are not frozen; notes can be split, merged, and restructured without migration pain.

## Standing rules

- **"Commit" means the full chain:** `git add` → `git commit` → `git push` to `origin`. Stop at a local commit only if explicitly asked for local-only.
- Repo: `github.com/Qriist/Qatena`.

## Local, uncommitted workspace

- `import_reference/` (gitignored, local-only): where the source database being migrated from lives, plus Python shims that act as the general starting basis for the design. Expect to diverge from and expand on those particulars as Qatena's own design takes shape.
- Nothing inside `import_reference/` is ever committed.

## Orientation

This file is the project's orientation document. Architecture decisions and status updates get recorded here (or in a document it points to) as the project develops.
