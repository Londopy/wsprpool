"""Joint search and soft-combined decoding of one 2-minute WSPR slot across receivers."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ._demod import FS, noise_power, peaks, soft_bits, spectrum, sync_score, tone_sums
from ._fano import fano
from ._wspr import Message, deinterleave, unpack

# windows start this long before the even minute, so early transmitters are still caught
LEAD = 1.0
COARSE_DF = np.arange(-1.0, 1.01, 0.1)
COARSE_LAGS = np.arange(0, int(5 * FS), 16)
FINE_DF = np.arange(-0.1, 0.101, 0.025)


@dataclass
class Spot:
    message: Message
    freq: float
    dt: float
    metric: float
    snr: dict[str, float] = field(default_factory=dict)

    @property
    def combined_snr(self) -> float:
        return float(10 * np.log10(sum(10 ** (s / 10) for s in self.snr.values())))


@dataclass
class Slot:
    alone: dict[str, list[Spot]]
    combined: list[Spot]


def _scores(
    xs: list[np.ndarray], sig2: list[float], fs: np.ndarray, lags: np.ndarray
) -> np.ndarray:
    """Sync scores with shape (receiver, frequency, lag)."""
    return np.array([[sync_score(tone_sums(x, f, lags), s) for f in fs] for x, s in zip(xs, sig2)])


def _refine(xs: list[np.ndarray], sig2: list[float], f: float, lag: int) -> tuple[float, int]:
    fs = f + FINE_DF
    lags = np.clip(np.arange(lag - 16, lag + 17, 2), 0, COARSE_LAGS[-1])
    score = _scores(xs, sig2, fs, lags).sum(axis=0)
    i, j = np.unravel_index(int(np.argmax(score)), score.shape)
    return float(fs[i]), int(lags[j])


def _decode(
    xs: list[np.ndarray], names: list[str], f: float, lag: int, min_metric: float, max_cycles: int
) -> Spot | None:
    llr = np.zeros(162)
    snr = {}
    for n, x in zip(names, xs):
        bits, s = soft_bits(tone_sums(x, f, np.array([lag]))[:, 0, :])
        llr += bits
        snr[n] = s
    out = fano(deinterleave(llr), max_cycles=max_cycles)
    if out is None or out[1] < min_metric:
        return None
    msg = unpack(out[0])
    if msg is None:
        return None
    return Spot(msg, f, lag / FS - LEAD - 1.0, out[1], snr)


def _distinct(points: list[tuple[float, int]]) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for f, lag in points:
        if all(abs(f - g) > 0.05 or abs(lag - m) > 8 for g, m in out):
            out.append((f, lag))
    return out


def _keep(spots: dict[str, Spot], spot: Spot | None) -> None:
    if spot is not None:
        key = str(spot.message)
        if key not in spots or spots[key].metric < spot.metric:
            spots[key] = spot


def decode_slot(
    windows: dict[str, np.ndarray],
    min_metric: float = 8.0,
    max_cycles: int = 10000,
    alone: bool = True,
) -> Slot:
    """Decode one slot from each receiver alone and from all of them combined.

    Each window is 375 Hz baseband starting LEAD seconds before the even minute. With
    alone=False and several receivers, only the combination is decoded.
    """
    power = {n: noise_power(x) for n, x in windows.items()}
    # an all-zero window is a receiver that dropped out mid-slot
    names = [n for n in windows if power[n] > 0]
    if not names:
        return Slot({}, [])
    xs = [windows[n] for n in names]
    sig2 = [power[n] for n in names]
    freqs, spec = spectrum(xs[0])
    for x in xs[1:]:
        spec = spec + spectrum(x)[1]
    # each receiver gets its own peak list too, so a strong local signal is never missed
    cands = set(peaks(freqs, spec / len(xs)))
    if len(xs) > 1:
        for x in xs:
            cands.update(peaks(*spectrum(x)))

    each = alone or len(xs) == 1
    found: dict[str, dict[str, Spot]] = {n: {} for n in names} if each else {}
    combined: dict[str, Spot] = {}
    for f0 in sorted(cands):
        fs = f0 + COARSE_DF
        grid = _scores(xs, sig2, fs, COARSE_LAGS)
        own = []
        for r in range(len(xs)):
            i, j = np.unravel_index(int(np.argmax(grid[r])), grid[r].shape)
            own.append(_refine([xs[r]], [sig2[r]], float(fs[i]), int(COARSE_LAGS[j])))
        for r, n in enumerate(names if each else []):
            _keep(found[n], _decode([xs[r]], [n], *own[r], min_metric, max_cycles))
        if len(xs) > 1:
            total = grid.sum(axis=0)
            i, j = np.unravel_index(int(np.argmax(total)), total.shape)
            joint = _refine(xs, sig2, float(fs[i]), int(COARSE_LAGS[j]))
            # two stations a hertz apart can each dominate a different receiver, so the
            # joint peak may belong to the other one; try every receiver's peak as well
            for f, lag in _distinct([joint, *own]):
                _keep(combined, _decode(xs, names, f, lag, min_metric, max_cycles))

    def ordered(d: dict[str, Spot]) -> list[Spot]:
        return sorted(d.values(), key=lambda s: s.freq)

    alone_out = {n: ordered(d) for n, d in found.items()}
    return Slot(alone_out, ordered(combined) if len(xs) > 1 else alone_out[names[0]])
