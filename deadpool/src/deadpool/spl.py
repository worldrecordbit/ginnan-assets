"""SPL Token / Token-2022 account and mint decoding.

Only the fixed prefix is decoded. Token-2022 appends extensions after byte
165, but the first 165 bytes are laid out identically to SPL Token, so mint,
owner and amount are read the same way for both -- which is what lets one
decoder serve both programs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base58 import b58encode
from .constants import TOKEN_2022_PROGRAM, TOKEN_PROGRAM

#: Byte offsets into a token account.
MINT_OFFSET = 0
OWNER_OFFSET = 32
AMOUNT_OFFSET = 64
STATE_OFFSET = 108
TOKEN_ACCOUNT_LEN = 165

#: Byte offsets into a mint account.
MINT_SUPPLY_OFFSET = 36
MINT_DECIMALS_OFFSET = 44
MINT_IS_INITIALIZED_OFFSET = 45
MINT_ACCOUNT_LEN = 82

STATE_UNINITIALIZED = 0
STATE_INITIALIZED = 1
STATE_FROZEN = 2


@dataclass(frozen=True)
class TokenAccount:
    address: str
    mint: str
    owner: str
    amount: int
    state: int
    program: str

    @property
    def initialised(self) -> bool:
        return self.state != STATE_UNINITIALIZED


@dataclass(frozen=True)
class Mint:
    address: str
    decimals: int
    initialised: bool
    #: Total supply in raw units. The detector uses it as the denominator for
    #: a scale-free depletion test -- see ``scorer`` signal S4.
    supply: int = 0


def is_token_program(program_id: str) -> bool:
    return program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)


def decode_token_account(address: str, data: bytes, program: str) -> TokenAccount | None:
    """Decode a token account, or return ``None`` if it is not one.

    Returns ``None`` rather than raising because the generic vault resolver
    speculatively decodes arbitrary accounts and a non-match is the expected
    case, not an error.
    """
    if not is_token_program(program) or len(data) < TOKEN_ACCOUNT_LEN:
        return None
    return TokenAccount(
        address=address,
        mint=b58encode(data[MINT_OFFSET : MINT_OFFSET + 32]),
        owner=b58encode(data[OWNER_OFFSET : OWNER_OFFSET + 32]),
        amount=int.from_bytes(data[AMOUNT_OFFSET : AMOUNT_OFFSET + 8], "little"),
        state=data[STATE_OFFSET],
        program=program,
    )


def decode_mint(address: str, data: bytes, program: str) -> Mint | None:
    if not is_token_program(program) or len(data) < MINT_ACCOUNT_LEN:
        return None
    # A Token-2022 *account* is >= 165 bytes and would otherwise decode as a
    # mint on its length alone; the discriminating fact is that mints are
    # exactly 82 bytes, or carry the mint extension marker at byte 165.
    if len(data) != MINT_ACCOUNT_LEN and (len(data) < 166 or data[165] != 1):
        return None
    return Mint(
        address=address,
        decimals=data[MINT_DECIMALS_OFFSET],
        initialised=bool(data[MINT_IS_INITIALIZED_OFFSET]),
        supply=int.from_bytes(data[MINT_SUPPLY_OFFSET : MINT_SUPPLY_OFFSET + 8], "little"),
    )


def encode_token_account(
    mint: bytes, owner: bytes, amount: int, *, state: int = STATE_INITIALIZED
) -> bytes:
    """Build a token account's bytes. Used by tests to mint fixtures."""
    if len(mint) != 32 or len(owner) != 32:
        raise ValueError("mint and owner must be 32 bytes")
    data = bytearray(TOKEN_ACCOUNT_LEN)
    data[MINT_OFFSET : MINT_OFFSET + 32] = mint
    data[OWNER_OFFSET : OWNER_OFFSET + 32] = owner
    data[AMOUNT_OFFSET : AMOUNT_OFFSET + 8] = int(amount).to_bytes(8, "little")
    data[STATE_OFFSET] = state
    return bytes(data)


def encode_mint(decimals: int, *, initialised: bool = True, supply: int = 0) -> bytes:
    data = bytearray(MINT_ACCOUNT_LEN)
    data[MINT_SUPPLY_OFFSET : MINT_SUPPLY_OFFSET + 8] = int(supply).to_bytes(8, "little")
    data[MINT_DECIMALS_OFFSET] = decimals
    data[MINT_IS_INITIALIZED_OFFSET] = 1 if initialised else 0
    return bytes(data)
