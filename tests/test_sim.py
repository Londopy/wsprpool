import numpy as np
import pytest

from wsprpool._demod import FS
from wsprpool._sim import fading


def test_fading_has_unit_mean_power_and_the_asked_for_spread():
    rng = np.random.default_rng(4)
    n = 162 * 256
    g = [fading(n, 0.2, rng) for _ in range(100)]
    assert np.mean([np.mean(np.abs(x) ** 2) for x in g]) == pytest.approx(1.0, abs=0.05)
    psd = np.mean([np.abs(np.fft.fft(x)) ** 2 for x in g], axis=0)
    f = np.fft.fftfreq(n, 1 / FS)
    # Watterson's spread is two standard deviations of the Doppler spectrum
    assert 2 * np.sqrt(np.sum(psd * f**2) / np.sum(psd)) == pytest.approx(0.2, rel=0.05)
