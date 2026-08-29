"""Extractability Scorer -- component 2 of the detection architecture.

Pure function, no I/O, no state, constant time. Given a pool snapshot and a
hypothetical incoming quote amount it answers: how much of that deposit can a
claim holder take back out, and what would holding such a claim have cost?

The whole thing rests on one identity (threat model section 2.1). A party
selling ``X`` tokens into a constant-product pool holding token reserve ``b``
and quote reserve ``q`` receives::

    dq_out = q * X / (b + X)

so the share of the quote reserve they capture is ``X / (b + X)`` -- a
function of the *ratio* alone. Neither the absolute sale size nor the value of
``q`` appears. Once ``b`` has collapsed to single or low-hundreds of raw units,
any holder of a non-trivial balance takes essentially all of ``q``.

**On the headline figure.** ``capture_fraction_at_1000x`` is named in the
threat model's output contract and is reported here, but note what it is: a
sale of 1000x the reserve captures 1000/1001 = 99.90% of *any* pool, live or
dead. It describes the sale ratio, not the pool, so no verdict is derived
from it. The verdict comes from reserve state (S1/S2), from the price impact
the deposit itself suffers, and from ``extractable_fraction_of_deposit``,
which is only computed against a *stated* adversary -- either the largest
holder actually observed on-chain, or a dust-ticket buyer sized at the
measured operator ticket. An extractability number with no claim size behind
it is unbounded and would mark every pool unsafe.

This module is deliberately isolated so it can be audited on its own, and it
is validated against the 32 captures in ``tests/fixtures/m3mx-transactions.tsv``
(see ``tests/test_replay.py``): the identity reproduces every observed capture
to within 0.25 percentage points, the residual being the AMM's swap fee.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import DEFAULT_THRESHOLDS, LAMPORTS_PER_SOL, Thresholds
from .models import Overhang, PoolSnapshot, RiskScore, Verdict

BPS = 10_000

#: Default adversary when no holder data is available: a single dust ticket
#: bought at current pool state. 20,000 lamports is operator A's measured
#: constant ticket size. This is a deliberately *weak* adversary -- real
#: operators hold positions acquired when the pool was cheaper -- so the
#: resulting figure is a floor on extractability, not a worst case.
DEFAULT_DUST_TICKET_LAMPORTS = 20_000

__all__ = [
    "Simulation",
    "simulate_capture",
    "capture_fraction",
    "quote_out",
    "tokens_out",
    "sale_size_for_capture",
    "claim_cost_for_capture",
    "score_pool",
    "score_reserves",
    "DEFAULT_DUST_TICKET_LAMPORTS",
]


# --- the identity ---------------------------------------------------------


def capture_fraction(sale_size: int, base_reserve: int) -> float:
    """Share of the quote reserve recovered by selling ``sale_size`` tokens.

    ``X / (b + X)``. Independent of the quote reserve entirely: draining a
    0.01 SOL corpse and draining a 37 SOL one use the same sale ratio.

    A zero token reserve is captured entirely by any non-zero sale; a zero
    sale captures nothing. That ordering matters -- 0/0 scores as no capture,
    since selling nothing takes nothing.
    """
    if sale_size <= 0:
        return 0.0
    if base_reserve <= 0:
        return 1.0
    return sale_size / (base_reserve + sale_size)


def quote_out(base_reserve: int, quote_reserve: int, sale_size: int, fee_bps: int = 0) -> int:
    """Quote lamports out for a token sale, floor-rounded like the programs.

    ``fee_bps`` is charged on the output, which is where the sampled venues
    take it. It defaults to zero so the bare identity is the default and any
    fee model is an explicit choice by the caller.
    """
    if sale_size <= 0 or quote_reserve <= 0:
        return 0
    gross = (quote_reserve * sale_size) // (base_reserve + sale_size)
    return gross - (gross * fee_bps) // BPS


def tokens_out(base_reserve: int, quote_reserve: int, quote_in: int, fee_bps: int = 0) -> int:
    """Tokens received for a quote deposit. The mirror of :func:`quote_out`."""
    if quote_in <= 0 or base_reserve <= 0:
        return 0
    effective_in = quote_in - (quote_in * fee_bps) // BPS
    return (base_reserve * effective_in) // (quote_reserve + effective_in)


def sale_size_for_capture(base_reserve: int, target_fraction: float) -> int:
    """Smallest sale that captures ``target_fraction`` of the quote reserve.

    Inverting the identity: ``X = b * f / (1 - f)``. Answers "how big a bag
    does an attacker actually need?" -- against a 4-unit reserve at 99.9%,
    about four thousand raw units, which is what a 0.00002 SOL dust buy
    already bought.
    """
    if not 0.0 <= target_fraction < 1.0:
        raise ValueError("target_fraction must be in [0, 1)")
    if base_reserve <= 0:
        return 1
    return int(base_reserve * target_fraction / (1.0 - target_fraction)) + 1


def claim_cost_for_capture(
    quote_reserve: int, quote_in: int, target_fraction: float = 0.99
) -> int:
    """Lamports needed *now* to buy a claim that captures ``target_fraction``.

    Model: an adversary buys into the pool ahead of the caller, the caller
    then deposits ``quote_in``, and the adversary sells its whole balance.

    Writing ``u`` for the adversary's share of the quote reserve after its own
    buy and ``v`` for the caller's share after theirs, the capture identity
    collapses to ``u / (u + (1-u)(1-v))`` -- the token reserve cancels out
    entirely, so this holds for any pool at any decimals. Inverting for ``u``
    at a target ``f``::

        u = f(1-v) / (1 - f + f(1-v))        a_in = q * u / (1 - u)

    On a live 345 SOL pool that comes to tens of thousands of SOL; on a
    depleted one it comes to a fraction of a cent. That gap *is* the
    vulnerability -- section 2.3 measures it at ~180,000x per lamport.

    Returns ``sys.maxsize``-free integers; an empty pool costs 1 lamport.
    """
    if not 0.0 <= target_fraction < 1.0:
        raise ValueError("target_fraction must be in [0, 1)")
    if quote_reserve <= 0:
        return 1
    if quote_in <= 0:
        return -1  # undefined: no deposit to capture
    v = quote_in / (quote_reserve + quote_in)
    f = target_fraction
    denom = 1.0 - f + f * (1.0 - v)
    if denom <= 0:
        return 1
    u = f * (1.0 - v) / denom
    if u >= 1.0:
        return -1
    return int(quote_reserve * u / (1.0 - u)) + 1


# --- scoring --------------------------------------------------------------


@dataclass(frozen=True)
class Simulation:
    """The four steps that turn a deposit into someone else's SOL."""

    victim_tokens: int
    #: Lamports the claim holder removes from the pool.
    taken_by_holder: int
    #: Lamports the depositor gets back if they then sell everything they
    #: bought. This is the number that separates a trap from a bad fill.
    recovered_by_victim: int
    #: Share of the deposit that cannot be recovered.
    loss_fraction: float
    capture_by_holder: float


