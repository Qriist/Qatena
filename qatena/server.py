"""Qatena MCP stdio server (SPEC 1.9, 3).

Transport: newline-delimited JSON (one JSON-RPC 2.0 object per line, UTF-8).
Nothing but MCP messages goes to stdout; all diagnostics go to stderr.
Multi-vambrace: every non-vambrace call routes to a vambrace file via its
``vambrace`` key; the ``vambrace`` verb creates vambraces. Serial message loop
until stdin EOF; ``serve(armory_dir) -> 0``.
"""

import json
import os
import re
import sqlite3
import sys

from . import __version__, autoexport, db, schema, uuid7
from .verbs import VerbError, _WRITE_DISPATCH, _resolve_endpoint, execute

# The 18 tools, copied from SPEC.md 3.1-3.18 (same JSON, no rewording).
TOOLS = [
    {
        "name": "chain_put",
        "description": "Create or update a chain (note). Address by id or name; if neither matches, a fresh UUIDv7 is minted and the name is required. Presence-mask: only supplied fields change. Intentions, if present, replace membership with exactly this set and upsert each intention by name (absent intentions are created; orientation defaults to 'B', or the value given; an existing intention's orientation updates only if supplied). Returns the chain composite and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Chain uuid. Takes precedence over name."
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Chain name (alias). Unique within the chain space, case-insensitive."
                },
                "intentions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 1
                            },
                            "orientation": {
                                "type": "string",
                                "enum": [
                                    "W",
                                    "B"
                                ]
                            }
                        },
                        "required": [
                            "name"
                        ]
                    },
                    "description": "If present, replaces the chain's intention membership with exactly this set. Intentions are upserted by name."
                }
            },
            "minProperties": 1,
            "required": [
                "vambrace"
            ]
        }
    },
    {
        "name": "chain_delete",
        "description": "Delete a chain by id or name. Blades and intention memberships are cascade-deleted; outgoing reblades are cascade-deleted and incoming reblades are preserved and reported in the response (they may still be meaningful). Returns the list of incoming reblades and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "name": {
                    "type": "string",
                    "minLength": 1
                }
            },
            "minProperties": 1,
            "required": [
                "vambrace"
            ]
        }
    },
    {
        "name": "chain_split",
        "description": "Carve a set of blades out of a source chain into a new chain. Blade UUIDs and their reblades travel intact. Blades are removed from the source and appended to the new chain in the order listed. The new chain's intentions are set from the optional intentions param (upsert semantics, as in chain_put). Returns both chain composites and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "source": {
                    "type": "string",
                    "description": "Source chain id or name."
                },
                "blades": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "minItems": 1,
                    "description": "Blade UUIDs to move, in the order they should appear in the new chain."
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Name for the new chain."
                },
                "intentions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string"
                            },
                            "orientation": {
                                "type": "string",
                                "enum": [
                                    "W",
                                    "B"
                                ]
                            }
                        },
                        "required": [
                            "name"
                        ]
                    },
                    "description": "Optional initial intention membership for the new chain."
                }
            },
            "required": [
                "vambrace",
                "source",
                "blades",
                "name"
            ]
        }
    },
    {
        "name": "chain_merge",
        "description": "Fold one chain into a survivor. The dissolved chain's blades are appended to the survivor in their order; intention memberships are unioned into the survivor; reblades aimed at the dissolved header are retargeted onto the survivor; the dissolved header's UUID stops resolving (recoverable via history on that UUID). Blade UUIDs and their reblades are unchanged. Returns the survivor composite, the retargeted reblades, and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "into": {
                    "type": "string",
                    "description": "Surviving chain id or name."
                },
                "fold": {
                    "type": "string",
                    "description": "Chain to dissolve (id or name)."
                }
            },
            "required": [
                "vambrace",
                "into",
                "fold"
            ]
        }
    },
    {
        "name": "blade_insert",
        "description": "Insert a blade into a chain at a 0-based index (default: end). Runs auto-linking: exact name matches mint name-ref reblades; near-matches are returned as candidates (never linked). Returns the new blade UUID, its index, minted reblades, candidates, and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "chain": {
                    "type": "string",
                    "description": "Target chain id or name."
                },
                "text": {
                    "type": "string",
                    "minLength": 1
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based position; omitted = append. Must be <= current blade count."
                }
            },
            "required": [
                "vambrace",
                "chain",
                "text"
            ]
        }
    },
    {
        "name": "blade_edit",
        "description": "Replace a blade's text by UUID. Reconciles the blade's outgoing name-ref reblades: stale ones are deleted, new exact matches minted; near-matches returned as candidates. Returns the updated blade, minted/deleted reblades, candidates, and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Blade UUID."
                },
                "text": {
                    "type": "string",
                    "minLength": 1
                }
            },
            "required": [
                "vambrace",
                "id",
                "text"
            ]
        }
    },
    {
        "name": "blade_move",
        "description": "Move a blade to a new position within its chain, or to another chain. blade_order is re-established gapless in the same transaction. The blade's UUID and reblades are unchanged. Returns the blade's new chain and index and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Blade UUID."
                },
                "to_chain": {
                    "type": "string",
                    "description": "Destination chain id or name; omitted = same chain."
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based destination position, measured after removal; omitted = end."
                }
            },
            "required": [
                "vambrace",
                "id"
            ]
        }
    },
    {
        "name": "blade_delete",
        "description": "Delete a blade by UUID. Its outgoing reblades are cascade-deleted; incoming reblades are preserved and reported. blade_order is re-established gapless in the same transaction. Returns the incoming reblades and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Blade UUID."
                }
            },
            "required": [
                "vambrace",
                "id"
            ]
        }
    },
    {
        "name": "batch",
        "description": "Execute a list of write ops (chain_put, chain_delete, chain_split, chain_merge, blade_insert, blade_edit, blade_move, blade_delete, reblade_add, reblade_remove) atomically: one transaction, all-or-nothing, applied in order. Later ops observe earlier ops' effects, including auto-linking. Returns per-op results (with minted/candidates where applicable) and the single minted log id. Any op failure rolls back the whole batch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "ops": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "chain_put",
                                    "chain_delete",
                                    "chain_split",
                                    "chain_merge",
                                    "blade_insert",
                                    "blade_edit",
                                    "blade_move",
                                    "blade_delete",
                                    "reblade_add",
                                    "reblade_remove"
                                ]
                            },
                            "args": {
                                "type": "object",
                                "description": "Arguments for this op, with the shape of the standalone verb's input, except the routing key, which is supplied once as this batch's vambrace argument."
                            }
                        },
                        "required": [
                            "op",
                            "args"
                        ]
                    }
                }
            },
            "required": [
                "vambrace",
                "ops"
            ]
        }
    },
    {
        "name": "reblade_add",
        "description": "Create an explicit typed link between two UUIDs. kind is the semantic relationship, never the endpoint type (that is implicit in the UUID). Endpoints must resolve in the tracked entity space (logs and registers are not endpoints). Idempotent on (src, dst, kind): an existing reblade is returned, not duplicated. Returns the reblade and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "src": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "dst": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "name-ref",
                        "supersedes",
                        "part-of",
                        "source",
                        "see"
                    ]
                },
                "note": {
                    "type": "string"
                }
            },
            "required": [
                "vambrace",
                "src",
                "dst",
                "kind"
            ]
        }
    },
    {
        "name": "reblade_remove",
        "description": "Remove the reblade identified by (src, dst, kind). Errors with not_found if absent. Returns the removed reblade and the minted log id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "src": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "dst": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "name-ref",
                        "supersedes",
                        "part-of",
                        "source",
                        "see"
                    ]
                }
            },
            "required": [
                "vambrace",
                "src",
                "dst",
                "kind"
            ]
        }
    },
    {
        "name": "get",
        "description": "Fetch any tracked entity by UUID. Chains return the composite: header, ordered blades (UUID, index, text), and intention names. Blades return the blade plus its chain's name. Intentions, open items, rulings, and terms return their bare records. A dissolved (merged) UUID returns not_found; the dissolving merge is recoverable via history on that UUID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                }
            },
            "required": [
                "vambrace",
                "id"
            ]
        }
    },
    {
        "name": "search",
        "description": "Multi-probe union search across entity spaces: chain names, blade text, intention names, open item names/text, ruling numbers/names/bodies, term names/definitions, log titles/bodies. Probes are OR'd (substring, case-insensitive). Results are unioned, each with its entity space, UUID, name, and a context window around the match. Returns up to limit hits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "probes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1
                    },
                    "minItems": 1,
                    "description": "Search terms; unioned across spaces."
                },
                "spaces": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "chain",
                            "blade",
                            "intention",
                            "open_item",
                            "ruling",
                            "term",
                            "log"
                        ]
                    },
                    "description": "Restrict to these entity spaces; omitted = all."
                },
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Characters of context on each side of a match. Default 120."
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum hits. Default 50."
                }
            },
            "required": [
                "vambrace",
                "probes"
            ]
        }
    },
    {
        "name": "list",
        "description": "List an entity space with filters. Returns bare records (chains as headers plus intention names; blade bodies only if include_blades). Filters are space-specific keys, ignored (with a warning in the response) where inapplicable: chains: intention (name), name_prefix; intentions: orientation; open items / rulings: state; logs: uuid (via log_refs), since, until (iso); any space: name_prefix where the space has a name/alias field.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "space": {
                    "type": "string",
                    "enum": [
                        "chain",
                        "intention",
                        "open_item",
                        "ruling",
                        "term",
                        "log"
                    ]
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "intention": {
                            "type": "string"
                        },
                        "orientation": {
                            "type": "string",
                            "enum": [
                                "W",
                                "B"
                            ]
                        },
                        "state": {
                            "type": "string"
                        },
                        "uuid": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                        },
                        "since": {
                            "type": "string",
                            "description": "iso, inclusive"
                        },
                        "until": {
                            "type": "string",
                            "description": "iso, inclusive"
                        },
                        "name_prefix": {
                            "type": "string"
                        }
                    }
                },
                "include_blades": {
                    "type": "boolean",
                    "description": "For space=chain: include ordered blades in each record. Default false."
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Default 50."
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Default 0."
                }
            },
            "required": [
                "vambrace",
                "space"
            ]
        }
    },
    {
        "name": "reblades_of",
        "description": "Incoming and/or outgoing reblades for a UUID, filterable by kind. Each edge is returned as {src, dst, kind, note, created} with both endpoints resolved: their entity space and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "direction": {
                    "type": "string",
                    "enum": [
                        "in",
                        "out",
                        "both"
                    ],
                    "description": "Default both."
                },
                "kind": {
                    "type": "string",
                    "enum": [
                        "name-ref",
                        "supersedes",
                        "part-of",
                        "source",
                        "see"
                    ]
                }
            },
            "required": [
                "vambrace",
                "id"
            ]
        }
    },
    {
        "name": "follow",
        "description": "Multi-hop traversal from a UUID through the reblade graph. Returns visited nodes {uuid, name, space} with the edge kinds traversed, up to max_hops. Cycles are detected and pruned. Chain nodes include blades only if expand_chains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "from": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "direction": {
                    "type": "string",
                    "enum": [
                        "out",
                        "in",
                        "both"
                    ],
                    "description": "Default out."
                },
                "kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "name-ref",
                            "supersedes",
                            "part-of",
                            "source",
                            "see"
                        ]
                    },
                    "description": "Restrict traversal to these kinds; omitted = any kind."
                },
                "max_hops": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Default 2, maximum 5."
                },
                "expand_chains": {
                    "type": "boolean",
                    "description": "Include blades for chain nodes. Default false."
                }
            },
            "required": [
                "vambrace",
                "from"
            ]
        }
    },
    {
        "name": "history",
        "description": "Reserved revision surface: contract now, mechanism deferred. Currently returns the coarse trace — log rows touching this UUID via log_refs, with op codes, titles, and timestamps, newest first. The field-level old→new history this contract carries is not yet implemented; clients may build against the contract shape.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "Routing key: the UUIDv7 returned by vambrace (action=equip). Required — calls without a known key fail closed; there is no default vambrace."
                },
                "id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Default 20."
                }
            },
            "required": [
                "vambrace",
                "id"
            ]
        }
    },
    {
        "name": "vambrace",
        "description": "Armory control. action=equip mints a UUIDv7, instantiates an empty Qatena database file named <UUIDv7>.db in the armory dir, bootstraps its schema, and returns exactly {\"vambrace\": \"<UUIDv7>\"} — the vambrace's key, required on every other verb's call. register_autoexport, deregister_autoexport, and export are key-bearing vambrace actions: alongside 'action' they take the vambrace's key as the 'vambrace' argument (validated fail-closed exactly like every routed verb — missing_vambrace / malformed_vambrace / unknown_vambrace), plus 'root' and 'emit_path', and 'emit_format' for register and export. export is one-shot: it exports a live intention/chain/blade stack to emit_path immediately, read-only against the vambrace (no register row, no log, no stored signature). See SPEC 3.18.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "equip",
                        "register_autoexport",
                        "deregister_autoexport",
                        "export"
                    ],
                    "description": "equip: key-less — mints a new vambrace. register_autoexport / deregister_autoexport / export: key-bearing — also pass 'vambrace' (the vambrace's key), 'root', and 'emit_path'; register_autoexport and export additionally require 'emit_format'. export is one-shot: immediate export of a live intention/chain/blade stack to emit_path, read-only against the vambrace (no register row, no log, no signature); reblade_fidelity selects the reblade layer for this render (default out)."
                },
                "vambrace": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "For register_autoexport / deregister_autoexport / export: the vambrace's key (returned by action=equip). Not used by equip."
                },
                "root": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "description": "For register_autoexport / deregister_autoexport / export: the stack root — a LIVE intention, chain, or blade (register / export); the registered root (deregister). Not used by equip."
                },
                "emit_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "For register_autoexport / deregister_autoexport / export: destination file path, used exactly as given; the parent directory must exist. Not used by equip."
                },
                "emit_format": {
                    "type": "string",
                    "enum": [
                        "md",
                        "csv",
                        "json",
                        "ndjson"
                    ],
                    "description": "For register_autoexport and export only: export format. register: re-registering the same root+path with a different format replaces it. Not used by equip or deregister."
                },
                "reblade_fidelity": {
                    "type": "string",
                    "enum": [
                        "none",
                        "in",
                        "out",
                        "both"
                    ],
                    "description": "For register_autoexport and export only: reblade layer (none|in|out|both). register: 'out' at creation, presence-masked on re-register, signed into the stack signature. export: the mode for this one-off render; omitted = 'out'. Not used by equip or deregister."
                }
            },
            "required": [
                "action"
            ]
        }
    }
]

_DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# Routing key format (SPEC 1.9): UUIDv7 — 36-char lowercase hyphenated, version nibble 7.
_VAMBRACE_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _send(payload):
    """One MCP message per line on stdout, flushed immediately."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _send_result(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _send_error(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _tool_error(code, message):
    return {"error": {"code": code, "message": message}}


def _call_vambrace(armory_dir, arguments):
    """The vambrace verb itself: armory control (SPEC 1.9, 3.18).

    ``equip`` is the only key-less action. ``register_autoexport``,
    ``deregister_autoexport``, and ``export`` are KEY-BEARING: they take the
    the vambrace's key in ``vambrace`` and are validated fail-closed exactly
    like a routed verb. ``export`` is one-shot: it renders and writes a
    stack file immediately, read-only against the vambrace (no register row,
    no log row, no stored signature).
    """
    action = arguments.get("action")
    if action == "equip":
        key = uuid7.new_uuid7()
        conn = db.connect(os.path.join(armory_dir, key + ".db"))
        try:
            schema.bootstrap(conn)
        finally:
            conn.close()
        return {"vambrace": key}, False
    if action == "register_autoexport":
        return _call_register_autoexport(armory_dir, arguments)
    if action == "deregister_autoexport":
        return _call_deregister_autoexport(armory_dir, arguments)
    if action == "export":
        return _call_export(armory_dir, arguments)
    return _tool_error(
        "unsupported_action",
        f"unsupported vambrace action {action!r} — v1 supports: equip, "
        "register_autoexport, deregister_autoexport, export"), True


def _validate_routing_key(key, armory_dir):
    """Shared fail-closed key check: routed verbs and key-bearing vambrace
    actions (SPEC 1.9). Returns (path, None) or (None, (payload, True))."""
    if not isinstance(key, str) or not key:
        return None, (_tool_error("missing_vambrace",
                                  "vambrace routing key is required — create a vambrace with the "
                                  "vambrace verb (action=equip) first"), True)
    if not _VAMBRACE_KEY_RE.match(key):
        return None, (_tool_error("malformed_vambrace",
                                  f"vambrace key must be a UUIDv7 (36-char lowercase, version 7): got {key!r}"), True)
    path = os.path.join(armory_dir, key + ".db")
    if not os.path.exists(path):
        return None, (_tool_error("unknown_vambrace",
                                  f"unknown vambrace '{key}': no such vambrace (create one with the vambrace verb)"), True)
    return path, None


# A stack is defined over intention -> chain -> blade only (SPEC 3.18).
_STACKABLE_ROOTS = ("intention", "chain", "blade")


def _register_tx(conn, now, root, space, emit_path, emit_format, reblade_fidelity=None):
    """Upsert the register row and mint its log row, one transaction.

    Identity (root_uuid, emit_path); re-registering with a different format
    OR reblade_fidelity replaces it. reblade_fidelity is presence-masked:
    None = keep the stored value ('out' on creation).
    Returns (replaced, log_id, effective_fidelity).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT emit_format, reblade_fidelity FROM autoexport_registers "
            "WHERE root_uuid = ? AND emit_path = ?",
            (root, emit_path),
        ).fetchone()
        if existing is None:
            replaced = False
            fidelity = reblade_fidelity if reblade_fidelity is not None else "out"
            conn.execute(
                "INSERT INTO autoexport_registers "
                "(root_uuid, emit_path, emit_format, reblade_fidelity, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (root, emit_path, emit_format, fidelity, now),
            )
        else:
            fidelity = (reblade_fidelity if reblade_fidelity is not None
                        else existing["reblade_fidelity"])
            fid_changed = (reblade_fidelity is not None
                           and reblade_fidelity != existing["reblade_fidelity"])
            replaced = existing["emit_format"] != emit_format or fid_changed
            if replaced:
                conn.execute(
                    "UPDATE autoexport_registers SET emit_format = ?, reblade_fidelity = ? "
                    "WHERE root_uuid = ? AND emit_path = ?",
                    (emit_format, fidelity, root, emit_path),
                )
        body = json.dumps(
            {
                "op": "register_autoexport",
                "root": root,
                "root_space": space,
                "emit_path": emit_path,
                "emit_format": emit_format,
                "reblade_fidelity": fidelity,
                "replaced": replaced,
            },
            sort_keys=True, separators=(",", ":"),
        )
        cur = conn.execute(
            "INSERT INTO logs (created, title, area, body) VALUES (?, ?, ?, ?)",
            (now, f"autoexport register {space} {root} -> {emit_path}", "autoexport", body),
        )
        conn.execute(
            "INSERT OR IGNORE INTO log_refs (log_id, uuid, op) VALUES (?, ?, ?)",
            (cur.lastrowid, root, "referenced"),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return replaced, cur.lastrowid, fidelity


def _deregister_tx(conn, now, root, emit_path):
    """Delete the register row and mint its log row, one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM autoexport_registers WHERE root_uuid = ? AND emit_path = ?",
            (root, emit_path),
        )
        body = json.dumps(
            {"op": "deregister_autoexport", "root": root, "emit_path": emit_path},
            sort_keys=True, separators=(",", ":"),
        )
        cur = conn.execute(
            "INSERT INTO logs (created, title, area, body) VALUES (?, ?, ?, ?)",
            (now, f"autoexport deregister {root} -> {emit_path}", "autoexport", body),
        )
        conn.execute(
            "INSERT OR IGNORE INTO log_refs (log_id, uuid, op) VALUES (?, ?, ?)",
            (cur.lastrowid, root, "referenced"),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return cur.lastrowid


def _call_register_autoexport(armory_dir, arguments):
    """register_autoexport (SPEC 3.18): key-bearing; synchronous first export."""
    path, err = _validate_routing_key(arguments.get("vambrace"), armory_dir)
    if err is not None:
        return err
    root = arguments.get("root")
    if not isinstance(root, str) or not root:
        return _tool_error("validation_failed",
                           "root is required for register_autoexport: the uuid of a live "
                           "intention, chain, or blade"), True
    emit_path = arguments.get("emit_path")
    if not isinstance(emit_path, str) or not emit_path:
        return _tool_error("validation_failed",
                           "emit_path is required for register_autoexport: a non-empty "
                           "destination file path"), True
    parent = os.path.dirname(emit_path)
    if parent and not os.path.isdir(parent):
        return _tool_error("validation_failed",
                           f"emit_path parent directory does not exist: {parent!r}"), True
    emit_format = arguments.get("emit_format")
    if emit_format not in autoexport.ALLOWED_FORMATS:
        return _tool_error("validation_failed",
                           f"emit_format must be one of {list(autoexport.ALLOWED_FORMATS)}: "
                           f"got {emit_format!r}"), True
    reblade_fidelity = arguments.get("reblade_fidelity")
    if reblade_fidelity is not None and reblade_fidelity not in autoexport.REBLADE_FIDELITIES:
        return _tool_error("validation_failed",
                           "reblade_fidelity must be one of none, in, out, both"), True

    conn = db.connect(path)
    try:
        schema.bootstrap(conn)
        res = _resolve_endpoint(conn, root)
        if res is None:
            return _tool_error("not_found",
                               f"root {root} does not resolve to a live tracked entity"), True
        if res["space"] not in _STACKABLE_ROOTS:
            return _tool_error("validation_failed",
                               f"root space {res['space']!r} is not stackable (must be "
                               "intention, chain, or blade)"), True
        space = res["space"]
        now = db.now_ns()
        replaced, log_id, fidelity = _register_tx(
            conn, now, root, space, emit_path, emit_format, reblade_fidelity)
        payload = autoexport.compute_stack(conn, root, fidelity)
        # Synchronous first export (SPEC 3.18): registration is a setup call,
        # not a hot path — the response reports the initial write's outcome.
        exported, exported_at, export_error = True, None, None
        try:
            info = autoexport.export(path, root, emit_path, emit_format)
            exported_at = info["exported_at"]
        except Exception as e:  # self-healing: stale sig re-exports on next change
            exported, export_error = False, f"{type(e).__name__}: {e}"
        result = {
            "root": root,
            "root_space": space,
            "emit_path": emit_path,
            "emit_format": emit_format,
            "reblade_fidelity": fidelity,
            "stack": {"chains": payload["counts"]["chains"],
                      "blades": payload["counts"]["blades"]},
            "exported": exported,
            "exported_at": exported_at,
            "replaced": replaced,
            "log_id": log_id,
        }
        if export_error is not None:
            result["export_error"] = export_error
        return result, False
    except sqlite3.Error as e:
        return _tool_error("db_error", str(e)), True
    finally:
        conn.close()


def _call_deregister_autoexport(armory_dir, arguments):
    """deregister_autoexport (SPEC 3.18): key-bearing; the file stays on disk."""
    path, err = _validate_routing_key(arguments.get("vambrace"), armory_dir)
    if err is not None:
        return err
    root = arguments.get("root")
    if not isinstance(root, str) or not root:
        return _tool_error("validation_failed",
                           "root is required for deregister_autoexport: the registered "
                           "root uuid"), True
    emit_path = arguments.get("emit_path")
    if not isinstance(emit_path, str) or not emit_path:
        return _tool_error("validation_failed",
                           "emit_path is required for deregister_autoexport: the path "
                           "given at registration"), True

    conn = db.connect(path)
    try:
        schema.bootstrap(conn)
        row = conn.execute(
            "SELECT 1 FROM autoexport_registers WHERE root_uuid = ? AND emit_path = ?",
            (root, emit_path),
        ).fetchone()
        if row is None:
            return _tool_error("not_found",
                               f"no autoexport register for root {root} at {emit_path!r}"), True
        log_id = _deregister_tx(conn, db.now_ns(), root, emit_path)
        return {"root": root, "emit_path": emit_path, "removed": True, "log_id": log_id}, False
    except sqlite3.Error as e:
        return _tool_error("db_error", str(e)), True
    finally:
        conn.close()


def _call_export(armory_dir, arguments):
    """export (SPEC 3.18): key-bearing ONE-SHOT.

    Mirrors the register_autoexport validation chain, then renders and
    writes the stack file immediately. Deliberately read-only against the
    vambrace: no register row, no log row, no log_refs, no stored signature --
    and so no schema.bootstrap on this path (the key check above already
    guarantees the vambrace file exists).
    """
    path, err = _validate_routing_key(arguments.get("vambrace"), armory_dir)
    if err is not None:
        return err
    root = arguments.get("root")
    if not isinstance(root, str) or not root:
        return _tool_error("validation_failed",
                           "root is required for export: the uuid of a live "
                           "intention, chain, or blade"), True
    emit_path = arguments.get("emit_path")
    if not isinstance(emit_path, str) or not emit_path:
        return _tool_error("validation_failed",
                           "emit_path is required for export: a non-empty "
                           "destination file path"), True
    parent = os.path.dirname(emit_path)
    if parent and not os.path.isdir(parent):
        return _tool_error("validation_failed",
                           f"emit_path parent directory does not exist: {parent!r}"), True
    emit_format = arguments.get("emit_format")
    if emit_format not in autoexport.ALLOWED_FORMATS:
        return _tool_error("validation_failed",
                           f"emit_format must be one of {list(autoexport.ALLOWED_FORMATS)}: "
                           f"got {emit_format!r}"), True
    reblade_fidelity = arguments.get("reblade_fidelity")
    if reblade_fidelity is not None and reblade_fidelity not in autoexport.REBLADE_FIDELITIES:
        return _tool_error("validation_failed",
                           "reblade_fidelity must be one of none, in, out, both"), True
    fidelity = reblade_fidelity if reblade_fidelity is not None else "out"

    conn = autoexport.open_readonly(path)
    try:
        res = _resolve_endpoint(conn, root)
        if res is None:
            return _tool_error("not_found",
                               f"root {root} does not resolve to a live tracked entity"), True
        if res["space"] not in _STACKABLE_ROOTS:
            return _tool_error("validation_failed",
                               f"root space {res['space']!r} is not stackable (must be "
                               "intention, chain, or blade)"), True
        space = res["space"]
        try:
            written = autoexport.write_export(
                conn, root, emit_path, emit_format, fidelity)
        except sqlite3.Error as e:
            return _tool_error("db_error", str(e)), True
        except Exception as e:
            return _tool_error("export_failed",
                               f"export write failed: {type(e).__name__}: {e}"), True
        if written is None:
            return _tool_error("not_found",
                               f"root {root} does not resolve to a live tracked entity"), True
        exported_at, counts = written
    except sqlite3.Error as e:
        return _tool_error("db_error", str(e)), True
    finally:
        conn.close()
    return {
        "root": root,
        "root_space": space,
        "emit_path": emit_path,
        "emit_format": emit_format,
        "reblade_fidelity": fidelity,
        "stack": {"chains": counts["chains"], "blades": counts["blades"]},
        "exported": True,
        "exported_at": exported_at,
    }, False


def _call_routed(armory_dir, name, arguments):
    """Route a non-vambrace call to its vambrace; fail closed, no default (SPEC 1.9)."""
    path, err = _validate_routing_key(arguments.get("vambrace"), armory_dir)
    if err is not None:
        return err
    conn = db.connect(path)
    try:
        schema.bootstrap(conn)  # idempotent: keeps a hand-touched vambrace file self-healing
        try:
            # the verb layer never sees the routing key
            args = {k: v for k, v in arguments.items() if k != "vambrace"}
            result = execute(conn, name, args)
        except VerbError as e:
            return _tool_error(e.code, e.message), True
        except Exception as e:  # unexpected: fail closed, never leak internals
            return _tool_error("db_error", f"internal error: {type(e).__name__}"), True
        if name in _WRITE_DISPATCH:  # writes only; batch = one check for the whole tx
            _autoexport_check(conn, path)
        return result, False
    finally:
        conn.close()


# Per-register in-flight debounce: at most one exporter child in flight per
# register. Keys are reaped lazily (poll) so a finished child unblocks the
# register's next export.
_EXPORT_INFLIGHT = {}


def _autoexport_check(conn, vault_path):
    """Post-commit hook (SPEC 3.18).

    After a write verb's transaction commits: for each active register,
    compare the current stack signature against the stored last_sig; on
    change, export. QATENA_EXPORT_SYNC=1 (test seam) runs the exporter
    inline — same code path, no spawn. Never raises into the caller: a
    failure leaves the sig stale and the next changed write re-triggers.
    """
    try:
        regs = conn.execute(
            "SELECT root_uuid, emit_path, emit_format, last_sig, reblade_fidelity "
            "FROM autoexport_registers"
        ).fetchall()
    except sqlite3.Error as e:
        print(f"qatena: autoexport check failed to list registers: {e!r}", file=sys.stderr)
        return
    for key in [k for k, p in _EXPORT_INFLIGHT.items() if p.poll() is not None]:
        del _EXPORT_INFLIGHT[key]
    for reg in regs:
        key = (reg["root_uuid"], reg["emit_path"])
        try:
            payload = autoexport.compute_stack(
                conn, reg["root_uuid"], reg["reblade_fidelity"])
            if payload is None:
                continue  # root died; its register row was cascade-deleted with it
            sig = autoexport.stack_signature(payload)
            if sig == reg["last_sig"]:
                continue
            if os.environ.get("QATENA_EXPORT_SYNC") == "1":
                autoexport.export(vault_path, key[0], key[1], reg["emit_format"])
            elif key not in _EXPORT_INFLIGHT:
                _EXPORT_INFLIGHT[key] = autoexport.spawn_exporter(
                    vault_path, key[0], key[1], reg["emit_format"])
        except Exception as e:  # self-healing on the next changed write
            print(f"qatena: autoexport failed for register {key}: {e!r}", file=sys.stderr)


def _handle(armory_dir, msg):
    method = msg.get("method")
    has_id = "id" in msg
    msg_id = msg.get("id") if has_id else None
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    if method is None:
        if has_id:
            _send_error(msg_id, -32600, "invalid request")
        return

    if method == "initialize":
        pv = params.get("protocolVersion")
        _send_result(msg_id, {
            "protocolVersion": pv if isinstance(pv, str) and pv else _DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "qatena", "version": __version__},
        })
        return

    if not has_id:
        return  # notification (notifications/* or any id-less request)

    if method == "ping":
        _send_result(msg_id, {})
        return

    if method == "tools/list":
        _send_result(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        if name == "vambrace":
            payload, is_error = _call_vambrace(armory_dir, arguments)
        else:
            payload, is_error = _call_routed(armory_dir, name, arguments)
        _send_result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}],
            "isError": is_error,
        })
        return

    _send_error(msg_id, -32601, "method not found")


def serve(armory_dir: str) -> int:
    """Serial MCP stdio loop until stdin EOF. Returns 0.

    armory_dir: directory holding the vambrace files (<key>.db). The server keeps
    no routing state — routing is the files themselves, so it survives restart.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            print(f"qatena: ignoring malformed json line", file=sys.stderr)
            _send_error(None, -32700, "parse error")
            continue
        if not isinstance(msg, dict):
            print("qatena: ignoring non-object json line", file=sys.stderr)
            _send_error(None, -32700, "parse error")
            continue
        try:
            _handle(armory_dir, msg)
        except Exception as e:
            print(f"qatena: handler error for {msg.get('method')!r}: {e!r}", file=sys.stderr)
            if "id" in msg:
                _send_error(msg.get("id"), -32603, f"internal error: {type(e).__name__}")
    return 0
