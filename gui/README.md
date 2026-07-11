# Color i5 local-web GUI

A single-user browser UI for the X-Rite Color i5, built on `../i5_driver.py`. It holds
one open serial session to the instrument and drives it over `localhost` — works fully
offline (the spectrum plot is inline SVG, no CDN). Cloud upload is optional (see below).

## Run

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

The FTDI cable auto-detects (COMx on Windows, `/dev/cu.usbserial-*` on macOS). Type a
port in the header field to override. **The serial port is exclusive** — close any other
software holding the port (including the `i5_driver.py` CLI) before starting the app.

**macOS gotcha:** AirPlay Receiver squats on HTTP port 5000, so run
`python app.py --port 5001`.

## Viewing without the instrument

Seed the readings log from driver/GUI `.csv` files, driver `.json` exports, and/or USBPcap
`.pcap` captures:

```bash
python app.py --load ../data/sample-readings.csv
```

## Two tabs

- **Measure** — Connect, pick SCI/SCE, calibrate (white tile → step 1, black trap → step 2),
  then `Measure` / `Recall last` (no flash) / `Trigger` (press the i5's **Standard** key).
  Shows L\*a\*b\*/C\*/h°, a Lab→sRGB swatch, the nearest SCA roast class, provisional Agtron,
  the reflectance spectrum, and a rolling readings log. Every reading also appends to
  `data/readings.csv` (same schema as the CLI); download it from the log card.

  The **spectrum plot** fills the area under the curve with the true color of each
  wavelength (360–750 nm is the visible band, so the x-axis carries the actual rainbow) —
  the plot literally shows *why* the sample has its color. Tick `cmp` boxes in the readings
  log to **overlay curves** (each in its own color; SCE dashed), hover for per-wavelength
  values, or pre-select overlays via URL: `/?cmp=label-a,label-b`.

  The **measurement class** selector (top-right of the Latest reading card) sets the plot
  scale: **General** = full 0–100 %R for arbitrary samples; **Coffee** = 0–40 %R, because
  all coffee lives below ~35 %R and the full scale flattens roast differences into a
  barely-visible slope. Curves above the scale top clip flat (e.g. the white tile in
  Coffee view). Persisted in the browser; URL override: `/?class=coffee`.
- **Maintain** — read-only diagnostics: firmware, decoded status word (cal state + specular),
  usage & lamp-flash counts, error counters, stored zoom (aperture) & UV-filter positions, config.

## Cloud sync (optional)

The **Cloud sync** card uploads the readings log — full spectra included — to a
Cloudflare Worker + D1 database, so multiple benches can pool roast color profiles on
one server. Deploy the Worker from `../cloud/` (5-minute setup, see its README), then
paste the endpoint URL + API token into the card. Uploads are deduped server-side, so
re-uploading is always safe. The upload goes through the Flask backend (no CORS games);
the endpoint/token persist in the browser's localStorage.

## Notes

- Agtron is a **provisional** L\*-derived estimate — replace with an in-house regression once a
  roast series has been measured against a reference device.
- The app serializes all instrument access behind a lock, so `Trigger` (which blocks until you
  press the key) will hold other actions until it returns or times out (~90 s).
