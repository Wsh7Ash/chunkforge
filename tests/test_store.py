"""Tests for the content-addressed store."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from chunkforge.store import (
    ChunkStore,
    CorruptChunk,
    MissingChunk,
    digest_of,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ChunkStore(self.dir.name)

    def payload(self, n: int = 1, fill: bytes = b"x") -> bytes:
        return fill * n


class TestDigesting(StoreTestCase):
    def test_digest_is_sha256_hex(self):
        import hashlib

        data = b"chunkforge"
        self.assertEqual(digest_of(data), hashlib.sha256(data).hexdigest())
        self.assertEqual(len(digest_of(data)), 64)

    def test_digest_is_lowercase_hex(self):
        d = digest_of(b"anything")
        self.assertTrue(all(c in "0123456789abcdef" for c in d))


class TestPutGet(StoreTestCase):
    def test_round_trip(self):
        data = self.payload(1000)
        stored = self.store.put(data)
        self.assertTrue(stored.created)
        self.assertEqual(self.store.get(stored.digest), data)

    def test_putting_twice_deduplicates(self):
        data = self.payload(500)
        first = self.store.put(data)
        second = self.store.put(data)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(len(list(self.store.iter_objects())), 1)

    def test_different_content_gives_different_digests(self):
        a = self.store.put(b"aaa")
        b = self.store.put(b"aab")
        self.assertNotEqual(a.digest, b.digest)

    def test_empty_payload_can_be_stored(self):
        stored = self.store.put(b"")
        self.assertEqual(stored.length, 0)
        self.assertEqual(self.store.get(stored.digest), b"")

    def test_large_payload_round_trips(self):
        data = os.urandom(300_000)
        self.assertEqual(self.store.get(self.store.put(data).digest), data)

    def test_objects_are_fanout_sharded(self):
        stored = self.store.put(b"hello")
        self.assertEqual(stored.digest[:2], self.store.path_for(stored.digest).parent.name)

    def test_store_creates_its_layout(self):
        self.assertTrue(self.store.objects_dir.is_dir())
        self.assertTrue(self.store.tmp_dir.is_dir())

    def test_staging_area_is_left_clean(self):
        for i in range(20):
            self.store.put(f"payload-{i}".encode())
        self.assertEqual(list(self.store.tmp_dir.iterdir()), [])

    def test_contains_and_len(self):
        stored = self.store.put(b"data")
        self.assertIn(stored.digest, self.store)
        self.assertNotIn("0" * 64, self.store)
        self.assertEqual(len(self.store), 1)

    def test_many_objects(self):
        for i in range(200):
            self.store.put(f"object number {i} with some padding".encode())
        self.assertEqual(len(self.store), 200)
        self.assertEqual(len(list(self.store.iter_objects())), 200)


class TestIntegrity(StoreTestCase):
    def test_corruption_is_detected(self):
        stored = self.store.put(b"original contents")
        self.store.path_for(stored.digest).write_bytes(b"tampered contents")

        with self.assertRaises(CorruptChunk):
            self.store.get(stored.digest)
        self.assertEqual(len(self.store.verify_all()), 1)

    def test_corruption_can_be_read_without_verification(self):
        stored = self.store.put(b"original contents")
        self.store.path_for(stored.digest).write_bytes(b"tampered contents")
        # Explicitly opting out is allowed; that is the point of the flag.
        self.assertEqual(self.store.get(stored.digest, verify=False), b"tampered contents")

    def test_truncation_is_detected(self):
        stored = self.store.put(b"a longer original payload")
        self.store.path_for(stored.digest).write_bytes(b"a longer")
        with self.assertRaises(CorruptChunk):
            self.store.get(stored.digest)

    def test_missing_chunk_raises(self):
        with self.assertRaises(MissingChunk):
            self.store.get("f" * 64)

    def test_verify_all_is_clean_on_a_healthy_store(self):
        for i in range(20):
            self.store.put(f"healthy payload {i}".encode())
        self.assertEqual(self.store.verify_all(), [])


class TestReadInto(StoreTestCase):
    def test_read_into_concatenates_in_order(self):
        import io

        a = b"first-payload"
        b = b"second-payload"
        da = self.store.put(a).digest
        db = self.store.put(b).digest

        sink = io.BytesIO()
        written = self.store.read_into([da, db, da], sink)
        self.assertEqual(sink.getvalue(), a + b + a)
        self.assertEqual(written, len(a) * 2 + len(b))

    def test_read_into_propagates_corruption(self):
        import io

        stored = self.store.put(b"payload")
        self.store.path_for(stored.digest).write_bytes(b"other")
        with self.assertRaises(CorruptChunk):
            self.store.read_into([stored.digest], io.BytesIO())


class TestLifecycle(StoreTestCase):
    def test_delete(self):
        stored = self.store.put(b"temporary")
        self.assertTrue(self.store.delete(stored.digest))
        self.assertFalse(self.store.has(stored.digest))
        self.assertFalse(self.store.delete(stored.digest))

    def test_delete_removes_the_empty_shard(self):
        stored = self.store.put(b"only object")
        shard = self.store.path_for(stored.digest).parent
        self.store.delete(stored.digest)
        self.assertFalse(shard.exists())

    def test_gc_keeps_reachable_and_drops_the_rest(self):
        keep = self.store.put(b"reachable").digest
        drop = self.store.put(b"unreachable").digest
        dropped, _ = self.store.collect_garbage({keep})
        self.assertEqual(dropped, 1)
        self.assertTrue(self.store.has(keep))
        self.assertFalse(self.store.has(drop))

    def test_gc_reports_reclaimed_bytes(self):
        data = os.urandom(4096)
        digest = self.store.put(data).digest
        _, reclaimed = self.store.collect_garbage(set())
        self.assertEqual(reclaimed, len(data))
        self.assertFalse(self.store.has(digest))

    def test_gc_with_everything_reachable_deletes_nothing(self):
        a = self.store.put(b"a").digest
        b = self.store.put(b"b").digest
        deleted, _ = self.store.collect_garbage({a, b})
        self.assertEqual(deleted, 0)

    def test_gc_tolerates_being_run_twice(self):
        keep = self.store.put(b"keep").digest
        self.store.put(b"drop")
        self.store.collect_garbage({keep})
        self.assertEqual(self.store.collect_garbage({keep}), (0, 0))


class TestStats(StoreTestCase):
    def test_stats_of_an_empty_store(self):
        stats = self.store.stats()
        self.assertEqual(stats.objects, 0)
        self.assertEqual(stats.unique_bytes, 0)

    def test_stats_counts_objects_and_bytes(self):
        self.store.put(b"a" * 1000)
        self.store.put(b"b" * 2000)
        stats = self.store.stats()
        self.assertEqual(stats.objects, 2)
        self.assertEqual(stats.unique_bytes, 3000)
        self.assertEqual(stats.logical_bytes, 3000)

    def test_stored_bytes_account_for_block_rounding(self):
        self.store.put(b"a" * 100)
        stats = self.store.stats()
        self.assertGreater(stats.stored_bytes, stats.unique_bytes)
        self.assertEqual(stats.overhead_bytes, stats.stored_bytes - stats.unique_bytes)

    def test_reopening_the_store_sees_the_same_objects(self):
        self.store.put(b"persisted payload")
        reopened = ChunkStore(self.dir.name)
        self.assertEqual(len(reopened), 1)


if __name__ == "__main__":
    unittest.main()
