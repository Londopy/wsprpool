import numpy as np
import pytest

from wsprpool import Message, pack, symbols, unpack

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


@pytest.mark.parametrize("call,grid,power", [
    ("K1ABC", "FN42", 36), ("K1ABC", "SS00", 37), ("KABC", "FN42", 37), ("K1ABCDE", "FN42", 37),
    ("K1ABCD", "FN42", 37),
])
def test_pack_rejects_what_type1_cannot_carry(call, grid, power):
    with pytest.raises(ValueError):
        pack(Message(call, grid, power))


def test_unpack_rejects_bad_power():
    value = pack(Message("K1ABC", "FN42", 37))
    assert unpack((value & ~0x7F) | (64 + 5)) is None
