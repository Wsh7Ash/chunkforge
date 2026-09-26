# Results

Measured by `python demo.py` on a 4 MiB seeded pseudo-random payload at the
default configuration (`min_size=2048, bits=14, max_size=65536`, mean chunk
16 KiB). Python 3.14.3, single core, pure Python.

The payload is seeded, so the chunk counts and ratios below reproduce exactly.
Only the throughput section varies between runs. Raw output is in
`results/verification.txt` and `results/verification.json`.

## Headline

| Scenario | New chunks | Chunks total | Reused |
| -------- | ---------- | ------------ | ------ |
| Identical file | 0 | 220 | 100.00% |
| One byte edited at the midpoint | 1 | 220 | 99.72% |
| One byte inserted at the front | 1 | 220 | 99.91% |

The third row is the one that matters. A fixed-size split of the same data needs
**2049 of 2049 chunks new** — inserting one byte moves every boundary, so
nothing at all is reusable. Content-defined chunking reuses all but one chunk.

## Deduplication

| Scenario | New bytes | Reused | Dedupe ratio |
| -------- | --------- | ------ | ------------ |
| Identical file | 0 | 4,194,304 | 100.00% |
| One byte edited at the midpoint | 19,418 | 4,174,886 | 99.54% |
| One byte inserted at the front | 8,192 | 4,186,112 | 99.80% |
| Fixed-size split, same insertion | 4,196,353 | 0 | 0.00% |

## Archive totals

Four files, 16,777,217 logical bytes, after the scenarios above:

```
files:   4
logical: 16,777,217 bytes
stored:  4,209,802 bytes
saved:   12,567,415 bytes (74.9%)
```

Three of the four files are the same 4 MiB payload and two variants of it. The
union of referenced chunks is 4,209,802 bytes for 16,777,217 bytes of files.

## Round trip

| Check | Result |
| ----- | ------ |
| 4,194,304 bytes chunked into | 220 chunks (mean 19,065 bytes) |
| Rebuilt from the archive | byte-identical |
| `verify` on a healthy archive | no problems |
| Resumed file rebuilt | byte-identical |

## Resume and refusal

Ingest was interrupted after 10 chunks by a callback raising `Interrupted`:

| Check | Result |
| ----- | ------ |
| Journal left behind | yes |
| Journal file on disk | one, in `journal/` |
| Resume skipped chunks | 10 |
| Manifest written by the resume | yes |
| Rebuilt file | byte-identical |
| Journal after success | deleted |

The same interrupted ingest, with the source's first byte changed:

| Check | Result |
| ----- | ------ |
| Resume | refused |
| Reason reported | `chunk 0 is 3724 bytes / 82fa208da244 but the journal recorded 3724 / 7fc4f04b` |
| Manifest written | no |
| Journal left in place | yes |

A refused resume is the point of the feature. Completing it would have produced
a file that was mostly one version and locally another, with a manifest that
claimed to be a single file.

## Integrity

One stored object was overwritten with different bytes, then repaired:

| Check | Result |
| ----- | ------ |
| Manifests reporting the problem | 3 |
| Manifests sharing that chunk | 3 |
| `restore` | refused, `CorruptChunk` |
| After restoring the correct bytes | `verify` clean |

Three manifests, one corrupted object. That is deduplication working and
integrity checking being stricter than a per-file scheme would be: every file
that shares the object notices.

## Garbage collection

```
objects: 2 -> 1
deleted 1 chunk(s), reclaimed 34 bytes
kept file still restores: True
```

The referenced chunk survived; only the orphan was removed.

## Throughput

| Operation | Rate |
| --------- | ---- |
| Chunking, 2 MiB | 0.42 MiB/s |

Observed range across runs: **0.34–0.50 MiB/s**.

This is the honest cost of a gear hash computed one Python-level operation per
byte. It is a single-threaded pure-Python figure and is not a statement about
the algorithm; the same configuration in a compiled implementation is two to
three orders of magnitude faster.

Hashing, disk writes, and integrity verification are all faster than chunking,
so chunking is the bottleneck and the only part worth optimising. The library
is aimed at correctness and readability; if throughput is the requirement,
borg or restic already do this and should be used instead.

## Test suite

```
Ran 218 tests in 145.544s
OK
```

| Module | Tests | Covers |
| ------ | ----- | ------ |
| `test_gear.py` | 13 | table reproducibility, pinned digest, rolling-hash equivalence |
| `test_chunker.py` | 30 | reference agreement, bounds, round trip, stream equivalence, resynchronisation |
| `test_store.py` | 29 | atomicity, idempotence, corruption detection, GC, stats |
| `test_manifest.py` | 31 | round trip, validation, version refusal, atomic writes |
| `test_forge.py` | 74 | ingest, dedupe, resume, every refusal path, GC, stats |
| `test_cli.py` | 41 | every command, exit codes, JSON output, a real process kill |

## Reproducing

```bash
python run_tests.py        # 218 tests
python demo.py             # regenerates results/
python demo.py --quick     # smaller inputs, ~30 s
```

`demo.py` uses a fixed seed, so the figures above reproduce on any machine with
the same Python version. Chunk counts depend on the version only through the
Gear table, which is generated deterministically from a fixed seed and pinned by
a test, so they are stable across versions and platforms.
