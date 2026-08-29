# Rugged-LP Harvesting: On-Chain Analysis of Three Solana Wallets

**Date:** 2026-08-29
**Method:** Full reconstruction of all 82 supplied transactions for `27HFmP7c…` ("m3mx") from Solana JSON-RPC
(`getTransaction`), plus sampled reconstruction of `Fs9RN3wA…` and `kiwiC4pg…`. Every figure below is
derived from `pre/postBalances` and `pre/postTokenBalances` deltas — never from instruction log names,
which are misleading here (see "Pool orientation" below).

---

## 1. Answer in one paragraph

All three wallets run the same business: **they are not trading tokens, they are draining the SOL out of
dead liquidity pools.** They scatter sub-cent dust buys across every newly-migrated pool, which leaves them
holding a token position in pools that later get rugged. When a pool is rugged the LP pulls the SOL but the
*token* side of the vault is left almost empty too — typically 1–300 raw units. That tiny residual reserve is
the whole game: under the constant-product curve, anyone holding a meaningful token balance can now sell into
that pool and take out essentially **100% of any SOL that ever enters it again**. So they wait. The moment a
victim buys into the corpse, a pre-signed sell fires and takes the victim's SOL. The dust buy is not an
investment — it is the purchase of a permanent, perpetual claim on all future SOL deposited into a dead pool.

---

## 2. Grading the hypothesis

| # | Your hypothesis | Verdict | What the chain actually shows |
|---|---|---|---|
| 1 | Buy dust (0.00001–0.002 SOL) of a token that just migrated on pumpfun/bags/meteora | **Confirmed** | Ticket sizes are fixed constants per wallet: m3mx 20,000 lamports, kiwi 15,000, Fs9RN3 10,000 (PumpSwap) / 70,000 (Meteora). |
| 2 | Wait for LP being removed (rug) | **Confirmed, and it's also an entry** | They don't only wait — they *also buy after* the rug. A post-rug dust buy gets 92–99.7% of the pool's entire token supply for 0.00002 SOL, because the pool is empty on both sides. |
| 3 | Wait for the first person to buy in after LP removed | **Confirmed** | This is the trigger. Sells fire in the same block or within seconds of fresh SOL landing. |
| 4 | Sell same amount as LP | **Incorrect** | They sell a *token* amount sized to the pool's residual **token** reserve, not to the SOL. The rule is `tokens_to_sell ≈ 1000 × base_reserve`, which by constant product yields `1000/1001 = 99.90%` of the pool's SOL. |

**On point 4 — the actual sizing rule.** Selling `X` tokens into a pool with token reserve `b` and SOL
reserve `q` returns `q·X/(b+X)`. Set `X = 1000b` and you get `q·1000/1001 = 99.90%` of the SOL, regardless
of how much SOL is in there. That constant is visibly hard-coded: 18 of the 32 sells land on a
`tokens_sold / base_reserve` ratio of **1002.0x**, and their capture rate is **99.90%** to four significant
figures every single time.

```
sig         mint        tokens_sold/base_reserve   pool SOL captured
15dy6vomK6  3FEiU97Nj5          1002.1x                 99.90%
HoEzpWbNHD  G9Ubyair6L          1002.0x                 99.90%
4k6CGkEVgP  GTvnzEnY7x          1002.0x                 99.90%
ddpwvc6QS4  BHsc52EvkM          1002.0x                 99.90%
3KBGrGv5Ym  HoNNWj6SVi          1002.0x                 99.90%
  … 13 more identical …
```

The absolute size of the sell is irrelevant and often absurdly small in percentage terms. In
`3Avz5tFCBk` they sold **999 raw units** — 0.00055% of their token holding — and took 0.1806 SOL out of a
0.1813 SOL pool. In `4RixEoKLTD` the pool's token reserve was literally **1 raw unit**; selling 999 units
returned 0.3167 SOL.

---

## 3. The PnL table (m3mx, all 28 mints, all 82 transactions)

Net PnL is computed as `native lamport delta + WSOL token-account delta`, which captures the swap, the
priority fee, the block-builder tip and the ATA rent in one figure. "SOL in" is gross swap cost only.
"Net/Cost" is net divided by (swap + fees + tips).

