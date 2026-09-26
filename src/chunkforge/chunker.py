"""Content-defined chunking.

Fixed-size splitting is the obvious approach and the wrong one. Insert one byte
near the front of a file and every subsequent boundary shifts by one, so the
whole tail re-stores and deduplication collapses. Content-defined chunking
instead cuts where the *content* says to cut, via a rolling hash, so a local
edit only disturbs the chunks around it and everything after it still matches
what is already stored.

This module turns bytes into chunks. It has no I/O, no storage and no policy --
see :mod:`chunkforge.store` and :mod:`chunkforge.forge` for those.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .gear import GEAR_TABLE, MASK64

__all__ = [
    "ChunkerConfig",
    "DEFAULT_CONFIG",
    "chunk_data",
    "chunk_stream",
    "chunk_digests",
    "average_size_for_bits",
    "bits_for_average_size",
]


def average_size_for_bits(bits: int) -> int:
    """Expected chunk length for a mask of ``bits`` low bits.

    The boundary test is ``(state & mask) == 0``, so with ``bits`` independent
    low bits the expected wait is ``2**bits`` bytes. The realised distribution
    is close to geometric around that mean, which is why the tail is long --
    a single unlucky run can produce a chunk far above the average.
    """
    if not 0 <= bits <= 32:
        raise ValueError(f"bits must be within 0..32, got {bits}")
    return 1 << bits


def bits_for_average_size(average: int) -> int:
    """Inverse of :func:`average_size_for_bits`."""
    if average < 1:
        raise ValueError(f"average must be >= 1, got {average}")
    return average.bit_length() - 1


@dataclass(frozen=True)
class ChunkerConfig:
    """Tunables for the chunker.

    ``min_size`` and ``max_size`` are hard guarantees: no chunk is ever shorter
    or longer, whatever the data. ``max_size`` earns its keep because a run with
    no boundary -- a long stretch of identical bytes, say -- would otherwise
    produce one enormous chunk and defeat deduplication entirely.

    With ``normalised=True`` the mask is tightened near the average and loosened
    outside it, which is the FastCDC refinement. It does not change *which* byte
    positions are eligible to be a boundary, only how eagerly the chunker takes
    one, and it pulls the size distribution in towards the average. The trade-off
    is that it needs a within-chunk position, which a streaming chunker knows
    exactly, so nothing is approximated.
    """

    min_size: int = 2048
    bits: int = 14
    max_size: int = 65536
    normalised: bool = False

    def __post_init__(self) -> None:
        if self.min_size < 1:
            raise ValueError(f"min_size must be >= 1, got {self.min_size}")
        if self.max_size < self.min_size:
            raise ValueError(
                f"max_size ({self.max_size}) must be >= min_size ({self.min_size})"
            )
        if not 0 <= self.bits <= 32:
            raise ValueError(f"bits must be within 0..32, got {self.bits}")

    @property
    def mask(self) -> int:
        """Boundary mask: ``bits`` low bits must all be zero."""
        return (1 << self.bits) - 1

    @property
    def average_size(self) -> int:
        return average_size_for_bits(self.bits)

    def describe(self) -> str:
        return (
            f"min={self.min_size} avg~{self.average_size} max={self.max_size} "
            f"bits={self.bits} normalised={self.normalised}"
        )


DEFAULT_CONFIG = ChunkerConfig()


def _residue_masks(config: ChunkerConfig) -> tuple[int, int, int, int]:
    """Return ``(strict_bits, loose_end, mid_bits, strict_end)`` for FastCDC.

    Hoisted out of the byte loop so the hot path is a compare rather than three
    integer comparisons per byte.
    """
    typical = config.average_size
    loose_end = typical - typical // 8
    strict_end = typical + typical // 4
    return (config.bits + 2, loose_end, config.bits + 1, strict_end)


def chunk_stream(
    source: BinaryIO, config: ChunkerConfig = DEFAULT_CONFIG, read_size: int = 1 << 20
) -> Iterator[bytes]:
    """Yield chunks from a binary stream.

    Memory is bounded by ``max_size + read_size`` regardless of input size: the
    buffer is compacted as soon as a chunk is emitted, so nothing accumulates.

    Three things this got wrong first, kept here as comments because each one
    fails silently rather than loudly:

    * Building each chunk with ``buffer.append(byte)`` then ``bytes(buffer)``
      costs more than the hashing itself. Chunks are cut by slicing instead.
    * Without ``hashed`` -- how far the gear state has advanced through the
      current chunk -- a fresh read rescans bytes already folded into ``state``
      and corrupts the rolling hash, yielding a few enormous chunks.
    * Flushing the remainder at EOF has to go through the same boundary search.
      Emitting it directly ignores ``max_size`` and hands back one enormous
      final chunk for every file.
    """
    if read_size < 1:
        raise ValueError(f"read_size must be >= 1, got {read_size}")

    # Local aliases. Each of these is a global or attribute lookup that would
    # otherwise be repeated once per input byte.
    table = GEAR_TABLE
    mask64 = MASK64
    max_size = config.max_size
    min_size = config.min_size
    normalised = config.normalised
    base_mask = config.mask
    strict_bits, loose_end, mid_bits, strict_end = _residue_masks(config)
    loose_bits = config.bits - 2

    buffer = bytearray()
    start = 0  # offset in buffer where the chunk in progress begins
    hashed = 0  # bytes of that chunk already folded into `state`
    state = 0
    at_eof = False

    while True:
        n = len(buffer)
        cut = -1

        # A memoryview is rebuilt each pass because `del buffer[:cut]` below
        # invalidates any view over it. Iterating the view yields ints directly,
        # which is measurably cheaper than indexing the bytearray per byte.
        for offset, byte in enumerate(memoryview(buffer)[hashed:n], hashed):
            state = ((state << 1) + table[byte]) & mask64
            position = offset + 1 - start

            if position >= max_size:
                cut = offset + 1
                break

            if position < min_size:
                continue

            if normalised:
                if position < loose_end:
                    active = (1 << strict_bits) - 1
                elif position < strict_end:
                    active = (1 << mid_bits) - 1
                else:
                    active = (1 << loose_bits) - 1
            else:
                active = base_mask

            if (state & active) == 0:
                cut = offset + 1
                break

        if cut >= 0:
            yield bytes(buffer[start:cut])
            del buffer[:cut]
            start = 0
            hashed = 0
            state = 0
            continue

        hashed = n

        if at_eof:
            if start < n:
                yield bytes(buffer[start:])
            return

        block = source.read(read_size)
        if block:
            buffer += block
        else:
            at_eof = True


def chunk_data(data: bytes, config: ChunkerConfig = DEFAULT_CONFIG) -> list[bytes]:
    """Split an in-memory buffer into chunks."""
    import io

    return list(chunk_stream(io.BytesIO(data), config))


def chunk_digests(
    data: bytes, config: ChunkerConfig = DEFAULT_CONFIG
) -> list[tuple[str, int]]:
    """Return ``[(sha256_hex, length), ...]`` for ``data``.

    This is what a manifest stores, so it is the function to reach for when you
    want boundaries without the payload.
    """
    return [
        (hashlib.sha256(chunk).hexdigest(), len(chunk))
        for chunk in chunk_data(data, config)
    ]
