"""Qatena entry point: `python -m qatena`.

Vault dir: env QATENA_VAULTS, else ./.qatena (auto-created). Each vault is one
database file (<UUIDv7>.db) created by the vambrace verb — there is no default
database; every call routes via its vambrace key. Serves MCP over stdio until
stdin EOF.
"""

import os
import sys


def main() -> int:
    from . import __version__, server

    vault_dir = os.environ.get("QATENA_VAULTS") or os.path.join(".", ".qatena")
    os.makedirs(vault_dir, exist_ok=True)
    print(f"qatena {__version__} ready (vaults: {os.path.abspath(vault_dir)})", file=sys.stderr)
    return server.serve(vault_dir)


if __name__ == "__main__":
    sys.exit(main())
