"""Command-line interface.

    python -m chunkforge --help

Deliberately built on ``argparse`` from the standard library: a backup tool that
needs a pip install before it can tell you your archive is corrupt is not much
use in the situation where you find out your archive is corrupt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .chunker import ChunkerConfig
from .forge import ChunkForge, Interrupted, ResumeRefused
from .manifest import ManifestError
from .store import CorruptChunk, MissingChunk

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_REFUSED = 4

DEFAULT_ROOT = "chunkforge-archive"


def _human(n: int) -> str:
    """Byte count with a binary suffix."""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < step or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TiB"


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _forge(args: argparse.Namespace) -> ChunkForge:
    return ChunkForge(args.root, config_from_args(args))


def config_from_args(args: argparse.Namespace) -> Optional[ChunkerConfig]:
    """Build a config from flags, or ``None`` to accept the defaults."""
    fields = ("min_size", "bits", "max_size", "normalised")
    if all(getattr(args, f, None) is None for f in fields):
        return None
    defaults = ChunkerConfig()
    return ChunkerConfig(
        min_size=getattr(args, "min_size", None) or defaults.min_size,
        bits=getattr(args, "bits", None) if getattr(args, "bits", None) is not None else defaults.bits,
        max_size=getattr(args, "max_size", None) or defaults.max_size,
        normalised=bool(getattr(args, "normalised", False)),
    )


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Accept ``--json`` after the subcommand as well as before it.

    The default is SUPPRESS so that, when the flag is absent here, it does not
    overwrite a value already set by the top-level flag. Without that, a global
    ``--json`` would be silently reset by every subcommand.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )


def _add_config_flags(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Chunker configuration flags.

    The configuration belongs to the archive, not to a single command -- an
    archive's manifests record the config that produced them, and a resume
    refuses to run under a different one. So these are accepted at the top level
    alongside ``--root``, and repeated on ``chunks`` for the one case where
    overriding it for a single command is genuinely useful.
    """
    default = argparse.SUPPRESS if suppress else None
    group = parser.add_argument_group("chunker configuration")
    group.add_argument("--min-size", type=int, default=default, help="smallest a chunk may be")
    group.add_argument(
        "--bits",
        type=int,
        default=default,
        help="mask width; larger means larger chunks",
    )
    group.add_argument("--max-size", type=int, default=default, help="hard cap on a chunk")
    group.add_argument(
        "--normalised",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="use FastCDC normalisation",
    )


# ----------------------------------------------------------------- commands


def cmd_add(args: argparse.Namespace) -> int:
    forge = _forge(args)
    paths: list[str] = args.paths
    if not paths:
        print("error: no input files given", file=sys.stderr)
        return EXIT_USAGE

    failures = 0
    refusals = 0
    for path in paths:
        name = args.name if len(paths) == 1 and args.name else os.path.basename(path)
        try:
            result = forge.add(path, name=name, resume=not args.no_resume)
        except ResumeRefused as exc:
            print(f"{path}: refusing to resume: {exc}", file=sys.stderr)
            refusals += 1
            continue
        except FileNotFoundError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        except KeyboardInterrupt:
            print(f"\n{path}: interrupted; rerun the same command to resume", file=sys.stderr)
            failures += 1
            continue

        if args.json:
            print(
                json.dumps(
                    {
                        "name": result.manifest.name,
                        "bytes": result.bytes_total,
                        "chunks": result.chunks_total,
                        "new_chunks": result.chunks_new,
                        "new_bytes": result.bytes_new,
                        "deduplicated_bytes": result.bytes_deduplicated,
                        "dedupe_ratio": result.dedupe_ratio,
                        "resumed": result.resumed,
                    },
                    indent=2,
                )
            )
        else:
            print(f"stored {name}: {_human(result.bytes_total)} in {result.chunks_total} chunks")
            print(
                f"  new: {_human(result.bytes_new)} ({result.chunks_new} chunks)   "
                f"reused: {_human(result.bytes_deduplicated)} "
                f"({_pct(result.dedupe_ratio)})"
            )
            if result.resumed:
                print(f"  resumed after {result.skipped_chunks} chunks from an earlier run")

    # A refusal gets its own exit code: it is the one failure with an obvious
    # remedy (re-add the original file, or pass --no-resume), and a script
    # driving this CLI should be able to tell it apart from a real error.
    if refusals:
        return EXIT_REFUSED
    return EXIT_OK if failures == 0 else EXIT_ERROR


