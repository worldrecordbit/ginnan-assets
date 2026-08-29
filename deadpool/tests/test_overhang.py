"""Claim enumeration and the bucket-partitioned operator census."""

from __future__ import annotations

import unittest

from deadpool.constants import RENT_TOKEN_2022_ACCOUNT, TOKEN_2022_PROGRAM
from deadpool.models import PoolSnapshot
from deadpool.overhang import BUCKET_COUNT, ClaimOverhangService
from deadpool.rpc import RpcError

from .support import MockChain, pubkey


def _snapshot(chain: MockChain, mint: str, base_vault: str, quote_vault: str, reserve: int):
    return PoolSnapshot(
        pool=pubkey("pool"),
        program="pumpswap",
        base_mint=mint,
        quote_mint=pubkey("wsol"),
        base_vault=base_vault,
        quote_vault=quote_vault,
        base_reserve=reserve,
        quote_reserve=10_000_002,
    )


class OverhangTests(unittest.TestCase):
    def setUp(self):
        self.chain = MockChain()
        self.mint = self.chain.add_mint("MINT", 6)
        self.pool = pubkey("pool")
        self.base_vault = self.chain.add_token_account("vault", self.mint, self.pool, 22)
        self.quote_vault = self.chain.add_token_account(
            "qvault", pubkey("wsol"), self.pool, 10_000_002
        )
        self.service = ClaimOverhangService(self.chain.client())

    def test_the_pool_vault_is_excluded_from_the_holder_set(self):
        # Counting the vault as a holder would report an overhang ratio of
        # 1.0 on every pool in existence.
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 22)
        result = self.service.overhang(self.mint, snapshot)
        self.assertEqual(result.holder_count, 0)
        self.assertEqual(result.largest_external_balance, 0)

    def test_largest_external_balance_and_ratio(self):
        self.chain.add_token_account("h1", self.mint, pubkey("op1"), 8_415_597)
        self.chain.add_token_account("h2", self.mint, pubkey("op2"), 2_361_606)
        self.chain.add_token_account("h3", self.mint, pubkey("op3"), 1)
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 22)
        result = self.service.overhang(self.mint, snapshot)
        self.assertEqual(result.holder_count, 3)
        self.assertEqual(result.largest_external_balance, 8_415_597)
        self.assertAlmostEqual(result.overhang_ratio, 8_415_597 / 22)

    def test_zero_balance_accounts_are_not_holders(self):
        # Rent-recycled positions: the account still exists, the claim does
        # not. Counting them would inflate every holder count.
        self.chain.add_token_account("empty", self.mint, pubkey("op"), 0)
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 22)
        self.assertEqual(self.service.overhang(self.mint, snapshot).holder_count, 0)

    def test_an_empty_reserve_gives_an_unbounded_ratio_not_a_crash(self):
        self.chain.add_token_account("h1", self.mint, pubkey("op1"), 5)
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 0)
        self.assertIsNone(self.service.overhang(self.mint, snapshot).overhang_ratio)

    def test_both_token_programs_are_enumerated(self):
        self.chain.add_token_account(
            "t22", self.mint, pubkey("op22"), 999, program=TOKEN_2022_PROGRAM
        )
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 22)
        self.assertEqual(self.service.overhang(self.mint, snapshot).holder_count, 1)

    def test_the_cache_is_reused_but_the_ratio_is_recomputed(self):
        # The balances are cached; the ratio depends on a reserve that moves,
        # so it must be recomputed against the snapshot every time.
        self.chain.add_token_account("h1", self.mint, pubkey("op1"), 1_000)
        first = self.service.overhang(
            self.mint, _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 100)
        )
        calls = len(self.chain.calls)
        second = self.service.overhang(
            self.mint, _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 10)
        )
        self.assertEqual(len(self.chain.calls), calls, "second lookup should be cached")
        self.assertAlmostEqual(first.overhang_ratio, 10.0)
        self.assertAlmostEqual(second.overhang_ratio, 100.0)

    def test_an_rpc_failure_propagates_rather_than_returning_zero(self):
        # A silent zero here would read as "no claim overhang", which is the
        # most dangerous possible wrong answer. The advisory layer catches
        # this and marks the response degraded instead.
        self.chain.fail_methods.add("getProgramAccounts")
        snapshot = _snapshot(self.chain, self.mint, self.base_vault, self.quote_vault, 22)
        with self.assertRaises(RpcError):
            self.service.overhang(self.mint, snapshot)


