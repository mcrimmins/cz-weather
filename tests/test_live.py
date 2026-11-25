"""Live checks against the real Open-Meteo API. Run with:  pytest --live -s

These answer the open questions the offline tests can't: whether each variable
name is accepted, which archive model is used, and whether every screen renders
from real data. Rendered screens are written to ./out_live/ for a visual check.
"""
import os

import pytest

import cz_weather as cz
from conftest import TUCSON

pytestmark = pytest.mark.live
OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "out_live")


# A persistent cache so reruns don't re-download the 30-year climatology (~700 API calls each
# time against a free limit of 10,000/day). Delete the .cache_live folder to force a fresh download.
LIVE_CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache_live")


@pytest.fixture(scope="module")
def live_bundle():
    cz.CACHE_DIR = LIVE_CACHE
    cz.SLEEP = lambda s: (print(f"\n[rate limited: waiting {s} s]"), __import__("time").sleep(s))
    data = cz.fetch_forecast(TUCSON["lat"], TUCSON["lon"], False)
    clim = cz.get_climatology(TUCSON, False, print)
    rec = cz.get_recent(TUCSON, False, clim["extras"], clim["model"])
    upper = cz.get_upper(TUCSON)
    return {"data": data, "clim": clim, "rec": rec, "upper": upper}


def test_forecast_fields(live_bundle):
    d = live_bundle["data"]
    for k in ("temperature_2m", "dew_point_2m", "wind_gusts_10m", "pressure_msl"):
        assert d["current"].get(k) is not None, k
    assert len(d["daily"]["time"]) == 16


def test_archive_model_and_extras(live_bundle):
    clim = live_bundle["clim"]
    print(f"\narchive model: {clim['model']}")
    print(f"climatology variables kept: {clim['extras']}")
    missing = sorted(set(cz.EXTRA_DAILY) - set(clim["extras"]))
    print(f"not available: {missing}")
    assert clim["model"] in cz.CLIM_MODELS


def test_recent_reaches_today(live_bundle):
    rec = live_bundle["rec"]
    print(f"\nlast reanalysis day: {rec['last_obs']}, today: {rec['today']}")
    assert rec["last_obs"] is not None
    assert rec["days"][-1]["src"] == "fc"


def test_upper_optional_variables(live_bundle):
    h = live_bundle["upper"]["hourly"]
    kept = [v for v in cz.UPPER_OPTIONAL if v in h]
    print(f"\noptional hourly variables accepted: {kept}")
    assert "temperature_500hPa" in h


def test_render_all_live(live_bundle):
    os.makedirs(OUT, exist_ok=True)
    b = live_bundle
    for i, (name, _, _) in enumerate(cz.SCREENS):
        img = cz.render(b["data"], TUCSON, i, clim=b["clim"], rec=b["rec"], upper=b["upper"])
        img.save(os.path.join(OUT, f"{i:02d}_{name.lower().replace(' ', '_')}.png"))
    print(f"\nwrote {len(cz.SCREENS)} screens to {OUT}")


def test_past_dates_live(live_bundle):
    """A 1991-2020 date should need no download; a 2023 date downloads its years once."""
    import datetime as dt
    b, clim = live_bundle, live_bundle["clim"]
    calls = []
    real = cz.http_json
    cz.http_json = lambda url, **kw: (calls.append(url), real(url, **kw))[1]
    try:
        r2005 = cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2005, 7, 20))
        assert r2005["days"][0]["date"] == "2004-10-01"
        n2005 = len(calls)
        r2023 = cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2023, 7, 20))
    finally:
        cz.http_json = real
    print(f"\n2005-07-20: {n2005} API requests; 2023-07-20: {len(calls) - n2005} API requests")
    assert n2005 == 0
    assert r2023["view_past"] and r2023["days"][0]["date"] == "2022-10-01"
    out = os.path.join(os.path.dirname(OUT), "out_live_2023")
    os.makedirs(out, exist_ok=True)
    for i, (name, fn, _) in enumerate(cz.SCREENS):
        if getattr(fn, "needs", None) == "clim":
            img = cz.render(b["data"], TUCSON, i, clim=clim, rec=r2023, upper=b["upper"])
            img.save(os.path.join(out, f"{i:02d}_{name.lower().replace(' ', '_')}.png"))
    print(f"wrote 2023-07-20 climate screens to {out}")


def test_hourly_archive_for_past_date(live_bundle):
    """Monsoon/Fire/Sounding on a past date come from the historical-forecast archive."""
    import datetime as dt
    up = cz.get_upper(TUCSON, dt.date(2023, 7, 20))
    h = up["hourly"]
    print(f"\nhistorical hourly: {len(h['time'])} hours from {h['time'][0]}")
    missing = [k for k in ("temperature_500hPa", "dew_point_700hPa", "cape",
                           "total_column_integrated_water_vapour", "relative_humidity_2m")
               if not any(v is not None for v in h.get(k, []))]
    print(f"variables with no data: {missing or 'none'}")
    pw = [v for v in h["total_column_integrated_water_vapour"] if v is not None]
    src = "derived from pressure levels" if up.get("_pw_derived") else "from the archive"
    print(f"PW {src}: {min(pw) / 25.4:.2f}-{max(pw) / 25.4:.2f} in over the 3 days")
    assert h["time"][0].startswith("2023-07-20T00")
    assert "temperature_500hPa" not in missing
    with pytest.raises(cz.HourlyUnavailable):
        cz.get_upper(TUCSON, dt.date(2015, 7, 20))
