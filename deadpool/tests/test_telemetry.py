"""Measurement, kept strictly off the protective path."""

from __future__ import annotations

import io
import json
import unittest

from deadpool.models import Advisory, RiskScore, Verdict
from deadpool.telemetry import CaptureEvent, Telemetry, detect_capture


def advisory(verdict: Verdict, *, signals=("S1",), degraded=False) -> Advisory:
    score = RiskScore(
        verdict=verdict,
        capture_fraction_at_1000x=0.999,
        residual_base_reserve=4,
        reserve_share_of_supply=4e-15,
        extractable_fraction_of_deposit=1.0,
        claim_cost_lamports=1,
        adversary_model="test",
        tokens_out=0,
        pool_share_acquired=1.0,
        price_impact=1.0,
        signals=signals,
        rationale=("because",),
    )
    return Advisory(
        pool="POOL",
        verdict=verdict,
        capture_fraction=0.999,
        residual_reserve=4,
        human_reason="because",
        snapshot_slot=442_634_543,
        quote_in=100_000_000,
        score=score,
        degraded=degraded,
    )


class CounterTests(unittest.TestCase):
    def test_verdicts_and_signals_accumulate(self):
        telemetry = Telemetry()
        telemetry.record_advisory(advisory(Verdict.UNSAFE))
        telemetry.record_advisory(advisory(Verdict.UNSAFE, signals=("S2",)))
        telemetry.record_advisory(advisory(Verdict.SAFE, signals=()))
        metrics = telemetry.snapshot_metrics()
        self.assertEqual(metrics["advisories"], 3)
        self.assertEqual(metrics["verdicts"], {"unsafe": 2, "safe": 1})
        self.assertEqual(metrics["signals"], {"S1": 1, "S2": 1})

    def test_only_unsafe_verdicts_enter_the_recent_list(self):
        telemetry = Telemetry()
        telemetry.record_advisory(advisory(Verdict.SAFE, signals=()))
        telemetry.record_advisory(advisory(Verdict.UNSAFE))
        recent = telemetry.snapshot_metrics()["recent_unsafe"]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["slot"], 442_634_543)

    def test_degraded_verdicts_are_counted_separately(self):
        telemetry = Telemetry()
        telemetry.record_advisory(advisory(Verdict.UNKNOWN, signals=(), degraded=True))
        self.assertEqual(telemetry.snapshot_metrics()["degraded"], 1)

    def test_latency_percentiles_are_reported_once_there_are_samples(self):
        telemetry = Telemetry()
        self.assertIsNone(telemetry.snapshot_metrics()["latency_p50_ms"])
        for latency in (0.001, 0.002, 0.05):
            telemetry.record_advisory(advisory(Verdict.SAFE, signals=()), latency_s=latency)
        metrics = telemetry.snapshot_metrics()
        self.assertGreater(metrics["latency_p99_ms"], 0)
        self.assertLessEqual(metrics["latency_p50_ms"], metrics["latency_p99_ms"])

    def test_prometheus_text_is_well_formed(self):
        telemetry = Telemetry()
        telemetry.record_advisory(advisory(Verdict.UNSAFE), latency_s=0.01)
        text = telemetry.prometheus()
        self.assertIn('deadpool_verdicts_total{verdict="unsafe"} 1', text)
        self.assertIn('deadpool_signals_total{signal="S1"} 1', text)
        self.assertTrue(text.endswith("\n"))
        for line in text.splitlines():
            self.assertTrue(line.startswith("#") or " " in line)


class EventLogTests(unittest.TestCase):
    def test_unsafe_verdicts_and_captures_are_written_as_jsonl(self):
        sink = io.StringIO()
        telemetry = Telemetry(event_log=sink)
        telemetry.record_advisory(advisory(Verdict.UNSAFE))
        telemetry.record_capture(CaptureEvent("POOL", 100, 1, 0.99, slot=7))
        lines = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([entry["type"] for entry in lines], ["unsafe", "capture"])
        self.assertEqual(lines[1]["slot"], 7)

    def test_safe_verdicts_are_not_logged(self):
        sink = io.StringIO()
        Telemetry(event_log=sink).record_advisory(advisory(Verdict.SAFE, signals=()))
        self.assertEqual(sink.getvalue(), "")

    def test_a_borrowed_sink_is_not_closed(self):
        sink = io.StringIO()
        telemetry = Telemetry(event_log=sink)
        telemetry.close()
        self.assertFalse(sink.closed)


class CaptureDetectionTests(unittest.TestCase):
    """Signal S9, from reserve deltas rather than log strings."""

    def test_a_full_drain_is_detected(self):
        event = detect_capture("POOL", 37_810_000_011, 37_584_494)
        self.assertIsNotNone(event)
        self.assertAlmostEqual(event.captured_fraction, 0.999, places=3)

    def test_an_ordinary_sale_is_not(self):
        self.assertIsNone(detect_capture("POOL", 100 * 10**9, 95 * 10**9))

    def test_a_deposit_is_not_a_capture(self):
        # Quote going up is an inflow. A detector that took absolute deltas
        # would report every victim's buy as a capture.
        self.assertIsNone(detect_capture("POOL", 100, 200))

    def test_an_empty_pool_cannot_be_captured(self):
        self.assertIsNone(detect_capture("POOL", 0, 0))

    def test_the_threshold_is_adjustable(self):
        self.assertIsNone(detect_capture("POOL", 1000, 100))
        self.assertIsNotNone(detect_capture("POOL", 1000, 100, threshold=0.5))


if __name__ == "__main__":
    unittest.main()
