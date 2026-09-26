"""Tests for the high-level API: ingest, deduplicate, resume, verify, restore.

The resume tests here are the ones worth reading. Resumable ingest is easy to
fake: a journal that records nothing looks exactly like one that works right up
until the source changes underneath it. So each test perturbs the source in one
specific way and asserts the resume is *refused* with a reason, rather than
silently producing a file that is a mixture of two different versions.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

from chunkforge.chunker import ChunkerConfig
from chunkforge.forge import EMPTY_CHAIN, ChunkForge, Interrupted, ResumeRefused, _Journal
from chunkforge.manifest import ChunkRef, Manifest
from chunkforge.store import CorruptChunk

SMALL = ChunkerConfig(min_size=256, bits=10, max_size=4096)


class NonSeekable:
    """A read-only stream that refuses to rewind, like a socket or a pipe."""

    def __init__(self, data: bytes, name: str = "pipe") -> None:
        self._data = data
        self._pos = 0
        self.name = name

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        out = self._data[self._pos : self._pos + size]
        self._pos += len(out)
        return out

    def seekable(self) -> bool:
        return False


class ForgeTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.forge = ChunkForge(self.root / "store", SMALL)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def blob(self, n: int = 400_000) -> bytes:
        return os.urandom(n)

    def object_path(self, digest: str) -> Path:
        return self.forge.store.path_for(digest)

    def corrupt(self, digest: str, data: bytes = b"corrupted") -> None:
        self.object_path(digest).write_bytes(data)

    def stop_after(self, count: int):
        def hook(index: int, total: int) -> None:
            if index >= count:
                raise Interrupted()

        return hook

    def journal_files(self):
        return list((self.root / "store" / "journal").glob("*.jsonl"))


class TestIngest(ForgeTestCase):
    def test_add_from_a_path(self):
        path = self.write("a.bin", self.blob())
        result = self.forge.add(path)
        self.assertEqual(result.manifest.name, "a.bin")
        self.assertEqual(result.manifest.size, path.stat().st_size)
        self.assertTrue(self.forge.has_manifest("a.bin"))

    def test_add_from_bytes(self):
        result = self.forge.add(b"some bytes", name="mem")
        self.assertEqual(result.manifest.name, "mem")
        self.assertEqual(result.manifest.size, 10)

    def test_add_from_a_stream(self):
        data = self.blob(50_000)
        result = self.forge.add(io.BytesIO(data), name="stream")
        self.assertEqual(result.manifest.size, len(data))

    def test_caller_stream_is_left_open(self):
        # The caller owns the stream they passed in; closing it would be rude and
        # surprising.
        stream = io.BytesIO(self.blob(50_000))
        self.forge.add(stream, name="s")
        self.assertFalse(stream.closed)

    def test_add_does_not_leak_file_handles(self):
        # 300 files in a tight loop: an unclosed handle per add would run into
        # the process limit long before this finishes.
        for i in range(300):
            self.forge.add(self.write(f"f{i}.bin", b"payload" * (i + 1)))
        self.assertEqual(len(self.forge.list_manifests()), 300)

    def test_add_records_the_config_in_force(self):
        self.forge.add(self.write("a.bin", self.blob(50_000)))
        self.assertEqual(
            self.forge.get_manifest("a.bin").config,
            {"min_size": 256, "bits": 10, "max_size": 4096, "normalised": False},
        )

    def test_file_digest_is_the_digest_of_the_whole_file(self):
        import hashlib

        data = self.blob(80_000)
        result = self.forge.add(self.write("a.bin", data))
        self.assertEqual(result.manifest.file_digest, hashlib.sha256(data).hexdigest())

    def test_add_an_empty_file(self):
        result = self.forge.add(self.write("empty.bin", b""))
        self.assertEqual(result.manifest.chunks, [])
        self.assertEqual(result.manifest.size, 0)
        self.assertEqual(result.dedupe_ratio, 0.0)

    def test_add_overwrites_an_existing_manifest(self):
        first = self.blob(50_000)
        second = self.blob(60_000)
        self.forge.add(self.write("a.bin", first))
        result = self.forge.add(self.write("a.bin", second))
        self.assertEqual(result.manifest.size, 60_000)
        self.assertEqual(len(self.forge.list_manifests()), 1)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.forge.add(self.root / "nope.bin")

    def test_successful_add_leaves_no_journal(self):
        self.forge.add(self.write("a.bin", self.blob(50_000)))
        self.assertEqual(self.journal_files(), [])

    def test_name_defaults_to_the_file_name(self):
        self.forge.add(self.write("named.bin", b"data"))
        self.assertTrue(self.forge.has_manifest("named.bin"))

    def test_result_describes_itself(self):
        result = self.forge.add(self.write("a.bin", self.blob(50_000)))
        self.assertIn("a.bin", result.describe())
        self.assertIn("reused", result.describe())


class TestDeduplication(ForgeTestCase):
    def test_an_identical_file_stores_nothing_new(self):
        data = self.blob(300_000)
        path = self.write("a.bin", data)
        self.forge.add(path)
        objects_after_first = len(self.forge.store)

        result = self.forge.add(self.write("b.bin", data))
        self.assertEqual(result.chunks_new, 0)
        self.assertEqual(len(self.forge.store), objects_after_first)
        self.assertAlmostEqual(result.dedupe_ratio, 1.0, places=6)

    def test_a_modified_copy_reuses_almost_everything(self):
        # The payoff of content-defined chunking: one byte changed in the middle
        # of a large file should cost one chunk, not the whole file.
        data = self.blob(400_000)
        self.forge.add(self.write("v1.bin", data))

        edited = bytearray(data)
        edited[200_000] ^= 0xFF
        result = self.forge.add(self.write("v2.bin", bytes(edited)))

        self.assertLess(
            result.chunks_new, 6,
            f"a one-byte edit cost {result.chunks_new} new chunks",
        )
        self.assertGreater(result.dedupe_ratio, 0.9)

    def test_a_prepended_file_reuses_almost_everything(self):
        data = self.blob(400_000)
        self.forge.add(self.write("v1.bin", data))
        result = self.forge.add(self.write("v2.bin", b"\x00" + data))
        self.assertGreater(result.dedupe_ratio, 0.9)

    def test_completely_different_files_share_nothing(self):
        self.forge.add(self.write("a.bin", self.blob(100_000)))
        result = self.forge.add(self.write("b.bin", self.blob(100_000)))
        self.assertEqual(result.chunks_new, result.chunks_total)
        self.assertEqual(result.dedupe_ratio, 0.0)

    def test_a_file_deduplicates_against_itself(self):
        # A file of periodic content is mostly the same bytes over and over.
        # Boundaries do not line up with the period, so the chunks are rotations
        # of it rather than one identical chunk -- but there are only as many
        # distinct chunks as there are phase offsets, so the savings are large.
        data = b"0123456789" * 50000
        result = self.forge.add(data, name="rep")
        self.assertGreater(result.chunks_total, 50)
        self.assertLessEqual(
            result.chunks_new, 10,
            "distinct chunk contents are limited by the phase of the period",
        )
        self.assertGreater(result.dedupe_ratio, 0.75)
        out = self.root / "rep.out"
        self.forge.restore("rep", out)
        self.assertEqual(out.read_bytes(), data)

    def test_new_and_dedup_bytes_add_up(self):
        data = self.blob(120_000)
        self.forge.add(self.write("a.bin", data))
        result = self.forge.add(self.write("b.bin", data))
        self.assertEqual(result.bytes_new + result.bytes_deduplicated, result.bytes_total)
        self.assertEqual(result.chunks_new + result.chunks_deduplicated, result.chunks_total)


class TestRestore(ForgeTestCase):
    def test_restore_reproduces_the_file(self):
        data = self.blob(300_000)
        self.forge.add(self.write("a.bin", data))
        out = self.root / "out" / "a.bin"
        written = self.forge.restore("a.bin", out)
        self.assertEqual(written, len(data))
        self.assertEqual(out.read_bytes(), data)

    def test_restore_of_an_empty_file(self):
        self.forge.add(self.write("empty.bin", b""))
        out = self.root / "out.bin"
        self.assertEqual(self.forge.restore("empty.bin", out), 0)
        self.assertEqual(out.read_bytes(), b"")

    def test_restore_creates_parent_directories(self):
        self.forge.add(self.write("a.bin", b"data"))
        out = self.root / "x" / "y" / "z" / "a.bin"
        self.forge.restore("a.bin", out)
        self.assertTrue(out.is_file())

    def test_restore_of_an_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            self.forge.restore("ghost", self.root / "out")

    def test_restore_detects_a_corrupted_chunk(self):
        data = self.blob(200_000)
        result = self.forge.add(self.write("a.bin", data))
        self.corrupt(result.manifest.chunks[0].digest)

        with self.assertRaises(CorruptChunk):
            self.forge.restore("a.bin", self.root / "out.bin")

    def test_round_trip_survives_content_sharing(self):
        a, b = self.blob(200_000), self.blob(200_000)
        self.forge.add(self.write("a.bin", a))
        self.forge.add(self.write("b.bin", b))
        self.forge.restore("a.bin", self.root / "a.out")
        self.forge.restore("b.bin", self.root / "b.out")
        self.assertEqual((self.root / "a.out").read_bytes(), a)
        self.assertEqual((self.root / "b.out").read_bytes(), b)


class TestVerify(ForgeTestCase):
    def test_a_healthy_store_verifies_clean(self):
        for i in range(5):
            self.forge.add(self.write(f"f{i}.bin", self.blob(80_000)))
        self.assertEqual(self.forge.verify(), [])

    def test_verify_reports_a_corrupted_chunk(self):
        result = self.forge.add(self.write("a.bin", self.blob(80_000)))
        self.corrupt(result.manifest.chunks[0].digest)
        problems = self.forge.verify()
        self.assertEqual(len(problems), 1)
        self.assertIn("a.bin", problems[0])

    def test_verify_reports_a_missing_chunk(self):
        result = self.forge.add(self.write("a.bin", self.blob(80_000)))
        self.object_path(result.manifest.chunks[0].digest).unlink()
        self.assertEqual(len(self.forge.verify()), 1)

    def test_verify_a_single_name_ignores_the_rest(self):
        self.forge.add(self.write("good.bin", b"fine"))
        bad = self.forge.add(self.write("bad.bin", self.blob(80_000)))
        self.corrupt(bad.manifest.chunks[0].digest)
        self.assertEqual(self.forge.verify("good.bin"), [])
        self.assertEqual(len(self.forge.verify()), 1)

    def test_verify_of_an_empty_archive_is_clean(self):
        self.assertEqual(self.forge.verify(), [])

    def test_verify_of_an_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            self.forge.verify("ghost")


class TestInterruption(ForgeTestCase):
    def test_interruption_leaves_a_resumable_journal(self):
        data = self.blob(300_000)
        path = self.write("a.bin", data)
        with self.assertRaises(Interrupted):
            self.forge.add(path, on_chunk=self.stop_after(5))

        self.assertEqual(len(self.journal_files()), 1)
        self.assertFalse(self.forge.has_manifest("a.bin"))

    def test_resume_finishes_the_file(self):
        data = self.blob(300_000)
        path = self.write("a.bin", data)
        with self.assertRaises(Interrupted):
            self.forge.add(path, on_chunk=self.stop_after(5))

        result = self.forge.add(path)
        self.assertTrue(result.resumed)
        self.assertGreater(result.skipped_chunks, 0)
        self.assertEqual(result.manifest.size, len(data))
        self.assertEqual(self.journal_files(), [])

        out = self.root / "out.bin"
        self.forge.restore("a.bin", out)
        self.assertEqual(out.read_bytes(), data)

    def test_resume_does_not_restore_stored_chunks_twice(self):
        data = self.blob(300_000)
        path = self.write("a.bin", data)
        with self.assertRaises(Interrupted):
            self.forge.add(path, on_chunk=self.stop_after(5))
        stored_before = len(self.forge.store)

        self.forge.add(path)
        added = len(self.forge.store) - stored_before
        total = self.forge.get_manifest("a.bin")
        self.assertLessEqual(added, len(total.chunks))

    def test_the_callback_sees_progress(self):
        seen: list[tuple[int, int]] = []
        self.forge.add(
            self.write("a.bin", self.blob(100_000)),
            on_chunk=lambda i, total: seen.append((i, total)),
        )
        self.assertEqual([i for i, _ in seen], list(range(1, len(seen) + 1)))
        self.assertTrue(all(t > 0 for _, t in seen))
        self.assertEqual(seen[-1][1], 100_000)

    def test_progress_is_monotonic(self):
        seen: list[int] = []
        self.forge.add(
            self.write("a.bin", self.blob(200_000)),
            on_chunk=lambda i, total: seen.append(total),
        )
        self.assertEqual(seen, sorted(seen))

    def test_resume_false_starts_over(self):
        data = self.blob(300_000)
        path = self.write("a.bin", data)
        with self.assertRaises(Interrupted):
            self.forge.add(path, on_chunk=self.stop_after(5))
        result = self.forge.add(path, resume=False)
        self.assertFalse(result.resumed)
        self.assertEqual(result.skipped_chunks, 0)
        self.assertEqual(self.journal_files(), [])

    def test_interruption_of_an_empty_file_completes(self):
        # No chunk means no callback, so there is nothing to interrupt.
        result = self.forge.add(self.write("empty.bin", b""), on_chunk=self.stop_after(1))
        self.assertTrue(result.manifest.chunks == [])


class TestResumeRefusals(ForgeTestCase):
    """A resume that cannot be trusted must fail loudly, not quietly."""

    def interrupt(self, name: str = "a.bin", data: bytes | None = None, after: int = 5):
        data = data if data is not None else self.blob(300_000)
        path = self.write(name, data)
        with self.assertRaises(Interrupted):
            self.forge.add(path, on_chunk=self.stop_after(after))
        return path

    def test_a_source_edited_mid_ingest_is_refused(self):
        path = self.interrupt()
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))

        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(path)
        self.assertIn("changed", str(ctx.exception))

    def test_a_source_grown_is_refused(self):
        path = self.interrupt()
        path.write_bytes(path.read_bytes() + os.urandom(1000))
        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(path)
        self.assertIn("bytes", str(ctx.exception))

    def test_a_source_shrunk_is_refused(self):
        path = self.interrupt()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(path)
        self.assertIn("bytes", str(ctx.exception))

    def test_a_swapped_source_of_the_same_size_is_refused(self):
        # Same length, different content: the case a size check alone would miss.
        path = self.interrupt()
        path.write_bytes(self.blob(path.stat().st_size))
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)

    def test_a_source_truncated_in_the_verified_prefix_is_refused(self):
        # Same total size, but the bytes moved within it.
        data = self.blob(300_000)
        path = self.interrupt(data=data)
        reordered = data[100_000:] + data[:100_000]
        path.write_bytes(reordered)
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)

    def test_a_non_seekable_stream_cannot_resume(self):
        data = self.blob(300_000)
        self.interrupt(data=data)
        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(NonSeekable(data), name="a.bin")
        self.assertIn("non-seekable", str(ctx.exception))

    def test_a_different_chunker_config_cannot_resume(self):
        data = self.blob(300_000)
        path = self.interrupt(data=data)
        other = ChunkForge(self.root / "store", ChunkerConfig(min_size=512, bits=10, max_size=4096))
        with self.assertRaises(ResumeRefused):
            other.add(path)

    def test_a_corrupt_journal_is_refused_not_ignored(self):
        path = self.interrupt()
        journal = self.journal_files()[0]
        journal.write_text("{ this is not a journal", encoding="utf-8")
        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(path)
        self.assertIn("unreadable", str(ctx.exception))

    def test_a_journal_from_a_future_version_is_refused(self):
        path = self.interrupt()
        import json

        journal = self.journal_files()[0]
        raw = json.loads(journal.read_text(encoding="utf-8"))
        raw["version"] = 999
        journal.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)

    def test_an_internally_inconsistent_journal_is_refused(self):
        # Lengths that do not add up to the byte count.
        path = self.interrupt()
        import json

        journal = self.journal_files()[0]
        raw = json.loads(journal.read_text(encoding="utf-8"))
        raw["consumed"] = 999_999
        journal.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)

    def test_a_journal_missing_its_config_is_refused(self):
        path = self.interrupt()
        import json

        journal = self.journal_files()[0]
        raw = json.loads(journal.read_text(encoding="utf-8"))
        raw["config"] = {}
        journal.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ResumeRefused) as ctx:
            self.forge.add(path)
        self.assertIn("config", str(ctx.exception))

    def test_a_refused_resume_leaves_the_journal_in_place(self):
        # So the user can inspect it, or retry against the original file.
        path = self.interrupt()
        before = self.journal_files()[0].read_text(encoding="utf-8")
        path.write_bytes(self.blob(path.stat().st_size))
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)
        self.assertEqual(self.journal_files()[0].read_text(encoding="utf-8"), before)

    def test_a_refused_resume_writes_no_manifest(self):
        path = self.interrupt()
        path.write_bytes(self.blob(path.stat().st_size))
        with self.assertRaises(ResumeRefused):
            self.forge.add(path)
        self.assertFalse(self.forge.has_manifest("a.bin"))


class TestJournalInternals(ForgeTestCase):
    def test_a_fresh_journal_starts_from_the_empty_chain(self):
        path = self.root / "j.jsonl"
        journal = _Journal(path=path, name="x", size=0, config={"min_size": 1, "bits": 0, "max_size": 1})
        self.assertEqual(journal.running, EMPTY_CHAIN)
        self.assertEqual(journal.consumed, 0)

    def test_the_chain_advances_with_each_chunk(self):
        path = self.root / "j.jsonl"
        journal = _Journal(path=path, name="x", size=0, config={"min_size": 1, "bits": 0, "max_size": 1})
        first = journal.running
        journal.append(ChunkRef("a" * 64, 4), b"abcd")
        self.assertNotEqual(journal.running, first)
        self.assertEqual(journal.consumed, 4)

    def test_load_returns_none_for_a_missing_file(self):
        self.assertIsNone(_Journal.load(self.root / "absent.jsonl"))

    def test_load_round_trips(self):
        path = self.root / "j.jsonl"
        journal = _Journal(path=path, name="x", size=10, config={"min_size": 1, "bits": 0, "max_size": 1})
        journal.append(ChunkRef("a" * 64, 10), b"0123456789")
        loaded = _Journal.load(path)
        self.assertEqual(loaded.name, "x")
        self.assertEqual(loaded.consumed, 10)
        self.assertEqual(loaded.running, journal.running)
        self.assertEqual(loaded.chunks, journal.chunks)

    def test_load_rejects_a_journal_whose_lengths_disagree(self):
        import json

        path = self.root / "j.jsonl"
        journal = _Journal(path=path, name="x", size=10, config={"min_size": 1, "bits": 0, "max_size": 1})
        journal.append(ChunkRef("a" * 64, 10), b"0123456789")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["consumed"] = 11
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertIsNone(_Journal.load(path))

    def test_load_rejects_an_empty_journal_with_a_non_empty_chain(self):
        import json

        path = self.root / "j.jsonl"
        journal = _Journal(path=path, name="x", size=0, config={"min_size": 1, "bits": 0, "max_size": 1})
        journal.flush()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["running"] = "f" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertIsNone(_Journal.load(path))


class TestManifestManagement(ForgeTestCase):
    def test_list_manifests_is_ordered_and_complete(self):
        for name in ("c.bin", "a.bin", "b.bin"):
            self.forge.add(self.write(name, b"data-" + name.encode()))
        names = [m.name for m in self.forge.list_manifests()]
        self.assertEqual(sorted(names), ["a.bin", "b.bin", "c.bin"])

    def test_get_manifest_of_an_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            self.forge.get_manifest("ghost")

    def test_forget_removes_the_manifest(self):
        self.forge.add(self.write("a.bin", b"data"))
        self.assertTrue(self.forge.forget("a.bin"))
        self.assertFalse(self.forge.has_manifest("a.bin"))
        self.assertFalse(self.forge.forget("a.bin"))

    def test_manifest_keys_are_collision_free_across_names(self):
        # Names that would be dangerous as raw filenames.
        for name in ("a/b/c.bin", "../escape.bin", "with:colon.bin", "UPPER.BIN"):
            with self.subTest(name=name):
                self.forge.add(self.write("tmp", b"data-" + name.encode()), name=name)
                self.assertTrue(self.forge.has_manifest(name))
        self.assertEqual(len(self.forge.list_manifests()), 4)

    def test_manifest_stays_inside_the_manifests_directory(self):
        self.forge.add(b"data", name="../../escape")
        path = self.forge.manifest_path("../../escape")
        self.assertEqual(path.parent, self.root / "store" / "manifests")


class TestStats(ForgeTestCase):
    def test_stats_of_an_empty_archive(self):
        stats = self.forge.stats()
        self.assertEqual(stats["manifests"], 0)
        self.assertEqual(stats["logical_bytes"], 0)
        self.assertEqual(stats["dedupe_ratio"], 0.0)

    def test_stats_report_the_saving_from_dedupe(self):
        data = self.blob(300_000)
        self.forge.add(self.write("a.bin", data))
        stats = self.forge.stats()
        self.assertEqual(stats["manifests"], 1)
        self.assertEqual(stats["logical_bytes"], 300_000)
        self.assertEqual(stats["saved_bytes"], 0)

        self.forge.add(self.write("b.bin", data))
        stats = self.forge.stats()
        self.assertEqual(stats["manifests"], 2)
        self.assertEqual(stats["logical_bytes"], 600_000)
        # 300 KiB of real bytes standing in for 600 KiB of files.
        self.assertEqual(stats["referenced_bytes"], 300_000)
        self.assertEqual(stats["saved_bytes"], 300_000)
        self.assertAlmostEqual(stats["dedupe_ratio"], 0.5, places=6)

    def test_stats_are_computable_at_scale(self):
        # The per-object stats used to make this quadratic. 60 files is enough to
        # notice a 100x slowdown without a long test.
        for i in range(60):
            self.forge.add(self.write(f"f{i}.bin", b"payload " * 500))
        self.assertEqual(self.forge.stats()["manifests"], 60)

    def test_stats_include_the_active_config(self):
        self.assertEqual(self.forge.stats()["config"], SMALL.describe())


class TestGarbageCollection(ForgeTestCase):
    def test_gc_keeps_referenced_chunks(self):
        self.forge.add(self.write("a.bin", self.blob(100_000)))
        deleted, _ = self.forge.collect_garbage()
        self.assertEqual(deleted, 0)
        self.assertEqual(self.forge.verify(), [])

    def test_gc_reclaims_orphans(self):
        orphan = self.forge.store.put(b"never referenced").digest
        self.forge.add(self.write("a.bin", b"referenced data"))
        deleted, reclaimed = self.forge.collect_garbage()
        self.assertEqual(deleted, 1)
        self.assertEqual(reclaimed, len(b"never referenced"))
        self.assertNotIn(orphan, self.forge.store)

    def test_gc_after_forget_reclaims_the_chunks(self):
        data = self.blob(100_000)
        self.forge.add(self.write("a.bin", data))
        self.forge.forget("a.bin")
        deleted, _ = self.forge.collect_garbage()
        self.assertEqual(deleted, len(self.forge.store) + deleted)

    def test_gc_keeps_chunks_shared_by_a_surviving_manifest(self):
        data = self.blob(200_000)
        self.forge.add(self.write("a.bin", data))
        self.forge.add(self.write("b.bin", data))
        self.forge.forget("a.bin")
        self.forge.collect_garbage()
        out = self.root / "b.out"
        self.forge.restore("b.bin", out)
        self.assertEqual(out.read_bytes(), data)


class TestConstruction(ForgeTestCase):
    def test_reopening_a_forge_sees_existing_manifests(self):
        data = self.blob(100_000)
        self.forge.add(self.write("a.bin", data))
        reopened = ChunkForge(self.root / "store", SMALL)
        out = self.root / "out.bin"
        reopened.restore("a.bin", out)
        self.assertEqual(out.read_bytes(), data)

    def test_a_default_config_is_used_when_none_is_given(self):
        forge = ChunkForge(self.root / "other")
        self.assertIsNotNone(forge.config)

    def test_layout_directories_are_created(self):
        ChunkForge(self.root / "fresh")
        self.assertTrue((self.root / "fresh" / "manifests").is_dir())
        self.assertTrue((self.root / "fresh" / "journal").is_dir())
        self.assertTrue((self.root / "fresh" / "objects").is_dir())

    def test_repr_is_informative(self):
        self.assertIn("ChunkForge", repr(self.forge))


if __name__ == "__main__":
    unittest.main()
