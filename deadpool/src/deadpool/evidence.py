"""Loaders for the forensic record the detector is validated against.

The TSVs in ``evidence/`` are the reconstructed transaction data from the
companion analysis: 82 transactions for one operator with the pool's reserves
before and after each one, three large captures decoded leg by leg, a hit-rate
sample pulled from a durable-nonce account, and dust buys from two other
operators. Column definitions are in ``evidence/schema.txt``.

They ship inside the package rather than beside the tests because they are not
only fixtures -- ``deadpool replay`` uses them to demonstrate, offline, that
the capture identity in :mod:`deadpool.scorer` reproduces what actually
happened on-chain.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

EVIDENCE_DIR = Path(__file__).parent / "evidence"

M3MX_TRANSACTIONS = EVIDENCE_DIR / "m3mx-transactions.tsv"
BIG_WINS = EVIDENCE_DIR / "big-wins.tsv"
HARVEST_ATTEMPTS = EVIDENCE_DIR / "harvest-attempts.tsv"
OTHER_WALLETS = EVIDENCE_DIR / "other-wallets-samples.tsv"


@dataclass(frozen=True)
class Swap:
    """One reconstructed transaction, with the pool's reserves either side."""

    sig: str
    mint: str
    ts: int
    side: str
    fee: int
    native_delta: int
    wsol_delta: int
    token_delta: int
    decimals: int
    pool: str
    base_pre: int
    base_post: int
    quote_pre: int
    quote_post: int
    program: str
    nonce: bool
    rent_delta: int
    tip: int

    @property
    def is_sell(self) -> bool:
        """Direction from the wallet's own token delta, never from a log.

        In pools where wrapped SOL is the program's base mint the instruction
        name is the opposite of the economic direction -- threat model 3.4.
        """
        return self.token_delta < 0

    @property
    def sale_size(self) -> int:
        return abs(self.token_delta)

    @property
    def observed_capture(self) -> float | None:
        """Share of the pool's quote reserve this transaction removed."""
        if self.quote_pre <= 0 or self.quote_post > self.quote_pre:
            return None
        return (self.quote_pre - self.quote_post) / self.quote_pre

    @property
    def net_lamports(self) -> int:
        """Native delta plus WSOL delta: swap, fee, tip and rent in one."""
        return self.native_delta + self.wsol_delta


def load_swaps(path: Path = M3MX_TRANSACTIONS) -> list[Swap]:
    return list(_iter_swaps(path))


def _iter_swaps(path: Path) -> Iterator[Swap]:
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            yield Swap(
                sig=row["sig"],
                mint=row["mint"],
                ts=int(row["ts"]),
                side=row["side"],
                fee=int(row["fee"]),
                native_delta=int(row["nat_d"]),
                wsol_delta=int(row["wsol_d"]),
                token_delta=int(row["tok_d"]),
                decimals=int(row["dec"]),
                pool=row["pool"],
                base_pre=int(row["base_pre"]),
                base_post=int(row["base_post"]),
                quote_pre=int(row["quote_pre"]),
                quote_post=int(row["quote_post"]),
                program=row["prog"],
                nonce=row["nonce"] == "1",
                rent_delta=int(row["rent_d"]),
                tip=int(row["tip"]),
            )


def load_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def load_harvest_attempts(path: Path = HARVEST_ATTEMPTS) -> dict[str, str]:
    """The nonce-account hit-rate sample, which is key/value rather than rows."""
    with path.open(encoding="utf-8") as handle:
        return {
            row["metric"]: row["value"]
            for row in csv.DictReader(handle, delimiter="\t")
        }
