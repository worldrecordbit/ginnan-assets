"""Composition, degradation, caching and the fail-closed option."""

from __future__ import annotations

import unittest

from deadpool.advisory import AdvisoryService
from deadpool.constants import LAMPORTS_PER_SOL
from deadpool.indexer import PoolStateIndexer
from deadpool.models import Verdict
from deadpool.overhang import ClaimOverhangService
from deadpool.spl import encode_mint
from deadpool.telemetry import Telemetry

from .support import MockChain, pubkey

SOL = LAMPORTS_PER_SOL


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build(*, base_reserve=4, quote_reserve=37_810_000_011, with_overhang=True, **kwargs):
    chain = MockChain()
    mint = chain.add_mint("TOKEN", 6)
    chain.accounts[mint].data = encode_mint(6, supply=10**15)
    pool = chain.add_pumpswap_pool(
        "p", token_mint=mint, base_reserve=base_reserve, quote_reserve=quote_reserve
    )
    client = chain.client()
    service = AdvisoryService(
        PoolStateIndexer(client),
        overhang=ClaimOverhangService(client) if with_overhang else None,
        **kwargs,
    )
    return chain, pool, mint, service


class VerdictTests(unittest.TestCase):
    def test_a_corpse_is_unsafe_and_carries_its_reasoning(self):
        _, pool, _, service = build()
        advisory = service.advise(pool, SOL // 10)
        self.assertIs(advisory.verdict, Verdict.UNSAFE)
        self.assertIn("S1", advisory.score.signals)
        self.assertTrue(advisory.human_reason)

    def test_a_live_pool_is_safe(self):
        _, pool, _, service = build(
            base_reserve=145_816_924_891_423, quote_reserve=345_500_000_000
        )
        self.assertIs(service.advise(pool, SOL // 10).verdict, Verdict.SAFE)

    def test_every_response_carries_the_slot_it_was_computed_against(self):
        chain, pool, _, service = build()
        advisory = service.advise(pool, SOL // 10)
        self.assertEqual(advisory.snapshot_slot, chain.slot)
        self.assertEqual(advisory.to_dict()["snapshot_slot"], chain.slot)

    def test_the_deposit_size_changes_the_answer(self):
        # A pool with 5 SOL of reserve is fine for 0.1 SOL and not for 500.
        _, pool, _, service = build(
            base_reserve=900_000_000_000_000, quote_reserve=5 * SOL
        )
        self.assertIs(service.advise(pool, SOL // 10).verdict, Verdict.SAFE)
        self.assertIs(service.advise(pool, 500 * SOL).verdict, Verdict.UNSAFE)

    def test_advise_sol_matches_advise_lamports(self):
        _, pool, _, service = build()
        self.assertEqual(
            service.advise_sol(pool, 0.1).quote_in, service.advise(pool, SOL // 10).quote_in
        )


class OverhangIntegrationTests(unittest.TestCase):
    def test_holder_data_is_attached_when_available(self):
        chain, pool, mint, service = build()
        chain.add_token_account("holder", mint, pubkey("operator"), 8_415_597)
        advisory = service.advise(pool, SOL // 10)
        self.assertIsNotNone(advisory.overhang)
        self.assertEqual(advisory.overhang.largest_external_balance, 8_415_597)
        self.assertIn("S3", advisory.score.signals)
        self.assertFalse(advisory.degraded)

    def test_a_failing_overhang_service_degrades_rather_than_erroring(self):
        # Contract from section 4.3: the overhang service is off the hot path
        # and its absence must not cost the caller a verdict.
        chain, pool, _, service = build()
        chain.fail_methods.add("getProgramAccounts")
        advisory = service.advise(pool, SOL // 10)
        self.assertIs(advisory.verdict, Verdict.UNSAFE)
        self.assertTrue(advisory.degraded)
        self.assertTrue(any("overhang unavailable" in w for w in advisory.warnings))

    def test_no_overhang_service_configured_is_also_degraded(self):
        _, pool, _, service = build(with_overhang=False)
        advisory = service.advise(pool, SOL // 10)
        self.assertTrue(advisory.degraded)

    def test_overhang_can_be_skipped_per_request(self):
        chain, pool, _, service = build()
        service.advise(pool, SOL // 10, with_overhang=False)
        self.assertNotIn("getProgramAccounts", chain.calls)


class FailClosedTests(unittest.TestCase):
    def test_an_unreadable_pool_is_unknown_by_default(self):
        _, _, _, service = build()
        advisory = service.advise(pubkey("does-not-exist"), SOL // 10)
        self.assertIs(advisory.verdict, Verdict.UNKNOWN)
        self.assertTrue(advisory.degraded)

    def test_an_unreadable_pool_is_caution_when_failing_closed(self):
        _, _, _, service = build(fail_closed=True)
        advisory = service.advise(pubkey("does-not-exist"), SOL // 10)
        self.assertIs(advisory.verdict, Verdict.CAUTION)

    def test_fail_closed_will_not_return_safe_on_incomplete_data(self):
        _, pool, _, service = build(
            base_reserve=145_816_924_891_423,
            quote_reserve=345_500_000_000,
            with_overhang=False,
            fail_closed=True,
        )
        advisory = service.advise(pool, SOL // 10)
        self.assertIs(advisory.verdict, Verdict.CAUTION)
        self.assertTrue(any("fail-closed" in w for w in advisory.warnings))

    def test_fail_closed_never_softens_a_verdict(self):
        _, pool, _, service = build(with_overhang=False, fail_closed=True)
        self.assertIs(service.advise(pool, SOL // 10).verdict, Verdict.UNSAFE)

    def test_a_transport_failure_does_not_raise_through_the_api(self):
        chain, pool, _, service = build()
        chain.fail_methods.add("getMultipleAccounts")
        service.invalidate(pool)
        advisory = service.advise(pool, SOL // 10)
        self.assertIs(advisory.verdict, Verdict.UNKNOWN)
        self.assertIn("Pool state unavailable", advisory.human_reason)


class CachingTests(unittest.TestCase):
    def test_repeat_requests_inside_the_ttl_reuse_the_snapshot(self):
        clock = _Clock()
        chain, pool, _, service = build(snapshot_ttl_s=2.0, clock=clock)
        service.advise(pool, SOL // 10, with_overhang=False)
        calls = len(chain.calls)
        service.advise(pool, SOL // 10, with_overhang=False)
        self.assertEqual(len(chain.calls), calls)

    def test_the_snapshot_is_refetched_once_the_ttl_expires(self):
        # The TTL absorbs bursts; it must never let a stale reserve through,
        # because a stale reserve is the failure this system exists to catch.
        clock = _Clock()
        chain, pool, _, service = build(snapshot_ttl_s=2.0, clock=clock)
        service.advise(pool, SOL // 10, with_overhang=False)
        calls = len(chain.calls)
        clock.now += 5.0
        service.advise(pool, SOL // 10, with_overhang=False)
        self.assertGreater(len(chain.calls), calls)

    def test_invalidate_forces_a_refetch(self):
        clock = _Clock()
        chain, pool, _, service = build(snapshot_ttl_s=60.0, clock=clock)
        service.advise(pool, SOL // 10, with_overhang=False)
        calls = len(chain.calls)
        service.invalidate(pool)
        service.advise(pool, SOL // 10, with_overhang=False)
        self.assertGreater(len(chain.calls), calls)


class TelemetryIntegrationTests(unittest.TestCase):
    def test_verdicts_and_signals_are_recorded(self):
        telemetry = Telemetry()
        _, pool, _, service = build(telemetry=telemetry)
        service.advise(pool, SOL // 10)
        metrics = telemetry.snapshot_metrics()
        self.assertEqual(metrics["verdicts"]["unsafe"], 1)
        self.assertIn("S1", metrics["signals"])
        self.assertEqual(len(metrics["recent_unsafe"]), 1)

    def test_errors_are_counted(self):
        telemetry = Telemetry()
        _, _, _, service = build(telemetry=telemetry)
        service.advise(pubkey("missing"), SOL // 10)
        self.assertTrue(telemetry.snapshot_metrics()["errors"])

    def test_telemetry_cannot_change_a_verdict(self):
        # Section 4.5: strictly read-only, off the hot path. The recorder is
        # handed finished advisories and has no way to hand anything back.
        telemetry = Telemetry()
        _, pool, _, service = build(telemetry=telemetry)
        first = service.advise(pool, SOL // 10)
        for _ in range(5):
            service.advise(pool, SOL // 10)
        self.assertIs(service.advise(pool, SOL // 10).verdict, first.verdict)


if __name__ == "__main__":
    unittest.main()
