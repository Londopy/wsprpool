import json
import socket
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from wsprpool import Message, Slot, Spot, _kiwi, _ui
from wsprpool._kiwi import Receiver, parse_list
from wsprpool._report import entries
from wsprpool._session import save
from wsprpool._ui import App, Server, handler, receiver_info

from test_recording import MINUTE, MSG, _signal, kiwi_wav

ENTRY = {
    "url": "http://kiwi.example.org:8074", "name": "0-30 MHz SDR | (51.8, 7.9)",
    "loc": "Somewhere, Europe", "gps": "(51.821407, 7.918051)", "grid": "JO31ut",
    "sdr_hw": "KiwiSDR 2 ⏳\U0001f6ab Limits", "users": "2", "users_max": "8",
    "gps_good": "7", "ext_api": "4", "bands": "0-30000000", "status": "active",
    "offline": "no",
}


def test_public_list_parses_despite_trailing_commas():
    text = ('var kiwisdr_com =\n[\n\t{\n\t\t"name":"a",\n\t\t"users":"1",\n\t},\n'
            '\t{"name":"b",},\n];\n')
    assert [e["name"] for e in parse_list(text)] == ["a", "b"]


def test_receiver_info_reads_a_list_entry():
    info = receiver_info(ENTRY)
    assert info["id"] == "kiwi.example.org:8074"
    assert (info["lat"], info["lon"]) == pytest.approx((51.821407, 7.918051))
    assert info["time_limit"] == "none" and info["usable"] and info["why"] is None
    assert (info["users"], info["users_max"]) == (2, 8)


@pytest.mark.parametrize("change,why", [
    ({"ext_api": "0"}, "doesn't allow recording programs"),
    ({"users": "8"}, "slots in use"),
    ({"gps_good": "0"}, "no GPS"),
    ({"users": ""}, "incomplete"),
])
def test_receiver_info_says_why_a_receiver_cant_record(change, why):
    info = receiver_info({**ENTRY, **change})
    assert not info["usable"] and why in info["why"]


def test_receiver_info_copes_with_missing_coordinates_and_skips_entries_without_an_address():
    info = receiver_info({**ENTRY, "gps": "", "sdr_hw": "KiwiSDR ⏳ Limits"})
    assert info["lat"] is None and info["time_limit"] == "yes"
    assert receiver_info({**ENTRY, "url": ""}) is None


