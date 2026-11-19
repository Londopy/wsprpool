"""Ground truth from the public wspr.live spot database."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://db1.wspr.live/"
CALL = re.compile(r"[A-Z0-9/]{3,10}")


def band(freq_hz: float) -> int:
    """wspr.live's band number: the integer MHz for every HF band."""
    return int(freq_hz // 1_000_000)


def reporters(slots: list[float], calls: set[str], freq_hz: float,
              timeout: float = 30.0) -> dict[tuple[float, str, str, int], set[str]]:
    """Who reported each (slot start, call, 4-character grid, power) on this band."""
    calls = {c for c in calls if CALL.fullmatch(c)}
    if not slots or not calls:
        return {}
    times = ",".join(
        "'" + datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "'"
        for t in sorted(set(slots)))
    names = ",".join(f"'{c}'" for c in sorted(calls))
    sql = (f"SELECT toUnixTimestamp(time) AS t, tx_sign, tx_loc, power, rx_sign FROM wspr.rx "
           f"WHERE band = {band(freq_hz)} AND time IN ({times}) AND tx_sign IN ({names}) "
           f"FORMAT JSON")
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode({"query": sql}),
                                 headers={"User-Agent": "wsprpool"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rows = json.load(r)["data"]
    out: dict[tuple[float, str, str, int], set[str]] = {}
    for row in rows:
        key = (float(row["t"]), row["tx_sign"], row["tx_loc"][:4].upper(), int(row["power"]))
        out.setdefault(key, set()).add(row["rx_sign"])
    return out
