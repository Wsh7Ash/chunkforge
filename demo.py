#!/usr/bin/env python3
"""Demonstrate chunkforge and record the results.

    python demo.py              # print the report and write results/
    python demo.py --quick      # smaller inputs, for a fast check

Everything here is seeded, so two runs produce the same numbers. The report is
written to ``results/verification.json`` and ``results/verification.txt``.

What it demonstrates, in order:

1. Round trip -- chunk, store, rebuild, compare byte for byte.
2. Dedupe -- an identical file costs nothing the second time.
3. Local edit -- one byte changed in the middle of a file.
4. Insertion -- a byte added at the front, the case fixed-size chunking cannot
   handle at all.
5. In-file repetition -- periodic content deduplicating against itself.
6. Resume -- an interrupted ingest picking up where it stopped.
7. Refusal -- a resume against a source that changed, which must be refused
   rather than quietly producing a mixture of two files.
8. Integrity -- a deliberately corrupted chunk must be detected.
9. Garbage collection -- chunks no manifest references.
10. Throughput, honestly measured.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from chunkforge import (  # noqa: E402
    ChunkerConfig,
    ChunkForge,
    CorruptChunk,
    Interrupted,
    ResumeRefused,
    chunk_data,
)

CONFIG = ChunkerConfig(min_size=2048, bits=14, max_size=65536)

# Enough to produce a few hundred chunks at the default configuration.
PAYLOAD = 4 * 1024 * 1024


def payload(n: int) -> bytes:
    """Seeded pseudo-random bytes. Deterministic, so the report is too."""
    return random.Random(20260926).randbytes(n)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def build(work: Path, size: int) -> dict:
    """Run every demonstration, returning a JSON-serialisable report."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    forge = ChunkForge(work / "archive", CONFIG)
    data = payload(size)
    report: dict = {
        "config": CONFIG.describe(),
        "payload_bytes": size,
        "sections": {},
    }

    # 1. Round trip
    section("1. round trip")
    source = work / "original.bin"
    source.write_bytes(data)
    result = forge.add(source)
    rebuilt = work / "rebuilt.bin"
    forge.restore("original.bin", rebuilt)
    identical = rebuilt.read_bytes() == data
    print(f"  {size} bytes -> {result.chunks_total} chunks")
    print(f"  rebuilt identically: {identical}")
    report["sections"]["round_trip"] = {
        "chunks": result.chunks_total,
        "identical": identical,
        "mean_chunk_bytes": size // result.chunks_total,
    }
    if not identical:
        raise SystemExit("demo aborted: round trip did not reproduce the file")

    # 2. Identical file
    section("2. identical file")
    copy = work / "copy.bin"
    copy.write_bytes(data)
    dup = forge.add(copy)
    print(f"  new chunks: {dup.chunks_new} of {dup.chunks_total}")
    print(f"  reused:    {dup.dedupe_ratio * 100:.2f}%")
    report["sections"]["identical_file"] = {
        "new_chunks": dup.chunks_new,
        "dedupe_ratio": round(dup.dedupe_ratio, 6),
    }

    # 3. One byte edited in the middle
    section("3. one byte edited at the midpoint")
    edited = bytearray(data)
    edited[size // 2] ^= 0xFF
    edited_path = work / "edited.bin"
    edited_path.write_bytes(bytes(edited))
    res = forge.add(edited_path)
    print(f"  new chunks: {res.chunks_new} of {res.chunks_total}")
    print(f"  reused:     {res.dedupe_ratio * 100:.2f}%")
    report["sections"]["single_byte_edit"] = {
        "new_chunks": res.chunks_new,
        "total_chunks": res.chunks_total,
        "dedupe_ratio": round(res.dedupe_ratio, 6),
    }

    # 4. One byte inserted at the front
    section("4. one byte inserted at the front")
    prepended = work / "prepended.bin"
    prepended.write_bytes(b"\x00" + data)
    res = forge.add(prepended)
    print(f"  new chunks: {res.chunks_new} of {res.chunks_total}")
    print(f"  reused:     {res.dedupe_ratio * 100:.2f}%")
    # The contrast: a fixed-size split keeps nothing at all here.
    fixed_config = ChunkerConfig(min_size=2048, bits=0, max_size=2048)
    fixed = len(chunk_data(b"\x00" + data, fixed_config))
    print(f"  a fixed-size split would need {fixed} of {fixed} chunks new")
    report["sections"]["prepend_one_byte"] = {
        "new_chunks": res.chunks_new,
        "total_chunks": res.chunks_total,
        "dedupe_ratio": round(res.dedupe_ratio, 6),
        "fixed_size_chunks_all_new": fixed,
    }

    # 5. Repetition within a single file
    # A period-2 payload with a fixed-size config: every chunk comes out
    # identical, so a highly repetitive file costs one object, not many. The
    # content-defined default would not show this, because chunk boundaries would
    # not line up with the period.
    section("5. repetition inside one file")
    rep_config = ChunkerConfig(min_size=4096, bits=0, max_size=4096)
    rep_forge = ChunkForge(work / "rep-archive", rep_config)
    repeated = b"ab" * 20000
    res = rep_forge.add(repeated, name="repeated.bin")
    rep_out = work / "repeated.out"
    rep_forge.restore("repeated.bin", rep_out)
    rep_ok = rep_out.read_bytes() == repeated
    print(f"  {res.chunks_total} chunks, {res.chunks_new} of them new")
    print(f"  reused:     {res.dedupe_ratio * 100:.2f}%")
    print(f"  rebuilt identically: {rep_ok}")
    report["sections"]["self_dedupe"] = {
        "bytes": len(repeated),
        "total_chunks": res.chunks_total,
        "new_chunks": res.chunks_new,
        "dedupe_ratio": round(res.dedupe_ratio, 6),
        "identical": rep_ok,
        "config": rep_config.describe(),
    }

    # 6. Resume
    section("6. interrupted ingest, then resumed")
    resume_data = payload(size // 2)
    resume_path = work / "resume.bin"
    resume_path.write_bytes(resume_data)
    partial_forge = ChunkForge(work / "resume-archive", CONFIG)

    def stop(index: int, total: int) -> None:
        if index >= 10:
            raise Interrupted("demo: stopping after 10 chunks")

    try:
        partial_forge.add(resume_path, on_chunk=stop)
        print("  error: the callback never fired")
    except Interrupted:
        print("  interrupted after 10 chunks")
    journals = list((work / "resume-archive" / "journal").glob("*.jsonl"))
    print(f"  journal left behind: {bool(journals)}")

    resumed = partial_forge.add(resume_path)
    print(f"  resumed: {resumed.resumed}, skipped {resumed.skipped_chunks} chunks")
    out = work / "resume-rebuilt.bin"
    partial_forge.restore("resume.bin", out)
    resumed_ok = out.read_bytes() == resume_data
    print(f"  rebuilt identically: {resumed_ok}")
    report["sections"]["resume"] = {
        "interrupted_after_chunks": 10,
        "journal_left_behind": bool(journals),
        "resumed": resumed.resumed,
        "skipped_chunks": resumed.skipped_chunks,
        "identical": resumed_ok,
    }
    if not resumed_ok:
        raise SystemExit("demo aborted: resumed file did not match")

    # 7. Refused resume
    section("7. resume against a changed source is refused")
    refuse_forge = ChunkForge(work / "refuse-archive", CONFIG)
    refuse_data = payload(size // 2)
    try:
        refuse_forge.add(refuse_data, name="target.bin", on_chunk=stop)
    except Interrupted:
        pass
    changed = bytearray(refuse_data)
    changed[0] ^= 0xFF
    refusal = None
    try:
        refuse_forge.add(bytes(changed), name="target.bin")
    except ResumeRefused as exc:
        refusal = str(exc)
    print(f"  refused: {refusal is not None}")
    if refusal:
        print(f"  reason:  {refusal[:90]}")
    print(f"  manifest written anyway: {refuse_forge.has_manifest('target.bin')}")
    report["sections"]["refused_resume"] = {
        "refused": refusal is not None,
        "reason": refusal,
        "manifest_written": refuse_forge.has_manifest("target.bin"),
    }

    # 8. Corruption detection
    section("8. corrupted chunk is detected")
    victim = forge.get_manifest("original.bin").chunks[0].digest
    victim_path = forge.store.path_for(victim)
    original_bytes = victim_path.read_bytes()
    victim_path.write_bytes(b"corrupted" + original_bytes[9:])
    problems = forge.verify()
    sharers = sum(1 for m in forge.list_manifests() if victim in m.reachable())
    try:
        forge.restore("original.bin", work / "should-fail.bin")
        restored_badly = True
    except CorruptChunk:
        restored_badly = False
    finally:
        victim_path.write_bytes(original_bytes)
    print(f"  one object corrupted; {len(problems)} manifest(s) report it")
    print(f"  ({sharers} manifests share that chunk, and each one notices)")
    print(f"  restore() refused: {not restored_badly}")
    print(f"  archive healthy after repair: {forge.verify() == []}")
    report["sections"]["corruption"] = {
        "manifests_reporting_problem": len(problems),
        "manifests_sharing_the_chunk": sharers,
        "restore_refused": not restored_badly,
        "healthy_after_repair": forge.verify() == [],
    }

    # 9. Garbage collection
    section("9. garbage collection")
    gc_forge = ChunkForge(work / "gc-archive", CONFIG)
    kept = b"kept content"
    gc_forge.add(kept, name="kept.bin")
    gc_forge.store.put(b"orphaned content nobody references")
    before = len(list(gc_forge.store.iter_objects()))
    deleted, reclaimed = gc_forge.collect_garbage()
    after = len(list(gc_forge.store.iter_objects()))
    kept_ok = gc_forge.restore("kept.bin", work / "kept.out") == len(kept)
    print(f"  objects: {before} -> {after}")
    print(f"  deleted {deleted} chunk(s), reclaimed {reclaimed} bytes")
    print(f"  kept file still restores: {kept_ok}")
    report["sections"]["garbage_collection"] = {
        "objects_before": before,
        "objects_after": after,
        "deleted": deleted,
        "reclaimed_bytes": reclaimed,
        "referenced_file_survives": kept_ok,
    }

    # 10. Throughput
    section("10. throughput")
    bench = payload(2 * 1024 * 1024)
    started = time.perf_counter()
    chunks = chunk_data(bench, CONFIG)
    elapsed = time.perf_counter() - started
    mib = len(bench) / (1024 * 1024)
    rate = mib / elapsed if elapsed else 0.0
    print(f"  {mib:.1f} MiB in {elapsed:.2f} s = {rate:.2f} MiB/s")
    print(f"  {len(chunks)} chunks, mean {len(bench) // max(1, len(chunks))} bytes")
    print("  pure Python, single-threaded: expected to be modest")
    report["sections"]["throughput"] = {
        "bytes": len(bench),
        "seconds": round(elapsed, 3),
        "mib_per_second": round(rate, 2),
        "chunks": len(chunks),
    }

    stats = forge.stats()
    section("archive totals")
    print(f"  files:   {stats['manifests']}")
    print(f"  logical: {stats['logical_bytes']:,} bytes")
    print(f"  stored:  {stats['unique_bytes']:,} bytes")
    print(
        f"  saved:   {stats['saved_bytes']:,} bytes "
        f"({stats['dedupe_ratio'] * 100:.1f}%)"
    )
    report["totals"] = stats
    return report


def render(report: dict) -> str:
    lines = [
        "chunkforge verification report",
        "=" * 60,
        f"chunker      {report['config']}",
        f"payload      {report['payload_bytes']:,} bytes",
        "",
    ]
    for name, body in report["sections"].items():
        lines.append(name)
        for key, value in body.items():
            lines.append(f"  {key:<32} {value}")
        lines.append("")
    lines.append("totals")
    for key, value in report["totals"].items():
        lines.append(f"  {key:<32} {value}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="smaller inputs")
    parser.add_argument(
        "--results", default=str(ROOT / "results"), help="where to write the report"
    )
    args = parser.parse_args(argv)

    size = 512 * 1024 if args.quick else PAYLOAD
    print(f"chunkforge demo ({'quick' if args.quick else 'full'}), payload {size:,} bytes")

    report = build(ROOT / ".demo-work", size)
    out = Path(args.results)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "verification.txt").write_text(render(report), encoding="utf-8")
    shutil.rmtree(ROOT / ".demo-work", ignore_errors=True)

    print(f"\nwrote {out / 'verification.json'}")
    print(f"wrote {out / 'verification.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
