"""The command line, including the two commands that need no network."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from deadpool import cli, evidence
from deadpool.base58 import b58decode
from deadpool.cli import main
from deadpool.constants import TOKEN_2022_PROGRAM
from deadpool.spl import encode_mint, encode_token_account

from .support import MockChain, pubkey


def run(argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class OfflineScoreTests(unittest.TestCase):
    """``score`` from raw reserves touches no RPC at all."""

    def test_a_corpse_exits_two_and_explains_itself(self):
        code, out, _ = run(
            ["score", "--base-reserve", "4", "--quote-reserve", "37810000011",
             "--amount-sol", "0.1"]
        )
        self.assertEqual(code, 2)
        self.assertIn("UNSAFE", out)
        self.assertIn("depletion floor", out)

    def test_a_live_pool_exits_zero(self):
        code, out, _ = run(
            ["score", "--base-reserve", "145816924891423",
             "--quote-reserve", "345500000000", "--amount-sol", "0.1"]
        )
        self.assertEqual(code, 0)
        self.assertIn("SAFE", out)

    def test_a_thin_pool_exits_one(self):
        # 0.15 SOL of reserve -- above the depletion floor, so not a trap,
        # but a 0.2 SOL deposit into it is a 57% price impact.
        code, out, _ = run(
            ["score", "--base-reserve", "900000000000000",
             "--quote-reserve", "150000000", "--amount-sol", "0.2"]
        )
        self.assertEqual(code, 1)
        self.assertIn("CAUTION", out)
        self.assertIn("price impact", out)

    def test_json_output_is_machine_readable(self):
        code, out, _ = run(
            ["score", "--base-reserve", "4", "--quote-reserve", "37810000011", "--json"]
        )
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "unsafe")
        self.assertEqual(payload["residual_base_reserve"], 4)
        self.assertIn("S1", payload["signals"])

    def test_lamports_and_sol_amounts_agree(self):
        args = ["score", "--base-reserve", "10", "--quote-reserve", "1000000", "--json"]
        by_sol = json.loads(run(args + ["--amount-sol", "0.25"])[1])
        by_lamports = json.loads(run(args + ["--amount-lamports", "250000000"])[1])
        self.assertEqual(by_sol, by_lamports)

    def test_half_specified_reserves_are_a_usage_error(self):
        code, _, err = run(["score", "--base-reserve", "4"])
        self.assertEqual(code, 4)
        self.assertIn("both", err)

    def test_pool_and_reserves_together_are_a_usage_error(self):
        code, _, err = run(
            ["score", "--pool", "X", "--base-reserve", "4", "--quote-reserve", "5"]
        )
        self.assertEqual(code, 4)
        self.assertIn("not both", err)

    def test_no_target_at_all_is_a_usage_error(self):
        code, _, err = run(["score"])
        self.assertEqual(code, 4)
        self.assertIn("--pool", err)


class ReplayTests(unittest.TestCase):
    """``replay`` validates the identity against the shipped record."""

    def test_replay_covers_every_capture_and_reports_the_error(self):
        code, out, _ = run(["replay"])
        self.assertEqual(code, 0)
        self.assertIn("Replaying 32 captures", out)
        self.assertIn("Worst error", out)
        self.assertIn("durable-nonce", out)

    def test_replay_json_carries_predicted_against_observed(self):
        code, out, _ = run(["replay", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 32)
        for row in payload["captures"]:
            self.assertLess(row["error"], 0.0025)
            self.assertIn("sig", row)

    def test_replay_accepts_an_explicit_file(self):
        code, out, _ = run(["replay", "--tsv", str(evidence.M3MX_TRANSACTIONS)])
        self.assertEqual(code, 0)
        self.assertIn("captures", out)


class LiveCommandTests(unittest.TestCase):
    """The RPC-backed commands, with the transport swapped for a mock chain."""

    def setUp(self):
        self.chain = MockChain()
        mint = self.chain.add_mint("TOKEN", 6)
        self.chain.accounts[mint].data = encode_mint(6, supply=10**15)
        self.pool = self.chain.add_pumpswap_pool(
            "corpse", token_mint=mint, base_reserve=4, quote_reserve=37_810_000_011
        )
        self.wallet = pubkey("operator")
        for bucket in (0, 128):
            for i in range(3):
                mint_bytes = bytes([(i * 7) % 251, bucket]) + i.to_bytes(30, "big")
                self.chain.add(
                    pubkey(f"acct/{bucket}/{i}"),
                    TOKEN_2022_PROGRAM,
                    encode_token_account(mint_bytes, b58decode(self.wallet), 1),
                )

        chain = self.chain
        original = cli.JsonRpcClient

        def factory(endpoint, **kwargs):
            return original(endpoint, transport=chain, **kwargs)

        cli.JsonRpcClient = factory
        self.addCleanup(setattr, cli, "JsonRpcClient", original)

    def test_score_against_a_pool_reads_reserves_and_exits_two(self):
        code, out, _ = run(["score", "--pool", self.pool, "--amount-sol", "0.1"])
        self.assertEqual(code, 2)
        self.assertIn("UNSAFE", out)
        self.assertIn("pumpswap", out)
        self.assertIn(str(self.chain.slot), out)

    def test_score_json_carries_the_snapshot(self):
        code, out, _ = run(["score", "--pool", self.pool, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["snapshot"]["base_reserve"], 4)
        self.assertEqual(payload["snapshot_slot"], self.chain.slot)

    def test_an_unknown_pool_exits_three(self):
        code, _, _ = run(["score", "--pool", pubkey("ghost")])
        self.assertEqual(code, 3)

    def test_census_prints_a_book_estimate(self):
        code, out, _ = run(["census", "--wallet", self.wallet, "--buckets", "2"])
        self.assertEqual(code, 0)
        self.assertIn("estimated claims  768", out)
        self.assertIn("rent locked", out)
        self.assertIn("bucket dispersion", out)

    def test_census_json(self):
        code, out, _ = run(["census", "--wallet", self.wallet, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["estimated_claims"], 768)
        self.assertIn("rent_locked_sol", payload)


class ParserTests(unittest.TestCase):
    def test_a_command_is_required(self):
        with self.assertRaises(SystemExit):
            run([])

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            run(["explode"])


if __name__ == "__main__":
    unittest.main()
