# Residual-Liquidity Capture on Solana AMMs
## Threat Model, Detection Architecture, and Mitigations

**Date:** 2026-08-29
**Companion document:** `solana-rug-harvester-analysis.md` — the forensic evidence base. Every empirical
figure cited here is derived there from raw `getTransaction`, `getSignaturesForAddress` and
`getProgramAccounts` output.

**Conventions.** All addresses, mints and signatures are written in full and unabbreviated. Reserve
quantities are given in raw base units unless a decimal scale is stated. "Operator" denotes a party running
the capture pattern; "affected buyer" denotes the party whose deposit is captured.

---

## 0. Summary

A liquidity pool whose reserves have been withdrawn does not become inert. It becomes a trap with a
specific, computable payoff structure: any subsequent deposit into it is almost entirely recoverable by
whoever holds the largest outstanding token balance against that pool.

The condition is detectable from pool state alone, before any user commits funds, using arithmetic that
runs in constant time. No adversary modelling is required — **a pool in this state is a bad trade even if
no operator is watching**, because the buyer receives a share of a token reserve that has already gone to
zero. The presence of operators simply converts a large loss into a total one.

Despite this, no wallet, router or block explorer surveyed performs the check. This document specifies the
threat, the detection system, and the mitigations available at each layer of the stack.

---

## 1. Threat model

### 1.1 Preconditions

The pattern requires all four of the following. All are publicly observable.

| # | Precondition | Observable as |
|---|---|---|
| P1 | A constant-product pool exists with both reserves near zero | Vault token account balances |
| P2 | One or more parties hold a token balance far exceeding the pool's residual token reserve | Token accounts for the mint, excluding the pool vault |
| P3 | A new party deposits SOL into the pool | Swap instruction against the pool |
| P4 | A holder from P2 can transact after P3 within the same or a nearby slot | Block inclusion |

P1 arises when liquidity is withdrawn from a migrated pool. P2 arises because acquiring a qualifying
balance is close to free once P1 holds — see §2.3.

### 1.2 Actor capabilities

Observed operator capabilities, established forensically:

- **Pool universe coverage.** Continuous monitoring of pool creation on both venues, at rates of
  4,400–4,900 transactions per day per wallet.
- **Persistent claim inventory.** Between 22,016 and ~250,368 open token accounts per operator,
  representing 45–511 SOL of immobilised rent.
- **Sub-slot reaction.** Pre-signed transactions using durable nonces, which do not expire and therefore
  require no signing round-trip at fire time.
- **Multi-relay submission.** Five distinct block-builder relays observed in use.
- **Asymmetric cost tolerance.** Willingness to spend 15–46% of the anticipated position on inclusion
  priority.

### 1.3 Exposure and loss distribution

The affected buyer's loss is the difference between what they deposit and what they can recover. Absent an
operator, a deposit into a pool satisfying P1 is *already* a near-total loss on a mark-to-market basis,
because the tokens received are a fraction of a reserve that is itself negligible. What the operator's
action changes is that the deposited SOL leaves the pool, removing even the theoretical ability to unwind.

Observed single-event losses in the evidence base range from **0.0099 SOL to 37.77 SOL**. The distribution
is severely right-skewed: a randomly drawn event is ~0.01 SOL, while the mean implied by operator income is
~25× that, indicating that a small number of large events dominate the total.

### 1.4 Out of scope

This document does not address the initial liquidity withdrawal that creates P1. That is a separate and
well-documented failure mode. The concern here is the persistent, exploitable state that survives it, and
which currently has no detection layer anywhere in the transaction path.

---

## 2. Mechanism

### 2.1 The capture identity

For a constant-product pool holding token reserve `b` and quote reserve `q`, a party selling `X` tokens
receives:

```
Δq_out = q · X / (b + X)
```

The fraction of the pool's quote reserve captured is therefore `X / (b + X)`, which depends only on the
**ratio** of the sale size to the residual token reserve — not on the absolute size of either, and not on
the value of `q`.

| X / b | Quote captured |
|---:|---:|
| 1 | 50.0% |
| 10 | 90.9% |
| 100 | 99.01% |
| **1,000** | **99.90%** |
| 10,000 | 99.99% |