def simulate_capture(
    base_reserve: int,
    quote_reserve: int,
    quote_in: int,
    claim_size: int,
    *,
    fee_bps: int = 0,
) -> Simulation:
    """Deposit, holder fires, depositor tries to leave.

    Modelling the depositor's *exit* is what stops a large holder on a live
    pool from reading as a trap. On a functioning pool a holder selling takes
    lamports out, but the depositor's tokens are still backed by a real
    reserve and sell back for very nearly what they cost. On a depleted pool
    the reserve behind them has gone, and the exit returns nothing. Same
    holder, same sale, opposite outcome -- and only the exit leg tells them
    apart.
    """
    victim_tokens = tokens_out(base_reserve, quote_reserve, quote_in, fee_bps=fee_bps)
    base_1 = base_reserve - victim_tokens
    quote_1 = quote_reserve + quote_in

    taken = quote_out(base_1, quote_1, claim_size, fee_bps=fee_bps)
    base_2 = base_1 + claim_size
    quote_2 = quote_1 - taken

    recovered = quote_out(base_2, quote_2, victim_tokens, fee_bps=fee_bps)
    loss = 1.0 - min(recovered / quote_in, 1.0) if quote_in > 0 else 0.0
    return Simulation(
        victim_tokens=victim_tokens,
        taken_by_holder=taken,
        recovered_by_victim=recovered,
        loss_fraction=max(loss, 0.0),
        capture_by_holder=capture_fraction(claim_size, base_1),
    )


