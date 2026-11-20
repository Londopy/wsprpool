"""Ground truth from the public wspr.live spot database."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ._wspr import Message, callhash

URL = "https://db1.wspr.live/"
CALL = re.compile(r"[A-Z0-9/]{3,10}")
GRID6 = re.compile(r"[A-R]{2}[0-9]{2}[A-X]{2}")

# (call, locator in upper case, power, reporter)
Row = tuple[str, str, int, str]


def band(freq_hz: float) -> int:
    """wspr.live's band number: the integer MHz for every HF band."""
    return int(freq_hz // 1_000_000)


def fetch(slots: list[float], messages: list[Message], freq_hz: float,
          timeout: float = 30.0) -> dict[float, list[Row]]:
    """Every wspr.live report on this band, per slot start, that could match a message.

    That is every report of a call we decoded, plus every report from a 6-character
    locator we decoded a type 3 message from, so a hash nobody else here heard can
    still be put to a call.
    """
    calls = {m.call for m in messages if m.call and CALL.fullmatch(m.call)}
    grids = {m.grid for m in messages if m.kind == 3 and GRID6.fullmatch(m.grid)}
    if not slots or not (calls or grids):
        return {}
    times = ",".join(
        "'" + datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "'"
        for t in sorted(set(slots)))
    conds = []
    if calls:
        conds.append("tx_sign IN (" + ",".join(f"'{c}'" for c in sorted(calls)) + ")")
    if grids:
        conds.append("upper(tx_loc) IN (" + ",".join(f"'{g}'" for g in sorted(grids)) + ")")
    match = " OR ".join(conds)
    sql = (f"SELECT toUnixTimestamp(time) AS t, tx_sign, tx_loc, power, rx_sign FROM wspr.rx "
           f"WHERE band = {band(freq_hz)} AND time IN ({times}) AND ({match}) FORMAT JSON")
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode({"query": sql}),
                                 headers={"User-Agent": "wsprpool"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]
    out: dict[float, list[Row]] = {}
    for row in data:
        out.setdefault(float(row["t"]), []).append(
            (row["tx_sign"], row["tx_loc"].upper(), int(row["power"]), row["rx_sign"]))
    return out


def confirm(msg: Message, rows: list[Row]) -> tuple[Message, set[str]]:
    """The message, with a hashed call named by wspr.live, and who else reported it.

    A report names a hash only when its call hashes to it from the same locator and power.
    That also overrides a name picked in-session, where two calls can share a hash.
    """
    if msg.kind == 3:
        for call, loc, power, _ in rows:
            if loc == msg.grid and power == msg.power and callhash(call) == msg.callhash:
                msg = Message(call, msg.grid, msg.power, msg.callhash)
                break
    if not msg.call:
        return msg, set()
    return msg, {rx for call, loc, power, rx in rows
                 if call == msg.call and power == msg.power
                 and (msg.kind == 2 or loc[:4] == msg.grid[:4])}
