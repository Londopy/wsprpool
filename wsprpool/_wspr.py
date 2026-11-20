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
ALPHA = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
M32 = 0xFFFFFFFF


def _bitrev8(i: int) -> int:
    return int(f"{i:08b}"[::-1], 2)


# coded bit p goes to channel position PERM[p]
PERM = np.array([j for j in (_bitrev8(i) for i in range(256)) if j < NSYM], dtype=np.int64)


@dataclass(frozen=True)
class Message:
    """A WSPR message of any of the three types.

    Type 1 is a plain call, a 4-character locator and power. Type 2 is a compound call
    like PJ4/K1ABC or K1ABC/P with power and no locator. Type 3 carries a 15-bit hash
    of the call, a 6-character locator and power: `call` stays empty until the hash is
    matched to a call heard in another message, and `callhash` holds the hash.
    """

    call: str
    grid: str
    power: int
    callhash: int | None = None

    @property
    def kind(self) -> int:
        if len(self.grid) == 6:
            return 3
        return 2 if "/" in self.call else 1

    def __str__(self) -> str:
        if self.kind == 3:
            return f"<{self.call or '...'}> {self.grid} {self.power}"
        if self.kind == 2:
            return f"{self.call} {self.power}"
        return f"{self.call} {self.grid} {self.power}"


def _rot(x: int, k: int) -> int:
    return ((x << k) | (x >> (32 - k))) & M32


def _mix(a: int, b: int, c: int) -> tuple[int, int, int]:
    for r1, r2, r3 in ((4, 6, 8), (16, 19, 4)):
        a = ((a - c) & M32) ^ _rot(c, r1)
        c = (c + b) & M32
        b = ((b - a) & M32) ^ _rot(a, r2)
        a = (a + c) & M32
        c = ((c - b) & M32) ^ _rot(b, r3)
        b = (b + a) & M32
    return a, b, c


def _final(a: int, b: int, c: int) -> int:
    c = ((c ^ b) - _rot(b, 14)) & M32
    a = ((a ^ c) - _rot(c, 11)) & M32
    b = ((b ^ a) - _rot(a, 25)) & M32
    c = ((c ^ b) - _rot(b, 16)) & M32
    a = ((a ^ c) - _rot(c, 4)) & M32
    b = ((b ^ a) - _rot(a, 14)) & M32
    return ((c ^ b) - _rot(b, 24)) & M32


def lookup3(data: bytes, initval: int) -> int:
    """Bob Jenkins' hashlittle from lookup3.c, which wsprd uses to hash callsigns."""
    a = b = c = (0xDEADBEEF + len(data) + initval) & M32
    while len(data) > 12:
        a = (a + int.from_bytes(data[0:4], "little")) & M32
        b = (b + int.from_bytes(data[4:8], "little")) & M32
        c = (c + int.from_bytes(data[8:12], "little")) & M32
        a, b, c = _mix(a, b, c)
        data = data[12:]
    if not data:
        return c
    tail = data.ljust(12, b"\0")
    a = (a + int.from_bytes(tail[0:4], "little")) & M32
    b = (b + int.from_bytes(tail[4:8], "little")) & M32
    c = (c + int.from_bytes(tail[8:12], "little")) & M32
    return _final(a, b, c)


def callhash(call: str) -> int:
    """The 15-bit hash a type 3 message carries in place of the call."""
    return lookup3(call.upper().encode(), 146) & 0x7FFF


def _charval(c: str) -> int:
    i = ALPHA.find(c)
    if i < 0:
        raise ValueError(f"bad character {c!r} in callsign")
    return i


def _normalize_call(call: str) -> str:
    call = call.upper()
    if len(call) > 6 or not call.isalnum():
        raise ValueError(f"{call!r} is not a plain callsign of up to 6 characters")
    if len(call) >= 3 and not call[2].isdigit():
        call = " " + call
    if len(call) < 3 or len(call) > 6 or not call[2].isdigit():
        raise ValueError(f"{call!r} has no digit in the third position")
    return call.ljust(6)


def _pack_call(call: str) -> int:
    v = [_charval(ch) for ch in _normalize_call(call)]
    if v[1] == 36 or v[2] > 9 or any(x < 10 for x in v[3:]):
        raise ValueError(f"{call!r} does not fit the WSPR callsign layout")
    n = v[0]
    n = n * 36 + v[1]
    n = n * 10 + v[2]
    for x in v[3:]:
        n = n * 27 + (26 if x == 36 else x - 10)
    return n


def _unpack_call(n: int) -> str | None:
    chars = []
    for _ in range(3):
        n, r = divmod(n, 27)
        chars.append(" " if r == 26 else ALPHA[r + 10])
    n, r = divmod(n, 10)
    chars.append(ALPHA[r])
    n, r = divmod(n, 36)
    chars.append(ALPHA[r])
    if n > 36:
        return None
    chars.append(ALPHA[n])
    return "".join(reversed(chars))


