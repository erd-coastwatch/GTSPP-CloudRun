"""GTSPP QC and CF export, ported from Roy's supplied EDDTableFromNcFiles.java.

Keep scientific rules here separate from network, cloud storage, and deployment.
SQLite spools profiles so a month's raw archives need not live in Python memory.
"""
from __future__ import annotations

from collections import Counter
import calendar
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
import json
import re
import sqlite3
import tarfile
import tempfile

import netCDF4 as nc
import numpy as np

UTC = timezone.utc
GOOD_QC = (1, 2, 5)
TIME_UNITS = "days since 1900-01-01 00:00:00"
SOURCE_EPOCH = datetime(1900, 1, 1, tzinfo=UTC).timestamp()
META_PATH = Path(__file__).resolve().parents[1] / "config/output_metadata.json"


def month_bounds(month):
    start = datetime.strptime(month, "%Y%m").replace(tzinfo=UTC)
    end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
    middle = start.replace(day=16)
    return start.timestamp(), middle.timestamp(), end.timestamp()


def text(var):
    if var is None:
        return ""
    a = np.asarray(var[:])
    if a.dtype.kind == "S":
        value = a.tobytes().decode("utf-8", "strict")
    elif a.dtype.kind == "U":
        value = "".join(a.reshape(-1).tolist())
    else:
        raise ValueError(f"Expected a character string: {var.name}")
    return " ".join(value.replace("\x00", "").split())


def array(dataset, name, kind, size=None):
    v = dataset.variables.get(name)
    if v is None:
        raise ValueError(f"Missing required variable {name}")
    a = np.asarray(v[:]).reshape(-1)
    expected_bytes = 8 if name == "time" else 4
    if a.dtype.kind != kind or a.dtype.itemsize != expected_bytes or (size is not None and a.size != size):
        raise ValueError(f"Invalid {name}: dtype={a.dtype}, size={a.size}")
    return a


def raw_float(dataset, name, size):
    if name not in dataset.variables:
        return np.full(size, np.nan, dtype="f4")
    a = array(dataset, name, "f", size).astype("f4", copy=True)
    fill = getattr(dataset.variables[name], "_FillValue", 99999)
    a[(a == 99999) | (a == fill)] = np.nan
    return a


