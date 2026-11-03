import math
import unittest

from ml.features import FEATURE_NAMES, FeatureConfig, FeatureState, REGULAR_OPEN_NS


def observation(sequence, offset=None, **changes):
    offset = sequence * 100_000_000 if offset is None else offset
    row = {"sequence": sequence, "timestamp_ns": REGULAR_OPEN_NS + offset,
           "bid": 1_000_000 + sequence * 100, "ask": 1_000_400 + sequence * 100,
           "bid_qty": 120 + sequence, "ask_qty": 100, "valid": True, "tradable": True}
    row.update(changes)
    return row


class CausalFeatureTests(unittest.TestCase):
    def test_prefix_features_are_unchanged_by_appending_future(self):
        inputs = [observation(index) for index in range(1, 80)]
        short, full = FeatureState(), FeatureState()
        short_samples = [sample for row in inputs[:45] if (sample := short.update(row))]
        full_samples = [sample for row in inputs if (sample := full.update(row))]
        self.assertEqual(short_samples, full_samples[:len(short_samples)])
        self.assertEqual(len(short_samples[0].features), len(FEATURE_NAMES))
        self.assertTrue(all(math.isfinite(value) for value in short_samples[-1].features))

    def test_same_timestamp_first_row_only_and_later_rows_update_future(self):
        state = FeatureState(FeatureConfig(window_events=4))
        for index in range(1, 4):
            self.assertIsNone(state.update(observation(index)))
        first = state.update(observation(4))
        self.assertIsNotNone(first)
        self.assertIsNone(state.update(observation(5, offset=400_000_000)))
        later = state.update(observation(6, offset=500_000_000))
        self.assertEqual(first.mid_units, 1_000_600)
        self.assertEqual(later.mid_units, 1_000_800)
        self.assertEqual(state.history[-2][1], 1_000_700)

    def test_same_timestamp_warmup_does_not_emit_hindsight_final_row(self):
        state = FeatureState(FeatureConfig(window_events=4))
        for index in range(1, 6):
            self.assertIsNone(state.update(observation(index, offset=100_000_000)))
        self.assertIsNotNone(state.update(observation(6, offset=200_000_000)))

    def test_duplicate_book_messages_do_not_advance_warmup(self):
        state = FeatureState(FeatureConfig(window_events=4))
        row = observation(1)
        for index in range(1, 9):
            copy = {**row, "sequence": index, "timestamp_ns": row["timestamp_ns"] + index * 1_000_000}
            self.assertIsNone(state.update(copy))
        self.assertEqual(len(state.history), 1)

    def test_halt_and_gap_require_new_warmup(self):
        for mode in ("halt", "gap"):
            state = FeatureState(FeatureConfig(window_events=4))
            for index in range(1, 5):
                state.update(observation(index))
            if mode == "halt":
                self.assertIsNone(state.update(observation(5, tradable=False)))
                base = 500_000_000
            else:
                base = 2_000_000_000
            for index in range(6, 9):
                self.assertIsNone(state.update(observation(index, offset=base + (index - 5) * 100_000_000)))
            self.assertIsNotNone(state.update(observation(9, offset=base + 400_000_000)))

    def test_all_messages_and_bbo_only_streams_match_after_unchanged_gap(self):
        online, offline = FeatureState(FeatureConfig(window_events=4)), FeatureState(FeatureConfig(window_events=4))
        prefix = [observation(index) for index in range(1, 5)]
        for row in prefix:
            self.assertEqual(online.update(row), offline.update(row))
        stale = prefix[-1]
        for sequence in range(5, 10):
            online.update({**stale, "sequence": sequence, "timestamp_ns": REGULAR_OPEN_NS + 1_400_000_000 + (sequence - 4) * 100_000_000})
        for sequence in range(10, 16):
            row = observation(sequence, offset=2_000_000_000 + (sequence - 9) * 100_000_000)
            self.assertEqual(online.update(row), offline.update(row))
        self.assertEqual(online.history, offline.history)

    def test_bad_market_or_ordering_fail_closed(self):
        for changes in ({"ask": 1}, {"bid_qty": 0}, {"valid": "true"}, {"bid": True}):
            with self.assertRaises(ValueError):
                FeatureState().update(observation(1, **changes))
        state = FeatureState()
        state.update(observation(1))
        with self.assertRaises(ValueError):
            state.update(observation(1))
        state = FeatureState()
        state.update(observation(1))
        with self.assertRaises(ValueError):
            state.update(observation(2, offset=0))


if __name__ == "__main__":
    unittest.main()