@pytest.fixture
def gui(tmp_path):
    app = App(tmp_path, None)
    server = Server(("127.0.0.1", 0), handler(app, 0))
    port = server.server_address[1]
    server.RequestHandlerClass = handler(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, port
    for job in app.jobs():
        job.stop.set()
        job.join(10)
    server.shutdown()
    server.server_close()


def call(port, path, body=None, token=None, host=None):
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"X-Token": token or "", "Host": host or f"127.0.0.1:{port}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def api(app, port, path, body=None):
    code, _, data = call(port, path, body, app.token)
    return code, json.loads(data)


def wait(app, port, path, phases=("done", "stopped", "error"), timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        _, st = api(app, port, path)
        if st["phase"] in phases:
            return st
        time.sleep(0.2)
    raise AssertionError(f"{path} still {st['phase']} after {timeout} s")


def test_page_loads_without_the_token_but_the_api_needs_it(gui):
    app, port = gui
    code, headers, body = call(port, "/")
    assert code == 200 and b"wsprpool" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert call(port, "/api/sessions")[0] == 403
    assert call(port, "/api/sessions", token="wrong")[0] == 403
    assert call(port, "/api/sessions", token=app.token)[0] == 200


def test_requests_naming_another_host_are_refused(gui):
    # a DNS rebinding page reaches 127.0.0.1 under its own name
    app, port = gui
    assert call(port, "/", host=f"evil.example:{port}")[0] == 403
    assert call(port, "/api/sessions", token=app.token, host="evil.example")[0] == 403


def test_unknown_paths_and_bad_bodies_get_plain_errors(gui):
    app, port = gui
    code, data = api(app, port, "/api/nothing")
    assert code == 404 and "no such page" in data["error"]
    code, _, body = call(port, "/api/sim", b"not json", app.token)
    assert code == 400


def make_session(folder, name="s.londopy"):
    rate, t0 = 12001.37, MINUTE - 3.2
    wav = folder / "20261119T115957Z_14097100_iq.wav"
    wav.write_bytes(kiwi_wav(_signal(rate, t0, 128.0, 37.5, -20.0, np.random.default_rng(8)),
                             rate, t0))
    save(folder / name, 14097.1, {Receiver("kiwi.example", 8073): wav})
    return folder / name


def test_sessions_are_listed_from_the_working_folder(gui):
    app, port = gui
    make_session(app.workdir)
    _, data = api(app, port, "/api/sessions")
    assert [s["name"] for s in data["sessions"]] == ["s.londopy"]


def test_decode_job_reports_progress_and_the_spot_table(gui):
    app, port = gui
    make_session(app.workdir)
    code, _ = api(app, port, "/api/decode", {"path": "s.londopy", "wspr_live": False})
    assert code == 200
    st = wait(app, port, "/api/decode")
    assert st["phase"] == "done" and st["done"] == st["total"] == 1
    rows = st["report"]["rows"]
    assert [r["message"] for r in rows] == [str(MSG)]
    assert rows[0]["tag"] == "alone" and rows[0]["reporters"] is None
    assert st["report"]["receivers"] == [{"name": "kiwi.example-8073", "alone": 1}]


def test_decode_refuses_a_file_that_isnt_a_session(gui):
    app, port = gui
    (app.workdir / "x.txt").write_text("hello")
    code, data = api(app, port, "/api/decode", {"path": "x.txt"})
    assert code == 400 and "isn't a .londopy session file" in data["error"]


def test_sim_job_fills_in_rates_and_gains(gui):
    app, port = gui
    code, _ = api(app, port, "/api/sim", {"receivers": [1, 2], "snr_low": -20, "snr_high": -19,
                                         "trials": 1})
    assert code == 200
    st = wait(app, port, "/api/sim")
    assert st["phase"] == "done"
    assert st["rates"] == {"1": [1.0, 1.0], "2": [1.0, 1.0]}
    assert [g["n"] for g in st["gains"]] == [1, 2]


def test_a_second_sim_while_one_runs_is_refused_and_stop_ends_it(gui):
    app, port = gui
    body = {"receivers": [1], "snr_low": -30, "snr_high": -10, "trials": 3}
    assert api(app, port, "/api/sim", body)[0] == 200
    code, data = api(app, port, "/api/sim", body)
    assert code == 409 and "already running" in data["error"]
    api(app, port, "/api/sim/stop", {})
    assert wait(app, port, "/api/sim")["phase"] == "stopped"


@pytest.mark.parametrize("body,error", [
    ({"receivers": []}, "between 1 and 8"),
    ({"receivers": ["a:8073"], "minutes": 90}, "2 to 60 minutes"),
    ({"receivers": ["a:8073"]}, "kiwirecorder.py is needed"),
    ({"receivers": ["a:8073"], "kiwirecorder": __file__, "output": "x.wav"}, "must be a .londopy"),
])
def test_record_checks_its_settings_before_touching_any_receiver(gui, body, error):
    app, port = gui
    code, data = api(app, port, "/api/record", body)
    assert code == 400 and error in data["error"]


def test_record_job_skips_busy_receivers_and_saves_the_rest(gui, monkeypatch):
    app, port = gui
    free = {"status": "active", "offline": "no", "gps_good": "6", "users": "1",
            "users_max": "8", "ext_api": "4"}
    monkeypatch.setattr(
        _kiwi, "status", lambda rx: {**free, "users": "8"} if rx.host == "busy.example" else free)
    made = {}

    def fake_record(rxs, freq, minutes, out, tool, log, stop, start):
        made["rxs"] = [r.host for r in rxs]
        made["wav"] = make_session(app.workdir, "unused.londopy").parent / (
            "20261119T115957Z_14097100_iq.wav")
        return {rxs[0]: made["wav"]}

    monkeypatch.setattr(_ui, "record", fake_record)
    code, st = api(app, port, "/api/record", {
        "receivers": ["busy.example:8073", "free.example:8073"], "minutes": 2,
        "kiwirecorder": __file__, "output": "mine.londopy"})
    # the slot isn't announced until the receivers have been checked
    assert code == 200 and st["phase"] == "checking" and "start" not in st
    st = wait(app, port, "/api/record")
    assert st["phase"] == "done" and st["recorded"] == 1 and st["start"] > st["started"]
    assert made["rxs"] == ["free.example"]
    assert any("skipping busy.example:8073" in line for line in st["log"])
    assert (app.workdir / "mine.londopy").is_file()


FREE = {"status": "active", "offline": "no", "gps_good": "6", "users": "1", "users_max": "8",
        "ext_api": "4"}


def raw_get(port, target, token=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = f"GET {target} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
    s.sendall((head + (f"X-Token: {token}\r\n" if token else "") + "\r\n").encode())
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    return int(data.split(b" ", 2)[1])


@pytest.mark.parametrize("target", ["http://127.0.0.1:{port}/api/sessions",
                                    "//127.0.0.1:{port}/api/sessions"])
def test_a_url_as_request_target_doesnt_get_past_the_token(gui, target):
    app, port = gui
    assert raw_get(port, "/api/sessions") == 403
    # Python folds a leading "//" into "/" before the handler sees it, so that one is a 404
    assert raw_get(port, target.format(port=port)) in (400, 404)


def test_a_session_file_that_isnt_a_zip_ends_the_job_with_an_error(gui):
    app, port = gui
    (app.workdir / "bad.londopy").write_bytes(b"not a zip" * 20)
    assert api(app, port, "/api/decode", {"path": "bad.londopy", "wspr_live": False})[0] == 200
    st = wait(app, port, "/api/decode", timeout=10)
    assert st["phase"] == "error" and "zip" in st["error"]


def test_a_missing_session_file_is_named_as_missing(gui):
    app, port = gui
    code, data = api(app, port, "/api/decode", {"path": "nowhere.londopy"})
    assert code == 400 and "no file at" in data["error"]


def test_stop_during_the_countdown_stops_the_job_instead_of_failing_it(gui, monkeypatch):
    app, port = gui
    monkeypatch.setattr(_kiwi, "status", lambda rx: FREE)
    monkeypatch.setattr(_ui, "next_start", lambda now: now + 600)
    code, _ = api(app, port, "/api/record", {"receivers": ["a.example:8073"], "minutes": 2,
                                             "kiwirecorder": __file__, "output": "x.londopy"})
    assert code == 200
    st = wait(app, port, "/api/record", phases=("recording",), timeout=10)
    # the countdown the page shows is the one the recorder waits for
    assert st["start"] - st["now"] == pytest.approx(600, abs=5)
    api(app, port, "/api/record/stop", {})
    assert wait(app, port, "/api/record", timeout=10)["phase"] == "stopped"
    assert not (app.workdir / "x.londopy").exists()


@pytest.mark.parametrize("freq", [0, -5, 1e999, 31000])
def test_record_refuses_a_frequency_a_kiwisdr_cant_tune(gui, freq):
    app, port = gui
    code, data = api(app, port, "/api/record", {"receivers": ["a.example:8073"], "minutes": 2,
                                                "freq_khz": freq, "kiwirecorder": __file__,
                                                "output": "x.londopy"})
    assert code == 400 and "frequency" in data["error"]


def test_record_never_writes_over_an_existing_session(gui):
    app, port = gui
    (app.workdir / "kept.londopy").write_bytes(b"earlier recording")
    code, data = api(app, port, "/api/record", {"receivers": ["a.example:8073"], "minutes": 2,
                                                "kiwirecorder": __file__,
                                                "output": "kept.londopy"})
    assert code == 400 and "already exists" in data["error"]
    assert (app.workdir / "kept.londopy").read_bytes() == b"earlier recording"


def test_a_receiver_picked_twice_is_recorded_once(gui, monkeypatch):
    app, port = gui
    monkeypatch.setattr(_kiwi, "status", lambda rx: FREE)
    seen = {}

    def fake_record(rxs, freq, minutes, out, tool, log, stop, start):
        seen["rxs"] = [f"{r.host}:{r.port}" for r in rxs]
        return {}

    monkeypatch.setattr(_ui, "record", fake_record)
    api(app, port, "/api/record", {"receivers": ["a.example:8073", "a.example:8073"],
                                   "minutes": 2, "kiwirecorder": __file__, "output": "y.londopy"})
    wait(app, port, "/api/record")
    assert seen["rxs"] == ["a.example:8073"]


def test_stopping_a_simulation_doesnt_wait_for_the_whole_point(gui):
    app, port = gui
    # one point of 200 four-receiver slots takes minutes
    assert api(app, port, "/api/sim", {"receivers": [4], "snr_low": -20, "snr_high": -20,
                                       "trials": 200})[0] == 200
    time.sleep(1)
    api(app, port, "/api/sim/stop", {})
    st = wait(app, port, "/api/sim", timeout=20)
    assert st["phase"] == "stopped" and st["rates"] == {}


def test_a_reversed_snr_range_is_named_as_reversed(gui):
    app, port = gui
    code, data = api(app, port, "/api/sim", {"snr_low": -20, "snr_high": -30})
    assert code == 400 and "below" in data["error"]


def test_decode_rows_keep_the_order_the_cli_always_printed():
    def spot(call_, freq):
        return Spot(Message(call_, "FN42", 37), freq, 0.0, 50.0, {"a": -20.0})

    rows = entries([(0.0, Slot({"a": [spot("ZZ9ZZ", -60.0), spot("AA1AA", 60.0)]}, []))])
    assert [str(r.spot.message) for r in rows] == ["AA1AA FN42 37", "ZZ9ZZ FN42 37"]


def test_a_receiver_list_in_a_new_format_says_so():
    with pytest.raises(ValueError, match="isn't a JavaScript array"):
        parse_list("<html>moved</html>")
