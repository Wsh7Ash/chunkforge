# chunkforge

Content-defined chunking with deduplication, integrity checking, and ingest you
can interrupt.

Store a file and chunkforge keeps only the parts it has not already stored.
Change one byte in the middle of a large file and one chunk is rewritten. Insert
a byte at the front and one chunk is rewritten. Interrupt an ingest — Ctrl-C, a
full disk, a closed laptop — and the next run picks up where it stopped, and
refuses to pick up if the file changed in the meantime.

```python
from chunkforge import ChunkForge

forge = ChunkForge("archive")

result = forge.add("notes.md")
print(f"{result.dedupe_ratio:.0%} of it was already there")

forge.add("notes-v2.md")        # one edited byte: one new chunk
forge.verify()                  # [] means intact
forge.restore("notes-v2.md", "rebuilt.md")
```

No dependencies, standard library only, Python 3.9+.

## Why content-defined chunking

Split a file into fixed-size pieces and you get deduplication only for files
that happen to start at the same offset as a file already stored. Insert a byte
at the front and *every* boundary moves, so every chunk is new.

Chunkforge picks boundaries from the content instead: it hashes the bytes as it
reads them and cuts where the hash's low bits come out zero. Boundaries land
where the data "says" they should, so an edit only disturbs its own
neighbourhood. On a 4 MiB file, a one-byte edit costs **1 new chunk out of 220**
(99.7% reused). A one-byte insertion costs **1 of 220** (99.9% reused), where a
fixed-size split needs all 2049.

## Install

```bash
pip install chunkforge
```

Or from a checkout, with no install step at all:

```bash
python run_tests.py
python demo.py
PYTHONPATH=src python -m chunkforge --help
```

## Command line

```bash
chunkforge --root archive add photo.jpg notes.md
chunkforge --root archive list
chunkforge --root archive stats
chunkforge --root archive verify
chunkforge --root archive restore notes.md -o notes-restored.md
chunkforge --root archive gc --dry-run
```

Add `--json` to `add`, `list`, `stats`, or `chunks` for machine-readable output.

Exit codes are distinct so a script can tell the failures apart:

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | error — missing file, no such manifest, bad arguments |
| 2 | usage error |
| 3 | corruption — a stored chunk failed its integrity check |
| 4 | resume refused — the source changed since the interrupted ingest |

## How it works

```
   source bytes
       |
   [ gear hash ]  rolling hash, one integer of state
       |
   [ chunker   ]  cut where the low `bits` bits are zero
       |          never shorter than min_size, never longer than max_size
   [ store     ]  SHA-256 per chunk, sharded two levels deep
       |
   [ manifest  ]  the file's digest and its ordered list of chunk digests
```

Naming every chunk by the hash of its own contents buys three things at once:
a chunk that no longer hashes to its name is corrupt, so integrity is free;
storing the same bytes twice is a no-op, so a retry cannot duplicate anything;
and two files that share a chunk share the object, so deduplication is the
default rather than a feature to enable.

## Configuration

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `min_size` | 2048 | no chunk smaller than this (except the last) |
| `max_size` | 65536 | hard cap; bounds the damage from pathological input |
| `bits` | 14 | cut when the low 14 bits of the hash are zero, so chunks average 16 KiB |
| `normalised` | `False` | FastCDC normalisation, for a tighter size distribution |

Larger chunks mean fewer objects and a smaller manifest, at the cost of more
bytes rewritten per edit. 16 KiB is a reasonable default for text and media;
64–256 KiB suits large files that change rarely.

The config belongs to the archive, not to a command, and every manifest records
the one that produced it. Resuming under a different config is refused, because
the result would be a file whose early chunks came from one configuration and
whose later ones came from another — a manifest that cannot be reproduced.

## Resumable ingest

Progress is journalled after every chunk, and the journal chains the chunks
together with a running digest. On resume, chunkforge re-chunks the recorded
prefix with the journalled config and checks every chunk digest *and* the chain
before writing anything.

A resume is refused, with a reason, when:

- the source's size changed;
- a chunk in the recorded prefix does not match — the file was edited;
- the source is shorter than the journal says;
- the source is not seekable, so the prefix cannot be re-read to check it;
- the chunker config differs from the one in the journal;
- the journal is unreadable or internally inconsistent.

In every one of those cases nothing is written and the journal is left in place
so you can inspect it. Start over deliberately with `add --no-resume`, or delete
the journal.

## Performance

Measured on one core of the development machine, pure Python:

| Operation | Rate |
| --------- | ---- |
| Chunking | ~0.4 MiB/s |

SHA-256 hashing, disk writes, and integrity verification are all faster than
chunking, so chunking is the bottleneck and the only part worth optimising if
this is too slow for your use.

This is an honest single-threaded pure-Python number, not a benchmark of the
algorithm. The same configuration in a compiled implementation runs two to three
orders of magnitude faster. If you need throughput rather than a dependency-free
library, this is the wrong tool.

## Library API

| Object | Purpose |
| ------ | ------- |
| `ChunkForge(root, config)` | the high-level API: add, restore, verify, gc, stats |
| `ChunkStore(root)` | content-addressed objects: put, get, read_into, gc |
| `chunk_stream(stream, config)` | yields chunks from any binary file object |
| `chunk_data(bytes, config)` | the same for in-memory data |
| `Manifest` | one file's digest and chunk list, versioned and validated |

`ChunkForge.add` accepts a path, `bytes`, or any object with a `read` method. A
stream you pass in stays open and is yours; a path you pass in is opened and
closed for you.

```python
result = forge.add(source, name="logical/name", on_chunk=lambda i, total: print(i, total))
result.chunks_total, result.chunks_new, result.bytes_new, result.dedupe_ratio
```

Raise `Interrupted` from the `on_chunk` callback to stop and leave a resumable
journal behind.

## Limitations

Worth knowing before you rely on it:

- **Not encrypted.** Chunks are plain files. Put the archive on an encrypted
  volume if it needs to be confidential.
- **Single writer.** Concurrent processes converge on the same object (writes
  are atomic), but there is no locking, and no filesystem-like interface.
- **No deletion tracking.** A file is stored whole; there is no incremental
  update when a file shrinks.
- **Not a backup scheduler.** chunkforge stores and rebuilds. Deciding *what* to
  store, and *when*, is yours.
- **Pure Python.** See Performance above.

## Documentation

- [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) — the problem, the constraints,
  the acceptance criteria
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces work and why
- [docs/USAGE.md](docs/USAGE.md) — every command and API, with recipes
- [RESULTS.md](RESULTS.md) — measured results from `demo.py`

## Development

```bash
python run_tests.py          # 218 tests
python run_tests.py -v
python run_tests.py forge    # one module
python demo.py               # regenerate results/
```

Standard library only, including the tests. CI runs the suite and the demo on
Python 3.9 through 3.13.

## License

MIT. See [LICENSE](LICENSE).