def _pack_grid(grid: str) -> int:
    g = grid.upper()
    if len(g) != 4 or not ("A" <= g[0] <= "R" and "A" <= g[1] <= "R"
                           and g[2].isdigit() and g[3].isdigit()):
        raise ValueError(f"{grid!r} is not a 4-character locator")
    a, b = ord(g[0]) - 65, ord(g[1]) - 65
    return (179 - 10 * a - int(g[2])) * 180 + 10 * b + int(g[3])


def _unpack_grid(m: int) -> str | None:
    x, y = divmod(m, 180)
    a, c = divmod(179 - x, 10)
    b, d = divmod(y, 10)
    if not (0 <= a < 18 and 0 <= b < 18):
        return None
    return f"{chr(65 + a)}{chr(65 + b)}{c}{d}"


def _affix(call: str) -> tuple[str, int]:
    """Split a compound call into the plain call and its prefix or suffix code."""
    left, _, right = call.upper().partition("/")
    if "/" in right or not left or not right:
        raise ValueError(f"{call!r} needs exactly one '/' between two parts")
    if len(right) == 1 and right.isalnum():
        return left, 60000 + _charval(right)
    if len(right) == 2 and right.isdigit() and right[0] != "0":
        return left, 60000 + 26 + int(right)
    if len(left) <= 3 and left.isalnum():
        n = 0
        for ch in left.rjust(3):
            n = n * 37 + _charval(ch)
        return right, n
    raise ValueError(f"{call!r}: WSPR allows a prefix of 1 to 3 characters, or a suffix of "
                     "one character or two digits")


def _unaffix(call: str, n3: int) -> str | None:
    if n3 < 60000:
        pfx = ""
        for _ in range(3):
            n3, r = divmod(n3, 37)
            pfx = ALPHA[r] + pfx
        # three base-37 characters only reach 50652; anything above is no prefix
        if n3:
            return None
        pfx = pfx.lstrip()
        return None if not pfx or " " in pfx else f"{pfx}/{call}"
    nc = n3 - 60000
    if nc < 36:
        return f"{call}/{ALPHA[nc]}"
    return f"{call}/{nc - 26}" if nc <= 125 else None


def _grid6_ok(g: str) -> bool:
    return ("A" <= g[0] <= "R" and "A" <= g[1] <= "R" and g[2].isdigit() and g[3].isdigit()
            and "A" <= g[4] <= "X" and "A" <= g[5] <= "X")


def pack(msg: Message) -> int:
    """Pack a message of any type into its 50-bit integer."""
    if msg.power not in VALID_POWERS:
        raise ValueError(f"{msg.power} dBm is not a valid WSPR power")
    if msg.kind == 3:
        g = msg.grid.upper()
        if not _grid6_ok(g):
            raise ValueError(f"{msg.grid!r} is not a 6-character locator")
        h = msg.callhash if msg.callhash is not None else callhash(msg.call)
        # the locator rides in the callsign field, rotated so a digit lands third
        n = _pack_call(g[1:] + g[0])
        m = h * 128 - (msg.power + 1) + 64
    elif msg.kind == 2:
        if msg.grid:
            raise ValueError(f"a compound call like {msg.call!r} carries no locator")
        base, n3 = _affix(msg.call)
        n = _pack_call(base)
        ng, high = (n3 - 32768, 1) if n3 > 32767 else (n3, 0)
        m = ng * 128 + msg.power + 1 + high + 64
    else:
        n = _pack_call(msg.call)
        m = _pack_grid(msg.grid) * 128 + msg.power + 64
    return (n << 22) | m


def unpack(value: int) -> Message | None:
    """Unpack a 50-bit value; None when it is not a valid message of any type."""
    n, m = value >> 22, value & 0x3FFFFF
    ntype = (m & 0x7F) - 64
    ng = m >> 7
    raw = _unpack_call(n)
    if raw is None or ntype > 62:
        return None
    if ntype < 0:
        grid = raw[5] + raw[:5]
        power = -(ntype + 1)
        if power not in VALID_POWERS or not _grid6_ok(grid):
            return None
        return Message("", grid, power, ng)
    call = raw.strip()
    if " " in call or not call:
        return None
    if ntype % 10 in (0, 3, 7):
        g4 = _unpack_grid(ng)
        return None if g4 is None else Message(call, g4, ntype)
    # type 2: the power's last digit is bumped by 1 or 2 to flag a prefix or a suffix
    nu = ntype % 10
    nadd = nu - 7 if nu > 7 else nu - 3 if nu > 3 else nu
    power = ntype - nadd
    full = _unaffix(call, ng + 32768 * (nadd - 1))
    if full is None or power not in VALID_POWERS:
        return None
    return Message(full, "", power)


def resolve(msg: Message, calls: dict[int, str]) -> Message:
    """Fill in a type 3 message's call from a table of hashes of calls heard elsewhere."""
    if msg.kind != 3 or msg.call or msg.callhash not in calls:
        return msg
    return Message(calls[msg.callhash], msg.grid, msg.power, msg.callhash)


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
