# deadpool

Detects Solana AMM pools whose reserves have been withdrawn, and whose next
deposit is therefore recoverable by whoever already holds tokens against them.

A rugged pool does not become inert. It becomes a trap with a computable
payoff: the token side of the vault is left at one to a few hundred raw units,
and under the constant-product curve anyone holding a meaningful balance can
take essentially **100% of any SOL that ever enters it again**. Operators dust-buy
every migration precisely to hold that claim, then fire a pre-signed sell the
moment a buyer walks in.

This is the detection system specified in
[`docs/threat-model-dead-pool-extraction.md`](docs/threat-model-dead-pool-extraction.md),
built against the evidence in
[`docs/solana-rug-harvester-analysis.md`](docs/solana-rug-harvester-analysis.md).
The condition is visible from pool state alone, before anyone signs anything —
and because the check evaluates the *pool* rather than the actor, it is
complete with respect to operators by construction. It never needs to know who
they are.

Python 3.10+, standard library only. No runtime dependencies, so it can be
dropped into a wallet or router without pulling a tree behind it.

## Try it without a network

Both of these run offline, against pool states taken from the forensic record.

```console
$ deadpool score --base-reserve 4 --quote-reserve 37810000011 --amount-sol 0.1
UNSAFE  (offline)
  deposit scored          0.100000000 SOL
  token reserve after buy 4 raw units
  you would receive       0 raw units (0.0000% of the reserve)
  ...
  - S1: token reserve is 4 raw units, at or below the 1000-unit depletion floor.
  - A 0.100000 SOL deposit would receive 0 raw units against a reserve of 4.
    The loss is total before any holder acts.

$ deadpool score --base-reserve 145816924891423 --quote-reserve 345500000000 --amount-sol 0.1
SAFE  (offline)
```

Those are the same pool: USWR before the rug, and after. The exit code is the
verdict — `0` safe, `1` caution, `2` unsafe, `3` unknown, `4` usage error — so a
CI step or a wallet shell hook can branch on it without parsing anything.

`deadpool replay` re-derives every capture in the record from the scorer's own
arithmetic:

```console
$ deadpool replay
Replaying 32 captures from m3mx-transactions.tsv

signature    venue               reserve  sale/reserve  predicted   observed     err     pool SOL
3DC3R5tAHPLF meteora-damm2     2,921,012          6.4x     86.44%     86.26%   0.17%     1.535977
2D9KMBcuqdqD pumpswap                 18      1,002.0x     99.90%     99.90%   0.00%     0.497500
...
32 captures. Worst error 0.25 percentage points (the AMM's swap fee, which the
bare identity omits).
32/32 were durable-nonce transactions -- pre-signed and held until a deposit landed.
```

## Against a live endpoint

```console
$ deadpool score --pool <address> --amount-sol 0.1 --rpc https://your-endpoint
$ deadpool serve --rpc https://your-endpoint --port 8080
$ deadpool census --wallet <address> --buckets 4
```

```python
from deadpool import AdvisoryService, ClaimOverhangService, JsonRpcClient, PoolStateIndexer

client = JsonRpcClient("https://your-endpoint")
service = AdvisoryService(PoolStateIndexer(client), overhang=ClaimOverhangService(client))

advisory = service.advise_sol(pool, 0.1)
if advisory.verdict is Verdict.UNSAFE:
    refuse(advisory.human_reason)
```

The HTTP surface:

```
GET /health
GET /v1/advisory?pool=<addr>[&amount_sol=0.1|&amount_lamports=N][&overhang=0]
GET /v1/pool/<addr>
GET /metrics                       # prometheus text
```

An `unsafe` verdict is served as HTTP 200. It is a successful answer to the
question asked; a 4xx would push callers toward treating it as a transport
failure to retry past.

## How it decides

Everything rests on one identity. Selling `X` tokens into a pool holding token
reserve `b` and quote reserve `q` returns `q·X/(b+X)`, so the share of the pool
captured is `X/(b+X)` — a function of the *ratio* alone. Neither the absolute
sale size nor the value of `q` appears anywhere. Once `b` collapses, any holder
takes everything.

| `X / b` | quote captured |
|--------:|---------------:|
| 1 | 50.0% |
| 100 | 99.01% |
| **1,000** | **99.90%** |

Observed sales cluster at `X ≈ 1000·b`. That is not a coincidence — it is the
sizing rule, hard-coded, and it yields 99.90% to four significant figures every
time.

Five signals feed the verdict. They are deliberately independent, so each
covers cases the others miss:

| | signal | unsafe at | why that number |
|---|---|---|---|
| **S1** | token reserve, raw units | ≤ 1,000 | Live pools hold 1e14–1e15. Twelve orders of magnitude of daylight. |
| **S4** | token reserve ÷ total supply | ≤ 1e-6 | Scale-free and decimals-free. Measured: live pools 0.6%–67%, drained pools ~1e-10. |
| **S2** | quote reserve | ≤ 0.1 SOL | Every drained pool in the record sits below this; every live one above 4 SOL. |
| **S2d** | deposit ÷ post-deposit reserve | ≥ 99% | You would be buying a reserve that has already gone. |
| — | cost to buy a 99% claim | ≤ 1% of the deposit | If the claim on your money is cheaper than your money, someone holds one. |

**S3** — a claim overhang of ≥100× the residual reserve — never manufactures an
unsafe verdict on its own. It raises an already-bad trade from *bad* to
*actively targeted*.

### Two things the design gets deliberately right