def clean_profile(dataset, filename, month, stats=None):
    """Return profile metadata and cleaned [depth, temperature, salinity] rows.

    Missing position/time QC is accepted with a warning, as in Roy's Java.
    Missing temperature/salinity QC masks that variable; missing depth QC fails.
    """
    stats = stats if stats is not None else Counter()
    dataset.set_auto_maskandscale(False)
    match = re.fullmatch(r"gtspp_(\d+)_.*\.nc", filename)
    if not match or int(match[1]) >= np.iinfo("i4").max:
        raise ValueError(f"Invalid station filename {filename}")
    stats["source_profiles"] += 1
    for name in ("position_quality_flag", "time_quality_flag"):
        if name not in dataset.variables:
            stats["missing_" + name] += 1
        elif int(array(dataset, name, "i", 1)[0]) not in GOOD_QC:
            stats["rejected_" + name] += 1
            return None
    tv = dataset.variables.get("time")
    if tv is None or getattr(tv, "units", None) != TIME_UNITS:
        raise ValueError("Missing time or unexpected time units")
    original_time = float(array(dataset, "time", "f", 1)[0]) * 86400 + SOURCE_EPOCH
    start, _, end = month_bounds(month)
    if not np.isfinite(original_time) or original_time < start or original_time > end:
        raise ValueError(f"Time outside source month {month}: {original_time}")
    epoch = float(np.rint(original_time))  # Java Math.rint: ties to even.
    coords = []
    for name in ("longitude", "latitude"):
        if name not in dataset.variables:
            stats["invalid_" + name] += 1
            return None
        v = dataset.variables[name]
        a = np.asarray(v[:]).reshape(-1)
        if a.dtype != np.dtype("f4") or a.size != 1 or np.isnan(a[0]):
            stats["invalid_" + name] += 1
            return None
        coords.append(float(a[0]))
    lon, lat = coords
    if not -90 <= lat <= 90:
        stats["invalid_latitude"] += 1
        return None
    if not -180 <= lon <= 180:
        stats["wrapped_longitude"] += 1
    # Math2.anglePM180 uses [-180, 180): +180 becomes -180.
    lon = (lon + 180) % 360 - 180 if np.isfinite(lon) else 0
    z = array(dataset, "z", "f").astype("f4", copy=True)
    if not z.size:
        raise ValueError("Empty depth array")
    n = z.size
    zfill = getattr(dataset.variables["z"], "_FillValue", 99999)
    qz = array(dataset, "z_variable_quality_flag", "i", n)
    good_z = np.isin(qz, GOOD_QC) & np.isfinite(z) & (z != 99999) & (z != zfill) & (z >= np.float32(-0.4)) & (z <= 10000)
    values = []
    for name, low, high in (("temperature", -4, 40), ("salinity", 0, 41)):
        a = raw_float(dataset, name, n)
        qname = name + "_quality_flag"
        if name not in dataset.variables or qname not in dataset.variables:
            q = np.full(n, 9)
            if name in dataset.variables:
                stats["missing_" + qname] += 1
        else:
            q = array(dataset, qname, "i", n)
        valid = np.isin(q, GOOD_QC) & np.isfinite(a) & (a >= low) & (a <= high)
        stats["masked_" + name] += int(np.count_nonzero(good_z & ~valid))
        a[~valid] = np.nan
        values.append(a)
    temp, salt = values
    keep = good_z & (np.isfinite(temp) | np.isfinite(salt))
    stats["rejected_depth_rows"] += int(np.count_nonzero(~good_z))
    stats["rejected_empty_rows"] += int(np.count_nonzero(good_z & ~keep))
    stats["kept_rows"] += int(np.count_nonzero(keep))
    if not keep.any() or epoch >= end:
        # Java accepts the exact next-month boundary during QC, but its export
        # time<nextMonth query excludes it from both half-month files.
        return None
    stream = text(dataset.variables.get("stream_ident"))
    if "stream_ident" not in dataset.variables:
        stats["missing_stream_ident"] += 1
    org = stream[:2].strip() if len(stream) >= 4 else ""
    typ = stream[2:4].strip() if len(stream) >= 4 else ""
    platform = text(dataset.variables.get("gtspp_platform_code"))
    cruise = text(dataset.variables.get("cruise_id"))
    if "cruise_id" not in dataset.variables:
        stats["missing_cruise_id"] += 1
    trajectory = "_".join((org, typ, platform, cruise))
    metadata = (trajectory, org, typ, platform, cruise, int(match[1]), np.float32(lon).item(), np.float32(lat).item(), epoch)
    rows = np.column_stack((z[keep], temp[keep], salt[keep])).astype("<f4")
    return metadata, rows


class ProfileStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-32768;
            CREATE TABLE profiles (trajectory TEXT, org TEXT, type TEXT,
                platform TEXT, cruise TEXT, station_id INTEGER,
                longitude REAL, latitude REAL, time REAL, n INTEGER, data BLOB);
        """)

    def add(self, profile):
        meta, rows = profile
        self.db.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*meta, len(rows), rows.tobytes()))

    def ingest(self, archive, month, stats):
        # Cloud Run's sandbox can reject NetCDF/HDF5 in-memory file operations.
        # Reuse one real /tmp file, keeping filesystem permissions conventional.
        with tempfile.TemporaryDirectory(prefix="gtspp-profile-", dir=Path(archive).parent) as scratch, tarfile.open(archive, "r|gz") as tar:
            profile_path = Path(scratch) / "profile.nc"
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile() or not member.name.endswith(".nc"):
                    raise ValueError(f"Unexpected archive entry: {member.name}")
                if member.size > 32 * 1024 * 1024:
                    raise ValueError(f"Oversized source profile: {member.name}")
                with tar.extractfile(member) as source, profile_path.open("wb") as target:
                    target.write(source.read())
                try:
                    with nc.Dataset(profile_path, "r") as ds:
                        profile = clean_profile(ds, Path(member.name).name, month, stats)
                finally:
                    profile_path.unlink(missing_ok=True)
                if profile is not None:
                    self.add(profile)
                if stats["source_profiles"] % 10000 == 0:
                    self.db.commit()
                    print(f"PROGRESS {month}: {stats['source_profiles']} profiles, {stats['kept_rows']} kept rows", flush=True)
        self.db.commit()

    def prepare(self):
        self.db.execute("CREATE INDEX profile_sort ON profiles (trajectory, station_id, time, longitude, latitude)")
        self.db.commit()

    def counts(self, start, end):
        nprofiles, nobs = self.db.execute("""SELECT COUNT(*), SUM(n) FROM (
            SELECT SUM(n) AS n FROM profiles WHERE time>=? AND time<?
            GROUP BY trajectory, station_id, time, longitude, latitude)""", (start, end)).fetchone()
        return nprofiles, nobs or 0

    def grouped(self, start, end):
        rows = self.db.execute("""SELECT * FROM profiles WHERE time>=? AND time<?
            ORDER BY trajectory, station_id, time, longitude, latitude, rowid""", (start, end))
        key = lambda r: (r[0], r[5], r[8], r[6], r[7])
        for _, group in groupby(rows, key):
            group = list(group)
            data = np.concatenate([np.frombuffer(r[10], dtype="<f4").reshape(-1, 3) for r in group])
            data = data[np.argsort(data[:, 0], kind="stable")]
            yield group[0][:9], data

    def close(self):
        self.db.close()


def write_cf(store, month, half, path, source_names):
    start, middle, end = month_bounds(month)
    left, right = (start, middle) if half == "a" else (middle, end)
    nprofile, nobs = store.counts(left, right)
    if not nprofile or not nobs:
        raise ValueError(f"No valid data for {month}{half}; refusing an empty replacement")
    trajectories = store.db.execute("""SELECT DISTINCT trajectory,org,type,platform,cruise
        FROM profiles WHERE time>=? AND time<? ORDER BY trajectory""", (left, right)).fetchall()
    by_id = {r[0]: i for i, r in enumerate(trajectories)}
    metadata = json.loads(META_PATH.read_text())
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    ga = metadata["global"]
    for name in list(ga):
        if name.startswith("geospatial_") or name.startswith("time_coverage_") or name in ("date_created", "date_modified", "history", "subsetVariables"):
            ga.pop(name)
    ga.update(history=f"{now} GTSPP best-copy archives: {', '.join(source_names)}\n{now} Ingest, QC, and CF export at ERD; filtering ported from Roy's EDDTableFromNcFiles.java.", date_created=now, date_modified=now)
    for name in ("acknowledgment", "license"):
        if name in ga:
            ga[name] = re.sub(r"(on )\d{4}-\d{2}-\d{2}( from https://www.nodc.noaa.gov/GTSPP/)", r"\g<1>" + now[:10] + r"\g<2>", ga[name])
    if "summary" in ga:
        ga["summary"] = re.sub(r"up to and including the \w+ \d{4} data", f"up to and including the {calendar.month_name[int(month[4:])]} {month[:4]} data", ga["summary"])
    with nc.Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("trajectory", len(trajectories))
        ds.createDimension("profile", nprofile)
        ds.createDimension("obs", nobs)
        ds.setncatts(ga)
        for col, name in enumerate(("trajectory", "org", "type", "platform", "cruise")):
            encoded = [r[col].encode("utf-8") for r in trajectories]
            width = max(1, max(map(len, encoded)))
            ds.createDimension(name + "_strlen", width)
            var = ds.createVariable(name, "S1", ("trajectory", name + "_strlen"))
            var[:] = np.asarray(encoded, dtype=f"S{width}").view("S1").reshape(len(encoded), width)
            var.setncatts(metadata["variables"][name])
        dims = {"station_id": ("i4", "profile"), "longitude": ("f4", "profile"), "latitude": ("f4", "profile"), "time": ("f8", "profile"), "trajectoryIndex": ("i4", "profile"), "rowSize": ("i4", "profile"), "depth": ("f4", "obs"), "temperature": ("f4", "obs"), "salinity": ("f4", "obs")}
        for name, (dtype, dim) in dims.items():
            atts = metadata["variables"][name].copy()
            fill = atts.pop("_FillValue", None)
            if isinstance(fill, str) and fill == "NaN":
                fill = np.nan
            var = ds.createVariable(name, dtype, (dim,), fill_value=fill if fill is not None else False)
            atts.pop("actual_range", None)
            if atts.get("missing_value") == "NaN":
                atts["missing_value"] = np.nan
            var.setncatts(atts)
        p = o = 0
        batch = []
        def flush(batch, p, o):
            size = len(batch)
            obs = np.concatenate([data for _, data in batch])
            for col, name in ((5, "station_id"), (6, "longitude"), (7, "latitude"), (8, "time")):
                ds[name][p:p+size] = [meta[col] for meta, _ in batch]
            ds["trajectoryIndex"][p:p+size] = [by_id[meta[0]] for meta, _ in batch]
            ds["rowSize"][p:p+size] = [len(data) for _, data in batch]
            for col, name in enumerate(("depth", "temperature", "salinity")):
                ds[name][o:o+len(obs)] = obs[:, col]
            return p + size, o + len(obs)
        for profile in store.grouped(left, right):
            batch.append(profile)
            if len(batch) == 512:
                p, o = flush(batch, p, o)
                batch.clear()
        if batch:
            p, o = flush(batch, p, o)
        if (p, o) != (nprofile, nobs):
            raise ValueError("Export count mismatch")
        for name in ("longitude", "latitude", "time", "depth", "temperature", "salinity"):
            a = ds[name][:]
            finite = np.isfinite(a)
            if finite.any():
                ds[name].actual_range = np.asarray([a[finite].min(), a[finite].max()], dtype=a.dtype)
        ds.time_coverage_start = datetime.fromtimestamp(float(ds["time"][:].min()), UTC).isoformat().replace("+00:00", "Z")
        ds.time_coverage_end = datetime.fromtimestamp(float(ds["time"][:].max()), UTC).isoformat().replace("+00:00", "Z")
        ds.geospatial_lon_min = float(ds["longitude"][:].min())
        ds.geospatial_lon_max = float(ds["longitude"][:].max())
        ds.geospatial_lat_min = float(ds["latitude"][:].min())
        ds.geospatial_lat_max = float(ds["latitude"][:].max())
    return validate_cf(path, month, half)


def validate_cf(path, month, half):
    with nc.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        required = ("trajectory", "org", "type", "platform", "cruise", "station_id", "longitude", "latitude", "time", "trajectoryIndex", "rowSize", "depth", "temperature", "salinity")
        if any(k not in ds.variables for k in required):
            raise ValueError("Missing CF variables")
        rows = ds["rowSize"][:]
        indices = ds["trajectoryIndex"][:]
        nobs = len(ds.dimensions["obs"])
        if not np.all(rows > 0) or int(rows.sum(dtype="i8")) != nobs:
            raise ValueError("Invalid ragged profile row counts")
        if not np.all((indices >= 0) & (indices < len(ds.dimensions["trajectory"]))):
            raise ValueError("Invalid trajectory references")
        start, middle, end = month_bounds(month)
        lo, hi = (start, middle) if half == "a" else (middle, end)
        t = ds["time"][:]
        if not np.all(np.isfinite(t) & (t >= lo) & (t < hi)):
            raise ValueError("Time outside requested half-month")
        for name, lo, hi in (("longitude", -180, 180), ("latitude", -90, 90), ("depth", -0.4, 10000), ("temperature", -4, 40), ("salinity", 0, 41)):
            a = ds[name][:]
            good = np.isfinite(a)
            if name in ("longitude", "latitude", "depth") and not good.all():
                raise ValueError(f"Missing {name}")
            if np.any((a[good] < np.float32(lo)) | (a[good] > hi)):
                raise ValueError(f"Out-of-range {name}")
        if np.any(~np.isfinite(ds["temperature"][:]) & ~np.isfinite(ds["salinity"][:])):
            raise ValueError("Observation has neither valid temperature nor salinity")
        return {"profiles": len(rows), "observations": nobs, "trajectories": len(ds.dimensions["trajectory"])}
