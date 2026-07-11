#!/usr/bin/env python3
"""
app.py — local-web GUI for the X-Rite Color i5, built on i5_driver.py.

A single-user Flask app that holds one open serial session to the instrument and
exposes it over localhost. Two tabs in the UI:

  * Measure   — connect, calibrate (white tile + black trap), measure / recall /
                trigger, live spectrum plot + L*a*b*/roast, rolling CSV log.
  * Maintain  — read-only diagnostics: firmware, decoded status word, usage &
                lamp counts, error counters, stored zoom (aperture) & UV positions.

The serial port is exclusive, so the app keeps a single global I5 session behind a
lock; run only one program that holds the serial port at a time (this app or
the i5_driver CLI).

    pip install -r requirements.txt
    python app.py                 # then open http://127.0.0.1:5000

Viewing without the instrument: seed the readings log from driver CSV/JSON files
and/or USBPcap captures —

    python app.py --load ../data/sample-readings.csv

The FTDI cable auto-detects (COMx on Windows, /dev/cu.usbserial-* on macOS).
Pass a port in the UI to override.
"""

import argparse
import json
import os
import re
import sys
import threading

from flask import Flask, jsonify, request, render_template, send_file

# Reuse the driver that lives one directory up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from i5_driver import (I5, I5Error, Measurement, WAVELENGTHS,  # noqa: E402
                       append_csv, parse_stream, pcap_rx_stream)

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CSV_PATH = os.path.join(DATA_DIR, "readings.csv")

# ── single exclusive instrument session ──────────────────────────────────────
_dev = None                       # I5 | None
_dev_mode = "sci"                 # currently configured specular component
_lock = threading.RLock()         # serialize all device access (port is exclusive)
_log = []                         # in-memory list of reading dicts (newest last)


def _need_dev():
    if _dev is None:
        raise I5Error("not connected — hit Connect first")
    return _dev


def decode_status(word):
    """Human-readable breakdown of a status word like '1120070000300m03'."""
    if not word or not re.match(r"\d", word):
        return {}
    out = {"raw": word}
    out["cal_state"] = {"99": "uncalibrated", "19": "white done, black pending",
                        "11": "calibrated"}.get(word[:2], f"({word[:2]})")
    out["specular"] = {"20": "SCI", "11": "SCE"}.get(word[2:4], f"({word[2:4]})")
    m = re.search(r"([a-z])(\d{2})$", word)
    if m:
        out["class"] = {"s": "status", "c": "config", "w": "whitecal", "b": "blackcal",
                        "m": "measure", "r": "recall"}.get(m.group(1), m.group(1))
    return out


def _rows_from_dict(d, label):
    """Measurement.to_dict() → the per-mode row dicts the UI log uses."""
    rows = []
    for mode, md in d["modes"].items():
        rows.append({"timestamp": d["timestamp"], "label": label, "mode": mode,
                     "L": md["L"], "a": md["a"], "b": md["b"], "C": md["C"], "h": md["h"],
                     "agtron": md["agtron_provisional"], "roast_class": md["roast_class"],
                     "roast_shade": md["roast_shade"], "dE76": md["roast_dE76"],
                     "datasum_ok": md["datasum_ok"], "crc_ok": d["crc_ok"],
                     "reflectance": md["reflectance_pct"], "wavelengths": md["wavelengths_nm"],
                     "flashes": d["fields"].get("flashes", ""),
                     "status": d["fields"].get(f"status[{mode}]", "")})
    return rows


def _record(meas: Measurement, label):
    """Append a measurement to the rolling CSV + in-memory log; return its dicts."""
    append_csv(CSV_PATH, [meas], label)
    rows = _rows_from_dict(meas.to_dict(label), label)
    _log.extend(rows)
    return rows


