#!/usr/bin/env python3
"""
i5_driver.py — open-source serial driver for the X-Rite / GretagMacbeth Color i5.

Drives the instrument over its RS-232 port (FTDI USB adapter) using its
plain-text ASCII protocol, documented in `docs/serial-protocol.md`. The only
dependency is pyserial.

Dependencies: Python 3.8+, pyserial (live modes only; `replay` is stdlib-only).

    pip install pyserial

Usage:
    python3 i5_driver.py info                      # connect + identify
    python3 i5_driver.py cal                       # white + black-trap calibration
    python3 i5_driver.py measure -n 3 --label my-coffee --csv readings.csv
    python3 i5_driver.py recall                     # re-pull last measurement (no flash)
    python3 i5_driver.py trigger -n 2               # measure on front-panel key press
    python3 i5_driver.py errors                     # instrument error counters
    python3 i5_driver.py diag                       # maintenance dump (usage/errors/positions)
    python3 i5_driver.py shell                     # raw protocol REPL (for RE)
    python3 i5_driver.py replay captures/i5-session-*.pcap   # offline: parse a pcap

Port notes:
    macOS  : --port auto-detects /dev/cu.usbserial-* (use cu.*, not tty.* —
             tty.* blocks on carrier-detect)
    Windows: --port auto-detects the FTDI COM port
             (verify with: python -m serial.tools.list_ports -v)
    Set I5_FTDI_SERIAL=<your cable's serial> to pin auto-detect to one cable.

Protocol summary (decoded 2026-07-08, firmware V2.23.00.001):
    38400 8N1. Host sends ASCII command + CR. Instrument replies, then a '>' prompt.
    Measurement: 'netprofiler disable' then 'fmeasure signature_version=4' →
    newline-separated key=value block with data[sci]= 40 reflectance floats
    (360–750 nm @ 10 nm), datasum (arithmetic sum), and crc=0x… (CRC-32, poly
    0x04C11DB7, init 0, not reflected, no final XOR, over the block from its
    leading LF through the datasum line's LF — verified against 12/12 captured
    measurements).
"""

import argparse
import csv
import datetime
import json
import math
import os
import re
import struct
import sys
import time

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────

BAUD = 38400
PROMPT = b">"
FTDI_VID, FTDI_PID = 0x0403, 0x6001     # a common FTDI USB-serial cable
FTDI_SERIAL_HINT = os.environ.get("I5_FTDI_SERIAL", "")  # pin detect to one cable

WLEN_START, WLEN_STEP, N_POINTS = 360, 10, 40
WAVELENGTHS = [WLEN_START + WLEN_STEP * i for i in range(N_POINTS)]

CMD_TIMEOUT = 5.0          # ordinary commands answer in tens of ms
MEASURE_TIMEOUT = 60.0     # fmeasure ≈ 2.2 s observed; leave slack
WHITECAL_TIMEOUT = 90.0    # whitecal ≈ 2 s observed; cal cycles can be longer
TRIGGER_TIMEOUT = 120.0    # trigger waits for a human to press the instrument key


# ─────────────────────────────────────────────────────────────────────────────
# CRC-32 as the i5 computes it
# poly 0x04C11DB7, init 0, no input/output reflection, no final XOR.
# Coverage: the response block from its leading '\n' (first byte the instrument
# sends) through the '\n' that ends the datasum line — i.e. everything before
# the literal 'crc=' text. Verified on 12/12 captured fmeasure blocks.
# ─────────────────────────────────────────────────────────────────────────────

def _make_crc_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
        table.append(c)
    return table

_CRC_TABLE = _make_crc_table()

def i5_crc32(data: bytes, init: int = 0) -> int:
    c = init
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((c >> 24) ^ b) & 0xFF]
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Colorimetry: reflectance (360–750 @ 10 nm) → XYZ → CIELAB, D65 / 10° observer
#
# Weights = CIE 1964 10° CMFs × D65 SPD at each band, normalized so ΣW_Y = 100
# (generated with the `colour-science` package, CIE 1964 observer + D65 aligned
# to 360–750 @ 10 nm). Validated against the instrument's OEM software on the
# same 2026-07-08 coffee sample: L*≈45, a*≈3.9, b*≈5.1 vs this table's
# 46.0 / 3.8 / 5.8 — within the expected table-choice/bandpass tolerance.
# ─────────────────────────────────────────────────────────────────────────────

