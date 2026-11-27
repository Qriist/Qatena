"""UUIDv7 generator (SPEC 1.1).

Layout (128 bits):
  [0:48]   unix timestamp, milliseconds since epoch
  [48:52]  version 7 (0b0111)
  [52:64]  rand_a — 12 random bits
  [64:66]  variant 10 (0b10)
  [66:128] rand_b — 62 random bits

Not built on uuid.uuid7; uses time.time_ns() and secrets for the random
bits. Returns the canonical 36-char lowercase hyphenated string.
"""

import secrets
import time


def new_uuid7() -> str:
    ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    g4 = (0b10 << 14) | (rand_b >> 48)   # variant bits 10 + top 14 bits of rand_b
    g5 = rand_b & 0xFFFFFFFFFFFF         # bottom 48 bits of rand_b
    return (
        f"{ms >> 16:08x}-{ms & 0xFFFF:04x}-"
        f"7{rand_a:03x}-"
        f"{g4:04x}-"
        f"{g5:012x}"
    )