| # | Mint | Buys | Sells | SOL in | SOL out | Fees | Tips | Net PnL (SOL) | Net/Cost | Pool SOL captured | Hold | Venue |
|---|------|-----:|------:|-------:|--------:|-----:|-----:|--------------:|---------:|------------------:|-----:|-------|
| 1 | AxAK6rzR4JDo2Lqjy7N2wsrQKXedTAGArQeGrt2i9wnP | 1 | 1 | 0.000020 | 1.325007 | 0.001010 | 0.066250 | +1.255688 | 18.66x | 86.26% | 15.0h | meteora-damm2 |
| 2 | 4d7BehtH7NdNpkVie8KYQjDUPLwBeQQaffc73vLuZ4PQ | 1 | 1 | 0.000020 | 0.497002 | 0.076127 | 0.000000 | +0.418781 | 5.50x | 99.90% | 20m | pumpswap |
| 3 | 3FEiU97Nj5NrXiw1Fm5oys6kDNjgGbqBp9HzCkqtNJKi | 1 | 1 | 0.000020 | 0.487062 | 0.074605 | 0.000000 | +0.410364 | 5.50x | 99.90% | 28m | pumpswap |
| 4 | G9Ubyair6LcaLntEQoXU7AWHdht4VCk7ZH7TcJsnvNA2 | 1 | 1 | 0.000020 | 0.469436 | 0.071905 | 0.000000 | +0.395437 | 5.50x | 99.90% | 3.2h | pumpswap |
| 5 | GTvnzEnY7xyRKsG82M4CeU143VHKhgme2kPyLwRHtrBz | 2 | 2 | 0.000040 | 0.420269 | 0.063927 | 0.021034 | +0.333194 | 3.92x | 99.82% | 14m | pumpswap |
| 6 | AopriLSFAZg8dkArfzdLH1G6RpptUEXifF3egTLR3ren | 1 | 1 | 0.000020 | 0.316719 | 0.048662 | 0.000000 | +0.265998 | 5.46x | 99.65% | 25m | pumpswap |
| 7 | BHsc52EvkMcB5rf6iK41VMiZTPSFaenwSSFrPZ1otQ9e | 1 | 1 | 0.000020 | 0.329086 | 0.090731 | 0.000000 | +0.236261 | 2.60x | 99.90% | 44m | pumpswap |
| 8 | 5JfAcKovuMwUL3eY2x6AynQb7dgR25MwCJQSj7C9fQt5 | 5 | 1 | 0.000100 | 0.223196 | 0.003406 | 0.011160 | +0.206491 | 14.08x | 54.52% | 160.3d | pumpswap |
| 9 | HoNNWj6SViNTQYTUMvTrS9Sh7cXN1e83Z1gTvwErw37E | 1 | 1 | 0.000020 | 0.224212 | 0.033675 | 0.011222 | +0.177221 | 3.95x | 99.90% | 9m | pumpswap |
| 10 | GZtrZnRXDx4BAb3iNiPmNaKDcgMQzDecNSQFixQqt5qQ | 2 | 1 | 0.000040 | 0.210716 | 0.031658 | 0.010546 | +0.166397 | 3.94x | 99.90% | 7m | pumpswap |
| 11 | DurEg2prmzUFkn6Rf3sg6TtKTWdHPntht6VfRLHsx5Xb | 1 | 1 | 0.000020 | 0.198801 | 0.030457 | 0.000000 | +0.166250 | 5.46x | 99.90% | 44m | pumpswap |
| 12 | 2sNgrxbrBvRL3cje8p6PBvwzH8oJLtwkiBQKJuvyErCr | 2 | 5 | 0.000040 | 0.233702 | 0.041804 | 0.026224 | +0.163560 | 2.40x | 99.42% | 10.4h | pumpswap |
| 13 | 4SZW7wa8XerMD1d4EnzxcKL8vGF5rKJcvGBp8cZ3ZSGt | 2 | 1 | 0.000040 | 0.180582 | 0.027759 | 0.000000 | +0.150744 | 5.42x | 99.65% | 15.2h | pumpswap |
| 14 | FiQxJN7yHuSYM7sqYTd6V5Q6NEPMzAPp9GggBbAPUten | 2 | 1 | 0.000040 | 0.173398 | 0.026051 | 0.008677 | +0.134481 | 3.87x | 99.90% | 11.5h | pumpswap |
| 15 | 8jKZpYVJkLhrsJcHym2BWUSYtUe3KFk9PnaLLjaCpump | 4 | 1 | 0.000080 | 0.173939 | 0.000044 | 0.079972 | +0.091770 | 1.15x | 4.16% | 3.4h | pumpswap |
| 16 | C6WhqN9G8FhuVNAmKNpzA8T6JTzhVHxMGu7eEmVYpqEh | 2 | 1 | 0.000040 | 0.099400 | 0.016258 | 0.000000 | +0.081029 | 4.97x | 99.90% | 2m | pumpswap |
| 17 | CtY7VfrUaHUA7h3sgjjVSmUw5kVEoPwyCB1Rv5ESgjgP | 1 | 1 | 0.000020 | 0.073092 | 0.019033 | 0.007317 | +0.044649 | 1.69x | 99.90% | 2m | pumpswap |
| 18 | CC9PaSpaD5d5bsKSELr68ywPVs8nBsUzMaD52qx5hwA4 | 2 | 1 | 0.000040 | 0.083250 | 0.000025 | 0.038333 | +0.042778 | 1.11x | 99.90% | 1.2h | pumpswap |
| 19 | 72cjKRD7v6gMaaiaVgCds6pmMB6wQaNYPYPkEcEnkn6g | 1 | 1 | 0.000020 | 0.057426 | 0.012656 | 0.005748 | +0.036927 | 2.00x | 99.90% | 24m | pumpswap |
| 20 | 2K1wRQNoTuvMuK2NXSJZb1b3dr6bA1vLXa5Gm4e2xAqV | 2 | 1 | 0.000040 | 0.065680 | 0.000025 | 0.030243 | +0.033298 | 1.10x | 99.90% | 16m | pumpswap |
| 21 | 3A8HAgxucEgQZwrMyf2RVH95nddD5wZSbkHSTHsT34j7 | 2 | 1 | 0.000040 | 0.030535 | 0.000324 | 0.001526 | +0.024565 | 12.99x | 96.85% | 12.5h | pumpswap |
| 22 | 2kJj2ftLUj6oK3Ao6oLBs5ViU26gENaYW3trPfhBBAGS | 4 | 1 | 0.000080 | 0.049686 | 0.000044 | 0.023000 | +0.024488 | 1.06x | 99.35% | 14.0h | pumpswap |
| 23 | 8Fv68mh4CG5R2iZS7r6se3aWobY1Cf7o4348WzsTZqN8 | 1 | 1 | 0.000020 | 0.023879 | 0.000248 | 0.001194 | +0.020378 | 13.94x | 26.87% | 59m | meteora-damm2 |
| 24 | E9Zhi2a7voN2CGfC5rBEAdUkc155b2ojVhNUMSVUymVg | 1 | 1 | 0.000020 | 0.019909 | 0.000209 | 0.000995 | +0.016646 | 13.60x | 25.62% | 49s | meteora-damm2 |
| 25 | DFeVPSHbYT2SXBiuQFRtuTs1oU2x8BzYPmigQZsYBAGS | 2 | 1 | 0.000040 | 0.022902 | 0.000025 | 0.010535 | +0.010228 | 0.96x | 75.46% | 5m | pumpswap |
| 26 | B5zkJ2at9h8mCo4WQeQv7ULNLojQDb8eJ7MiagoZAHsx | 2 | 1 | 0.000040 | 0.009940 | 0.001213 | 0.000498 | +0.006115 | 3.49x | 99.65% | 48s | pumpswap |
| 27 | 6LJfxtrbceqsutxiddxUs7GidZNg1WtuMbmdiB1KbZ64 | 1 | 1 | 0.000020 | 0.009990 | 0.001210 | 0.000500 | +0.004112 | 2.38x | 99.90% | 8m | pumpswap |
| 28 | ezoBXANwRAa9dcsage2E54QvjYLmTgjuaYnaNFwpump | 1 | 0 | 0.000020 | 0.000000 | 0.000010 | 0.000000 | -0.002104 | -71.07x | n/a | 0s | pumpswap |

