"""The analytic core, tested on its own."""

from __future__ import annotations

import unittest

from deadpool.constants import LAMPORTS_PER_SOL, Thresholds
from deadpool.models import Overhang, Verdict
from deadpool.scorer import (
    capture_fraction,
    simulate_capture,
    claim_cost_for_capture,
    quote_out,
    sale_size_for_capture,
    score_reserves,
    tokens_out,
)

SOL = LAMPORTS_PER_SOL


class CaptureIdentityTests(unittest.TestCase):
    """``X / (b + X)`` -- the whole mechanism in one expression."""

    def test_ratio_table_from_the_threat_model(self):
        # Threat model 2.1 tabulates these five rungs.
        for multiple, expected in [(1, 0.5), (10, 0.909091), (100, 0.990099), (1000, 0.999001),
                                   (10000, 0.99990)]:
            with self.subTest(multiple=multiple):
                self.assertAlmostEqual(capture_fraction(multiple * 500, 500), expected, places=5)

    def test_capture_is_independent_of_the_quote_reserve(self):
        # The same sale ratio drains a 0.01 SOL corpse and a 37 SOL one
        # equally. This is why the absolute pot size is irrelevant to sizing.
        small = quote_out(4, 10_000_002, 4000)
        large = quote_out(4, 37_810_000_011, 4000)
        self.assertAlmostEqual(small / 10_000_002, large / 37_810_000_011, places=6)

    def test_capture_depends_only_on_the_ratio_not_the_scale(self):
        self.assertAlmostEqual(capture_fraction(1_000, 1), capture_fraction(1_000_000, 1_000), places=9)

    def test_empty_reserve_is_captured_entirely(self):
        self.assertEqual(capture_fraction(1, 0), 1.0)

    def test_selling_nothing_captures_nothing(self):
        # Ordering matters: 0 tokens into a 0 reserve must not read as 100%.
        self.assertEqual(capture_fraction(0, 0), 0.0)
        self.assertEqual(capture_fraction(0, 5_000), 0.0)
        self.assertEqual(quote_out(0, 10 * SOL, 0), 0)

    def test_negative_sale_is_treated_as_no_sale(self):
        self.assertEqual(capture_fraction(-5, 100), 0.0)

    def test_fee_is_charged_on_the_output(self):
        gross = quote_out(1_000, 10 * SOL, 1_000, fee_bps=0)
        net = quote_out(1_000, 10 * SOL, 1_000, fee_bps=25)
        self.assertAlmostEqual(net / gross, 0.9975, places=6)

    def test_quote_out_never_exceeds_the_reserve(self):
        self.assertLess(quote_out(1, 5 * SOL, 10**18), 5 * SOL)

    def test_tokens_out_is_the_mirror_of_quote_out(self):
        base, quote = 10**12, 50 * SOL
        bought = tokens_out(base, quote, SOL)
        # Selling straight back into the moved pool returns the deposit, less
        # rounding: a round trip is neutral before fees, so the loss in a dead
        # pool is not slippage -- it is someone else taking the reserve.
        back = quote_out(base - bought, quote + SOL, bought)
        self.assertAlmostEqual(back / SOL, 1.0, places=6)


class SaleSizingTests(unittest.TestCase):
    def test_sale_size_inverts_the_capture_identity(self):
        for reserve in (1, 4, 22, 477, 8_172, 380_327):
            with self.subTest(reserve=reserve):
                size = sale_size_for_capture(reserve, 0.999)
                self.assertGreaterEqual(capture_fraction(size, reserve), 0.999)

    def test_a_four_unit_reserve_needs_only_four_thousand_units(self):
        # The USWR pool held 4 raw units. A 0.00002 SOL dust buy had already
        # bought 8,415,597 -- three orders of magnitude more than needed.
        self.assertLessEqual(sale_size_for_capture(4, 0.999), 5_000)

    def test_rejects_an_unreachable_target(self):
        with self.assertRaises(ValueError):
            sale_size_for_capture(100, 1.0)


