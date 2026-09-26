# Architecture

Four modules, each depending only on the ones above it, plus a manifest format
that sits beside the store.

```
                    gear.py
                      |
                    chunker.py
                      |
   manifest.py ---- store.py
        \            /
         \          /
            forge.py
              |
            cli.py
```

There are no circular imports, and each module is independently testable.

---

## gear.py — the rolling hash

A Gear hash carries one 64-bit integer of state. For each input byte:

```
h = ((h << 1) + GEAR[b]) & 0xFFFFFFFFFFFFFFFF
```

The table is 256 pseudorandom 64-bit values, generated from a fixed seed by
SplitMix64. The property that matters is that a prefix hash can be continued:
`gear_hash_from(gear_hash(a), b) == gear_hash(a + b)`. That is what allows a
chunker to resume from a rolling state instead of re-hashing from the start.

The table's SHA-256 is pinned in `test_gear.py`:

```
ae29eb591df382f2324979d66ca9451f49c12a11168c98fccd5e8425c12ff376
```

Pinning it means a change to the generator, the seed, or the hashing shows up as
a test failure rather than as every boundary in every existing archive quietly
moving.

## chunker.py — boundaries from content

`chunk_stream(stream, config)` is a generator that reads in `read_size` blocks
and yields `bytes`.

**The hot loop is inlined on purpose.** It is the bottleneck — everything else
in the library is an order of magnitude faster — so it avoids the function call
per byte that a naive `gear_hash_from` composition would cost. The trade is
duplicated arithmetic, and `TestReferenceAgreement` exists to keep the fast copy
honest against the simple one.

**Three conditions end a chunk**, checked in this order:

1. `max_size` reached — unconditional. This is the backstop against adversarial
   or degenerate input; without it a long run of identical bytes becomes one
   enormous chunk.
2. `min_size` reached *and* the low `bits` bits of the hash are zero — the
   content-defined cut.
3. End of input — the final chunk may be short.

`min_size` is not cosmetic. Gear hashing has poor distribution on some inputs
(very low entropy, long repeats), and without a floor a chunk can be a handful
of bytes. The floor costs a little redundancy and bounds the object count.

**The hash state resets to zero at every boundary.** This is the standard
choice: it makes each chunk's boundaries depend only on its own content, so
identical regions in different files chunk identically. The cost is that an edit
shifts the first boundary of the *following* chunk, which is why one edit costs
one or two chunks rather than exactly one.

**`bits` is a mask width, not an average.** The mean chunk is `2**bits` bytes:
14 gives 16 KiB, 12 gives 4 KiB. `average_size_for_bits` and
`bits_for_average_size` convert, and the tests check they are inverses.

**`normalised=True`** applies FastCDC's normalisation: two thresholds below and
one above the mean, with a harder condition in the middle band. It costs a few
comparisons per byte and produces a tighter size distribution, which matters
when you are paying for a fixed number of objects.

**Streaming is an implementation detail, not a promise.** Chunking a 300 KiB
buffer and streaming it with `read_size=1` produce identical output, which
`TestStreamEquivalence` checks across seven read sizes. Memory is bounded by
`max_size + read_size`, not by input length.

## store.py — objects named by their contents

```
<root>/objects/ab/cdef0123…     two-level fanout on the hex digest
<root>/tmp/                     staging, same filesystem, for atomic renames
```

**`put` is idempotent.** If the digest is already present, nothing is written
and `created=False`. This is the deduplication path, and it is why a retried
ingest cannot duplicate anything.

**Writes are staged and renamed.** `tempfile.mkstemp` in `tmp/`, write,
`flush`, `fsync`, then `os.replace` onto the target. `os.replace` is atomic
within a filesystem, so a reader sees either the old state or the complete new
object, never a partial one. The `except BaseException` handler unlinks the
staged file, which matters because it also catches `KeyboardInterrupt` — a
Ctrl-C during ingest should not leave debris in the archive.

**`get` verifies by default.** It hashes what it read and compares against the
name the data was found under. Silent bit rot in the archive becomes
`CorruptChunk` at read time rather than a wrong file after reassembly.
`verify=False` is available for the rare case where you know the data is good
and want the copy anyway.

**Fanout is two levels of one hex byte**, giving 65,536 leaf directories. One
level would be fine up to about 65,000 objects; two levels is where
filesystem directory fanout stops helping.

**`collect_garbage` takes a set of wanted digests**, not a reference count. A
refcount that drifts out of step with reality is how backup tools lose data;
deriving reachability from the manifests themselves cannot drift.

**Known wart.** The module docstring mentions an `index.json` cache of
digest → size. Nothing writes or reads it; the store is self-describing from
its own layout, and a cache would only be a second source of truth to keep in
step. It is documented here rather than quietly left in the docstring, and
should be deleted.

## manifest.py — the durable record

```json
{
  "version": 1,
  "name": "docs/notes.txt",
  "size": 3000,
  "file_digest": "…",
  "created": "2026-09-26T00:00:00+00:00",
  "config": { "min_size": 2048, "bits": 14, "max_size": 65536, "normalised": false },
  "chunks": [ { "digest": "…", "length": 1000 } ]
}
```