WEIGHTS_D65_10 = [  # (W_X, W_Y, W_Z) per wavelength, 360 → 750 nm
    (0.000000, 0.000000, 0.000002), (0.000027, 0.000003, 0.000117),
    (0.000688, 0.000075, 0.003031), (0.011107, 0.001192, 0.049300),
    (0.136104, 0.014276, 0.612589), (0.667182, 0.068942, 3.065734),
    (1.644345, 0.172008, 7.820327), (2.347578, 0.288532, 11.589319),
    (3.463241, 0.560252, 17.754914), (3.733037, 0.900838, 20.088002),
    (3.064861, 1.299879, 17.696968), (1.933763, 1.830678, 13.024613),
    (0.803203, 2.530008, 7.703340), (0.151446, 3.175884, 3.888736),
    (0.035914, 4.336579, 2.056421), (0.347596, 5.629269, 1.039531),
    (1.061937, 6.870022, 0.547513), (2.191835, 8.111589, 0.282225),
    (3.385492, 8.643961, 0.122886), (4.744391, 8.880844, 0.035711),
    (6.069444, 8.583513, 0.000000), (7.284852, 7.922398, 0.000000),
    (8.360638, 7.163409, 0.000000), (8.537264, 5.933655, 0.000000),
    (8.706763, 5.099706, 0.000000), (7.946302, 4.071261, 0.000000),
    (6.463078, 3.004417, 0.000000), (4.641144, 2.032122, 0.000000),
    (3.108790, 1.295390, 0.000000), (1.848098, 0.741315, 0.000000),
    (1.053268, 0.416156, 0.000000), (0.575419, 0.225184, 0.000000),
    (0.275230, 0.107160, 0.000000), (0.119658, 0.046497, 0.000000),
    (0.059022, 0.022912, 0.000000), (0.029131, 0.011316, 0.000000),
    (0.011531, 0.004486, 0.000000), (0.006284, 0.002450, 0.000000),
    (0.003285, 0.001284, 0.000000), (0.001374, 0.000539, 0.000000),
]
# White point = column sums of the weight table (perfect reflecting diffuser).
WHITE_XYZ = (94.824320, 100.000000, 107.381280)


def spectrum_to_xyz(reflectance_pct):
    """40 %R values (360–750 @ 10 nm) → XYZ on the 0–100 scale."""
    if len(reflectance_pct) != N_POINTS:
        raise ValueError(f"expected {N_POINTS} reflectance values, got {len(reflectance_pct)}")
    X = Y = Z = 0.0
    for r, (wx, wy, wz) in zip(reflectance_pct, WEIGHTS_D65_10):
        f = r / 100.0
        X += f * wx; Y += f * wy; Z += f * wz
    return X, Y, Z


def xyz_to_lab(xyz, white=WHITE_XYZ):
    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29
    fx, fy, fz = (f(c / w) for c, w in zip(xyz, white))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab_extras(L, a, b):
    """Chroma C*ab and hue angle h° (degrees, 0–360)."""
    return math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


# SCA "universal color curve" reference points (Nature Sci. Reports 2025 via
# reference-sca/universal-color-curve-table1.csv) — used to classify a reading.
UCC_POINTS = [
    ("White Roast", "Lighter Grayish Yellow", 70.9, 1.3, 19.3),
    ("White Roast", "Moderate Grayish Yellow", 69.0, 3.2, 22.2),
    ("White Roast", "Darker Grayish Yellow", 66.8, 5.1, 25.0),
    ("White Roast", "Lighter Brownish Yellow", 64.3, 7.0, 27.6),
    ("White Roast", "Moderate Brownish Yellow", 61.4, 9.0, 29.8),
    ("White Roast", "Darker Brownish Yellow", 58.2, 10.8, 31.4),
    ("Ultra Light Roast", "Lighter Yellowish Brown", 54.6, 12.4, 32.3),
    ("Ultra Light Roast", "Moderate Yellowish Brown", 50.8, 13.6, 32.2),
    ("Ultra Light Roast", "Darker Yellowish Brown", 47.0, 14.4, 31.2),
    ("Ultra Light Roast", "Lighter Reddish Brown", 43.4, 14.6, 29.5),
    ("Ultra Light Roast", "Moderate Reddish Brown", 40.0, 14.5, 27.3),
    ("Ultra Light Roast", "Darker Reddish Brown", 36.9, 14.1, 24.8),
    ("Light Roast", "Lighter Moderate Brown", 33.9, 13.3, 22.1),
    ("Light Roast", "Moderate Moderate Brown", 31.1, 12.4, 19.3),
    ("Light Roast", "Darker Moderate Brown", 28.4, 11.3, 16.4),
    ("Medium Roast", "Lighter Deep Brown", 25.8, 10.0, 13.6),
    ("Medium Roast", "Moderate Deep Brown", 23.4, 8.6, 10.7),
    ("Medium Roast", "Darker Deep Brown", 21.0, 7.0, 7.9),
    ("Dark Roast", "Lighter Blackish Brown", 18.6, 5.3, 5.1),
    ("Dark Roast", "Moderate Blackish Brown", 16.3, 3.4, 2.4),
    ("Dark Roast", "Darker Blackish Brown", 14.0, 1.4, -0.3),
]


