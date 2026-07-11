# Color i5 — Serial Protocol (the Rosetta Stone)

**Status: DECODED 2026-07-08.** Documented by capturing the OEM software driving a Color i5 over
RS-232 (USBPcap on the FTDI adapter's USB traffic), then confirmed live against the instrument.
The i5 speaks a **plain-text ASCII line protocol** — no binary framing — so any serial tool can
drive it directly.

## Link settings

- **38400 baud, 8 data bits, No parity, 1 stop bit** (38400 8N1), over an FTDI USB-serial
  cable (`0403:6001`; macOS `/dev/cu.usbserial-*`, Windows `COMx`).
- Manual's "USB **or** RS-232, never both at once" holds — USB-B must be unplugged from the i5.

## Framing

- **Host → instrument:** an ASCII command terminated with **`\r`** (`0x0D`).
- **Instrument → host:** the response, then a **`>`** prompt character marking "ready for next
  command." Multi-line responses use `\n` (`0x0A`) between fields; some end with `\r\n>`.
- It's an interactive command shell — like talking to a serial console. Send command, read until `>`.

## Commands observed (from the OEM software's connect + white-cal sequence)

| Command (as sent) | Purpose | Example response |
|---|---|---|
| `status` | instrument status word | `9920070000300s19` then `\r\n>` |
| `va` | version / all firmware banner | `Color i5: V2.23.00.001:27 5-Jun-2007 09:58:38` + `Boot: V2.2.17` + `I/O PIC1…` + `I/O PIC3…` |
| `usage` | lifetime counters | `-measure=187037, -lifetime_measure=187037, -flash=748070, -lifetime_flash=748070,` |
| `models` | supported instrument models | `Color i5 (base)` / `CE3100` / `Model-D` / `Model-8X00` |
| `r` | read serial number | `i5XXXX` (your unit's serial) |
| `config` | dump current config | `-mode=sci -zoom=rlav -uv=d65 -model="Color i5 (base)" -wlen=10 -flash=4 -flashmode=min -aperture=auto -led=auto -crc_on=false -sequence_on=false` |
| `config -model="Color i5 (base)" -wlen=10` | set model / wavelength interval | status word `…c19` |
| `config -mode=sci -zoom=rlav -wlen=10` | set specular / aperture(zoom) / wlen | status word `…c03` |
| `button all -notify=off` | disable front-panel button notifications | status word |
| `led 0 1 2 -state=off` | control status LEDs | status word |
| `netprofiler created_on` | NetProfiler profile date | `created_on=30-Jun-2022` |
| `netprofiler disable` | disable NetProfiler | `status=disable` |
| `setclock MM-DD-YYYY HH:MM:SS escape` | set RTC | `date= 8-Jul-2026` / `time=19:24:48` |
| `uvrecall -mem=d65` | recall UV/D65 calibration memory | `885` |
| `whitecal` | **white calibration** (white tile on port) | status word `1920070000300w03` (leading `19` = white done, black pending) |
| `blackcal` | **black-trap calibration** (black trap on port) | status word `1120070000300b03` (leading `11` = fully calibrated) |
| `fmeasure signature_version=4` | **take a measurement** (formatted, signed) | full spectral block — see below |

### Calibration sequence (captured 2026-07-09, Session A)

A full auto-cal is simply **`whitecal` then `blackcal`**, ~20 s apart (operator swaps
tile→trap in between); each fires the lamp and answers in ~2 s. No separate "start cal" verb.

```
setclock …            → date/time echo
whitecal              → 1920070000300w03      (white measured; leading 19 = black still pending)
  (operator places black trap)
blackcal              → 1120070000300b03      (black measured; leading 11 = CALIBRATED)
```

The `i5_driver.py cal` command replays exactly this (`calibrate()` = whitecal → blackcal) and treats
a leading-`11` blackcal word as success. Post-cal white-tile reads come back ~90 % flat (L\*≈96,
verified against the OEM software on the same instrument minutes apart: coffee L\*45.7/45.5 vs 45.6).

**The measurement command is `fmeasure`.** Every measurement the OEM software took was preceded by a
`netprofiler disable` (leading space + `netprofiler disable\r` → `status=disable`), then
`fmeasure signature_version=4\r`. The instrument fires its flashes and (~2.2 s later) returns a
multi-line block terminated by `>`.

Config keys of interest for coffee work: `-mode=sci|sce`, `-zoom=rlav` (reflectance LAV),
`-uv=d65`, `-wlen=10` (10 nm interval), `-aperture=auto`, `-flashmode=min`.

## Full command set (from firmware `help`, 2026-07-09)

The firmware **self-documents**: `help` (or `?`) prints a 30-command reference, and
`help "<command>"` prints per-command parameters + the response template. Captured with
`probe_commands.py`; raw dumps in `firmware-help.txt` (overview) and `firmware-help-detail.txt`
(per-command). Unknown verbs return `ERROR:  unknown command '<x>'`. This is the authoritative
vocabulary for the driver/GUI — 30 commands, grouped:

| Command | Params (from `help "cmd"`) | Purpose |
|---|---|---|
| `help` / `?` | `help "cmd"` | list commands / per-command help |
| `status` | — | status string (see status-word section) |
| `r` | — | serial number (`i5XXXX`) |
| `va` | — | firmware/boot/PIC version banner |
| `models` | `-version` | list model transforms |
| `usage` | `-clear` | view **or reset** lifetime measure/flash counters |
| `errors` | — | **show error counts** (untapped; feeds error decode) |
| `comm_sequence` | — | count of received commands (power-cycle/channel-switch detect) |
| `config` | `-mode -zoom -uv -model -wlen -flash -flashmode -aperture -led -crc_on -sequence_on -show_values` | get/set measurement configuration |
| `setclock` | `date time [continuous]` | set real-time clock |
| `whitecal` | — | white calibration (white tile on port) |
| `blackcal` | — | black calibration (open/black-trap port) |
| `setwref` | — | set last-measured standard as new white reference |
| `measure` | measurement params | measure, **unformatted** response |
| `fmeasure` | `signature_version` … | measure, **formatted** response (what the OEM software uses) |
| `trigger` | `trigger_key[key_num]` … | measure **triggered by a front-panel key press** |
| `recall` | `signature_version mode wavelength` | **resend previous measurement** (no re-flash) |
| `zoomrecall` | `-mem` | print stored zoom-lens position (mem e.g. `rlav`) |
| `zoomseek` | `-mem -abs -offset` | **move zoom lens** (aperture) to position |
| `zoomstore` | `-mem` | store zoom-lens position in a memory |
| `uvrecall` | `-mem` | print stored UV-filter position (mem e.g. `d65`) |
| `uvseek` | `-mem -abs -offset` | **move UV filter** to position |
| `uvstore` | `-mem` | store UV-filter position in a memory |
| `button` | `-button[index] -notify` | set front-panel button params (`-notify=off`) |
| `led` | `-led[index] -state` | set front-panel LED params (`-state=off`) |
| `preview` | `-state` | video preview on/off (i5 has a targeting camera) |
| `enablebinary` / `disablebinary` | — | toggle binary CCD-data output |
| `netprofiler` | (multiple) | NetProfiler control; reports `expires_on` (ours lapsed 4-Aug-2022) |

The measurement family (`measure`/`fmeasure`/`trigger`/`recall`) shares one *conceptual* schema
(`help` template): `trigger_key[key]`, `status[mode]`, `data[mode][wavelength]`, `datasum[mode]`,
`gloss`, `measurement_date/time`, `signature`, `signature_version`, `flashes`, `mode`, `wavelength`,
`crc`. But there are **two wire formats**:

### Formatted vs unformatted responses (2026-07-09)

- **`fmeasure`** → the **formatted** `key=value` block documented above (`gloss=…` … `crc=0x…`, `>`).
  This is what the OEM software uses and what `parse_fmeasure()` handles.
- **`measure`** and **`trigger`** → an **unformatted** response: the status word on its own line,
  then the 40 reflectance floats (6 per line, comma-separated, space-padded), terminated by **`$`**
  (for `measure`) or **`=`** (for `trigger`), then the usual `>`. **No gloss/date/signature/datasum/
  crc.** Example (`measure`, white tile, SCI):

  ```
  1120070000300m03
   -0.50,  71.27,  77.79,  83.37,  87.43,  90.02
   … 40 values total …
   97.64,  98.19,  97.83,  97.71
  $
  ```

  `parse_unformatted()` handles this (infers mode from the status word, computes datasum, no CRC).
- **`trigger`** waits for a front-panel key press (the i5's **Standard**/**Trial** buttons) and needs
  button notifications **on** — the standard connect sequence sends `button all -notify=off`, so re-enable with
  `button all -notify=on` before arming. Confirmed live: pressing **Standard** fires the measurement
  and returns the unformatted block, e.g. status word `1120070000300003` then 40 floats then `=`.
  Note **position 13 of the trigger status word is a digit** (the `trigger_key` number, `0` for the
  Standard key) where `measure` puts the class letter `m` — so `parse_unformatted()` accepts an
  alnum there.
- **`recall`** re-sends the previous measurement with **no new flash**, in the **formatted** block —
  its status-word letter is **`r`** (`…r03`), joining s/c/w/b/m as command classes.

## The `fmeasure` response block (DECODED 2026-07-08)

Newline-separated `key=value` fields, then `>`. Example (white tile, `sci`):

```
gloss=100.00
measurement_date= 8-Jul-2026
measurement_time=20:36:51
signature=V4S3N05i5XXXXA6a4eb4e3M3baseW0a2ee168X63
flashes=4
status[sci]=1120070000300m03
data[sci]=-0.20,69.69,75.44,79.32,80.74,83.57,84.62,85.84,86.33,86.74,87.45,87.84,88.21,88.84,89.37,89.14,89.13,89.74,90.09,90.26,90.42,90.35,90.37,90.58,90.64,90.92,91.04,90.72,90.44,90.08,90.54,91.03,91.38,90.87,90.82,91.17,90.72,90.96,91.20,91.19
datasum[sci]=3437.60
crc=0xd4f3c128
```

- **`data[sci]=` is the spectral curve: exactly 40 comma-separated %-reflectance values.** With
  `-wlen=10`, that maps to **360–750 nm in 10 nm steps** (`(750−360)/10 + 1 = 40`). First value
  (360 nm, deep UV) reads slightly negative on non-fluorescent targets — normal near-zero noise.
- The `[sci]`/`[sce]` tag echoes the specular mode (`-mode`). A dual-mode capture would emit both
  `data[sci]=…` and `data[sce]=…` blocks.
- **Specular mode is selected with `config -mode=sci|sce`** (just send
  `config -mode=sce -wlen=10`). In `sce` the sphere's specular port is open, so a
  measurement excludes the gloss component and reads **lower** than `sci`: the white tile gave
  SCE L\*≈94.1 / datasum ≈3327 vs SCI L\*≈96.7 / datasum ≈3502. The driver parses either mode with
  no code change; `i5_driver.py measure --mode sce` configures it live.
- **`datasum[sci]`** = arithmetic sum of the 40 values (a cheap integrity/quick-brightness check);
  **`crc`** = CRC-32, **cracked 2026-07-09**: polynomial `0x04C11DB7`, **init 0, not reflected
  (MSB-first), no final XOR** — a plain forward CRC-32, *not* the common zlib/PKZIP variant.
  Coverage: the response block from its **leading `\n`** (the first byte the instrument sends)
  through the `\n` ending the `datasum[…]=` line — everything before the literal `crc=` text.
  Verified against **12/12** captured `fmeasure` blocks (all 10 in the 203139 session + both
  coffee reads in 211922). `i5_driver.py` computes and checks both fields on every measurement.
- `signature` is an opaque signed token (version `V4`, embeds the unit's serial number); `gloss=100.00` is a
  placeholder on this base (non-gloss) unit; `flashes=4` matches `config -flash=4`.

### Cross-check: the 8 captured measurements match their known targets

| time | datasum | curve shape | target |
|---|---|---|---|
| 20:36:51 / 20:37:01 | ~3438–3443 | flat ~90% | white tile (`…white-tile` / `…white`) |
| 20:39:34 | 1738.95 | low blue-green, steep rise to ~91% at red | `red-box` |
| 20:41:30 / 20:41:34 | ~2051–2054 | mid bump ~530 nm, dip, high red end (~104%) | `brown-leaf` |
| 20:42:13 / 20:42:16 | ~1017.6 | peak ~460 nm (~73%), low elsewhere | `blue-test` |

Reflectance >100% at the red end on the warm targets is expected (relative to the white reference).

## The status word (trailing letter = command class; leading digits = cal state)

Fixed-width numeric string + a command-type letter + a 2-digit code, e.g. `1120070000300` `m` `03`.
- **Trailing letter = command class**: **`s`**=status, **`c`**=config, **`w`**=whitecal,
  **`b`**=blackcal, **`m`**=measure, **`r`**=recall (confirmed 2026-07-09).
- **Leading digits = calibration state** (confirmed across the Session A cal sequence):
  - `99…` — **uncalibrated** (fresh power-on `status` returns `9920070000300s19`).
  - `19…` — **white done, black-trap pending** (the `whitecal` reply).
  - `11…` — **fully calibrated** (the `blackcal` reply `…b03`, and every subsequent measure/config
    word `…m03` / `…c03`).
- The trailing 2-digit code was `19` on the uncalibrated status and `03` once calibrated.
- **Positions 3–4 encode the specular mode**: SCI measure word `11`**`20`**`070000300m03` vs SCE
  `11`**`11`**`070000300m03` (Session C). The rest of the middle field (`070000300`) was constant
  across every capture. All states needed for coffee work are decoded.

## Calibration troubleshooting

"Incorrect port plate used" / "light levels too low" errors are usually **not** comms problems.
Two common root causes: the configured aperture (`-zoom`/`-aperture`) doesn't match the physically
installed port plate, or the "white tile" on the port isn't a genuine reflectance calibration tile.
Leaving `config` at `-zoom=auto -aperture=auto` lets the instrument report its own aperture rather
than being forced into a mismatched one; with that plus the real white tile + black trap,
calibration completes and measurements follow.

## Why this matters

- **The driver exists: `i5_driver.py`** (2026-07-09) — connect handshake, `whitecal`, `fmeasure`,
  block parsing with datasum+CRC verification, spectral→L\*a\*b\* (D65/10°), SCA universal-color-curve
  roast classification, CSV/JSON logging, a raw-protocol `shell`, and an offline `replay` mode that
  parses USBPcap captures with zero hardware (validated 12/12 against the July 8 sessions).
  Needs only pyserial (replay mode is stdlib-only).
- Works identically on macOS and Windows — the protocol is OS-independent.

*Documented from USBPcap captures of the OEM software driving the instrument (connect, white/black
calibration, and an 8-measurement run); the CRC was verified against 12/12 captured
`fmeasure` blocks. `i5_driver.py replay <pcap>` parses such captures offline.*