### Totals

| Metric | Value |
|---|---|
| Transactions reconstructed | 82 (49 buys, 32 sells, 1 burn+close) |
| Distinct mints | 28 |
| Window | 2026-03-21 → 2026-08-29 (161 days) |
| **Capital deployed (swap-in)** | **0.000980 SOL** |
| **Gross harvested (swap-out)** | **6.008817 SOL** |
| Priority fees | 0.673098 SOL |
| Block-builder tips | 0.354975 SOL |
| Total execution cost | 1.028073 SOL — **17.1% of gross** |
| ATA rent paid (recoverable) | 0.064018 SOL |
| **Net profit** | **+4.915746 SOL** |
| Return on swap capital | ~5,016x net |
| Return on total cost incl. fees/tips | 4.78x |

**27 of 28 mints were profitable**, matching your note. The 28th (`ezoBXAN…`) is not a loss — it is an
open position bought 0 seconds before the end of the window; its −0.002104 SOL is 0.00207 of recoverable
ATA rent plus 0.0000296 of real cost.

### Reading the outliers

- **`AxAK6rzR4J` (+1.2557 SOL)** — the largest single win, and a Meteora DAMM v2 pool rather than PumpSwap.
  Captured 86.26% of a 1.536 SOL pool. Paid a 0.0663 SOL tip (5.1% of gross) to win it.
