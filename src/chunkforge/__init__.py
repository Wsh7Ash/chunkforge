"""chunkforge -- content-defined chunking with deduplication and resumable ingest.

Store a file, and store only what you have not stored before. Change a byte in
the middle and only the chunks around it are rewritten. Interrupt an ingest and
pick it up where it stopped.

    from chunkforge import ChunkForge

    forge = ChunkForge("archive")
    result = forge.add("notes.md")
    print(f"{result.dedupe_ratio:.0%} of it was already there")
    forge.restore("notes.md", "rebuilt.md")

The four pieces, in dependency order:

``gear``
    A deterministic rolling hash for chunk boundaries.
``chunker``
    Content-defined chunking, streaming, with configurable bounds.
``store``
    A content-addressed object store. Integrity and deduplication fall out of
    naming objects by the hash of their contents.
``forge``
    The high-level API: ingest, resume, verify, restore, collect.
``manifest``
    The durable record mapping a file to its chunks.

Nothing here depends on anything outside the standard library.
"""

from .chunker import (
    ChunkerConfig,
    average_size_for_bits,
    bits_for_average_size,
    chunk_data,
    chunk_digests,
    chunk_stream,
)
from .forge import ChunkForge, IngestResult, Interrupted, ResumeRefused
from .gear import GEAR_DIGEST, GEAR_SEED, GEAR_TABLE, gear_hash
from .manifest import (
    MANIFEST_VERSION,
    ChunkRef,
    Manifest,
    ManifestError,
    UnsupportedVersion,
)
from .store import (
    ChunkStore,
    CorruptChunk,
    MissingChunk,
    StoredChunk,
    StoreStats,
    digest_of,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # chunker
    "ChunkerConfig",
    "chunk_data",
    "chunk_digests",
    "chunk_stream",
    "average_size_for_bits",
    "bits_for_average_size",
    # store
    "ChunkStore",
    "StoredChunk",
    "StoreStats",
    "CorruptChunk",
    "MissingChunk",
    "digest_of",
    # manifest
    "Manifest",
    "ChunkRef",
    "MANIFEST_VERSION",
    "ManifestError",
    "UnsupportedVersion",
    # forge
    "ChunkForge",
    "IngestResult",
    "ResumeRefused",
    "Interrupted",
    # gear
    "GEAR_TABLE",
    "GEAR_SEED",
    "GEAR_DIGEST",
    "gear_hash",
]
