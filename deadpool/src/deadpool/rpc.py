"""A minimal Solana JSON-RPC client. Stdlib only.

The client owns two things worth having in one place: batching (the indexer
resolves vaults and mints in groups, and one HTTP round trip beats four) and
retry with backoff on the 429s a public endpoint will hand you.

Transport is injected, so every component above this one can be tested with
no network at all -- see ``tests/support.py`` for the mock used throughout the
test suite.
"""

from __future__ import annotations

import itertools
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .base58 import b58decode

Transport = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]

DEFAULT_ENDPOINT = "https://api.mainnet-beta.solana.com"
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
#: getMultipleAccounts caps at 100 keys per call.
MAX_MULTIPLE_ACCOUNTS = 100


class RpcError(RuntimeError):
    """A JSON-RPC error response, or a transport failure that outlived retries."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AccountInfo:
    """One account, with its data already base64-decoded."""

    pubkey: str
    owner: str
    lamports: int
    data: bytes
    executable: bool = False
    rent_epoch: int = 0
    slot: int = 0


class HttpTransport:
    """urllib-backed transport with exponential backoff.

    Retries only on the status codes and network errors that are plausibly
    transient. A JSON-RPC *application* error is not retried -- it is a real
    answer and retrying it just burns the rate limit.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    def __call__(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        body = json.dumps(payload if len(payload) > 1 else payload[0]).encode()
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                decoded = self._post(body)
                return decoded if isinstance(decoded, list) else [decoded]
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in _RETRY_STATUS:
                    raise RpcError(f"HTTP {exc.code} from {self.endpoint}: {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
            if attempt < self.max_retries:
                self._sleep(self.backoff_base * (2 ** attempt))
        raise RpcError(f"{self.endpoint} unreachable after {self.max_retries} retries: {last}")

    def _post(self, body: bytes) -> Any:
        """One attempt. Separated from the retry loop so the loop is testable."""
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())


class JsonRpcClient:
    """Typed wrappers over the handful of RPC methods this system needs."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        transport: Transport | None = None,
        commitment: str = "confirmed",
    ) -> None:
        self.endpoint = endpoint
        self.commitment = commitment
        self._transport: Transport = transport or HttpTransport(endpoint)
        self._ids = itertools.count(1)

    # --- plumbing ---------------------------------------------------------

    def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        return self.batch([(method, params or [])])[0]

    def batch(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Issue calls as one JSON-RPC batch and return results in order.

        Responses are re-ordered by id: a batch response is not required to
        come back in request order, and treating it as though it were is a
        subtle way to attribute one pool's reserves to another.
        """
        if not calls:
            return []
        payload = [
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": list(params)}
            for method, params in calls
        ]
        raw = self._transport(payload)
        by_id = {item.get("id"): item for item in raw}
        results = []
        for request in payload:
            item = by_id.get(request["id"])
            if item is None:
                raise RpcError(f"no response for {request['method']} (id {request['id']})")
            if "error" in item:
                err = item["error"]
                raise RpcError(f"{request['method']}: {err.get('message')}", err.get("code"))
            results.append(item.get("result"))
        return results

    # --- methods ----------------------------------------------------------

    def get_slot(self) -> int:
        return int(self.call("getSlot", [{"commitment": self.commitment}]))

    def get_health(self) -> str:
        return self.call("getHealth", [])

    def get_account_info(self, pubkey: str) -> AccountInfo | None:
        return self.get_multiple_accounts([pubkey])[0]

    def get_multiple_accounts(self, pubkeys: Sequence[str]) -> list[AccountInfo | None]:
        """Fetch accounts, chunked to the 100-key server limit."""
        out: list[AccountInfo | None] = []
        for chunk in _chunks(list(pubkeys), MAX_MULTIPLE_ACCOUNTS):
            result = self.call(
                "getMultipleAccounts",
                [list(chunk), {"encoding": "base64", "commitment": self.commitment}],
            )
            slot = int((result or {}).get("context", {}).get("slot", 0))
            values = (result or {}).get("value") or [None] * len(chunk)
            for key, value in zip(chunk, values):
                out.append(_to_account(key, value, slot))
        return out

    def get_token_accounts_by_owner(self, owner: str, program_id: str) -> list[AccountInfo]:
        result = self.call(
            "getTokenAccountsByOwner",
            [
                owner,
                {"programId": program_id},
                {"encoding": "base64", "commitment": self.commitment},
            ],
        )
        slot = int((result or {}).get("context", {}).get("slot", 0))
        found = []
        for item in (result or {}).get("value") or []:
            account = _to_account(item["pubkey"], item["account"], slot)
            if account is not None:
                found.append(account)
        return found

    def get_program_accounts(
        self,
        program_id: str,
        *,
        filters: Iterable[dict[str, Any]] | None = None,
        data_slice: tuple[int, int] | None = None,
        with_context: bool = True,
    ) -> list[AccountInfo]:
        config: dict[str, Any] = {"encoding": "base64", "commitment": self.commitment}
        if filters:
            config["filters"] = list(filters)
        if data_slice is not None:
            config["dataSlice"] = {"offset": data_slice[0], "length": data_slice[1]}
        if with_context:
            config["withContext"] = True
        result = self.call("getProgramAccounts", [program_id, config])
        if isinstance(result, dict):
            slot = int(result.get("context", {}).get("slot", 0))
            items = result.get("value") or []
        else:
            slot, items = 0, result or []
        found = []
        for item in items:
            account = _to_account(item["pubkey"], item["account"], slot)
            if account is not None:
                found.append(account)
        return found


# --- filter helpers -------------------------------------------------------


def memcmp(offset: int, value_base58: str) -> dict[str, Any]:
    """A ``memcmp`` filter. ``value_base58`` is passed through as base58."""
    return {"memcmp": {"offset": offset, "bytes": value_base58}}


def data_size(size: int) -> dict[str, Any]:
    return {"dataSize": size}


def _to_account(pubkey: str, value: dict[str, Any] | None, slot: int) -> AccountInfo | None:
    if not value:
        return None
    import base64

    data = value.get("data")
    if isinstance(data, list):
        raw = base64.b64decode(data[0]) if data[0] else b""
    elif isinstance(data, str):
        raw = base64.b64decode(data) if data else b""
    else:  # jsonParsed -- not requested anywhere, but do not silently corrupt
        raise RpcError("unexpected jsonParsed account data; this client requires base64")
    return AccountInfo(
        pubkey=pubkey,
        owner=value.get("owner", ""),
        lamports=int(value.get("lamports", 0)),
        data=raw,
        executable=bool(value.get("executable", False)),
        rent_epoch=int(value.get("rentEpoch", 0) or 0),
        slot=slot,
    )


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def encode_u8_filter(offset: int, byte_value: int) -> dict[str, Any]:
    """memcmp on a single byte -- the bucket partition from analysis 5e."""
    from .base58 import b58encode

    if not 0 <= byte_value <= 255:
        raise ValueError("byte_value must fit in a byte")
    return memcmp(offset, b58encode(bytes([byte_value])))


def validate_pubkey(text: str) -> str:
    if len(b58decode(text)) != 32:
        raise ValueError(f"not a 32-byte public key: {text}")
    return text
