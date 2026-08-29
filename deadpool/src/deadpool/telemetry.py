"""Telemetry & Alerting -- component 5 of the detection architecture.

Everything that is measurement rather than protection: how many pools entered
the unsafe state, how many captures were observed, how stale the data is, and
what the detector itself is doing.

**Strictly read-only and off the hot path.** Nothing here may influence a
pre-trade verdict; the protective path stays simple and auditable, and this
component only ever observes it. The recorder is therefore given verdicts
after the fact and has no way to hand anything back.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, TextIO

from .models import Advisory, PoolSnapshot, Verdict


@dataclass
class CaptureEvent:
    """Signal S9: a swap taking >=99% of a pool's pre-transaction reserve."""

    pool: str
    quote_before: int
    quote_after: int
    captured_fraction: float
    slot: int = 0
    signature: str | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "capture",
            "pool": self.pool,
            "quote_before": self.quote_before,
            "quote_after": self.quote_after,
            "captured_fraction": self.captured_fraction,
            "slot": self.slot,
            "signature": self.signature,
            "ts": self.ts,
        }


class Telemetry:
    """In-memory counters plus an optional JSONL event log.

    Thread-safe: the HTTP API serves requests on threads and all of them
    record here.
    """

    def __init__(self, *, event_log: Path | TextIO | None = None, recent: int = 256) -> None:
        self._lock = threading.Lock()
        self.verdicts: Counter[str] = Counter()
        self.signals: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.degraded = 0
        self.advisories = 0
        self.captures = 0
        self._recent_unsafe: Deque[dict[str, Any]] = deque(maxlen=recent)
        self._latencies: Deque[float] = deque(maxlen=1024)
        self._sink: TextIO | None = None
        self._owns_sink = False
        if isinstance(event_log, Path):
            self._sink = event_log.open("a", encoding="utf-8")
            self._owns_sink = True
        elif event_log is not None:
            self._sink = event_log

    # --- recording --------------------------------------------------------

    def record_advisory(self, advisory: Advisory, *, latency_s: float | None = None) -> None:
        with self._lock:
            self.advisories += 1
            self.verdicts[advisory.verdict.value] += 1
            if advisory.degraded:
                self.degraded += 1
            if latency_s is not None:
                self._latencies.append(latency_s)
            if advisory.score is not None:
                for signal in advisory.score.signals:
                    self.signals[signal] += 1
            if advisory.verdict is Verdict.UNSAFE:
                entry = {
                    "type": "unsafe",
                    "pool": advisory.pool,
                    "slot": advisory.snapshot_slot,
                    "reason": advisory.human_reason,
                    "ts": time.time(),
                }
                self._recent_unsafe.append(entry)
                self._emit(entry)

    def record_capture(self, event: CaptureEvent) -> None:
        with self._lock:
            self.captures += 1
            self._emit(event.to_dict())

    def record_error(self, kind: str) -> None:
        with self._lock:
            self.errors[kind] += 1

    def observe_snapshot(self, snapshot: PoolSnapshot) -> None:
        """Record structural facts worth counting across the pool universe."""
        with self._lock:
            if snapshot.orientation_flipped:
                self.signals["orientation_flipped"] += 1

    # --- reporting --------------------------------------------------------

    def snapshot_metrics(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(self._latencies)
            return {
                "advisories": self.advisories,
                "verdicts": dict(self.verdicts),
                "signals": dict(self.signals),
                "errors": dict(self.errors),
                "degraded": self.degraded,
                "captures": self.captures,
                "latency_p50_ms": _percentile(latencies, 0.50) * 1000 if latencies else None,
                "latency_p99_ms": _percentile(latencies, 0.99) * 1000 if latencies else None,
                "recent_unsafe": list(self._recent_unsafe)[-10:],
            }

    def prometheus(self) -> str:
        """Text-format metrics, for anything that scrapes."""
        metrics = self.snapshot_metrics()
        lines = [
            "# HELP deadpool_advisories_total Advisory requests served.",
            "# TYPE deadpool_advisories_total counter",
            f"deadpool_advisories_total {metrics['advisories']}",
            "# HELP deadpool_verdicts_total Advisory verdicts by outcome.",
            "# TYPE deadpool_verdicts_total counter",
        ]
        for verdict, count in sorted(metrics["verdicts"].items()):
            lines.append(f'deadpool_verdicts_total{{verdict="{verdict}"}} {count}')
        lines += [
            "# HELP deadpool_signals_total Detection signals fired.",
            "# TYPE deadpool_signals_total counter",
        ]
        for signal, count in sorted(metrics["signals"].items()):
            lines.append(f'deadpool_signals_total{{signal="{signal}"}} {count}')
        lines += [
            "# HELP deadpool_degraded_total Verdicts reached on incomplete data.",
            "# TYPE deadpool_degraded_total counter",
            f"deadpool_degraded_total {metrics['degraded']}",
            "# HELP deadpool_captures_total Capture events observed (S9).",
            "# TYPE deadpool_captures_total counter",
            f"deadpool_captures_total {metrics['captures']}",
        ]
        for name, key in (("p50", "latency_p50_ms"), ("p99", "latency_p99_ms")):
            if metrics[key] is not None:
                lines += [
                    f"# HELP deadpool_latency_{name}_ms Advisory latency, {name}.",
                    f"# TYPE deadpool_latency_{name}_ms gauge",
                    f"deadpool_latency_{name}_ms {metrics[key]:.3f}",
                ]
        return "\n".join(lines) + "\n"

    # --- lifecycle --------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._sink is None:
            return
        self._sink.write(json.dumps(payload) + "\n")
        self._sink.flush()

    def close(self) -> None:
        if self._sink is not None and self._owns_sink:
            self._sink.close()
        self._sink = None


def detect_capture(
    pool: str,
    quote_before: int,
    quote_after: int,
    *,
    threshold: float = 0.99,
    slot: int = 0,
    signature: str | None = None,
) -> CaptureEvent | None:
    """Signal S9, from a pair of pre/post quote reserves.

    Deltas, never log strings: in pools where wrapped SOL is the program's
    base mint the instruction name is the opposite of the economic direction
    (threat model 3.4), so a detector keyed on logs misses a real fraction of
    captures and invents others.
    """
    if quote_before <= 0 or quote_after > quote_before:
        return None
    fraction = (quote_before - quote_after) / quote_before
    if fraction < threshold:
        return None
    return CaptureEvent(
        pool=pool,
        quote_before=quote_before,
        quote_after=quote_after,
        captured_fraction=fraction,
        slot=slot,
        signature=signature,
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(q * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]
