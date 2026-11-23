"""Recording sessions saved as .londopy files, and cutting them into aligned WSPR slots."""

from __future__ import annotations

import os
import re
import sys
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ._version import __version__
from ._decode import LEAD
from ._demod import WINDOW, baseband
from ._kiwi import Receiver, parse_wav, start_from_name, timebase

FORMAT = 1
SCHEMA = 1
MAX_ENTRY = 4 << 30


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(path: Path, freq_khz: float, files: dict[Receiver, Path]) -> None:
    """Write the recorded wavs and their metadata into one .londopy file."""
    lines = [f"schema = {SCHEMA}", f"freq_khz = {freq_khz}", ""]
    for rx, wav in files.items():
        lines += ["[[receivers]]", f"label = {_q(rx.label)}", f"host = {_q(rx.host)}",
                  f"port = {rx.port}", f"wav = {_q(rx.label + '.wav')}",
                  f"started = {_q(_iso(start_from_name(wav.name)))}", ""]
    manifest = "\n".join([
        f"format = {FORMAT}", 'app = "wsprpool"', 'kind = "recording"',
        f"title = {_q(f'WSPR {freq_khz:g} kHz, {len(files)} receivers')}",
        f"app_version = {_q(__version__)}",
        f"created = {_q(_iso(datetime.now(timezone.utc).timestamp()))}",
        'homepage = "https://github.com/Londopy/wsprpool"', "",
    ])
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr("londopy.toml", manifest, compress_type=zipfile.ZIP_STORED)
        z.writestr("session.toml", "\n".join(lines), compress_type=zipfile.ZIP_DEFLATED)
        for rx, wav in files.items():
            z.write(wav, rx.label + ".wav", compress_type=zipfile.ZIP_DEFLATED)
    os.replace(tmp, path)


def _entry(z: zipfile.ZipFile, name: str) -> bytes:
    if ".." in name.split("/") or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"refusing entry name {name!r}")
    info = z.getinfo(name)
    if info.file_size > MAX_ENTRY:
        raise ValueError(f"{name} is {info.file_size} bytes, over the {MAX_ENTRY} byte limit")
    return z.read(info)


@dataclass
class Stream:
    label: str
    start: float
    x: np.ndarray
    rate: float

    @property
    def end(self) -> float:
        return self.start + len(self.x) / self.rate


def _stderr(text: str) -> None:
    print(text, file=sys.stderr)


def load(path: Path, log: Callable[[str], None] = _stderr) -> tuple[float, list[Stream]]:
    """Read a recording session: its frequency and each receiver's 375 Hz baseband."""
    with zipfile.ZipFile(path) as z:
        head = tomllib.loads(_entry(z, "londopy.toml").decode())
        if head.get("app") != "wsprpool" or head.get("kind") != "recording":
            raise ValueError(f"{path} is a {head.get('app')} {head.get('kind')} file, "
                             "not a wsprpool recording")
        if head.get("format", 0) > FORMAT:
            raise ValueError(f"{path} was written by a newer wsprpool; update to read it")
        meta = tomllib.loads(_entry(z, "session.toml").decode())
        streams = []
        for r in meta["receivers"]:
            wav = parse_wav(_entry(z, r["wav"]))
            approx = datetime.strptime(r["started"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
            try:
                t0, rate = timebase(wav, approx)
            except ValueError as e:
                log(f"skipping {r['label']}: {e}")
                continue
            streams.append(Stream(r["label"], t0, *baseband(wav.iq, rate)))
    if not streams:
        raise ValueError(f"{path} has no receiver with usable GPS timestamps")
    return float(meta["freq_khz"]), streams


def window(s: Stream, minute: float) -> np.ndarray | None:
    i = int(round((minute - LEAD - s.start) * s.rate))
    if i < 0 or i + WINDOW > len(s.x):
        return None
    return s.x[i: i + WINDOW]


def slots(streams: list[Stream]) -> Iterator[tuple[float, dict[str, np.ndarray]]]:
    """Every even minute that at least one stream fully covers, with the covering windows."""
    first = min(s.start for s in streams)
    last = max(s.end for s in streams)
    minute = (first + LEAD) // 120 * 120
    while minute <= last:
        ws = {s.label: w for s in streams if (w := window(s, minute)) is not None}
        if ws:
            yield minute, ws
        minute += 120
