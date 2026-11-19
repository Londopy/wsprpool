"""WSPR message packing, convolutional coding, interleaving and channel symbols."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NSYM = 162
NBITS = 81
NDATA = 50
SYMBOL_RATE = 12000 / 8192
TONE_SPACING = SYMBOL_RATE
POLY1 = 0xF2D05351
POLY2 = 0xE4613C47

SYNC = np.array([
    1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 0, 0,
    0, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 0, 1,
    1, 0, 1, 0, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 1,
    1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 1, 1,
    0, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0,
    1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0,
], dtype=np.int8)

VALID_POWERS = frozenset(p for p in range(61) if p % 10 in (0, 3, 7))


def _bitrev8(i: int) -> int:
    return int(f"{i:08b}"[::-1], 2)


# coded bit p goes to channel position PERM[p]
PERM = np.array([j for j in (_bitrev8(i) for i in range(256)) if j < NSYM], dtype=np.int64)


@dataclass(frozen=True)
class Message:
    call: str
    grid: str
    power: int

    def __str__(self) -> str:
        return f"{self.call} {self.grid} {self.power}"


def _charval(c: str) -> int:
    if c.isdigit():
        return ord(c) - ord("0")
    if "A" <= c <= "Z":
        return ord(c) - ord("A") + 10
    if c == " ":
        return 36
    raise ValueError(f"bad character {c!r} in callsign")


def _normalize_call(call: str) -> str:
    call = call.upper()
    if len(call) > 6 or not call.isalnum():
        raise ValueError(f"{call!r} is not a plain type 1 callsign")
    if len(call) >= 3 and not call[2].isdigit():
        call = " " + call
    if len(call) < 3 or len(call) > 6 or not call[2].isdigit():
        raise ValueError(f"{call!r} has no digit in the third position")
    return call.ljust(6)


def pack(msg: Message) -> int:
    """Pack a type 1 message into its 50-bit integer."""
    c = _normalize_call(msg.call)
    v = [_charval(ch) for ch in c]
    if v[1] == 36 or v[2] > 9 or any(x < 10 for x in v[3:]):
        raise ValueError(f"{msg.call!r} does not fit the type 1 callsign layout")
    n = v[0]
    n = n * 36 + v[1]
    n = n * 10 + v[2]
    for x in v[3:]:
        n = n * 27 + (26 if x == 36 else x - 10)

    g = msg.grid.upper()
    if len(g) != 4 or not ("A" <= g[0] <= "R" and "A" <= g[1] <= "R"
                           and g[2].isdigit() and g[3].isdigit()):
        raise ValueError(f"{msg.grid!r} is not a 4-character locator")
    if msg.power not in VALID_POWERS:
        raise ValueError(f"{msg.power} dBm is not a valid WSPR power")
    a, b = ord(g[0]) - 65, ord(g[1]) - 65
    m = (179 - 10 * a - int(g[2])) * 180 + 10 * b + int(g[3])
    m = m * 128 + msg.power + 64
    return (n << 22) | m


def unpack(value: int) -> Message | None:
    """Unpack a 50-bit value; None when it is not a valid type 1 message."""
    n, m = value >> 22, value & 0x3FFFFF
    power = (m & 0x7F) - 64
    m >>= 7
    x, y = divmod(m, 180)
    a, c = divmod(179 - x, 10)
    b, d = divmod(y, 10)
    if not (0 <= a < 18 and 0 <= b < 18) or power not in VALID_POWERS:
        return None
    grid = f"{chr(65 + a)}{chr(65 + b)}{c}{d}"

    alpha = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    chars = []
    for _ in range(3):
        n, r = divmod(n, 27)
        chars.append(" " if r == 26 else alpha[r + 10])
    n, r = divmod(n, 10)
    chars.append(alpha[r])
    n, r = divmod(n, 36)
    chars.append(alpha[r])
    if n > 36:
        return None
    chars.append(alpha[n])
    call = "".join(reversed(chars)).strip()
    if " " in call or not call:
        return None
    return Message(call, grid, power)


def parity(x: int) -> int:
    return x.bit_count() & 1


def bits_of(value: int) -> list[int]:
    """The 81 encoder input bits: 50 message bits, MSB first, then 31 zero tail bits."""
    return [(value >> (NDATA - 1 - i)) & 1 for i in range(NDATA)] + [0] * (NBITS - NDATA)


def convolve(bits: list[int]) -> np.ndarray:
    reg = 0
    out = np.empty(2 * len(bits), dtype=np.int8)
    for i, b in enumerate(bits):
        reg = ((reg << 1) | b) & 0xFFFFFFFF
        out[2 * i] = parity(reg & POLY1)
        out[2 * i + 1] = parity(reg & POLY2)
    return out


def interleave(coded: np.ndarray) -> np.ndarray:
    out = np.empty_like(coded)
    out[PERM] = coded
    return out


def deinterleave(chan: np.ndarray) -> np.ndarray:
    return chan[PERM]


def symbols(msg: Message) -> np.ndarray:
    """The 162 channel tones (0-3) for a message."""
    data = interleave(convolve(bits_of(pack(msg))))
    return (SYNC + 2 * data).astype(np.int8)