def _rows_from_csv(path):
    """Rows from a driver/GUI-format CSV (readings.csv / data/benchmark-readings.csv)."""
    import csv as _csv
    fnum = lambda row, key: float(row[key]) if row.get(key) not in (None, "") else 0.0
    rows = []
    with open(path, newline="") as f:
        for row in _csv.DictReader(f):
            spectrum = [float(row[f"r{wl}"]) for wl in WAVELENGTHS if row.get(f"r{wl}")]
            if len(spectrum) != len(WAVELENGTHS):
                continue
            rows.append({"timestamp": row.get("timestamp", ""), "label": row.get("label", ""),
                         "mode": row.get("mode", "sci"),
                         "L": fnum(row, "L"), "a": fnum(row, "a"), "b": fnum(row, "b"),
                         "C": fnum(row, "C"), "h": fnum(row, "h"),
                         "agtron": fnum(row, "agtron_provisional"),
                         "roast_class": row.get("roast_class", ""), "roast_shade": "",
                         "dE76": fnum(row, "roast_dE76"),
                         "datasum_ok": row.get("datasum_ok") == "True",
                         "crc_ok": row.get("crc_ok") == "True",
                         "reflectance": spectrum, "wavelengths": list(WAVELENGTHS),
                         "flashes": row.get("flashes", ""),
                         "status": row.get("status_word", "")})
    return rows


def load_files(paths):
    """Seed the in-memory log from driver CSV/JSON exports and/or USBPcap
    captures, so the GUI can browse/overlay past readings with no instrument."""
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        if path.lower().endswith(".json"):
            with open(path) as f:
                for d in json.load(f):
                    _log.extend(_rows_from_dict(d, d.get("label") or name))
        elif path.lower().endswith(".csv"):
            _log.extend(_rows_from_csv(path))
        else:  # pcap
            try:
                meas = parse_stream(pcap_rx_stream(path))
            except (I5Error, OSError) as e:
                print(f"[load] {path}: {e}", file=sys.stderr)
                continue
            for i, m in enumerate(meas):
                _log.extend(_rows_from_dict(m.to_dict(), f"{name}#{i + 1}"))
        print(f"[load] {path}: log now {len(_log)} readings", file=sys.stderr)


def ok(**kw):
    return jsonify({"ok": True, **kw})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


# ── pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", wavelengths=WAVELENGTHS)


# ── connection ──────────────────────────────────────────────────────────────
@app.get("/api/state")
def api_state():
    with _lock:
        connected = _dev is not None
        info = {"connected": connected, "mode": _dev_mode,
                "port": getattr(_dev, "port", None), "n_readings": len(_log)}
        if connected:
            try:
                info["status"] = decode_status(_dev.status())
            except I5Error:
                pass
        return ok(**info)


@app.post("/api/connect")
def api_connect():
    global _dev, _dev_mode
    body = request.get_json(silent=True) or {}
    port = (body.get("port") or "").strip() or None
    mode = body.get("mode", "sci")
    with _lock:
        if _dev is not None:
            return fail("already connected — disconnect first")
        try:
            dev = I5(port=port, quiet=True)
            info = dev.connect(mode=mode)
        except I5Error as e:
            return fail(e)
        except Exception as e:  # pyserial raises its own error types
            return fail(f"could not open port: {e}")
        _dev, _dev_mode = dev, mode
        return ok(port=dev.port, mode=mode,
                  version=info["version"], serial=info["serial"],
                  status=decode_status(info["status"]),
                  config=info["config"])


@app.post("/api/disconnect")
def api_disconnect():
    global _dev
    with _lock:
        if _dev is not None:
            _dev.close()
            _dev = None
        return ok(connected=False)


@app.post("/api/mode")
def api_mode():
    """Switch the specular component live (config -mode=sci|sce)."""
    global _dev_mode
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("sci", "sce"):
        return fail("mode must be 'sci' or 'sce'")
    with _lock:
        try:
            dev = _need_dev()
            dev.set_config(mode=mode, wlen=10)
        except I5Error as e:
            return fail(e)
        _dev_mode = mode
        return ok(mode=mode)


# ── calibration (two explicit steps so the UI can prompt for tile / trap) ─────
@app.post("/api/whitecal")
def api_whitecal():
    with _lock:
        try:
            word = _need_dev().whitecal()
        except I5Error as e:
            return fail(e)
        return ok(word=word, status=decode_status(word))


@app.post("/api/blackcal")
def api_blackcal():
    with _lock:
        try:
            word = _need_dev().blackcal()
        except I5Error as e:
            return fail(e)
        return ok(word=word, status=decode_status(word),
                  calibrated=word.startswith("11"))


