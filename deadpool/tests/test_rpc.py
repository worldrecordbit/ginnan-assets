"""The transport layer: batching, retries and error handling."""

from __future__ import annotations

import json
import unittest
import urllib.error

from deadpool.base58 import b58decode, b58encode
from deadpool.rpc import (
    HttpTransport,
    JsonRpcClient,
    RpcError,
    encode_u8_filter,
    memcmp,
    validate_pubkey,
)

from .support import MockChain, pubkey


class BatchTests(unittest.TestCase):
    def test_results_are_matched_by_id_not_by_arrival_order(self):
        # A batch response is not required to come back in request order.
        # Zipping it positionally would attribute one pool's reserves to
        # another -- silently, and only under load.
        def shuffling_transport(payload):
            responses = [
                {"jsonrpc": "2.0", "id": item["id"], "result": item["params"][0]}
                for item in payload
            ]
            return list(reversed(responses))

        client = JsonRpcClient("mock://", transport=shuffling_transport)
        results = client.batch([("m", ["a"]), ("m", ["b"]), ("m", ["c"])])
        self.assertEqual(results, ["a", "b", "c"])

    def test_an_empty_batch_makes_no_call(self):
        def transport(payload):
            raise AssertionError("should not be called")

        self.assertEqual(JsonRpcClient("mock://", transport=transport).batch([]), [])

    def test_a_missing_response_is_an_error(self):
        client = JsonRpcClient("mock://", transport=lambda payload: [])
        with self.assertRaises(RpcError):
            client.call("getSlot")

    def test_an_application_error_is_raised_with_its_code(self):
        def transport(payload):
            return [
                {
                    "jsonrpc": "2.0",
                    "id": payload[0]["id"],
                    "error": {"code": -32602, "message": "bad params"},
                }
            ]

        with self.assertRaises(RpcError) as caught:
            JsonRpcClient("mock://", transport=transport).call("getSlot")
        self.assertEqual(caught.exception.code, -32602)


class AccountFetchTests(unittest.TestCase):
    def setUp(self):
        self.chain = MockChain()
        self.client = self.chain.client()

    def test_more_than_a_hundred_keys_are_chunked(self):
        keys = [pubkey(f"k{i}") for i in range(250)]
        for key in keys:
            self.chain.add(key, "owner", b"x")
        results = self.client.get_multiple_accounts(keys)
        self.assertEqual(len(results), 250)
        self.assertEqual(self.chain.calls.count("getMultipleAccounts"), 3)

    def test_missing_accounts_come_back_as_none_in_place(self):
        present = self.chain.add(pubkey("a"), "owner", b"data")
        results = self.client.get_multiple_accounts([present, pubkey("missing"), present])
        self.assertIsNotNone(results[0])
        self.assertIsNone(results[1])
        self.assertIsNotNone(results[2])

    def test_data_is_base64_decoded(self):
        address = self.chain.add(pubkey("a"), "owner", b"\x00\x01\x02")
        self.assertEqual(self.client.get_account_info(address).data, b"\x00\x01\x02")

    def test_json_parsed_data_is_refused_rather_than_misread(self):
        def transport(payload):
            return [
                {
                    "jsonrpc": "2.0",
                    "id": payload[0]["id"],
                    "result": {
                        "context": {"slot": 1},
                        "value": [{"owner": "o", "lamports": 1, "data": {"parsed": {}}}],
                    },
                }
            ]

        client = JsonRpcClient("mock://", transport=transport)
        with self.assertRaises(RpcError):
            client.get_multiple_accounts(["x"])

    def test_program_accounts_respect_a_data_slice(self):
        self.chain.add(pubkey("big"), "prog", bytes(range(200)))
        found = self.chain.client().get_program_accounts("prog", data_slice=(10, 4))
        self.assertEqual(found[0].data, bytes([10, 11, 12, 13]))


