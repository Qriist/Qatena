"""Qatena entry point: `python -m qatena`.

DB path: env QATENA_DB, else ./.qatena/qatena.db (parent dir auto-created).
Bootstraps the schema, then serves MCP over stdio until EOF.
"""

import os
import sys


def main() -> int:
    from . import __version__, db, schema, server

    db_path = os.environ.get("QATENA_DB") or os.path.join(".", ".qatena", "qatena.db")
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)

    conn = db.connect(db_path)
    schema.bootstrap(conn)
    print(f"qatena {__version__} ready", file=sys.stderr)
    try:
        return server.serve(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
