"""Base58 (Bitcoin alphabet), which is how Solana writes public keys.

Pure stdlib. Needed because the indexer reads 32-byte pubkeys out of raw
account data and has to name them, and because filters are supplied as
base58 strings that must go back to bytes.
"""

from __future__ import annotations

ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}

__all__ = ["b58encode", "b58decode", "is_pubkey"]


def b58encode(data: bytes) -> str:
    if not data:
        return ""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n:
        n, rem = divmod(n, 58)
        out.append(ALPHABET[rem])
    # Leading zero bytes are encoded as leading '1's, not dropped.
    for byte in data:
        if byte:
            break
        out.append(ALPHABET[0])
    return bytes(reversed(out)).decode("ascii")


def b58decode(text: str) -> bytes:
    if text == "":
        return b""
    n = 0
    for ch in text.encode("ascii"):
        digit = _INDEX.get(ch)
        if digit is None:
            raise ValueError(f"invalid base58 character: {chr(ch)!r}")
        n = n * 58 + digit
    leading = len(text) - len(text.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * leading + body


def is_pubkey(text: str) -> bool:
    """True when ``text`` decodes to exactly 32 bytes."""
    try:
        return len(b58decode(text)) == 32
    except (ValueError, UnicodeEncodeError):
        return False
