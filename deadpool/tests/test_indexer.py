"""Pool resolution, orientation, and the layers that catch a bad layout."""

from __future__ import annotations

import unittest

from deadpool.base58 import b58decode
from deadpool.constants import PUMPSWAP_PROGRAM, WSOL_MINT
from deadpool.indexer import (
    PoolResolutionError,
    PoolStateIndexer,
    decode_pumpswap_pool,
    scan_pubkey_candidates,
)
from deadpool.spl import encode_mint

from .support import MockChain, pubkey


class PumpSwapDecodingTests(unittest.TestCase):
    def test_resolves_reserves_and_decimals(self):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        chain.accounts[mint].data = encode_mint(6, supply=10**15)
        pool = chain.add_pumpswap_pool(
            "corpse", token_mint=mint, base_reserve=4, quote_reserve=37_810_000_011
        )
        snapshot = PoolStateIndexer(chain.client()).snapshot(pool)
        self.assertEqual(snapshot.program, "pumpswap")
        self.assertEqual(snapshot.base_reserve, 4)
        self.assertEqual(snapshot.quote_reserve, 37_810_000_011)
        self.assertEqual(snapshot.base_mint, mint)
        self.assertEqual(snapshot.quote_mint, WSOL_MINT)
        self.assertEqual(snapshot.base_decimals, 6)
        self.assertEqual(snapshot.base_supply, 10**15)
        self.assertTrue(snapshot.initialised)

    def test_a_short_account_is_not_decoded(self):
        self.assertIsNone(decode_pumpswap_pool(b"\x00" * 32))

    def test_identical_vault_fields_are_rejected(self):
        data = bytearray(243)
        data[139:171] = b58decode(WSOL_MINT)
        data[171:203] = b58decode(WSOL_MINT)
        self.assertIsNone(decode_pumpswap_pool(bytes(data)))


class OrientationTests(unittest.TestCase):
    """Threat model 3.4: the AMM's field order is not the economic order."""

    def _snapshot(self, flipped: bool):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool(
            "p", token_mint=mint, base_reserve=1_234, quote_reserve=99_000, flipped=flipped
        )
        return PoolStateIndexer(chain.client()).snapshot(pool), mint

    def test_orientation_comes_from_mint_identity_not_field_order(self):
        upright, mint = self._snapshot(flipped=False)
        flipped, mint_f = self._snapshot(flipped=True)
        # Both pools hold the same reserves. The economic assignment must be
        # identical even though the program's base_mint field differs.
        for snapshot in (upright, flipped):
            self.assertEqual(snapshot.quote_mint, WSOL_MINT)
            self.assertEqual(snapshot.base_reserve, 1_234)
            self.assertEqual(snapshot.quote_reserve, 99_000)
        self.assertEqual(upright.base_mint, mint)
        self.assertEqual(flipped.base_mint, mint_f)

    def test_the_trap_is_reported_when_present(self):
        upright, _ = self._snapshot(flipped=False)
        flipped, _ = self._snapshot(flipped=True)
        self.assertFalse(upright.orientation_flipped)
        self.assertTrue(flipped.orientation_flipped)

    def test_a_pool_with_no_sol_side_is_refused(self):
        # Better to refuse than to invent an orientation and score against
        # the wrong reserve.
        chain = MockChain()
        a, b = chain.add_mint("A", 6), chain.add_mint("B", 9)
        pool = pubkey("pool")
        chain.add_token_account("va", a, pool, 10)
        chain.add_token_account("vb", b, pool, 20)
        chain.add(pool, PUMPSWAP_PROGRAM, bytes(243))
        with self.assertRaises(PoolResolutionError):
            PoolStateIndexer(chain.client()).snapshot(pool)


