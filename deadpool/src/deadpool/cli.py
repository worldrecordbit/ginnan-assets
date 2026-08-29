"""Command line entry point.

    deadpool score   --pool <addr> [--amount-sol 0.1]      # against an RPC
    deadpool score   --base-reserve N --quote-reserve N    # offline, no RPC
    deadpool serve   [--host H] [--port P]
    deadpool census  --wallet <addr> [--buckets N]
    deadpool replay  [--tsv PATH]                          # offline validation

``score`` in its offline form and ``replay`` need no network at all, which
makes the analytic core demonstrable on a machine with no RPC access.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

from . import evidence
from .advisory import DEFAULT_PROBE_LAMPORTS, AdvisoryService
from .api import serve as serve_http
from .constants import DEFAULT_THRESHOLDS, LAMPORTS_PER_SOL
from .indexer import PoolStateIndexer
from .models import Verdict
from .overhang import ClaimOverhangService
from .rpc import DEFAULT_ENDPOINT, JsonRpcClient
from .scorer import capture_fraction, score_reserves
from .telemetry import Telemetry

_VERDICT_MARK = {
    Verdict.SAFE: "SAFE",
    Verdict.CAUTION: "CAUTION",
    Verdict.UNSAFE: "UNSAFE",
    Verdict.UNKNOWN: "UNKNOWN",
}

#: Exit codes, so a wallet or CI step can branch on the verdict without
#: parsing stdout. 0 safe, 1 caution, 2 unsafe, 3 unknown, 4 usage error.
_EXIT = {Verdict.SAFE: 0, Verdict.CAUTION: 1, Verdict.UNSAFE: 2, Verdict.UNKNOWN: 3}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadpool",
        description="Detect liquidity pools whose reserves have been withdrawn and "
        "whose next deposit is recoverable by an existing token holder.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="score a pool, live or from raw reserves")
    score.add_argument("--pool", help="pool address (requires --rpc)")
    score.add_argument("--rpc", default=DEFAULT_ENDPOINT, help="JSON-RPC endpoint")
    score.add_argument("--base-reserve", type=int, help="offline: token reserve, raw units")
    score.add_argument("--quote-reserve", type=int, help="offline: quote reserve, lamports")
    score.add_argument("--amount-sol", type=float, help="deposit to score (default 0.1)")
    score.add_argument("--amount-lamports", type=int, help="deposit to score, in lamports")
    score.add_argument("--no-overhang", action="store_true", help="skip the holder lookup")
    score.add_argument("--fail-closed", action="store_true", help="incomplete data returns caution")
    score.add_argument("--json", action="store_true", help="machine-readable output")

    api = sub.add_parser("serve", help="run the pre-trade advisory HTTP API")
    api.add_argument("--rpc", default=DEFAULT_ENDPOINT)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--fail-closed", action="store_true")
    api.add_argument("--no-overhang", action="store_true")
    api.add_argument("--event-log", type=Path, help="append JSONL telemetry events here")

    census = sub.add_parser("census", help="size a wallet's claim book (signal S6)")
    census.add_argument("--wallet", required=True)
    census.add_argument("--rpc", default=DEFAULT_ENDPOINT)
    census.add_argument("--buckets", type=int, default=2, help="buckets to sample of 256")
    census.add_argument("--json", action="store_true")

    replay = sub.add_parser(
        "replay", help="validate the capture identity against the forensic record"
    )
    replay.add_argument("--tsv", type=Path, default=evidence.M3MX_TRANSACTIONS)
    replay.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "score":
        return _cmd_score(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "census":
        return _cmd_census(args)
    if args.command == "replay":
        return _cmd_replay(args)
    return 4


# --- commands -------------------------------------------------------------


def _cmd_score(args) -> int:
    amount = _amount(args)
    offline = args.base_reserve is not None or args.quote_reserve is not None

    if offline:
        if args.pool:
            print("error: pass --pool or raw reserves, not both", file=sys.stderr)
            return 4
        if args.base_reserve is None or args.quote_reserve is None:
            print(
                "error: offline scoring needs both --base-reserve and --quote-reserve",
                file=sys.stderr,
            )
            return 4
        score = score_reserves(args.base_reserve, args.quote_reserve, amount)
        if args.json:
            print(json.dumps(score.to_dict(), indent=2))
        else:
            _print_score(score, amount, pool="(offline)")
        return _EXIT[score.verdict]

    if not args.pool:
        print("error: --pool is required (or pass raw reserves)", file=sys.stderr)
        return 4

    service = _service(args.rpc, fail_closed=args.fail_closed, with_overhang=not args.no_overhang)
    advisory = service.advise(args.pool, amount, with_overhang=not args.no_overhang)
    if args.json:
        print(json.dumps(advisory.to_dict(), indent=2))
    else:
        _print_advisory(advisory)
    return _EXIT[advisory.verdict]


def _cmd_serve(args) -> int:
    telemetry = Telemetry(event_log=args.event_log)
    service = _service(
        args.rpc,
        fail_closed=args.fail_closed,
        with_overhang=not args.no_overhang,
        telemetry=telemetry,
    )
    logging.getLogger("deadpool.api").setLevel(logging.INFO)
    serve_http(service, args.host, args.port)
    telemetry.close()
    return 0


def _cmd_census(args) -> int:
    client = JsonRpcClient(args.rpc)
    result = ClaimOverhangService(client).census(args.wallet, buckets=args.buckets)
    if args.json:
        payload = dataclasses.asdict(result)
        payload["rent_locked_sol"] = result.rent_locked_sol
        print(json.dumps(payload, indent=2))
        return 0
    print(f"wallet            {result.wallet}")
    print(f"buckets sampled   {result.buckets_sampled} of 256"
          f"{' (exact)' if result.exact else ''}")
    print(f"SPL Token         {result.spl_token_accounts:,}")
    print(f"Token-2022        {result.token_2022_accounts:,}")
    print(f"estimated claims  {result.estimated_claims:,}")
    print(f"rent locked       {result.rent_locked_sol:,.1f} SOL")
    if result.bucket_dispersion is not None:
        print(f"bucket dispersion {result.bucket_dispersion * 100:.1f}%"
              "   (spread between sampled buckets; the uniformity check)")
    return 0


def _cmd_replay(args) -> int:
    """Replay the forensic record through the scorer's own arithmetic.

    For every capture in the record the identity ``X / (b + X)`` is compared
    against what the pool's reserves actually did. It is the test that the
    analytic core is not merely self-consistent.
    """
    swaps = evidence.load_swaps(args.tsv)
    rows = []
    for swap in swaps:
        observed = swap.observed_capture
        if swap.side != "SELL" or observed is None or swap.sale_size == 0:
            continue
        predicted = capture_fraction(swap.sale_size, swap.base_pre)
        rows.append(
            {
                "sig": swap.sig,
                "mint": swap.mint,
                "program": swap.program,
                "base_reserve": swap.base_pre,
                "sale_size": swap.sale_size,
                "sale_ratio": (swap.sale_size / swap.base_pre) if swap.base_pre else None,
                "predicted_capture": predicted,
                "observed_capture": observed,
                "error": abs(predicted - observed),
                "pool_sol": swap.quote_pre / LAMPORTS_PER_SOL,
                "nonce": swap.nonce,
            }
        )

    if args.json:
        print(json.dumps({"captures": rows, "count": len(rows)}, indent=2))
        return 0

    worst = max((r["error"] for r in rows), default=0.0)
    print(f"Replaying {len(rows)} captures from {args.tsv.name}\n")
    print(f"{'signature':12} {'venue':14} {'reserve':>12} {'sale/reserve':>13} "
          f"{'predicted':>10} {'observed':>10} {'err':>7} {'pool SOL':>12}")
    for row in sorted(rows, key=lambda r: -r["pool_sol"]):
        ratio = f"{row['sale_ratio']:,.1f}x" if row["sale_ratio"] is not None else "-"
        print(
            f"{row['sig'][:12]} {row['program']:14} {row['base_reserve']:>12,} {ratio:>13} "
            f"{row['predicted_capture'] * 100:>9.2f}% {row['observed_capture'] * 100:>9.2f}% "
            f"{row['error'] * 100:>6.2f}% {row['pool_sol']:>12.6f}"
        )
    nonce_sells = sum(1 for r in rows if r["nonce"])
    print(
        f"\n{len(rows)} captures. Worst error {worst * 100:.2f} percentage points "
        f"(the AMM's swap fee, which the bare identity omits)."
    )
    print(
        f"{nonce_sells}/{len(rows)} were durable-nonce transactions -- pre-signed and held "
        f"until a deposit landed."
    )
    return 0


# --- helpers --------------------------------------------------------------


def _amount(args) -> int:
    if getattr(args, "amount_lamports", None) is not None:
        return args.amount_lamports
    if getattr(args, "amount_sol", None) is not None:
        return int(round(args.amount_sol * LAMPORTS_PER_SOL))
    return DEFAULT_PROBE_LAMPORTS


def _service(
    endpoint: str,
    *,
    fail_closed: bool,
    with_overhang: bool,
    telemetry: Telemetry | None = None,
) -> AdvisoryService:
    client = JsonRpcClient(endpoint)
    return AdvisoryService(
        PoolStateIndexer(client),
        overhang=ClaimOverhangService(client) if with_overhang else None,
        telemetry=telemetry,
        thresholds=DEFAULT_THRESHOLDS,
        fail_closed=fail_closed,
    )


def _print_score(score, amount: int, *, pool: str) -> None:
    print(f"{_VERDICT_MARK[score.verdict]}  {pool}")
    print(f"  deposit scored          {amount / LAMPORTS_PER_SOL:.9f} SOL")
    print(f"  token reserve after buy {score.residual_base_reserve:,} raw units")
    print(f"  you would receive       {score.tokens_out:,} raw units "
          f"({score.pool_share_acquired * 100:.4f}% of the reserve)")
    print(f"  price impact            {score.price_impact * 100:.4f}%")
    print(f"  extractable from you    {score.extractable_fraction_of_deposit * 100:.2f}%")
    print(f"  adversary modelled      {score.adversary_model}")
    if score.claim_cost_lamports >= 0:
        print(f"  cost to claim 99%       "
              f"{score.claim_cost_lamports / LAMPORTS_PER_SOL:.9f} SOL")
    if score.signals:
        print(f"  signals                 {', '.join(score.signals)}")
    for line in score.rationale:
        print(f"  - {line}")


def _print_advisory(advisory) -> None:
    snapshot = advisory.snapshot
    print(f"{_VERDICT_MARK[advisory.verdict]}  {advisory.pool}")
    if snapshot is not None:
        print(f"  venue                   {snapshot.program}"
              f"{'  (orientation flipped)' if snapshot.orientation_flipped else ''}")
        print(f"  token mint              {snapshot.base_mint}")
        print(f"  reserves                {snapshot.base_reserve:,} raw / "
              f"{snapshot.quote_reserve_sol:.9f} SOL")
    print(f"  slot                    {advisory.snapshot_slot}")
    if advisory.score is not None:
        _print_score(advisory.score, advisory.quote_in, pool=advisory.pool)
    else:
        print(f"  {advisory.human_reason}")
    if advisory.overhang is not None:
        over = advisory.overhang
        ratio = "unbounded" if over.overhang_ratio is None else f"{over.overhang_ratio:,.0f}x"
        print(f"  holders                 {over.holder_count} "
              f"(largest {over.largest_external_balance:,} raw, {ratio} the reserve)")
    for warning in advisory.warnings:
        print(f"  ! {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
