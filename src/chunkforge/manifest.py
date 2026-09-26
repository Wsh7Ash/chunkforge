"""Manifests: the map from a file to the chunks that make it up.

A manifest is the only thing you need to rebuild a file, and it is small --
digests and lengths, no payload. It records the whole-file digest as well, so a
round trip through the store can be checked end to end and not merely
chunk by chunk.

The format is versioned. Unknown versions are rejected rather than guessed at,
because a manifest that is silently misread is a file that is silently rebuilt
wrong.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "Manifest",
    "ChunkRef",
    "MANIFEST_VERSION",
    "ManifestError",
    "UnsupportedVersion",
]

MANIFEST_VERSION = 1


class ManifestError(Exception):
    """Raised for a structurally invalid manifest."""


class UnsupportedVersion(ManifestError):
    """Raised when a manifest was written by a newer version of this library."""


@dataclass(frozen=True)
class ChunkRef:
    digest: str
    length: int

    def as_json(self) -> dict[str, Any]:
        return {"digest": self.digest, "length": self.length}

    @classmethod
    def from_json(cls, raw: Any) -> "ChunkRef":
        if not isinstance(raw, dict):
            raise ManifestError(f"chunk entry must be an object, got {type(raw).__name__}")
        digest = raw.get("digest")
        length = raw.get("length")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestError(f"chunk digest must be 64 hex chars, got {digest!r}")
        if any(c not in "0123456789abcdef" for c in digest):
            raise ManifestError(f"chunk digest must be lowercase hex, got {digest!r}")
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            # bool is checked first: isinstance(True, int) is True in Python, so
            # a JSON ``true`` would otherwise be silently read as a length of 1.
            raise ManifestError(f"chunk length must be a positive int, got {length!r}")
        return cls(digest=digest, length=length)


@dataclass(frozen=True)
class Manifest:
    """One file, chunked and addressed."""

    name: str
    """Logical name, usually the path it was ingested from. Metadata only --
    it is not used to open anything."""

    size: int
    file_digest: str
    chunks: list[ChunkRef]
    version: int = MANIFEST_VERSION
    created: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    """The :class:`~chunkforge.chunker.ChunkerConfig` in force. Recorded because
    a manifest is only reproducible if you know how the boundaries were chosen."""

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ManifestError(f"size must be >= 0, got {self.size}")
        if not isinstance(self.chunks, list):
            raise ManifestError("chunks must be a list")
        total = sum(c.length for c in self.chunks)
        if total != self.size:
            raise ManifestError(
                f"chunk lengths sum to {total} but size is {self.size}; "
                "manifest is internally inconsistent"
            )

    @property
    def unique_bytes(self) -> int:
        """Bytes actually stored for this file, counting each digest once.

        A file cannot dedupe against itself, so this normally equals
        ``sum(c.length for c in chunks)``. It is spelled out because the store's
        figure is global and comparing the two is how you tell whether
        deduplication is working.
        """
        return sum({c.digest: c.length for c in self.chunks}.values())

    def reachable(self) -> set[str]:
        """Digests this manifest depends on -- the input to garbage collection."""
        return {c.digest for c in self.chunks}

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "size": self.size,
            "file_digest": self.file_digest,
            "created": self.created,
            "config": self.config,
            "chunks": [c.as_json() for c in self.chunks],
        }

    @classmethod
    def from_json(cls, raw: Any) -> "Manifest":
        if not isinstance(raw, dict):
            raise ManifestError(f"manifest must be an object, got {type(raw).__name__}")
        version = raw.get("version")
        if not isinstance(version, int):
            raise ManifestError(f"manifest version must be an int, got {version!r}")
        if version > MANIFEST_VERSION:
            raise UnsupportedVersion(
                f"manifest version {version} is newer than this library "
                f"understands ({MANIFEST_VERSION}); upgrade chunkforge"
            )
        if version < 1:
            raise ManifestError(f"manifest version must be >= 1, got {version}")

        size = raw.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ManifestError(f"manifest size must be an int, got {size!r}")
        file_digest = raw.get("file_digest")
        if not isinstance(file_digest, str) or len(file_digest) != 64:
            raise ManifestError(f"file_digest must be 64 hex chars, got {file_digest!r}")

        chunks_raw = raw.get("chunks")
        if not isinstance(chunks_raw, list):
            raise ManifestError("manifest chunks must be a list")

        return cls(
            name=raw.get("name") or "",
            size=size,
            file_digest=file_digest,
            chunks=[ChunkRef.from_json(c) for c in chunks_raw],
            version=version,
            created=raw.get("created") or "",
            config=raw.get("config") or {},
        )

    # ------------------------------------------------------------------- I/O

    def write(self, path: os.PathLike | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash never leaves a truncated manifest, which
        # would be indistinguishable from a corrupt one.
        staged = target.with_name(target.name + ".tmp")
        staged.write_text(json.dumps(self.as_json(), indent=2) + "\n", encoding="utf-8")
        os.replace(staged, target)

    @classmethod
    def read(cls, path: os.PathLike | str) -> "Manifest":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
