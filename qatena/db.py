"""Connection factory and time helpers (SPEC 1.2, 2)."""

import re
import sqlite3
import time
from datetime import datetime, timezone

# Tool-surface timestamp format: YYYY-MM-DDTHH:MM:SS.sssZ
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$")


def connect(path):
    """Open the Qatena DB with the SPEC 2 pragmas applied.

    autocommit=True: all transaction control is explicit (BEGIN IMMEDIATE /
    COMMIT / ROLLBACK), which is exactly the SPEC 1.4 write rule.
    check_same_thread=False: the stdio server processes messages serially on
    a single thread; allowing cross-thread use lets test harnesses share the
    one connection without extra plumbing.
    """
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def now_ns() -> int:
    """Current time as Unix epoch nanoseconds (storage format, SPEC 1.2)."""
    return time.time_ns()


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Exact integer days since 1970-01-01 (Hinnant's days_from_civil)."""
    year -= 1 if month <= 2 else 0
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def ns_to_iso(ns) -> str:
    """Render stored Unix ns as the tool-surface ISO-8601 UTC millisecond form."""
    ns = int(ns)
    secs, rem = divmod(ns, 1_000_000_000)
    ms = rem // 1_000_000
    dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{ms:03d}Z"


def iso_to_ns(value) -> int:
    """Parse the tool-surface ISO-8601 UTC millisecond form back to Unix ns.

    Pure integer arithmetic — no float round-trip. Raises ValueError on any
    deviation from the exact surface format.
    """
    m = _ISO_RE.match(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError(
            f"expected ISO-8601 UTC millis like 2026-11-27T06:31:00.123Z, got {value!r}"
        )
    y, mo, d, hh, mm, ss, ms = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31 and hh <= 23 and mm <= 59 and ss <= 59):
        raise ValueError(f"timestamp component out of range: {value!r}")
    days = _days_from_civil(y, mo, d)
    return (days * 86400 + hh * 3600 + mm * 60 + ss) * 1_000_000_000 + ms * 1_000_000
