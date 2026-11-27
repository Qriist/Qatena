"""Auto-linking engine (SPEC 1.6).

Loaded alias registry (name column of every entity space):
  chain_headers.name  -> space "chain"
  chain_sections.name -> space "section"
  open_items.name     -> space "open_item"
  rulings.name        -> space "ruling"
  rulings.number      -> space "ruling"
  terms.term          -> space "term"

Exact match: the alias (casefolded) appears in the casefolded text at word
boundaries — works for multi-word aliases like "The Forge".
Candidates: aliases that do not exact-match but where some text token of
>= 4 chars is a prefix of the alias, or Levenshtein distance <= 2. Aliases
shorter than 3 chars are skipped for candidacy. Max 5 candidates, each with
its triggering token. Candidates are never linked.
"""

import re
import sqlite3

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def load_aliases(conn) -> list:
    """Return the alias registry as dicts: {"alias", "id", "space"}."""
    rows = conn.execute(
        """
        SELECT name AS alias, id, 'chain'     AS space FROM chain_headers
        UNION ALL SELECT name,   id, 'section'   FROM chain_sections
        UNION ALL SELECT name,   id, 'open_item' FROM open_items
        UNION ALL SELECT name,   id, 'ruling'    FROM rulings
        UNION ALL SELECT number, id, 'ruling'    FROM rulings
        UNION ALL SELECT term,   id, 'term'      FROM terms
        """
    ).fetchall()
    return [{"alias": r["alias"], "id": r["id"], "space": r["space"]} for r in rows]


def levenshtein(a: str, b: str, limit: int = 2) -> int:
    """Plain two-row DP Levenshtein distance; capped early by length delta."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    if a == b:
        return 0
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        ca = a[i - 1]
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _tokens(text: str) -> list:
    """Distinct word tokens of the casefolded text, first-appearance order."""
    seen = set()
    out = []
    for t in _TOKEN_RE.findall(text.casefold()):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def analyze(text: str, aliases: list):
    """Run the SPEC 1.6 analysis over written blade text.

    Returns (exact, candidates):
      exact      — alias entries whose alias appears in the text at word
                   boundaries (deduped by entity id, so a ruling matched via
                   both name and number counts once);
      candidates — near-matches, each {"name", "space", "token"}, max 5.
    """
    tcf = text.casefold()
    exact = []
    exact_ids = set()
    for a in aliases:
        alias_cf = a["alias"].casefold()
        if not alias_cf or len(alias_cf) > len(tcf):
            continue
        if re.search(rf"(?<!\w){re.escape(alias_cf)}(?!\w)", tcf):
            if a["id"] not in exact_ids:
                exact_ids.add(a["id"])
                exact.append(a)

    tokens = _tokens(text)
    candidates = []
    for a in aliases:
        if a["id"] in exact_ids:
            continue
        alias_cf = a["alias"].casefold()
        if len(alias_cf) < 3:
            continue
        trigger = None
        for t in tokens:
            if (len(t) >= 4 and alias_cf.startswith(t)) or levenshtein(t, alias_cf) <= 2:
                trigger = t
                break
        if trigger is not None:
            candidates.append({"name": a["alias"], "space": a["space"], "token": trigger})
            if len(candidates) >= 5:
                break
    return exact, candidates


def mint_name_refs(conn, src_uuid, exact_hits, now) -> list:
    """Insert name-ref pointers from src to every exact-matched entity.

    INSERT OR IGNORE makes this idempotent on (src, dst, kind). Returns only
    the pointers actually created now, as {"src", "dst", "kind"} dicts.
    """
    minted = []
    seen = set()
    for hit in exact_hits:
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO pointers (src_uuid, dst_uuid, kind, created) "
            "VALUES (?, ?, 'name-ref', ?)",
            (src_uuid, hit["id"], now),
        )
        if cur.rowcount:
            minted.append({"src": src_uuid, "dst": hit["id"], "kind": "name-ref"})
    return minted
