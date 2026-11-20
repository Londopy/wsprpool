import csv

from wsprpool import Message, Slot, Spot
from wsprpool._cli import _resolve_hashes, _write_csv, main
from wsprpool._wspr import callhash


def test_hashed_calls_are_named_from_other_slots_but_not_from_each_other():
    plain = Spot(Message("K1ABC", "FN42", 37), 10.0, 0.1, 50.0, {"a": -20.0})
    hashed = Spot(Message("", "FN42AB", 37, callhash("K1ABC")), 10.0, 0.1, 50.0, {"a": -20.0})
    other = Spot(Message("", "EM10CR", 37, callhash("KC1ATT")), 30.0, 0.1, 50.0, {"a": -20.0})
    _resolve_hashes([(0.0, Slot({"a": [hashed, other]}, [])), (120.0, Slot({"a": [plain]}, []))])
    assert hashed.message.call == "K1ABC"
    assert other.message.call == ""


def test_csv_has_one_row_per_spot_with_its_drift(tmp_path):
    spot = Spot(Message("K1ABC", "FN42", 37), 97.1, 0.25, 42.0, {"a": -21.0, "b": -24.0}, -1.5)
    path = tmp_path / "spots.csv"
    _write_csv(path, [(1795120200.0, spot, ["a"], "both", {"N1A", "N2B"})], 14_097_000.0)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows == [{
        "utc": "2026-11-19T20:30:00Z", "mhz": "14.097097", "snr": "-21.0", "dt": "0.25",
        "drift": "-1.5", "metric": "42.0", "call": "K1ABC", "grid": "FN42", "power": "37",
        "found_by": "both", "receivers_alone": "a", "wspr_live_reporters": "2",
    }]


def test_decode_refuses_a_csv_path_it_cannot_write_before_decoding(tmp_path, capsys):
    missing = tmp_path / "no-such-dir" / "spots.csv"
    assert main(["decode", str(tmp_path / "absent.londopy"), "--csv", str(missing)]) == 1
    assert "not a directory" in capsys.readouterr().err
