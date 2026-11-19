import numpy as np

from wsprpool import Message, pack
from wsprpool._fano import fano
from wsprpool._wspr import bits_of, convolve


def _llrs(msg, ebn0_db, rng):
    coded = convolve(bits_of(pack(msg)))
    # rate 1/2, so each coded bit carries half the energy of a data bit
    sigma = np.sqrt(1 / (2 * 0.5 * 10 ** (ebn0_db / 10)))
    r = (1 - 2 * coded) + rng.normal(0, sigma, coded.size)
    return 2 * r / sigma**2


def test_decodes_noisy_bpsk():
    rng = np.random.default_rng(3)
    msg = Message("K1ABC", "FN42", 37)
    hits = 0
    for _ in range(10):
        out = fano(_llrs(msg, 5.0, rng))
        hits += out is not None and out[0] == pack(msg)
    assert hits >= 9


def test_gives_up_on_noise():
    rng = np.random.default_rng(4)
    assert all(fano(rng.normal(0, 1, 162), max_cycles=200) is None for _ in range(5))