def classify_roast(L, a, b):
    """Nearest universal-color-curve anchor by ΔE*76. Large ΔE = off-curve
    (e.g. bad sample presentation) — trust the number accordingly."""
    best = min(UCC_POINTS, key=lambda p: (L - p[2]) ** 2 + (a - p[3]) ** 2 + (b - p[4]) ** 2)
    de = math.sqrt((L - best[2]) ** 2 + (a - best[3]) ** 2 + (b - best[4]) ** 2)
    return best[0], best[1], de


# PROVISIONAL L*→Agtron. 3-point fit to Vignoli et al. 2014 anchors from the
# SCA value-assessment tables (L* 24→55, 27→60, 28→65). Instrument-geometry
# dependent; replace slope/intercept with an in-house regression once a
# roast series has been measured against a reference Agtron device.
AGTRON_SLOPE, AGTRON_INTERCEPT = 2.30769, -0.76923
AGTRON_L_RANGE = (14.0, 35.0)   # anchors span L* 24–28; beyond ~this, don't trust it

def agtron_provisional(L):
    return AGTRON_SLOPE * L + AGTRON_INTERCEPT

def agtron_str(L):
    a = agtron_provisional(L)
    lo, hi = AGTRON_L_RANGE
    return f"{a:.0f}" if lo <= L <= hi else f"{a:.0f} (L* outside anchor range — ignore)"


# ─────────────────────────────────────────────────────────────────────────────
# Measurement block parsing
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_RE = re.compile(rb"(\ngloss=.*?)crc=0x([0-9a-f]{8})\s*\n?", re.S)
_DATA_RE = re.compile(r"data\[(sci|sce)\]=([-0-9.,]+)")
_DATASUM_RE = re.compile(r"datasum\[(sci|sce)\]=([-0-9.]+)")
_KV_RE = re.compile(r"^([A-Za-z_]+(?:\[[a-z]+\])?)=(.*)$", re.M)


class Measurement:
    """One parsed fmeasure response."""

    def __init__(self, fields, spectra, datasums, crc_stated, crc_computed, raw):
        self.fields = fields                  # all key=value pairs (str→str)
        self.spectra = spectra                # mode ('sci'/'sce') → [40 floats]
        self.datasums = datasums              # mode → float as reported
        self.crc_stated = crc_stated
        self.crc_computed = crc_computed
        self.raw = raw                        # the exact bytes the CRC covers + crc line
        self.mode = next(iter(spectra)) if spectra else None
        self.timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    # -- integrity ------------------------------------------------------------
    @property
    def crc_ok(self):
        return self.crc_stated is not None and self.crc_stated == self.crc_computed

    def datasum_ok(self, mode=None):
        mode = mode or self.mode
        if mode not in self.spectra or mode not in self.datasums:
            return False
        return abs(sum(self.spectra[mode]) - self.datasums[mode]) < 0.05

    # -- colorimetry ----------------------------------------------------------
    def lab(self, mode=None):
        mode = mode or self.mode
        return xyz_to_lab(spectrum_to_xyz(self.spectra[mode]))

    def summary(self, label=""):
        lines = []
        for mode, values in self.spectra.items():
            L, a, b = self.lab(mode)
            C, h = lab_extras(L, a, b)
            roast, shade, de = classify_roast(L, a, b)
            lines.append(
                f"{label or 'measurement'} [{mode}] "
                f"{self.fields.get('measurement_date', '?').strip()} {self.fields.get('measurement_time', '?')}\n"
                f"  L*={L:6.2f}  a*={a:5.2f}  b*={b:5.2f}   C*={C:5.2f}  h={h:5.1f}°\n"
                f"  roast (nearest SCA curve pt): {roast} / {shade}  (dE76={de:.1f}"
                f"{', OFF-CURVE — check sample presentation' if de > 10 else ''})\n"
                f"  Agtron (L*-derived, PROVISIONAL): {agtron_str(L)}\n"
                f"  datasum={self.datasums.get(mode, float('nan')):.2f} "
                f"({'OK' if self.datasum_ok(mode) else 'MISMATCH'})  "
                f"crc={'OK' if self.crc_ok else 'MISMATCH' if self.crc_stated is not None else 'absent'}  "
                f"flashes={self.fields.get('flashes', '?')}  status={self.fields.get(f'status[{mode}]', '?')}"
            )
        return "\n".join(lines)

    def to_dict(self, label=""):
        out = {"label": label, "timestamp": self.timestamp, "fields": self.fields,
               "crc_ok": self.crc_ok, "modes": {}}
        for mode, values in self.spectra.items():
            L, a, b = self.lab(mode)
            C, h = lab_extras(L, a, b)
            roast, shade, de = classify_roast(L, a, b)
            out["modes"][mode] = {
                "wavelengths_nm": WAVELENGTHS, "reflectance_pct": values,
                "datasum": self.datasums.get(mode), "datasum_ok": self.datasum_ok(mode),
                "L": round(L, 3), "a": round(a, 3), "b": round(b, 3),
                "C": round(C, 3), "h": round(h, 2),
                "agtron_provisional": round(agtron_provisional(L), 1),
                "roast_class": roast, "roast_shade": shade, "roast_dE76": round(de, 2),
            }
        return out

    CSV_HEADER = (["timestamp", "label", "mode", "L", "a", "b", "C", "h",
                   "agtron_provisional", "roast_class", "roast_dE76",
                   "datasum", "datasum_ok", "crc_ok", "flashes", "status_word",
                   "measurement_date", "measurement_time"]
                  + [f"r{wl}" for wl in WAVELENGTHS])

    def csv_rows(self, label=""):
        rows = []
        for mode, values in self.spectra.items():
            L, a, b = self.lab(mode)
            C, h = lab_extras(L, a, b)
            roast, shade, de = classify_roast(L, a, b)
            rows.append([self.timestamp, label, mode,
                         f"{L:.3f}", f"{a:.3f}", f"{b:.3f}", f"{C:.3f}", f"{h:.2f}",
                         f"{agtron_provisional(L):.1f}", roast, f"{de:.2f}",
                         f"{self.datasums.get(mode, float('nan')):.2f}",
                         self.datasum_ok(mode), self.crc_ok,
                         self.fields.get("flashes", ""),
                         self.fields.get(f"status[{mode}]", ""),
                         self.fields.get("measurement_date", "").strip(),
                         self.fields.get("measurement_time", "")]
                        + [f"{v:.2f}" for v in values])
        return rows