Small, ordered, human-readable, and enough to rebuild the file. `file_digest`
covers the reassembled whole, so a round trip can be checked end to end rather
than only chunk by chunk.

Validation is strict on read, because a manifest that is silently misread is a
file that is silently rebuilt wrong:

- chunk lengths must sum to `size` — the single most important check;
- digests must be 64 lowercase hex characters;
- `length` must be a positive `int` and specifically not a `bool`
  (`isinstance(True, int)` is `True` in Python, so a JSON `true` would
  otherwise be read as a length of 1);
- a version newer than this library is **refused**, not interpreted.

Writes go through a `.tmp` file and `os.replace`, so a crash cannot leave a
truncated manifest that would be mistaken for corruption.

## forge.py — the high-level API

`ChunkForge` owns a `ChunkStore`, a manifests directory, and a journal
directory.

### Ingest

```
open source
  |
  find journal
  |  none      -> create one, record the config
  |  unreadable-> refuse
  |  complete  -> delete, start over
  |  wrong size-> refuse
  |  wrong cfg -> refuse
  |  otherwise -> verify the prefix
  |
  for each chunk: store (deduplicating), append to the journal, call on_chunk
  |
  seal: whole-file digest, write the manifest, delete the journal
```

The order matters. The journal is appended *after* the object is stored and
*before* the callback fires, so an interrupt at any point leaves a journal whose
last entry is a chunk that is actually in the store. A journal that claimed a
chunk that was never written would resume into a manifest with a hole in it.

`_open_source` is a context manager. Paths are opened there and closed there; a
stream the caller passed in is left open, because it is the caller's. The test
`test_add_does_not_leak_file_handles` ingests 300 files in a loop, which an
unclosed handle per call would not survive.

### The journal and the prefix check

The journal holds the name, declared size, chunker config, byte count consumed,
the ordered chunk refs, and a chained digest over the bytes consumed so far:

```
running_0 = SHA256("")
running_n = SHA256(running_{n-1} || chunk_n)
```

`_verify_prefix` re-derives the prefix and checks it:

1. Refuse if the journal recorded no config — a prefix cannot be re-derived
   without knowing how the boundaries were chosen.
2. Refuse if the source is not seekable — the prefix has to be re-read to be
   checked, and quietly skipping it would be a lie.
3. Rebuild the config; refuse if the recorded values are unusable.
4. Seek to 0, re-chunk, and for each recorded chunk compare length and digest,
   extending the chain as it goes.
5. Refuse if the byte count or the chain disagrees.
6. Seek to the end of the verified prefix so ingest continues from there.

Seeking back matters. The chunker reads ahead by up to `read_size`, so after
yielding the last verified chunk the stream is somewhere past it. Without the
seek, the remainder would be re-read and the file would gain duplicate bytes.

Stopping at `len(journal.chunks)` is also deliberate. The source normally has
*more* chunks than the journal recorded — that is the rest of the file. Only a
prefix that ends early, or a chunk that does not match, indicates a changed
source.

> This replaced a block-based scheme that chained 64 KiB blocks. That verified
> nothing at all when an interrupted ingest had progressed less than one block,
> which is every file under 64 KiB. The per-chunk chain means every journalled
> chunk is attestable, and the check now covers the entire prefix rather than a
> rounded-down sample of it.

### Accounting

`IngestResult` reports chunks and bytes both new and deduplicated, and
`dedupe_ratio`. On a resume, chunks carried over from the earlier attempt are
counted in `bytes_deduplicated`, since from this attempt's point of view they
cost nothing. `skipped_chunks` reports how many were carried over.

`stats()` reports the size of the *union* of referenced chunks, not the sum per
manifest. Two manifests that share every chunk reference 2N bytes between them
and store N; summing per manifest reports zero bytes saved for a perfect
deduplication. `dedupe_ratio` is `1 - referenced / logical` over the whole
archive, so a perfect duplicate reports 0.5 for two copies of one file.

## cli.py — argparse, and exit codes that mean something

Built on `argparse` from the standard library. A backup tool that needs a `pip
install` before it can tell you your archive is broken is not much use.

Two argparse pitfalls are handled explicitly, because both are silent failures
rather than errors:

- **Global flags are repeated on subparsers with
  `default=argparse.SUPPRESS`.** Without the suppression, a subparser's default
  overwrites a value the top-level flag already set, so `chunkforge --json
  stats` would quietly produce text output. `--json` and the config flags are
  accepted on either side of the subcommand.
- **The chunker config lives at the top level**, next to `--root`, because it is
  a property of the archive: manifests record it and a resume refuses to run
  under a different one. It is repeated on `chunks`, where overriding it for one
  command is genuinely useful.

Exit codes are distinct so a script can react rather than just fail:

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | error — missing file, no such manifest |
| 2 | usage error |
| 3 | corruption detected |
| 4 | resume refused — the source changed |

`restore` refuses to write to a destination derived from a name containing `..`
or an absolute path, because a logical name is caller-supplied data and should
not get to choose where a file lands.
