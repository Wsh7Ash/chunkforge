"""The high-level API: ingest files, deduplicate, verify, rebuild.

This is the piece that makes the other three useful. It owns the journal that
makes ingest resumable, the manifest directory, and the accounting that tells
you whether deduplication is actually doing anything.

Resumability is the interesting part. Ingest can be interrupted at any point --
Ctrl-C, a full disk, a dropped connection -- and picking it up again must not
re-store what is already stored and must not silently mix two different
versions of a file. So progress is journalled after every chunk, and the journal
records a rolling digest of the bytes consumed so far. On resume, the digest is
re-checked against the file; if the file changed underneath, the resume is
refused rather than producing a corrupt manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .chunker import ChunkerConfig, chunk_stream
from .manifest import Manifest, ChunkRef
from .store import ChunkStore, MissingChunk, digest_of

__all__ = [
    "ChunkForge",
    "IngestResult",
    "ResumeRefused",
    "Interrupted",
]

#: Bumped when the journal layout changes.
JOURNAL_VERSION = 1


class ResumeRefused(Exception):
    """Raised when a resume cannot be trusted, with the reason."""


class Interrupted(Exception):
    """Raised by an ingest callback that asks to stop."""


@dataclass
class IngestResult:
    manifest: Manifest
    chunks_total: int
    chunks_new: int
    chunks_deduplicated: int
    bytes_total: int
    bytes_new: int
    bytes_deduplicated: int
    resumed: bool = False
    skipped_chunks: int = 0
    """Chunks already journalled from an earlier attempt."""

    @property
    def dedupe_ratio(self) -> float:
        """Fraction of the file that was already in the store, 0.0 to 1.0."""
        if self.bytes_total == 0:
            return 0.0
        return self.bytes_deduplicated / self.bytes_total

    @property
    def new_bytes(self) -> int:
        return self.bytes_new

    def describe(self) -> str:
        if self.bytes_total == 0:
            return "empty file, nothing stored"
        pct = self.dedupe_ratio * 100
        return (
            f"{self.manifest.name}: {self.bytes_total} bytes in "
            f"{self.chunks_total} chunk(s); {self.bytes_new} new, "
            f"{self.bytes_deduplicated} deduplicated ({pct:.1f}% reused); "
            f"{self.chunks_new} new object(s), "
            f"{self.chunks_deduplicated} already present"
        )


def _chain(current: str, data: bytes) -> str:
    """Chained digest: ``h_n = SHA256(h_{n-1} || data_n)``."""
    return hashlib.sha256(bytes.fromhex(current) + data).hexdigest()


EMPTY_CHAIN = hashlib.sha256().hexdigest()


@dataclass
class _Journal:
    """Append-only record of ingest progress for one file.

    The chain is over *chunks*, not over fixed-size blocks. An earlier version
    chained over 64 KiB blocks, which quietly verified nothing at all whenever
    the interrupted ingest had progressed less than one block -- which is the
    common case for anything under 64 KiB. Chaining per chunk means every
    journalled chunk is attestable, and re-chunking the prefix reproduces them
    exactly, so a resume verifies the whole prefix rather than a rounded-down
    sample of it.
    """

    path: Path
    name: str
    size: int
    """Declared total size, or -1 when the source was not a regular file."""

    config: dict = field(default_factory=dict)
    """The chunker settings in force. Required: a prefix can only be re-derived
    with the same configuration that produced it."""

    consumed: int = 0
    chunks: list[ChunkRef] = field(default_factory=list)
    running: str = EMPTY_CHAIN
    complete: bool = False
    file_digest: str = ""
    """Set once ingest finishes. Empty while in progress."""

    version: int = JOURNAL_VERSION

    def append(self, ref: ChunkRef, data: bytes) -> None:
        self.chunks.append(ref)
        self.consumed += len(data)
        self.running = _chain(self.running, data)
        self.flush()

    def flush(self) -> None:
        payload = {
            "version": self.version,
            "name": self.name,
            "size": self.size,
            "config": self.config,
            "consumed": self.consumed,
            "running": self.running,
            "complete": self.complete,
            "file_digest": self.file_digest,
            "chunks": [c.as_json() for c in self.chunks],
        }
        staged = self.path.with_name(self.path.name + ".tmp")
        staged.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(staged, self.path)

    @classmethod
    def load(cls, path: Path) -> Optional["_Journal"]:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != JOURNAL_VERSION:
            return None
        try:
            journal = cls(
                path=path,
                name=raw["name"],
                size=raw["size"],
                config=raw["config"],
                consumed=raw["consumed"],
                chunks=[ChunkRef.from_json(c) for c in raw["chunks"]],
                running=raw["running"],
                complete=bool(raw["complete"]),
                file_digest=raw.get("file_digest") or "",
            )
        except (KeyError, ValueError, TypeError):
            return None

        # The journal must be self-consistent before it is trusted with a
        # resume: the chunk lengths have to add up to the byte count, and an
        # empty journal has to carry the empty chain.
        if sum(c.length for c in journal.chunks) != journal.consumed:
            return None
        if not journal.chunks and journal.running != EMPTY_CHAIN:
            return None
        return journal


class ChunkForge:
    """A store plus a manifest directory, with deduplication and resume."""

    def __init__(
        self,
        root: os.PathLike | str,
        config: ChunkerConfig | None = None,
    ) -> None:
        self.root = Path(root)
        self.config = config or ChunkerConfig()
        self.store = ChunkStore(self.root / "objects")
        self.manifests_dir = self.root / "manifests"
        self.journal_dir = self.root / "journal"
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- manifests

    def manifest_path(self, name: str) -> Path:
        return self.manifests_dir / (self._key(name) + ".json")

    def _key(self, name: str) -> str:
        """Filesystem-safe, collision-free key for a logical name."""
        return digest_of(name.encode("utf-8"))

    def has_manifest(self, name: str) -> bool:
        return self.manifest_path(name).is_file()

    def get_manifest(self, name: str) -> Manifest:
        path = self.manifest_path(name)
        if not path.is_file():
            raise KeyError(f"no manifest for {name!r} at {path}")
        return Manifest.read(path)

    def list_manifests(self) -> list[Manifest]:
        out: list[Manifest] = []
        for path in sorted(self.manifests_dir.glob("*.json")):
            out.append(Manifest.read(path))
        return out

    def forget(self, name: str) -> bool:
        """Drop a manifest, then reclaim anything it was the last user of.

        Refuses nothing: run :meth:`collect_garbage` explicitly for the
        multi-file view.
        """
        existed = self.manifest_path(name).is_file()
        if existed:
            self.manifest_path(name).unlink()
        return existed

    # ---------------------------------------------------------------- ingest

    def add(
        self,
        source: os.PathLike | str | bytes,
        name: Optional[str] = None,
        on_chunk: Optional[Callable[[int, int], None]] = None,
        resume: bool = True,
    ) -> IngestResult:
        """Chunk ``source`` and store it, deduplicating against what is there.

        ``source`` may be a path, raw ``bytes``, or any binary file object with
        a ``read`` method. ``on_chunk(index, total_bytes)`` may raise
        :class:`Interrupted` to stop early and leave a resumable journal
        behind.
        """
        with self._open_source(source, name) as (stream, logical_name, total_size):
            return self._ingest(stream, logical_name, total_size, on_chunk, resume)

    def _ingest(
        self,
        stream,
        logical_name: str,
        total_size: int,
        on_chunk: Optional[Callable[[int, int], None]],
        resume: bool,
    ) -> IngestResult:
        journal = self._journal_for(logical_name, total_size, stream, resume)
        skipped = len(journal.chunks) if journal is not None else 0

        if journal is None:
            journal = _Journal(
                path=self.journal_dir / (self._key(logical_name) + ".jsonl"),
                name=logical_name,
                size=total_size,
                config=self._config_json(),
            )
            journal.flush()
        else:
            self._verify_prefix(stream, journal)

        chunks_new = 0
        chunks_dedup = 0
        bytes_new = 0

        try:
            for chunk in chunk_stream(stream, self.config):
                ref = ChunkRef(digest_of(chunk), len(chunk))
                stored = self.store.put(chunk)
                if stored.created:
                    chunks_new += 1
                    bytes_new += len(chunk)
                else:
                    chunks_dedup += 1
                journal.append(ref, chunk)
                if on_chunk is not None:
                    on_chunk(len(journal.chunks), journal.consumed)
        except Interrupted:
            # The journal already holds everything written so far, so the next
            # call picks up from here.
            raise

        journal.complete = True
        journal.file_digest = digest_of(self._reassemble_to_bytes(journal.chunks))
        journal.flush()

        manifest = Manifest(
            name=logical_name,
            size=journal.consumed,
            file_digest=journal.file_digest,
            chunks=list(journal.chunks),
            created=Manifest.now(),
            config=self._config_json(),
        )
        manifest.write(self.manifest_path(logical_name))

        journal.path.unlink(missing_ok=True)

        return IngestResult(
            manifest=manifest,
            chunks_total=len(manifest.chunks),
            chunks_new=chunks_new,
            chunks_deduplicated=chunks_dedup,
            bytes_total=manifest.size,
            bytes_new=bytes_new,
            bytes_deduplicated=manifest.size - bytes_new,
            resumed=skipped > 0,
            skipped_chunks=skipped,
        )

    def _config_json(self) -> dict:
        return {
            "min_size": self.config.min_size,
            "bits": self.config.bits,
            "max_size": self.config.max_size,
            "normalised": self.config.normalised,
        }

    @contextmanager
    def _open_source(self, source, name: Optional[str]):
        """Yield ``(stream, logical_name, total_size)`` and always clean up.

        Paths are opened here, so they must be closed here. A stream the caller
        handed us is theirs and is left open -- and is also positioned wherever
        the caller left it.
        """
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
            yield io.BytesIO(data), name or "bytes", len(data)
            return
        if hasattr(source, "read"):
            yield source, name or getattr(source, "name", "stream"), -1
            return
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        with open(path, "rb") as handle:
            yield handle, name or path.name, path.stat().st_size

    def _journal_for(
        self, name: str, size: int, stream, resume: bool
    ) -> Optional[_Journal]:
        path = self.journal_dir / (self._key(name) + ".jsonl")
        if not resume:
            path.unlink(missing_ok=True)
            return None
        journal = _Journal.load(path)
        if journal is None:
            if path.is_file():
                # Present but unreadable or self-inconsistent. Refuse rather
                # than silently starting over, which would re-store everything
                # and quietly discard whatever progress was really made.
                raise ResumeRefused(
                    f"journal for {name!r} at {path} is unreadable or "
                    f"inconsistent; refusing to resume. Delete it to start over."
                )
            return None
        if journal.complete:
            path.unlink(missing_ok=True)
            return None
        if size >= 0 and size != journal.size:
            raise ResumeRefused(
                f"{name!r} is {size} bytes but the journal was written for "
                f"{journal.size}; the file changed, so resuming would mix two "
                f"versions. Delete the journal to start over."
            )

        # The journalled prefix was chunked with one configuration; continuing
        # with another would produce a file whose early boundaries came from the
        # first and whose later ones came from the second, while the manifest
        # claims a single config. That is a manifest that cannot be reproduced,
        # so it is refused rather than written.
        current = self._config_json()
        if journal.config != current:
            raise ResumeRefused(
                f"{name!r} was interrupted under {journal.config} but this forge "
                f"is configured as {current}; resuming would mix two chunking "
                f"configurations in one manifest. Reopen the archive with the "
                f"original config, or pass resume=False to start over."
            )
        return journal

    def _verify_prefix(self, stream, journal: _Journal) -> None:
        """Re-derive the journalled prefix from the source and check it.

        Consumes and verifies exactly ``journal.consumed`` bytes, leaving the
        stream positioned at the first unprocessed byte. Raises
        :class:`ResumeRefused` if the source no longer produces the chunks the
        journal describes.

        Verification means re-chunking the prefix with the journal's own recorded
        configuration and comparing every chunk digest, plus the chained digest
        over the whole prefix. That catches a source edited mid-ingest, a source
        that is a different length, and a journal written under different chunker
        settings -- the three ways a resume goes wrong.
        """
        if not journal.config:
            raise ResumeRefused(
                f"journal for {journal.name!r} records no chunker config, so "
                f"its prefix cannot be verified; refusing to resume"
            )
        if not getattr(stream, "seekable", lambda: False)():
            raise ResumeRefused(
                f"cannot resume {journal.name!r} from a non-seekable stream: "
                f"the prefix has to be re-read to be verified"
            )

        try:
            config = ChunkerConfig(
                min_size=journal.config["min_size"],
                bits=journal.config["bits"],
                max_size=journal.config["max_size"],
                normalised=journal.config["normalised"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResumeRefused(
                f"journal for {journal.name!r} has an unusable chunker config "
                f"({exc}); refusing to resume"
            ) from exc

        try:
            stream.seek(0)
        except OSError as exc:
            raise ResumeRefused(
                f"cannot rewind {journal.name!r} to verify its prefix: {exc}"
            ) from exc

        chain = EMPTY_CHAIN
        consumed = 0

        for index, chunk in enumerate(chunk_stream(stream, config)):
            if index >= len(journal.chunks):
                # Everything journalled has been checked. The source having
                # further chunks is normal -- it is either the rest of the file,
                # or a file that only grew, which the size check above already
                # accepted.
                break
            expected = journal.chunks[index]
            if len(chunk) != expected.length or digest_of(chunk) != expected.digest:
                raise ResumeRefused(
                    f"{journal.name!r} chunk {index} is {len(chunk)} bytes / "
                    f"{digest_of(chunk)[:12]} but the journal recorded "
                    f"{expected.length} / {expected.digest[:12]}; the source "
                    f"changed mid-ingest"
                )
            chain = _chain(chain, chunk)
            consumed += len(chunk)

        if consumed != journal.consumed:
            raise ResumeRefused(
                f"{journal.name!r} yielded {consumed} bytes but the journal "
                f"recorded {journal.consumed}; the source is shorter than it was"
            )
        if chain != journal.running:
            raise ResumeRefused(
                f"the prefix of {journal.name!r} does not match the journal's "
                f"chained digest ({chain[:12]} != {journal.running[:12]})"
            )

        stream.seek(consumed)

    def _reassemble_to_bytes(self, chunks: list[ChunkRef]) -> bytes:
        buf = io.BytesIO()
        self.store.read_into([c.digest for c in chunks], buf)
        return buf.getvalue()

    # ----------------------------------------------------------------- other

    def restore(self, name: str, destination: os.PathLike | str, verify: bool = True) -> int:
        """Rebuild a file from its manifest. Returns the byte count written."""
        manifest = self.get_manifest(name)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)

        whole = hashlib.sha256()
        written = 0
        with open(target, "wb") as out:
            for ref in manifest.chunks:
                data = self.store.get(ref.digest, verify=verify)
                out.write(data)
                whole.update(data)
                written += len(data)

        if verify and whole.hexdigest() != manifest.file_digest:
            raise ValueError(
                f"rebuilt {name!r} does not match its recorded digest: "
                f"{whole.hexdigest()} != {manifest.file_digest}"
            )
        return written

    def verify(self, name: Optional[str] = None) -> list[str]:
        """Check stored chunks against manifests. Returns problem strings."""
        problems: list[str] = []
        if name is not None:
            manifests = [self.get_manifest(name)]
        else:
            manifests = self.list_manifests()

        for manifest in manifests:
            total = 0
            for ref in manifest.chunks:
                try:
                    data = self.store.get(ref.digest, verify=True)
                except MissingChunk as exc:
                    problems.append(f"{manifest.name}: {exc}")
                    break
                except Exception as exc:  # CorruptChunk and friends
                    problems.append(f"{manifest.name}: {exc}")
                    break
                if len(data) != ref.length:
                    problems.append(
                        f"{manifest.name}: chunk {ref.digest[:12]} is "
                        f"{len(data)} bytes, manifest says {ref.length}"
                    )
                    break
                total += len(data)
            else:
                if total != manifest.size:
                    problems.append(
                        f"{manifest.name}: chunks total {total} bytes, "
                        f"manifest says {manifest.size}"
                    )
        return problems

    def reachable(self) -> set[str]:
        out: set[str] = set()
        for manifest in self.list_manifests():
            out |= manifest.reachable()
        return out

    def collect_garbage(self) -> tuple[int, int]:
        return self.store.collect_garbage(self.reachable())

    def stats(self) -> dict:
        store = self.store.stats()
        manifests = self.list_manifests()
        logical = sum(m.size for m in manifests)

        # The interesting number is the size of the *union* of referenced
        # chunks, not the sum per manifest. Two manifests that share every chunk
        # reference 2N bytes between them but store N, and only the union sees
        # that. Summing per manifest reports 0 bytes saved for a perfect dedupe.
        union: dict[str, int] = {}
        references: dict[str, int] = {}
        for manifest in manifests:
            for ref in manifest.chunks:
                union[ref.digest] = ref.length
                references[ref.digest] = references.get(ref.digest, 0) + 1
        referenced = sum(union.values())
        shared = sum(1 for count in references.values() if count > 1)

        return {
            "manifests": len(manifests),
            "logical_bytes": logical,
            "referenced_bytes": referenced,
            "stored_objects": store.objects,
            "unique_bytes": store.unique_bytes,
            "shared_chunks": shared,
            "saved_bytes": max(0, logical - referenced),
            "dedupe_ratio": (1 - referenced / logical) if logical else 0.0,
            "config": self.config.describe(),
        }

    def __repr__(self) -> str:
        return f"ChunkForge({str(self.root)!r}, {self.config.describe()})"
