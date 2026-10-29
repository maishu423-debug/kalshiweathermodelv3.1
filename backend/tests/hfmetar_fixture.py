"""Builds small gzip'd MADIS-HFMETAR-style netCDF3 files for tests (no network, no NOAA)."""
import gzip
import io
from datetime import datetime, timezone

import numpy as np
from scipy.io import netcdf_file

DD_VARS = ["temperatureDD", "dewpointDD", "altimeterDD", "windDirDD", "windSpeedDD", "windGustDD", "visibilityDD"]


def kelvin(temp_f):
    return (temp_f - 32.0) * 5.0 / 9.0 + 273.15


def row(when, temp_f, station="KMIA", dew_f=None, wind_dir=90.0, wind_mph=6.0, pressure_hpa=1015.0,
        sky=("FEW",), dd="V"):
    """One observation. `when` is a UTC datetime (or epoch seconds)."""
    t = when.timestamp() if isinstance(when, datetime) else float(when)
    return {
        "station": station, "t": t,
        "temp_k": None if temp_f is None else kelvin(temp_f),
        "dew_k": kelvin(temp_f - 8.0 if dew_f is None else dew_f) if temp_f is not None else None,
        "wind_dir": wind_dir, "wind_ms": wind_mph / 2.23694, "gust_ms": None,
        "altimeter_pa": pressure_hpa * 100.0, "visibility_m": 16093.0,
        "sky": list(sky), "dd": dd,
    }


class _KeepOpen(io.BytesIO):
    """scipy closes the file it writes to; we still need the bytes afterwards."""

    _done = False

    def close(self):
        self._done = True          # the bytes stay readable; scipy must not flush a second time

    @property
    def closed(self):
        return self._done


def _chars(strings, width):
    data = b"".join(s.encode("ascii")[:width].ljust(width, b"\0") for s in strings)
    return np.frombuffer(data, dtype="S1").reshape(len(strings), width)


def build_netcdf(rows):
    """netCDF3 bytes (uncompressed) holding `rows` in the layout asos_client.decode_hfmetar_ex reads."""
    n = len(rows)
    buf = _KeepOpen()
    nc = netcdf_file(buf, "w", mmap=False)
    nc.createDimension("recNum", n if n else None)
    nc.createDimension("staLen", 6)
    nc.createDimension("skyN", 2)
    nc.createDimension("skyLen", 8)
    nc.createDimension("wxLen", 8)

    def num(name, values):
        var = nc.createVariable(name, "d", ("recNum",))
        if n:
            var[:] = np.array([1e38 if v is None else v for v in values], dtype="d")

    ids = nc.createVariable("stationId", "c", ("recNum", "staLen"))
    if n:
        ids[:] = _chars([r["station"] for r in rows], 6)
    num("observationTime", [r["t"] for r in rows])
    num("temperature", [r["temp_k"] for r in rows])
    num("dewpoint", [r["dew_k"] for r in rows])
    num("windDir", [r["wind_dir"] for r in rows])
    num("windSpeed", [r["wind_ms"] for r in rows])
    num("windGust", [r["gust_ms"] for r in rows])
    num("altimeter", [r["altimeter_pa"] for r in rows])
    num("visibility", [r["visibility_m"] for r in rows])
    for name in DD_VARS:
        var = nc.createVariable(name, "c", ("recNum",))
        if n:
            var[:] = np.frombuffer(b"".join(r["dd"].encode("ascii") for r in rows), dtype="S1")
    sky = nc.createVariable("skyCvr", "c", ("recNum", "skyN", "skyLen"))
    if n:
        layers = np.zeros((n, 2, 8), dtype="S1")
        for i, r in enumerate(rows):
            for j, cover in enumerate((r["sky"] + ["", ""])[:2]):
                layers[i, j, :] = _chars([cover], 8)[0]
        sky[:] = layers
    wx = nc.createVariable("presWx", "c", ("recNum", "wxLen"))
    if n:
        wx[:] = _chars([""] * n, 8)
    nc.close()
    return buf.getvalue()


def build_gz(rows):
    return gzip.compress(build_netcdf(rows))
