"""Recording IQ from KiwiSDRs with kiwirecorder.py, and reading its GPS-stamped wav files."""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.error import URLError
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


# the public receiver list the KiwiSDR map tools use, generated from kiwisdr.com/public
LIST_URL = "http://rx.linkfanel.net/kiwisdr_com.js"


def parse_list(text: str) -> list[dict[str, str]]:
    """The receivers in kiwisdr_com.js, a JavaScript array with trailing commas."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("the public receiver list isn't a JavaScript array any more")
    return json.loads(re.sub(r",\s*([\]}])", r"\1", text[start: end + 1]))


def public_receivers(timeout: float = 30.0) -> list[dict[str, str]]:
    req = urllib.request.Request(LIST_URL, headers={"User-Agent": "wsprpool"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_list(r.read().decode("utf-8", "replace"))


def _stderr(text: str) -> None:
    print(text, file=sys.stderr)


def next_start(now: float) -> float:
    """When recording starts: 8 seconds before the next even minute, at least 2 s away."""
    start = (now // 120 + 1) * 120 - 8
    return start + 120 if start - now < 2 else start


def usable(receivers: list[Receiver], log: Callable[[str], None] = _stderr) -> list[Receiver]:
    """The receivers whose /status says they can record now, each once; the rest are logged."""
    ok = []
    for rx in dict.fromkeys(receivers):
        try:
            why = unusable(status(rx))
        except (URLError, OSError, ValueError) as e:
            why = f"status check failed: {e}"
        if why:
            log(f"skipping {rx.host}:{rx.port}: {why}")
        else:
            ok.append(rx)
    return ok


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


def _span(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def _watch(procs: dict[Receiver, subprocess.Popen], out: Path, start: float,
           deadline: float, poll: float = 1.0, log: Callable[[str], None] = _stderr,
           stop: threading.Event | None = None, quiet: float = SILENT_AFTER,
           end: float | None = None) -> None:
    """Wait for the recorders, stopping any the receiver has cut off or stopped feeding.

    A receiver ends the session at its owner's time limit, and kiwiclient would quietly
    reconnect and carry on past it. Some owners also limit recording programs in ways
    /status doesn't show: the receiver keeps the connection but sends nothing, from the
    start or after a few seconds. Both are stopped here, so the owner's rules hold and no
    slot is held for nothing. A recorder that exits well before end is reported, since a
    short recording otherwise looks like a whole one.
    """
    sizes: dict[Receiver, int] = {}
    gone: set[Receiver] = set()
    grew = {rx: time.time() for rx in procs}
    while True:
        for rx, p in procs.items():
            size = max((w.stat().st_size for w in (out / rx.label).glob("*.wav")
                        if w.stat().st_mtime >= start - 30), default=0)
            if p.poll() is not None:
                if rx not in gone and end is not None and time.time() < end - 30:
                    what = "so its recording is short" if size else "and recorded nothing"
                    log(f"{rx.host}:{rx.port} stopped by itself after "
                        f"{_span(time.time() - start)} (exit {p.returncode}), {what}; "
                        f"{out / rx.label / 'kiwirecorder.log'} may say why")
                gone.add(rx)
                continue
            text = (out / rx.label / "kiwirecorder.log").read_text(errors="replace")
            ended = next((line for line in text.splitlines() if ENDED in line), None)
            if ended:
                _stop(p)
                gone.add(rx)
                reason = ended.split(ENDED)[0].rstrip(" .").rsplit(f"{rx.port} ", 1)[-1]
                log(f"{rx.host}:{rx.port}: {reason} after {_span(time.time() - start)}; keeping "
                    "what was recorded and not reconnecting, since the owner's time limit is "
                    "the usual reason")
                continue
            if size != sizes.get(rx):
                sizes[rx], grew[rx] = size, time.time()
            elif time.time() - grew[rx] >= quiet:
                _stop(p)
                gone.add(rx)
                if size > 4096:
                    log(f"{rx.host}:{rx.port} stopped sending samples after "
                        f"{_span(grew[rx] - start)}, so it was dropped and what it sent was "
                        "kept; its owner may limit recording programs")
                else:
                    log(f"{rx.host}:{rx.port} connected but sent nothing in {quiet:.0f} s, "
                        "so it was dropped; its owner may limit recording programs")
        if (time.time() >= deadline or len(gone) == len(procs)
                or (stop is not None and stop.is_set())):
            return
        time.sleep(poll)


def record(
    receivers: list[Receiver], freq_khz: float, minutes: int, out: Path, kiwirecorder: Path,
    log: Callable[[str], None] = _stderr, stop: threading.Event | None = None,
    start: float | None = None,
) -> dict[Receiver, Path]:
    """Record every receiver at once, starting just before the next even minute.

    Returns the wav file each receiver produced; receivers that failed are left out.
    Setting stop ends the recording early and keeps what was recorded. start overrides
    when recording begins, for a caller that has already shown it to someone.
    """
    receivers = list(dict.fromkeys(receivers))
    now = time.time()
    if start is None:
        start = next_start(now)
    if stop is None:
        time.sleep(start - now)
    elif stop.wait(start - now):
        return {}
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
            with open(d / "kiwirecorder.log", "w") as handle:
                procs[rx] = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT)
        _watch(procs, out, start, deadline, log=log, stop=stop, end=start + minutes * 60)
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
            log(f"{rx.host}:{rx.port} recorded nothing (exit {p.returncode}): {tail}")
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
