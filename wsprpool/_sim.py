"""Synthetic WSPR slots heard by several receivers, for measuring the combining gain."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._decode import LEAD, decode_slot
from ._demod import FS, SPS, TONE_OFFSETS, WINDOW
from ._wspr import Message, symbols


@dataclass
class Channel:
    snr_db: float
    freq_error: float = 0.0
    time_error: float = 0.0
    doppler: float = 0.0


def waveform(msg: Message, freq: float, drift: float = 0.0) -> np.ndarray:
    """Continuous-phase 4-FSK at 375 Hz baseband with unit amplitude.

    drift moves the frequency linearly by that many Hz from the first symbol to the last.
    """
    tones = symbols(msg)
    k = np.arange(len(tones) * SPS) - len(tones) * SPS / 2
    inst = np.repeat(freq + TONE_OFFSETS[tones], SPS) + drift * k / (len(tones) * SPS)
    return np.exp(2j * np.pi * np.cumsum(inst) / FS)


def fading(n: int, doppler: float, rng: np.random.Generator) -> np.ndarray:
    """Rayleigh fading gain with unit mean power and a Gaussian Doppler spectrum.

    doppler is the two-sigma spread, as in the Watterson HF channel model (ITU-R F.1487).
    """
    if doppler <= 0:
        return np.ones(n, complex)
    shape = np.sqrt(np.exp(-np.fft.fftfreq(n, 1 / FS) ** 2 / (2 * (doppler / 2) ** 2)))
    white = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
    return np.fft.ifft(np.fft.fft(white) * shape) / np.sqrt(np.mean(shape**2))


def slot(
    msg: Message,
    freq: float,
    channels: list[Channel],
    start: float = 1.0,
    rng: np.random.Generator | None = None,
    noise: bool = True,
    drift: float = 0.0,
    shared: float = 0.0,
) -> list[np.ndarray]:
    """One decode window per receiver: noise of unit variance per sample plus the signal.

    snr_db uses the WSPR convention (signal power over noise in 2500 Hz). Pass
    noise=False to get the signal alone, for adding more stations to a slot. shared is the
    fraction of each receiver's noise power that is the same at every receiver, like
    interference that reaches all the sites at once.
    """
    rng = rng or np.random.default_rng()
    common = None
    if noise and shared:
        common = (rng.normal(size=WINDOW) + 1j * rng.normal(size=WINDOW)) / np.sqrt(2)
    out = []
    for ch in channels:
        sig = waveform(msg, freq + ch.freq_error, drift)
        amp = np.sqrt(10 ** (ch.snr_db / 10) * 2500 / FS)
        sig = amp * sig * fading(len(sig), ch.doppler, rng)
        x = np.zeros(WINDOW, complex)
        if noise:
            own = (rng.normal(size=WINDOW) + 1j * rng.normal(size=WINDOW)) / np.sqrt(2)
            x += own if common is None else np.sqrt(1 - shared) * own + np.sqrt(shared) * common
        i = int(round((LEAD + start + ch.time_error) * FS))
        x[i : i + len(sig)] += sig[: WINDOW - i]
        out.append(x)
    return out


def random_message(rng: np.random.Generator) -> Message:
    def pick(chars: str, n: int = 1) -> str:
        return "".join(chars[int(i)] for i in rng.integers(len(chars), size=n))

    letters, digits = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789"
    call = pick(letters, 2) + pick(digits) + pick(letters, int(rng.integers(1, 4)))
    grid = pick(letters[:18], 2) + pick(digits, 2)
    return Message(call, grid, int(rng.choice([23, 30, 33, 37])))


def decode_rate(
    n: int, snr_db: float, trials: int, doppler: float, rng: np.random.Generator,
    shared: float = 0.0, freq_spread: float = 0.03,
) -> float:
    """Fraction of slots where the transmitted message is decoded by n combined receivers.

    Each receiver's oscillator is off by a normal draw with sd freq_spread Hz.
    """
    hits = 0
    for _ in range(trials):
        msg = random_message(rng)
        chans = [
            Channel(snr_db, rng.normal(0, freq_spread), rng.normal(0, 0.01), doppler)
            for _ in range(n)
        ]
        xs = slot(msg, rng.uniform(-90, 90), chans, rng=rng, shared=shared)
        spots = decode_slot({str(i): x for i, x in enumerate(xs)}, alone=False, passes=1).combined
        hits += any(s.message == msg for s in spots)
    return hits / trials


def threshold(snrs: list[float], rates: list[float], level: float = 0.5) -> float | None:
    """SNR where the decode rate crosses `level`, by linear interpolation."""
    for (s0, r0), (s1, r1) in zip(zip(snrs, rates), zip(snrs[1:], rates[1:])):
        if (r0 - level) * (r1 - level) <= 0 and r0 != r1:
            return s0 + (level - r0) * (s1 - s0) / (r1 - r0)
    return None


def threshold_error(snrs: list[float], rates: list[float], trials: int,
                    level: float = 0.5) -> float | None:
    """One standard error of `threshold`, from the binomial spread of a rate near `level`.

    The rate measured at the crossing could be off by sqrt(p(1-p)/trials); dividing by the
    curve's slope there turns that into decibels.
    """
    for (s0, r0), (s1, r1) in zip(zip(snrs, rates), zip(snrs[1:], rates[1:])):
        if (r0 - level) * (r1 - level) <= 0 and r0 != r1:
            return float(np.sqrt(level * (1 - level) / trials) * abs((s1 - s0) / (r1 - r0)))
    return None
