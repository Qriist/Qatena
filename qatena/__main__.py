"""Qatena entry point: `python -m qatena`.

Armory dir: env QATENA_ARMORIES, else ./.qatena (auto-created). Each armory is one
database file (<UUIDv7>.db) created by the vambrace verb — there is no default
database; every call routes via its vambrace key. Serves MCP over stdio until
stdin EOF.
"""

import os
import sys


def main() -> int:
    from . import __version__, server

    armory_dir = os.environ.get("QATENA_ARMORIES") or os.path.join(".", ".qatena")
    os.makedirs(armory_dir, exist_ok=True)
    print(f"qatena {__version__} ready (armories: {os.path.abspath(armory_dir)})", file=sys.stderr)
    return server.serve(armory_dir)


if __name__ == "__main__":
    sys.exit(main())