Consequently, when `b` has collapsed to single or low-hundreds of raw units, **any** holder of a
non-trivial balance captures essentially the entire quote reserve. The sale need not be large in absolute
terms; it needs only to be large relative to `b`.

### 2.2 Empirical confirmation

Observed sale sizing clusters tightly at `X ≈ 1000·b`, yielding 99.90% capture to four significant figures.
Eighteen of thirty-two sales in the primary sample land on a ratio of 1002.0×. Representative residual
reserves at time of capture: **1, 4, 22, 477, 8,172 and 380,327 raw units**, against healthy-pool reserves
on the order of 10¹⁴–10¹⁵ raw units.

That is a separation of roughly **twelve orders of magnitude** between a functioning pool and one
satisfying P1. This is the single most important fact for detection: the two states are not close, and no
delicate threshold tuning is required to tell them apart.

### 2.3 Why P2 is inexpensive to satisfy

The cost of acquiring a qualifying balance collapses once P1 holds. The same fixed ticket buys a vastly
larger share of a depleted pool than of a functioning one:

| Pool state | Quote reserve | Ticket | Share of token reserve acquired |
|---|---:|---:|---:|
| Functioning | 345.5 SOL | 20,000 lamports | 0.0000058% |
| Depleted | 0.00188 SOL | 20,000 lamports | **1.05%** |

That is a difference of approximately **180,000×** in tokens acquired per lamport spent. Operators
therefore acquire positions both before and after depletion, and the post-depletion entry is by far the
more efficient of the two.

The residual cost of a position that never pays out is the token account rent — 2,039,280 lamports for SPL
Token, 2,074,080 for Token-2022 — and that rent is recoverable by burning the balance and closing the
account. Recovery has been observed directly. The unrecoverable cost of a position is therefore the
transaction fee alone, on the order of 10⁻⁵ SOL.

### 2.4 Execution characteristics

The following are documented operator behaviours. They are relevant to detection because each leaves an
observable signature, and to mitigation because each represents a cost that a defender's response must
overcome or render moot.

**Durable-nonce pre-signing.** Every contested transaction observed advances a durable nonce account rather
than referencing a recent blockhash. Durable nonce transactions do not expire, so a transaction may be
constructed and signed arbitrarily far in advance and held until its triggering condition occurs. This
removes signing latency from the critical path entirely. Two nonce accounts identified:
`ED8oGfupSeNzsNoECY81E4XUDpdUFVCB4HRJ2qtPZTmC` and
`5FX8Ymc8KTcMW4NDQns9Toyei9irLKeWVvmCLoQhrgAd`.

The nonce account is also a forensic asset: because only contested transactions advance it, its signature
history constitutes a complete attempt log including failures, which an operator's main transaction history
does not isolate.

**Inclusion-cost asymmetry.** A block-builder tip is a transfer instruction inside the transaction and is
therefore reverted if the transaction fails. A priority fee is charged regardless of outcome. At the
observed success rates (26% and 52.5% for two operators), this asymmetry strongly favours bidding through
tips. Both regimes appear in the data, selected per transaction:

| Regime | Priority fee | Tip | Charged on failure |
|---|---:|---:|---|
| Fee-weighted | 1.0M–90.7M lamports | 0 | Yes |
| Tip-weighted | ~6,000 lamports | up to 46% of position | No |

A hard-coded rung of **15.0% priority fee plus 5.0% tip** recurs across multiple transactions to four
significant figures. On the largest positions the priority fee is capped at a flat 320,000,001 lamports and
the bid moves to a flat 4,000,000,000-lamport tip.

**Selectivity dominates inventory.** Across the operators measured, inventory size is inversely related to
efficiency. The operator with the smallest claim inventory (22,016 accounts) produced the highest income
and the highest success rate; the operator with the largest (~179,712 accounts) produced the lowest return
on the capital its inventory immobilised (~5% per month against ~198% for the smallest). Holding more
positions does not increase income — it increases rent capital tied up and, at the observed acquisition
rates, working-capital pressure. What distinguishes the effective operators is **which** pools they hold a
position against and **when** they act, not how many positions they hold. A detector therefore does not
need to enumerate every operator to be useful; it needs only to evaluate pool state, which is common to all
of them.

