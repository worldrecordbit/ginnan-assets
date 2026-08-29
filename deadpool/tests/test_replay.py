"""Validation against the forensic record.

These are the tests that matter most. The scorer can be internally consistent
and still wrong about the chain; here its arithmetic is replayed against 82
reconstructed transactions whose pool reserves before and after are known, and
against three large captures decoded leg by leg.

Every figure asserted below appears in ``docs/solana-rug-harvester-analysis.md``
and was derived there from raw ``getTransaction`` output.
"""

from __future__ import annotations

import statistics
import unittest

from deadpool import evidence
from deadpool.constants import LAMPORTS_PER_SOL
from deadpool.models import Verdict
from deadpool.scorer import capture_fraction, score_reserves
from deadpool.telemetry import detect_capture

SOL = LAMPORTS_PER_SOL

#: Standard supply for the mints in this record: 1e9 tokens at 6 decimals.
#: Corroborated by the record itself -- functioning pools in it hold 1e14-1e15
#: raw units, which is a large fraction of exactly this number, and the threat
#: model (2.2) states the same range independently.
SUPPLY = 10**15


class CaptureIdentityReplayTests(unittest.TestCase):
    """Does ``X / (b + X)`` reproduce what the pools actually did?"""

    @classmethod
    def setUpClass(cls):
        cls.swaps = evidence.load_swaps()
        cls.captures = [
            s for s in cls.swaps
            if s.side == "SELL" and s.observed_capture is not None and s.sale_size
        ]

    def test_the_record_is_the_shape_the_analysis_describes(self):
        self.assertEqual(len(self.swaps), 82)
        self.assertEqual(sum(1 for s in self.swaps if s.side == "BUY"), 49)
        self.assertEqual(len(self.captures), 32)

    def test_identity_predicts_every_observed_capture(self):
        # The bare identity omits the AMM's swap fee, so it should be high by
        # at most that fee -- a quarter of a percentage point, never more.
        for swap in self.captures:
            with self.subTest(sig=swap.sig[:12]):
                predicted = capture_fraction(swap.sale_size, swap.base_pre)
                observed = swap.observed_capture
                self.assertLess(
                    abs(predicted - observed),
                    0.0025,
                    f"{swap.sig}: predicted {predicted:.6f}, observed {observed:.6f}",
                )

    def test_the_identity_never_understates_the_capture(self):
        for swap in self.captures:
            with self.subTest(sig=swap.sig[:12]):
                self.assertGreaterEqual(
                    capture_fraction(swap.sale_size, swap.base_pre) + 1e-9,
                    swap.observed_capture,
                )

    def test_the_hard_coded_sale_ratio_is_visible_in_the_data(self):
        # Analysis section 2: sales cluster on ~1002x the residual reserve,
        # which by the identity yields 99.90% capture every time.
        ratios = [
            s.sale_size / s.base_pre for s in self.captures if s.base_pre > 0
        ]
        at_1002x = [r for r in ratios if 995 <= r <= 1010]
        self.assertGreaterEqual(len(at_1002x), 12)
        for swap in self.captures:
            if swap.base_pre > 0 and 995 <= swap.sale_size / swap.base_pre <= 1010:
                self.assertAlmostEqual(swap.observed_capture, 0.999, places=2)

    def test_supply_share_separates_drained_pools_from_live_ones(self):
        # The scale-free test, measured across the record. 23 of the 24
        # fully-drained pools hold 4.2e-10 of supply or less, while every
        # live pool holds at least 1.1e-3 -- six empty decades between them,
        # which is where the S4 threshold sits.
        drained = sorted(
            s.base_pre / SUPPLY for s in self.captures if s.observed_capture > 0.99
        )
        live = sorted(
            s.base_pre / SUPPLY for s in self.swaps
            if s.side == "BUY" and s.base_pre > 10**12
        )
        self.assertTrue(drained and live)
        self.assertGreaterEqual(sum(1 for x in drained if x < 1e-6), len(drained) - 1)
        self.assertLess(drained[-2], 1e-9)
        self.assertGreater(min(live), 1e-3)

    def test_the_one_pool_supply_share_misses_is_caught_by_the_quote_floor(self):
        # S4 is not sufficient alone, and this is the case that proves it:
        # a pool still holding 0.015% of supply but only 0.05 SOL. The
        # signals are deliberately independent so one covers the other.
        outlier = max(
            (s for s in self.captures if s.observed_capture > 0.99),
            key=lambda s: s.base_pre,
        )
        self.assertGreater(outlier.base_pre / SUPPLY, 1e-6)
        score = score_reserves(
            outlier.base_pre, outlier.quote_pre, SOL // 10, base_supply=SUPPLY
        )
        self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertIn("S2", score.signals)
        self.assertNotIn("S4", score.signals)

    def test_residual_reserves_are_orders_of_magnitude_below_live_pools(self):
        # Threat model 2.2 cites residual reserves of 1, 4, 22, 477, 8172 and
        # 380327 raw units against live reserves of 1e14-1e15: a separation
        # of about twelve orders of magnitude, which is why the thresholds
        # need no delicate tuning.
        harvested = sorted(s.base_pre for s in self.captures if s.observed_capture > 0.99)
        self.assertTrue(harvested)
        # All but one sit under ten million raw units; the exception is the
        # 0.05 SOL pool that S2 covers instead.
        self.assertLess(harvested[-2], 10**7)
        self.assertLess(statistics.median(harvested), 10**3)
        seeds = [s.base_pre for s in self.swaps if s.side == "BUY" and s.base_pre > 10**12]
        self.assertTrue(seeds)
        self.assertGreater(min(seeds) / harvested[-2], 10**6)


