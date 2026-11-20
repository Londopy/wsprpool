import numpy as np

from wsprpool import Message, symbols
from wsprpool._demod import subtract
from wsprpool._sim import Channel, slot

MSG = Message("K1ABC", "FN42", 37)
OTHER = Message("W5KV", "DM79", 23)
START = 750
SEG = slice(START, START + 162 * 256)


def _left(x, out):
    return np.sum(np.abs(out[SEG]) ** 2) / np.sum(np.abs(x[SEG]) ** 2)


def test_exact_signal_is_removed_to_the_last_sample():
    x = slot(MSG, 30.0, [Channel(0.0)], rng=np.random.default_rng(3), noise=False)[0]
    assert _left(x, subtract(x, symbols(MSG), 30.0, START)) < 1e-6


def test_finds_a_start_and_frequency_it_was_not_given():
    x = slot(MSG, 30.0, [Channel(0.0)], rng=np.random.default_rng(3), noise=False)[0]
    assert _left(x, subtract(x, symbols(MSG), 30.27, START + 20)) < 1e-4


def test_removal_follows_fading():
    rng = np.random.default_rng(3)
    x = slot(MSG, 30.0, [Channel(0.0, doppler=0.2)], rng=rng, noise=False)[0]
    assert _left(x, subtract(x, symbols(MSG), 30.0, START)) < 0.01


def test_leaves_a_neighbouring_station_alone():
    x = slot(OTHER, 30.2, [Channel(-12.0)], rng=np.random.default_rng(2))[0]
    assert np.array_equal(subtract(x, symbols(MSG), 30.0, START), x)


def test_removal_follows_drift():
    x = slot(MSG, 30.0, [Channel(0.0)], rng=np.random.default_rng(3), noise=False, drift=3.0)[0]
    assert _left(x, subtract(x, symbols(MSG), 30.0, START, 3.0)) < 1e-4
    assert _left(x, subtract(x, symbols(MSG), 30.0, START)) > 0.1