def cmd_restore(args: argparse.Namespace) -> int:
    forge = _forge(args)
    destination = args.output
    if destination is None:
        # A name may contain slashes; treat it as a relative path but never let
        # it escape upwards out of the working directory.
        destination = Path(args.name)
        if destination.is_absolute() or ".." in destination.parts:
            print(
                f"error: refusing to write {destination}: give --output explicitly",
                file=sys.stderr,
            )
            return EXIT_USAGE

    try:
        written = forge.restore(args.name, destination, verify=not args.no_verify)
    except KeyError:
        print(f"error: no manifest for {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    except (CorruptChunk, MissingChunk) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CORRUPT

    if args.stdout:
        with open(destination, "rb") as fh:
            os.write(sys.stdout.fileno(), fh.read())
        destination.unlink()
    else:
        print(f"restored {args.name} to {destination} ({_human(written)})")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    forge = _forge(args)
    manifests = forge.list_manifests()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": m.name,
                        "bytes": m.size,
                        "chunks": len(m.chunks),
                        "unique_bytes": m.unique_bytes,
                        "digest": m.file_digest,
                        "created": m.created,
                    }
                    for m in manifests
                ],
                indent=2,
            )
        )
        return EXIT_OK

    if not manifests:
        print(f"no files in {forge.root}")
        return EXIT_OK

    width = max(len(m.name) for m in manifests)
    print(f"{'name'.ljust(width)}  {'size':>10}  {'chunks':>7}  stored")
    for m in sorted(manifests, key=lambda m: m.name):
        print(
            f"{m.name.ljust(width)}  {_human(m.size):>10}  "
            f"{len(m.chunks):>7}  {_human(m.unique_bytes)}"
        )
    print(f"\n{len(manifests)} file(s), {_human(sum(m.size for m in manifests))} logical")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    forge = _forge(args)
    try:
        problems = forge.verify(args.name)
    except KeyError:
        print(f"error: no manifest for {args.name!r}", file=sys.stderr)
        return EXIT_ERROR

    if not problems:
        scope = args.name or "the archive"
        print(f"ok: {scope} verified")
        return EXIT_OK
    for problem in problems:
        print(f"corrupt: {problem}", file=sys.stderr)
    return EXIT_CORRUPT


def cmd_stats(args: argparse.Namespace) -> int:
    forge = _forge(args)
    stats = forge.stats()
    if args.json:
        print(json.dumps(stats, indent=2))
        return EXIT_OK

    print(f"archive:  {forge.root}")
    print(f"config:   {stats['config']}")
    print(f"files:    {stats['manifests']}")
    print(f"logical:  {_human(stats['logical_bytes'])}")
    print(f"stored:   {_human(stats['unique_bytes'])} in {stats['stored_objects']} chunks")
    if stats["shared_chunks"]:
        print(f"shared:   {stats['shared_chunks']} chunks referenced more than once")
    print(f"saved:    {_human(stats['saved_bytes'])} ({_pct(stats['dedupe_ratio'])})")
    return EXIT_OK


def cmd_gc(args: argparse.Namespace) -> int:
    forge = _forge(args)
    if args.dry_run:
        orphans = len(list(forge.store.iter_objects())) - len(forge.reachable())
        print(f"would delete {max(0, orphans)} chunk(s)")
        return EXIT_OK
    deleted, reclaimed = forge.collect_garbage()
    print(f"deleted {deleted} chunk(s), reclaimed {_human(reclaimed)}")
    return EXIT_OK


def cmd_forget(args: argparse.Namespace) -> int:
    forge = _forge(args)
    if not forge.forget(args.name):
        print(f"error: no manifest for {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    print(f"forgot {args.name}; run 'gc' to reclaim its chunks")
    return EXIT_OK


def cmd_chunks(args: argparse.Namespace) -> int:
    """Show where the boundaries fall. Diagnostic, not part of the format."""
    from .chunker import chunk_digests

    data = Path(args.file).read_bytes()
    pairs = list(chunk_digests(data, config_from_args(args) or ChunkerConfig()))
    if args.json:
        print(json.dumps([{"index": i, "length": n, "digest": d} for i, (d, n) in enumerate(pairs)], indent=2))
        return EXIT_OK
    for i, (digest, length) in enumerate(pairs):
        print(f"{i:>5}  {_human(length):>10}  {digest}")
    print(f"\n{len(pairs)} chunk(s) for {_human(len(data))}")
    return EXIT_OK


# -------------------------------------------------------------------- parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chunkforge",
        description="Content-defined chunking with deduplication and resumable ingest.",
    )
    parser.add_argument("--version", action="version", version=f"chunkforge {__version__}")
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help=f"archive directory (default: {DEFAULT_ROOT})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    _add_config_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("add", help="chunk a file into the archive")
    p.add_argument("paths", nargs="*")
    p.add_argument("--name", help="logical name (single input only)")
    p.add_argument("--no-resume", action="store_true", help="ignore any journal and start over")
    _add_json_flag(p)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("restore", help="rebuild a file from the archive")
    p.add_argument("name")
    p.add_argument("-o", "--output", help="where to write (default: the name itself)")
    p.add_argument("--no-verify", action="store_true", help="skip integrity checking")
    p.add_argument("--stdout", action="store_true", help="write to standard output")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("list", help="list stored files")
    _add_json_flag(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="check stored chunks against their manifests")
    p.add_argument("name", nargs="?", help="one file, or everything")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("stats", help="archive totals and deduplication savings")
    _add_json_flag(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("gc", help="delete chunks no manifest references")
    p.add_argument("--dry-run", action="store_true", help="only report what would go")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("forget", help="remove a manifest")
    p.add_argument("name")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("chunks", help="show the chunk boundaries of a file")
    p.add_argument("file")
    _add_config_flags(p, suppress=True)
    _add_json_flag(p)
    p.set_defaults(func=cmd_chunks)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR
    except (CorruptChunk, MissingChunk) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CORRUPT
    except ResumeRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (ManifestError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
