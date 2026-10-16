# Texas Lease Production Map

An interactive map of every producing oil and gas lease in Texas, built entirely from Texas Railroad Commission (RRC) public data. It shows each lease's average monthly production (BOE), the top 20 operators by oil and by gas, horizontal laterals, and how each lease ranks against other leases in the same field nearby.

One command downloads the RRC data, processes it and writes:

| File | What it is |
|---|---|
| `map.html` | The interactive map. Self-contained: double-click to open in any current browser. |
| `texas_leases_header.csv` | One row per lease: lease ID, operator, field, county, well counts, location (NAD83), production totals, per-well averages, operator ranks. |
| `texas_leases_monthly_wide.csv` | One row per lease with monthly BOE, oil (bbl) and gas (mcf) columns. |
| `texas_wells.csv` | One row per well: API10, lease ID, county, active flag, surface and bottom-hole lat/long, surface-to-bottom-hole reach. |
| `top20_operators_by_oil.csv`, `top20_operators_by_gas.csv` | Operator rankings for the period. |

The CSVs load into SeisWare Mercury or any GIS tool. Join them on `lease_id` and `API10`.

A finished map for **Aug 2024 – Jul 2026** is in `docs/index.html`. Open it to see the result without installing anything.

---

## Quick start

You need **Python 3.9 or newer**, an internet connection and about **6 GB of free disk space**.

### Windows

```bat
git clone https://github.com/<your-account>/texas-lease-production-map.git
cd texas-lease-production-map
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python texas_pipeline.py
```

### macOS / Linux

```bash
git clone https://github.com/<your-account>/texas-lease-production-map.git
cd texas-lease-production-map
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python texas_pipeline.py
```

Results land in `output/<first month>_<last month>/`. Open `map.html` there.

### Other date ranges

```bash
python texas_pipeline.py                              # last 24 published months (default)
python texas_pipeline.py --months 60                  # last 5 years
python texas_pipeline.py --start 2025-01 --end 2025-12
```

| Option | Default | Meaning |
|---|---|---|
| `--months` | `24` | Number of most recent published months |
| `--start` / `--end` | — | Exact range (YYYY-MM); overrides `--months` |
| `--out` | `output/<start>_<end>` | Output folder |
| `--cache` | `data_cache/` | Where downloads are kept |

### Run time and disk space

- **Downloads:** the first run downloads the RRC production dump (**3.9 GB**, all months since 1993) and 255 county well-location files (about 0.6 GB). The RRC only publishes the full dump, so a shorter date range doesn't shrink the download. Everything is cached in `data_cache/`, and later runs re-download only if a file is missing. Delete `data_cache/pdq/PDQ_DSV.zip` to pick up a newer month. The RRC refreshes the dump on the last Saturday of each month.
- **Processing:** about 5 minutes and 2.5 GB of memory for 24 months. Longer ranges need more memory; plan on a computer with 8 GB or more for 5 years.

### Connection problems

The RRC file server (`mft.rrc.texas.gov`) has a faulty secure-connection setup: it rejects modern TLS 1.3 connections and drops many others. The script works around this by forcing TLS 1.2 and retrying automatically, so errors such as `SSLEOFError` or `connection reset` during download are normal and usually recover. If a run gives up, run the same command again; finished files are kept.

---

## Using the map

- **Dots** are producing leases, placed at the centre of their wells. Colour and size show average BOE per producing month, divided by the lease's active wells. Gas wells get a blue ring when zoomed in.
- **Zoom in** past level 11 to see horizontal laterals.
- **Filters:** lease type (oil leases or gas wells), operator, single-well only, and a search box. Search takes a field name (e.g. *Spraberry*, *Eagleford*, *Wolfcamp*, *Haynesville*), a 10-digit API or a lease ID such as `O-08-12345`.
- **Lease tab:** monthly production, wells (with APIs), oil, gas, GOR, and a ranking against leases in the **same RRC field within 2–20 km**.
- **Operators tab:** top 20 by oil or gas, with the median for true well-level leases.
- **Compare tab:** pin up to six leases side by side.

The map needs internet access to load its mapping library (Leaflet) from cdnjs.cloudflare.com.

### Sharing

- **Email or Teams:** send `map.html` (about 19 MB). It holds all its data.
- **GitHub Pages:** in **Settings → Pages**, choose *Deploy from a branch*, branch `main`, folder `/docs`. The map is then live at `https://<your-account>.github.io/texas-lease-production-map/`. A Pages site is public, and Pages on a private repository needs a paid GitHub plan. To publish a new run, copy its `map.html` over `docs/index.html`.

---

## Method

### Data sources (public, no login)

| Source | Used for |
|---|---|
| [RRC Production Data Query (PDQ) dump](https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/) | Monthly production by lease (`OG_LEASE_CYCLE_DATA_TABLE`), lease ↔ well links (`OG_WELL_COMPLETION_DATA_TABLE`) |
| [RRC Well Layers by County](https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/) | Surface and bottom-hole latitude/longitude (NAD83) by API |
| [us-atlas](https://github.com/topojson/us-atlas) (US Census boundaries) | Texas outline and county lines (`tx_geo.json`) |

### Rules

- **Texas reports production by lease, not by well.** A gas lease is always one well, so gas figures are well-level. An oil lease can hold many wells and reports one combined volume. About half of producing oil leases are single-well. The map's per-well value is the lease total divided by its active wells; the *Single-well only* filter restricts the map to true well-level data.
- **BOE** = oil + condensate + (gas well gas + casinghead gas) ÷ 6.
- **Average BOE per producing month** = total BOE ÷ months with production. **Per well** divides that by active wells, meaning wells not shut in before the window started.
- **Location:** a lease sits at the centre of its wells' bottom holes. Where an API has several bottom holes (sidetracks, laterals), the one farthest from surface is used.
- **Neighbours:** leases in the same RRC field. Field names usually carry the producing formation, so this is the closest public stand-in for "same zone".
- **Operator:** the operator of record in the latest month. Acquired companies often keep filing under their own names.

### Limits

- The most recent 3–4 months are incomplete because of late filings; the map shades them.
- A few single-well oil leases show volumes too high for one well, which suggests more wells report into them than the RRC lists. The map flags single-well oil leases above 60,000 BOE/month.
- About 0.7% of leases have no located well and aren't on the map; they're still in the CSVs.
- Public data has no completion design, landing interval, petrophysics or seismic. Those are what explain performance gaps between neighbouring wells, and they're the data to load alongside these files in Mercury.

---

## Repository layout

```
texas_pipeline.py     download, process, write CSVs and map (the only script to run)
map_template.html     map page; texas_pipeline.py fills in the data
tx_geo.json           Texas outline and county lines
requirements.txt      Python packages
docs/index.html       finished map, Aug 2024 – Jul 2026 (also the GitHub Pages site)
data_cache/           downloads (created on first run, not committed)
output/               results (created on each run, not committed: the CSVs are too large for GitHub)
```

## Data terms

RRC data is published by the Railroad Commission of Texas for public use. Review the RRC's terms before redistributing derived data.
