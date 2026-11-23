"""Command line: sim, record and decode."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ._version import __version__
from ._decode import Slot, Spot
from ._kiwi import Receiver, record, usable
from ._report import Entry, decode_session
from ._session import load, save
from ._sim import gains, sweep


def _floats(text: str) -> list[float]:
    lo, _, hi = text.partition(":")
    return [float(lo)] if not hi else list(np.arange(float(lo), float(hi) + 1e-9, 1.0))


def cmd_sim(a: argparse.Namespace) -> int:
    if not 0 <= a.shared <= 1:
        raise ValueError(f"--shared is a fraction between 0 and 1, not {a.shared:g}")
    if not 0 <= a.freq_spread < float("inf"):
        raise ValueError(f"--freq-spread is a spread in Hz, 0 or more, not {a.freq_spread:g}")
    ns = list(dict.fromkeys(int(n) for n in a.receivers.split(",")))
    snrs = _floats(a.snr)
    print(f"decode rate over {a.trials} slots per cell, doppler {a.doppler} Hz, "
          f"shared noise {a.shared:g}, receiver frequency spread {a.freq_spread:g} Hz")
    print("snr dB  " + "".join(f"{f'N={n}':>8}" for n in ns))
    row: list[float] = []

    def show(snr: float, n: int, rate: float) -> None:
        row.append(rate)
        if n == ns[-1]:
            print(f"{snr:6.1f}  " + "".join(f"{r:8.2f}" for r in row), flush=True)
            row.clear()

    rates = sweep(ns, snrs, a.trials, a.doppler, a.shared, a.freq_spread, a.seed, show)
    print()
    print("50% threshold and gain over the first column, each with one standard error "
          "(ideal gain is 10 log10 N)")
    for g in gains(ns, snrs, rates, a.trials):
        if g["threshold"] is None or g["error"] is None:
            print(f"  N={g['n']:<3} threshold outside the SNR range")
            continue
        gain = ""
        if g["gain"] is not None:
            gain = f"gain {g['gain']:4.1f} +- {g['gain_error']:.1f} dB, ideal {g['ideal']:4.1f}"
        print(f"  N={g['n']:<3} {g['threshold']:6.1f} +- {g['error']:.1f} dB  {gain}")
    return 0


def cmd_record(a: argparse.Namespace) -> int:
    tool = a.kiwirecorder or os.environ.get("WSPRPOOL_KIWIRECORDER")
    if not tool or not Path(tool).is_file():
        print("need the path to kiwirecorder.py: pass --kiwirecorder or set "
              "WSPRPOOL_KIWIRECORDER (see the README)", file=sys.stderr)
        return 2
    ok = usable([Receiver.parse(text) for text in a.receivers])
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


def _write_csv(path: Path, rows: list[Entry], freq_hz: float) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["utc", "mhz", "snr", "dt", "drift", "metric", "call", "grid", "power",
                    "found_by", "receivers_alone", "wspr_live_reporters"])
        for e in rows:
            m, spot = e.spot.message, e.spot
            w.writerow([
                datetime.fromtimestamp(e.minute, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{(freq_hz + spot.freq) / 1e6:.6f}", f"{max(spot.snr.values()):.1f}",
                f"{spot.dt:.2f}", f"{spot.drift:.1f}", f"{spot.metric:.1f}",
                m.call or f"<{m.callhash}>", m.grid, m.power, e.tag, " ".join(e.alone),
                "" if e.others is None else len(e.others),
            ])


def cmd_decode(a: argparse.Namespace) -> int:
    if a.csv and not a.csv.resolve().parent.is_dir():
        raise ValueError(f"can't write {a.csv}: {a.csv.resolve().parent} is not a directory")
    freq_khz, streams = load(a.file)
    print(f"{a.file}: {len(streams)} receivers at {freq_khz:g} kHz")

    def progress(minute: float | None, res: Slot | None, done: int, total: int) -> None:
        if minute is None or res is None:
            return
        print(f"{datetime.fromtimestamp(minute, timezone.utc):%H:%M}  "
              + "  ".join(f"{n}={len(s)}" for n, s in res.alone.items())
              + f"  combined={len(res.combined)}", file=sys.stderr, flush=True)

    rep = decode_session(freq_khz, streams, a.check, progress)
    if rep.check_error:
        print(f"wspr.live check failed: {rep.check_error}", file=sys.stderr)

    print()
    print(HEADER)
    for e in rep.entries:
        print(_row(e.minute, rep.freq_hz, e.spot, e.alone, e.tag, e.others))
    if a.csv:
        _write_csv(a.csv, rep.entries, rep.freq_hz)

    print()
    for name in rep.receivers:
        print(f"{name:<32} {rep.alone_count(name):5d} alone")
    print(f"{'any receiver alone':<32} {rep.any_alone:5d}")
    if len(streams) > 1:
        print(f"{'combined':<32} {rep.combined:5d}")
        line = f"{'combined only':<32} {rep.combined_only:5d}"
        if rep.checked:
            line += f"  ({rep.confirmed} also reported to wspr.live by other stations)"
        print(line)
        if rep.correlation is not None:
            print(f"{'LLR error correlation':<32} {rep.correlation[0]:5.2f}  (over "
                  f"{rep.correlation[1]} receiver pairs at combined spots; 0 means independent "
                  "receivers)")
    return 0


def cmd_ui(a: argparse.Namespace) -> int:
    from ._ui import serve

    serve(a.port, a.browser, a.dir)
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

    u = sub.add_parser("ui", help="pick receivers, record, decode and simulate in your browser")
    u.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    u.add_argument("--dir", type=Path, default=None,
                   help="folder for session files (default: the current one)")
    u.add_argument("--no-browser", dest="browser", action="store_false",
                   help="print the address instead of opening a browser")
    u.set_defaults(fn=cmd_ui)

    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        print(f"wsprpool: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