class RetryTests(unittest.TestCase):
    """The retry loop, exercised through the seam ``_post`` provides."""

    @staticmethod
    def _transport(responses, **kwargs):
        """A transport whose single attempt yields ``responses`` in turn.

        Each entry is either an exception to raise or a value to return; the
        last entry repeats once the script runs out.
        """
        attempts = []

        class Scripted(HttpTransport):
            def _post(self, body):
                attempts.append(body)
                outcome = responses[min(len(attempts) - 1, len(responses) - 1)]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return Scripted("mock://", **kwargs), attempts

    def test_transient_statuses_are_retried_with_exponential_backoff(self):
        slept = []
        transport, attempts = self._transport(
            [
                urllib.error.HTTPError("u", 429, "slow down", None, None),
                urllib.error.HTTPError("u", 503, "unavailable", None, None),
                {"jsonrpc": "2.0", "id": 1, "result": 7},
            ],
            max_retries=4,
            sleep=slept.append,
        )
        client = JsonRpcClient("mock://", transport=transport)
        self.assertEqual(client.call("getSlot"), 7)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept, [0.5, 1.0])

    def test_network_errors_are_retried_too(self):
        slept = []
        transport, attempts = self._transport(
            [urllib.error.URLError("no route"), {"jsonrpc": "2.0", "id": 1, "result": 1}],
            sleep=slept.append,
        )
        self.assertEqual(JsonRpcClient("mock://", transport=transport).call("getSlot"), 1)
        self.assertEqual(len(attempts), 2)

    def test_a_non_transient_status_is_not_retried(self):
        # Retrying a 400 just burns the rate limit: it is a real answer, not
        # a transient failure.
        slept = []
        transport, attempts = self._transport(
            [urllib.error.HTTPError("u", 400, "bad request", None, None)], sleep=slept.append
        )
        with self.assertRaises(RpcError):
            JsonRpcClient("mock://", transport=transport).call("getSlot")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(slept, [])

    def test_retries_are_bounded_and_then_it_gives_up(self):
        slept = []
        transport, attempts = self._transport(
            [urllib.error.URLError("no route")], max_retries=2, sleep=slept.append
        )
        with self.assertRaises(RpcError):
            JsonRpcClient("mock://", transport=transport).call("getSlot")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept, [0.5, 1.0])

    def test_malformed_json_is_treated_as_transient(self):
        slept = []
        transport, attempts = self._transport(
            [
                json.JSONDecodeError("bad", "", 0),
                {"jsonrpc": "2.0", "id": 1, "result": "ok"},
            ],
            sleep=slept.append,
        )
        self.assertEqual(JsonRpcClient("mock://", transport=transport).call("getSlot"), "ok")
        self.assertEqual(len(attempts), 2)


class FilterTests(unittest.TestCase):
    def test_single_byte_filters_round_trip(self):
        for value in (0, 1, 57, 58, 200, 255):
            with self.subTest(value=value):
                spec = encode_u8_filter(1, value)
                self.assertEqual(b58decode(spec["memcmp"]["bytes"]), bytes([value]))

    def test_byte_values_are_bounded(self):
        with self.assertRaises(ValueError):
            encode_u8_filter(1, 256)

    def test_memcmp_passes_base58_through(self):
        key = pubkey("x")
        self.assertEqual(memcmp(32, key)["memcmp"]["bytes"], key)

    def test_pubkey_validation(self):
        self.assertEqual(validate_pubkey(pubkey("x")), pubkey("x"))
        with self.assertRaises(ValueError):
            validate_pubkey("abc")


class Base58Tests(unittest.TestCase):
    def test_round_trip(self):
        for name in ("a", "b", "operator", "pool"):
            raw = b58decode(pubkey(name))
            self.assertEqual(len(raw), 32)
            self.assertEqual(b58encode(raw), pubkey(name))

    def test_leading_zero_bytes_survive(self):
        # Encoded as leading '1's, not dropped. Get this wrong and any
        # address with a zero first byte decodes to 31 bytes.
        raw = b"\x00\x00" + bytes(range(30))
        self.assertEqual(b58decode(b58encode(raw)), raw)

    def test_empty_input(self):
        self.assertEqual(b58encode(b""), "")
        self.assertEqual(b58decode(""), b"")

    def test_invalid_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            b58decode("0OIl")


if __name__ == "__main__":
    unittest.main()