def parse_fmeasure(raw: bytes):
    """Parse one fmeasure response (bytes from leading LF up to & incl. crc line)."""
    m = _BLOCK_RE.search(raw)
    if not m:
        raise ValueError("no fmeasure block (gloss=…crc=0x…) found in response")
    body, crc_stated = m.group(1), int(m.group(2), 16)
    crc_computed = i5_crc32(body)
    text = body.decode("ascii", "replace")
    fields = {k: v for k, v in _KV_RE.findall(text) if not k.startswith("data")}
    spectra, datasums = {}, {}
    for mode, csv_vals in _DATA_RE.findall(text):
        vals = [float(x) for x in csv_vals.split(",") if x]
        if len(vals) == N_POINTS:
            spectra[mode] = vals
    for mode, s in _DATASUM_RE.findall(text):
        datasums[mode] = float(s)
    if not spectra:
        raise ValueError("fmeasure block contained no 40-point data[] array")
    return Measurement(fields, spectra, datasums, crc_stated, crc_computed, m.group(0))


def parse_stream(raw: bytes):
    """All fmeasure blocks in an RX byte stream (e.g. a whole session)."""
    return [parse_fmeasure(m.group(0)) for m in _BLOCK_RE.finditer(raw)]


# Unformatted response (from `measure`/`trigger`): a status-word line, then the
# reflectance floats (6/line), terminated by '$' (measure) or '=' (trigger). No
# gloss/date/datasum/crc. Position 13 of the status word is the command-class
# letter for `measure` ('m') but the trigger_key digit for `trigger` (e.g. '0'),
# so accept either an alnum there.
_UNFMT_STATUS_RE = re.compile(r"(\d{13})([0-9A-Za-z])(\d{2})")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")
_SPEC_BY_CODE = {"20": "sci", "11": "sce"}   # status-word positions 3–4


def parse_unformatted(raw: bytes, mode=None) -> "Measurement":
    """Parse an unformatted `measure`/`trigger` response into a Measurement.
    Mode is inferred from the status word (positions 3–4: 20=sci, 11=sce) unless
    given. No integrity fields exist here, so datasum is computed and crc absent."""
    text = raw.decode("ascii", "replace")
    sm = _UNFMT_STATUS_RE.search(text)
    status = sm.group(0) if sm else None
    if mode is None and sm:
        mode = _SPEC_BY_CODE.get(sm.group(1)[2:4], "sci")
    mode = mode or "sci"
    vals = [float(x) for x in _FLOAT_RE.findall(text)]
    if len(vals) != N_POINTS:
        raise ValueError(f"unformatted response had {len(vals)} values, expected {N_POINTS}")
    fields = {f"status[{mode}]": status} if status else {}
    return Measurement(fields, {mode: vals}, {mode: round(sum(vals), 2)}, None, None, raw)


# ─────────────────────────────────────────────────────────────────────────────
# Live serial driver
# ─────────────────────────────────────────────────────────────────────────────

class I5Error(Exception):
    pass


