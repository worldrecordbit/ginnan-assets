"""A synthetic chain, so every component is testable with no network.

``MockChain`` implements the handful of JSON-RPC methods this system calls,
over an in-memory account map. It is deliberately literal: accounts are raw
bytes with an owning program, exactly as a node returns them, so the decoders
under test do real byte-level work rather than being handed structured data.

It also counts calls, which is how the caching and batching tests assert that
a cache hit really did avoid a round trip.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any

from deadpool.base58 import b58decode, b58encode
from deadpool.constants import (
    METEORA_DAMM_V2_POOL_AUTHORITY,
    METEORA_DAMM_V2_PROGRAM,
    PUMPSWAP_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
)
from deadpool.rpc import JsonRpcClient
from deadpool.spl import encode_mint, encode_token_account

SYSTEM_PROGRAM = "11111111111111111111111111111111"


def pubkey(name: str) -> str:
    """A deterministic 32-byte address from a readable name."""
    return b58encode(hashlib.sha256(name.encode()).digest())


@dataclass
class Account:
    owner: str
    data: bytes
    lamports: int = 1_000_000


@dataclass
class MockChain:
    """An in-memory account map that speaks JSON-RPC."""

    accounts: dict[str, Account] = field(default_factory=dict)
    slot: int = 442_634_543
    calls: list[str] = field(default_factory=list)
    fail_methods: set[str] = field(default_factory=set)

    # --- building ---------------------------------------------------------

    def add(self, address: str, owner: str, data: bytes, lamports: int = 1_000_000) -> str:
        self.accounts[address] = Account(owner, data, lamports)
        return address

    def add_mint(self, name: str, decimals: int = 6, program: str = TOKEN_PROGRAM) -> str:
        return self.add(pubkey(name), program, encode_mint(decimals))

    def add_token_account(
        self,
        name: str,
        mint: str,
        owner: str,
        amount: int,
        *,
        program: str = TOKEN_PROGRAM,
        state: int = 1,
    ) -> str:
        data = encode_token_account(b58decode(mint), b58decode(owner), amount, state=state)
        return self.add(pubkey(name), program, data)

    def add_wsol_mint(self) -> str:
        self.accounts[WSOL_MINT] = Account(TOKEN_PROGRAM, encode_mint(9))
        return WSOL_MINT

    def add_pumpswap_pool(
        self,
        name: str,
        *,
        token_mint: str,
        base_reserve: int,
        quote_reserve: int,
        flipped: bool = False,
        token_program: str = TOKEN_PROGRAM,
    ) -> str:
        """A PumpSwap pool whose Pool account really carries the layout.

        ``flipped`` puts wrapped SOL in the program's *base_mint* field, which
        is the orientation trap from threat model 3.4: the correct economic
        answer is unchanged, and a decoder that trusts field order gets it
        backwards.
        """
        pool = pubkey(name)
        self.add_wsol_mint()
        token_vault = self.add_token_account(
            f"{name}/token-vault", token_mint, pool, base_reserve, program=token_program
        )
        quote_vault = self.add_token_account(
            f"{name}/quote-vault", WSOL_MINT, pool, quote_reserve
        )
        if flipped:
            mint_a, mint_b = WSOL_MINT, token_mint
            vault_a, vault_b = quote_vault, token_vault
        else:
            mint_a, mint_b = token_mint, WSOL_MINT
            vault_a, vault_b = token_vault, quote_vault

        data = bytearray(243)
        data[43:75] = b58decode(mint_a)
        data[75:107] = b58decode(mint_b)
        data[139:171] = b58decode(vault_a)
        data[171:203] = b58decode(vault_b)
        self.add(pool, PUMPSWAP_PROGRAM, bytes(data))
        return pool

    def add_meteora_pool(
        self, name: str, *, token_mint: str, base_reserve: int, quote_reserve: int
    ) -> str:
        """A Meteora DAMM v2 pool with an *undocumented* account layout.

        The vault addresses are embedded at offsets this codebase does not
        know, and the vaults are owned by the shared pool authority rather
        than the pool, so neither the PumpSwap decoder nor an owner lookup
        can resolve them. Only the validated scan can.
        """
        pool = pubkey(name)
        self.add_wsol_mint()
        token_vault = self.add_token_account(
            f"{name}/token-vault",
            token_mint,
            METEORA_DAMM_V2_POOL_AUTHORITY,
            base_reserve,
            program=TOKEN_2022_PROGRAM,
        )
        quote_vault = self.add_token_account(
            f"{name}/quote-vault", WSOL_MINT, METEORA_DAMM_V2_POOL_AUTHORITY, quote_reserve
        )
        # Offsets chosen to be ones this codebase has no knowledge of.
        token_at, quote_at = 328, 392
        data = bytearray(1112)
        data[8:40] = b58decode(pubkey(f"{name}/creator"))
        data[token_at : token_at + 32] = b58decode(token_vault)
        data[quote_at : quote_at + 32] = b58decode(quote_vault)
        self.add(pool, METEORA_DAMM_V2_PROGRAM, bytes(data))
        return pool

    # --- serving ----------------------------------------------------------

    def client(self) -> JsonRpcClient:
        return JsonRpcClient("mock://chain", transport=self)

    def __call__(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for request in payload:
            method = request["method"]
            self.calls.append(method)
            if method in self.fail_methods:
                out.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32000, "message": f"{method} disabled in test"},
                    }
                )
                continue
            handler = getattr(self, f"_rpc_{method}", None)
            if handler is None:
                out.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": f"unsupported: {method}"},
                    }
                )
                continue
            out.append({"jsonrpc": "2.0", "id": request["id"], "result": handler(request["params"])})
        return out

    def _encode(self, address: str) -> dict[str, Any] | None:
        account = self.accounts.get(address)
        if account is None:
            return None
        return {
            "owner": account.owner,
            "lamports": account.lamports,
            "data": [base64.b64encode(account.data).decode(), "base64"],
            "executable": False,
            "rentEpoch": 0,
        }

    def _rpc_getSlot(self, params) -> int:  # noqa: N802
        return self.slot

    def _rpc_getHealth(self, params) -> str:  # noqa: N802
        return "ok"

    def _rpc_getMultipleAccounts(self, params) -> dict[str, Any]:  # noqa: N802
        keys = params[0]
        return {"context": {"slot": self.slot}, "value": [self._encode(k) for k in keys]}

    def _rpc_getTokenAccountsByOwner(self, params) -> dict[str, Any]:  # noqa: N802
        owner, filters = params[0], params[1]
        program = filters.get("programId")
        value = []
        for address, account in self.accounts.items():
            if account.owner != program or len(account.data) < 165:
                continue
            if b58encode(account.data[32:64]) != owner:
                continue
            value.append({"pubkey": address, "account": self._encode(address)})
        return {"context": {"slot": self.slot}, "value": value}

    def _rpc_getProgramAccounts(self, params) -> dict[str, Any]:  # noqa: N802
        program, config = params[0], params[1] if len(params) > 1 else {}
        filters = config.get("filters") or []
        data_slice = config.get("dataSlice")
        value = []
        for address, account in self.accounts.items():
            if account.owner != program:
                continue
            if not all(_matches(account.data, f) for f in filters):
                continue
            data = account.data
            if data_slice:
                start = data_slice["offset"]
                data = data[start : start + data_slice["length"]]
            value.append(
                {
                    "pubkey": address,
                    "account": {
                        "owner": account.owner,
                        "lamports": account.lamports,
                        "data": [base64.b64encode(data).decode(), "base64"],
                        "executable": False,
                        "rentEpoch": 0,
                    },
                }
            )
        if config.get("withContext"):
            return {"context": {"slot": self.slot}, "value": value}
        return value


def _matches(data: bytes, spec: dict[str, Any]) -> bool:
    if "dataSize" in spec:
        return len(data) == spec["dataSize"]
    if "memcmp" in spec:
        offset = spec["memcmp"]["offset"]
        wanted = b58decode(spec["memcmp"]["bytes"])
        return data[offset : offset + len(wanted)] == wanted
    return True
