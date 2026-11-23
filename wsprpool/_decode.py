"""Joint search and soft-combined decoding of one 2-minute WSPR slot across receivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from ._demod import (
    FS,
    SIGN,
    noise_power,
    peaks,
    soft_bits,
    spectrum,
    subtract,
    sync_score,
    tone_sums,
)
from ._fano import fano
from ._wspr import NSYM, SYNC, Message, deinterleave, symbols, unpack

# windows start this long before the even minute, so early transmitters are still caught
LEAD = 1.0
COARSE_DF = np.arange(-1.0, 1.01, 0.1)
COARSE_LAGS = np.arange(0, int(5 * FS), 16)
FINE_DF = np.arange(-0.1, 0.101, 0.025)
# total frequency change over the transmission
DRIFTS = np.arange(-4.0, 4.01, 1.0)
WEIGHTINGS = ("llr", "snr", "equal")
# a receiver whose own sync score reaches this sees the signal well enough to trust its own
# frequency in the combination; below it the joint frequency is the better guess
OWN_SYNC = 6.0
OWN_RANGE = 1.0
OWN_LAG = 64
# receivers below this at a spot don't hear it, so their LLRs say nothing about shared noise
HEARS_DB = -40.0


@dataclass
class Spot:
    message: Message
    freq: float
    dt: float
    metric: float
    snr: dict[str, float] = field(default_factory=dict)
    drift: float = 0.0
    # each receiver's LLRs for the 162 data bits, in channel order
    llr: dict[str, np.ndarray] = field(default_factory=dict, repr=False, compare=False)

    @property
    def combined_snr(self) -> float:
        return float(10 * np.log10(sum(10 ** (s / 10) for s in self.snr.values())))


@dataclass
class Slot:
    alone: dict[str, list[Spot]]
    combined: list[Spot]


def _scores(
    xs: list[np.ndarray], sig2: list[float], fs: np.ndarray, lags: np.ndarray, drift: float = 0.0
) -> np.ndarray:
    """Sync scores with shape (receiver, frequency, lag)."""
    return np.array([[sync_score(tone_sums(x, f, lags, drift), s) for f in fs]
                     for x, s in zip(xs, sig2)])


def _coarse(x: np.ndarray, sigma2: float, fs: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """Sync scores with shape (drift, frequency, lag).

    Symbol powers are worked out once on the frequency grid. A drifting signal is a path
    that moves across that grid symbol by symbol, so each drift is scored by reading its
    path off the grid instead of mixing the samples down again.
    """
    p = np.array([np.abs(tone_sums(x, f, lags)) ** 2 for f in fs]) / sigma2
    diff = ((p[:, 1] + p[:, 3]) - (p[:, 0] + p[:, 2])) * SIGN
    k = np.arange(NSYM)
    rows = np.arange(len(fs))[:, None]
    out = np.empty((len(DRIFTS), len(fs), len(lags)))
    for t, drift in enumerate(DRIFTS):
        shift = np.round(drift * (k - (NSYM - 1) / 2) / NSYM / (fs[1] - fs[0])).astype(int)
        out[t] = diff[np.clip(rows + shift, 0, len(fs) - 1), :, k].sum(axis=1)
    return out / np.sqrt(4 * NSYM)


def _refine(
    xs: list[np.ndarray], sig2: list[float], f: float, lag: int, drift: float
) -> tuple[float, int, float, float]:
    """Fine frequency, start and drift near a coarse peak, and the summed sync score there."""
    fs = f + FINE_DF
    lags = np.clip(np.arange(lag - 16, lag + 17, 2), 0, COARSE_LAGS[-1])
    score = _scores(xs, sig2, fs, lags, drift).sum(axis=0)
    i, j = np.unravel_index(int(np.argmax(score)), score.shape)
    f, lag = float(fs[i]), int(lags[j])
    # the coarse grid only knows drift to the nearest hertz
    best, drift = max((float(_scores(xs, sig2, np.array([f]), np.array([lag]), d).sum()), d)
                      for d in (drift - 0.5, drift, drift + 0.5))
    return f, lag, drift, best


def combine(each: dict[str, np.ndarray], snr: dict[str, float], weighting: str) -> np.ndarray:
    """Sum the receivers' LLRs.

    "llr" adds them as they are: an exact LLR already weights a receiver by what it heard.
    "snr" also multiplies each by its measured linear SNR, and "equal" scales each to the
    same mean size, so the plain sum can be measured against both. Both keep the overall
    scale, since the Fano decoder reads LLRs as probabilities.
    """
    if weighting == "llr":
        return np.sum(list(each.values()), axis=0)
    if weighting == "snr":
        lin = {n: 10 ** (snr[n] / 10) for n in each}
        mean = np.mean(list(lin.values()))
        return np.sum([v * lin[n] / mean for n, v in each.items()], axis=0)
    if weighting == "equal":
        size = {n: max(float(np.mean(np.abs(v))), 1e-12) for n, v in each.items()}
        mean = np.mean(list(size.values()))
        return np.sum([v * mean / size[n] for n, v in each.items()], axis=0)
    raise ValueError(f"unknown weighting {weighting!r}; use one of {', '.join(WEIGHTINGS)}")


def _decode(
    xs: list[np.ndarray], names: list[str], f: float, lag: int, drift: float,
    min_metric: float, max_cycles: int, offsets: list[float] | None = None,
    weighting: str = "llr",
) -> Spot | None:
    """Add every receiver's LLRs at (f, lag, drift) and run the Fano decoder on the sum.

    offsets moves each receiver's frequency away from f, for receivers whose own oscillator
    puts the signal somewhere slightly different.
    """
    snr, each = {}, {}
    for n, x, off in zip(names, xs, offsets or [0.0] * len(xs)):
        each[n], snr[n] = soft_bits(tone_sums(x, f + off, np.array([lag]), drift)[:, 0, :])
    out = fano(deinterleave(combine(each, snr, weighting)), max_cycles=max_cycles)
    if out is None or out[1] < min_metric:
        return None
    msg = unpack(out[0])
    if msg is None:
        return None
    return Spot(msg, f, lag / FS - LEAD - 1.0, out[1], snr, drift, each)


def _offsets(own: list[tuple[float, int, float, float]], f: float, lag: int) -> list[float]:
    """Each receiver's own frequency relative to f, where it clearly sees the same signal."""
    return [g - f if score >= OWN_SYNC and abs(g - f) <= OWN_RANGE and abs(m - lag) <= OWN_LAG
            else 0.0 for g, m, _, score in own]