class I5:
    """Session with a Color i5 over RS-232 (38400 8N1, command + CR, reply + '>')."""

    def __init__(self, port=None, baud=BAUD, quiet=False, trace=None):
        try:
            import serial  # noqa
        except ImportError:
            raise I5Error("pyserial not installed — run: pip install pyserial")
        import serial
        self._serial_mod = serial
        self.port = port or self.detect_port()
        self.quiet = quiet
        # Wire tap: echo raw TX/RX bytes to stderr as they cross the serial line
        # (--trace flag, or I5_TRACE=1 env for embedders like the GUI).
        self.trace = bool(int(os.environ.get("I5_TRACE", "0"))) if trace is None else trace
        self.ser = serial.Serial(self.port, baudrate=baud, bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                 timeout=0.15)
        # Opening the port toggles DTR/RTS; the instrument drops the first command
        # or two until its serial line settles. Let it settle, then clear any
        # power-on/banner bytes so the first real command starts clean.
        time.sleep(0.4)
        self.ser.reset_input_buffer()
        self._log(f"open {self.port} @ {baud} 8N1")

    # -- plumbing --------------------------------------------------------------
    @staticmethod
    def detect_port():
        """Find the bench FTDI cable: prefer its serial number, then any FTDI,
        then any USB-serial device. Works on macOS (cu.*) and Windows (COMx)."""
        from serial.tools import list_ports
        candidates = list(list_ports.comports())
        def rank(p):
            score = 2
            if (p.vid, p.pid) == (FTDI_VID, FTDI_PID):
                score = 1
            if FTDI_SERIAL_HINT and p.serial_number \
                    and FTDI_SERIAL_HINT in p.serial_number:
                score = 0
            return score
        usable = [p for p in candidates if p.vid is not None]
        if not usable:
            raise I5Error("no USB serial port found — plug in the FTDI cable or pass --port")
        best = sorted(usable, key=rank)[0]
        dev = best.device
        # macOS: prefer the callout device; tty.* blocks on carrier-detect.
        if dev.startswith("/dev/tty."):
            dev = dev.replace("/dev/tty.", "/dev/cu.", 1)
        return dev

    def _log(self, msg):
        if not self.quiet:
            print(f"[i5] {msg}", file=sys.stderr)

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _trace(self, text):
        sys.stderr.write(text)
        sys.stderr.flush()

    def cmd(self, command: str, timeout: float = CMD_TIMEOUT) -> bytes:
        """Send one command, return raw response bytes (without the trailing '>')."""
        self.ser.reset_input_buffer()
        if self.trace:
            self._trace(f"\nTX> {command}\n")
        self.ser.write(command.encode("ascii") + b"\r")
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                if self.trace:
                    self._trace(chunk.decode("ascii", "replace"))
                buf += chunk
                if buf.rstrip().endswith(PROMPT):
                    if self.trace:
                        self._trace("\n")
                    resp = bytes(buf)
                    return resp[: resp.rfind(PROMPT)]
        raise I5Error(f"timeout ({timeout:.0f}s) waiting for '>' after {command!r}; "
                      f"got {len(buf)} bytes: {bytes(buf)[:80]!r}")

    def cmd_text(self, command: str, timeout: float = CMD_TIMEOUT) -> str:
        return self.cmd(command, timeout).decode("ascii", "replace").strip()

    # -- protocol verbs ----------------------------------------------------------
    def status(self):
        return self.cmd_text("status")

    def version(self):
        return self.cmd_text("va")

    def serial_number(self):
        return self.cmd_text("r")

    def usage(self):
        return self.cmd_text("usage")

    def get_config(self):
        text = self.cmd_text("config")
        return dict(re.findall(r"-([a-z_]+)=(\"[^\"]*\"|\S+)", text))

    def set_config(self, **kv):
        """set_config(mode='sci', wlen=10) → sends `config -mode=sci -wlen=10`."""
        args = " ".join(f"-{k}={v}" for k, v in kv.items())
        return self.cmd_text(f"config {args}")

    def set_clock(self, when=None):
        when = when or datetime.datetime.now()
        return self.cmd_text(when.strftime("setclock %m-%d-%Y %H:%M:%S escape"))

    def connect(self, setclock=True, leds_off=False, mode="sci"):
        """Standard connect handshake (identify + configure for reflectance,
        10 nm, D65 UV). `mode` selects the specular component: 'sci' (included)
        or 'sce' (excluded) — sent as `config -mode=…`. Returns an info dict."""
        if mode not in ("sci", "sce"):
            raise I5Error(f"mode must be 'sci' or 'sce', got {mode!r}")
        info = {"status": self.status(), "version": self.version(),
                "serial": self.serial_number(), "usage": self.usage()}
        self._log(f"connected: {info['version'].splitlines()[0]}  s/n {info['serial']}")
        self.cmd_text('config -model="Color i5 (base)" -wlen=10')
        self.cmd_text("button all -notify=off")
        if leds_off:
            self.cmd_text("led 0 1 2 -state=off")
        if setclock:
            self.set_clock()
        self.cmd_text("netprofiler disable")
        self.set_config(mode=mode, wlen=10)
        self.cmd_text("uvrecall -mem=d65")
        info["config"] = self.get_config()
        return info

    def whitecal(self):
        """Trigger white calibration (white tile on the port!). Returns the raw
        status word, e.g. '1920070000300w03' — leading '19' = white done but
        black-trap calibration still pending. Follow with blackcal()."""
        self._log("white calibration — white tile on port, measuring…")
        return self.cmd_text("whitecal", timeout=WHITECAL_TIMEOUT)

    def blackcal(self):
        """Trigger black-trap calibration (black trap on the port!). Returns the
        raw status word, e.g. '1120070000300b03' — leading '11' = fully calibrated
        (same leading digits as a good measure word '1120070000300m03')."""
        self._log("black calibration — black trap on port, measuring…")
        return self.cmd_text("blackcal", timeout=WHITECAL_TIMEOUT)

    def calibrate(self, white_prompt=None, black_prompt=None):
        """Full two-step calibration: whitecal then blackcal.
        `white_prompt`/`black_prompt` are optional
        callables invoked before each step (e.g. to ask the operator to place
        the tile/trap). Returns (white_word, black_word, calibrated: bool)."""
        if white_prompt:
            white_prompt()
        w = self.whitecal()
        if black_prompt:
            black_prompt()
        b = self.blackcal()
        return w, b, b.startswith("11")

    def _check(self, meas):
        if meas.crc_stated is not None and not meas.crc_ok:
            self._log(f"WARNING: CRC mismatch (stated {meas.crc_stated:#010x}, "
                      f"computed {meas.crc_computed:#010x})")
        if not meas.datasum_ok():
            self._log("WARNING: datasum mismatch")
        return meas

    def measure(self, signature_version=4) -> Measurement:
        """netprofiler disable + fmeasure → parsed, integrity-checked Measurement."""
        self.cmd_text("netprofiler disable")
        raw = self.cmd(f"fmeasure signature_version={signature_version}",
                       timeout=MEASURE_TIMEOUT)
        return self._check(parse_fmeasure(raw))

    def recall(self, signature_version=4) -> Measurement:
        """Re-send the *previous* measurement's data (no new lamp flash). Same
        formatted block as fmeasure — useful to re-pull without re-measuring."""
        raw = self.cmd(f"recall signature_version={signature_version}", timeout=CMD_TIMEOUT)
        return self._check(parse_fmeasure(raw))

    def enable_buttons(self, on=True):
        """Enable/disable front-panel button notifications (connect() turns them
        off; trigger needs them on)."""
        return self.cmd_text(f"button all -notify={'on' if on else 'off'}")

    def trigger(self, signature_version=4, timeout=TRIGGER_TIMEOUT) -> Measurement:
        """Arm a measurement fired by a front-panel key press. Blocks until the
        operator presses the instrument's measure button (or `timeout`).
        Enables button notifications first (connect() disables them)."""
        self.enable_buttons(True)
        self._log("waiting for a front-panel key press on the instrument…")
        raw = self.cmd(f"trigger signature_version={signature_version}", timeout=timeout)
        return self._check(parse_unformatted(raw))  # trigger uses the unformatted format

    def errors(self):
        """Instrument error counters (raw text)."""
        return self.cmd_text("errors")

    def zoom_position(self, mem):
        """Stored zoom-lens position for a memory name (e.g. rlav/rmav/rsav)."""
        return self.cmd_text(f"zoomrecall -mem={mem}")

    def uv_position(self, mem="d65"):
        """Stored UV-filter position for a memory name."""
        return self.cmd_text(f"uvrecall -mem={mem}")


