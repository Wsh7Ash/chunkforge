"""Tests for the command-line interface.

Run in a subprocess against the real entry point rather than calling ``main()``
in-process, so that exit codes, argument parsing and stdout all behave the way a
user would see them. ``PYTHONPATH`` is pointed at ``src/``, so no install step
is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_REFUSED = 4


def run(*args: str, cwd: Path | None = None, stdin: bytes | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "chunkforge", *args],
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=stdin,
    )


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = Path(self.dir.name)
        self.archive = str(self.work / "archive")
        self.add_args = ["--root", self.archive, "--bits", "10", "--min-size", "256", "--max-size", "4096"]

    def payload(self, n: int = 120_000) -> bytes:
        return os.urandom(n)

    def make(self, name: str, data: bytes) -> Path:
        path = self.work / name
        path.write_bytes(data)
        return path

    def add(self, *args: str, expect: int = EXIT_OK):
        result = run(*self.add_args, "add", *args)
        self.assertEqual(result.returncode, expect, result.stderr.decode())
        return result


class TestEntryPoint(CliTestCase):
    def test_help_exits_zero(self):
        result = run("--help")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"chunkforge", result.stdout)

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(run().returncode, EXIT_USAGE)

    def test_unknown_command_is_a_usage_error(self):
        self.assertEqual(run("frobnicate").returncode, EXIT_USAGE)

    def test_version_is_reported(self):
        result = run("--version")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"chunkforge", result.stdout)

    def test_every_documented_command_exists(self):
        output = run("--help").stdout.decode()
        for command in ("add", "restore", "list", "verify", "stats", "gc", "forget", "chunks"):
            self.assertIn(command, output)


class TestAdd(CliTestCase):
    def test_add_then_verify(self):
        self.add(str(self.make("a.bin", self.payload())))
        self.assertEqual(run(*self.add_args, "verify").returncode, EXIT_OK)

    def test_add_reports_progress(self):
        result = self.add(str(self.make("a.bin", self.payload())))
        self.assertIn(b"stored a.bin", result.stdout)

    def test_add_json_output(self):
        result = self.add(str(self.make("a.bin", self.payload())), "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["name"], "a.bin")
        self.assertGreater(report["chunks"], 1)
        self.assertEqual(report["new_bytes"], report["bytes"])

    def test_json_flag_works_before_and_after_the_subcommand(self):
        # argparse pitfall: a global flag that a subparser silently resets.
        data = self.payload()
        self.make("a.bin", data)
        self.add(str(self.work / "a.bin"))
        after = run(*self.add_args, "stats", "--json")
        before = run(*self.add_args, "--json", "stats")
        self.assertEqual(after.returncode, EXIT_OK)
        self.assertEqual(before.returncode, EXIT_OK)
        self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))

    def test_add_with_a_custom_name(self):
        self.add(str(self.make("a.bin", self.payload())), "--name", "archive/2026/notes")
        self.assertEqual(run(*self.add_args, "list").stdout.count(b"notes"), 1)

    def test_add_two_files(self):
        self.add(str(self.make("a.bin", self.payload(60_000))), str(self.make("b.bin", self.payload(70_000))))
        listing = run(*self.add_args, "list").stdout
        self.assertIn(b"a.bin", listing)
        self.assertIn(b"b.bin", listing)

    def test_add_the_same_file_twice_deduplicates(self):
        data = self.payload()
        self.make("a.bin", data)
        self.add(str(self.work / "a.bin"))
        result = self.add(str(self.make("b.bin", data)), "--json")
        self.assertEqual(json.loads(result.stdout)["new_bytes"], 0)

    def test_add_a_missing_file_fails(self):
        result = run(*self.add_args, "add", str(self.work / "ghost.bin"))
        self.assertEqual(result.returncode, EXIT_ERROR)
        self.assertIn(b"no such file", result.stderr)

    def test_add_with_no_paths_is_a_usage_error(self):
        result = run(*self.add_args, "add")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn(b"no input files", result.stderr)

    def test_one_bad_file_does_not_stop_the_others(self):
        good = str(self.make("good.bin", self.payload(30_000)))
        result = run(*self.add_args, "add", str(self.work / "ghost.bin"), good)
        self.assertEqual(result.returncode, EXIT_ERROR)
        self.assertIn(b"good.bin", result.stdout)

    def test_config_flags_change_the_chunking(self):
        path = str(self.make("a.bin", self.payload(200_000)))
        run("--root", self.archive, "--bits", "14", "--max-size", "65536", "add", path)
        wide = json.loads(run(*self.add_args, "list", "--json").stdout)[0]["chunks"]
        self.assertGreater(wide, 1)

    def test_add_an_empty_file(self):
        result = self.add(str(self.make("empty.bin", b"")), "--json")
        self.assertEqual(json.loads(result.stdout)["chunks"], 0)


class TestRestore(CliTestCase):
    def test_round_trip_through_the_cli(self):
        data = self.payload()
        self.make("a.bin", data)
        self.add(str(self.work / "a.bin"))
        out = self.work / "out.bin"
        result = run(*self.add_args, "restore", "a.bin", "-o", str(out))
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(out.read_bytes(), data)

    def test_restore_to_stdout(self):
        data = self.payload(20_000)
        self.make("a.bin", data)
        self.add(str(self.work / "a.bin"))
        result = run(*self.add_args, "restore", "a.bin", "--stdout")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(result.stdout, data)

    def test_restore_without_output_uses_the_name(self):
        self.make("a.bin", b"payload")
        self.add(str(self.work / "a.bin"))
        workdir = self.work
        result = run(*self.add_args, "restore", "a.bin", cwd=workdir)
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual((workdir / "a.bin").read_bytes(), b"payload")

    def test_restore_refuses_a_traversing_name(self):
        # A name is attacker-influenced data; it must not choose where to write.
        self.add(str(self.make("a.bin", b"payload")), "--name", "../escape")
        result = run(*self.add_args, "restore", "../escape", cwd=self.work)
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertFalse((self.work.parent / "escape").exists())

    def test_restore_an_unknown_name_fails(self):
        self.assertEqual(run(*self.add_args, "restore", "ghost").returncode, EXIT_ERROR)

    def test_restore_detects_corruption(self):
        self.add(str(self.make("a.bin", self.payload())))
        listing = run(*self.add_args, "list", "--json").stdout
        # Corrupt the first object on disk by truncating it.
        objects = sorted((Path(self.archive) / "objects").rglob("*"))
        victim = next(p for p in objects if p.is_file() and len(p.name) == 62)
        victim.write_bytes(b"corrupt")
        self.assertIsInstance(listing, bytes)
        result = run(*self.add_args, "restore", "a.bin", "-o", str(self.work / "out.bin"))
        self.assertEqual(result.returncode, EXIT_CORRUPT)
        self.assertIn(b"corrupt", result.stderr.lower())


class TestList(CliTestCase):
    def test_list_of_an_empty_archive(self):
        result = run(*self.add_args, "list")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"no files", result.stdout)

    def test_list_shows_files(self):
        self.add(str(self.make("a.bin", b"one")))
        self.add(str(self.make("b.bin", b"two")))
        listing = run(*self.add_args, "list").stdout
        self.assertIn(b"a.bin", listing)
        self.assertIn(b"b.bin", listing)
        self.assertIn(b"2 file(s)", listing)

    def test_list_json(self):
        self.add(str(self.make("a.bin", self.payload())))
        report = json.loads(run(*self.add_args, "list", "--json").stdout)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["name"], "a.bin")
        self.assertEqual(len(report[0]["digest"]), 64)


class TestVerify(CliTestCase):
    def test_verify_a_healthy_archive(self):
        self.add(str(self.make("a.bin", self.payload())))
        result = run(*self.add_args, "verify")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"ok", result.stdout)

    def test_verify_one_name(self):
        self.add(str(self.make("a.bin", b"fine")))
        self.add(str(self.make("b.bin", self.payload())))
        result = run(*self.add_args, "verify", "a.bin")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"a.bin", result.stdout)

    def test_verify_reports_corruption_with_a_distinct_exit_code(self):
        self.add(str(self.make("a.bin", self.payload())))
        objects = sorted((Path(self.archive) / "objects").rglob("*"))
        victim = next(p for p in objects if p.is_file() and len(p.name) == 62)
        victim.write_bytes(b"corrupt")
        result = run(*self.add_args, "verify")
        self.assertEqual(result.returncode, EXIT_CORRUPT)
        self.assertIn(b"corrupt", result.stderr)

    def test_verify_an_unknown_name_fails(self):
        self.assertEqual(run(*self.add_args, "verify", "ghost").returncode, EXIT_ERROR)


class TestStatsAndGc(CliTestCase):
    def test_stats_of_an_empty_archive(self):
        result = run(*self.add_args, "stats")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"files:    0", result.stdout)

    def test_stats_json(self):
        self.add(str(self.make("a.bin", self.payload())))
        report = json.loads(run(*self.add_args, "stats", "--json").stdout)
        self.assertEqual(report["manifests"], 1)
        self.assertIn("config", report)

    def test_stats_shows_savings(self):
        data = self.payload()
        self.make("a.bin", data)
        self.add(str(self.work / "a.bin"))
        self.add(str(self.make("b.bin", data)))
        report = json.loads(run(*self.add_args, "stats", "--json").stdout)
        self.assertEqual(report["saved_bytes"], len(data))

    def test_gc_keeps_referenced_chunks(self):
        self.add(str(self.make("a.bin", self.payload())))
        result = run(*self.add_args, "gc")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"deleted 0", result.stdout)
        self.assertEqual(run(*self.add_args, "verify").returncode, EXIT_OK)

    def test_gc_dry_run_changes_nothing(self):
        self.add(str(self.make("a.bin", self.payload())))
        result = run(*self.add_args, "gc", "--dry-run")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"would delete", result.stdout)

    def test_forget_then_gc(self):
        self.add(str(self.make("a.bin", self.payload())))
        self.add(str(self.make("b.bin", self.payload(50_000))))
        result = run(*self.add_args, "forget", "a.bin")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"forgot", result.stdout)
        run(*self.add_args, "gc")
        self.assertEqual(run(*self.add_args, "verify", "b.bin").returncode, EXIT_OK)

    def test_forget_an_unknown_name_fails(self):
        self.assertEqual(run(*self.add_args, "forget", "ghost").returncode, EXIT_ERROR)


class TestChunksCommand(CliTestCase):
    def test_chunks_lists_boundaries(self):
        path = self.make("a.bin", self.payload())
        result = run(*self.add_args, "chunks", str(path))
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(b"chunk(s) for", result.stdout)

    def test_chunks_json(self):
        path = str(self.make("a.bin", self.payload(60_000)))
        report = json.loads(run(*self.add_args, "chunks", path, "--json").stdout)
        self.assertGreater(len(report), 1)
        self.assertEqual(sum(c["length"] for c in report), 60_000)
        self.assertEqual(len(report[0]["digest"]), 64)


class TestInterruptedIngest(CliTestCase):
    def test_a_journal_survives_a_kill_and_the_resume_works(self):
        # Kill the process mid-ingest for real, rather than simulating it.
        data = os.urandom(1_500_000)
        path = self.make("big.bin", data)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)

        # Ingest with a callback that hard-exits partway through.
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from chunkforge import ChunkForge, ChunkerConfig, Interrupted\n"
            "f = ChunkForge(%r, ChunkerConfig(min_size=256, bits=10, max_size=4096))\n"
            "def stop(i, t):\n"
            "    if i >= 12: raise SystemExit(3)\n"
            "f.add(%r, on_chunk=stop)\n" % (str(SRC), self.archive, str(path))
        )
        first = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, env=env
        )
        self.assertEqual(first.returncode, 3, first.stderr.decode())

        journals = list((Path(self.archive) / "journal").glob("*.jsonl"))
        self.assertEqual(len(journals), 1, "a journal should survive the kill")

        # The plain command resumes it.
        second = run(*self.add_args, "add", str(path), "--json")
        self.assertEqual(second.returncode, EXIT_OK, second.stderr.decode())
        report = json.loads(second.stdout)
        self.assertTrue(report["resumed"])
        self.assertGreater(report["chunks"], 12)

        out = self.work / "out.bin"
        self.assertEqual(run(*self.add_args, "restore", "big.bin", "-o", str(out)).returncode, EXIT_OK)
        self.assertEqual(out.read_bytes(), data)

    def test_a_resume_against_a_changed_file_is_refused(self):
        data = os.urandom(1_500_000)
        path = self.make("big.bin", data)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from chunkforge import ChunkForge, ChunkerConfig\n"
            "f = ChunkForge(%r, ChunkerConfig(min_size=256, bits=10, max_size=4096))\n"
            "def stop(i, t):\n"
            "    if i >= 12: raise SystemExit(3)\n"
            "f.add(%r, on_chunk=stop)\n" % (str(SRC), self.archive, str(path))
        )
        subprocess.run([sys.executable, "-c", script], capture_output=True, env=env)

        changed = bytearray(data)
        changed[0] ^= 0xFF
        path.write_bytes(bytes(changed))

        result = run(*self.add_args, "add", str(path))
        self.assertEqual(result.returncode, EXIT_REFUSED)
        self.assertIn(b"refusing to resume", result.stderr)


if __name__ == "__main__":
    unittest.main()