---

## 3. Observable signatures

Everything the pattern relies on is visible on-chain before any user is harmed. This section enumerates the
signals in decreasing order of reliability, so a detector can be specified against them directly.

### 3.1 Pool-state signals (primary, pre-commitment)

These are computable from pool account data alone and require no history, no labelling, and no adversary
model. They are the signals a pre-trade check must use.

| ID | Signal | Source | Meaning |
|---|---|---|---|
| S1 | Token reserve collapsed to ≤ ~10³ raw units while the pool remains initialised | Pool vault token account | P1 satisfied — capture identity yields ≥99% for a 1000× sale |
| S2 | Quote reserve near zero (≤ ~0.01 SOL) with a non-zero, tradable market cap advertised elsewhere | Pool vault quote account vs. off-chain listing | Pool is depleted but still routable — the trap condition |
| S3 | Ratio of largest external token holder's balance to residual reserve ≥ ~10² | Mint holder set minus pool vault | A capitalised claim overhang exists (P2) |
| S4 | Reserve product `b·q` has fallen by ≥ ~10 orders of magnitude from its post-migration peak | Reserve snapshots over time | Depletion event has occurred |

S1 and S2 together are sufficient to mark a pool unsafe to buy into **regardless of whether any operator is
present**, because the incoming buyer is purchasing a share of a token reserve that is already at zero. S3
raises the severity from "bad trade" to "actively harvested."

### 3.2 Claim-overhang signals (secondary)

| ID | Signal | Source |
|---|---|---|
| S5 | A single mint held by hundreds of distinct accounts each with a dust-scale balance | `getProgramAccounts` on the token program, filtered by mint |
| S6 | A holder whose account set numbers in the tens of thousands, each a distinct mint | `getProgramAccounts` filtered by owner (bucket-partitioned) |

S6 identifies a probable operator wallet directly and is the basis of the census in the companion document.
It is a strong classifier: ordinary participants do not hold tens of thousands of distinct dust positions.

### 3.3 Execution signals (tertiary, post-hoc)

Useful for attribution and for measuring the field, not for pre-trade protection since they appear at or
after the moment of capture.

| ID | Signal | Source |
|---|---|---|
| S7 | Durable-nonce advance paired with an AMM swap instruction | Transaction instruction list |
| S8 | Slippage-revert bursts on a single pool vault (multiple failures within seconds, long silences between) | Vault signature history |
| S9 | A swap whose quote-out equals ≥99% of the pool's pre-transaction quote reserve | Pre/post balance deltas |
| S10 | Recurring fee/tip structure at the 15%/5% rung, or the flat 320,000,001-lamport fee cap | Fee and tip accounting |

### 3.4 The orientation caveat

Signal derivation must never use the AMM instruction name to infer trade direction. In pools where the
quote mint (wrapped SOL) is the base mint, a program `Sell` instruction corresponds to an economic buy and
vice versa. Direction must be taken from the signer's own pre/post token-balance deltas. A detector that
keys on log strings will misclassify a material fraction of activity.

---

## 4. Detection architecture

The system is specified as five components with disjoint responsibilities. Each has one job, a defined
input contract and a defined output contract, so that any component can be replaced or scaled independently.
The design goal is a pre-commitment verdict: a caller must be able to ask "is this pool safe to buy into?"
and receive an answer before a user signs.

```
                         ┌───────────────────────┐
   AMM programs  ───────▶│  1. Pool State         │  reserves, liveness,
   (PumpSwap,            │     Indexer            │  orientation per pool
    Meteora DAMM v2)     └───────────┬───────────┘
                                     │ pool snapshots
                                     ▼
                         ┌───────────────────────┐
                         │  2. Extractability     │  capture fraction for a
                         │     Scorer             │  hypothetical incoming buy
                         └───────────┬───────────┘
                                     │ per-pool risk score
                    ┌────────────────┼────────────────┐
                    ▼                                  ▼
        ┌───────────────────────┐         ┌───────────────────────┐
        │  3. Claim-Overhang    │         │  4. Pre-Trade          │◀── wallet /
        │     Service           │────────▶│     Advisory API       │    router call
        └───────────────────────┘         └───────────┬───────────┘
          outstanding-claim weight                     │ verdict + reason
                    │                                   ▼
                    └──────────────▶┌───────────────────────┐
                                    │  5. Telemetry &        │  field metrics,
                                    │     Alerting           │  operator census
                                    └───────────────────────┘
```

