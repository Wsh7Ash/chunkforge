# chunkforge

## 1. Problem

Backups store whole files. A file that changes costs a full copy, and a file
that is re-saved with one byte different is a new file as far as the backup is
concerned. On any archive with history this dominates: the current version of a
file is a rounding error against the accumulated earlier versions.

Content-defined chunking (CDC) fixes this. Instead of cutting a file at fixed
offsets, it cuts where the *content* says to, so an edit disturbs only the
chunks around it. Two files that share a region share the stored bytes.

This is a well-trodden idea — FastCDC, restic, borg, and others all do it. The
gap is a dependency-free, readable implementation in pure Python, where the
interesting parts (boundary selection, resume correctness, integrity) are
visible rather than hidden behind a C extension.

## 2. Goals

1. **Correct.** A file restored from the archive is byte-identical to the
   original, always. Verified by test, not by assertion.
2. **Deduplicating.** A one-byte edit to a multi-megabyte file must cost one
   chunk, not a file.
3. **Honest about interruption.** Ingest can be stopped at any point and
   resumed. A resume against a source that has changed must be **refused**, not
   completed against a mixture of two files.
4. **Verifiable.** Corruption anywhere in the store must be detectable, and
   detection must not depend on a second checksum scheme.
5. **Dependency-free.** Standard library only, at runtime and in the tests. A
   tool you reach for when a backup is already broken should not need a working
   package manager.
6. **Readable.** The mechanism should be understandable from the source. This
   is a teaching implementation as much as a working one.

## 3. Non-goals

- **Encryption.** Out of scope. The archive is plain files; use an encrypted
  filesystem.
- **Compression.** Content-defined chunking and compression compete for the same
  CPU. Combining them here would muddy both. Chunks could be compressed by
  wrapping the object layer.
- **Concurrency control.** No locking, no multi-writer coordination. Atomic
  writes mean concurrent writers of the *same* object converge, which is all
  that is claimed.
- **High throughput.** Pure Python, single-threaded, ~0.4 MiB/s. Fast enough to
  be correct and to be read; not a competitor to borg.
- **Remote backends.** S3, SSH, HTTP. The store is a directory; a remote backend
  would be a new `ChunkStore` subclass.
- **Snapshot scheduling.** chunkforge stores and rebuilds. Deciding what to
  store and when is the caller's business.

## 4. Constraints

- Python 3.9 and later.
- No third-party packages, runtime or test.
- The archive must be a plain directory of files, so it can be inspected,
  backed up, and synced with ordinary tools.
- Every write must be atomic: a crash must not leave a half-written object that
  is indistinguishable from a corrupt one.
- A format change must be detectable. A manifest written by a future version
  must be refused, not guessed at.

## 5. Design

### 5.1 Boundaries from content

A 64-bit Gear hash is rolled over the input one byte at a time:

```
h = (h << 1) + GEAR[b]
```

A chunk ends where the low `bits` bits of `h` are all zero, subject to
`min_size` and `max_size`. The table is generated from a fixed seed by
SplitMix64 and its digest is pinned in the test suite, so a change to the
generator cannot silently move every boundary in every existing archive.

The `min_size` floor suppresses a pathological case — a run of identical bytes
can produce many short chunks — and the `max_size` ceiling bounds the cost of
adversarial input, which is what keeps a 16-byte file from becoming one chunk.

### 5.2 Objects named by their hash

Each chunk is stored under the lowercase hex SHA-256 of its own contents,
sharded two levels deep by the leading bytes. Three properties follow:

- **Integrity.** A read that hashes to its own name is intact.
- **Idempotence.** Storing the same bytes twice is a no-op, so retries cannot
  duplicate anything.
- **Deduplication.** Sharing a chunk means sharing the object.

Writes are staged in a temporary directory on the same filesystem and renamed
into place, so a reader never sees a partial object and a crash never leaves
debris.

### 5.3 Manifests

A manifest is the ordered list of chunk digests plus the file's overall digest
and size. It is small, human-readable JSON, written atomically, and versioned.
Reading validates it: chunk lengths must sum to the declared size, digests must
be lowercase hex, and an unknown version is refused.

Each manifest records the chunker configuration that produced it, because a
manifest is only reproducible if you know how the boundaries were chosen.

### 5.4 Resume

Progress is journalled after every chunk, and the journal chains the chunks
with a running digest `h_n = SHA256(h_{n-1} || chunk_n)`.

