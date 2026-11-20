import struct
import time
import zipfile
from datetime import datetime, timezone

import numpy as np
import pytest

from wsprpool import Message, decode_slot
from wsprpool._cli import _tag
from wsprpool._demod import TONE_OFFSETS, baseband
from wsprpool import _kiwi
from wsprpool._kiwi import (
    GPS_EPOCH, WEEK, Receiver, _watch, parse_wav, record, timebase, unusable,
)
from wsprpool._session import load, save, slots
from wsprpool._wspr import symbols

MINUTE = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc).timestamp()
MSG = Message("K1ABC", "FN42", 37)


def kiwi_wav(
    iq: np.ndarray, rate: float, t0_utc: float, block: int = 512, no_fix: tuple[int, ...] = ()
) -> bytes:
    """Bytes laid out the way kiwirecorder -w writes them."""
    s = np.empty(2 * len(iq), "<i2")
    s[0::2] = np.round(iq.real * 32767)
    s[1::2] = np.round(iq.imag * 32767)
    body = b""
    for k, i in enumerate(range(0, len(iq), block)):
        tow = (t0_utc + 18 - GPS_EPOCH + i / rate) % WEEK
        sec = int(tow)
        age = 255 if k in no_fix else 0
        nsec = int((tow - sec) * 1e9)
        if k == 0:
            # kiwirecorder's first block carries an all-zero stamp
            sec = nsec = 0
        body += struct.pack("<4sIBBII", b"kiwi", 10, age, 0, sec, nsec)
        chunk = s[2 * i : 2 * (i + block)].tobytes()
        body += struct.pack("<4sI", b"data", len(chunk)) + chunk
    head = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 2, round(rate), round(rate) * 4, 4, 16)
    return struct.pack("<4sI4s", b"RIFF", 4 + len(head) + len(body), b"WAVE") + head + body


def test_timebase_recovers_start_and_rate():
    rate, t0 = 12001.37, MINUTE - 3.25
    wav = parse_wav(kiwi_wav(np.zeros(60000, complex), rate, t0, no_fix=(3, 4)))
    start, fs = timebase(wav, approx_start=t0 + 1.4)
    assert start == pytest.approx(t0, abs=1e-4)
    assert fs == pytest.approx(rate, rel=1e-6)


def test_timebase_refuses_recording_without_gps():
    wav = parse_wav(kiwi_wav(np.zeros(5000, complex), 12000, MINUTE, no_fix=tuple(range(10))))
    with pytest.raises(ValueError, match="no GPS"):
        timebase(wav, MINUTE)


