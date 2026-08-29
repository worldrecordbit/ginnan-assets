"""Detection of residual-liquidity capture on Solana AMMs.

A liquidity pool whose reserves have been withdrawn does not become inert. It
becomes a trap with a computable payoff: any subsequent deposit into it is
almost entirely recoverable by whoever holds the largest outstanding token
balance against that pool. The condition is visible from pool state alone,
before anyone commits funds.

This package implements the five components specified in
``docs/threat-model-dead-pool-extraction.md``:

===========================  ========================================
:mod:`deadpool.indexer`      1. Pool State Indexer
:mod:`deadpool.scorer`       2. Extractability Scorer
:mod:`deadpool.overhang`     3. Claim-Overhang Service
:mod:`deadpool.advisory`     4. Pre-Trade Advisory API
:mod:`deadpool.telemetry`    5. Telemetry & Alerting
===========================  ========================================

Quick start, with no network::

    >>> from deadpool import score_reserves
    >>> score_reserves(4, 37_810_000_011, 100_000_000).verdict
    <Verdict.UNSAFE: 'unsafe'>

and against a live endpoint::

    >>> from deadpool import AdvisoryService, JsonRpcClient, PoolStateIndexer
    >>> client = JsonRpcClient("https://your-endpoint")
    >>> service = AdvisoryService(PoolStateIndexer(client))
    >>> service.advise_sol(pool, 0.1).verdict            # doctest: +SKIP
"""

from .advisory import AdvisoryService
from .constants import DEFAULT_THRESHOLDS, LAMPORTS_PER_SOL, Thresholds
from .indexer import PoolResolutionError, PoolStateIndexer
from .models import Advisory, Overhang, PoolSnapshot, RiskScore, Verdict
from .overhang import Census, ClaimOverhangService
from .rpc import JsonRpcClient, RpcError
from .scorer import (
    capture_fraction,
    claim_cost_for_capture,
    quote_out,
    sale_size_for_capture,
    score_pool,
    score_reserves,
    simulate_capture,
    tokens_out,
)
from .telemetry import CaptureEvent, Telemetry, detect_capture

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # contracts
    "Advisory",
    "Census",
    "Overhang",
    "PoolSnapshot",
    "RiskScore",
    "Thresholds",
    "Verdict",
    "DEFAULT_THRESHOLDS",
    "LAMPORTS_PER_SOL",
    # components
    "AdvisoryService",
    "ClaimOverhangService",
    "PoolStateIndexer",
    "Telemetry",
    # scorer
    "capture_fraction",
    "claim_cost_for_capture",
    "quote_out",
    "sale_size_for_capture",
    "score_pool",
    "score_reserves",
    "simulate_capture",
    "tokens_out",
    # telemetry
    "CaptureEvent",
    "detect_capture",
    # transport
    "JsonRpcClient",
    "RpcError",
    "PoolResolutionError",
]
