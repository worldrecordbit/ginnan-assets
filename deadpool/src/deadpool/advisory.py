"""Pre-Trade Advisory API -- component 4 of the detection architecture.

The public surface. Answers "is it safe to buy into this pool right now?"
synchronously, before a user signs.

Three contract points from the specification drive the design:

*Every response carries the slot it was computed against.* Reserves can change
between the check and the inclusion of the user's transaction, so a caller has
to be able to see how fresh the answer is rather than take it on trust.

*The overhang service may be absent.* It is slower than the S1/S2 pool-state
signals and sits off the hot path, so a cold or failing overhang service
degrades the verdict rather than failing the request -- the response is marked
``degraded`` and the pool-state verdict stands on its own.

*Fail-closed is offered as an option.* With ``fail_closed=True`` a pool that
cannot be read returns ``caution``, not ``safe``. The asymmetry is deliberate:
a missed detection can cost a user their whole deposit, a false positive costs
one trade.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .constants import DEFAULT_THRESHOLDS, LAMPORTS_PER_SOL, Thresholds
from .indexer import PoolResolutionError, PoolStateIndexer
from .models import Advisory, Overhang, PoolSnapshot, Verdict
from .overhang import ClaimOverhangService
from .scorer import score_pool
from .telemetry import Telemetry

DEFAULT_PROBE_LAMPORTS = 100_000_000  # 0.1 SOL


@dataclass
class _CacheEntry:
    snapshot: PoolSnapshot
    stored_at: float


class AdvisoryService:
    """Composes indexer, scorer and overhang into one verdict."""

    def __init__(
        self,
        indexer: PoolStateIndexer,
        *,
        overhang: ClaimOverhangService | None = None,
        telemetry: Telemetry | None = None,
        thresholds: Thresholds = DEFAULT_THRESHOLDS,
        snapshot_ttl_s: float = 2.0,
        fail_closed: bool = False,
        clock=time.monotonic,
    ) -> None:
        self.indexer = indexer
        self.overhang = overhang
        self.telemetry = telemetry or Telemetry()
        self.thresholds = thresholds
        self.snapshot_ttl_s = snapshot_ttl_s
        self.fail_closed = fail_closed
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    # --- public API -------------------------------------------------------

    def advise(
        self,
        pool: str,
        quote_in: int = DEFAULT_PROBE_LAMPORTS,
        *,
        with_overhang: bool = True,
    ) -> Advisory:
        started = self._clock()
        warnings: list[str] = []
        degraded = False

        try:
            snapshot = self._snapshot(pool)
        except PoolResolutionError as exc:
            self.telemetry.record_error("pool_resolution")
            return self._unresolved(pool, quote_in, str(exc))
        except Exception as exc:  # transport, decoding, anything below us
            self.telemetry.record_error(type(exc).__name__)
            return self._unresolved(pool, quote_in, f"{type(exc).__name__}: {exc}")

        self.telemetry.observe_snapshot(snapshot)

        overhang: Overhang | None = None
        if with_overhang and self.overhang is not None:
            try:
                overhang = self.overhang.overhang(snapshot.base_mint, snapshot)
            except Exception as exc:
                # Off the hot path by design: its absence lowers confidence,
                # never the availability of a verdict.
                degraded = True
                warnings.append(f"overhang unavailable ({type(exc).__name__}); pool-state verdict only")
                self.telemetry.record_error("overhang_unavailable")
        elif with_overhang and self.overhang is None:
            degraded = True
            warnings.append("no overhang service configured; pool-state verdict only")
        elif not with_overhang:
            # An explicit opt-out is not a degradation: the caller chose this
            # contract, and marking it degraded would push every such request
            # to `caution` under fail-closed. It is still worth saying out
            # loud that the verdict rests on pool state alone.
            warnings.append("overhang lookup skipped by request; pool-state verdict only")

        score = score_pool(snapshot, quote_in, overhang=overhang, thresholds=self.thresholds)
        verdict = score.verdict
        if degraded and self.fail_closed and verdict is Verdict.SAFE:
            verdict = Verdict.CAUTION
            warnings.append("fail-closed: incomplete data cannot return safe")

        advisory = Advisory(
            pool=pool,
            verdict=verdict,
            capture_fraction=score.capture_fraction_at_1000x,
            residual_reserve=score.residual_base_reserve,
            human_reason=score.human_reason,
            snapshot_slot=snapshot.slot,
            quote_in=quote_in,
            score=score,
            snapshot=snapshot,
            overhang=overhang,
            degraded=degraded,
            warnings=tuple(warnings),
        )
        self.telemetry.record_advisory(advisory, latency_s=self._clock() - started)
        return advisory

    def advise_sol(self, pool: str, amount_sol: float, **kwargs) -> Advisory:
        return self.advise(pool, int(round(amount_sol * LAMPORTS_PER_SOL)), **kwargs)

    def invalidate(self, pool: str) -> None:
        with self._lock:
            self._cache.pop(pool, None)

    # --- internals --------------------------------------------------------

    def _snapshot(self, pool: str) -> PoolSnapshot:
        """Serve from cache only within the TTL.

        The TTL is short and exists to absorb bursts of identical requests,
        not to save round trips: a stale reserve is exactly the failure mode
        this system exists to catch.
        """
        now = self._clock()
        with self._lock:
            entry = self._cache.get(pool)
            if entry is not None and now - entry.stored_at < self.snapshot_ttl_s:
                return entry.snapshot
        snapshot = self.indexer.snapshot(pool)
        with self._lock:
            self._cache[pool] = _CacheEntry(snapshot, now)
        return snapshot

    def _unresolved(self, pool: str, quote_in: int, reason: str) -> Advisory:
        verdict = Verdict.CAUTION if self.fail_closed else Verdict.UNKNOWN
        advisory = Advisory(
            pool=pool,
            verdict=verdict,
            capture_fraction=0.0,
            residual_reserve=0,
            human_reason=f"Pool state unavailable: {reason}",
            snapshot_slot=0,
            quote_in=quote_in,
            degraded=True,
            warnings=("pool state could not be read",)
            + (("fail-closed: returning caution",) if self.fail_closed else ()),
        )
        self.telemetry.record_advisory(advisory)
        return advisory
