"""Claim-Overhang Service -- component 3 of the detection architecture.

Answers "how much claim pressure is outstanding against this mint?" -- signals
S3 and S5 -- so the scorer's verdict can be raised from *bad trade* to
*actively targeted*.

Two measurements live here:

``overhang(mint, snapshot)``
    Enumerates the mint's token accounts, excludes the pool's own vault, and
    reports the largest external balance and the holder count. Cheap: one
    ``getProgramAccounts`` per token program, with a ``dataSlice`` that fetches
    only the 40 bytes covering owner and amount.

``census(wallet)``
    Signal S6, the operator classifier. ``getTokenAccountsByOwner`` has no
    cursor and dies on wallets holding six figures of accounts, so the working
    substitute from analysis section 5e is to partition: a memcmp on the token
    account's owner field plus a memcmp on the *second* byte of the mint gives
    256 disjoint, uniformly-sized buckets, and sampling a few and scaling by
    256 estimates the book. Offset 0 cannot be used for the partition -- nodes
    special-case it as the mint field and reject anything that is not 32 bytes
    -- which is why the partition sits at offset 1.

This service is explicitly **not on the pre-trade hot path**. It is slower
than S1/S2 and its absence must degrade gracefully: a verdict computed from
pool state alone is valid, just less severe.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .constants import (
    RENT_SPL_TOKEN_ACCOUNT,
    RENT_TOKEN_2022_ACCOUNT,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    TOKEN_PROGRAMS,
)
from .models import Overhang, PoolSnapshot
from .rpc import JsonRpcClient, data_size, encode_u8_filter, memcmp
from .spl import OWNER_OFFSET, TOKEN_ACCOUNT_LEN

#: The bucket partition: one byte, so 256 disjoint buckets.
BUCKET_COUNT = 256
BUCKET_OFFSET = 1

#: dataSlice covering owner (32 bytes) and amount (8 bytes) only.
_OWNER_AMOUNT_SLICE = (OWNER_OFFSET, 40)


@dataclass(frozen=True)
class Census:
    """Output of the S6 operator classifier."""

    wallet: str
    spl_token_accounts: int
    token_2022_accounts: int
    estimated_claims: int
    rent_locked_lamports: int
    buckets_sampled: int
    #: Spread between sampled buckets, as a fraction of the mean. The
    #: uniformity assumption is what the whole extrapolation rests on, so the
    #: measurement that would falsify it is reported alongside the estimate.
    bucket_dispersion: float | None
    exact: bool = False

    @property
    def rent_locked_sol(self) -> float:
        return self.rent_locked_lamports / 1_000_000_000


class ClaimOverhangService:
    """Rate-limit-aware, cached. Never required for a verdict."""

    def __init__(self, client: JsonRpcClient, *, cache_size: int = 4096) -> None:
        self.client = client
        self._cache: dict[str, Overhang] = {}
        self._cache_size = cache_size

    def overhang(
        self, mint: str, snapshot: PoolSnapshot | None = None, *, use_cache: bool = True
    ) -> Overhang:
        """Largest external balance and holder count for ``mint``."""
        if use_cache and mint in self._cache:
            cached = self._cache[mint]
            return self._rescore(cached, snapshot)

        exclude = {snapshot.base_vault, snapshot.quote_vault} if snapshot else set()
        largest = 0
        holders = 0
        slot = 0
        for program in TOKEN_PROGRAMS:
            accounts = self.client.get_program_accounts(
                program,
                filters=[data_size(TOKEN_ACCOUNT_LEN), memcmp(0, mint)],
                data_slice=_OWNER_AMOUNT_SLICE,
            )
            for account in accounts:
                slot = max(slot, account.slot)
                if account.pubkey in exclude:
                    continue
                if len(account.data) < 40:
                    continue
                amount = int.from_bytes(account.data[32:40], "little")
                if amount <= 0:
                    continue
                holders += 1
                largest = max(largest, amount)

        result = Overhang(
            mint=mint,
            largest_external_balance=largest,
            holder_count=holders,
            overhang_ratio=None,
            estimated=False,
            slot=slot,
        )
        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[mint] = result
        return self._rescore(result, snapshot)

    @staticmethod
    def _rescore(overhang: Overhang, snapshot: PoolSnapshot | None) -> Overhang:
        """Attach the ratio, which depends on the pool's current reserve."""
        if snapshot is None:
            return overhang
        residual = snapshot.base_reserve
        ratio = (
            None
            if residual <= 0
            else overhang.largest_external_balance / residual
        )
        return Overhang(
            mint=overhang.mint,
            largest_external_balance=overhang.largest_external_balance,
            holder_count=overhang.holder_count,
            overhang_ratio=ratio,
            estimated=overhang.estimated,
            slot=overhang.slot,
        )

    # --- S6 -------------------------------------------------------------

    def census(self, wallet: str, *, buckets: int = 2) -> Census:
        """Estimate a wallet's claim book by bucket-partitioned sampling.

        ``buckets`` of 1 gives an estimate with no way to check it. Two or
        more also yields the dispersion between them, which is the direct
        test of the uniformity the extrapolation assumes -- two independent
        buckets agreeing to within a fraction of a percent is what validated
        the method originally.
        """
        if not 1 <= buckets <= BUCKET_COUNT:
            raise ValueError(f"buckets must be in 1..{BUCKET_COUNT}")
        if buckets == BUCKET_COUNT:
            return self._exact_census(wallet)

        # Evenly spread the sampled buckets across the space rather than
        # taking 0..n, so a clustered mint distribution cannot bias them.
        step = BUCKET_COUNT // buckets
        chosen = [i * step for i in range(buckets)]

        per_program: dict[str, list[int]] = {}
        for program in TOKEN_PROGRAMS:
            counts = []
            for bucket in chosen:
                counts.append(len(self._bucket(program, wallet, bucket)))
            per_program[program] = counts

        spl_counts = per_program[TOKEN_PROGRAM]
        t22_counts = per_program[TOKEN_2022_PROGRAM]
        spl = round(statistics.fmean(spl_counts) * BUCKET_COUNT) if spl_counts else 0
        t22 = round(statistics.fmean(t22_counts) * BUCKET_COUNT) if t22_counts else 0

        all_counts = [a + b for a, b in zip(spl_counts, t22_counts)]
        dispersion = _dispersion(all_counts)

        return Census(
            wallet=wallet,
            spl_token_accounts=spl,
            token_2022_accounts=t22,
            estimated_claims=spl + t22,
            rent_locked_lamports=spl * RENT_SPL_TOKEN_ACCOUNT + t22 * RENT_TOKEN_2022_ACCOUNT,
            buckets_sampled=buckets,
            bucket_dispersion=dispersion,
            exact=False,
        )

    def _bucket(self, program: str, wallet: str, bucket: int) -> list:
        """One partition: accounts of ``program`` owned by ``wallet`` whose
        mint's second byte is ``bucket``.

        No ``dataSize`` filter: Token-2022 accounts vary in length once
        extensions are present, and filtering on 165 would silently drop the
        transfer-fee mints one operator was observed holding. Only the
        addresses matter here, so the response is sliced to nothing.
        """
        return self.client.get_program_accounts(
            program,
            filters=[
                memcmp(OWNER_OFFSET, wallet),
                encode_u8_filter(BUCKET_OFFSET, bucket),
            ],
            data_slice=(0, 0),
        )

    def _exact_census(self, wallet: str) -> Census:
        """Every bucket, no extrapolation. 512 calls -- for verification."""
        spl = sum(len(self._bucket(TOKEN_PROGRAM, wallet, b)) for b in range(BUCKET_COUNT))
        t22 = sum(len(self._bucket(TOKEN_2022_PROGRAM, wallet, b)) for b in range(BUCKET_COUNT))
        return Census(
            wallet=wallet,
            spl_token_accounts=spl,
            token_2022_accounts=t22,
            estimated_claims=spl + t22,
            rent_locked_lamports=spl * RENT_SPL_TOKEN_ACCOUNT + t22 * RENT_TOKEN_2022_ACCOUNT,
            buckets_sampled=BUCKET_COUNT,
            bucket_dispersion=0.0,
            exact=True,
        )


def _dispersion(counts: list[int]) -> float | None:
    if len(counts) < 2:
        return None
    mean = statistics.fmean(counts)
    if mean == 0:
        return 0.0
    return (max(counts) - min(counts)) / mean