def _distinct(points: list[tuple[float, int, float]]) -> list[tuple[float, int, float]]:
    out: list[tuple[float, int, float]] = []
    for f, lag, drift in points:
        if all(abs(f - g) > 0.05 or abs(lag - m) > 8 or d != drift for g, m, d in out):
            out.append((f, lag, drift))
    return out


def _keep(spots: dict[str, Spot], spot: Spot | None) -> bool:
    """Add or improve a spot; True when its message is new."""
    if spot is None:
        return False
    key = str(spot.message)
    new = key not in spots
    if new or spots[key].metric < spot.metric:
        spots[key] = spot
    return new


def _residual(x: np.ndarray, spots: list[Spot]) -> np.ndarray:
    # strongest first, so each fit sees as little of the others as possible
    for s in sorted(spots, key=lambda s: -max(s.snr.values())):
        x = subtract(x, symbols(s.message), s.freq, round((s.dt + LEAD + 1.0) * FS), s.drift)
    return x


def decode_slot(
    windows: dict[str, np.ndarray],
    min_metric: float = 10.0,
    max_cycles: int = 10000,
    alone: bool = True,
    passes: int = 2,
    weighting: str = "llr",
) -> Slot:
    """Decode one slot from each receiver alone and from all of them combined.

    Each window is 375 Hz baseband starting LEAD seconds before the even minute. With
    alone=False and several receivers, only the combination is decoded. After each pass,
    every decoded signal is subtracted and the rest decoded again, so a weak station
    beside a strong one gets its turn. Each receiver alone, and the combination, only
    subtract what they decoded themselves, so neither borrows the other's results.
    weighting picks how receivers' LLRs are summed; see combine().
    """
    # an all-zero window is a receiver that dropped out mid-slot
    live = {n: x for n, x in windows.items() if noise_power(x) > 0}
    if not live:
        return Slot({}, [])
    first = _decode_pass(live, min_metric, max_cycles, alone, weighting)
    by_rx = {n: {str(s.message): s for s in v} for n, v in first.alone.items()}
    comb = {str(s.message): s for s in first.combined} if len(live) > 1 else {}

    for _ in range(passes - 1):
        grew = False
        for n, found in by_rx.items():
            if found:
                res = _decode_pass(
                    {n: _residual(live[n], list(found.values()))}, min_metric, max_cycles, True
                )
                for s in res.combined:
                    grew |= _keep(found, s)
        if comb:
            rest = {n: _residual(x, list(comb.values())) for n, x in live.items()}
            for s in _decode_pass(rest, min_metric, max_cycles, False, weighting).combined:
                grew |= _keep(comb, s)
        if not grew:
            break

    _drop_shadows([*by_rx.values(), comb])
    alone_out = {n: _ordered(d) for n, d in by_rx.items()}
    return Slot(alone_out, _ordered(comb) if len(live) > 1 else alone_out[next(iter(live))])