# ─────────────────────────────────────────────────────────────────────────────
# Offline replay: pull the instrument RX stream out of a USBPcap capture of the
# FTDI adapter. Minimal re-implementation of captures/decode_ftdi_serial.py so
# this file stands alone (same DLT-249 parse, same 2-status-byte strip).
# ─────────────────────────────────────────────────────────────────────────────

def pcap_rx_stream(path, device=None):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"\xd4\xc3\xb2\xa1":
        endian = "<"
    elif data[:4] == b"\xa1\xb2\xc3\xd4":
        endian = ">"
    else:
        raise I5Error(f"{path}: not a pcap")
    if struct.unpack(endian + "I", data[20:24])[0] != 249:
        raise I5Error(f"{path}: not USBPcap (DLT 249)")
    off, pkts = 24, []
    while off + 16 <= len(data):
        _, _, incl, _ = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        pkt = data[off:off + incl]
        off += incl
        if len(pkt) < 27:
            continue
        hdr_len, _, _, _, _, _, dev, endp, transfer, dlen = struct.unpack("<HQIHBHHBBI", pkt[:27])
        pkts.append((dev, endp, transfer, pkt[hdr_len:hdr_len + dlen]))
    if device is None:  # find the FTDI adapter from its device descriptor
        for dev, endp, transfer, payload in pkts:
            if transfer == 2 and endp & 0x80 and len(payload) >= 18 \
                    and payload[0] == 18 and payload[1] == 1:
                vid, pid = struct.unpack("<HH", payload[8:12])
                if (vid, pid) == (FTDI_VID, FTDI_PID):
                    device = dev
                    break
    if device is None:
        raise I5Error(f"{path}: no FTDI device descriptor found; pass --device")
    buf = bytearray()
    for dev, endp, transfer, payload in pkts:
        if dev == device and transfer == 3 and endp & 0x80 and len(payload) > 2:
            buf += payload[2:]  # strip FTDI's 2 modem-status bytes
    return bytes(buf)


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def append_csv(path, measurements, label=""):
    new = True
    try:
        new = not open(path).readline().startswith("timestamp,")
    except FileNotFoundError:
        pass
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(Measurement.CSV_HEADER)
        for m in measurements:
            for row in m.csv_rows(label):
                w.writerow(row)


