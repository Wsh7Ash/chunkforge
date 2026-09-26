"""Gear rolling hash for content-defined chunking.

A "gear" hash is a rolling hash in the Buzhash family specialised to bytes. It
keeps a single 64-bit state, and folding in a new byte is one shift, one add and
one mask. That is cheap enough to run over every byte of every file, which is
the whole point: the chunker needs to hash the *entire* stream, not a sampled
window, because it must find a boundary at exactly the right byte.

The table is generated from a fixed seed with SplitMix64 rather than being pasted
in as 256 magic numbers. That keeps it auditable -- anyone can regenerate it and
get the same bytes -- and :data:`GEAR_DIGEST` pins the result so an accidental
change to the generator is caught by a test rather than silently altering every
chunk boundary in every archive built with this library.
"""

from __future__ import annotations

import hashlib
from typing import Iterator

__all__ = [
    "MASK64",
    "GEAR_SEED",
    "GEAR_TABLE",
    "GEAR_DIGEST",
    "build_gear_table",
    "gear_hash",
    "gear_hash_from",
]

MASK64 = 0xFFFFFFFFFFFFFFFF

#: Fixed seed. Changing it changes every boundary, so it is part of the format.
GEAR_SEED = 0x9E3779B97F4A7C15


def _splitmix64(seed: int) -> Iterator[int]:
    """Yield 64-bit values from SplitMix64.

    Chosen because it is short enough to read and audit, and because it is
    specified exactly, so the table is reproducible on any platform and any
    Python version -- unlike :func:`random` or :func:`hash`, both of which are
    unsuitable for a format-defining constant.
    """
    state = seed & MASK64
    while True:
        state = (state + 0x9E3779B97F4A7C15) & MASK64
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        yield z ^ (z >> 31)


def build_gear_table(seed: int = GEAR_SEED) -> tuple[int, ...]:
    """Build the 256-entry substitution table for ``seed``."""
    gen = _splitmix64(seed)
    return tuple(next(gen) for _ in range(256))


#: ``table[byte]`` -- the contribution of each possible byte.
GEAR_TABLE: tuple[int, ...] = build_gear_table()

#: Digest of the packed table, pinned by a test.
GEAR_DIGEST: str = hashlib.sha256(
    b"".join(value.to_bytes(8, "big") for value in GEAR_TABLE)
).hexdigest()


def gear_hash(data: bytes) -> int:
    """Hash ``data`` from scratch and return the 64-bit state."""
    table = GEAR_TABLE
    h = 0
    for byte in data:
        h = ((h << 1) + table[byte]) & MASK64
    return h


def gear_hash_from(prefix: int, data: bytes) -> int:
    """Continue a hash whose state is already ``prefix``.

    This is what makes the hash *rolling*: the caller keeps the state between
    chunks instead of rehashing, so overlapping windows cost only the bytes they
    actually cover. ``gear_hash(data) == gear_hash_from(gear_hash(b"ab"), b"c")``
    holds for all inputs.
    """
    table = GEAR_TABLE
    h = prefix
    for byte in data:
        h = ((h << 1) + table[byte]) & MASK64
    return h
