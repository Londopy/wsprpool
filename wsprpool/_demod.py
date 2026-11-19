"""Baseband conversion, signal search and soft symbols for one receiver's stream."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly
from scipy.special import i0e

from ._wspr import NSYM, SYNC, TONE_SPACING

FS = 375.0
SPS = 256
WINDOW = int(120 * FS)
TONE_OFFSETS = (np.arange(4) - 1.5) * TONE_SPACING
SIGN = 2.0 * SYNC - 1.0


def baseband(iq: np.ndarray, fs: float, shift_hz: float = 0.0) -> tuple[np.ndarray, float]:
    """Shift iq down by shift_hz and resample it to about 375 Hz.

    Returns the samples and their exact rate. A Kiwi runs some 100 ppm off 12 kHz, which
    is harmless within one slot but adds up over a long recording, so callers index time
    with the returned rate rather than FS.
    """
    n = np.arange(len(iq))
    x = iq * np.exp(-2j * np.pi * shift_hz * n / fs) if shift_hz else iq
    ratio = Fraction(FS / fs).limit_denominator(4000)
    out = resample_poly(x, ratio.numerator, ratio.denominator).astype(np.complex128)
    return out, fs * ratio.numerator / ratio.denominator


def spectrum(x: np.ndarray, nfft: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """Time-averaged power spectrum, normalised so the noise floor sits at 1."""
    hop = nfft // 2
    frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop]
    p = np.abs(np.fft.fftshift(np.fft.fft(frames * np.hanning(nfft), axis=1), axes=1)) ** 2
    avg = p.mean(axis=0)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS))
    inner = np.abs(freqs) < 130
    return freqs, avg / np.median(avg[inner])


def peaks(freqs: np.ndarray, spec: np.ndarray, limit: float = 150.0,
          sigmas: float = 4.0, most: int = 40) -> list[float]:
    """Candidate centre frequencies: local maxima of the 4-tone-wide smoothed spectrum."""
    width = int(round(4 * TONE_SPACING / (freqs[1] - freqs[0])))
    sm = np.convolve(spec, np.ones(width) / width, mode="same")
    inner = np.abs(freqs) < limit
    base = np.median(sm[inner])
    spread = 1.4826 * np.median(np.abs(sm[inner] - base))
    cand = [
        i for i in range(1, len(sm) - 1)
        if inner[i] and sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and sm[i] > base + sigmas * spread
    ]
    cand.sort(key=lambda i: -sm[i])
    return [float(freqs[i]) for i in cand[:most]]


def tone_sums(x: np.ndarray, f: float, lags: np.ndarray) -> np.ndarray:
    """Complex symbol integrals, shape (4 tones, len(lags), 162), for a signal centred on f."""
    n = np.arange(len(x))
    tones = f + TONE_OFFSETS
    y = x[None, :] * np.exp(-2j * np.pi * np.outer(tones, n) / FS)
    cs = np.concatenate([np.zeros((4, 1), complex), np.cumsum(y, axis=1)], axis=1)
    starts = lags[:, None] + SPS * np.arange(NSYM)[None, :]
    return cs[:, starts + SPS] - cs[:, starts]


def noise_power(x: np.ndarray) -> float:
    """Expected |symbol integral|^2 of the noise alone, from the robust spectral floor."""
    nfft = 512
    frames = np.lib.stride_tricks.sliding_window_view(x, nfft)[:: nfft // 2]
    p = np.abs(np.fft.fft(frames, axis=1)) ** 2 / nfft
    freqs = np.fft.fftfreq(nfft, 1 / FS)
    per_sample = float(np.median(p.mean(axis=0)[np.abs(freqs) < 130]))
    return per_sample * SPS


def sync_score(c: np.ndarray, sigma2: float) -> np.ndarray:
    """Sync statistic for each lag, scaled so pure noise is roughly N(0, 1)."""
    p = np.abs(c) ** 2 / sigma2
    diff = (p[1] + p[3]) - (p[0] + p[2])
    return (diff * SIGN).sum(axis=-1) / np.sqrt(4 * NSYM)


def _pairs(c: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per symbol: integral for data bit 0, for data bit 1, and the two noise-only tones."""
    i = np.arange(NSYM)
    s = SYNC.astype(int)
    return c[s, i], c[s + 2, i], np.stack([c[1 - s, i], c[3 - s, i]])


