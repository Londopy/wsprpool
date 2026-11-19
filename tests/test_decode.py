import numpy as np
import pytest

from wsprpool import Message, decode_slot
from wsprpool._sim import Channel, slot

MSG = Message("K1ABC", "FN42", 37)


def test_one_receiver_finds_frequency_and_timing():
    xs = slot(MSG, 42.0, [Channel(-22.0)], start=1.5, rng=np.random.default_rng(1))
    spots = decode_slot({"a": xs[0]}).combined
    assert [s.message for s in spots] == [MSG]
    assert spots[0].freq == pytest.approx(42.0, abs=0.1)
    assert spots[0].dt == pytest.approx(0.5, abs=0.05)
    assert spots[0].snr["a"] == pytest.approx(-22.0, abs=1.5)


def test_nothing_from_noise():
    xs = slot(MSG, 0.0, [Channel(-80.0), Channel(-80.0)], rng=np.random.default_rng(2))
    res = decode_slot({"a": xs[0], "b": xs[1]})
    assert res.combined == [] and res.alone == {"a": [], "b": []}


def test_dead_receiver_does_not_spoil_combination():
    xs = slot(MSG, -60.0, [Channel(-24.0), Channel(-80.0)], rng=np.random.default_rng(5))
    assert [s.message for s in decode_slot({"a": xs[0], "b": xs[1]}).combined] == [MSG]


@pytest.mark.slow
def test_combination_decodes_what_no_receiver_can():
    rng = np.random.default_rng(6)
    alone = combined = 0
    for _ in range(8):
        chans = [Channel(-32.0, rng.normal(0, 0.03), rng.normal(0, 0.01)) for _ in range(4)]
        xs = slot(MSG, rng.uniform(-80, 80), chans, rng=rng)
        res = decode_slot({str(i): x for i, x in enumerate(xs)})
        alone += any(MSG in [s.message for s in v] for v in res.alone.values())
        combined += MSG in [s.message for s in res.combined]
    assert alone <= 1
    assert combined >= 5


def test_combination_keeps_stations_that_each_dominate_one_receiver():
    rng = np.random.default_rng(11)
    other = Message("W5KV", "DM79", 23)
    a = slot(MSG, 20.0, [Channel(-15.0), Channel(-80.0)], rng=rng)
    b = slot(other, 21.0, [Channel(-80.0), Channel(-15.0)], rng=rng)
    res = decode_slot({"a": a[0] + b[0], "b": a[1] + b[1]})
    assert {s.message for s in res.combined} == {MSG, other}


def test_receiver_that_dropped_out_is_ignored():
    xs = slot(MSG, 10.0, [Channel(-22.0)], rng=np.random.default_rng(12))
    res = decode_slot({"a": xs[0], "dead": np.zeros_like(xs[0])})
    assert [s.message for s in res.combined] == [MSG]