class FallbackResolutionTests(unittest.TestCase):
    def test_meteora_resolves_with_no_layout_knowledge(self):
        # Vaults owned by the shared pool authority, at offsets this codebase
        # does not know. Only the validated scan can find them.
        chain = MockChain()
        mint = chain.add_mint("MET", 9)
        pool = chain.add_meteora_pool(
            "dead", token_mint=mint, base_reserve=159_678_267, quote_reserve=340_672
        )
        snapshot = PoolStateIndexer(chain.client()).snapshot(pool)
        self.assertEqual(snapshot.program, "meteora-damm2")
        self.assertEqual(snapshot.base_reserve, 159_678_267)
        self.assertEqual(snapshot.quote_reserve, 340_672)

    def test_a_wrong_pumpswap_layout_falls_through_instead_of_lying(self):
        # Shift the vault pointers so the layout hypothesis is wrong. The
        # decoder's claimed mints will not match the vaults' real mints, so
        # it must be rejected and resolution must continue -- the failure
        # mode to avoid is confidently reporting some other pool's reserves.
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool(
            "p", token_mint=mint, base_reserve=7_777, quote_reserve=5 * 10**9
        )
        data = bytearray(chain.accounts[pool].data)
        decoy = chain.add_token_account("decoy", mint, pubkey("someone-else"), 10**12)
        data[139:171] = b58decode(decoy)
        chain.accounts[pool].data = bytes(data)

        snapshot = PoolStateIndexer(chain.client()).snapshot(pool)
        # Resolved by the owner lookup instead, and the reserves are the
        # pool's own -- not the decoy's 1e12.
        self.assertEqual(snapshot.base_reserve, 7_777)

    def test_explicit_vaults_are_validated_before_use(self):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool(
            "p", token_mint=mint, base_reserve=42, quote_reserve=10**9
        )
        indexer = PoolStateIndexer(chain.client())
        stranger = chain.add_token_account("stranger", mint, pubkey("nobody"), 5)
        with self.assertRaises(PoolResolutionError):
            indexer.snapshot(pool, vaults=[stranger, WSOL_MINT])

    def test_a_missing_pool_is_an_explicit_failure(self):
        chain = MockChain()
        with self.assertRaises(PoolResolutionError):
            PoolStateIndexer(chain.client()).snapshot(pubkey("nothing-here"))


class ScanTests(unittest.TestCase):
    def test_an_embedded_key_is_found_at_its_aligned_offset(self):
        data = bytes(64) + b58decode(WSOL_MINT) + bytes(64)
        self.assertIn(WSOL_MINT, scan_pubkey_candidates(data))

    def test_all_zero_data_yields_no_candidates(self):
        # Struct padding is mostly zeroes, and the all-zero pubkey is the
        # system program -- never a vault. Emitting those would waste most of
        # the candidate budget the batch lookup has.
        self.assertEqual(scan_pubkey_candidates(bytes(512)), [])

    def test_partial_overlaps_are_candidates_too(self):
        # An aligned scan cannot know where a field starts, so windows that
        # straddle a key are offered as well. They are harmless: each is
        # fetched and discarded unless it is really a vault of this pool.
        data = bytes(64) + b58decode(WSOL_MINT) + bytes(64)
        self.assertGreater(len(scan_pubkey_candidates(data)), 1)

    def test_candidates_are_capped(self):
        data = bytes(range(256)) * 40
        self.assertLessEqual(len(scan_pubkey_candidates(data, limit=10)), 10)

    def test_every_candidate_is_a_valid_pubkey(self):
        data = bytes(range(256)) * 8
        for candidate in scan_pubkey_candidates(data):
            self.assertEqual(len(b58decode(candidate)), 32)


class CachingTests(unittest.TestCase):
    def test_vault_resolution_is_cached_but_reserves_are_not(self):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool(
            "p", token_mint=mint, base_reserve=1_000, quote_reserve=10**9
        )
        indexer = PoolStateIndexer(chain.client())
        first = indexer.snapshot(pool)
        calls_after_first = len(chain.calls)

        # Drain the pool behind the indexer's back.
        vault = first.base_vault
        data = bytearray(chain.accounts[vault].data)
        data[64:72] = (4).to_bytes(8, "little")
        chain.accounts[vault].data = bytes(data)

        second = indexer.snapshot(pool)
        self.assertEqual(second.base_reserve, 4, "a cached reserve would be the whole bug")
        self.assertLess(
            len(chain.calls) - calls_after_first,
            calls_after_first,
            "resolution should not be repeated",
        )

    def test_forget_clears_the_resolution(self):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool(
            "p", token_mint=mint, base_reserve=1, quote_reserve=1
        )
        indexer = PoolStateIndexer(chain.client())
        indexer.snapshot(pool)
        before = len(chain.calls)
        indexer.forget(pool)
        indexer.snapshot(pool)
        self.assertGreater(len(chain.calls) - before, 2)

    def test_two_indexers_do_not_share_a_cache(self):
        chain = MockChain()
        mint = chain.add_mint("TOKEN", 6)
        pool = chain.add_pumpswap_pool("p", token_mint=mint, base_reserve=5, quote_reserve=6)
        first = PoolStateIndexer(chain.client())
        first.snapshot(pool)
        second = PoolStateIndexer(chain.client())
        before = len(chain.calls)
        second.snapshot(pool)
        self.assertGreater(len(chain.calls) - before, 2)


if __name__ == "__main__":
    unittest.main()