### 4.1 Pool State Indexer

**Responsibility:** maintain a current, correct view of every pool on the supported AMMs — its reserve
balances, its base/quote orientation, and whether it is initialised and routable. Nothing else.

- **Inputs:** account subscriptions to pool vault token accounts on PumpSwap
  (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`) and Meteora DAMM v2
  (`cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG`), plus new-pool discovery from migration events.
- **Outputs:** a `PoolSnapshot { pool, base_mint, quote_mint, base_reserve, quote_reserve, base_is_quote_token, initialised, slot }` per pool, updated on change.
- **Contract:** orientation is resolved once at pool creation from mint identities, not per transaction, and
  is authoritative for every downstream consumer, closing the §3.4 caveat at the source.
- **Scaling:** partition the pool set by vault-address prefix; the indexer is horizontally shardable because
  pools are independent.

### 4.2 Extractability Scorer

**Responsibility:** given a `PoolSnapshot` and a hypothetical incoming quote amount, compute what fraction
of that amount is recoverable by an existing claim holder. Pure function; no I/O, no state.

- **Inputs:** `PoolSnapshot`, hypothetical buy size `q_in`.
- **Core computation:** for the incoming buyer, tokens received `Δb = b·q_in/(q+q_in)`; for a subsequent
  claim-holder sale of size `X`, quote recovered `q·X/(b+X)`. The pool is scored unsafe when a claim holder
  selling any `X ≥ k·b` recovers ≥ threshold of `(q + q_in)` — which reduces, via §2.1, to a condition on
  `b` and the largest external balance alone.
- **Outputs:** `RiskScore { capture_fraction_at_1000x, residual_base_reserve, verdict ∈ {safe, caution, unsafe}, rationale }`.
- **Contract:** constant-time, deterministic, dependency-free, unit-testable against the empirical residual
  reserves in §2.2 (1, 4, 22, 477, 8,172, 380,327). This is the analytic heart of the system and is
  deliberately isolated so it can be audited in isolation.

### 4.3 Claim-Overhang Service

**Responsibility:** quantify how much outstanding claim pressure exists against a given mint — the S3/S5
signals — so the scorer's verdict can be raised from "bad trade" to "actively targeted."

- **Inputs:** mint address, `PoolSnapshot`.
- **Method:** enumerate token accounts for the mint via bucket-partitioned `getProgramAccounts` (256
  uniform buckets, offset-1 partition, validated against ≥2 buckets — see companion §5e), excluding the
  pool vault; report the largest external balance and the holder count.
- **Outputs:** `Overhang { largest_external_balance, overhang_ratio = largest/residual_base, holder_count }`.
- **Contract:** this service is rate-limited and cached; it is not on the pre-trade hot path (it is slower
  than S1/S2) and its absence must degrade gracefully — the pre-trade verdict is valid on pool state alone.

### 4.4 Pre-Trade Advisory API

**Responsibility:** the public surface. Answer "is it safe to buy into this pool right now?" for a wallet,
router or aggregator, synchronously, before signing.

- **Inputs:** `pool` or `(mint, amount)`.
- **Outputs:** `Advisory { verdict, capture_fraction, residual_reserve, human_reason, snapshot_slot }`.
- **Contract:** p99 latency low enough to sit in a signing flow; verdict derives from the Scorer on the
  latest snapshot, enriched by the Overhang service when warm; every response carries the slot it was
  computed against so a caller can reason about staleness. Fail-closed is offered as an option: on missing
  data the API may return `caution` rather than `safe`.
- **Integration shapes:** a synchronous REST/gRPC check, a wallet pre-sign hook, a router candidate-filter,
  or a block explorer badge.

### 4.5 Telemetry & Alerting

**Responsibility:** everything that is measurement rather than protection — field sizing, operator census,
and monitoring of the detector itself.

- **Inputs:** scorer verdicts over time, execution signals S7–S10.
- **Outputs:** counts of pools entering the unsafe state per day, capture events observed (S9), a running
  operator census (S6), and detector health (coverage, staleness, false-positive review queue).
- **Contract:** strictly read-only and off the hot path; it may never influence a pre-trade verdict, to keep
  the protective path simple and auditable.

---

## 5. Mitigations by layer

The condition is fixable at several layers, in increasing order of how completely it closes the problem.

### 5.1 Wallet / signing layer

Call the Pre-Trade Advisory API (§4.4) before presenting a swap for signature; block or hard-warn on an
`unsafe` verdict. This is the fastest path to protecting users and requires no cooperation from any
protocol. Limitation: it protects only users of participating wallets.

### 5.2 Aggregator / router layer

Exclude pools with an `unsafe` verdict from routing candidates entirely. Routers are a high-leverage point
because a large share of retail flow passes through a small number of them, and a pool depleted to the S1
state can never offer a legitimate quote, so removing it costs nothing in execution quality.

### 5.3 Explorer / analytics layer

Surface an `unsafe` badge and the capture fraction on pool and token pages. This does not block the trade
but corrects the information asymmetry that the pattern depends on — the affected buyer currently sees a
tradable market with no indication the reserves are gone.

### 5.4 AMM program layer (structural)

The pattern is only possible because the swap instruction will fill against a token reserve at or near
zero. A minimum-reserve invariant — rejecting swaps when either reserve is below a floor, or when a single
swap would move the price by more than a bounded factor — makes the depleted-pool trade impossible to
execute. This closes the condition at its root rather than warning around it. It is a program-level change
and therefore requires the AMM maintainers to act; the detection system above is what protects users in the
interim, and what would measure whether such a change had the intended effect.

### 5.5 Launchpad layer

At migration, pools could be created with liquidity locked or with a floor reserve that cannot be fully
withdrawn, which prevents P1 from ever being satisfied for pools launched through that venue. This is
preventive rather than detective and applies only to future pools.

---

## 6. Limitations and residual risk

**What detection cannot do.** The pre-trade check protects only callers who consult it; it cannot help a
user transacting through a wallet or router that has not integrated it. It is a warning system, not an
enforcement mechanism — only the §5.4 structural change removes the condition itself.

**Staleness.** A verdict is computed against a specific slot. Reserves can change between the check and
inclusion of the user's transaction. The exposure window is small but non-zero, which is why the API
returns its snapshot slot and why fail-closed is offered.

**False positives.** A pool legitimately transiting a low-reserve state — for example, momentarily during
its own migration — could score `unsafe`. The S1/S2 thresholds are set against a twelve-order-of-magnitude
separation (§2.2), so this is rare, but the cost of a false positive (a blocked or warned legitimate trade)
is not zero and belongs in a review queue (§4.5). The asymmetry favours caution: a missed detection can
cost a user their whole deposit, a false positive costs one trade.

**Population coverage.** The operator census (S6) is a lower bound. Enumerating holders of one harvested
mint surfaced operators larger than any of the three originally profiled, on the first sample. The field is
larger than any enumeration so far performed, which strengthens rather than weakens the case for
pool-state-based detection: because the check evaluates the pool rather than the actor, it is complete with
respect to operators by construction — it does not need to know who they are.

**What this document is not.** It specifies detection and mitigation. It deliberately does not provide an
acquisition, timing, or bidding strategy for running the pattern; those elements of operator behaviour are
described in §2.4 only to the depth required to detect them and to justify the mitigations, and the
efficient composition of them is out of scope by intent.

---

## Appendix A. Reference addresses

| Role | Address |
|---|---|
| PumpSwap AMM program | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` |
| PumpSwap fee-config program | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` |
| Meteora DAMM v2 program | `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG` |
| Meteora DAMM v2 pool authority | `HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC` |
| SPL Token program | `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` |
| Token-2022 program | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` |
| Nonce account (operator A) | `ED8oGfupSeNzsNoECY81E4XUDpdUFVCB4HRJ2qtPZTmC` |
| Nonce account (operator B) | `5FX8Ymc8KTcMW4NDQns9Toyei9irLKeWVvmCLoQhrgAd` |

Operator wallet addresses and full transaction evidence are catalogued in the companion document,
`solana-rug-harvester-analysis.md`.