# ── measurement family ────────────────────────────────────────────────────────
def _measure_response(meas, label):
    rows = _record(meas, label)
    return ok(readings=rows)


@app.post("/api/measure")
def api_measure():
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip()
    with _lock:
        try:
            meas = _need_dev().measure()
        except (I5Error, ValueError) as e:
            return fail(e)
        return _measure_response(meas, label)


@app.post("/api/recall")
def api_recall():
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip() or "recall"
    with _lock:
        try:
            meas = _need_dev().recall()
        except (I5Error, ValueError) as e:
            return fail(e)
        return _measure_response(meas, label)


@app.post("/api/trigger")
def api_trigger():
    """Blocks until the operator presses the instrument's Standard/Trial key."""
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip() or "trigger"
    timeout = float(body.get("timeout", 90))
    with _lock:
        try:
            meas = _need_dev().trigger(timeout=timeout)
        except (I5Error, ValueError) as e:
            return fail(e)
        return _measure_response(meas, label)


# ── diagnostics / maintain ─────────────────────────────────────────────────────
@app.get("/api/diag")
def api_diag():
    with _lock:
        try:
            dev = _need_dev()
            out = {"version": dev.version(), "serial": dev.serial_number(),
                   "status": decode_status(dev.status()), "usage": dev.usage(),
                   "errors": dev.errors() or "(none)", "config": dev.get_config(),
                   "zoom": {}, "uv": {}}
            for mem in ("rlav", "rmav", "rsav"):
                try:
                    out["zoom"][mem] = dev.zoom_position(mem)
                except I5Error as e:
                    out["zoom"][mem] = f"error: {e}"
            try:
                out["uv"]["d65"] = dev.uv_position("d65")
            except I5Error as e:
                out["uv"]["d65"] = f"error: {e}"
        except I5Error as e:
            return fail(e)
        return ok(**out)


# ── cloud sync (Cloudflare Worker + D1; see ../cloud/) ────────────────────────
# The GUI proxies uploads through Flask so the browser never talks cross-origin
# and the token stays in one request path. stdlib-only (urllib).

def _cloud_request(base_url, token, path, payload=None, timeout=20):
    import urllib.error
    import urllib.request
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        raise I5Error(f"endpoint returned {e.code} {detail}".strip())
    except urllib.error.URLError as e:
        raise I5Error(f"could not reach endpoint: {e.reason}")


@app.post("/api/cloud/test")
def api_cloud_test():
    body = request.get_json(silent=True) or {}
    try:
        j = _cloud_request(body.get("url", ""), body.get("token", ""), "/api/health")
    except I5Error as e:
        return fail(e)
    return ok(message=f"connected — server has {j.get('readings', '?')} readings")


@app.post("/api/cloud/upload")
def api_cloud_upload():
    body = request.get_json(silent=True) or {}
    if not _log:
        return fail("no readings in the log to upload")
    payload = {"device": body.get("device") or "color-i5",
               "readings": [{k: v for k, v in r.items() if k != "wavelengths"}
                            for r in _log]}
    try:
        j = _cloud_request(body.get("url", ""), body.get("token", ""),
                           "/api/readings", payload)
    except I5Error as e:
        return fail(e)
    return ok(inserted=j.get("inserted", 0), skipped=j.get("skipped", 0),
              total=j.get("total", "?"))


# ── log / export ──────────────────────────────────────────────────────────────
@app.get("/api/log")
def api_log():
    return ok(readings=_log)


@app.post("/api/log/clear")
def api_log_clear():
    _log.clear()
    return ok(readings=[])


@app.get("/download/csv")
def download_csv():
    if not os.path.exists(CSV_PATH):
        return fail("no readings logged yet", 404)
    return send_file(CSV_PATH, as_attachment=True, download_name="i5-readings.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Color i5 local-web GUI")
    ap.add_argument("--load", nargs="*", default=[], metavar="FILE",
                    help="seed the readings log from driver .json exports and/or .pcap captures")
    ap.add_argument("--port", type=int, default=5000, help="HTTP port (default 5000)")
    args = ap.parse_args()
    if args.load:
        load_files(args.load)
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
