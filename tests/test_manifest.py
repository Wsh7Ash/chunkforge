"""Tests for the manifest format.

The format is the durable interface between chunkforge and a future version of
it, so the tests care about two things in particular: a round trip loses
nothing, and anything malformed is rejected loudly instead of being guessed at.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chunkforge.manifest import (
    MANIFEST_VERSION,
    ChunkRef,
    Manifest,
    ManifestError,
    UnsupportedVersion,
)


def sample() -> Manifest:
    return Manifest(
        name="docs/notes.txt",
        size=3000,
        file_digest="a" * 64,
        chunks=[
            ChunkRef("b" * 64, 1000),
            ChunkRef("c" * 64, 1000),
            ChunkRef("d" * 64, 1000),
        ],
        created="2026-09-26T00:00:00+00:00",
        config={"min_size": 2048, "bits": 14, "max_size": 65536, "normalised": False},
    )


class TestChunkRef(unittest.TestCase):
    def test_round_trip(self):
        ref = ChunkRef("a" * 64, 42)
        self.assertEqual(ChunkRef.from_json(ref.as_json()), ref)

    def test_rejects_wrong_digest_length(self):
        for bad in ("a" * 63, "a" * 65, "", 123, None):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ChunkRef.from_json({"digest": bad, "length": 10})

    def test_rejects_non_hex_digest(self):
        with self.assertRaises(ManifestError):
            ChunkRef.from_json({"digest": "z" * 64, "length": 10})

    def test_rejects_uppercase_hex_digest(self):
        # Case matters: digests are compared as strings throughout the store.
        with self.assertRaises(ManifestError):
            ChunkRef.from_json({"digest": "A" * 64, "length": 10})

    def test_rejects_bad_lengths(self):
        for bad in (0, -1, 1.5, "10", True, None):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ChunkRef.from_json({"digest": "a" * 64, "length": bad})

    def test_rejects_non_object(self):
        with self.assertRaises(ManifestError):
            ChunkRef.from_json(["a" * 64, 10])

    def test_is_hashable_and_comparable(self):
        self.assertEqual({ChunkRef("a" * 64, 1), ChunkRef("a" * 64, 1)}, {ChunkRef("a" * 64, 1)})


class TestManifestValidation(unittest.TestCase):
    def test_rejects_negative_size(self):
        with self.assertRaises(ManifestError):
            Manifest(name="x", size=-1, file_digest="a" * 64, chunks=[])

    def test_rejects_chunk_lengths_that_disagree_with_size(self):
        # The single most important check in the format: a manifest whose parts
        # do not add up to its whole cannot be trusted to rebuild anything.
        with self.assertRaises(ManifestError):
            Manifest(name="x", size=999, file_digest="a" * 64, chunks=[ChunkRef("a" * 64, 10)])

    def test_accepts_an_empty_file(self):
        m = Manifest(name="empty", size=0, file_digest="e" * 64, chunks=[])
        self.assertEqual(m.unique_bytes, 0)
        self.assertEqual(m.reachable(), set())

    def test_accepts_a_consistent_manifest(self):
        self.assertEqual(sample().size, 3000)


class TestDerivedValues(unittest.TestCase):
    def test_unique_bytes_counts_each_digest_once(self):
        m = Manifest(
            name="dupes",
            size=300,
            file_digest="a" * 64,
            chunks=[ChunkRef("a" * 64, 100), ChunkRef("a" * 64, 100), ChunkRef("b" * 64, 100)],
        )
        self.assertEqual(m.unique_bytes, 200)

    def test_reachable_is_the_digest_set(self):
        self.assertEqual(sample().reachable(), {"b" * 64, "c" * 64, "d" * 64})


class TestSerialisation(unittest.TestCase):
    def test_json_round_trip(self):
        original = sample()
        restored = Manifest.from_json(original.as_json())
        self.assertEqual(restored, original)

    def test_json_keys_are_stable(self):
        self.assertEqual(
            set(sample().as_json()),
            {"version", "name", "size", "file_digest", "created", "config", "chunks"},
        )

    def test_version_is_recorded(self):
        self.assertEqual(sample().as_json()["version"], MANIFEST_VERSION)

    def test_rejects_a_future_version(self):
        # Better to fail than to misread someone else's future format.
        raw = sample().as_json()
        raw["version"] = MANIFEST_VERSION + 1
        with self.assertRaises(UnsupportedVersion):
            Manifest.from_json(raw)

    def test_unsupported_version_is_a_manifest_error(self):
        self.assertTrue(issubclass(UnsupportedVersion, ManifestError))

    def test_rejects_version_zero_and_negatives(self):
        for bad in (0, -1):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                Manifest.from_json({**sample().as_json(), "version": bad})

    def test_rejects_non_integer_version(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json({**sample().as_json(), "version": "1"})

    def test_rejects_a_non_object(self):
        for bad in ([], "manifest", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                Manifest.from_json(bad)

    def test_rejects_missing_chunks(self):
        raw = sample().as_json()
        del raw["chunks"]
        with self.assertRaises(ManifestError):
            Manifest.from_json(raw)

    def test_rejects_a_bad_file_digest(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json({**sample().as_json(), "file_digest": "short"})

    def test_optional_fields_default_when_absent(self):
        raw = sample().as_json()
        del raw["name"], raw["created"], raw["config"]
        restored = Manifest.from_json(raw)
        self.assertEqual(restored.name, "")
        self.assertEqual(restored.created, "")
        self.assertEqual(restored.config, {})


class TestFileIO(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "nested" / "m.json"

    def test_write_then_read(self):
        original = sample()
        original.write(self.path)
        self.assertEqual(Manifest.read(self.path), original)

    def test_write_creates_parent_directories(self):
        sample().write(self.path)
        self.assertTrue(self.path.is_file())

    def test_write_leaves_no_staging_file(self):
        sample().write(self.path)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_write_is_atomic_and_replaces(self):
        first = sample()
        first.write(self.path)
        second = Manifest(
            name="other", size=0, file_digest="e" * 64, chunks=[]
        )
        second.write(self.path)
        self.assertEqual(Manifest.read(self.path), second)

    def test_file_is_human_readable_json(self):
        sample().write(self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["name"], "docs/notes.txt")
        self.assertEqual(len(raw["chunks"]), 3)

    def test_read_of_malformed_json_raises(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            Manifest.read(self.path)

    def test_now_is_a_utc_iso_timestamp(self):
        stamp = Manifest.now()
        self.assertTrue(stamp.endswith("+00:00"), stamp)
        self.assertNotIn(".", stamp, "seconds resolution is deliberate")


if __name__ == "__main__":
    unittest.main()