On resume, chunkforge re-chunks the recorded prefix using the journalled config
and checks every chunk digest and the chain before storing anything new. It
refuses, with a reason, if the size changed, a prefix chunk does not match, the
source is shorter, the source is not seekable, the config differs, or the
journal is internally inconsistent.

> An earlier version chained over fixed 64 KiB blocks rather than chunks. That
> verified nothing at all whenever an ingest had progressed less than one block,
> which is every file under 64 KiB — the most common case. The chain is per
> chunk now, and the whole prefix is checked.

## 6. Acceptance criteria

| # | Criterion | Verified by |
| - | --------- | ----------- |
| 1 | Restore is byte-identical for any input, including empty and < `min_size` | `test_forge.py`, `test_chunker.py` |
| 2 | Chunking is independent of read size | `TestStreamEquivalence` |
| 3 | No chunk exceeds `max_size`; none but the last is under `min_size` | `TestInvariants` |
| 4 | Boundaries match an independent reimplementation of the hash arithmetic | `TestReferenceAgreement` |
| 5 | A one-byte edit reuses > 90% of a 300 KiB file | `test_a_modified_copy_reuses_almost_everything` |
| 6 | A byte inserted at the front reuses > 90%, where fixed-size reuses none | `test_a_prepended_file_reuses_almost_everything` |
| 7 | An interrupted ingest resumes and rebuilds the file exactly | `TestInterruption`, `demo.py` §6 |
| 8 | A resume against a changed, resized, or swapped source is refused, and writes nothing | `TestResumeRefusals`, `demo.py` §7 |
| 9 | A resume under a different config is refused | `test_a_different_chunker_config_cannot_resume` |
| 10 | A non-seekable source cannot resume | `test_a_non_seekable_stream_cannot_resume` |
| 11 | Corrupted and missing chunks are detected | `TestIntegrity`, `demo.py` §8 |
| 12 | Garbage collection keeps everything a manifest references, including shared chunks | `TestGarbageCollection` |
| 13 | A crashed write leaves no debris and no partial object | `test_staging_area_is_left_clean` |
| 14 | A future manifest version is refused, not misread | `test_rejects_a_future_version` |
| 15 | No file descriptor leaks across many ingests | `test_add_does_not_leak_file_handles` |
| 16 | The CLI is usable and returns distinct exit codes | `test_cli.py` |
| 17 | `demo.py` output is reproducible | seeded payload; verified by re-running |

## 7. Results

Full output in [RESULTS.md](../RESULTS.md) and `results/verification.txt`. Headline
figures on a 4 MiB seeded payload at the default configuration (220 chunks):

| Scenario | New chunks | Reused |
| -------- | ---------- | ------ |
| Identical file | 0 of 220 | 100.00% |
| One byte edited at the midpoint | 1 of 220 | 99.72% |
| One byte inserted at the front | 1 of 220 | 99.91% |
| Fixed-size split, same insertion | 2049 of 2049 | 0% |

## 8. Future work

In rough order of value:

1. **Speed.** The gear loop is the bottleneck. A `bytearray` window and local
   variable hoisting should help; a C extension would help far more.
2. **Compression**, below the object layer, as an option per archive.
3. **Sparse files**, so adding a large file of mostly zeros costs almost
   nothing.
4. **Retention**, pruning old manifests and then collecting what they alone
   referenced.
5. **A remote store**, as a `ChunkStore` subclass over S3 or SSH.
6. **A `--verify-all` that samples**, for large archives where a full hash pass
   is expensive.

## 9. Design decisions worth arguing with

**Fixed-size `index.json` cache in the store** — written by nothing, read by
nothing. The store is self-describing from its filesystem layout, so a cache
would be a second source of truth to keep in step. It should be deleted.

**SHA-256 rather than BLAKE3 or xxhash** — a hash has to be in the standard
library to keep the no-dependency promise, and SHA-256 is the one that is both
fast in `hashlib` and a real integrity check. BLAKE3 would be faster and would
give content-defined *hashing* as well, at the cost of a dependency.

**Gear rather than Buzhash** — Buzhash gives better distribution in theory; Gear
is one table lookup and one add per byte, which matters a great deal in Python.
The pinned table digest is the guard against silent drift.

**Refusing rather than repairing** — when a source has changed under an
interrupted ingest, the tempting options are to start over (wastes the work done
and silently discards progress) or to continue anyway (produces a file that is
part one version and part another, which is worse than useless in a backup).
Refusing, with a message that says what to do, is the only honest option.

**`.jsonl` for the journal** — the journal is a single JSON document rewritten
atomically, not a line-oriented log. The extension is misleading and should be
`.json`. It is kept for compatibility with archives already written during
development.
