"""Tests for content-defined chunking.

The test that matters most is :meth:`TestResynchronisation.test_a_local_edit_leaves_the_rest_intact`.
It is the whole reason content-defined chunking exists, and a fixed-size
chunker fails it while still passing every other test here.
"""

from __future__ import annotations

import io
import os
import unittest

from chunkforge.chunker import (
    ChunkerConfig,
    average_size_for_bits,
    bits_for_average_size,
    chunk_data,
    chunk_digests,
    chunk_stream,
)
from chunkforge.gear import GEAR_TABLE, MASK64, gear_hash

SMALL = ChunkerConfig(min_size=64, bits=8, max_size=2048)


def digests(data: bytes, config: ChunkerConfig = SMALL) -> list[str]:
    return [d for d, _ in chunk_digests(data, config)]


def fixed_split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


class TestChunkerConfig(unittest.TestCase):
    def test_rejects_zero_min_size(self):
        with self.assertRaises(ValueError):
            ChunkerConfig(min_size=0)

    def test_rejects_max_below_min(self):
        with self.assertRaises(ValueError):
            ChunkerConfig(min_size=100, max_size=99)

    def test_rejects_out_of_range_bits(self):
        for bits in (-1, 33):
            with self.subTest(bits=bits), self.assertRaises(ValueError):
                ChunkerConfig(bits=bits)

    def test_mask_has_the_requested_width(self):
        self.assertEqual(ChunkerConfig(bits=10).mask, 0b1111111111)
        self.assertEqual(ChunkerConfig(bits=0).mask, 0)

    def test_average_size_matches_the_mask_width(self):
        for bits in (4, 10, 14):
            with self.subTest(bits=bits):
                self.assertEqual(average_size_for_bits(bits), 1 << bits)

    def test_bits_and_average_are_inverse(self):
        for average in (1, 2, 256, 16384, 65536):
            with self.subTest(average=average):
                self.assertEqual(
                    average_size_for_bits(bits_for_average_size(average)), average
                )

    def test_config_is_immutable(self):
        with self.assertRaises(Exception):
            SMALL.bits = 20  # type: ignore[misc]


class TestReferenceAgreement(unittest.TestCase):
    def test_boundaries_match_the_documented_hash_arithmetic(self):
        # An independent reimplementation of "walk the bytes, cut when the low
        # bits of the gear state are all zero". If the chunker's inlined hot loop
        # ever drifts from chunkforge.gear, this fails.
        data = os.urandom(20_000)
        config = ChunkerConfig(min_size=32, bits=9, max_size=1024)

        expected: list[bytes] = []
        buf = bytearray()
        state = 0
        for byte in data:
            buf.append(byte)
            state = ((state << 1) + GEAR_TABLE[byte]) & MASK64
            if len(buf) >= config.max_size:
                expected.append(bytes(buf))
                buf.clear()
                state = 0
            elif len(buf) >= config.min_size and (state & config.mask) == 0:
                expected.append(bytes(buf))
                buf.clear()
                state = 0
        if buf:
            expected.append(bytes(buf))

        self.assertEqual(chunk_data(data, config), expected)


