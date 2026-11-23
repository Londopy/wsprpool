"""A local web GUI: pick KiwiSDRs, record a session, decode it and run the simulator.

The server listens on 127.0.0.1 only. Every API call needs the random token the page
gets from its URL fragment, and the Host header must name this server, so neither
another site in the browser nor a DNS rebinding trick can drive it.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse

from ._kiwi import Receiver, next_start, public_receivers, record, unusable, usable
from ._report import Report, decode_session
from ._session import load, save
from ._sim import gains, sweep

STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/app.css": ("app.css", "text/css")}
# keep sessions on other people's receivers short
MAX_MINUTES = 60
LIST_TTL = 600.0


class Job:
    """One background task at a time, with a state the page polls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.stop = threading.Event()
        self._state: dict[str, Any] = {"phase": "idle"}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, work: Callable[[Job], None], state: dict[str, Any]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.stop = threading.Event()
            self._state = {**state, "log": [], "started": time.time()}
            self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
            self._thread.start()
            return True

    def _run(self, work: Callable[[Job], None]) -> None:
        try:
            work(self)
            self.update(phase="stopped" if self.stop.is_set() else "done")
        # a corrupt session file can fail in any parser along the way; whatever it is, the
        # page has to hear about it or the job looks stuck forever
        except Exception as e:
            self.update(phase="error", error=str(e) or type(e).__name__)

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def log(self, text: str) -> None:
        with self._lock:
            self._state["log"] = [*self._state.get("log", []), text]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**json.loads(json.dumps(self._state)), "now": time.time()}

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def receiver_info(e: dict[str, str]) -> dict[str, Any] | None:
    """What the page shows for one entry of the public list, or None if it has no address."""
    url = urlparse(e.get("url", ""))
    if not url.hostname:
        return None
    lat: float | None
    lon: float | None
    try:
        lat, lon = (float(v) for v in e.get("gps", "").strip("()").split(","))
    except ValueError:
        lat = lon = None
    try:
        why = unusable(e)
    except (TypeError, ValueError):
        why = "its listing is incomplete"
    try:
        lo, hi = (int(v) for v in e.get("bands", "").split("-"))
    except ValueError:
        lo, hi = 0, 30_000_000
    hw = e.get("sdr_hw", "")
    limit = "none" if "⏳\U0001f6ab" in hw else "yes" if "⏳" in hw else "unknown"
    return {
        "id": f"{url.hostname}:{url.port or 80}", "name": e.get("name", ""),
        "loc": e.get("loc", ""), "grid": e.get("grid", "")[:6], "lat": lat, "lon": lon,
        "users": _int(e.get("users")), "users_max": _int(e.get("users_max")),
        "antenna": e.get("antenna", ""), "time_limit": limit, "low_hz": lo, "high_hz": hi,
        "usable": why is None, "why": why,
    }


