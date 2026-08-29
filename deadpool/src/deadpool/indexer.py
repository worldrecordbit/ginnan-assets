"""Pool State Indexer -- component 1 of the detection architecture.

One job: produce a correct :class:`PoolSnapshot` for a pool -- its reserves,
its orientation, and whether it is initialised. Nothing else.

**Orientation is resolved here, once, from mint identity**, and is
authoritative for everything downstream. That closes the trap in threat model
section 3.4 at the source: in a material minority of PumpSwap pools wrapped
SOL is the program's *base* mint, so a system that trusts the AMM's field
names -- or worse, its ``Instruction: Sell`` log line -- gets the direction
backwards. This module never reads a log and never trusts a field name; the
vault whose mint is wrapped SOL is the quote side, and that is the whole rule.

**Vault resolution is layered, and every layer is validated on-chain.** A
program-specific layout is a guess about someone else's account struct, and a
wrong guess would silently produce reserves for the wrong accounts. So the
fast path is tried, then *checked* (are these really token accounts owned by
this pool?), and a failed check falls through to a slower resolver that needs
no layout knowledge at all:

1. vaults supplied by the caller,
2. a program-specific decoder (PumpSwap),
3. ``getTokenAccountsByOwner(pool)`` -- exact where vaults are pool-owned,
4. a scan of the pool account for embedded pubkeys, each candidate fetched
   and kept only if it is a token account owned by the pool or by a known
   pool authority.

Layer 4 is how Meteora DAMM v2 resolves: its vaults are owned by a shared
pool authority and this codebase does not hard-code its account layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .base58 import b58encode, is_pubkey
from .constants import (
    METEORA_DAMM_V2_POOL_AUTHORITY,
    METEORA_DAMM_V2_PROGRAM,
    PUMPSWAP_PROGRAM,
    QUOTE_MINTS,
    TOKEN_PROGRAMS,
)
from .models import PoolSnapshot
from .rpc import AccountInfo, JsonRpcClient
from .spl import Mint, TokenAccount, decode_mint, decode_token_account

PROGRAM_NAMES = {
    PUMPSWAP_PROGRAM: "pumpswap",
    METEORA_DAMM_V2_PROGRAM: "meteora-damm2",
}

#: Accounts allowed to own a pool's vaults, beyond the pool address itself.
KNOWN_POOL_AUTHORITIES = frozenset({METEORA_DAMM_V2_POOL_AUTHORITY})

#: Candidate pubkeys are read at 8-byte-aligned offsets. Anchor structs pack
#: 32-byte fields on 8-byte boundaries in practice, and the alignment cuts the
#: candidate set by 8x -- which matters because each candidate costs a slot in
#: a getMultipleAccounts batch.
SCAN_ALIGNMENT = 8
SCAN_MAX_CANDIDATES = 96


class PoolResolutionError(RuntimeError):
    """The pool's two vaults could not be identified with confidence."""


@dataclass(frozen=True)
class PumpSwapPool:
    """The fields of a PumpSwap ``Pool`` account this system needs."""

    base_mint: str
    quote_mint: str
    base_vault: str
    quote_vault: str


# Offsets into the PumpSwap Pool account: 8-byte discriminator, pool_bump u8,
# index u16, then creator, base_mint, quote_mint, lp_mint, and the two vaults.
# Treated as a hypothesis, never as fact -- decode_pumpswap_pool's output is
# validated against the vault accounts themselves before it is used.
_PUMPSWAP_BASE_MINT = 43
_PUMPSWAP_QUOTE_MINT = 75
_PUMPSWAP_BASE_VAULT = 139
_PUMPSWAP_QUOTE_VAULT = 171
_PUMPSWAP_MIN_LEN = 211


def decode_pumpswap_pool(data: bytes) -> PumpSwapPool | None:
    if len(data) < _PUMPSWAP_MIN_LEN:
        return None
    read = lambda off: b58encode(data[off : off + 32])  # noqa: E731
    pool = PumpSwapPool(
        base_mint=read(_PUMPSWAP_BASE_MINT),
        quote_mint=read(_PUMPSWAP_QUOTE_MINT),
        base_vault=read(_PUMPSWAP_BASE_VAULT),
        quote_vault=read(_PUMPSWAP_QUOTE_VAULT),
    )
    if len({pool.base_vault, pool.quote_vault}) != 2:
        return None
    return pool


