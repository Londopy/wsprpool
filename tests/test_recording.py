import struct
import threading
import time
import zipfile
from datetime import datetime, timezone

import numpy as np
import pytest

from wsprpool import Message, decode_slot
from wsprpool._report import tag
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
    assert tag(1, ["a"], False) == "alone"
    assert tag(2, [], True) == "combined only"
    assert tag(2, ["a"], True) == "both"
    assert tag(2, ["a"], False) == "alone only"


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


def test_watch_stops_receivers_that_end_the_session_or_stop_sending(tmp_path, capsys):
    names = ("talks.example", "silent.example", "stalls.example", "cut.example")
    talking, silent, stalled, cut = (Receiver(h) for h in names)
    ended = ("INFO cut.example:8073 server closed the connection cleanly. "
             "Reconnecting after 5 seconds.")
    for rx, wav, log in ((talking, 100_000, "Block: 1"), (silent, 44, ""),
                         (stalled, 100_000, "Block: 9"), (cut, 100_000, ended)):
        (tmp_path / rx.label).mkdir()
        (tmp_path / rx.label / "x_iq.wav").write_bytes(bytes(wav))
        (tmp_path / rx.label / "kiwirecorder.log").write_text(log)
    procs = {rx: _Proc() for rx in (talking, silent, stalled, cut)}
    done = threading.Event()

    def stream():
        while not done.is_set():
            with open(tmp_path / talking.label / "x_iq.wav", "ab") as f:
                f.write(bytes(4800))
            time.sleep(0.02)

    feeder = threading.Thread(target=stream)
    feeder.start()
    try:
        _watch(procs, tmp_path, time.time() - 60, time.time() + 0.6, poll=0.05, quiet=0.3)
    finally:
        done.set()
        feeder.join()
    assert [procs[rx].killed for rx in (talking, silent, stalled, cut)] == [False, True, True, True]
    err = capsys.readouterr().err
    assert "silent.example:8073 connected but sent nothing" in err
    assert "stalls.example:8073 stopped sending samples after 1 min" in err
    assert "cut.example:8073: server closed the connection cleanly after 1 min" in err


T0 = 1_000_000.0


class Clock:
    """Stands in for the time module inside _kiwi; sleep() moves time and runs scripted events."""

    def __init__(self):
        self.now = T0
        self.events = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        due = [e for e in self.events if e[0] <= self.now]
        self.events = [e for e in self.events if e[0] > self.now]
        for _, fn in sorted(due, key=lambda e: e[0]):
            fn()

    def at(self, offset, fn):
        self.events.append((T0 + offset, fn))


class Proc:
    def __init__(self, clock, exits_at=None, code=0):
        self.clock, self.exits_at, self.code = clock, exits_at, code
        self.killed = False
        self.returncode = None

    def poll(self):
        if self.killed:
            self.returncode = -9
        elif self.exits_at is not None and self.clock.now >= T0 + self.exits_at:
            self.returncode = self.code
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(_kiwi, "time", c)
    return c


def recorder(tmp_path, host, wav=0, log=""):
    rx = Receiver(host)
    d = tmp_path / rx.label
    d.mkdir()
    (d / "kiwirecorder.log").write_text(log)
    if wav:
        (d / "x_iq.wav").write_bytes(bytes(wav))
    return rx, d / "x_iq.wav"


def grow(clock, wav, seconds):
    """One 2 KB block per second at each of the given offsets."""
    for s in seconds:
        clock.at(s, lambda: wav.write_bytes(wav.read_bytes() + bytes(2066)))


CUT = "pid 1 cut.example:8073 server closed the connection cleanly. Reconnecting after 5 seconds."


def test_every_dropped_recorder_is_reported_once_and_none_as_quitting(tmp_path, clock):
    cut, _ = recorder(tmp_path, "cut.example", 100_000, CUT)
    silent, _ = recorder(tmp_path, "silent.example", 44)
    live, wav = recorder(tmp_path, "live.example", 100_000, "Block: 1")
    grow(clock, wav, range(1, 40))
    procs = {rx: Proc(clock) for rx in (cut, silent, live)}
    msgs = []
    _watch(procs, tmp_path, T0, T0 + 40, poll=1.0, log=msgs.append, quiet=5.0, end=T0 + 120)
    assert [m.split()[0] for m in msgs] == ["cut.example:8073:", "silent.example:8073"]
    assert [procs[rx].killed for rx in (cut, silent, live)] == [True, True, False]