class TestInvariants(unittest.TestCase):
    def setUp(self):
        self.data = os.urandom(200_000)

    def test_reassembly_is_lossless(self):
        for config in (
            SMALL,
            ChunkerConfig(min_size=1, bits=4, max_size=64),
            ChunkerConfig(min_size=256, bits=12, max_size=8192),
            ChunkerConfig(min_size=100, bits=9, max_size=1024, normalised=True),
        ):
            with self.subTest(config=config.describe()):
                self.assertEqual(b"".join(chunk_data(self.data, config)), self.data)

    def test_no_chunk_is_below_min_size_except_the_last(self):
        config = ChunkerConfig(min_size=100, bits=9, max_size=2048)
        chunks = chunk_data(self.data, config)
        for chunk in chunks[:-1]:
            self.assertGreaterEqual(len(chunk), config.min_size)
        self.assertLessEqual(len(chunks[-1]), config.max_size)

    def test_no_chunk_exceeds_max_size(self):
        for config in (
            ChunkerConfig(min_size=32, bits=6, max_size=512),
            ChunkerConfig(min_size=32, bits=14, max_size=512),
            ChunkerConfig(min_size=32, bits=6, max_size=512, normalised=True),
        ):
            with self.subTest(config=config.describe()):
                self.assertLessEqual(
                    max(len(c) for c in chunk_data(self.data, config)), config.max_size
                )

    def test_max_size_caps_a_run_with_no_natural_boundary(self):
        # Repeated bytes are the pathological case for a rolling hash. Without a
        # hard cap this yields one enormous chunk.
        config = ChunkerConfig(min_size=16, bits=20, max_size=256)
        chunks = chunk_data(b"\x5a" * 100_000, config)
        self.assertGreater(len(chunks), 100)
        self.assertTrue(all(len(c) <= 256 for c in chunks))

    def test_empty_input_yields_no_chunks(self):
        self.assertEqual(chunk_data(b"", SMALL), [])

    def test_input_shorter_than_min_size_is_one_chunk(self):
        data = b"tiny"
        self.assertEqual(chunk_data(data, SMALL), [data])

    def test_chunking_is_deterministic(self):
        first = chunk_data(self.data, SMALL)
        second = chunk_data(self.data, SMALL)
        self.assertEqual(first, second)

    def test_chunk_count_tracks_the_size(self):
        config = ChunkerConfig(min_size=64, bits=8, max_size=2048)
        small = len(chunk_data(self.data, config))
        large = len(chunk_data(self.data + self.data, config))
        self.assertGreater(large, small * 1.5)

    def test_mask_of_zero_yields_fixed_size_chunks(self):
        # bits=0 means the condition is always true, so the only thing bounding a
        # chunk is max_size -- and with min_size == max_size the two coincide, so
        # the output is exactly fixed-size splitting. A useful cross-check that
        # the content-defined machinery can be switched off cleanly.
        config = ChunkerConfig(min_size=1000, bits=0, max_size=1000)
        chunks = chunk_data(self.data, config)
        self.assertTrue(all(len(c) == 1000 for c in chunks[:-1]))
        self.assertLessEqual(len(chunks[-1]), 1000)
        self.assertEqual(
            [len(c) for c in chunks],
            fixed_split(self.data, 1000) and [len(c) for c in fixed_split(self.data, 1000)],
        )

    def test_normalised_keeps_sizes_closer_to_the_average(self):
        config = ChunkerConfig(min_size=256, bits=13, max_size=32768)
        plain = chunk_data(self.data, config)
        normalised = chunk_data(
            self.data, ChunkerConfig(min_size=256, bits=13, max_size=32768, normalised=True)
        )

        def spread(chunks):
            sizes = [len(c) for c in chunks]
            mean = sum(sizes) / len(sizes)
            return max(abs(s - mean) for s in sizes) / mean

        self.assertLessEqual(
            spread(normalised),
            spread(plain),
            "FastCDC normalisation should tighten the size distribution",
        )


