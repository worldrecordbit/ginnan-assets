"""Program IDs, mints and detector thresholds.

Addresses are from Appendix A of ``docs/threat-model-dead-pool-extraction.md``.
Thresholds are from section 3.1 (signals S1-S3) of the same document; every
default here is traceable to a measured quantity in the companion forensic
analysis, and the docstring on each one says which.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- programs -------------------------------------------------------------

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPSWAP_FEE_CONFIG_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
METEORA_DAMM_V2_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
METEORA_DAMM_V2_POOL_AUTHORITY = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

#: Wrapped SOL. Membership in this set is what decides a pool's *economic*
#: orientation, rather than the AMM's own base/quote field names -- see the
#: orientation trap in threat model section 3.4.
WSOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = frozenset({WSOL_MINT})

#: The smallest pot operator A was measured bothering to harvest: its
#: pre-signed sells carry a ``min_out`` floor here and revert below it. Named
#: for telemetry and documentation; it is not a detector threshold.
OPERATOR_MIN_OUT_FLOOR_LAMPORTS = 10_000_000

#: Rent for a token account, in lamports. Measured directly on operator
#: wallets: every sampled account was exactly one of these two sizes.
RENT_SPL_TOKEN_ACCOUNT = 2_039_280
RENT_TOKEN_2022_ACCOUNT = 2_074_080

LAMPORTS_PER_SOL = 1_000_000_000

# Known operator nonce accounts. Present for telemetry/attribution only
# (signal S7); nothing on the protective path consults them.
KNOWN_OPERATOR_NONCE_ACCOUNTS = {
    "ED8oGfupSeNzsNoECY81E4XUDpdUFVCB4HRJ2qtPZTmC": "operator A (m3mx)",
    "5FX8Ymc8KTcMW4NDQns9Toyei9irLKeWVvmCLoQhrgAd": "operator B (gNfR)",
}


@dataclass(frozen=True)
class Thresholds:
    """Detector thresholds. All four map onto a documented signal.

    The separation between a functioning pool and a depleted one is about
    twelve orders of magnitude (threat model 2.2: residual reserves of 1, 4,
    22, 477, 8172 and 380327 raw units against healthy reserves of 1e14-1e15),
    so none of these needs delicate tuning.
    """

    #: S1. Token reserve at or below this many raw units means a 1000x sale
    #: recovers >=99.9% of the quote reserve.
    s1_base_reserve_raw: int = 1_000

    #: S1, weaker rung. Still orders of magnitude below a live pool, but not
    #: yet a certainty -- scored ``caution`` rather than ``unsafe``.
    s1_base_reserve_caution_raw: int = 1_000_000

    #: S2. Quote reserve at or below 0.1 SOL. Every depleted pool in the
    #: evidence sits below this and every functioning one above 4 SOL, so the
    #: threshold falls in an empty band nearly two orders of magnitude wide.
    #: Note this is *ten times* the 0.01 SOL figure named in the threat
    #: model: that number is the operators' own ``min_out`` floor -- the
    #: smallest pot worth harvesting -- not the boundary of a live pool.
    s2_quote_reserve_lamports: int = 100_000_000

    #: S4. Token reserve as a share of total supply. A functioning pool holds
    #: percent-scale supply (measured: 0.6%-67%); a depleted one holds around
    #: 1e-10. The threshold sits in the empty decades between, and unlike the
    #: raw-unit floors it is free of both decimals and token scale.
    #:
    #: This is the stateless form of the threat model's S4. Rather than
    #: comparing the reserve product against a historical post-migration
    #: peak, it compares the reserve against supply, which needs no history
    #: and works on a pool first seen after it was already drained.
    s4_reserve_share_of_supply: float = 1e-6

    #: Same, for a ``caution`` verdict.
    s4_reserve_share_caution: float = 1e-4

    #: S3. Largest external token balance divided by residual token reserve.
    #: At 100x a single holder recovers 99.01% of the quote reserve.
    s3_overhang_ratio: float = 100.0

    #: Fraction of the incoming deposit an existing claim holder could take
    #: back out, above which the pool is unsafe.
    unsafe_extractable_fraction: float = 0.99

    #: Same, for a ``caution`` verdict.
    caution_extractable_fraction: float = 0.90

    #: Cost of a 99%-capture claim, as a fraction of the deposit, below which
    #: the pool is unsafe: if the claim on your money is cheaper than 1% of
    #: it, someone already holds one.
    unsafe_claim_cost_ratio: float = 0.01

    #: Same, for a ``caution`` verdict.
    caution_claim_cost_ratio: float = 0.10

    #: Price impact above which a deposit is flagged regardless of depletion.
    #: Not a trap signal -- a pool this thin is simply a bad fill.
    caution_price_impact: float = 0.50

    #: Sale size, as a multiple of the residual token reserve, used for the
    #: headline capture figure. Operators were measured firing at ~1002x.
    reference_sale_multiple: int = 1_000


DEFAULT_THRESHOLDS = Thresholds()