class ClaimCostTests(unittest.TestCase):
    """What buying a 99%-capture claim costs, at current pool state."""

    def test_a_dead_pool_is_claimable_for_a_fraction_of_a_cent(self):
        cost = claim_cost_for_capture(1_880_518, 100 * SOL)  # TripleP post-rug
        self.assertLess(cost, SOL // 100)

    def test_a_live_pool_costs_thousands_of_sol(self):
        cost = claim_cost_for_capture(345_500_000_000, SOL // 10)  # USWR pre-rug
        self.assertGreater(cost, 1_000 * SOL)

    def test_the_gap_between_the_two_is_the_vulnerability(self):
        # Threat model 2.3 measures ~180,000x more tokens per lamport once a
        # pool is depleted. The cost ratio is of that order or larger.
        dead = claim_cost_for_capture(1_880_518, SOL)
        live = claim_cost_for_capture(345_500_000_000, SOL)
        self.assertGreater(live / dead, 100_000)

    def test_no_deposit_means_nothing_to_capture(self):
        self.assertEqual(claim_cost_for_capture(1_000_000, 0), -1)

    def test_an_empty_pool_is_claimable_for_one_lamport(self):
        self.assertEqual(claim_cost_for_capture(0, SOL), 1)


class VerdictTests(unittest.TestCase):
    """The verdict matrix, anchored on measured pool states."""

    def test_depleted_token_reserve_is_unsafe(self):
        score = score_reserves(4, 37_810_000_011, SOL // 10)
        self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertIn("S1", score.signals)

    def test_depleted_quote_reserve_is_unsafe(self):
        # A pool holding 0.00034 SOL cannot quote anybody honestly.
        score = score_reserves(159_678_267, 340_672, SOL // 10)
        self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertIn("S2", score.signals)

    def test_the_modal_opportunity_is_unsafe(self):
        # Slot 442634543: 22 raw units against 0.010000002 SOL, fought over
        # by four bots across three operators.
        score = score_reserves(22, 10_000_002, SOL // 10)
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_healthy_pools_are_safe(self):
        for base, quote in [
            (145_816_924_891_423, 345_500_000_000),  # USWR before the rug
            (968_324_657_665_297, 87_830_476_208),   # a pool kiwi seeded
            (973_531_464_420_738, 87_306_624_865),
            (945_319_266_170_413, 89_932_390_529),
        ]:
            with self.subTest(quote=quote):
                score = score_reserves(base, quote, SOL // 10)
                self.assertIs(score.verdict, Verdict.SAFE)
                self.assertEqual(score.signals, ())

    def test_a_thin_but_live_pool_gets_caution_not_unsafe(self):
        # 0.15 SOL of reserve against a 0.2 SOL deposit. Above the depletion
        # floor, with a full token reserve behind it -- a bad fill, not a
        # trap. Keeping the two apart is the point of having three verdicts.
        score = score_reserves(900_000_000_000_000, 150_000_000, 200_000_000)
        self.assertIs(score.verdict, Verdict.CAUTION)

    def test_uninitialised_pool_is_unsafe(self):
        score = score_reserves(10**14, 100 * SOL, SOL, initialised=False)
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_overhang_escalates_caution_to_unsafe(self):
        base, quote, deposit = 900_000_000_000_000, 150_000_000, 200_000_000
        clean = score_reserves(base, quote, deposit)
        self.assertIs(clean.verdict, Verdict.CAUTION)
        loaded = score_reserves(
            base,
            quote,
            deposit,
            overhang=Overhang("mint", 10**14, 112, overhang_ratio=5_000.0),
        )
        self.assertIs(loaded.verdict, Verdict.UNSAFE)
        self.assertIn("S3", loaded.signals)

    def test_overhang_alone_never_makes_a_healthy_pool_unsafe(self):
        # S3 raises severity; it does not manufacture a verdict. A live pool
        # with a large holder is a live pool: the holder's sale moves the
        # price, but the deposit's tokens are still backed by a real reserve
        # and sell back for very nearly what they cost.
        score = score_reserves(
            145_816_924_891_423,
            345_500_000_000,
            SOL // 10,
            overhang=Overhang("mint", 10**12, 400, overhang_ratio=1_000.0),
        )
        self.assertIs(score.verdict, Verdict.SAFE)

    def test_overhang_supplies_the_adversary_when_present(self):
        # USWR: 4 raw units of reserve against the 8,415,597 units the
        # operator's 0.00002 SOL dust buy acquired. Total loss.
        score = score_reserves(
            4, 37_810_000_011, SOL // 10,
            overhang=Overhang("mint", 8_415_597, 112, overhang_ratio=2_103_899.0),
        )
        self.assertAlmostEqual(score.extractable_fraction_of_deposit, 1.0, places=4)
        self.assertIn("largest observed external holder", score.adversary_model)
        self.assertGreater(score.capture_fraction_by_largest_holder, 0.999)

    def test_a_real_claim_on_a_real_corpse_reproduces_the_observed_loss(self):
        # TripleP after the rug: 0.00188 SOL of reserve, against the
        # 2,361,606 units m3mx's post-rug dust buy acquired.
        score = score_reserves(
            225_089_172, 1_880_518, SOL // 10,
            overhang=Overhang("mint", 2_361_606, 112, overhang_ratio=0.0105),
        )
        self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertAlmostEqual(score.extractable_fraction_of_deposit, 0.369, places=2)

    def test_extractability_states_its_adversary_when_none_is_observed(self):
        score = score_reserves(145_816_924_891_423, 345_500_000_000, SOL // 10)
        self.assertIn("dust ticket", score.adversary_model)
        self.assertLess(score.extractable_fraction_of_deposit, 0.01)

    def test_degenerate_adversary_is_named_rather_than_asserted(self):
        # 4 raw units against 37.8 SOL. A dust ticket buys nothing at this
        # state, so there is no adversary to model and the string says so
        # rather than implying an absence of risk.
        score = score_reserves(4, 37_810_000_011, SOL // 10)
        self.assertIn("not measurable", score.adversary_model)
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_a_deposit_into_a_four_unit_reserve_buys_literally_nothing(self):
        # Against 4 raw units, 0.1 SOL rounds down to zero tokens out. The
        # loss is total before any operator is involved -- which is the
        # threat model's point that this is a bad trade even unobserved.
        score = score_reserves(4, 37_810_000_011, SOL // 10)
        self.assertEqual(score.tokens_out, 0)
        self.assertEqual(score.extractable_fraction_of_deposit, 1.0)

    def test_thresholds_are_configurable(self):
        # Every signal switched off scores a corpse safe. Integrators who
        # want a different policy get one; the defaults are not baked in.
        loose = Thresholds(s1_base_reserve_raw=0, s1_base_reserve_caution_raw=0,
                           s2_quote_reserve_lamports=0, caution_price_impact=1.1,
                           unsafe_extractable_fraction=1.1, caution_extractable_fraction=1.1,
                           unsafe_claim_cost_ratio=0.0, caution_claim_cost_ratio=0.0)
        score = score_reserves(4_000, 30_000, SOL // 10, thresholds=loose)
        self.assertIs(score.verdict, Verdict.SAFE)

    def test_a_deposit_that_buys_nothing_is_unsafe_at_any_threshold(self):
        # Not a policy call: receiving zero tokens for real SOL is a total
        # loss by arithmetic, so no threshold configuration excuses it.
        loose = Thresholds(s1_base_reserve_raw=0, s1_base_reserve_caution_raw=0,
                           s2_quote_reserve_lamports=0, caution_price_impact=1.1,
                           unsafe_extractable_fraction=1.1, caution_extractable_fraction=1.1,
                           unsafe_claim_cost_ratio=0.0, caution_claim_cost_ratio=0.0)
        score = score_reserves(4, 37_810_000_011, SOL // 10, thresholds=loose)
        self.assertEqual(score.tokens_out, 0)
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_zero_deposit_is_scored_without_dividing_by_zero(self):
        score = score_reserves(4, 10_000_000, 0)
        self.assertEqual(score.tokens_out, 0)
        self.assertIs(score.verdict, Verdict.UNSAFE)

    def test_negative_deposit_is_rejected(self):
        with self.assertRaises(ValueError):
            score_reserves(10**14, 10 * SOL, -1)

    def test_rationale_is_populated_for_every_non_safe_verdict(self):
        for base, quote in [(4, 37_810_000_011), (159_678_267, 340_672), (10**14, SOL // 20)]:
            score = score_reserves(base, quote, SOL // 10)
            self.assertNotEqual(score.verdict, Verdict.SAFE)
            self.assertTrue(score.rationale)
            self.assertNotEqual(score.human_reason, "No depletion signals fired.")


class VerdictOrderingTests(unittest.TestCase):
    def test_worst_escalates(self):
        self.assertIs(Verdict.worst(Verdict.SAFE, Verdict.CAUTION), Verdict.CAUTION)
        self.assertIs(Verdict.worst(Verdict.CAUTION, Verdict.UNSAFE), Verdict.UNSAFE)
        self.assertIs(Verdict.worst(Verdict.SAFE, Verdict.UNKNOWN), Verdict.UNKNOWN)
        self.assertIs(Verdict.worst(Verdict.UNKNOWN, Verdict.CAUTION), Verdict.CAUTION)


if __name__ == "__main__":
    unittest.main()


class SimulationTests(unittest.TestCase):
    """The exit leg -- what separates a trap from a bad fill."""

    def test_a_depositor_can_leave_a_live_pool_almost_whole(self):
        sim = simulate_capture(145_816_924_891_423, 345 * SOL, SOL // 10, 10**12)
        self.assertLess(sim.loss_fraction, 0.02)

    def test_a_depositor_cannot_leave_a_dead_one(self):
        # 4 raw units of reserve; the holder's 8.4M units take everything.
        sim = simulate_capture(4, 30_000, 37 * SOL, 8_415_597)
        self.assertGreater(sim.loss_fraction, 0.999)

    def test_the_holder_takes_what_the_depositor_loses(self):
        sim = simulate_capture(225_089_172, 1_880_518, SOL // 10, 2_361_606)
        self.assertAlmostEqual(
            sim.taken_by_holder / SOL,
            (SOL // 10 - sim.recovered_by_victim) / SOL,
            places=2,
        )

    def test_no_claim_means_no_loss_beyond_rounding(self):
        sim = simulate_capture(10**14, 100 * SOL, SOL, 0)
        self.assertLess(sim.loss_fraction, 1e-6)

    def test_loss_is_never_negative_or_above_one(self):
        for base, quote, claim in [(1, 1, 10**18), (10**15, 10**12, 1), (4, 30_000, 10**9)]:
            with self.subTest(base=base, claim=claim):
                sim = simulate_capture(base, quote, SOL // 10, claim)
                self.assertGreaterEqual(sim.loss_fraction, 0.0)
                self.assertLessEqual(sim.loss_fraction, 1.0)


class FieldSemanticsTests(unittest.TestCase):
    def test_price_impact_uses_the_bounded_convention(self):
        # 1 - mid/execution = q_in / (q + q_in). Bounded by 1, which is what
        # DEX front ends show; the unbounded q_in/q convention is not used.
        score = score_reserves(10**15, 10 * SOL, 10 * SOL)
        self.assertAlmostEqual(score.price_impact, 0.5, places=6)
        self.assertLessEqual(score.price_impact, 1.0)

    def test_an_uninitialised_pool_is_unsafe_without_a_depletion_signal(self):
        # Not tradable is a different fact from depleted, and mislabelling it
        # would corrupt the signal counts the telemetry reports.
        score = score_reserves(10**14, 100 * SOL, SOL, initialised=False)
        self.assertIs(score.verdict, Verdict.UNSAFE)
        self.assertEqual(score.signals, ())
        self.assertIn("not initialised", score.human_reason)

    def test_the_headline_capture_figure_is_a_property_of_the_sale_ratio(self):
        # 1000x captures 99.90% of any pool, live or dead. Documented here so
        # nobody later mistakes it for a risk measure.
        live = score_reserves(145_816_924_891_423, 345 * SOL, SOL // 10)
        dead = score_reserves(4, 30_000, SOL // 10)
        self.assertAlmostEqual(live.capture_fraction_at_1000x, 0.999, places=3)
        self.assertAlmostEqual(dead.capture_fraction_at_1000x, 0.999, places=3)
        self.assertIsNot(live.verdict, dead.verdict)