def scan_pubkey_candidates(
    data: bytes, *, alignment: int = SCAN_ALIGNMENT, limit: int = SCAN_MAX_CANDIDATES
) -> list[str]:
    """Every 32-byte window at an aligned offset, as a base58 string.

    Zero-filled windows are dropped -- an all-zero pubkey is the system
    program, never a vault, and unused struct padding produces a lot of them.
    """
    out: list[str] = []
    seen: set[str] = set()
    for offset in range(0, max(len(data) - 32 + 1, 0), alignment):
        window = data[offset : offset + 32]
        if not any(window):
            continue
        key = b58encode(window)
        if key not in seen:
            seen.add(key)
            out.append(key)
        if len(out) >= limit:
            break
    return out


class PoolStateIndexer:
    """Reads pool state. Caches vault and mint resolution, never reserves.

    Vault addresses and mint decimals are immutable for the life of a pool, so
    they are cached indefinitely. Reserves are not cached here at all: this
    component's contract is a *current* snapshot, and any staleness policy
    belongs to the advisory layer, which has to report the slot it used.
    """

    def __init__(self, client: JsonRpcClient) -> None:
        self.client = client
        self._vaults: dict[str, tuple[TokenAccount, TokenAccount]] = {}
        self._mints: dict[str, Mint] = {}
        self._program_name: dict[str, str] = {}
        self._flipped: dict[str, bool] = {}

    # --- public API -------------------------------------------------------

    def snapshot(
        self, pool: str, *, vaults: Sequence[str] | None = None
    ) -> PoolSnapshot:
        """Current reserves and orientation for ``pool``."""
        base_acct, quote_acct, program, flipped = self._resolve(pool, vaults)
        # Re-read the vaults so reserves are current even on a cache hit.
        fresh = self.client.get_multiple_accounts([base_acct.address, quote_acct.address])
        slot = max((a.slot for a in fresh if a), default=0)
        base_now = _as_token_account(fresh[0]) or base_acct
        quote_now = _as_token_account(fresh[1]) or quote_acct

        mints = self._mint_info([base_now.mint, quote_now.mint])
        return PoolSnapshot(
            pool=pool,
            program=program,
            base_mint=base_now.mint,
            quote_mint=quote_now.mint,
            base_vault=base_now.address,
            quote_vault=quote_now.address,
            base_reserve=base_now.amount,
            quote_reserve=quote_now.amount,
            base_decimals=mints[base_now.mint].decimals,
            quote_decimals=mints[quote_now.mint].decimals,
            base_supply=mints[base_now.mint].supply,
            initialised=base_now.initialised and quote_now.initialised,
            slot=slot,
            orientation_flipped=flipped,
        )

    def forget(self, pool: str) -> None:
        """Drop cached vault resolution for a pool."""
        self._vaults.pop(pool, None)
        self._program_name.pop(pool, None)
        self._flipped.pop(pool, None)

    # --- resolution -------------------------------------------------------

    def _resolve(
        self, pool: str, vaults: Sequence[str] | None
    ) -> tuple[TokenAccount, TokenAccount, str, bool]:
        cached = self._vaults.get(pool) if vaults is None else None
        if cached is not None:
            base, quote = cached
            return (
                base,
                quote,
                self._program_name.get(pool, "unknown"),
                self._flipped.get(pool, False),
            )

        pool_account = self.client.get_account_info(pool)
        if pool_account is None:
            raise PoolResolutionError(f"pool account {pool} does not exist")
        program = PROGRAM_NAMES.get(pool_account.owner, pool_account.owner)

        found = None
        if vaults is not None:
            found = self._validate_vaults(pool, list(vaults))
            if found is None:
                raise PoolResolutionError(f"supplied vaults are not token accounts of {pool}")
        if found is None:
            found = self._try_program_decoder(pool, pool_account)
        if found is None:
            found = self._try_owned_token_accounts(pool)
        if found is None:
            found = self._try_scan(pool, pool_account)
        if found is None:
            raise PoolResolutionError(
                f"could not resolve two distinct token vaults for pool {pool} "
                f"(program {pool_account.owner})"
            )

        first, second = found
        base, quote, flipped = _orient(first, second)
        self._vaults[pool] = (base, quote)
        self._program_name[pool] = program
        self._flipped[pool] = flipped
        return base, quote, program, flipped

    def _try_program_decoder(
        self, pool: str, account: AccountInfo
    ) -> tuple[TokenAccount, TokenAccount] | None:
        if account.owner != PUMPSWAP_PROGRAM:
            return None
        decoded = decode_pumpswap_pool(account.data)
        if decoded is None:
            return None
        validated = self._validate_vaults(pool, [decoded.base_vault, decoded.quote_vault])
        if validated is None:
            return None
        # The layout is only trusted if the mints it claims match the mints
        # the vaults actually hold. A shifted offset fails here and falls
        # through to a resolver that needs no layout at all.
        claimed = {decoded.base_mint, decoded.quote_mint}
        actual = {validated[0].mint, validated[1].mint}
        return validated if claimed == actual else None

    def _try_owned_token_accounts(self, pool: str) -> tuple[TokenAccount, TokenAccount] | None:
        accounts: list[TokenAccount] = []
        for program in TOKEN_PROGRAMS:
            for info in self.client.get_token_accounts_by_owner(pool, program):
                decoded = decode_token_account(info.pubkey, info.data, info.owner)
                if decoded is not None:
                    accounts.append(decoded)
        return _pick_pair(accounts)

    def _try_scan(self, pool: str, account: AccountInfo) -> tuple[TokenAccount, TokenAccount] | None:
        candidates = [k for k in scan_pubkey_candidates(account.data) if is_pubkey(k)]
        if not candidates:
            return None
        allowed = KNOWN_POOL_AUTHORITIES | {pool}
        fetched = self.client.get_multiple_accounts(candidates)
        accounts = [
            decoded
            for info in fetched
            if info is not None
            and (decoded := decode_token_account(info.pubkey, info.data, info.owner)) is not None
            and decoded.owner in allowed
        ]
        return _pick_pair(accounts)

    def _validate_vaults(
        self, pool: str, addresses: list[str]
    ) -> tuple[TokenAccount, TokenAccount] | None:
        if len(addresses) != 2 or len(set(addresses)) != 2:
            return None
        if not all(is_pubkey(a) for a in addresses):
            return None
        fetched = self.client.get_multiple_accounts(addresses)
        allowed = KNOWN_POOL_AUTHORITIES | {pool}
        decoded = [_as_token_account(info) for info in fetched]
        if any(d is None for d in decoded):
            return None
        if any(d.owner not in allowed for d in decoded):  # type: ignore[union-attr]
            return None
        return _pick_pair([d for d in decoded if d is not None])

    def _mint_info(self, mints: Iterable[str]) -> dict[str, Mint]:
        """Decimals and supply for a set of mints, cached indefinitely.

        Both come from the same account, so supply -- which the scale-free
        depletion test needs -- costs no extra round trip. Supply is not in
        fact immutable, but the test that consumes it spans ten orders of
        magnitude, so a mint or burn cannot move a verdict.
        """
        wanted = [m for m in dict.fromkeys(mints) if m not in self._mints]
        if wanted:
            for address, info in zip(wanted, self.client.get_multiple_accounts(wanted)):
                mint = decode_mint(address, info.data, info.owner) if info is not None else None
                # An unreadable mint yields zero supply, which switches the
                # supply test off rather than defaulting it either way; the
                # raw-unit floors are in raw units precisely so a missing
                # mint account cannot move a verdict on its own.
                self._mints[address] = mint or Mint(address, decimals=0, initialised=False)
        return {m: self._mints.get(m, Mint(m, 0, False)) for m in mints}