def report_json(rep: Report) -> dict[str, Any]:
    rows = []
    for e in rep.entries:
        m, s = e.spot.message, e.spot
        rows.append({
            "utc": datetime.fromtimestamp(e.minute, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "mhz": round((rep.freq_hz + s.freq) / 1e6, 6), "snr": round(max(s.snr.values()), 1),
            "dt": round(s.dt, 2), "drift": s.drift, "metric": round(s.metric, 1),
            "message": str(m), "call": m.call, "callhash": m.callhash, "grid": m.grid,
            "power": m.power, "tag": e.tag,
            "alone": e.alone, "reporters": None if e.others is None else len(e.others),
        })
    return {
        "rows": rows,
        "receivers": [{"name": n, "alone": rep.alone_count(n)} for n in rep.receivers],
        "any_alone": rep.any_alone, "combined": rep.combined,
        "combined_only": rep.combined_only, "confirmed": rep.confirmed,
        "checked": rep.checked, "check_error": rep.check_error,
        "correlation": None if rep.correlation is None else {
            "value": round(rep.correlation[0], 3), "pairs": rep.correlation[1]},
    }


class App:
    def __init__(self, workdir: Path, kiwirecorder: str | None) -> None:
        self.workdir = workdir
        self.kiwirecorder = kiwirecorder
        self.token = secrets.token_urlsafe(24)
        self.recorder, self.decoder, self.simulator = Job(), Job(), Job()
        self._list: tuple[float, list[dict[str, Any]]] | None = None
        self._list_lock = threading.Lock()

    def jobs(self) -> list[Job]:
        return [self.recorder, self.decoder, self.simulator]

    def receivers(self, refresh: bool) -> dict[str, Any]:
        with self._list_lock:
            if refresh or self._list is None or time.time() - self._list[0] > LIST_TTL:
                entries = public_receivers()
                info = (receiver_info(e) for e in entries)
                self._list = (time.time(), [i for i in info if i is not None])
            fetched, rows = self._list
        return {"receivers": rows, "fetched": fetched}

    def sessions(self) -> list[dict[str, Any]]:
        found = sorted(self.workdir.glob("*.londopy")) + sorted(self.workdir.glob("*/*.londopy"))
        return [{"path": str(p), "name": str(p.relative_to(self.workdir)),
                 "size": p.stat().st_size, "modified": p.stat().st_mtime} for p in found]

    def start_record(self, body: dict[str, Any]) -> dict[str, Any]:
        ids = body.get("receivers") or []
        if not isinstance(ids, list):
            raise ValueError("receivers must be a list of host:port")
        ids = list(dict.fromkeys(str(i) for i in ids))
        if not 1 <= len(ids) <= 8:
            raise ValueError("pick between 1 and 8 receivers")
        minutes = int(body.get("minutes", 10))
        if not 2 <= minutes <= MAX_MINUTES:
            raise ValueError(f"record for 2 to {MAX_MINUTES} minutes")
        freq = float(body.get("freq_khz", 14097.1))
        if not (math.isfinite(freq) and 10 <= freq <= 30000):
            raise ValueError("the frequency has to be between 10 and 30000 kHz")
        tool = str(body.get("kiwirecorder") or self.kiwirecorder or "")
        if not tool or not Path(tool).is_file():
            raise ValueError("the path to kiwiclient's kiwirecorder.py is needed to record")
        name = str(body.get("output") or datetime.now(timezone.utc).strftime(
            "session-%Y%m%dT%H%MZ.londopy"))
        out = (self.workdir / name).resolve()
        if out.suffix != ".londopy" or not out.parent.is_dir():
            raise ValueError(f"{name} must be a .londopy file in an existing folder")
        # a recording can't be made again, so never write over one
        if out.exists():
            raise ValueError(f"{out.name} already exists; pick another name")
        rxs = [Receiver.parse(i) for i in ids]
        self.kiwirecorder = tool

        def work(job: Job) -> None:
            ok = usable(rxs, job.log)
            if not ok:
                raise ValueError("none of the picked receivers can be used right now")
            # the status checks take a while; the countdown starts from after them
            start = next_start(time.time())
            work_dir = out.with_name(out.stem + "-wav")
            job.update(phase="recording", start=start, end=start + minutes * 60,
                       receivers=[f"{r.host}:{r.port}" for r in ok], work_dir=str(work_dir))
            got = record(ok, freq, minutes, work_dir, Path(tool), log=job.log, stop=job.stop,
                         start=start)
            if not got and job.stop.is_set():
                return
            if not got:
                raise ValueError("no receiver recorded anything")
            job.update(phase="saving")
            save(out, freq, got)
            job.update(output=str(out), recorded=len(got))

        if not self.recorder.start(work, {"phase": "checking", "freq_khz": freq,
                                          "minutes": minutes, "output": str(out)}):
            raise RuntimeError("a recording is already running")
        return self.recorder.snapshot()

    def record_state(self) -> dict[str, Any]:
        st = self.recorder.snapshot()
        wd = st.get("work_dir")
        if wd and st.get("phase") == "recording" and "start" in st:
            sizes = {}
            for rid in st.get("receivers", []):
                d = Path(wd) / Receiver.parse(rid).label
                wavs = [w for w in d.glob("*.wav") if w.stat().st_mtime >= st["start"] - 30]
                sizes[rid] = max((w.stat().st_size for w in wavs), default=0)
            st["bytes"] = sizes
        st["now"] = time.time()
        return st

    def start_decode(self, body: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(body.get("path", ""))).expanduser()
        if not path.is_absolute():
            path = self.workdir / path
        if not path.is_file():
            raise ValueError(f"there's no file at {path}")
        if path.suffix != ".londopy":
            raise ValueError(f"{path.name} isn't a .londopy session file")
        check = bool(body.get("wspr_live", True))

        def work(job: Job) -> None:
            freq, streams = load(path, job.log)
            job.update(phase="decoding", freq_khz=freq,
                       receivers=[s.label for s in streams], slots=[])

            def progress(minute: float | None, res: Any, done: int, total: int) -> None:
                if minute is None:
                    job.update(done=0, total=total)
                    return
                st = job.snapshot()
                job.update(done=done, total=total, slots=[*st.get("slots", []), {
                    "utc": datetime.fromtimestamp(minute, timezone.utc).strftime("%H:%M"),
                    "alone": {n: len(v) for n, v in res.alone.items()},
                    "combined": len(res.combined)}])
                if done == total and check:
                    job.update(phase="checking")

            job.update(report=report_json(decode_session(freq, streams, check, progress,
                                                         job.stop)))

        if not self.decoder.start(work, {"phase": "loading", "path": str(path), "done": 0}):
            raise RuntimeError("a decode is already running")
        return self.decoder.snapshot()

    def start_sim(self, body: dict[str, Any]) -> dict[str, Any]:
        ns = sorted({int(n) for n in body.get("receivers", [1, 2, 4])})
        lo, hi = float(body.get("snr_low", -34)), float(body.get("snr_high", -26))
        trials = int(body.get("trials", 20))
        doppler, shared = float(body.get("doppler", 0)), float(body.get("shared", 0))
        spread, seed = float(body.get("freq_spread", 0.03)), int(body.get("seed", 1))
        if not ns or ns[0] < 1 or ns[-1] > 32 or len(ns) > 8:
            raise ValueError("receiver counts must be 1 to 32, at most 8 of them")
        if lo > hi:
            raise ValueError("SNR from has to be below SNR to")
        if not (-50 <= lo and hi <= 10 and hi - lo <= 30):
            raise ValueError("pick an SNR range within -50 to 10 dB, at most 30 dB wide")
        if not 1 <= trials <= 200:
            raise ValueError("trials must be 1 to 200")
        if not (0 <= shared <= 1 and 0 <= doppler <= 5 and 0 <= spread <= 5):
            raise ValueError("shared is 0 to 1; doppler and frequency spread are 0 to 5 Hz")
        snrs = [lo + i for i in range(int(hi - lo) + 1)]

        def work(job: Job) -> None:
            rates: dict[int, list[float]] = {n: [] for n in ns}

            def got(snr: float, n: int, rate: float) -> None:
                rates[n].append(rate)
                job.update(rates={str(k): v for k, v in rates.items()})

            done = sweep(ns, snrs, trials, doppler, shared, spread, seed, got, job.stop)
            if all(len(v) == len(snrs) for v in done.values()):
                job.update(gains=gains(ns, snrs, done, trials))

        state = {"phase": "running", "receivers": ns, "snrs": snrs, "trials": trials,
                 "doppler": doppler, "shared": shared, "freq_spread": spread, "seed": seed,
                 "rates": {}}
        if not self.simulator.start(work, state):
            raise RuntimeError("a simulation is already running")
        return self.simulator.snapshot()


def handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "wsprpool"
        # a client that opens a connection and goes quiet shouldn't hold a thread forever
        timeout = 30

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def _allowed(self) -> bool:
            if self.headers.get("Host") not in hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return False
            # the token check and the routing must read the same path, so a full URL or a
            # "//host" target can't reach /api/ without the key
            if not self.path.startswith("/") or self.path.startswith("//"):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected a path, not a URL"})
                return False
            given = self.headers.get("X-Token", "").encode("utf-8", "replace")
            if self.path.startswith("/api/") and not secrets.compare_digest(
                    given, app.token.encode()):
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._allowed():
                return
            path = urlparse(self.path)
            if path.path in STATIC:
                name, ctype = STATIC[path.path]
                body = (files("wsprpool") / "ui" / name).read_bytes()
                self._send(HTTPStatus.OK, body, ctype + "; charset=utf-8")
                return
            routes: dict[str, Callable[[], Any]] = {
                "/api/receivers": lambda: app.receivers("refresh=1" in path.query),
                "/api/record": app.record_state,
                "/api/decode": app.decoder.snapshot,
                "/api/sim": app.simulator.snapshot,
                "/api/sessions": lambda: {"sessions": app.sessions(),
                                          "workdir": str(app.workdir),
                                          "kiwirecorder": app.kiwirecorder or ""},
            }
            self._route(routes.get(path.path))

        def do_POST(self) -> None:
            if not self._allowed():
                return
            length = _int(self.headers.get("Content-Length") or 0)
            if length is None or length < 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
                return
            if length > 65536:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "the request body isn't JSON"})
                return
            if not isinstance(body, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object"})
                return
            path = urlparse(self.path).path
            stops = {"/api/record/stop": app.recorder, "/api/decode/stop": app.decoder,
                     "/api/sim/stop": app.simulator}
            if path in stops:
                stops[path].stop.set()
                self._json(HTTPStatus.OK, {"stopping": True})
                return
            starts: dict[str, Callable[[dict[str, Any]], Any]] = {
                "/api/record": app.start_record, "/api/decode": app.start_decode,
                "/api/sim": app.start_sim}
            fn = starts.get(path)
            self._route(None if fn is None else lambda: fn(body))

        def _route(self, fn: Callable[[], Any] | None) -> None:
            if fn is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"no such page: {self.path}"})
                return
            try:
                self._json(HTTPStatus.OK, fn())
            except RuntimeError as e:
                self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            except (ValueError, TypeError, OverflowError) as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except (URLError, OSError) as e:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"network error: {e}"})

    return Handler


class Server(ThreadingHTTPServer):
    # on Windows SO_REUSEADDR lets a second program bind a port already in use, and the
    # browser then talks to whichever answers; elsewhere it only allows quick restarts
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(port: int = 0, browser: bool = True, workdir: Path | None = None) -> None:
    folder = (workdir or Path.cwd()).resolve()
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    app = App(folder, os.environ.get("WSPRPOOL_KIWIRECORDER"))
    try:
        server = Server(("127.0.0.1", port), handler(app, port))
    except OSError as e:
        raise OSError(f"can't listen on 127.0.0.1:{port}, most likely another program has "
                      f"it; pick another --port or leave it out ({e.strerror})") from e
    port = server.server_address[1]
    server.RequestHandlerClass = handler(app, port)
    url = f"http://127.0.0.1:{port}/#t={app.token}"
    print(f"wsprpool GUI at {url}\nworking folder {app.workdir}\nCtrl+C to stop", flush=True)
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for job in app.jobs():
            job.stop.set()
        # a recording only lets go of its receivers once its own thread unwinds; the other
        # jobs touch nothing outside this process
        app.recorder.join(15.0)
        print("stopped", file=sys.stderr)
