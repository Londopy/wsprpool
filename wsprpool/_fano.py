"""Fano sequential decoder for the WSPR K=32, r=1/2 convolutional code, driven by soft LLRs."""

from __future__ import annotations

import numpy as np

from ._wspr import NBITS, NDATA, POLY1, POLY2


def branch_metrics(llr: np.ndarray, bias: float = 0.5) -> np.ndarray:
    """Fano metric, in bits, for each node and each pair of coded bits (index 2*c0 + c1).

    llr is in deinterleaved order, positive meaning the coded bit is more likely 0.
    """
    lp = llr.reshape(NBITS, 2)
    # log2(2 * P(c | r)) - bias, with P(c=0 | r) = 1 / (1 + exp(-L))
    m0 = 1.0 - np.logaddexp(0.0, -lp) / np.log(2) - bias / 2
    m1 = 1.0 - np.logaddexp(0.0, lp) / np.log(2) - bias / 2
    out = np.empty((NBITS, 4))
    out[:, 0] = m0[:, 0] + m0[:, 1]
    out[:, 1] = m0[:, 0] + m1[:, 1]
    out[:, 2] = m1[:, 0] + m0[:, 1]
    out[:, 3] = m1[:, 0] + m1[:, 1]
    return out


def _pair(reg: int) -> int:
    return (((reg & POLY1).bit_count() & 1) << 1) | ((reg & POLY2).bit_count() & 1)


def fano(llr: np.ndarray, delta: float = 1.5, max_cycles: int = 10000,
         bias: float = 0.5) -> tuple[int, float, int] | None:
    """Decode 162 deinterleaved LLRs. Returns (50-bit value, path metric, cycles) or None."""
    met = branch_metrics(np.asarray(llr, dtype=float), bias).tolist()
    limit = max_cycles * NBITS

    gamma = [0.0] * (NBITS + 1)
    state = [0] * (NBITS + 1)
    first = [0] * NBITS
    second = [0.0] * NBITS
    best = [0.0] * NBITS
    choice = [0] * NBITS
    path = [0] * NBITS

    def expand(k: int) -> None:
        reg = (state[k] << 1) & 0xFFFFFFFF
        m0 = met[k][_pair(reg)]
        if k >= NDATA:
            first[k], best[k], second[k] = 0, m0, float("-inf")
            return
        m1 = met[k][_pair(reg | 1)]
        if m0 >= m1:
            first[k], best[k], second[k] = 0, m0, m1
        else:
            first[k], best[k], second[k] = 1, m1, m0

    k = 0
    t = 0.0
    choice[0] = 0
    expand(0)
    for cycle in range(1, limit + 1):
        bm = best[k] if choice[k] == 0 else second[k]
        ng = gamma[k] + bm
        if ng >= t:
            if gamma[k] < t + delta:
                while ng >= t + delta:
                    t += delta
            bit = first[k] ^ choice[k]
            path[k] = bit
            gamma[k + 1] = ng
            state[k + 1] = ((state[k] << 1) | bit) & 0xFFFFFFFF
            k += 1
            if k == NBITS:
                value = int("".join(map(str, path[:NDATA])), 2)
                return value, ng, cycle
            choice[k] = 0
            expand(k)
            continue
        while True:
            if k == 0 or gamma[k - 1] < t:
                t -= delta
                choice[k] = 0
                break
            k -= 1
            if k < NDATA and choice[k] == 0:
                choice[k] = 1
                break
    return None
