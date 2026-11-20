"""Recording IQ from KiwiSDRs with kiwirecorder.py, and reading its GPS-stamped wav files."""

from __future__ import annotations

import re
import struct
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc).timestamp()
WEEK = 604800
NO_FIX = (254, 255)
GPS_UTC = 18


@dataclass(frozen=True)
class Receiver:
    host: str
    port: int = 8073

    @property
    def label(self) -> str:
        return re.sub(r"[^a-z0-9.-]", "-", f"{self.host}-{self.port}".lower())

    @classmethod
    def parse(cls, text: str) -> Receiver:
        host, _, port = text.rpartition(":") if ":" in text else (text, "", "8073")
        return cls(host, int(port))


def status(rx: Receiver, timeout: float = 10.0) -> dict[str, str]:
    """The key=value lines a KiwiSDR serves at /status."""
    with urllib.request.urlopen(f"http://{rx.host}:{rx.port}/status", timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def unusable(st: dict[str, str]) -> str | None:
    """Why a receiver can't be used right now, or None if it can."""
    if st.get("offline") == "yes" or st.get("status", "active") != "active":
        return "offline"
    if int(st.get("gps_good", "0") or 0) <= 0:
        return "no GPS fix, so its timestamps can't be lined up with the others"
    if int(st.get("users", "0")) >= int(st.get("users_max", "0")):
        return f"all {st.get('users_max')} slots in use"
    # the owner allows no recording programs; the receiver takes the connection, then sends nothing
    if st.get("ext_api") == "0":
        return "its owner doesn't allow recording programs like kiwirecorder (ext_api is 0)"
    return None


# a receiver that is streaming has written well past its wav header by now
SILENT_AFTER = 45.0
# what kiwiclient logs when the receiver ends the session, 5 seconds before it reconnects;
# its busy-retry line differs in the delay
ENDED = "Reconnecting after 5 seconds"


def _stop(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
        p.wait()


def _watch(procs: dict[Receiver, subprocess.Popen], out: Path, start: float,
           deadline: float, poll: float = 1.0) -> None:
    """Wait for the recorders, stopping any the receiver has cut off or never served.

    A receiver ends the session at its owner's time limit, and kiwiclient would quietly
    reconnect and carry on past it. Some owners also limit recording programs in ways
    /status doesn't show: the receiver takes the connection and then sends nothing. Both
    are stopped here, so the owner's rules hold and no slot is held for nothing.
    """
    silent_checked = False
    while time.time() < deadline and any(p.poll() is None for p in procs.values()):
        for rx, p in procs.items():
            if p.poll() is not None:
                continue
            log = (out / rx.label / "kiwirecorder.log").read_text(errors="replace")
            ended = next((line for line in log.splitlines() if ENDED in line), None)
            if ended:
                _stop(p)
                held = time.time() - start
                reason = ended.split(ENDED)[0].rstrip(" .").rsplit(f"{rx.port} ", 1)[-1]
                print(f"{rx.host}:{rx.port}: {reason} after "
                      + (f"{held / 60:.0f} min" if held >= 60 else f"{held:.0f} s")
                      + "; keeping what was recorded and not reconnecting, since this is "
                      "usually the owner's time limit", file=sys.stderr)
        if not silent_checked and time.time() >= start + SILENT_AFTER:
            silent_checked = True
            for rx, p in procs.items():
                fresh = [w for w in (out / rx.label).glob("*.wav")
                         if w.stat().st_mtime >= start - 30]
                if p.poll() is None and not any(w.stat().st_size > 4096 for w in fresh):
                    _stop(p)
                    print(f"{rx.host}:{rx.port} connected but sent nothing in "
                          f"{SILENT_AFTER:.0f} s, so it was dropped; its owner may limit "
                          "recording programs", file=sys.stderr)
        time.sleep(poll)


def record(
    receivers: list[Receiver], freq_khz: float, minutes: int, out: Path, kiwirecorder: Path
) -> dict[Receiver, Path]:
    """Record every receiver at once, starting just before the next even minute.

    Returns the wav file each receiver produced; receivers that failed are left out.
    """
    now = time.time()
    start = (now // 120 + 1) * 120 - 8
    if start - now < 2:
        start += 120
    time.sleep(start - now)
    procs = {}
    deadline = start + minutes * 60 + 120
    try:
        for rx in receivers:
            d = out / rx.label
            d.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                str(kiwirecorder),
                "-s",
                rx.host,
                "-p",
                str(rx.port),
                "-f",
                f"{freq_khz:.3f}",
                "-m",
                "iq",
                "-w",
                "--tlimit",
                str(minutes * 60 + 20),
                "-d",
                str(d),
                "-u",
                "wsprpool",
                "--connect-retries",
                "2",
                # kiwiclient retries a full receiver forever unless this is above 0
                "--busy-retries",
                "1",
                # the session-ended message is logged at info level
                "--log-level",
                "info",
            ]
            # kiwirecorder logs every GPS block; a pipe nobody reads fills up and stalls it
            with open(d / "kiwirecorder.log", "w") as log:
                procs[rx] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        _watch(procs, out, start, deadline)
    finally:
        # a stuck connection still holds one of the owner's few slots
        for p in procs.values():
            _stop(p)

    files = {}
    for rx, p in procs.items():
        # the work directory may hold wavs from an earlier run
        wavs = [w for w in (out / rx.label).glob("*.wav") if w.stat().st_mtime >= start - 30]
        if wavs:
            files[rx] = max(wavs, key=lambda w: w.stat().st_size)
        else:
            log_text = (out / rx.label / "kiwirecorder.log").read_text(errors="replace")
            tail = " | ".join(log_text.strip().splitlines()[-3:])
            print(
                f"{rx.host}:{rx.port} recorded nothing (exit {p.returncode}): {tail}",
                file=sys.stderr,
            )
    return files


@dataclass
class KiwiWav:
    iq: np.ndarray
    rate: int
    blocks: list[tuple[int, float, int]]


def parse_wav(data: bytes) -> KiwiWav:
    """Parse a kiwirecorder -w file: a 'kiwi' GPS chunk before every 'data' chunk."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, rate, channels = 12, 0, 0
    parts: list[np.ndarray] = []
    blocks: list[tuple[int, float, int]] = []
    pending: tuple[float, int] | None = None
    count = 0
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from("<4sI", data, pos)
        body = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            channels, rate = struct.unpack_from("<HI", body, 2)
        elif cid == b"kiwi":
            age, _, sec, nsec = struct.unpack_from("<BBII", body)
            pending = (sec + 1e-9 * nsec, age)
        elif cid == b"data":
            s = np.frombuffer(body[: len(body) // 4 * 4], dtype="<i2")
            if pending is not None:
                blocks.append((count, pending[0], pending[1]))
                pending = None
            parts.append(s)
            count += len(s) // 2
        pos += 8 + size + (size & 1)
    if channels != 2:
        raise ValueError(f"expected 2-channel IQ, found {channels} channel(s)")
    s = np.concatenate(parts) if parts else np.zeros(0, "<i2")
    iq = (s[0::2].astype(np.float32) + 1j * s[1::2].astype(np.float32)) / 32768
    return KiwiWav(iq.astype(np.complex64), rate, blocks)


def timebase(wav: KiwiWav, approx_start: float) -> tuple[float, float]:
    """UTC time of the first sample and the true sample rate, from the GPS block stamps.

    Kiwis send GPS seconds-of-week, 18 s ahead of UTC. The week is whichever puts the
    start nearest the file's wall-clock time.
    """
    # kiwirecorder stamps the very first block with zeros before any GPS data arrives
    good = [(i, t) for i, t, age in wav.blocks if age not in NO_FIX and t > 0]
    if len(good) < 2:
        raise ValueError("no GPS timestamps in this recording")
    idx = np.array([i for i, _ in good], float)
    tow = np.unwrap(np.array([t for _, t in good]), period=WEEK)
    slope, tow0 = np.polyfit(idx, tow, 1)
    base = GPS_EPOCH + (approx_start + GPS_UTC - GPS_EPOCH) // WEEK * WEEK
    t0 = min((base + w * WEEK + tow0 - GPS_UTC for w in (-1, 0, 1)),
             key=lambda t: abs(t - approx_start))
    if abs(t0 - approx_start) > 60:
        raise ValueError(f"GPS time is {t0 - approx_start:+.1f} s from the file's wall clock")
    return t0, 1.0 / slope


def start_from_name(name: str) -> float:
    """Wall-clock start that kiwirecorder writes into its default file names."""
    m = re.match(r"(\d{8}T\d{6}Z)", name)
    if not m:
        raise ValueError(f"{name!r} doesn't start with a kiwirecorder timestamp")
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
