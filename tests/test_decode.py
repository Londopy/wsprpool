import numpy as np
import pytest

from wsprpool import Message, decode_slot, symbols
from wsprpool._decode import Spot, _drop_shadows, _offsets, llr_correlation
from wsprpool._sim import Channel, slot
from wsprpool._wspr import SYNC

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


def test_second_pass_finds_weak_station_beside_strong_one():
    rng = np.random.default_rng(13)
    weak = Message("W5KV", "DM79", 23)
    x = (slot(MSG, 30.0, [Channel(-8.0)], rng=rng, noise=False)[0]
         + slot(weak, 31.5, [Channel(-26.0)], rng=rng)[0])
    assert {s.message for s in decode_slot({"r": x}).combined} == {MSG, weak}


@pytest.mark.slow
def test_combination_recovers_station_masked_by_neighbour_at_other_sites():
    # receiver a hears MSG well; b and c hear MSG weakly under a loud station 0.2 Hz away
    rng = np.random.default_rng(14)
    loud = Message("W5KV", "DM79", 23)
    xs = slot(MSG, 30.0, [Channel(-18.0), Channel(-28.0), Channel(-28.0)], rng=rng)
    ys = slot(loud, 30.2, [Channel(-40.0), Channel(-12.0), Channel(-12.0)], rng=rng,
              noise=False)
    res = decode_slot({k: a + b for k, a, b in zip("abc", xs, ys)})
    assert {s.message for s in res.combined} == {MSG, loud}


def test_second_pass_lets_combination_past_a_loud_neighbour():
    # a hears MSG well; b hears it weakly beside a loud station whose confident bits outvote a
    rng = np.random.default_rng(21)
    loud = Message("W5KV", "DM79", 23)
    xs = slot(MSG, 30.0, [Channel(-16.0), Channel(-30.0)], rng=rng)
    ys = slot(loud, 30.2, [Channel(-80.0), Channel(-10.0)], rng=rng, noise=False)
    res = decode_slot({"a": xs[0] + ys[0], "b": xs[1] + ys[1]})
    assert {s.message for s in res.combined} == {MSG, loud}


def test_shadow_of_a_decode_is_dropped():
    real = Spot(MSG, 20.0, 0.11, 23.2, {"a": -27.0})
    junk = Spot(Message("W5KV", "DM79", 23), 20.02, 0.11, 10.5, {"b": -27.0})
    apart = Spot(Message("N0STP", "EN34", 30), 20.5, 0.11, 10.5, {"b": -27.0})
    a, b = {"x": real}, {"y": junk, "z": apart}
    _drop_shadows([a, b])
    assert list(a.values()) == [real] and list(b.values()) == [apart]


@pytest.mark.parametrize("drift", [3.0, -3.5])
def test_drifting_transmitter_is_found_with_its_drift(drift):
    xs = slot(MSG, 30.0, [Channel(-25.0)], rng=np.random.default_rng(7), drift=drift)
    spots = decode_slot({"a": xs[0]}).combined
    assert [s.message for s in spots] == [MSG]
    assert spots[0].drift == pytest.approx(drift, abs=0.25)


def test_llrs_carry_each_receivers_snr_as_their_weight():
    xs = slot(MSG, 30.0, [Channel(-24.0), Channel(-30.0)], rng=np.random.default_rng(4))
    spot = decode_slot({"near": xs[0], "far": xs[1]}).combined[0]
    sign = 1 - (symbols(MSG) - SYNC)
    near, far = (float(np.mean(spot.llr[n] * sign)) for n in ("near", "far"))
    # 6 dB is 4 times the SNR. Noncoherent LLRs fall off faster than SNR as it drops, so
    # the weaker receiver's say shrinks by more than a linear SNR weight would shrink it
    assert 5.0 <= near / far < 10.0


def test_receiver_uses_its_own_frequency_only_when_it_clearly_sees_the_signal():
    own = [(30.3, 750, 0.0, 12.0), (30.4, 752, 0.0, 3.0), (31.2, 750, 0.0, 12.0),
           (30.2, 900, 0.0, 12.0), (29.1, 810, 0.0, 6.5)]
    assert _offsets(own, 30.0, 750) == pytest.approx([0.3, 0.0, 0.0, 0.0, -0.9])


@pytest.mark.parametrize("shared,lo,hi", [(0.0, -0.1, 0.1), (0.5, 0.3, 0.6)])
def test_llr_correlation_measures_noise_shared_between_receivers(shared, lo, hi):
    rng = np.random.default_rng(3)
    spots = []
    for _ in range(3):
        xs = slot(MSG, 20.0, [Channel(-26.0)] * 3, rng=rng, shared=shared)
        spots += decode_slot({str(i): x for i, x in enumerate(xs)}, alone=False).combined
    cor, pairs = llr_correlation(spots)
    assert pairs == 9
    assert lo < cor < hi


@pytest.mark.parametrize("seed", [7, 14, 19])
def test_combination_follows_each_receivers_own_frequency_offset(seed):
    # three oscillators that put the same signal 0.8 Hz apart, near the combined threshold;
    # at one shared frequency these seeds fail to decode
    xs = slot(MSG, 30.0, [Channel(-31.5, off) for off in (-0.4, 0.0, 0.4)],
              rng=np.random.default_rng(seed))
    spots = decode_slot({str(i): x for i, x in enumerate(xs)}, alone=False).combined
    assert MSG in [s.message for s in spots]


@pytest.mark.parametrize("seed", [500, 501])
def test_a_neighbour_heard_by_one_receiver_doesnt_keep_the_pair_from_combining(seed):
    # b's own strongest peak is a louder neighbour 0.8 Hz away; the pair only decodes when
    # the combination also tries both receivers on the joint frequency
    other = Message("W5KV", "DM79", 23)
    rng = np.random.default_rng(seed)
    a = slot(MSG, 30.0, [Channel(-26.0)], rng=rng)[0]
    b = slot(MSG, 30.0, [Channel(-26.0)], rng=rng)[0]
    b = b + slot(other, 30.8, [Channel(-18.0)], rng=rng, noise=False)[0]
    res = decode_slot({"a": a, "b": b}, alone=False, passes=1)
    assert MSG in [s.message for s in res.combined]
