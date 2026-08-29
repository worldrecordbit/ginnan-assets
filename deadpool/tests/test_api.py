"""The HTTP surface, exercised over a real socket."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from deadpool.advisory import AdvisoryService
from deadpool.api import make_server
from deadpool.constants import LAMPORTS_PER_SOL
from deadpool.indexer import PoolStateIndexer
from deadpool.overhang import ClaimOverhangService
from deadpool.spl import encode_mint

from .support import MockChain, pubkey

SOL = LAMPORTS_PER_SOL


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = MockChain()
        mint = cls.chain.add_mint("TOKEN", 6)
        cls.chain.accounts[mint].data = encode_mint(6, supply=10**15)
        cls.corpse = cls.chain.add_pumpswap_pool(
            "corpse", token_mint=mint, base_reserve=4, quote_reserve=37_810_000_011
        )
        live_mint = cls.chain.add_mint("LIVE", 6)
        cls.chain.accounts[live_mint].data = encode_mint(6, supply=10**15)
        cls.live = cls.chain.add_pumpswap_pool(
            "live",
            token_mint=live_mint,
            base_reserve=145_816_924_891_423,
            quote_reserve=345_500_000_000,
        )
        client = cls.chain.client()
        cls.service = AdvisoryService(
            PoolStateIndexer(client), overhang=ClaimOverhangService(client)
        )
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    def get_raw(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode()

    # --- routes -----------------------------------------------------------

    def test_health(self):
        self.assertEqual(self.get("/health"), (200, {"status": "ok"}))

    def test_advisory_on_a_corpse(self):
        status, body = self.get(f"/v1/advisory?pool={self.corpse}&amount_sol=0.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["verdict"], "unsafe")
        self.assertEqual(body["residual_reserve"], 4)
        self.assertIn("S1", body["score"]["signals"])
        self.assertTrue(body["human_reason"])

    def test_advisory_on_a_live_pool(self):
        _, body = self.get(f"/v1/advisory?pool={self.live}&amount_sol=0.1")
        self.assertEqual(body["verdict"], "safe")

    def test_an_unsafe_verdict_is_served_as_200(self):
        # It is a successful answer to the question asked. A 4xx would push
        # callers toward treating it as a transport failure to retry past.
        status, _ = self.get(f"/v1/advisory?pool={self.corpse}")
        self.assertEqual(status, 200)

    def test_snapshot_slot_is_always_present(self):
        _, body = self.get(f"/v1/advisory?pool={self.live}")
        self.assertEqual(body["snapshot_slot"], self.chain.slot)

    def test_pool_route_returns_state_without_a_verdict_lookup(self):
        status, body = self.get(f"/v1/pool/{self.live}")
        self.assertEqual(status, 200)
        self.assertEqual(body["snapshot"]["base_reserve"], 145_816_924_891_423)
        self.assertEqual(body["snapshot"]["quote_mint"],
                         "So11111111111111111111111111111111111111112")

    def test_pool_route_404s_on_an_unknown_pool(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v1/pool/{pubkey('nope')}")
        self.assertEqual(caught.exception.code, 404)

    def test_metrics_are_prometheus_text(self):
        self.get(f"/v1/advisory?pool={self.corpse}")
        status, body = self.get_raw("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("deadpool_advisories_total", body)
        self.assertIn('deadpool_verdicts_total{verdict="unsafe"}', body)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)

    # --- parameter handling ----------------------------------------------

    def test_missing_pool_is_a_400(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/v1/advisory")
        self.assertEqual(caught.exception.code, 400)

    def test_both_amount_spellings_are_rejected_together(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v1/advisory?pool={self.live}&amount_sol=1&amount_lamports=1")
        self.assertEqual(caught.exception.code, 400)

    def test_a_non_numeric_amount_is_a_400(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v1/advisory?pool={self.live}&amount_sol=lots")
        self.assertEqual(caught.exception.code, 400)

    def test_a_negative_amount_is_a_400(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(f"/v1/advisory?pool={self.live}&amount_lamports=-1")
        self.assertEqual(caught.exception.code, 400)

    def test_lamports_and_sol_agree(self):
        _, by_sol = self.get(f"/v1/advisory?pool={self.live}&amount_sol=0.25")
        _, by_lamports = self.get(f"/v1/advisory?pool={self.live}&amount_lamports=250000000")
        self.assertEqual(by_sol["quote_in"], by_lamports["quote_in"])

    def test_overhang_can_be_switched_off_by_query(self):
        # Opting out is a choice, not a degradation -- but the response says
        # plainly that the verdict rests on pool state alone.
        _, body = self.get(f"/v1/advisory?pool={self.live}&overhang=0")
        self.assertIsNone(body["overhang"])
        self.assertFalse(body["degraded"])
        self.assertTrue(any("skipped by request" in w for w in body["warnings"]))

    def test_an_unresolvable_pool_returns_unknown_not_an_error(self):
        status, body = self.get(f"/v1/advisory?pool={pubkey('ghost')}")
        self.assertEqual(status, 200)
        self.assertEqual(body["verdict"], "unknown")

    def test_responses_are_not_cacheable(self):
        # A cached advisory is a stale advisory.
        with urllib.request.urlopen(f"{self.base}/v1/advisory?pool={self.live}") as response:
            self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
