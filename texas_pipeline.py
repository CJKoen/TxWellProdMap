"""
Texas oil & gas lease production map, built from Texas Railroad Commission (RRC) public data.

One command downloads the RRC data, keeps the months you ask for, links leases to their wells
and locations, and writes CSVs plus a self-contained interactive map (map.html) that opens with
a double-click.

    python texas_pipeline.py                          # last 24 published months
    python texas_pipeline.py --months 60              # last 5 years
    python texas_pipeline.py --start 2025-01 --end 2025-12

Data sources (public, no login):
  RRC Production Data Query (PDQ) dump      monthly production by lease, 1993 to date (3.85 GB zip)
  RRC Well Layers by County (shapefiles)    surface and bottom-hole locations by API number
See README.md for the method and its limits.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import ssl
import sys
import time
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "map_template.html")
GEO = os.path.join(HERE, "tx_geo.json")
MFT = "https://mft.rrc.texas.gov"
PDQ_LINK = "1f5ddb8d-329a-4459-b7f8-177b4f5ee60d"        # Production Data Query Dump (CSV)
WELLS_LINK = "d551fb20-442e-4b67-84fa-ac3f23ecabb4"      # Well Layers by County (shapefiles)
ROW_RE = re.compile(r'id="(fileTable:(\d+):j_id_\w+)"[^>]*>([^<]+)</a></td><td role="gridcell" class="ModifiedOnColumn">'
                    r'([^<]*)</td><td role="gridcell" class="SizeColumn">([^<]*)')


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# =========================================================================== RRC downloads
# The RRC file portal (GoAnywhere) fails TLS 1.3 handshakes and drops about half of TLS 1.2
# handshakes. So: force TLS 1.2, keep one connection alive per session, and retry.
class _TLS12(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


def _session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _TLS12())
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; texas-lease-production-map)"
    return s


def _call(s, method, url, tries=12, timeout=90, **kw):
    for i in range(tries):
        try:
            return s.request(method, url, timeout=timeout, **kw)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            if i == tries - 1:
                raise
            time.sleep(3 + 2 * i)


def _page(s, vs, first):
    data = {"javax.faces.partial.ajax": "true", "javax.faces.source": "fileTable",
            "javax.faces.partial.execute": "fileTable", "javax.faces.partial.render": "fileTable",
            "fileTable": "fileTable", "fileTable_pagination": "true", "fileTable_first": str(first),
            "fileTable_rows": "250", "fileTable_skipChildren": "true", "fileTable_encodeFeature": "true",
            "fileList": "fileList", "javax.faces.ViewState": vs}
    r = _call(s, "POST", f"{MFT}/webclient/godrive/PublicGoDrive.xhtml", data=data,
              headers={"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"})
    vs2 = re.findall(r"javax.faces.ViewState[^>]*><!\[CDATA\[([^\]]+)\]\]>", r.text)
    return ROW_RE.findall(r.text), (vs2[0] if vs2 else vs)


def rrc_download(link: str, dest: str, pattern: str = ".*", tries: int = 8) -> None:
    """Download every file in an RRC portal folder whose name matches `pattern` (skips files already saved)."""
    os.makedirs(dest, exist_ok=True)
    rx = re.compile(pattern)
    for attempt in range(tries):
        s = _session()
        try:
            html = _call(s, "GET", f"{MFT}/link/{link}").text
            vs = re.findall(r'name="javax.faces.ViewState"[^>]*value="([^"]+)"', html)[0]
            pages = [ROW_RE.findall(html)]
            total = int((re.findall(r"rowCount:(\d+)", html) or [len(pages[0])])[0])
            first = len(pages[0])
            while first < total:
                more, vs = _page(s, vs, first)
                pages.append(more)
                first += len(more) or 250
            todo = [(p, cid, name.strip(), size) for p, rows in enumerate(pages) for cid, _i, name, _m, size in rows
                    if rx.search(name.strip()) and not os.path.exists(os.path.join(dest, name.strip()))]
            if not todo:
                return
            cur = len(pages) - 1
            for k, (p, cid, name, size) in enumerate(todo, 1):
                if p != cur:          # the table must show the page that holds the row
                    _, vs = _page(s, vs, p * 250)
                    cur = p
                _call(s, "POST", f"{MFT}/webclient/godrive/PublicGoDrive.xhtml", allow_redirects=False,
                      data={"fileList": "fileList", "fileList_SUBMIT": "1", "fileTable_selection": "",
                            "javax.faces.ViewState": vs, cid: cid})
                r = _call(s, "GET", f"{MFT}/link/godrivedownload", stream=True, timeout=600)
                r.raise_for_status()
                expect = int(r.headers.get("Content-Length") or 0)
                tmp = os.path.join(dest, name + ".part")
                got, last = 0, time.time()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                        got += len(chunk)
                        if expect > 200e6 and time.time() - last > 15:
                            log(f"  {name}: {got / 1e9:.2f} of {expect / 1e9:.2f} GB")
                            last = time.time()
                if expect and got != expect:
                    raise IOError(f"{name}: got {got} of {expect} bytes")
                os.replace(tmp, os.path.join(dest, name))
                if len(todo) > 5 and (k % 25 == 0 or k == len(todo)):
                    log(f"  {k} of {len(todo)} files")
            return
        except Exception as e:  # new session; files already saved are skipped
            log(f"  connection problem ({type(e).__name__}); retrying ({attempt + 1}/{tries})")
            time.sleep(10)
    sys.exit("Could not download from mft.rrc.texas.gov. Check your connection and run the command again; "
             "finished files are kept.")


# =========================================================================== processing
def newest_month(pdq: str) -> str:
    with zipfile.ZipFile(pdq) as z:
        t = pd.read_csv(z.open("GP_DATE_RANGE_CYCLE_DATA_TABLE.dsv"), sep="}", dtype=str)
    return t.NEWEST_PROD_CYCLE_YEAR_MONTH.iloc[0]          # YYYYMM


def read_lease_months(pdq: str, start: str, end: str):
    """Stream the 12 GB lease table out of the zip, keeping only months in [start, end] (YYYYMM)."""
    cols = ["OIL_GAS_CODE", "DISTRICT_NO", "LEASE_NO", "CYCLE_YEAR_MONTH", "LEASE_OIL_PROD_VOL", "LEASE_GAS_PROD_VOL",
            "LEASE_COND_PROD_VOL", "LEASE_CSGD_PROD_VOL", "LEASE_NAME", "OPERATOR_NAME", "FIELD_NAME"]
    keep, read = [], 0
    with zipfile.ZipFile(pdq) as z, z.open("OG_LEASE_CYCLE_DATA_TABLE.dsv") as fh:
        for ch in pd.read_csv(fh, sep="}", usecols=cols, dtype=str, chunksize=2_000_000, quoting=3, keep_default_na=False):
            read += len(ch)
            ch = ch[(ch.CYCLE_YEAR_MONTH >= start) & (ch.CYCLE_YEAR_MONTH <= end)]
            if len(ch):
                keep.append(ch)
            log(f"  scanned {read / 1e6:.0f}M rows, kept {sum(map(len, keep)):,}")
    d = pd.concat(keep, ignore_index=True)
    d["key"] = d.OIL_GAS_CODE + d.DISTRICT_NO + d.LEASE_NO
    for c in ("LEASE_OIL_PROD_VOL", "LEASE_GAS_PROD_VOL", "LEASE_COND_PROD_VOL", "LEASE_CSGD_PROD_VOL"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    with zipfile.ZipFile(pdq) as z:
        wc = pd.read_csv(z.open("OG_WELL_COMPLETION_DATA_TABLE.dsv"), sep="}", dtype=str, quoting=3, keep_default_na=False)
    return d, wc


def well_locations(wells_dir: str) -> pd.DataFrame:
    """Bottom-hole and surface locations for every API in the county shapefiles (read straight from the zips)."""
    from pyogrio.raw import read

    def tbl(path, cols):
        m, _f, _g, v = read(path, columns=cols, read_geometry=False)
        return pd.DataFrame(dict(zip(m["fields"], v)))

    B, S = [], []
    for z in sorted(f for f in os.listdir(wells_dir) if re.match(r"well\w+\.zip$", f)):
        stem = z[:-4]
        p = os.path.join(wells_dir, z).replace("\\", "/")
        names = zipfile.ZipFile(os.path.join(wells_dir, z)).namelist()
        if f"{stem}b.shp" in names:
            B.append(tbl(f"/vsizip/{p}/{stem}b.shp", ["SURFACE_ID", "API", "LAT83", "LONG83"]))
        if f"{stem}s.shp" in names:
            S.append(tbl(f"/vsizip/{p}/{stem}s.shp", ["SURFACE_ID", "LAT83", "LONG83"]))
    bh = pd.concat(B, ignore_index=True)
    sh = pd.concat(S, ignore_index=True).drop_duplicates("SURFACE_ID").set_index("SURFACE_ID")
    bh = bh[bh.API.str.len() == 8]
    bh = bh.join(sh.rename(columns={"LAT83": "SLAT", "LONG83": "SLON"}), on="SURFACE_ID")
    bh["SLAT"] = bh.SLAT.fillna(bh.LAT83)
    bh["SLON"] = bh.SLON.fillna(bh.LONG83)
    bh["reach"] = np.hypot((bh.LAT83 - bh.SLAT) * 111320, (bh.LONG83 - bh.SLON) * 111320 * np.cos(np.radians(bh.LAT83)))
    # sidetracks / laterals share an API: keep the bottom hole farthest from surface
    return bh.sort_values("reach", ascending=False).drop_duplicates("API")


def b64gz(raw: bytes) -> str:
    return base64.b64encode(gzip.compress(raw, 9)).decode()


def build(start: str, end: str, out_dir: str, cache: str) -> None:
    pdq_dir, wells_dir = os.path.join(cache, "pdq"), os.path.join(cache, "wells")

    log("Step 1/5  Downloading RRC data (first run: ~3.9 GB production dump + 255 county well files)")
    rrc_download(PDQ_LINK, pdq_dir, r"^PDQ_DSV\.zip$")
    rrc_download(WELLS_LINK, wells_dir, r"^well\w+\.zip$")
    pdq = os.path.join(pdq_dir, "PDQ_DSV.zip")

    newest = newest_month(pdq)
    end = min(end or newest, newest)
    if start is None:
        start = (pd.Period(f"{end[:4]}-{end[4:]}", "M") - (MONTHS - 1)).strftime("%Y%m")
    months = [f"{p.year}{p.month:02d}" for p in pd.period_range(f"{start[:4]}-{start[4:]}", f"{end[:4]}-{end[4:]}", freq="M")]
    mlab = [f"{m[:4]}-{m[4:]}" for m in months]
    days = np.array([pd.Period(m, "M").days_in_month for m in mlab])
    out_dir = out_dir or os.path.join(HERE, "output", f"{mlab[0]}_{mlab[-1]}")
    os.makedirs(out_dir, exist_ok=True)
    log(f"  window {mlab[0]} to {mlab[-1]} ({len(months)} months; newest RRC month is {newest[:4]}-{newest[4:]})")

    log("Step 2/5  Reading lease production (scans the whole 12 GB table; a few minutes)")
    d, wc = read_lease_months(pdq, start, end)
    d["oil"], d["cond"] = d.LEASE_OIL_PROD_VOL, d.LEASE_COND_PROD_VOL
    d["gas"] = d.LEASE_GAS_PROD_VOL + d.LEASE_CSGD_PROD_VOL                  # mcf, incl. casinghead gas
    d["boe"] = d.oil + d.cond + d.gas / 6
    tot = d.groupby("key").boe.sum()
    d = d[d.key.isin(tot[tot > 0].index)]
    last = d.sort_values("CYCLE_YEAR_MONTH").groupby("key")[["LEASE_NAME", "OPERATOR_NAME", "FIELD_NAME"]].last()
    keys = sorted(d.key.unique())
    ki = {k: i for i, k in enumerate(keys)}
    mi = {m: i for i, m in enumerate(months)}
    r, c = d.key.map(ki).values, d.CYCLE_YEAR_MONTH.map(mi).values
    B, O, G, C = (np.zeros((len(keys), len(months))) for _ in range(4))
    for arr, col in ((B, "boe"), (O, "oil"), (G, "gas"), (C, "cond")):
        np.add.at(arr, (r, c), d[col].values)
    del d
    log(f"  {len(keys):,} producing leases")

    log("Step 3/5  Linking leases to wells and locations")
    wc["key"] = wc.OIL_GAS_CODE + wc.DISTRICT_NO + wc.LEASE_NO
    w = wc[wc.key.isin(ki)].copy()
    w["API"] = w.API_COUNTY_CODE + w.API_UNIQUE_NO
    sd = w.WELL_SHUTIN_DT.replace("", "000000")
    w["active"] = (sd == "000000") | (sd >= start)
    w = w.drop_duplicates(["key", "API"])
    bh = well_locations(wells_dir)
    w = w.merge(bh[["API", "LAT83", "LONG83", "SLAT", "SLON", "reach"]], on="API", how="left")

    L = pd.DataFrame(index=keys)
    L["type"] = [k[0] for k in keys]
    L["district"] = [k[1:3] for k in keys]
    L["lease_no"] = [k[3:] for k in keys]
    L = L.join(last)
    L["county"] = w.groupby("key").COUNTY_NAME.agg(lambda s: s.mode().iat[0] if len(s.mode()) else "").reindex(keys).fillna("").values
    L["wells"] = w.groupby("key").size().reindex(keys).fillna(0).astype(int).values
    L["active_wells"] = w[w.active].groupby("key").size().reindex(keys).fillna(0).astype(int).values
    cen = w.dropna(subset=["LAT83"]).groupby("key")[["LAT83", "LONG83"]].mean()
    L["lat"], L["lon"] = cen.LAT83.reindex(keys).values, cen.LONG83.reindex(keys).values
    on = (B > 0).sum(1)
    L["months_on"] = on
    L["oil_bbl"], L["cond_bbl"], L["gas_mcf"], L["boe"] = O.sum(1), C.sum(1), G.sum(1), B.sum(1)
    L["avg_boe_per_prod_month"] = L.boe / np.maximum(on, 1)
    L["avg_boe_per_prod_month_per_well"] = L.avg_boe_per_prod_month / np.maximum(L.active_wells, 1)
    L["single_well"] = L.wells == 1
    op_oil = L.groupby("OPERATOR_NAME").oil_bbl.sum().sort_values(ascending=False)
    op_gas = L.groupby("OPERATOR_NAME").gas_mcf.sum().sort_values(ascending=False)
    L["operator_oil_rank"] = L.OPERATOR_NAME.map({o: i + 1 for i, o in enumerate(op_oil.index)})
    L["operator_gas_rank"] = L.OPERATOR_NAME.map({o: i + 1 for i, o in enumerate(op_gas.index)})
    log(f"  {len(w):,} wells; {L.lat.notna().sum():,} leases located")

    log("Step 4/5  Writing CSVs")
    tdays = int(days.sum())
    lease_id = [f"{k[0]}-{k[1:3]}-{k[3:]}" for k in keys]
    for name, ser in (("oil", op_oil), ("gas", op_gas)):
        t = (L.groupby("OPERATOR_NAME").agg(oil_bbl=("oil_bbl", "sum"), cond_bbl=("cond_bbl", "sum"), gas_mcf=("gas_mcf", "sum"),
                                            boe=("boe", "sum"), leases=("type", "size"), wells=("wells", "sum"))
             .loc[ser.index[:20]])
        t.insert(0, "rank", range(1, len(t) + 1))
        t["oil_bbl_per_day"], t["gas_mcf_per_day"], t["boe_per_day"] = t.oil_bbl / tdays, t.gas_mcf / tdays, t.boe / tdays
        t.round(1).reset_index().rename(columns={"OPERATOR_NAME": "operator"}).to_csv(
            os.path.join(out_dir, f"top20_operators_by_{name}.csv"), index=False)
    hdr = L.copy()
    hdr.insert(0, "lease_id", lease_id)
    hdr = hdr.round({c: 2 for c in hdr.columns if c not in ("lat", "lon")}).round({"lat": 6, "lon": 6})
    hdr.to_csv(os.path.join(out_dir, "texas_leases_header.csv"), index=False)
    mon = pd.concat([pd.DataFrame({"lease_id": lease_id}),
                     pd.DataFrame(np.round(B, 1), columns=[f"BOE_{m}" for m in mlab]),
                     pd.DataFrame(np.round(O, 1), columns=[f"OIL_BBL_{m}" for m in mlab]),
                     pd.DataFrame(np.round(G, 1), columns=[f"GAS_MCF_{m}" for m in mlab])], axis=1)
    mon.to_csv(os.path.join(out_dir, "texas_leases_monthly_wide.csv"), index=False)
    wv = w.assign(lease_id=w.key.str[0] + "-" + w.key.str[1:3] + "-" + w.key.str[3:], API10="42" + w.API)
    wv[["API10", "lease_id", "WELL_NO", "COUNTY_NAME", "active", "SLAT", "SLON", "LAT83", "LONG83", "reach"]].rename(columns={
        "WELL_NO": "well_no", "COUNTY_NAME": "county", "SLAT": "sh_lat_nad83", "SLON": "sh_lon_nad83",
        "LAT83": "bh_lat_nad83", "LONG83": "bh_lon_nad83", "reach": "sh_to_bh_reach_m"}).round(6).to_csv(
        os.path.join(out_dir, "texas_wells.csv"), index=False)

    log("Step 5/5  Building the map")
    M = L[L.lat.notna()].copy()
    idx = np.array([ki[k] for k in M.index])
    order = np.argsort(M.avg_boe_per_prod_month_per_well.values)          # draw low values first
    M, idx = M.iloc[order], idx[order]
    ops = sorted(M.OPERATOR_NAME.fillna("").unique()); opi = {o: i for i, o in enumerate(ops)}
    flds = sorted(M.FIELD_NAME.fillna("").unique()); fli = {f: i for i, f in enumerate(flds)}
    cnts = sorted(M.county.unique()); ci = {c: i for i, c in enumerate(cnts)}
    li = {k: j for j, k in enumerate(M.index)}
    wl = w.dropna(subset=["LAT83"]).assign(li=lambda x: x.key.map(li)).dropna(subset=["li"]).sort_values("li")
    woff = np.zeros(len(M) + 1, dtype=np.int64)
    np.add.at(woff, wl.li.astype(int).values + 1, 1)
    woff = np.cumsum(woff)
    lat_mask = wl.reach.fillna(0).values > 150
    LOGQ = 2900.0
    blocks = [("lat", np.round(M.lat.values * 1e5).astype(np.int32)), ("lon", np.round(M.lon.values * 1e5).astype(np.int32)),
              ("ser", np.round(np.log1p(B[idx]) * LOGQ).clip(0, 65535).astype(np.uint16)),
              ("woff", woff.astype(np.int32)),
              ("wlat", np.round(wl.LAT83.values * 1e5).astype(np.int32)), ("wlon", np.round(wl.LONG83.values * 1e5).astype(np.int32)),
              ("lati", np.nonzero(lat_mask)[0].astype(np.int32)),
              ("latdy", np.round((wl.SLAT.values[lat_mask] - wl.LAT83.values[lat_mask]) * 1e5).clip(-32767, 32767).astype(np.int16)),
              ("latdx", np.round((wl.SLON.values[lat_mask] - wl.LONG83.values[lat_mask]) * 1e5).clip(-32767, 32767).astype(np.int16))]
    layout, parts, off = {}, [], 0
    for name, arr in blocks:
        pad = (-off) % 8
        parts.append(b"\0" * pad); off += pad
        layout[name] = [off, str(arr.dtype), int(arr.size)]
        by = np.ascontiguousarray(arr).tobytes(); parts.append(by); off += len(by)
    meta = dict(
        months=mlab, days=days.tolist(), total_days=tdays, n=len(M), logq=LOGQ, layout=layout,
        ops=ops, fields=flds, counties=cnts, top_oil=list(op_oil.index[:20]), top_gas=list(op_gas.index[:20]),
        id=[f"{k[0]}-{k[1:3]}-{k[3:]}" for k in M.index], name=M.LEASE_NAME.fillna("").str.strip().tolist(),
        t="".join(M.type.values), op=[opi[o] for o in M.OPERATOR_NAME.fillna("")], fld=[fli[f] for f in M.FIELD_NAME.fillna("")],
        cty=[ci[c] for c in M.county], wells=M.wells.tolist(), act=M.active_wells.tolist(), on=M.months_on.tolist(),
        oil=np.round(M.oil_bbl.values).astype(int).tolist(), cond=np.round(M.cond_bbl.values).astype(int).tolist(),
        gas=np.round(M.gas_mcf.values).astype(int).tolist(), wapi="".join(wl.API.values),
        state_monthly_oil=np.round(O.sum(0) + C.sum(0)).tolist(), state_monthly_gas=np.round(G.sum(0)).tolist(),
        generated=date.today().isoformat())
    html = open(TEMPLATE, encoding="utf-8").read()
    for slot, payload in (("inline-meta", b64gz(json.dumps(meta, separators=(",", ":")).encode())),
                          ("inline-arrays", b64gz(b"".join(parts))),
                          ("inline-geo", open(GEO, encoding="utf-8").read())):
        kind = "application/json" if slot == "inline-geo" else "application/octet-stream"
        empty = f'<script id="{slot}" type="{kind}"></script>'
        assert empty in html, slot
        html = html.replace(empty, f'<script id="{slot}" type="{kind}">{payload}</script>', 1)
    with open(os.path.join(out_dir, "map.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"Done: {len(M):,} leases mapped -> {os.path.abspath(out_dir)}")
    log(f"  top oil operator: {op_oil.index[0]}; top gas operator: {op_gas.index[0]}")


MONTHS = 24


def main() -> None:
    global MONTHS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=24, help="number of most recent published months (default 24)")
    ap.add_argument("--start", help="first month YYYY-MM (overrides --months)")
    ap.add_argument("--end", help="last month YYYY-MM (default: newest month the RRC has published)")
    ap.add_argument("--out", help="output folder (default output/<start>_<end>)")
    ap.add_argument("--cache", default=os.path.join(HERE, "data_cache"), help="where downloads are kept (default ./data_cache)")
    a = ap.parse_args()
    MONTHS = a.months
    ym = lambda s: s.replace("-", "") if s else None
    build(ym(a.start), ym(a.end), a.out, a.cache)


if __name__ == "__main__":
    main()