**The headline capture number proves nothing by itself.** A 1000× sale captures
99.90% of *any* pool, live or dead, so `capture_fraction_at_1000x` describes the
sale ratio, not the pool. It is reported because the threat model names it in
the output contract, but no verdict derives from it. Extractability is only ever
computed against a *stated* adversary — the largest holder actually observed
on-chain, or failing that a single 20,000-lamport dust ticket, the measured
operator ticket size. `adversary_model` on every response says which was used.

**The exit leg is what separates a trap from a bad fill.** `simulate_capture`
runs all four steps: deposit lands, holder fires, depositor tries to leave. On a
live pool a large holder selling moves the price, but the deposit's tokens are
still backed by a real reserve and sell back for very nearly what they cost. On
a depleted pool the reserve behind them has gone and the exit returns nothing.
Same holder, same sale, opposite outcome — and only modelling the exit tells
them apart. Scoring on the holder's proceeds alone would flag every liquid pool
with a whale in it.

## Architecture

Five components with disjoint responsibilities, so any one can be replaced or
scaled independently.

| Module | Component | Responsibility |
|---|---|---|
| `indexer.py` | Pool State Indexer | Reserves, orientation, liveness. Nothing else. |
| `scorer.py` | Extractability Scorer | Pure, constant-time, no I/O. The analytic core. |
| `overhang.py` | Claim-Overhang Service | Who holds claims (S3/S5) and how big their books are (S6). |
| `advisory.py` | Pre-Trade Advisory API | Composes the above into one verdict. |
| `telemetry.py` | Telemetry & Alerting | Measurement only. Strictly off the protective path. |

Three contract points shape the rest:

- **Every response carries `snapshot_slot`.** Reserves can move between the
  check and the inclusion of the user's transaction. Callers need to see how
  fresh the answer is rather than take it on trust.
- **The overhang service may be absent.** It is slower than S1/S2 and sits off
  the hot path, so a cold or failing lookup marks the response `degraded` and
  the pool-state verdict stands alone. An explicit `overhang=0` is a choice
  rather than a degradation, and says so in `warnings`.
- **Fail-closed is available.** With `fail_closed=True` an unreadable pool
  returns `caution`, not `safe`. The asymmetry is deliberate: a missed
  detection can cost a user their whole deposit, a false positive costs one
  trade.

### Orientation is resolved once, from mint identity

In a material minority of PumpSwap pools **wrapped SOL is the program's base
mint**, so the program logs `Instruction: Sell` while the wallet *gains* tokens.
Anything keyed on log strings or field order gets the direction backwards on a
large minority of transactions.

The indexer therefore never reads a log and never trusts a field name. The vault
whose mint is wrapped SOL is the quote side; that is the whole rule, it is
applied once at resolution time, and it is authoritative downstream.
`orientation_flipped` reports when the trap was present.

### Vault resolution is layered, and every layer is checked on-chain

A hard-coded account layout is a guess about someone else's struct, and a wrong
guess would silently report some other account's reserves. So each layer is
*validated* — are these really token accounts held by this pool, and do the
mints they hold match what the layout claimed? — and a failed check falls
through:

1. vaults supplied by the caller,
2. the PumpSwap layout decoder,
3. `getTokenAccountsByOwner(pool)`, exact wherever vaults are pool-owned,
4. a scan of the pool account for embedded pubkeys, each candidate fetched and
   kept only if it is a token account owned by the pool or a known pool
   authority.

Meteora DAMM v2 resolves through layer 4: its vaults belong to a shared pool
authority, and this codebase does not hard-code its layout. There is a test that
shifts a PumpSwap vault pointer to a decoy account holding 1e12 tokens and
asserts the indexer reports the pool's real reserves anyway.

## Validation

`tests/test_replay.py` replays the forensic record — 82 reconstructed
transactions with each pool's reserves before and after, plus three large
captures decoded leg by leg. It asserts, among other things, that:

- the identity predicts all 32 observed captures to within **0.25 percentage
  points**, the residual being the AMM's swap fee;
- **every pool that was actually drained scores `unsafe`** when scored as it
  stood immediately beforehand;
- the healthy pools those same operators dust-bought score `safe` — a detector
  that flagged those would flag every migration on the chain;
- the one drained pool S4 misses is caught by S2, and vice versa.

```console
$ python -m unittest discover -s tests -t .
Ran 182 tests in 0.66s
OK
```

The evidence ships inside the package (`src/deadpool/evidence/`) rather than
beside the tests, because `deadpool replay` uses it too.

## Limitations

**No live verification has been run.** This was built in an environment with no
route to a Solana RPC endpoint, so every component below the scorer is exercised
against a synthetic chain (`tests/support.py`) that serves real account bytes
over the real JSON-RPC shapes. The arithmetic is validated against real
historical data; the RPC integration is not. Point it at an endpoint and check
`deadpool score --pool <known corpse>` before trusting it in a signing path.

**PumpSwap's account layout is a hypothesis.** It is validated against the vault
accounts before use and falls back when the check fails, so a wrong offset costs
latency rather than correctness — but it has not been confirmed against a live
pool.

**Detection is not enforcement.** It protects callers who consult it, and
nothing else. Only a minimum-reserve invariant in the AMM programs themselves
removes the condition; §5.4 of the threat model covers what that would take.

**Staleness.** A verdict is computed against a slot, and reserves can change
before the user's transaction lands. The window is small but non-zero, which is
why the slot is on every response and why fail-closed exists.

**False positives.** A pool legitimately transiting a low-reserve state — during
its own migration, say — could score unsafe. The thresholds sit in bands several
orders of magnitude wide so this should be rare, but the cost is not zero and
belongs in the review queue the telemetry component feeds.

## Layout

```
src/deadpool/       the five components, plus rpc/spl/base58 plumbing
src/deadpool/evidence/   the forensic record, as TSV
tests/              182 tests, no network required
docs/               the threat model and the analysis it rests on
```