def write_json(path, measurements, label=""):
    with open(path, "w") as f:
        json.dump([m.to_dict(label) for m in measurements], f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def cli_info(args):
    with I5(args.port, trace=args.trace) as dev:
        info = dev.connect(setclock=not args.no_setclock)
        print(info["version"])
        print(f"serial number : {info['serial']}")
        print(f"status word   : {info['status']}")
        print(f"usage         : {info['usage']}")
        print("config        :", " ".join(f"{k}={v}" for k, v in info["config"].items()))


def cli_cal(args):
    with I5(args.port, trace=args.trace) as dev:
        dev.connect(setclock=not args.no_setclock, mode=args.mode)
        w, b, ok = dev.calibrate(
            white_prompt=lambda: input("Place the WHITE TILE on the port, then press Enter… "),
            black_prompt=lambda: input("Place the BLACK TRAP on the port, then press Enter… "))
        print(f"whitecal status word: {w}")
        print(f"blackcal status word: {b}")
        print("calibration COMPLETE — instrument calibrated (leading '11')." if ok else
              f"calibration INCOMPLETE — expected leading '11', got {b!r}. Re-run.")
        print("(Verify with `measure --no-connect --label white-check`: expect ~90% flat, L*≈96.)")


def _emit(results, args):
    """Shared CSV/JSON output for measure/recall/trigger."""
    if getattr(args, "csv", None):
        append_csv(args.csv, results, args.label)
        print(f"appended {len(results)} row(s) → {args.csv}")
    if getattr(args, "json", None):
        write_json(args.json, results, args.label)
        print(f"wrote {args.json}")


def cli_measure(args):
    with I5(args.port, trace=args.trace) as dev:
        if not args.no_connect:
            dev.connect(setclock=not args.no_setclock, mode=args.mode)
        results = []
        for i in range(args.n):
            if args.pause and i:
                input(f"[{i + 1}/{args.n}] reposition sample, Enter to measure… ")
            m = dev.measure()
            results.append(m)
            print(m.summary(args.label or f"read {i + 1}"))
        _emit(results, args)


def cli_recall(args):
    with I5(args.port, trace=args.trace) as dev:
        m = dev.recall()
        print(m.summary(args.label or "recall"))
        _emit([m], args)


def cli_trigger(args):
    with I5(args.port, trace=args.trace) as dev:
        if not args.no_connect:
            dev.connect(setclock=not args.no_setclock, mode=args.mode)
        results = []
        for i in range(args.n):
            print(f"[{i + 1}/{args.n}] press the measure key on the instrument…")
            m = dev.trigger()
            results.append(m)
            print(m.summary(args.label or f"trigger {i + 1}"))
        _emit(results, args)


def cli_errors(args):
    with I5(args.port, trace=args.trace) as dev:
        print(dev.errors())


def cli_diag(args):
    """Read-only maintenance dump (the future GUI 'maintain' tab)."""
    with I5(args.port, trace=args.trace) as dev:
        print(dev.version())
        print(f"serial   : {dev.serial_number()}")
        print(f"status   : {dev.status()}")
        print(f"usage    : {dev.usage()}")
        print(f"errors   : {dev.errors()}")
        for mem in ("rlav", "rmav", "rsav"):
            try:
                print(f"zoom[{mem}]: {dev.zoom_position(mem)}")
            except I5Error as e:
                print(f"zoom[{mem}]: (error: {e})")
        try:
            print(f"uv[d65]  : {dev.uv_position('d65')}")
        except I5Error as e:
            print(f"uv[d65]  : (error: {e})")


def cli_shell(args):
    with I5(args.port, trace=args.trace) as dev:
        print("raw protocol shell — type commands (e.g. status, va, config); "
              "Ctrl-D / 'quit' to exit")
        while True:
            try:
                line = input("i5> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line in ("quit", "exit"):
                break
            if not line:
                continue
            try:
                t0 = time.time()
                resp = dev.cmd(line, timeout=MEASURE_TIMEOUT)
                print(resp.decode("ascii", "replace").strip())
                print(f"({len(resp)}B, {time.time() - t0:.2f}s)", file=sys.stderr)
            except I5Error as e:
                print(f"error: {e}", file=sys.stderr)


def cli_replay(args):
    all_meas = []
    for path in args.pcap:
        try:
            stream = pcap_rx_stream(path, args.device)
        except I5Error as e:
            print(f"{path}: {e}", file=sys.stderr)
            continue
        meas = parse_stream(stream)
        print(f"── {path}: {len(meas)} measurement(s)")
        for i, m in enumerate(meas):
            print(m.summary(f"#{i + 1}"))
        all_meas += meas
    if args.csv and all_meas:
        append_csv(args.csv, all_meas, args.label)
        print(f"appended {len(all_meas)} row(s) → {args.csv}")
    if args.json and all_meas:
        write_json(args.json, all_meas, args.label)
        print(f"wrote {args.json}")
    if not all_meas:
        sys.exit(1)


def _force_utf8_stdio():
    """Windows consoles default to cp1252, which can't encode the '°'/box-drawing
    glyphs in our output. Reconfigure to UTF-8 where supported (Python 3.7+)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(
        description="Open-source serial driver for the X-Rite Color i5 (38400 8N1).")
    ap.add_argument("--port", help="serial device (auto-detects the FTDI cable if omitted)")
    ap.add_argument("--no-setclock", action="store_true",
                    help="don't set the instrument RTC during connect")
    ap.add_argument("--trace", action="store_true", default=None,
                    help="echo raw serial TX/RX to the terminal as it happens "
                         "(also: I5_TRACE=1 env var)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="connect and identify the instrument")
    cp = sub.add_parser("cal", help="full calibration: white tile then black trap")
    cp.add_argument("--mode", choices=("sci", "sce"), default="sci",
                    help="specular component to configure (default sci)")

    mp = sub.add_parser("measure", help="take measurement(s) and report L*a*b*/roast")
    mp.add_argument("-n", type=int, default=1, help="number of readings (default 1)")
    mp.add_argument("--mode", choices=("sci", "sce"), default="sci",
                    help="specular component: sci=included, sce=excluded (default sci)")
    mp.add_argument("--label", default="", help="sample label for output rows")
    mp.add_argument("--pause", action="store_true",
                    help="prompt between readings (to re-pack/reposition the sample)")
    mp.add_argument("--csv", help="append rows to this CSV")
    mp.add_argument("--json", help="write full spectra to this JSON")
    mp.add_argument("--no-connect", action="store_true",
                    help="skip the connect handshake (instrument already configured)")

    rc = sub.add_parser("recall", help="re-pull the previous measurement (no new flash)")
    rc.add_argument("--label", default="", help="sample label for output rows")
    rc.add_argument("--csv", help="append rows to this CSV")
    rc.add_argument("--json", help="write full spectra to this JSON")

    tp = sub.add_parser("trigger", help="measure on a front-panel key press")
    tp.add_argument("-n", type=int, default=1, help="number of triggered readings (default 1)")
    tp.add_argument("--mode", choices=("sci", "sce"), default="sci",
                    help="specular component (default sci)")
    tp.add_argument("--label", default="", help="sample label for output rows")
    tp.add_argument("--csv", help="append rows to this CSV")
    tp.add_argument("--json", help="write full spectra to this JSON")
    tp.add_argument("--no-connect", action="store_true",
                    help="skip the connect handshake (instrument already configured)")

    sub.add_parser("errors", help="show instrument error counters")
    sub.add_parser("diag", help="read-only maintenance dump (version/usage/errors/positions)")

    sub.add_parser("shell", help="interactive raw command REPL")

    rp = sub.add_parser("replay", help="parse fmeasure blocks out of USBPcap capture(s)")
    rp.add_argument("pcap", nargs="+")
    rp.add_argument("--device", type=int, help="USB device address of the FTDI adapter")
    rp.add_argument("--label", default="", help="sample label for output rows")
    rp.add_argument("--csv", help="append rows to this CSV")
    rp.add_argument("--json", help="write full spectra to this JSON")

    args = ap.parse_args()
    try:
        {"info": cli_info, "cal": cli_cal, "measure": cli_measure,
         "recall": cli_recall, "trigger": cli_trigger, "errors": cli_errors,
         "diag": cli_diag, "shell": cli_shell, "replay": cli_replay}[args.cmd](args)
    except I5Error as e:
        sys.exit(f"i5: {e}")


if __name__ == "__main__":
    main()
