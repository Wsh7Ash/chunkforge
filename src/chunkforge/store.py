"""Content-addressed chunk store with deduplication.

Objects are named by the SHA-256 of their contents, which gives three properties
for free:

* **Integrity.** A read that hashes to its own name is intact, so corruption is
  detectable without a second checksum scheme.
* **Idempotence.** Storing the same bytes twice is a no-op, so a retried upload
  cannot duplicate anything.
* **Deduplication.** Two files that share a chunk share the stored object.

Layout under the store root::

    objects/ab/cdef0123...   two-level fanout, keeps directories small
    tmp/                     staging area for atomic writes
    index.json               cache of digest -> stored size

The fanout is on the first byte of the hex digest, so a store with a million
objects has 256 directories rather than a million. Two levels means 65,536 leaf
directories, which is the point where filesystem fanout stops helping.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

__all__ = [
    "ChunkStore",
    "CorruptChunk",
    "MissingChunk",
    "StoredChunk",
    "StoreStats",
    "digest_of",
]

#: Bytes of the hex digest used for directory fanout.
FANOUT = 2


class MissingChunk(KeyError):
    """Raised when a digest is not present in the store."""


class CorruptChunk(Exception):
    """Raised when a stored object's contents do not match its name."""


def digest_of(data: bytes) -> str:
    """The canonical name of a chunk: lowercase hex SHA-256."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StoredChunk:
    digest: str
    length: int
    created: bool
    """``False`` when the object was already present, i.e. deduplicated."""


@dataclass(frozen=True)
class StoreStats:
    objects: int
    logical_bytes: int
    stored_bytes: int
    """Bytes on disk, which is smaller than ``logical_bytes`` by filesystem block
    rounding. ``unique_bytes`` is the figure to compare against a backup size."""

    unique_bytes: int

    @property
    def overhead_bytes(self) -> int:
        return self.stored_bytes - self.unique_bytes


class ChunkStore:
    """A directory of content-addressed chunks.

    Safe to share between processes for writing in the sense that concurrent
    writers of the *same* object converge: the write is atomic (staged in
    ``tmp/`` then renamed), so a reader never sees a half-written object. This
    library does not take a lock, and does not try to be a filesystem.
    """

    def __init__(self, root: os.PathLike | str) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.tmp_dir = self.root / "tmp"
        self._index_path = self.root / "index.json"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- locations

    def path_for(self, digest: str) -> Path:
        """Where a digest lives. Pure path arithmetic; does not touch disk."""
        return self.objects_dir / digest[:FANOUT] / digest[FANOUT:]

    def has(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def __contains__(self, digest: str) -> bool:
        return self.has(digest)

    def __len__(self) -> int:
        return self.stats().objects

    # ----------------------------------------------------------------- write

    def put(self, data: bytes) -> StoredChunk:
        """Store ``data`` and return its digest.

        Idempotent: if the digest is already present nothing is written and
        ``created`` is ``False``. That is the deduplication path, and it is the
        reason the caller can retry freely.
        """
        digest = digest_of(data)
        target = self.path_for(digest)

        if target.is_file():
            return StoredChunk(digest, len(data), created=False)

        target.parent.mkdir(parents=True, exist_ok=True)

        # Stage in the same filesystem as the target so the rename is atomic.
        handle, staged = tempfile.mkstemp(dir=self.tmp_dir, prefix="obj-")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(staged, target)
        except BaseException:
            # Never leave debris behind on failure, including a Ctrl-C.
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise

        return StoredChunk(digest, len(data), created=True)

    # ------------------------------------------------------------------ read

    def get(self, digest: str, verify: bool = True) -> bytes:
        """Return the chunk named ``digest``.

        With ``verify`` (the default) the contents are hashed and compared to the
        name, so silent corruption surfaces as :class:`CorruptChunk` rather than
        as a silently wrong file after reassembly.
        """
        target = self.path_for(digest)
        try:
            data = target.read_bytes()
        except FileNotFoundError as exc:
            raise MissingChunk(
                f"chunk {digest} is not in the store at {target}"
            ) from exc

        if verify:
            actual = digest_of(data)
            if actual != digest:
                raise CorruptChunk(
                    f"chunk at {target} is corrupt: stored as {digest}, "
                    f"contents hash to {actual}"
                )
        return data

    def read_into(self, digests: list[str], sink, verify: bool = True) -> int:
        """Concatenate chunks into a writable ``sink``; return the byte count.

        Used by reassembly. Streams chunk by chunk, so reconstructing a large
        file never holds more than one chunk in memory.
        """
        written = 0
        for digest in digests:
            data = self.get(digest, verify=verify)
            sink.write(data)
            written += len(data)
        return written

    def iter_objects(self) -> Iterator[str]:
        """Yield every stored digest, in sorted order."""
        if not self.objects_dir.is_dir():
            return
        for shard in sorted(self.objects_dir.iterdir()):
            if not shard.is_dir():
                continue
            for obj in sorted(shard.iterdir()):
                if obj.is_file():
                    yield shard.name + obj.name

    # -------------------------------------------------------------- lifecycle

    def delete(self, digest: str) -> bool:
        """Remove one object. Returns whether it existed."""
        target = self.path_for(digest)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        # Tidy up the now-possibly-empty shard directory.
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return True

    def collect_garbage(self, reachable: set[str]) -> tuple[int, int]:
        """Delete every stored object not in ``reachable``.

        Returns ``(deleted, bytes_reclaimed)``. Callers derive ``reachable``
        from their manifests rather than from a refcount, because a refcount
        that drifts out of step with reality is how backup tools lose data.
        """
        wanted = {d for d in reachable}
        deleted = 0
        reclaimed = 0
        for digest in list(self.iter_objects()):
            if digest in wanted:
                continue
            path = self.path_for(digest)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if self.delete(digest):
                deleted += 1
                reclaimed += size
        return deleted, reclaimed

    def stats(self) -> StoreStats:
        objects = 0
        unique = 0
        stored = 0
        for digest in self.iter_objects():
            objects += 1
            try:
                info = self.path_for(digest).stat()
            except OSError:
                continue
            unique += info.st_size
            stored += (info.st_size + 4095) // 4096 * 4096
        return StoreStats(
            objects=objects, logical_bytes=unique, stored_bytes=stored, unique_bytes=unique
        )

    def verify_all(self) -> list[CorruptChunk]:
        """Hash every object. Returns the problems; empty means intact."""
        problems: list[CorruptChunk] = []
        for digest in self.iter_objects():
            try:
                self.get(digest, verify=True)
            except CorruptChunk as exc:
                problems.append(exc)
            except OSError as exc:  # unreadable
                problems.append(CorruptChunk(f"{digest}: {exc}"))
        return problems

    # ----------------------------------------------------------------- debug

    def __repr__(self) -> str:
        return f"ChunkStore({str(self.root)!r})"
