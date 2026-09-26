# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-26

First release.

### Added

- **Content-defined chunking** (`chunker.py`). Gear-hash boundaries chosen from
  the data, with `min_size`, `max_size`, `bits`, and optional FastCDC
  normalisation. Streaming; output is independent of read size.
- **Rolling hash** (`gear.py`). Deterministic 64-bit Gear table generated from a
  fixed seed by SplitMix64, with the table digest pinned in the test suite.
- **Content-addressed store** (`store.py`). SHA-256 named objects, two-level
  fanout, atomic staged writes, verification on read, and garbage collection
  driven by an explicit reachable set.
- **Manifests** (`manifest.py`). Versioned, validated, atomically written JSON
  recording a file's digest, size, ordered chunk references, and the chunker
  configuration that produced it.
- **High-level API** (`forge.py`). `add`, `restore`, `verify`, `stats`,
  `forget`, `collect_garbage`, and manifest listing.
- **Resumable ingest.** Progress journalled after every chunk, with a chained
  digest over the consumed bytes. A resume re-chunks and verifies the whole
  recorded prefix before storing anything.
- **Command-line interface** (`cli.py`), with `add`, `restore`, `list`,
  `verify`, `stats`, `gc`, `forget`, and `chunks`; `--json` output where it
  makes sense; and distinct exit codes for usage errors, corruption, and
  refused resumes.
- **218 tests** across six modules, standard library only.
- **`demo.py`**, producing the reproducible results in `results/`.
- Documentation: README, `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`,
  `docs/USAGE.md`, `RESULTS.md`, and `CONTRIBUTING.md`.

### Notes

- Manifest format version 1, journal format version 1.
- The journal is a single JSON document rewritten atomically, but keeps a
  `.jsonl` extension for compatibility with archives written during
  development. The extension is misleading and should become `.json` in the next
  format bump.
- `ChunkStore`'s module docstring still describes an `index.json` cache that
  nothing reads or writes. It is dead documentation and is scheduled for
  removal.

[1.0.0]: https://github.com/Wsh7Ash/chunkforge/releases/tag/v1.0.0