def soft_bits(c: np.ndarray, smooth: int = 15) -> tuple[np.ndarray, float]:
    """LLRs for the 162 data bits (channel order) and the estimated SNR in 2500 Hz.

    c is the (4, 162) tone matrix at the chosen frequency and lag. Noise comes from the two
    tones the sync vector rules out in each symbol; signal power is tracked over a short
    window so a fade lowers this receiver's say in the combination.
    """
    r0, r1, quiet = _pairs(c)
    sigma2 = float(np.mean(np.abs(quiet) ** 2))
    pw = np.abs(r0) ** 2 + np.abs(r1) ** 2 - 2 * sigma2
    kernel = np.ones(smooth) / smooth
    local = np.convolve(np.pad(pw, smooth // 2, mode="edge"), kernel, mode="valid")
    a2 = np.clip(local, 0.02 * sigma2, None)
    a = np.sqrt(a2)
    x0 = 2 * a * np.abs(r0) / sigma2
    x1 = 2 * a * np.abs(r1) / sigma2
    llr = (np.log(i0e(x0)) + x0) - (np.log(i0e(x1)) + x1)
    es = max(float(np.mean(pw)), 1e-9 * sigma2)
    snr = 10 * np.log10(es / sigma2 * TONE_SPACING / 2500)
    return llr, snr


def fit(x: np.ndarray, tones: np.ndarray, f: float, lag: int) -> tuple[float, int, float]:
    """Best frequency and start for a known transmission near (f, lag), and how sure it's there.

    The last value is a z-score: how often the transmitted tone beats the tone the other
    data bit would have used. An unrelated neighbouring station lands on either equally
    often, so it doesn't fake a transmission the way raw tone energy would. A neighbour
    sending a nearly identical message (same call and grid, say) still can.
    """
    i = np.arange(NSYM)
    lags = np.arange(lag - 16, lag + 17)
    lags = lags[(lags >= 0) & (lags + NSYM * SPS <= len(x))]
    if not len(lags):
        return f, lag, 0.0
    best = (f, int(lags[0]), -np.inf)
    for step in (np.arange(-0.25, 0.251, 0.05), np.arange(-0.04, 0.041, 0.01)):
        for ff in best[0] + step:
            known = (np.abs(tone_sums(x, ff, lags)[tones, :, i]) ** 2).sum(axis=0)
            j = int(np.argmax(known))
            if known[j] > best[2]:
                best = (float(ff), int(lags[j]), float(known[j]))
    f, lag = best[0], best[1]
    p = np.abs(tone_sums(x, f, np.array([lag]))[:, 0, :]) ** 2
    agree = int(np.sum(p[tones, i] > p[tones ^ 2, i]))
    return f, lag, (agree - NSYM / 2) / np.sqrt(NSYM / 4)


def _smooth(env: np.ndarray, width: int) -> np.ndarray:
    """Centred moving average that shrinks at the ends instead of padding with zeros."""
    cs = np.concatenate([[0], np.cumsum(env)])
    n = len(env)
    lo = np.clip(np.arange(n) - width // 2, 0, n)
    hi = np.clip(np.arange(n) + width - width // 2, 0, n)
    return (cs[hi] - cs[lo]) / (hi - lo)


def _removed(x: np.ndarray, tones: np.ndarray, f: float, lag: int) -> np.ndarray:
    """The part of x[lag:] that follows the transmission's tones, as a slowly varying envelope."""
    n = min(NSYM * SPS, len(x) - lag)
    ref = np.exp(2j * np.pi * np.cumsum(np.repeat(f + TONE_OFFSETS[tones], SPS)[:n]) / FS)
    return _smooth(x[lag: lag + n] * np.conj(ref), int(FS)) * ref


def subtract(x: np.ndarray, tones: np.ndarray, f: float, lag: int,
             min_z: float = 3.0) -> np.ndarray:
    """x with a known transmission removed, or x unchanged if it isn't clearly there.

    The frequency and start are fitted on x first, so a spot found at another receiver
    can be removed here. The complex envelope is tracked with a one-second moving
    average, which follows slow fading and any leftover frequency error. The start is
    then trimmed to the sample by whichever leaves the least energy behind, since a few
    samples off at every tone change limits the removal to about 20 dB.
    """
    f, lag, z = fit(x, tones, f, lag)
    if z < min_z:
        return x
    def gain(m: int) -> tuple[float, int, np.ndarray]:
        r = _removed(x, tones, f, m)
        seg = x[m: m + len(r)]
        return float(np.sum(np.abs(seg) ** 2) - np.sum(np.abs(seg - r) ** 2)), m, r

    _, m, r = max((gain(m) for m in range(max(0, lag - 8), lag + 9)),
                  key=lambda g: g[0])
    out = x.copy()
    out[m: m + len(r)] -= r
    return out
