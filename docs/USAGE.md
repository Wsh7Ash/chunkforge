# Usage

## Contents

- [Install](#install)
- [Command line](#command-line)
- [Library](#library)
- [Configuration](#configuration)
- [Recipes](#recipes)
- [Recovering from interruption](#recovering-from-interruption)
- [Inspecting an archive](#inspecting-an-archive)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)

## Install

```bash
pip install chunkforge
```

Python 3.9 or later. No third-party dependencies.

From a checkout, with nothing installed:

```bash
export PYTHONPATH=src
python -m chunkforge --help
```

## Command line

Every command takes `--root` to choose the archive (default
`chunkforge-archive`) and the chunker config flags. `add`, `list`, `stats`, and
`chunks` also take `--json`.

### add

```bash
chunkforge --root archive add notes.md
chunkforge --root archive add photo.jpg report.pdf      # several at once
chunkforge --root archive add notes.md --name docs/2026/notes
chunkforge --root archive add notes.md --no-resume       # ignore any journal
```

By default the logical name is the file's basename; `--name` overrides it for a
single input. The name is metadata — it is never used to open anything, and
names containing slashes are stored safely (the manifest key is the hash of the
name, not the name).

One bad file in a list does not stop the others: each is reported, the rest are
stored, and the exit code reflects that something failed.

### restore

```bash
chunkforge --root archive restore notes.md -o rebuilt.md
chunkforge --root archive restore notes.md               # writes ./notes.md
chunkforge --root archive restore notes.md --stdout | sha256sum
chunkforge --root archive restore notes.md --no-verify
```

Restoring is verified by default: every chunk is hashed as it is read and the
reassembled whole is compared to the recorded `file_digest`. A mismatch raises
rather than writing a file that looks fine.

With no `-o`, the name is used as a relative path. If the name is absolute or
contains `..`, chunkforge refuses and asks for `-o` explicitly — a logical name
is caller-supplied data and should not choose where a file lands.

`--stdout` writes to standard output and removes the temporary file, so
`chunkforge restore notes.md --stdout > copy.md` never leaves a stray file behind.

### list

```bash
chunkforge --root archive list
chunkforge --root archive list --json
```

### verify

```bash
chunkforge --root archive verify          # everything
chunkforge --root archive verify notes.md # one file
```

Hashes every chunk a manifest references and compares against its name, then
checks that the lengths add up. Exits 3 if anything is wrong. A chunk shared by
several manifests is reported once per manifest, so one corrupt object can
produce several lines — that is expected, not a bug.

### stats

```bash
chunkforge --root archive stats
chunkforge --root archive stats --json
```

```
archive:  archive
config:   min=2048 avg~16384 max=65536 bits=14 normalised=False
files:    4
logical:  16,777,217 bytes
stored:   4,209,802 bytes
shared:   217 chunks referenced more than once
saved:    12,567,415 bytes (74.9%)
```

`logical` is the sum of file sizes. `stored` is the size of the union of
referenced chunks. `saved` is the difference — what deduplication is actually
buying. A `dedupe_ratio` of 0.5 for two identical copies is the honest answer,
not a failure.

### gc

```bash
chunkforge --root archive gc --dry-run
chunkforge --root archive gc
```

Deletes stored chunks that no manifest references. Reachability is derived from
the manifests, not from a reference count, so a count that drifted cannot cause
data loss. Chunks shared by a surviving manifest are always kept.

Run `gc` after `forget`, not before.

### forget

```bash
chunkforge --root archive forget notes.md
chunkforge --root archive gc
```

Removes the manifest. The chunks stay until `gc`.

### chunks

```bash
chunkforge --root archive chunks notes.md
chunkforge --root archive chunks notes.md --json
chunkforge --root archive chunks notes.md --bits 12   # override the config
```

Shows where the boundaries would fall. A diagnostic, not part of the format.

## Library

```python
from chunkforge import ChunkForge, ChunkerConfig

forge = ChunkForge("archive", ChunkerConfig(min_size=4096, bits=12, max_size=16384))
```

### add

```python
result = forge.add(source, name=None, on_chunk=None, resume=True)
```

`source` may be a path, `bytes`, or any object with a `read` method. A stream you
pass stays open and is yours; a path is opened and closed for you.

`on_chunk(index, total_bytes)` is called after each chunk. Raise `Interrupted`
from it to stop and leave a resumable journal:

```python
from chunkforge import Interrupted

def stop_when_big(index, total):
    if total > 100 * 1024 * 1024:
        raise Interrupted("enough for now")

result = forge.add("huge.iso", on_chunk=stop_when_big)
```

`IngestResult`:

| Field | Meaning |
| ----- | ------- |
| `manifest` | the `Manifest` written |
| `chunks_total`, `chunks_new`, `chunks_deduplicated` | chunk counts |
| `bytes_total`, `bytes_new`, `bytes_deduplicated` | byte counts |
| `dedupe_ratio` | 0.0 to 1.0, the fraction already in the store |
| `resumed`, `skipped_chunks` | whether this continued an earlier attempt |
| `describe()` | a one-line human summary |

### restore and verify

```python
written = forge.restore("notes.md", "rebuilt.md", verify=True)
problems = forge.verify()             # [] means intact
problems = forge.verify("notes.md")   # one file
```

### manifests and garbage collection

```python
forge.has_manifest("notes.md")
forge.get_manifest("notes.md")
[m.name for m in forge.list_manifests()]
forge.forget("notes.md")
deleted, reclaimed = forge.collect_garbage()
stats = forge.stats()                 # dict
```

### the store on its own

```python
from chunkforge import ChunkStore, digest_of

store = ChunkStore("objects")
stored = store.put(b"payload")         # stored.digest, .length, .created
store.get(stored.digest)              # verified; raises CorruptChunk if not
store.read_into([a, b, a], open("out.bin", "wb"))
len(store), store.has(stored.digest)
```

### chunking without storing

```python
from chunkforge import chunk_data, chunk_stream, ChunkerConfig

chunks = chunk_data(data, ChunkerConfig(min_size=2048, bits=14))
for chunk in chunk_stream(open("big.bin", "rb"), ChunkerConfig()):
    ...
```

## Configuration

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `min_size` | 2048 | no chunk smaller than this, except the last |
| `max_size` | 65536 | hard cap on any chunk |
| `bits` | 14 | cut when the low `bits` bits of the hash are zero; mean size `2**bits` |
| `normalised` | `False` | FastCDC normalisation, tighter size distribution |

Rough guide:

| Data | Suggested |
| ---- | --------- |
| Text, code, config | defaults (16 KiB mean) |
| Many small files, version history | `bits=10` (1 KiB) — more objects, finer sharing |
| Large media, changing rarely | `bits=16`–`18` (64–256 KiB) — fewer objects |
| Already-compressed media | `bits=16` or higher; there is little structure to exploit |

The config is a property of the archive. Every manifest records it, and a resume
under a different config is refused. To use a different config, use a different
archive, or pass `resume=False` and accept that existing manifests are rewritten
with the new boundaries.

## Recipes

### Snapshot a directory

```bash
#!/bin/sh
set -eu
root=/var/backups
dest=$root/$(date +%Y-%m-%d)
mkdir -p "$dest"
for f in /home/*/Documents; do
    [ -d "$f" ] || continue
    find "$f" -type f -print0 | while IFS= read -r -d '' file; do
        chunkforge --root "$dest" add "$file" --name "${file#/}" || exit 1
    done
done
chunkforge --root "$dest" verify
```

`verify` at the end is the point of the whole exercise.

### Keep daily snapshots and prune

```bash
for d in /var/backups/2026-09-*; do
    [ "$d" = /var/backups/$(date +%Y-%m-%d) ] && continue
    for name in $(chunkforge --root "$d" list --json | jq -r '.[].name'); do
        chunkforge --root "$d" forget "$name"
    done
    chunkforge --root "$d" gc
done
```

### Watch what deduplication is worth

```bash
chunkforge --root archive add big.iso
cp big.iso big-copy.iso
chunkforge --root archive add big-copy.iso
chunkforge --root archive stats
```

`stored` barely moves the second time. That is the whole idea.

### Verify on a schedule

```bash
#!/bin/sh
chunkforge --root /var/backups/daily verify || \
    logger -t backup "archive verification FAILED"
```

Exit 3 means corruption. Non-zero means something is wrong, either way.

### Pipe a file out without touching the disk twice

```bash
chunkforge --root archive restore report.pdf --stdout > /tmp/report.pdf
```

## Recovering from interruption

If an ingest was interrupted, the journal is still there. Re-run the same
command:

```bash
chunkforge --root archive add big.iso
stored big.iso: 1.9 GiB in 48210 chunks
  new: 1.4 GiB (33920 chunks)   reused: 512.0 MiB (20.4%)
  resumed after 14290 chunks from an earlier run
```

The already-stored chunks are counted as reused, because this attempt paid
nothing for them.

### When it refuses

A resume is refused when it cannot be trusted. The message says why:

```
big.iso: refusing to resume: 'big.iso' is 1048576000 bytes but the journal was
written for 1048576001; the file changed, so resuming would mix two versions.
Delete the journal to start over.
```

The refusals, and what to do:

| Message mentions | Cause | Fix |
| ---------------- | ----- | --- |
| a byte count | the file changed size | restore the original file, or re-add with `--no-resume` |
| a chunk digest | the file was edited | as above |
| `non-seekable` | piping or a socket | ingest from a real file, which is seekable |
| `configurations` | different chunker settings | reopen with the original config, or `--no-resume` |
| `unreadable` or `inconsistent` | damaged journal | delete the journal to start over |

Nothing is written and the journal is left in place, so you can look at it or
retry against the original file. Chunks already stored are not deleted — they
are still valid data, and `gc` will reclaim any that end up unreferenced.

## Inspecting an archive

```
archive/
  objects/ab/cdef0123…    chunk contents, named by SHA-256
  tmp/                    staging; empty when idle
  manifests/<hash>.json   one per file
  journal/<hash>.jsonl    only while an ingest is in progress
```

Manifest keys and journal keys are the SHA-256 of the logical name, so a name
with slashes, dots, or a colon cannot escape the directory:

```bash
chunkforge --root archive list --json | jq -r '.[] | "\(.name) \(.digest)"'
```

A healthy archive has nothing in `tmp/`. Objects that no manifest references are
garbage:

```bash
chunkforge --root archive gc --dry-run
```

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | error — missing file, no such manifest, unreadable archive |
| 2 | usage error — bad or missing arguments |
| 3 | corruption — a stored chunk failed its integrity check |
| 4 | resume refused — the source changed since the interrupted ingest |

```bash
chunkforge --root archive verify
case $? in
  0) echo "archive intact" ;;
  3) echo "CORRUPT: restore from another copy" >&2 ;;
  *) echo "could not check the archive" >&2 ;;
esac
```

## Troubleshooting

**`resume refused` immediately, on a file I have not touched.** The journal was
written under a different chunker config. Pass the same `--bits` and friends
that were used originally, or `--no-resume`.

**`journal ... is unreadable or inconsistent`.** The journal was damaged, most
likely by a hard kill during its atomic rewrite. Delete it; the chunks already
stored are unaffected and deduplication still applies.

**`no manifest for 'x'`.** The logical name does not match. `list` shows the
names as stored. Names are the basename unless `--name` was given.

**Deduplication is lower than expected.** Check that files were added to the
same `--root`. Two archives do not share chunks. Also check `bits`: at 18 the
chunks are large, so an edit disturbs more of the file.

**`restore` fails with a digest mismatch but `verify` passes.** Something wrote
to the destination between the chunks being read and the file being assembled —
most often two restores racing to the same `-o`. Restore to a fresh path.

**Very slow.** ~0.4 MiB/s is expected for pure Python; see the README's
Performance section. Reducing `bits` will not help much, since the gear loop
runs per byte regardless of where the boundaries fall. What helps is storing
less.
