"""Command line: sim, record and decode."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

import numpy as np

from ._version import __version__
from ._decode import Slot, Spot, decode_slot, llr_correlation
from ._kiwi import Receiver, record, status, unusable
from ._session import load, save, slots
from ._sim import decode_rate, threshold, threshold_error
from ._spots import Row, confirm, fetch
from ._wspr import callhash, resolve


def _floats(text: str) -> list[float]:
    lo, _, hi = text.partition(":")
    return [float(lo)] if not hi else list(np.arange(float(lo), float(hi) + 1e-9, 1.0))


def cmd_sim(a: argparse.Namespace) -> int:
    if not 0 <= a.shared <= 1:
        raise ValueError(f"--shared is a fraction between 0 and 1, not {a.shared:g}")
    if not 0 <= a.freq_spread < float("inf"):
        raise ValueError(f"--freq-spread is a spread in Hz, 0 or more, not {a.freq_spread:g}")
    ns = [int(n) for n in a.receivers.split(",")]
    snrs = _floats(a.snr)
    rng = np.random.default_rng(a.seed)
    print(f"decode rate over {a.trials} slots per cell, doppler {a.doppler} Hz, "
          f"shared noise {a.shared:g}, receiver frequency spread {a.freq_spread:g} Hz")
    print("snr dB  " + "".join(f"{f'N={n}':>8}" for n in ns))
    rates: dict[int, list[float]] = {n: [] for n in ns}
    for s in snrs:
        for n in ns:
            rates[n].append(decode_rate(n, s, a.trials, a.doppler, rng, a.shared,
                                        a.freq_spread))
        print(f"{s:6.1f}  " + "".join(f"{rates[n][-1]:8.2f}" for n in ns), flush=True)
    print()
    print("50% threshold and gain over the first column, each with one standard error "
          "(ideal gain is 10 log10 N)")
    base = threshold(snrs, rates[ns[0]])
    base_err = threshold_error(snrs, rates[ns[0]], a.trials)
    for n in ns:
        t = threshold(snrs, rates[n])
        err = threshold_error(snrs, rates[n], a.trials)
        if t is None or err is None:
            print(f"  N={n:<3} threshold outside the SNR range")
            continue
        gain = ""
        if base is not None and base_err is not None:
            gain = (f"gain {base - t:4.1f} +- {np.hypot(err, base_err) if n != ns[0] else 0:.1f} "
                    f"dB, ideal {10 * np.log10(n / ns[0]):4.1f}")
        print(f"  N={n:<3} {t:6.1f} +- {err:.1f} dB  {gain}")
    return 0


def cmd_record(a: argparse.Namespace) -> int:
    tool = a.kiwirecorder or os.environ.get("WSPRPOOL_KIWIRECORDER")
    if not tool or not Path(tool).is_file():
        print("need the path to kiwirecorder.py: pass --kiwirecorder or set "
              "WSPRPOOL_KIWIRECORDER (see the README)", file=sys.stderr)
        return 2
    ok = []
    for text in a.receivers:
        rx = Receiver.parse(text)
        try:
            why = unusable(status(rx))
        except (URLError, OSError, ValueError) as e:
            why = f"status check failed: {e}"
        if why:
            print(f"skipping {rx.host}:{rx.port}: {why}", file=sys.stderr)
        else:
            ok.append(rx)
    if not ok:
        print("no usable receivers", file=sys.stderr)
        return 1
    work = a.output.with_name(a.output.stem + "-wav")
    print(f"recording {len(ok)} receiver(s) at {a.freq} kHz for {a.minutes} min, "
          f"starting at the next even minute")
    files = record(ok, a.freq, a.minutes, work, Path(tool))
    if not files:
        print("every receiver failed to record", file=sys.stderr)
        return 1
    save(a.output, a.freq, files)
    print(f"wrote {a.output} ({len(files)} receivers); the raw wavs are in {work}")
    return 0


HEADER = (f"{'UTC':<16}  {'MHz':>9}  {'SNR':>5}  {'dt':>5}  {'drift':>5}  {'metric':>6}  "
          f"{'message':<20} {'found by':<14} receivers alone / wspr.live reporters")


def _row(when: float, freq_hz: float, spot: Spot, alone: list[str], tag: str,
         others: set[str] | None) -> str:
    t = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M")
    heard = ",".join(alone) if alone else "-"
    check = "" if others is None else f" / {len(others)}"
    return (f"{t}  {(freq_hz + spot.freq) / 1e6:9.6f}  {max(spot.snr.values()):5.1f}  "
            f"{spot.dt:5.2f}  {spot.drift:5.1f}  {spot.metric:6.1f}  {str(spot.message):<20} "
            f"{tag:<14} {heard}{check}")


def _tag(receivers: int, alone: list[str], in_comb: bool) -> str:
    if receivers == 1:
        return "alone"
    if in_comb:
        return "both" if alone else "combined only"
    return "alone only"


def _resolve_hashes(results: list[tuple[float, Slot]]) -> None:
    """Name type 3 calls from any type 1 or 2 message heard in the session."""
    every = [s for _, res in results for s in res.combined + sum(res.alone.values(), [])]
    calls = {callhash(s.message.call): s.message.call for s in every if s.message.kind != 3}
    for s in every:
        s.message = resolve(s.message, calls)


def _write_csv(path: Path, rows: list[tuple[float, Spot, list[str], str, set[str] | None]],
               freq_hz: float) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["utc", "mhz", "snr", "dt", "drift", "metric", "call", "grid", "power",
                    "found_by", "receivers_alone", "wspr_live_reporters"])
        for minute, spot, alone, tag, others in rows:
            m = spot.message
            w.writerow([
                datetime.fromtimestamp(minute, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{(freq_hz + spot.freq) / 1e6:.6f}", f"{max(spot.snr.values()):.1f}",
                f"{spot.dt:.2f}", f"{spot.drift:.1f}", f"{spot.metric:.1f}",
                m.call or f"<{m.callhash}>", m.grid, m.power, tag, " ".join(alone),
                "" if others is None else len(others),
            ])


def cmd_decode(a: argparse.Namespace) -> int:
    if a.csv and not a.csv.resolve().parent.is_dir():
        raise ValueError(f"can't write {a.csv}: {a.csv.resolve().parent} is not a directory")
    freq_khz, streams = load(a.file)
    freq_hz = freq_khz * 1000
    print(f"{a.file}: {len(streams)} receivers at {freq_khz:g} kHz")

    results = []
    for minute, windows in slots(streams):
        res = decode_slot(windows)
        print(f"{datetime.fromtimestamp(minute, timezone.utc):%H:%M}  "
              + "  ".join(f"{n}={len(s)}" for n, s in res.alone.items())
              + f"  combined={len(res.combined)}", file=sys.stderr, flush=True)
        results.append((minute, res))
    _resolve_hashes(results)

    # (slot, message) -> best spot, receivers that decoded it alone, in the combination,
    # and how many receivers covered that slot
    found: dict[tuple[float, str], tuple[Spot, list[str], bool, int]] = {}
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
            found[(minute, key)] = (spot, alone, key in comb, live)

    seen: dict[float, list[Row]] | None = None
    if a.check:
        try:
            seen = fetch([m for m, _ in found], [s.message for s, _, _, _ in found.values()],
                         freq_hz)
        except (URLError, OSError, ValueError) as e:
            print(f"wspr.live check failed: {e}", file=sys.stderr)

    print()
    print(HEADER)
    combined = combined_only = confirmed = 0
    table = []
    for (minute, _), (spot, alone, in_comb, live) in sorted(found.items()):
        tag = _tag(live, alone, in_comb)
        others = None
        if seen is not None:
            spot.message, others = confirm(spot.message, seen.get(minute, []))
        combined += in_comb
        if tag == "combined only":
            combined_only += 1
            confirmed += bool(others)
        print(_row(minute, freq_hz, spot, alone, tag, others))
        table.append((minute, spot, alone, tag, others))
    if a.csv:
        _write_csv(a.csv, table, freq_hz)

    print()
    for st in streams:
        n = sum(st.label in alone for _, alone, _, _ in found.values())
        print(f"{st.label:<32} {n:5d} alone")
    print(f"{'any receiver alone':<32} {sum(bool(al) for _, al, _, _ in found.values()):5d}")
    if len(streams) > 1:
        print(f"{'combined':<32} {combined:5d}")
        line = f"{'combined only':<32} {combined_only:5d}"
        if seen is not None:
            line += f"  ({confirmed} also reported to wspr.live by other stations)"
        print(line)
        cor = llr_correlation([s for _, res in results if len(res.alone) > 1
                               for s in res.combined])
        if cor is not None:
            print(f"{'LLR error correlation':<32} {cor[0]:5.2f}  (over {cor[1]} receiver pairs at "
                  "combined spots; 0 means independent receivers)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wsprpool", description="Decode WSPR by pooling the soft "
                                "decisions of several KiwiSDRs.")
    p.add_argument("--version", action="version", version=f"wsprpool {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sim", help="measure combining gain on synthetic signals")
    s.add_argument("--receivers", default="1,2,4,8", help="comma-separated receiver counts")
    s.add_argument("--snr", default="-36:-26", help="SNR or LO:HI range in dB (1 dB steps)")
    s.add_argument("--trials", type=int, default=20)
    s.add_argument("--doppler", type=float, default=0.0,
                   help="Rayleigh fading spread in Hz, independent per receiver (0 = none)")
    s.add_argument("--shared", type=float, default=0.0,
                   help="fraction of noise power that is the same at every receiver (0 to 1)")
    s.add_argument("--freq-spread", type=float, default=0.03,
                   help="sd in Hz of each receiver's own frequency error")
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=cmd_sim)

    r = sub.add_parser("record", help="record several KiwiSDRs at once")
    r.add_argument("receivers", nargs="+", metavar="HOST[:PORT]")
    r.add_argument("-o", "--output", type=Path, required=True, help="session .londopy file")
    r.add_argument("--freq", type=float, default=14097.1, help="centre in kHz (20 m WSPR)")
    r.add_argument("--minutes", type=int, default=10)
    r.add_argument("--kiwirecorder", help="path to kiwiclient's kiwirecorder.py")
    r.set_defaults(fn=cmd_record)

    d = sub.add_parser("decode", help="decode a session alone and combined")
    d.add_argument("file", type=Path)
    d.add_argument("--no-check", dest="check", action="store_false",
                   help="skip looking spots up on wspr.live")
    d.add_argument("--csv", type=Path, help="also write the spots to this CSV file")
    d.set_defaults(fn=cmd_decode)

    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        print(f"wsprpool: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
