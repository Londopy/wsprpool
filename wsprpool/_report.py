"""Decoding a whole recorded session into one table of spots, for the CLI and the GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import URLError

from ._decode import Slot, Spot, decode_slot, llr_correlation
from ._session import Stream, slots
from ._spots import Row, confirm, fetch
from ._wspr import callhash, resolve


@dataclass
class Entry:
    """One message in one slot: its best spot, who decoded it alone, and how it was found."""

    minute: float
    spot: Spot
    alone: list[str]
    tag: str
    others: set[str] | None = None


@dataclass
class Report:
    freq_hz: float
    receivers: list[str]
    entries: list[Entry]
    check_error: str | None = None
    correlation: tuple[float, int] | None = None
    checked: bool = False

    def alone_count(self, receiver: str) -> int:
        return sum(receiver in e.alone for e in self.entries)

    @property
    def any_alone(self) -> int:
        return sum(bool(e.alone) for e in self.entries)

    @property
    def combined(self) -> int:
        return sum(e.tag in ("both", "combined only") for e in self.entries)

    @property
    def combined_only(self) -> int:
        return sum(e.tag == "combined only" for e in self.entries)

    @property
    def confirmed(self) -> int:
        return sum(e.tag == "combined only" and bool(e.others) for e in self.entries)


def tag(receivers: int, alone: list[str], in_comb: bool) -> str:
    if receivers == 1:
        return "alone"
    if in_comb:
        return "both" if alone else "combined only"
    return "alone only"


def resolve_hashes(results: list[tuple[float, Slot]]) -> None:
    """Name type 3 calls from any type 1 or 2 message heard in the session."""
    every = [s for _, res in results for s in res.combined + sum(res.alone.values(), [])]
    calls = {callhash(s.message.call): s.message.call for s in every if s.message.kind != 3}
    for s in every:
        s.message = resolve(s.message, calls)


def entries(results: list[tuple[float, Slot]]) -> list[Entry]:
    """Every message of every slot once, with its best spot, sorted by time and message."""
    out = []
    for minute, res in results:
        live = len(res.alone)
        best: dict[str, Spot] = {}
        for spot in [s for spots in res.alone.values() for s in spots] + res.combined:
            key = str(spot.message)
            if key not in best or best[key].metric < spot.metric:
                best[key] = spot
        comb = {str(s.message) for s in res.combined} if live > 1 else set()
        for key, spot in best.items():
            alone = [n for n, spots in res.alone.items() if key in {str(s.message) for s in spots}]
            out.append(Entry(minute, spot, alone, tag(live, alone, key in comb)))
    return sorted(out, key=lambda e: (e.minute, str(e.spot.message)))


def check(report: Report) -> None:
    """Look every entry up on wspr.live; a failure is kept in check_error, not raised."""
    try:
        seen: dict[float, list[Row]] = fetch(
            [e.minute for e in report.entries], [e.spot.message for e in report.entries],
            report.freq_hz)
    except (URLError, OSError, ValueError) as err:
        report.check_error = str(err)
        return
    report.checked = True
    for e in report.entries:
        e.spot.message, e.others = confirm(e.spot.message, seen.get(e.minute, []))


def decode_session(
    freq_khz: float,
    streams: list[Stream],
    wspr_live: bool = True,
    on_slot: Callable[[float | None, Slot | None, int, int], None] | None = None,
    stop: threading.Event | None = None,
) -> Report:
    """Decode every slot of a session, alone and combined, and build the spot table.

    on_slot gets each slot as it finishes, with how many are done and how many there are;
    it is first called with no slot and none done, as soon as the count is known. Setting
    stop ends the run after the current slot, with the table of what was decoded.
    """
    todo = list(slots(streams))
    if on_slot is not None:
        on_slot(None, None, 0, len(todo))
    results = []
    for i, (minute, windows) in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        res = decode_slot(windows)
        results.append((minute, res))
        if on_slot is not None:
            on_slot(minute, res, i + 1, len(todo))
    resolve_hashes(results)
    report = Report(freq_khz * 1000, [s.label for s in streams], entries(results))
    if wspr_live:
        check(report)
    report.correlation = llr_correlation(
        [s for _, res in results if len(res.alone) > 1 for s in res.combined])
    return report