def _drop_shadows(tracks: list[dict[str, Spot]]) -> None:
    """Remove spots sitting on a better decode of a different message, in any track.

    What a subtraction leaves of a transmission can decode as a second, wrong message at
    the same frequency and time. Two real stations can't share both that closely.
    """
    every = [s for t in tracks for s in t.values()]
    for t in tracks:
        for key, s in list(t.items()):
            if any(o.message != s.message and o.metric > s.metric
                   and abs(o.freq - s.freq) <= 0.05 and abs(o.dt - s.dt) <= 8 / FS
                   for o in every):
                del t[key]


def llr_correlation(spots: list[Spot]) -> tuple[float, int] | None:
    """Mean correlation between receivers' LLR errors at decoded spots, and how many pairs.

    Signing each receiver's LLRs by the decoded bits, then taking out their 15-symbol
    moving average, leaves that receiver's noise. Only pairs of receivers that both hear
    the spot count. Independent receivers give about 0. Interference reaching several
    sites at once pushes it up, and with correlation c, N receivers are worth only
    N / (1 + (N - 1) c) independent ones.
    """
    cors = []
    for s in spots:
        sign = 1 - (symbols(s.message) - SYNC)
        errs = [_detrend(v * sign) for n, v in s.llr.items() if s.snr[n] >= HEARS_DB]
        cors += [float(np.corrcoef(a, b)[0, 1]) for a, b in combinations(errs, 2)]
    return (float(np.mean(cors)), len(cors)) if cors else None


def _detrend(v: np.ndarray, width: int = 15) -> np.ndarray:
    """v less its moving average, so slow fading common to all sites doesn't count."""
    trend = np.convolve(np.pad(v, width // 2, mode="edge"), np.ones(width) / width, "valid")
    return v - trend


def _ordered(d: dict[str, Spot]) -> list[Spot]:
    return sorted(d.values(), key=lambda s: s.freq)


def _decode_pass(
    windows: dict[str, np.ndarray], min_metric: float, max_cycles: int, alone: bool,
    weighting: str = "llr",
) -> Slot:
    names = list(windows)
    xs = [windows[n] for n in names]
    sig2 = [noise_power(x) for x in xs]
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
        grid = np.array([_coarse(x, s, fs, COARSE_LAGS) for x, s in zip(xs, sig2)])
        own = []
        for r in range(len(xs)):
            t, i, j = np.unravel_index(int(np.argmax(grid[r])), grid[r].shape)
            own.append(_refine([xs[r]], [sig2[r]], float(fs[i]), int(COARSE_LAGS[j]),
                               float(DRIFTS[t])))
        for r, n in enumerate(names if each else []):
            _keep(found[n], _decode([xs[r]], [n], *own[r][:3], min_metric, max_cycles))
        if len(xs) > 1:
            total = grid.sum(axis=0)
            t, i, j = np.unravel_index(int(np.argmax(total)), total.shape)
            joint = _refine(xs, sig2, float(fs[i]), int(COARSE_LAGS[j]), float(DRIFTS[t]))
            # two stations a hertz apart can each dominate a different receiver, so the
            # joint peak may belong to the other one; try every receiver's peak as well
            for f, lag, drift in _distinct([joint[:3], *(o[:3] for o in own)]):
                off = _offsets(own, f, lag)
                # an own peak can belong to a neighbour, so also try everyone at one frequency
                for o in [off, None] if any(off) else [None]:
                    _keep(combined, _decode(xs, names, f, lag, drift, min_metric, max_cycles,
                                            o, weighting))

    alone_out = {n: _ordered(d) for n, d in found.items()}
    return Slot(alone_out, _ordered(combined) if len(xs) > 1 else alone_out[names[0]])
