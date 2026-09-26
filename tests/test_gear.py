"""Tests for the gear rolling hash."""

from __future__ import annotations

import unittest

from chunkforge.gear import (
    GEAR_DIGEST,
    GEAR_SEED,
    GEAR_TABLE,
    MASK64,
    build_gear_table,
    gear_hash,
    gear_hash_from,
)


class TestGearTable(unittest.TestCase):
    def test_table_is_the_documented_one(self):
        # Pinned so a change to the generator cannot silently move every chunk
        # boundary in every archive built with this library.
        self.assertEqual(
            GEAR_DIGEST,
            "ae29eb591df382f2324979d66ca9451f49c12a11168c98fccd5e8425c12ff376",
        )

    def test_table_has_one_entry_per_byte_value(self):
        self.assertEqual(len(GEAR_TABLE), 256)

    def test_table_entries_fit_in_64_bits(self):
        self.assertTrue(all(0 <= v <= MASK64 for v in GEAR_TABLE))

    def test_table_is_reproducible_from_the_seed(self):
        self.assertEqual(build_gear_table(GEAR_SEED), GEAR_TABLE)

    def test_a_different_seed_gives_a_different_table(self):
        self.assertNotEqual(build_gear_table(GEAR_SEED + 1), GEAR_TABLE)

    def test_table_is_not_trivially_degenerate(self):
        # 256 distinct values, and not merely a permutation of 0..255, which is
        # what a broken generator would tend to produce.
        self.assertEqual(len(set(GEAR_TABLE)), 256)
        self.assertNotEqual(set(GEAR_TABLE), set(range(256)))


class TestGearHash(unittest.TestCase):
    def test_empty_input_is_the_empty_hash(self):
        self.assertEqual(gear_hash(b""), 0)

    def test_hash_is_stable(self):
        self.assertEqual(gear_hash(b"the quick brown fox"), gear_hash(b"the quick brown fox"))

    def test_single_bit_difference_changes_the_hash(self):
        a = gear_hash(b"\x00" * 64)
        b = gear_hash(b"\x00" * 63 + b"\x01")
        self.assertNotEqual(a, b)

    def test_rolling_matches_whole_hashing(self):
        # The property that makes the chunker possible: hashing a prefix and
        # continuing must equal hashing the whole thing.
        data = bytes(range(256)) * 4
        for split in (0, 1, 255, 256, 257, 1000):
            with self.subTest(split=split):
                prefix = gear_hash(data[:split])
                self.assertEqual(gear_hash_from(prefix, data[split:]), gear_hash(data))

    def test_rolling_from_zero_is_plain_hashing(self):
        data = b"chunkforge" * 40
        self.assertEqual(gear_hash_from(0, data), gear_hash(data))

    def test_state_stays_inside_64_bits(self):
        # The mask is applied every step, so a long input cannot overflow into
        # a value outside the documented range.
        for length in (1, 100, 5000):
            with self.subTest(length=length):
                self.assertLessEqual(gear_hash(b"x" * length), MASK64)

    def test_hash_survives_a_long_run_of_identical_bytes(self):
        # A pathological input for any rolling hash. It must not collapse.
        state = gear_hash(b"\x00" * 100_000)
        self.assertLessEqual(state, MASK64)


if __name__ == "__main__":
    unittest.main()