def score_reserves(
    base_reserve: int,
    quote_reserve: int,
    quote_in: int,
    *,
    base_supply: int | None = None,
    overhang: Overhang | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    initialised: bool = True,
    fee_bps: int = 0,
    dust_ticket_lamports: int = DEFAULT_DUST_TICKET_LAMPORTS,
) -> RiskScore:
    """Score raw reserves. The snapshot-free form, for tests and backtesting.

    The caller's deposit is modelled first, because it is the deposit that
    creates the pot: after it lands the pool holds ``q + quote_in`` against a
    token reserve of ``b - tokens_out``, and it is *that* state a claim holder
    fires into.
    """
    if quote_in < 0:
        raise ValueError("quote_in must not be negative")
    base_reserve = max(base_reserve, 0)
    quote_reserve = max(quote_reserve, 0)

    # What the caller gets at the pool's current state -- reported as-is,
    # before any adversary is introduced.
    bought = tokens_out(base_reserve, quote_reserve, quote_in, fee_bps=fee_bps)
    base_after = base_reserve - bought
    quote_after = quote_reserve + quote_in

    # Share of the token reserve the deposit buys, and the price impact it
    # eats. Both collapse to q_in / (q + q_in), which goes to 1 as the quote
    # reserve goes to zero: the caller ends up owning a reserve that is gone.
    deposit_dominance = quote_in / quote_after if quote_after > 0 else 0.0
    pool_share_acquired = (
        bought / base_reserve if base_reserve > 0 else (1.0 if quote_in else 0.0)
    )

    reference_sale = max(thresholds.reference_sale_multiple * max(base_after, 0), 1)
    capture_1000x = capture_fraction(reference_sale, base_after)

    reserve_share_of_supply = (
        base_reserve / base_supply if base_supply and base_supply > 0 else None
    )

    # Extractability against a *stated* adversary. Prefer the largest holder
    # actually on-chain -- it already holds its position, so the pool state
    # the caller trades into is unchanged. Otherwise assume one dust ticket,
    # bought now, ahead of the caller.
    if overhang is not None and overhang.largest_external_balance > 0:
        claim_size = overhang.largest_external_balance
        adversary_model = (
            f"largest observed external holder ({claim_size:,} raw units, "
            f"{overhang.holder_count} holders)"
        )
        sim_base, sim_quote = base_reserve, quote_reserve
    else:
        claim_size = tokens_out(base_reserve, quote_reserve, dust_ticket_lamports)
        # The ticket lands before the caller, so it moves the reserves the
        # caller then trades into.
        sim_base = base_reserve - claim_size
        sim_quote = quote_reserve + dust_ticket_lamports
        if claim_size > 0:
            adversary_model = (
                f"single {dust_ticket_lamports:,}-lamport dust ticket bought at current pool "
                f"state, {claim_size:,} raw units (no holder data; real operators hold more)"
            )
        else:
            # Degenerate: at this state a dust ticket rounds to zero tokens,
            # so the weak adversary buys nothing and extractability reads 0.
            # It says nothing about holders who bought when the pool was
            # cheaper -- which is how every observed capture was set up.
            # S1/S2 carry the verdict here, not this figure.
            adversary_model = (
                f"none -- a {dust_ticket_lamports:,}-lamport ticket buys 0 raw units at this "
                f"state; extractability is not measurable without holder data"
            )

    simulation = simulate_capture(sim_base, sim_quote, quote_in, claim_size, fee_bps=fee_bps)
    extractable = simulation.loss_fraction
    capture_by_largest = (
        simulation.capture_by_holder if overhang is not None and claim_size > 0 else None
    )
    claim_cost = claim_cost_for_capture(quote_reserve, quote_in, 0.99)

    signals: list[str] = []
    rationale: list[str] = []
    unsafe: list[str] = []
    caution: list[str] = []

    if not initialised:
        # Not a depletion signal -- the pool is simply not tradable, which is
        # a different fact and does not belong under an S-number.
        unsafe.append("Pool is not initialised or not routable.")

    # --- S1: the token reserve is gone -----------------------------------
    if base_reserve <= thresholds.s1_base_reserve_raw:
        signals.append("S1")
        unsafe.append(
            f"S1: token reserve is {base_reserve} raw units, at or below the "
            f"{thresholds.s1_base_reserve_raw}-unit depletion floor. A functioning pool holds "
            f"1e14-1e15 raw units; against a reserve this size anyone holding a non-trivial "
            f"balance takes the entire quote reserve."
        )
    elif base_reserve <= thresholds.s1_base_reserve_caution_raw:
        signals.append("S1w")
        caution.append(
            f"S1(weak): token reserve is {base_reserve} raw units, orders of magnitude below a "
            f"functioning pool though above the hard depletion floor."
        )

    # --- S4: the reserve against total supply, free of scale and decimals -
    if reserve_share_of_supply is not None:
        if reserve_share_of_supply <= thresholds.s4_reserve_share_of_supply:
            signals.append("S4")
            unsafe.append(
                f"S4: the pool holds {reserve_share_of_supply * 100:.2e}% of the token's total "
                f"supply. A functioning pool holds percent-scale supply; this reserve is gone."
            )
        elif reserve_share_of_supply <= thresholds.s4_reserve_share_caution:
            signals.append("S4w")
            caution.append(
                f"S4(weak): the pool holds {reserve_share_of_supply * 100:.2e}% of total supply, "
                f"orders of magnitude below a functioning pool."
            )

    # --- S2: the quote reserve is gone, but the pool still routes --------
    if quote_reserve <= thresholds.s2_quote_reserve_lamports:
        signals.append("S2")
        unsafe.append(
            f"S2: quote reserve is {quote_reserve / LAMPORTS_PER_SOL:.9f} SOL, at or below the "
            f"{thresholds.s2_quote_reserve_lamports / LAMPORTS_PER_SOL:.2f} SOL floor. A pool "
            f"holding this little cannot quote anybody honestly, yet it is still routable -- "
            f"this is the trap condition."
        )

    # --- the deposit against the pool it is landing in -------------------
    if quote_in > 0 and deposit_dominance >= thresholds.unsafe_extractable_fraction:
        signals.append("S2d")
        unsafe.append(
            f"The deposit would be {deposit_dominance * 100:.2f}% of the pool's entire quote "
            f"reserve, buying {pool_share_acquired * 100:.2f}% of a token reserve that has "
            f"already gone. Price impact is effectively total."
        )
    elif quote_in > 0 and deposit_dominance >= thresholds.caution_price_impact:
        caution.append(
            f"The deposit would be {deposit_dominance * 100:.2f}% of the pool's quote reserve; "
            f"price impact is {deposit_dominance * 100:.2f}%."
        )

    # --- extractability against a stated adversary -----------------------
    if quote_in > 0 and bought == 0:
        # No adversary needed: the reserve is so small that the deposit
        # rounds down to zero tokens out. The loss is total on its own.
        unsafe.append(
            f"A {quote_in / LAMPORTS_PER_SOL:.6f} SOL deposit would receive 0 raw units "
            f"against a reserve of {base_reserve}. The loss is total before any holder acts."
        )
    elif extractable >= thresholds.unsafe_extractable_fraction:
        unsafe.append(
            f"{extractable * 100:.2f}% of a {quote_in / LAMPORTS_PER_SOL:.6f} SOL deposit is "
            f"recoverable by a claim holder ({adversary_model})."
        )
    elif extractable >= thresholds.caution_extractable_fraction:
        caution.append(
            f"{extractable * 100:.2f}% of a {quote_in / LAMPORTS_PER_SOL:.6f} SOL deposit is "
            f"recoverable by a claim holder ({adversary_model})."
        )

    # --- what a 99%-capture claim would cost an attacker today -----------
    if quote_in > 0 and claim_cost >= 0:
        if claim_cost <= quote_in * thresholds.unsafe_claim_cost_ratio:
            unsafe.append(
                f"A claim large enough to take 99% of the deposit can be bought right now for "
                f"{claim_cost / LAMPORTS_PER_SOL:.9f} SOL -- under 1% of the deposit itself."
            )
        elif claim_cost <= quote_in * thresholds.caution_claim_cost_ratio:
            caution.append(
                f"A claim large enough to take 99% of the deposit costs "
                f"{claim_cost / LAMPORTS_PER_SOL:.9f} SOL, under 10% of the deposit."
            )

    verdict = Verdict.UNSAFE if unsafe else (Verdict.CAUTION if caution else Verdict.SAFE)
    rationale = unsafe + caution

    # --- S3: is anyone actually holding a claim? -------------------------
    if overhang is not None:
        ratio = overhang.overhang_ratio
        if ratio is None or ratio >= thresholds.s3_overhang_ratio:
            signals.append("S3")
            shown = "unbounded" if ratio is None else f"{ratio:,.0f}x"
            rationale.append(
                f"S3: claim overhang present -- the largest external holder's balance is {shown} "
                f"the residual token reserve, across {overhang.holder_count} holders. This pool "
                f"is not merely a bad trade, it is actively claimable."
            )
            # S3 raises an established bad trade to an actively harvested one;
            # on its own it never manufactures an unsafe verdict.
            if verdict is Verdict.CAUTION:
                verdict = Verdict.UNSAFE

    return RiskScore(
        verdict=verdict,
        capture_fraction_at_1000x=capture_1000x,
        residual_base_reserve=base_after,
        reserve_share_of_supply=reserve_share_of_supply,
        extractable_fraction_of_deposit=extractable,
        claim_cost_lamports=claim_cost,
        adversary_model=adversary_model,
        tokens_out=bought,
        pool_share_acquired=pool_share_acquired,
        price_impact=deposit_dominance,
        capture_fraction_by_largest_holder=capture_by_largest,
        signals=tuple(dict.fromkeys(signals)),
        rationale=tuple(rationale),
    )


def score_pool(
    snapshot: PoolSnapshot,
    quote_in: int,
    *,
    overhang: Overhang | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    fee_bps: int = 0,
) -> RiskScore:
    """Score a :class:`PoolSnapshot`. The form the advisory service calls."""
    return score_reserves(
        snapshot.base_reserve,
        snapshot.quote_reserve,
        quote_in,
        base_supply=snapshot.base_supply or None,
        overhang=overhang,
        thresholds=thresholds,
        initialised=snapshot.initialised,
        fee_bps=fee_bps,
    )