class CensusTests(unittest.TestCase):
    """Signal S6, the operator classifier."""

    def setUp(self):
        self.chain = MockChain()
        self.wallet = pubkey("operator")
        self.service = ClaimOverhangService(self.chain.client())

    def _seed(self, counts: dict[int, int]) -> None:
        """Give the wallet ``n`` Token-2022 accounts in each named bucket.

        The partition keys on the mint's *second* byte, so the fixture builds
        mints whose byte 1 is the bucket they belong to. Byte 0 is deliberately
        varied too: a partition that accidentally keyed on byte 0 would still
        pass a fixture that held it constant.
        """
        from deadpool.base58 import b58decode
        from deadpool.spl import encode_token_account

        owner = b58decode(self.wallet)
        for bucket, count in counts.items():
            for i in range(count):
                mint = bytes([(i * 7) % 251, bucket]) + i.to_bytes(30, "big")
                self.chain.add(
                    pubkey(f"acct/{bucket}/{i}"),
                    TOKEN_2022_PROGRAM,
                    encode_token_account(mint, owner, 1),
                )

    def test_a_uniform_book_scales_by_the_bucket_count(self):
        self._seed({0: 703, 128: 703})
        census = self.service.census(self.wallet, buckets=2)
        self.assertEqual(census.token_2022_accounts, 703 * BUCKET_COUNT)
        self.assertEqual(census.estimated_claims, 703 * BUCKET_COUNT)
        self.assertFalse(census.exact)

    def test_rent_is_priced_per_token_program(self):
        self._seed({0: 10, 128: 10})
        census = self.service.census(self.wallet, buckets=2)
        self.assertEqual(
            census.rent_locked_lamports, 10 * BUCKET_COUNT * RENT_TOKEN_2022_ACCOUNT
        )
        self.assertAlmostEqual(census.rent_locked_sol, census.rent_locked_lamports / 1e9)

    def test_dispersion_reports_agreement_between_buckets(self):
        # Two buckets agreeing closely is what validates the uniformity the
        # extrapolation assumes -- the measurement that would falsify it is
        # reported next to the estimate rather than left implicit.
        self._seed({0: 703, 128: 701})
        census = self.service.census(self.wallet, buckets=2)
        self.assertLess(census.bucket_dispersion, 0.01)

    def test_dispersion_exposes_a_non_uniform_book(self):
        self._seed({0: 900, 128: 100})
        census = self.service.census(self.wallet, buckets=2)
        self.assertGreater(census.bucket_dispersion, 1.0)

    def test_a_single_bucket_cannot_report_dispersion(self):
        self._seed({0: 5})
        census = self.service.census(self.wallet, buckets=1)
        self.assertIsNone(census.bucket_dispersion)

    def test_sampled_buckets_are_spread_across_the_space(self):
        # Taking buckets 0..n instead would bias the estimate if mints
        # cluster; these must be evenly spaced.
        self._seed({0: 4, 64: 4, 128: 4, 192: 4})
        census = self.service.census(self.wallet, buckets=4)
        self.assertEqual(census.token_2022_accounts, 4 * BUCKET_COUNT)
        self.assertEqual(census.bucket_dispersion, 0.0)

    def test_an_empty_wallet_reports_nothing_rather_than_failing(self):
        census = self.service.census(pubkey("stranger"), buckets=2)
        self.assertEqual(census.estimated_claims, 0)
        self.assertEqual(census.rent_locked_lamports, 0)

    def test_a_full_sweep_is_exact(self):
        self._seed({3: 2, 200: 5})
        census = self.service.census(self.wallet, buckets=BUCKET_COUNT)
        self.assertTrue(census.exact)
        self.assertEqual(census.estimated_claims, 7)

    def test_bucket_count_is_validated(self):
        for bad in (0, -1, 257):
            with self.subTest(buckets=bad), self.assertRaises(ValueError):
                self.service.census(self.wallet, buckets=bad)

    def test_other_wallets_accounts_are_not_counted(self):
        self._seed({0: 3, 128: 3})
        from deadpool.base58 import b58decode
        from deadpool.spl import encode_token_account

        for i in range(50):
            mint = bytes([1, 0]) + i.to_bytes(30, "big")
            self.chain.add(
                pubkey(f"other/{i}"),
                TOKEN_2022_PROGRAM,
                encode_token_account(mint, b58decode(pubkey("someone-else")), 1),
            )
        census = self.service.census(self.wallet, buckets=2)
        self.assertEqual(census.token_2022_accounts, 3 * BUCKET_COUNT)


if __name__ == "__main__":
    unittest.main()