# --- helpers --------------------------------------------------------------


def _as_token_account(info: AccountInfo | None) -> TokenAccount | None:
    if info is None:
        return None
    return decode_token_account(info.pubkey, info.data, info.owner)


def _pick_pair(accounts: Sequence[TokenAccount]) -> tuple[TokenAccount, TokenAccount] | None:
    """Choose the two vaults from a candidate set.

    A pool has exactly one vault per mint. Where several candidates share a
    mint the largest balance wins, since a pool's own vault is the account
    holding the reserves. More than two distinct mints means the candidates
    are not one pool's vaults and the caller should fall through.
    """
    by_mint: dict[str, TokenAccount] = {}
    for account in accounts:
        existing = by_mint.get(account.mint)
        if existing is None or account.amount > existing.amount:
            by_mint[account.mint] = account
    if len(by_mint) != 2:
        return None
    first, second = by_mint.values()
    return first, second


def _orient(a: TokenAccount, b: TokenAccount) -> tuple[TokenAccount, TokenAccount, bool]:
    """Assign economic base/quote roles by mint identity.

    ``flipped`` records that the pair arrived quote-first, which for a
    program-decoded PumpSwap pool means the AMM's own ``base_mint`` field held
    wrapped SOL -- the orientation trap, present on this pool.
    """
    a_is_quote = a.mint in QUOTE_MINTS
    b_is_quote = b.mint in QUOTE_MINTS
    if a_is_quote and not b_is_quote:
        return b, a, True
    if b_is_quote and not a_is_quote:
        return a, b, False
    raise PoolResolutionError(
        f"neither vault holds a recognised quote mint ({a.mint}, {b.mint}); "
        f"this pool is not quoted in SOL"
    )