def test_a_recorder_that_quits_early_is_reported_once_while_others_keep_going(tmp_path, clock):
    early, _ = recorder(tmp_path, "early.example", 100_000, "Block: 1")
    live, wav = recorder(tmp_path, "live.example", 100_000, "Block: 1")
    grow(clock, wav, range(1, 30))
    grow(clock, tmp_path / early.label / "x_iq.wav", range(1, 10))
    procs = {early: Proc(clock, exits_at=10, code=3), live: Proc(clock)}
    msgs = []
    _watch(procs, tmp_path, T0, T0 + 30, poll=1.0, log=msgs.append, quiet=5.0, end=T0 + 120)
    assert len(msgs) == 1
    assert msgs[0].startswith("early.example:8073 stopped by itself after 10 s (exit 3)")


def test_a_pause_shorter_than_quiet_is_not_a_stall(tmp_path, clock):
    rx, wav = recorder(tmp_path, "pauses.example", 44, "Block: 1")
    grow(clock, wav, [*range(1, 11), *range(25, 41)])
    msgs = []
    p = Proc(clock)
    _watch({rx: p}, tmp_path, T0, T0 + 40, poll=1.0, log=msgs.append, quiet=20.0)
    assert not p.killed and msgs == []


def test_a_stall_is_dropped_quiet_after_the_last_data_and_says_when_it_stopped(tmp_path, clock):
    rx, wav = recorder(tmp_path, "stalls.example", 44, "Block: 1")
    grow(clock, wav, range(1, 11))
    msgs = []
    p = Proc(clock)
    _watch({rx: p}, tmp_path, T0, T0 + 100, poll=1.0, log=msgs.append, quiet=20.0)
    assert p.killed
    assert 30 <= clock.now - T0 <= 32
    assert msgs[0].startswith("stalls.example:8073 stopped sending samples after 10 s,")


def test_stop_ends_the_watch_at_the_next_poll(tmp_path, clock):
    rx, wav = recorder(tmp_path, "ok.example", 44, "Block: 1")
    grow(clock, wav, range(1, 100))
    ev = threading.Event()
    clock.at(3, ev.set)
    msgs = []
    p = Proc(clock)
    _watch({rx: p}, tmp_path, T0, T0 + 100, poll=1.0, log=msgs.append, stop=ev, end=T0 + 90)
    assert clock.now - T0 <= 4 and not p.killed and msgs == []


def test_the_watch_ends_when_every_recorder_has_exited(tmp_path, clock):
    a, _ = recorder(tmp_path, "a.example", 100_000, "Block: 1")
    b, _ = recorder(tmp_path, "b.example", 100_000, "Block: 1")
    procs = {a: Proc(clock, exits_at=2), b: Proc(clock, exits_at=3)}
    _watch(procs, tmp_path, T0, T0 + 100, poll=1.0, log=lambda m: None, end=T0 + 4)
    assert clock.now - T0 <= 5


def test_an_exit_between_the_poll_and_the_final_check_is_still_reported(tmp_path, clock):
    class Late(Proc):
        calls = 0

        def poll(self):
            self.calls += 1
            if self.calls > 1:
                self.returncode = 7
            return self.returncode

    rx, _ = recorder(tmp_path, "late.example", 100_000, "Block: 1")
    msgs = []
    _watch({rx: Late(clock)}, tmp_path, T0 - 70, T0 + 100, poll=1.0, log=msgs.append, end=T0 + 50)
    assert len(msgs) == 1 and "stopped by itself" in msgs[0]


def test_record_reports_a_recorder_that_quits_on_its_own(tmp_path):
    tool = tmp_path / "krec.py"
    tool.write_text("import sys\nsys.exit(3)\n")
    msgs = []
    record([Receiver("quits.example")], 14097.1, 1, tmp_path / "w", tool, log=msgs.append,
           start=time.time() + 0.2)
    assert any("quits.example:8073 stopped by itself" in m and "(exit 3)" in m for m in msgs)


def test_record_returns_soon_after_stop_is_set(tmp_path):
    tool = tmp_path / "krec.py"
    tool.write_text("import time\ntime.sleep(60)\n")
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    began = time.time()
    record([Receiver("slow.example")], 14097.1, 1, tmp_path / "w", tool, stop=ev,
           start=began + 0.1)
    assert time.time() - began < 5


def test_a_stop_during_the_countdown_starts_no_recorder(tmp_path):
    tool = tmp_path / "krec.py"
    tool.write_text("import time\ntime.sleep(60)\n")
    ev = threading.Event()
    ev.set()
    assert record([Receiver("slow.example")], 14097.1, 1, tmp_path / "w", tool, stop=ev,
                  start=time.time() + 30) == {}
    assert not (tmp_path / "w").exists()


def test_a_recorder_that_never_wrote_is_not_also_called_a_short_recording(tmp_path, clock):
    rx, _ = recorder(tmp_path, "nothing.example", 0, "boom")
    msgs = []
    _watch({rx: Proc(clock, exits_at=0, code=1)}, tmp_path, T0, T0 + 100, poll=1.0,
           log=msgs.append, end=T0 + 60)
    assert len(msgs) == 1 and "short" not in msgs[0] and "recorded nothing" in msgs[0]


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
