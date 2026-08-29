"""Data contracts shared by the five components.

Each dataclass is the output contract of exactly one component, as specified
in section 4 of ``docs/threat-model-dead-pool-extraction.md``. They are plain
dataclasses with ``to_dict`` so the API layer never needs a serialiser.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any

from .constants import LAMPORTS_PER_SOL


class Verdict(str, enum.Enum):
    """Ordered worst-last so ``Verdict.worst`` escalates."""

    SAFE = "safe"
    CAUTION = "caution"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return _VERDICT_RANK[self]

    @staticmethod
    def worst(*verdicts: "Verdict") -> "Verdict":
        return max(verdicts, key=lambda v: _VERDICT_RANK[v])


_VERDICT_RANK = {
    Verdict.SAFE: 0,
    Verdict.UNKNOWN: 1,
    Verdict.CAUTION: 2,
    Verdict.UNSAFE: 3,
}


@dataclass(frozen=True)
class PoolSnapshot:
    """Output of the Pool State Indexer (section 4.1).

    ``base``/``quote`` are *economic* roles resolved from mint identity, not
    the AMM's own field names. In pools where wrapped SOL is the program's
    base mint the two disagree, and keying off the program's naming (or its
    log strings) gets the direction backwards -- threat model section 3.4.
    """

    pool: str
    program: str
    base_mint: str
    quote_mint: str
    base_vault: str
    quote_vault: str
    base_reserve: int
    quote_reserve: int
    base_decimals: int = 0
    quote_decimals: int = 9
    #: Total supply of the token mint, raw units. Zero when unavailable.
    base_supply: int = 0
    initialised: bool = True
    slot: int = 0
    #: True when the AMM's own "base" field held the quote mint, i.e. the
    #: orientation trap is present on this pool.
    orientation_flipped: bool = False

    @property
    def quote_reserve_sol(self) -> float:
        return self.quote_reserve / LAMPORTS_PER_SOL

    @property
    def base_reserve_ui(self) -> float:
        return self.base_reserve / (10 ** self.base_decimals)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quote_reserve_sol"] = self.quote_reserve_sol
        d["base_reserve_ui"] = self.base_reserve_ui
        return d


@dataclass(frozen=True)
class Overhang:
    """Output of the Claim-Overhang Service (section 4.3)."""

    mint: str
    largest_external_balance: int
    holder_count: int
    #: largest_external_balance / residual base reserve. ``None`` when the
    #: reserve is zero, where the ratio is unbounded.
    overhang_ratio: float | None
    #: True when the figures come from a partitioned estimate rather than a
    #: full enumeration.
    estimated: bool = False
    slot: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskScore:
    """Output of the Extractability Scorer (section 4.2)."""

    verdict: Verdict
    #: Share of the quote reserve a holder recovers selling 1000x the
    #: residual token reserve. The headline number from section 2.1.
    capture_fraction_at_1000x: float
    residual_base_reserve: int
    #: Token reserve as a share of the mint's total supply -- the scale-free
    #: depletion test (S4). ``None`` when the supply was not available, in
    #: which case the raw-unit reserve floors carry the verdict alone.
    reserve_share_of_supply: float | None
    #: Share of the caller's deposit that is *unrecoverable* once a claim
    #: holder fires: what they put in, less what selling their tokens back
    #: would return afterwards. Modelling the exit is what separates a trap
    #: from a large holder on a live pool -- see ``scorer.simulate_capture``.
    #: Always read alongside ``adversary_model``: the figure is only as
    #: meaningful as the claim size it assumes.
    extractable_fraction_of_deposit: float
    #: What it would cost, in lamports, to buy a claim on this pool large
    #: enough to capture 99% of the caller's deposit. An upper bound on the
    #: attacker's cost: an operator holding a pre-rug position paid less.
    claim_cost_lamports: int
    #: Which adversary ``extractable_fraction_of_deposit`` was computed for.
    adversary_model: str
    #: Tokens the caller would receive for ``quote_in``.
    tokens_out: int
    #: Share of the pool's token reserve the caller's deposit buys. Near 1.0
    #: means the caller is buying a reserve that is already gone.
    pool_share_acquired: float
    #: Price impact as ``1 - mid/execution``, which for a constant-product
    #: pool is ``q_in / (q + q_in)`` -- the bounded convention most DEX front
    #: ends display. (The unbounded convention, ``q_in / q``, expresses the
    #: same trade as a number that can exceed 100%.)
    price_impact: float
    #: Capture achievable by the largest *observed* external holder. Only set
    #: when overhang data was supplied.
    capture_fraction_by_largest_holder: float | None = None
    #: Signal IDs from threat model section 3 that fired, e.g. ``("S1","S2")``.
    signals: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()

    @property
    def human_reason(self) -> str:
        return " ".join(self.rationale) if self.rationale else "No depletion signals fired."

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["signals"] = list(self.signals)
        d["rationale"] = list(self.rationale)
        d["human_reason"] = self.human_reason
        return d


@dataclass(frozen=True)
class Advisory:
    """Output of the Pre-Trade Advisory API (section 4.4).

    Carries ``snapshot_slot`` so a caller can reason about staleness, per the
    contract in section 4.4 and the staleness limitation in section 6.
    """

    pool: str
    verdict: Verdict
    capture_fraction: float
    residual_reserve: int
    human_reason: str
    snapshot_slot: int
    quote_in: int
    score: RiskScore | None = None
    snapshot: PoolSnapshot | None = None
    overhang: Overhang | None = None
    #: Set when the verdict was reached without complete data, e.g. the
    #: overhang service was cold or the pool could not be read.
    degraded: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "verdict": self.verdict.value,
            "capture_fraction": self.capture_fraction,
            "residual_reserve": self.residual_reserve,
            "human_reason": self.human_reason,
            "snapshot_slot": self.snapshot_slot,
            "quote_in": self.quote_in,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "score": self.score.to_dict() if self.score else None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "overhang": self.overhang.to_dict() if self.overhang else None,
        }