- **`5JfAcKovuM` (+0.2065 SOL, 160.3 days)** — the most instructive row. Bought 2026-03-21, **burned and
  closed the position 2026-03-31** to reclaim rent (tx `4reUng4DoT`), then re-bought the *same dead pool*
  on Aug 19, Aug 21 and Aug 28, and harvested on Aug 28. The buy and the sell are in the **same block**.
  They keep a watchlist of dead pools open indefinitely.
- **`2sNgrxbrBv` (5 sells)** — the same pool `4wPjJcfb` was milked **five separate times** at ~0.0469 SOL
  each as a victim kept re-buying into it.
- **`8jKZpYVJkL` (4.16% capture)** and the three Meteora rows at 25–27% — the cases where the sizing rule
  did *not* dominate the pool, so capture was partial. Still profitable, but these are the misses.

---

## 4. Execution mechanics — the part that is genuinely hard to replicate

### Pool orientation is not fixed (a trap)
In several PumpSwap pools **WSOL is the base mint**, so the program logs `Instruction: Sell` while the
wallet *gains* tokens and *loses* WSOL (`4aKpMPLt`, `2wCm4nxU`, `o8KwwaCm`, `5JckDSVu`, and both kiwi
samples). Every direction in this analysis was determined from the wallet's own balance deltas. Anyone
classifying these wallets by log string will get the buy/sell direction backwards on a large minority of
transactions.

### Durable nonces — the strongest structural signature in the dataset

| | nonce used | nonce absent |
|---|---:|---:|
| SELL | **32** | 0 |
| BUY | 0 | **49** |

Every single sell is a **durable-nonce transaction** (`advanceNonce`, nonce account
`ED8oGfupSeNzsNoECY81E4XUDpdUFVCB4HRJ2qtPZTmC`); no buy is. Durable nonce transactions never expire.
This is decisive: the sells are **pre-signed offline and held in memory**, ready to fire the instant a
victim's SOL lands, with zero signing latency and no blockhash-expiry risk while waiting hours or days for
a target to appear. The buys are ordinary, uncontested, recent-blockhash transactions. This is the single
clearest piece of evidence that the whole thing is an automated, purpose-built system rather than manual
trading.

### Cost asymmetry: buys are free, sells are a war

- **Buys**: 9,600 lamports priority fee, no tip, ever. Nobody is competing for them.
- **Sells**: priority fees up to **90,720,918 lamports** and tips up to **66,250,340 lamports**.
  Race cost is 15–20% of gross on most harvests and reaches **46%** on the tipped ones.

Four separate relay networks are used, which implies either a multi-relay submission strategy or several
bot builds:

| Relay | Tip account |
|---|---|
| Jito | `Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY`, `9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta` |
| Astralane | `astraEJ2fEj8Xmy6KLG7B3VfbKfsHXhHrNdCQx7iGJK`, `astra4uejePWneqNaJKuFFA8oonqCE1sqF6b45kDMZm` |
| Nozomi / Temporal | `nozFrhfnNGoyqwVuwPAW4aaGqempx4PU6g6D9CJMv7Z` |
| NextBlock | `neXtBLock1LeC67jYd1QdAa32kbVeubsfPNTJC1V5At` |

Notice the two distinct fee regimes in the data: some sells pay ~15% as a **priority fee with no tip**,
others pay ~6 lamports of priority fee and 46% as a **tip**. That is two different submission paths —
public mempool vs. private relay — chosen per transaction.

### Rent recycling
m3mx holds a persistent WSOL ATA (`EMpzysJ72GYNYrVu4qP5uEgAnqWc6SBGbkqsQfn3ajX4`) so it never wraps/unwraps
per trade. Token ATAs are created per mint (2,039,280 lamports for SPL, 2,074,080 for Token-2022) and
reclaimed via `Burn` + `CloseAccount` when a position is written off — see `4reUng4DoT`, which recovered
+2,034,080 lamports net. **This makes the true cost of a losing lottery ticket ~0.00003 SOL, not
~0.00205 SOL** — a ~70x difference that is what makes spraying thousands of tickets a day viable.

### Speed
- `CtY7VfrU`: migration → dust buy → rug → harvest in **92 seconds**.
- `B5zkJ2at`: a 152.42 SOL pool drained **31 seconds** after the dust buy.
- Five mints show the buy and the sell in the **same block** (atomic reload-and-harvest).