class ScorerAgainstTheRecordTests(unittest.TestCase):
    """Would the detector have flagged the pools that were actually drained?"""

    @classmethod
    def setUpClass(cls):
        cls.swaps = evidence.load_swaps()

    def test_every_pool_that_was_drained_scores_unsafe_beforehand(self):
        # Score each pool as it stood immediately before its capture, with a
        # 0.1 SOL hypothetical deposit. Not one may come back safe.
        missed = []
        for swap in self.swaps:
            if swap.side != "SELL" or swap.observed_capture is None:
                continue
            if swap.observed_capture < 0.99:
                continue  # partial captures are judged separately below
            score = score_reserves(swap.base_pre, swap.quote_pre, SOL // 10, base_supply=SUPPLY)
            if score.verdict is not Verdict.UNSAFE:
                missed.append((swap.sig, swap.base_pre, swap.quote_pre, score.verdict))
        self.assertEqual(missed, [], f"{len(missed)} drained pools not flagged unsafe")

    def test_dominated_partial_captures_are_flagged_too(self):
        # Captures of 50-99%: the sizing rule did not take everything, but
        # the pool was still dominated. These must not read as safe.
        checked = 0
        for swap in self.swaps:
            if swap.side != "SELL" or swap.observed_capture is None:
                continue
            if not 0.50 <= swap.observed_capture < 0.99:
                continue
            checked += 1
            with self.subTest(sig=swap.sig[:12]):
                score = score_reserves(
                    swap.base_pre, swap.quote_pre, SOL // 10, base_supply=SUPPLY
                )
                self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertGreaterEqual(checked, 3)

    def test_small_captures_into_functioning_pools_are_not_flagged(self):
        # The 4% and 27% rows are the operator selling tokens into a pool
        # that still had real reserves -- ordinary trading, not capture. A
        # detector that flagged these would flag half the chain.
        row = next(s for s in self.swaps if s.sig.startswith("Az7gQk4EpfF5"))
        self.assertLess(row.observed_capture, 0.05)
        score = score_reserves(row.base_pre, row.quote_pre, SOL // 10, base_supply=SUPPLY)
        self.assertIs(score.verdict, Verdict.SAFE)

    def test_healthy_pools_the_operators_seeded_score_safe(self):
        # The other side of the ledger: dust buys into functioning pools must
        # not be flagged, or the detector is useless to a router.
        rows = evidence.load_tsv(evidence.OTHER_WALLETS)
        healthy = [r for r in rows if r["note"].startswith("HEALTHY")]
        self.assertGreaterEqual(len(healthy), 3)
        for row in healthy:
            with self.subTest(sig=row["sig"]):
                score = score_reserves(
                    int(row["pool_tok_pre"]), int(row["pool_sol_pre"]), SOL // 10,
                    base_supply=SUPPLY,
                )
                self.assertIs(score.verdict, Verdict.SAFE)

    def test_dead_pools_the_operators_reloaded_score_unsafe(self):
        rows = evidence.load_tsv(evidence.OTHER_WALLETS)
        dead = [r for r in rows if r["note"].startswith("dead pool")]
        self.assertGreaterEqual(len(dead), 2)
        for row in dead:
            with self.subTest(sig=row["sig"]):
                score = score_reserves(
                    int(row["pool_tok_pre"]), int(row["pool_sol_pre"]), SOL // 10,
                    base_supply=SUPPLY,
                )
                self.assertIs(score.verdict, Verdict.UNSAFE)


class BigWinTests(unittest.TestCase):
    """The three large captures, decoded leg by leg."""

    @classmethod
    def setUpClass(cls):
        cls.rows = {(r["token"], r["leg"]): r for r in evidence.load_tsv(evidence.BIG_WINS)}

    def test_reported_capture_percentages_are_reproduced(self):
        for token, expected in [("USWR", 99.90), ("RICO", 99.90), ("TripleP", 99.01)]:
            with self.subTest(token=token):
                row = self.rows[(token, "SELL")]
                predicted = capture_fraction(abs(int(row["tok_delta"])), int(row["pool_tok_pre"]))
                self.assertAlmostEqual(predicted * 100, expected, places=1)
                observed = (
                    int(row["pool_sol_pre"]) - int(row["pool_sol_post"])
                ) / int(row["pool_sol_pre"])
                self.assertAlmostEqual(observed * 100, expected, places=1)

    def test_the_pools_drained_were_all_flagged_unsafe(self):
        for token in ("USWR", "RICO", "TripleP"):
            with self.subTest(token=token):
                row = self.rows[(token, "SELL")]
                score = score_reserves(
                    int(row["pool_tok_pre"]), int(row["pool_sol_pre"]), SOL // 10,
                    base_supply=SUPPLY,
                )
                self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_the_pools_seeded_while_healthy_were_not_flagged(self):
        # The USWR dust buy went into a live 345 SOL pool. A detector that
        # flagged that would flag every migration on the chain.
        row = self.rows[("USWR", "BUY")]
        score = score_reserves(
            int(row["pool_tok_pre"]), int(row["pool_sol_pre"]), SOL // 10, base_supply=SUPPLY
        )
        self.assertIs(score.verdict, Verdict.SAFE)

    def test_the_post_rug_reload_went_into_a_pool_scored_unsafe(self):
        # TripleP's second ticket bought into a pool holding 0.00188 SOL --
        # a full rug, and 180,000x more token-efficient per lamport.
        row = self.rows[("TripleP", "BUY2")]
        score = score_reserves(
            int(row["pool_tok_pre"]), int(row["pool_sol_pre"]), SOL // 10, base_supply=SUPPLY
        )
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_capture_events_are_detected_from_reserve_deltas(self):
        # Signal S9, computed from pre/post balances rather than log strings.
        for token in ("USWR", "RICO", "TripleP"):
            with self.subTest(token=token):
                row = self.rows[(token, "SELL")]
                event = detect_capture(
                    row["mint"], int(row["pool_sol_pre"]), int(row["pool_sol_post"])
                )
                self.assertIsNotNone(event)
                self.assertGreaterEqual(event.captured_fraction, 0.99)


class OperatorSignatureTests(unittest.TestCase):
    """Structural facts the detector's telemetry claims to key on."""

    @classmethod
    def setUpClass(cls):
        cls.swaps = evidence.load_swaps()

    def test_every_sell_is_a_durable_nonce_transaction_and_no_buy_is(self):
        # Analysis section 4: 32 nonce sells, 0 nonce buys. The clearest
        # structural signature in the dataset (signal S7).
        sells = [s for s in self.swaps if s.side == "SELL"]
        buys = [s for s in self.swaps if s.side == "BUY"]
        self.assertTrue(all(s.nonce for s in sells))
        self.assertFalse(any(b.nonce for b in buys))

    def test_direction_is_taken_from_balance_deltas_not_side_labels(self):
        # The orientation trap: a wallet's own token delta is the only
        # trustworthy direction signal. Every SELL must show tokens leaving.
        for swap in self.swaps:
            if swap.side == "SELL":
                with self.subTest(sig=swap.sig[:12]):
                    self.assertTrue(swap.is_sell)

    def test_hit_rate_sample_reconciles(self):
        # 26 successes against 74 failures across four error classes.
        attempts = evidence.load_harvest_attempts()
        failures = (
            int(attempts["fail_6002_ExceededSlippage_meteora"])
            + int(attempts["fail_6040_BuySlippageBelowMinBaseAmountOut_pumpswap"])
            + int(attempts["fail_6004"])
            + int(attempts["fail_ProgramFailedToComplete"])
        )
        success = int(attempts["success"])
        self.assertEqual(success + failures, 100)
        self.assertAlmostEqual(success / (success + failures) * 100,
                               float(attempts["hit_rate_pct"]), places=1)

    def test_the_min_out_floor_sits_at_the_modal_opportunity(self):
        # Analysis 5h: the resolved three-way race was over a pool holding
        # 10,000,002 lamports -- two lamports above the operator's own
        # min_out floor, which is what makes that the modal opportunity. The
        # detector's quote floor sits above it, so such a pool is flagged.
        from deadpool.constants import DEFAULT_THRESHOLDS, OPERATOR_MIN_OUT_FLOOR_LAMPORTS

        self.assertEqual(OPERATOR_MIN_OUT_FLOOR_LAMPORTS, 10_000_000)
        self.assertGreaterEqual(
            DEFAULT_THRESHOLDS.s2_quote_reserve_lamports, OPERATOR_MIN_OUT_FLOOR_LAMPORTS
        )
        score = score_reserves(22, 10_000_002, SOL // 10, base_supply=SUPPLY)
        self.assertIs(score.verdict, Verdict.UNSAFE)


if __name__ == "__main__":
    unittest.main()