class TestStreamEquivalence(unittest.TestCase):
    def setUp(self):
        self.data = os.urandom(120_000)

    def test_chunking_does_not_depend_on_read_size(self):
        expected = chunk_data(self.data, SMALL)
        for read_size in (1, 2, 7, 999, 4096, 1 << 20):
            with self.subTest(read_size=read_size):
                got = list(chunk_stream(io.BytesIO(self.data), SMALL, read_size=read_size))
                self.assertEqual(got, expected)

    def test_streaming_rejects_a_zero_read_size(self):
        with self.assertRaises(ValueError):
            list(chunk_stream(io.BytesIO(b"x"), SMALL, read_size=0))

    def test_empty_stream_yields_nothing(self):
        self.assertEqual(list(chunk_stream(io.BytesIO(b""), SMALL)), [])

    def test_chunk_data_delegates_to_the_stream(self):
        self.assertEqual(chunk_data(self.data, SMALL), list(chunk_stream(io.BytesIO(self.data), SMALL)))

    def test_memory_stays_bounded_for_a_large_input(self):
        # The generator must not accumulate; peak buffered bytes are bounded by
        # max_size + read_size, not by the input length.
        data = os.urandom(3_000_000)
        config = ChunkerConfig(min_size=256, bits=12, max_size=4096)
        stream = chunk_stream(io.BytesIO(data), config, read_size=64 * 1024)
        total = 0
        for chunk in stream:
            total += len(chunk)
            self.assertLessEqual(len(chunk), config.max_size)
        self.assertEqual(total, len(data))


class TestResynchronisation(unittest.TestCase):
    """The property content-defined chunking exists to provide."""

    def setUp(self):
        self.data = os.urandom(300_000)

    def test_a_local_edit_leaves_the_rest_intact(self):
        # Prepending one byte shifts every fixed-size boundary in the file, so a
        # fixed chunker keeps nothing. CDC keeps everything but the first chunk.
        config = ChunkerConfig(min_size=256, bits=10, max_size=4096)
        original = digests(self.data, config)
        edited = digests(b"\x00" + self.data, config)

        shared = set(original) & set(edited)
        self.assertGreater(
            len(shared),
            len(original) * 0.9,
            f"only {len(shared)}/{len(original)} chunks survived a one-byte prepend",
        )

    def test_an_edit_in_the_middle_does_not_disturb_the_prefix(self):
        config = ChunkerConfig(min_size=256, bits=10, max_size=4096)
        cut = len(self.data) // 2
        edited = self.data[:cut] + b"\xff\xff\xff" + self.data[cut:]

        before = digests(self.data, config)
        after = digests(edited, config)

        # Chunks entirely before the edit must survive untouched.
        prefix_bytes = self.data[:cut]
        expected_prefix = 0
        seen = 0
        for chunk in chunk_data(self.data, config):
            if seen + len(chunk) > cut:
                break
            expected_prefix += 1
            seen += len(chunk)

        survivors = [
            d
            for d in before[:expected_prefix]
            if d in set(after)
        ]
        self.assertGreater(
            len(survivors),
            expected_prefix - 3,
            "chunks entirely before the edit should be reused",
        )

    def test_identical_content_gives_identical_chunks(self):
        self.assertEqual(
            chunk_data(self.data, SMALL),
            chunk_data(self.data, SMALL),
        )

    def test_different_content_gives_different_chunks(self):
        self.assertNotEqual(
            set(digests(self.data, SMALL)),
            set(digests(os.urandom(300_000), SMALL)),
        )

    def test_fixed_size_chunking_has_none_of_this_property(self):
        # The contrast, so the test above is not just asserting that two lists
        # happened to be equal. Fixed boundaries shift by one, so a one-byte
        # prepend leaves nothing reusable.
        a = fixed_split(self.data, 1024)
        b = fixed_split(b"\x00" + self.data, 1024)
        self.assertEqual({c for c in a} & {c for c in b}, set())
        self.assertGreater(len(a), 10)


class TestChunkDigests(unittest.TestCase):
    def test_digests_and_lengths_match_the_chunks(self):
        data = os.urandom(50_000)
        chunks = chunk_data(data, SMALL)
        pairs = chunk_digests(data, SMALL)
        self.assertEqual(len(pairs), len(chunks))
        for (digest, length), chunk in zip(pairs, chunks):
            self.assertEqual(length, len(chunk))
            self.assertEqual(len(digest), 64)

    def test_lengths_sum_to_the_input(self):
        data = os.urandom(50_000)
        self.assertEqual(sum(l for _, l in chunk_digests(data, SMALL)), len(data))


if __name__ == "__main__":
    unittest.main()
