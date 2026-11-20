import numpy as np
import pytest

from wsprpool import Message, decode_slot, pack, symbols, unpack
from wsprpool._sim import Channel, slot
from wsprpool._spots import confirm
from wsprpool._wspr import callhash, lookup3, resolve

# channel symbols for "K1ABC FN42 37" from the WSJT-X reference encoder
K1ABC = (
    "3 3 0 0 2 0 0 0 1 0 2 0 1 3 1 2 2 2 1 0 0 3 2 3 1 3 3 2 2 0 2 0 0 0 3 2 0 1 2 3 2 2 0 0 2 2 "
    "3 2 1 1 0 2 3 3 2 1 0 2 2 1 3 2 1 2 2 2 0 3 3 0 3 0 3 0 1 2 1 0 2 1 2 0 3 2 1 3 2 0 0 3 3 2 "
    "3 0 3 2 2 0 3 0 2 0 2 0 1 0 2 3 0 2 1 1 1 2 3 3 0 2 3 1 2 1 2 2 2 1 3 3 2 0 0 0 0 1 0 3 2 0 "
    "1 3 2 2 2 2 2 0 2 3 3 2 3 2 3 3 2 0 0 3 1 2 2 2"
)


def test_symbols_match_reference_encoder():
    expected = np.array(K1ABC.split(), dtype=int)
    assert symbols(Message("K1ABC", "FN42", 37)).tolist() == expected.tolist()


@pytest.mark.parametrize("call,grid,power", [
    ("K1ABC", "FN42", 37), ("G4ABC", "IO91", 23), ("VK2XYZ", "QF56", 0),
    ("W1A", "AA00", 60), ("2E0ABC", "RR99", 10),
])
def test_pack_round_trip(call, grid, power):
    msg = Message(call, grid, power)
    assert unpack(pack(msg)) == msg


@pytest.mark.parametrize("call,power", [
    ("PJ4/K1ABC", 37), ("G/K1ABC", 60), ("K1ABC/P", 23), ("K1ABC/7", 0), ("K1ABC/12", 10),
    ("VE3/W1A", 30), ("K1ABC/99", 33),
])
def test_compound_call_round_trip(call, power):
    msg = Message(call, "", power)
    assert unpack(pack(msg)) == msg


@pytest.mark.parametrize("call,grid,power", [
    ("K1ABC", "FN42AB", 37), ("PJ4/K1ABC", "FK52UD", 0), ("W1A", "RR99XX", 60),
])
def test_hashed_message_round_trip(call, grid, power):
    out = unpack(pack(Message(call, grid, power)))
    assert out == Message("", grid, power, callhash(call))
    assert resolve(out, {callhash(call): call}) == Message(call, grid, power, callhash(call))


def test_lookup3_matches_its_published_test_vectors():
    assert lookup3(b"", 0) == 0xDEADBEEF
    assert lookup3(b"Four score and seven years ago", 0) == 0x17770551
    assert lookup3(b"Four score and seven years ago", 1) == 0xCD628161


def test_callhash_matches_real_type3_transmissions():
    # hashes carried by type 3 messages decoded from KiwiSDR recordings on 2026-11-19,
    # whose calls wspr.live reported from the same 6-character locator
    assert callhash("W6LPM") == 26993
    assert callhash("K0CFW") == 23937
    assert callhash("KC1ATT") == 6926


@pytest.mark.parametrize("call,grid,power", [
    ("K1ABC", "FN42", 36), ("K1ABC", "SS00", 37), ("KABC", "FN42", 37), ("K1ABCDE", "FN42", 37),
    ("K1ABCD", "FN42", 37), ("K1ABC/05", "", 37), ("ABCD/K1ABC", "", 37), ("K1ABC/P", "FN42", 37),
    ("K1ABC", "FN42YA", 37),
])
def test_pack_rejects_what_wspr_cannot_carry(call, grid, power):
    with pytest.raises(ValueError):
        pack(Message(call, grid, power))


def test_unpack_rejects_bad_power():
    value = pack(Message("K1ABC", "FN42", 37))
    assert unpack((value & ~0x7F) | (64 + 63)) is None


def test_hashed_message_decodes_from_a_slot():
    msg = Message("K1ABC", "FN42AB", 37)
    xs = slot(msg, 30.0, [Channel(-24.0)], rng=np.random.default_rng(5))
    assert [s.message for s in decode_slot({"a": xs[0]}).combined] == [
        Message("", "FN42AB", 37, callhash("K1ABC"))]


def test_wspr_live_names_a_hashed_call_only_when_the_hash_matches():
    msg = Message("", "EM10CR", 37, callhash("KC1ATT"))
    rows = [("W1XYZ", "EM10CR", 37, "N1A"), ("KC1ATT", "EM10CR", 37, "N2B"),
            ("KC1ATT", "EM10CR", 37, "N3C")]
    named, heard = confirm(msg, rows)
    assert named.call == "KC1ATT"
    assert heard == {"N2B", "N3C"}
    assert confirm(msg, rows[:1]) == (msg, set())


def test_wspr_live_match_needs_the_same_power_and_square():
    msg = Message("K1ABC", "FN42", 37)
    rows = [("K1ABC", "FN42AB", 37, "N1A"), ("K1ABC", "FN43", 37, "N2B"),
            ("K1ABC", "FN42", 23, "N3C")]
    assert confirm(msg, rows)[1] == {"N1A"}
    compound = Message("PJ4/K1ABC", "", 37)
    assert confirm(compound, [("PJ4/K1ABC", "", 37, "N4D")])[1] == {"N4D"}


# 50-bit values worked out by hand from WSJT-X's wqencode.f90 and packjt.f90, not from this
# package, so a mistake made the same way in pack and unpack still fails. EM10CR is the value
# KC1ATT actually sent, decoded from a KiwiSDR recording on 2026-11-19.
@pytest.mark.parametrize("msg,value", [
    (Message("PJ4/K1ABC", "", 37), 1086526029314663),
    (Message("G/K1ABC", "", 60), 1086526031324286),
    (Message("K1ABC/P", "", 23), 1086526032526553),
    (Message("K1ABC/12", "", 10), 1086526032528204),
    (Message("", "FN42AB", 37, 26993), 687035211102362),
    (Message("", "EM10CR", 37, 6926), 654680989206298),
])
def test_packed_value_matches_the_wsjtx_encoder(msg, value):
    assert pack(msg) == value
    assert unpack(value) == msg


def test_unpack_rejects_prefix_codes_no_encoder_makes():
    base = pack(Message("PJ4/K1ABC", "", 37))
    # prefix code 50653 (one past "ZZZ"), with the power digits flagging a prefix
    assert unpack((base & ~0x3FFFFF) | ((50653 - 32768) * 128 + 37 + 2 + 64)) is None


def test_wspr_live_names_a_hash_only_from_the_same_locator_and_power():
    msg = Message("", "EM10CR", 37, callhash("KC1ATT"))
    assert confirm(msg, [("KC1ATT", "EM10CQ", 37, "N1A")])[0].call == ""
    assert confirm(msg, [("KC1ATT", "EM10CR", 33, "N1A")])[0].call == ""


def test_wspr_live_overrides_a_hash_collision_named_in_session():
    wrong = Message("W1XYZ", "EM10CR", 37, callhash("KC1ATT"))
    named, heard = confirm(wrong, [("KC1ATT", "EM10CR", 37, "N2B")])
    assert named.call == "KC1ATT" and heard == {"N2B"}
