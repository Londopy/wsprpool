import numpy as np
import pytest

from wsprpool._demod import FS
from wsprpool._sim import Channel, fading, slot, threshold_error
from wsprpool import Message


def test_fading_has_unit_mean_power_and_the_asked_for_spread():
    rng = np.random.default_rng(4)
    n = 162 * 256
    g = [fading(n, 0.2, rng) for _ in range(100)]
    assert np.mean([np.mean(np.abs(x) ** 2) for x in g]) == pytest.approx(1.0, abs=0.05)
    psd = np.mean([np.abs(np.fft.fft(x)) ** 2 for x in g], axis=0)
    f = np.fft.fftfreq(n, 1 / FS)
    # Watterson's spread is two standard deviations of the Doppler spectrum
    assert 2 * np.sqrt(np.sum(psd * f**2) / np.sum(psd)) == pytest.approx(0.2, rel=0.05)


def test_shared_noise_is_common_to_every_receiver_in_the_asked_for_share():
    msg = Message("K1ABC", "FN42", 37)
    for shared in (0.0, 0.3, 0.8):
        a, b = slot(msg, 0.0, [Channel(-80.0)] * 2, rng=np.random.default_rng(1), shared=shared)
        assert np.mean(np.abs(a) ** 2) == pytest.approx(1.0, abs=0.03)
        assert np.real(np.vdot(a, b)) / len(a) == pytest.approx(shared, abs=0.03)


def test_threshold_error_turns_the_binomial_spread_into_decibels():
    # rate climbs 0.5 per dB through the crossing; sqrt(0.25 / 20) / 0.5 dB
    assert threshold_error([-31.0, -30.0, -29.0], [0.0, 0.25, 0.75], 20) == pytest.approx(
        np.sqrt(0.25 / 20) / 0.5)
    assert threshold_error([-31.0, -30.0], [0.0, 0.25], 20) is None