---

## 5. The other two wallets

These are **the same strategy at a completely different point on the frequency/size curve**. m3mx is 82
transactions across 161 days. These two run ~5,000–6,500 transactions per day.

| | m3mx `27HFmP7c…` | `Fs9RN3wA…` | `kiwiC4pg…` |
|---|---|---|---|
| Native balance now | 99.87 SOL | 8.20 SOL | 3.94 SOL (+11.13 WSOL) |
| Observed tx rate | 82 / 161 days | ~6,500/day | ~5,100/day |
| Ticket size | 20,000 lamports | 10,000 (PumpSwap) / 70,000 (Meteora) | 15,000 lamports |
| Priority fee/tx | 9,600 | 5,000 | 11,000 |
| Venue | PumpSwap + Meteora DAMM v2 | **Meteora DAMM v2 heavy** + PumpSwap | **PumpSwap only** |
| Role in the pattern | Both legs; does the harvesting | Both legs, Meteora-specialised | Almost pure seeding |

**Six sampled transactions across both wallets were all dust buys** — none were harvests. That is itself the
finding: at these rates the seeding leg dominates overwhelmingly, and the harvest fires only on the rare
occasion a corpse gets refilled.

**`kiwiC4pg…` is the purest seeder.** All three samples are 15,000-lamport buys into **healthy** PumpSwap
pools (86.9, 87.8, 89.9 SOL reserves), taking 0.000017% of pool tokens — economically meaningless as a
trade, and only sensible as buying an option on the pool later dying. Two of them
(`5DgZhE8ENi`, `4Qvgp41vJc`) are **in the same block on different mints**: it is indexing migrations and
firing at every one. It uses a persistent WSOL ATA holding 11.13 SOL as its float. This explains the
profile you described — highest lifetime PnL, lowest recent PnL: it is carrying the largest inventory of
outstanding claims, but claims only pay when someone walks into a dead pool.

**`Fs9RN3wA…` is the Meteora specialist and the one clearly *reloading corpses*.** Its 70,000-lamport
Meteora buys go into pools whose SOL reserve is already **0.00034 SOL** and **0.0239 SOL** — dead pools,
where the ticket buys 1.5–17% of the entire token supply. And it is compounding: in `2e3AXCwMfM` its
balance in one mint goes **79,807 → 9,839,597 tokens** in a single buy, reusing an existing ATA. It wraps
SOL fresh per transaction rather than keeping a float. Its 10,000-lamport PumpSwap buys are the ordinary
seeding leg.

### Why the daily-revenue figures differ so much
The economics are entirely driven by seeding volume × hit rate:

- kiwi: ~5,100/day × (15,000 swap + 11,000 fee) ≈ **0.13 SOL/day** in unrecoverable cost, plus ~10.6 SOL/day
  of ATA rent that must be actively recycled or the strategy dies on working capital alone.
- Fs9RN3: ~6,500/day × ~15,000 ≈ **0.10 SOL/day** unrecoverable, plus similar rent churn.
- m3mx: negligible seeding cost; nearly all its expense is the 1.028 SOL of fees and tips spent *winning
  harvests*, i.e. it is positioned at the profitable end.

The rent recycling is not an optimisation — it is the binding constraint. At 5,000 ATAs/day, ~10 SOL/day of
rent is locked up, so the burn-and-close loop has to run continuously.

---

## 6. What this is, plainly

This is a **latency race against retail buyers who wander into rugged pools**. It is not arbitrage and it
carries no market risk — the token price is irrelevant, the pool's residual reserves are everything. The
edge decomposes into three parts, in ascending order of difficulty:

1. **Coverage** — dust-buying essentially every migration, cheaply enough that a miss costs ~0.00003 SOL.
2. **Detection** — knowing, within a block, that SOL has landed in a pool you hold a claim on.
3. **Execution** — pre-signed durable-nonce sells across four relays, willing to pay up to 46% of the
   harvest to land first.

Parts 1 and 2 are engineering. Part 3 is where the profit actually goes: **17.1% of everything they take
out is paid straight to validators and block builders**, and on the contested ones, nearly half.

---

## 7. Data files

- `data/m3mx-transactions.tsv` — all 82 reconstructed transactions, 18 columns (see `data/schema.txt`).
- `data/other-wallets-samples.tsv` — the 6 sampled transactions from `Fs9RN3wA…` and `kiwiC4pg…`.
- `data/schema.txt` — column definitions.

Every row was derived from raw `getTransaction` output; nothing is inferred from a block explorer's
labelling.