def _signal(
    rate: float, t0: float, seconds: float, freq: float, snr_db: float, rng: np.random.Generator
) -> np.ndarray:
    n = int(seconds * rate)
    t = t0 + np.arange(n) / rate
    sym = np.floor((t - (MINUTE + 1.0)) * 12000 / 8192).astype(int)
    on = (sym >= 0) & (sym < 162)
    inst = np.where(on, freq + TONE_OFFSETS[symbols(MSG)[np.clip(sym, 0, 161)]], 0.0)
    sigma = 0.05
    amp = sigma * np.sqrt(10 ** (snr_db / 10) * 2500 / rate)
    noise = sigma * (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
    return noise + on * amp * np.exp(2j * np.pi * np.cumsum(inst) / rate)


def test_session_round_trip_decodes(tmp_path):
    rate, t0 = 12001.37, MINUTE - 3.2
    iq = _signal(rate, t0, 128.0, 37.5, -20.0, np.random.default_rng(8))
    wav = tmp_path / "20261119T115957Z_14097100_iq.wav"
    wav.write_bytes(kiwi_wav(iq, rate, t0))
    path = tmp_path / "s.londopy"
    save(path, 14097.1, {Receiver("kiwi.example", 8073): wav})

    freq, streams = load(path)
    assert freq == 14097.1
    assert streams[0].start == pytest.approx(t0, abs=1e-3)
    found = [(m, decode_slot(w).combined) for m, w in slots(streams)]
    assert [m for m, _ in found] == [MINUTE]
    spot = found[0][1][0]
    assert spot.message == MSG
    assert spot.freq == pytest.approx(37.5, abs=0.1)
    assert spot.dt == pytest.approx(0.0, abs=0.05)


def test_load_refuses_other_files(tmp_path):
    path = tmp_path / "x.londopy"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("londopy.toml", 'format = 1\napp = "clipvault"\nkind = "profile"\n')
    with pytest.raises(ValueError, match="clipvault profile"):
        load(path)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("londopy.toml", 'format = 9\napp = "wsprpool"\nkind = "recording"\n')
    with pytest.raises(ValueError, match="newer"):
        load(path)


def test_receiver_parse():
    assert Receiver.parse("kiwi.example:8074") == Receiver("kiwi.example", 8074)
    assert Receiver.parse("kiwi.example") == Receiver("kiwi.example", 8073)


def test_baseband_keeps_time_over_a_long_recording():
    rate = 11998.983
    n = int(300 * rate)
    t = np.arange(n) / rate
    iq = np.where((t >= 290) & (t < 291), np.exp(2j * np.pi * 50 * t), 0).astype(np.complex64)
    x, out_rate = baseband(iq, rate)
    onset = int(np.argmax(np.abs(x) > 0.5))
    assert onset / out_rate == pytest.approx(290.0, abs=0.004)


def test_single_receiver_slots_are_not_called_combined():
    assert _tag(1, ["a"], False) == "alone"
    assert _tag(2, [], True) == "combined only"
    assert _tag(2, ["a"], True) == "both"
    assert _tag(2, ["a"], False) == "alone only"


def test_receivers_that_refuse_recording_programs_are_skipped():
    ok = {"status": "active", "offline": "no", "gps_good": "6", "users": "1", "users_max": "8",
          "ext_api": "4"}
    assert unusable(ok) is None
    assert "doesn't allow recording programs" in unusable({**ok, "ext_api": "0"})
    assert "no GPS" in unusable({**ok, "gps_good": "0"})
    assert "slots in use" in unusable({**ok, "users": "8"})


class _Proc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self):
        return -9


def test_watch_stops_receivers_that_end_the_session_or_never_send(tmp_path, capsys):
    talking, silent, cut = (Receiver(h) for h in ("talks.example", "silent.example", "cut.example"))
    ended = ("INFO cut.example:8073 server closed the connection cleanly. "
             "Reconnecting after 5 seconds.")
    for rx, wav, log in ((talking, 100_000, "Block: 1"), (silent, 44, ""), (cut, 100_000, ended)):
        (tmp_path / rx.label).mkdir()
        (tmp_path / rx.label / "x_iq.wav").write_bytes(bytes(wav))
        (tmp_path / rx.label / "kiwirecorder.log").write_text(log)
    procs = {rx: _Proc() for rx in (talking, silent, cut)}
    _watch(procs, tmp_path, time.time() - 60, time.time() + 0.3, poll=0.05)
    assert [procs[rx].killed for rx in (talking, silent, cut)] == [False, True, True]
    err = capsys.readouterr().err
    assert "silent.example:8073 connected but sent nothing" in err
    assert "cut.example:8073: server closed the connection cleanly after 1 min" in err
    assert "not reconnecting" in err


def test_recorder_logs_session_ends_and_gives_up_on_a_full_receiver(tmp_path, monkeypatch):
    seen = []

    class Quiet(_Proc):
        returncode = 0

        def __init__(self, cmd, **kw):
            super().__init__()
            seen.append(cmd)

    monkeypatch.setattr(_kiwi.subprocess, "Popen", Quiet)
    monkeypatch.setattr(_kiwi.time, "sleep", lambda s: None)
    monkeypatch.setattr(_kiwi, "_watch", lambda *a, **k: None)
    record([Receiver("a.example")], 14097.1, 1, tmp_path, tmp_path / "kiwirecorder.py")
    cmd = seen[0]
    # _watch finds session ends in the info-level log; 1 is the lowest count that stops retrying
    assert cmd[cmd.index("--log-level") + 1] == "info"
    assert cmd[cmd.index("--busy-retries") + 1] == "1"
