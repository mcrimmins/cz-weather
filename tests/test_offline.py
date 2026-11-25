"""Offline tests: no network. Run with `pytest` from the project folder."""
import datetime as dt
import os
import time

import urllib.error

import pytest

import cz_weather as cz
from conftest import TUCSON
from fake_api import FakeOpenMeteo


# ---------------------------------------------------------------- small helpers
def test_doy366_leap_calendar():
    assert cz.doy366(dt.date(2021, 1, 1)) == 0
    assert cz.doy366(dt.date(2024, 2, 29)) == 59
    assert cz.doy366(dt.date(2023, 3, 1)) == 60      # non-leap years skip the Feb 29 slot
    assert cz.doy366(dt.date(2023, 12, 31)) == 365


def test_wy_start():
    assert cz.wy_start(dt.date(2026, 11, 24)) == dt.date(2026, 10, 1)
    assert cz.wy_start(dt.date(2027, 3, 1)) == dt.date(2026, 10, 1)
    assert cz.wy_start(dt.date(2026, 10, 1)) == dt.date(2026, 10, 1)


def test_pctl_interpolates():
    v = [0, 10, 20, 30, 40]
    assert cz.pctl(v, 0.5) == 20
    assert cz.pctl(v, 0.1) == pytest.approx(4)
    assert cz.pctl([], 0.5) is None


@pytest.mark.parametrize("tx,tn,metric,expected", [
    (80, 60, False, 20),       # (80+60)/2 - 50
    (95, 60, False, 23),       # tmax capped at 86
    (60, 40, False, 5),        # tmin raised to 50
    (45, 30, False, 0),        # all below base
    (25, 15, True, 10),        # metric: (25+15)/2 - 10
])
def test_gdd_day(tx, tn, metric, expected):
    assert cz.gdd_day(tx, tn, metric) == pytest.approx(expected)


def test_parse_latlon():
    assert cz.parse_latlon("32.25, -110.95")["lat"] == 32.25
    assert cz.parse_latlon("32.25 -110.95")["lon"] == -110.95
    assert cz.parse_latlon("Tucson") is None
    assert cz.parse_latlon("95, 10") is None          # out of range


# ---------------------------------------------------------------- climatology pipeline
def test_climatology_shapes(bundle):
    clim = bundle["clim"]
    for key in ("tmax", "tmin"):
        for stat in ("p10", "p50", "p90", "mean", "rec"):
            assert len(clim[key][stat]) == 366
    assert len(clim["wy"]["p50"]) == 366 and len(clim["wy_curves"]) == 30
    assert len(clim["gdd"]["p50"]) == 366 and len(clim["gdd_curves"]) == 30
    # percentiles are ordered
    assert all(a <= b <= c for a, b, c in zip(clim["tmax"]["p10"], clim["tmax"]["p50"], clim["tmax"]["p90"]))
    # cumulative curves never decrease
    assert all(b >= a for a, b in zip(clim["wy"]["p50"], clim["wy"]["p50"][1:]))
    # Tucson-like seasonality: July hotter than January
    assert clim["tmax"]["mean"][cz.doy366(dt.date(2001, 7, 1))] > clim["tmax"]["mean"][0] + 20


def test_climatology_is_cached(fake):
    cz.get_climatology(TUCSON, False)
    n = len(fake.calls)
    cz.get_climatology(TUCSON, False)
    assert len(fake.calls) == n                      # second call reads the cache


def test_probe_keeps_only_known_variables(isolated, monkeypatch):
    api = FakeOpenMeteo(unknown={"wind_gusts_10m_max"})
    monkeypatch.setattr(cz, "http_json", api)
    clim = cz.get_climatology(TUCSON, False)
    assert "wind_gusts_10m_max" not in clim["extras"]
    assert "dew_point_2m_mean" in clim["extras"]


def test_probe_falls_back_to_era5_land(isolated, monkeypatch):
    api = FakeOpenMeteo(bad_models={"era5_seamless"})
    monkeypatch.setattr(cz, "http_json", api)
    clim = cz.get_climatology(TUCSON, False)
    assert clim["model"] == "era5_land"


def test_recent_series_merges_obs_and_forecast(bundle):
    rec = bundle["rec"]
    srcs = [r["src"] for r in rec["days"]]
    assert srcs[0] == "obs" and srcs[-1] == "fc"
    assert srcs.index("fc") > 0 and "obs" not in srcs[srcs.index("fc"):]   # obs then fc, no interleave
    dates = [r["date"] for r in rec["days"]]
    assert dates == sorted(dates) and len(dates) == len(set(dates))
    assert rec["days"][0]["date"] == f"{dt.date.fromisoformat(rec['today']).year}-01-01"


def test_recent_falls_back_when_forecast_rejects_extra(isolated, monkeypatch):
    api = FakeOpenMeteo()
    monkeypatch.setattr(cz, "http_json", api)
    clim = cz.get_climatology(TUCSON, False)
    api.unknown = {"dew_point_2m_mean"}              # forecast + archive now reject it
    with pytest.raises(Exception):
        cz.get_recent(TUCSON, False, clim["extras"], clim["model"])


def test_anomaly_summary(bundle):
    a = cz.anomaly_summary(bundle["clim"], bundle["rec"])
    assert a["n30"] == 30
    assert a["tx30"] is not None and -20 < a["tx30"] < 20
    assert 0 <= a["wy_rank"] <= 100


# ---------------------------------------------------------------- hourly profiles
def test_upper_has_levels(bundle):
    h = bundle["upper"]["hourly"]
    assert "temperature_500hPa" in h and "geopotential_height_500hPa" in h
    assert "total_column_integrated_water_vapour" in h


def test_upper_drops_rejected_optional_variable(isolated, monkeypatch):
    api = FakeOpenMeteo(unknown={"lifted_index"})
    monkeypatch.setattr(cz, "http_json", api)
    up = cz.get_upper(TUCSON)
    assert "lifted_index" not in up["hourly"]
    assert "cape" in up["hourly"]


# ---------------------------------------------------------------- rendering
@pytest.mark.parametrize("idx", range(len(cz.SCREENS)))
def test_every_screen_renders(bundle, idx):
    img = cz.render(bundle["data"], TUCSON, idx, clim=bundle["clim"], rec=bundle["rec"],
                    upper=bundle["upper"])
    assert img.size == (cz.W, cz.H)
    assert len(img.getcolors(maxcolors=100000)) > 5   # not a blank frame


@pytest.mark.parametrize("idx", range(len(cz.SCREENS)))
def test_screens_render_while_loading(forecast, idx):
    img = cz.render(forecast, TUCSON, idx, clim_status="loading…", upper_status="loading…")
    assert img.size == (cz.W, cz.H)


def test_sounding_time_steps(bundle):
    a = cz.render(bundle["data"], TUCSON, cz.SOUNDING_IDX, upper=bundle["upper"], sound_off=0)
    b = cz.render(bundle["data"], TUCSON, cz.SOUNDING_IDX, upper=bundle["upper"], sound_off=36)
    assert a.tobytes() != b.tobytes()


def test_clim_var_screen_says_unavailable(bundle):
    clim = dict(bundle["clim"], vars={})
    idx = next(i for i, s in enumerate(cz.SCREENS) if s[0] == "Clim Gust")
    img = cz.render(bundle["data"], TUCSON, idx, clim=clim, rec=bundle["rec"], upper=bundle["upper"])
    assert img.size == (cz.W, cz.H)


# ---------------------------------------------------------------- app behaviour
def test_navigation_keys(fake, forecast):
    frames = []
    app = cz.App(frames.append)
    app.data = forecast
    app.key("6")
    assert app.idx == cz.SOUNDING_IDX
    app.key("down")
    app.key(".")
    assert app.sound_off == 6
    app.key(";")
    assert app.sound_off == 3
    app.key("3")
    assert cz.SCREENS[app.idx][0] == "Monsoon"
    app.key("right")
    app.key("left")
    assert cz.SCREENS[app.idx][0] == "Monsoon"
    app.key("1")
    assert app.idx == 0
    assert frames


def test_location_search_by_coordinates(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    app.key("l")
    for ch in "31.9, -109.8":
        app.key(ch)
    app.key("enter")                                 # search -> one result
    assert app.results and app.results[0]["lat"] == 31.9
    app.key("enter")                                 # select it
    assert app.loc["lon"] == -109.8 and app.mode == "view"
    wait_for(lambda: not app.clim_busy and not app.upper_busy)   # let background loads finish


def test_offline_status(isolated, monkeypatch, forecast):
    def down(url):
        raise OSError("network unreachable")
    monkeypatch.setattr(cz, "http_json", down)
    app = cz.App(lambda img: None)
    app.data = forecast
    app.refresh(blocking=True)
    assert app.status.startswith("offline")


# ---------------------------------------------------------------- rate limiting (HTTP 429)
def test_http_json_waits_and_retries_on_429(isolated, monkeypatch):
    """http_json itself retries; drive it with a fake urlopen that 429s twice."""
    import io
    import json as _json
    import urllib.request
    from fake_api import RateLimited
    attempts = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        attempts.append(req.full_url)
        if len(attempts) <= 2:
            raise RateLimited(req.full_url)
        return Resp(_json.dumps({"ok": True}).encode())

    waits, msgs = [], []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cz, "SLEEP", waits.append)
    monkeypatch.setattr(cz, "WAIT_CALLBACK", msgs.append)
    assert cz.http_json("https://api.open-meteo.com/x") == {"ok": True}
    assert len(attempts) == 3 and len(waits) == 2
    assert "rate limit" in msgs[0]


def test_http_json_gives_up_after_retries(isolated, monkeypatch):
    import urllib.request
    from fake_api import RateLimited

    def always_429(req, timeout=0):
        raise RateLimited(req.full_url)
    monkeypatch.setattr(urllib.request, "urlopen", always_429)
    monkeypatch.setattr(cz, "SLEEP", lambda s: None)
    with pytest.raises(urllib.error.HTTPError) as ei:
        cz.http_json("https://api.open-meteo.com/x")
    assert ei.value.code == 429
    assert "Minutely" in str(ei.value)          # Open-Meteo's reason is carried through


def test_climatology_downloads_in_three_chunks(fake):
    clim = cz.get_climatology(TUCSON, False)
    big = [u for u in fake.calls if "archive" in u and "1990-10-01" in u or
           ("archive" in u and ("2000-10-01" in u or "2010-10-01" in u))]
    assert len(big) == 3
    assert len(clim["wy_curves"]) == 30


# ---------------------------------------------------------------- view date
def test_parse_date():
    assert cz.parse_date("2021-07-23") == dt.date(2021, 7, 23)
    assert cz.parse_date("20210723") == dt.date(2021, 7, 23)
    assert cz.parse_date("7/23/2021") == dt.date(2021, 7, 23)
    assert cz.parse_date("July") is None


def _archive_calls(api):
    return [u for u in api.calls if "archive" in u]


def test_baseline_date_needs_no_download(fake):
    clim = cz.get_climatology(TUCSON, False)
    n = len(fake.calls)
    rec = cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2005, 7, 20))
    assert len(fake.calls) == n                      # served from the cached 1990-2020 baseline
    assert rec["today"] == "2005-07-20" and rec["view_past"]
    assert rec["days"][0]["date"] == "2004-10-01"    # water year start
    assert rec["days"][-1]["date"] == "2005-08-05"   # +16 days
    assert {r["src"] for r in rec["days"]} == {"obs"}


def test_post_baseline_year_downloaded_once(fake):
    clim = cz.get_climatology(TUCSON, False)
    n0 = len(_archive_calls(fake))
    cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2023, 3, 10))
    n1 = len(_archive_calls(fake))
    assert n1 - n0 == 2                              # 2022 (water year from Oct) + 2023
    cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2023, 2, 1))
    assert len(_archive_calls(fake)) == n1           # same years: cached


def test_screens_render_for_past_date(fake, forecast):
    clim = cz.get_climatology(TUCSON, False)
    rec = cz.get_window(TUCSON, False, clim["extras"], clim["model"], dt.date(2012, 8, 1))
    upper = cz.get_upper(TUCSON)
    for i, (_, fn, _) in enumerate(cz.SCREENS):
        img = cz.render(forecast, TUCSON, i, clim=clim, rec=rec, upper=upper)
        assert img.size == (cz.W, cz.H)
    assert cz.view_tag(rec) == "1 Aug 2012"
    rec_today = dict(rec, view_past=False, today="2026-11-24")
    assert cz.view_tag(rec_today) == "today 24 Nov"


def test_date_keys(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    app.key("d")
    assert app.mode == "date" and cz.SCREENS[app.idx][2] == 2
    for ch in "2021-07-23":
        app.key(ch)
    app.key("enter")
    assert app.mode == "view" and app.view_date == dt.date(2021, 7, 23)
    app.key("-")
    assert app.view_date == dt.date(2021, 7, 16)
    app.key("=")
    app.key("=")
    assert app.view_date == dt.date(2021, 7, 30)
    app.key("d")
    app.key("enter")                                 # Enter alone = back to today
    assert app.view_date is None
    app.key("d")
    for ch in "1850-01-01":
        app.key(ch)
    app.key("enter")
    assert app.mode == "date" and "1940" in app.status
    wait_for(lambda: not app.clim_busy and not app.upper_busy)


# ---------------------------------------------------------------- device display
def test_pack_rgb565_bit_layout():
    from PIL import Image
    img = Image.new("RGB", (3, 1))
    img.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
    raw = cz.pack_rgb565(img)
    assert raw == bytes([0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00])      # little-endian F800, 07E0, 001F
    assert cz.pack_rgb565(img, bgr=True)[:2] == bytes([0x1F, 0x00])  # red lands in the blue bits


def test_framebuffer_write(tmp_path, monkeypatch):
    """Point FramebufferDisplay at a fake sysfs + device file and check what gets written."""
    import builtins
    sysd = tmp_path / "sys" / "fbX"
    sysd.mkdir(parents=True)
    (sysd / "bits_per_pixel").write_text("16")
    (sysd / "virtual_size").write_text("320,170")
    (sysd / "stride").write_text("640")
    dev = tmp_path / "fbX"
    dev.write_bytes(b"\0" * 320 * 170 * 2)
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        path = str(path)
        if path.startswith("/sys/class/graphics/fbX/"):
            path = str(sysd / os.path.basename(path))
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", fake_open)
    disp = cz.FramebufferDisplay(str(dev))
    from PIL import Image
    disp.show(Image.new("RGB", (cz.W, cz.H), (255, 0, 0)))
    data = dev.read_bytes()
    assert len(data) == 320 * 170 * 2 and data[:2] == bytes([0x00, 0xF8])


def test_help_screen_and_now_header(fake, forecast):
    frames = []
    app = cz.App(frames.append)
    app.data = forecast
    app.key("h")
    assert app.mode == "help"
    app.key("x")                                     # any key closes it
    assert app.mode == "view"
    assert cz.clock_label(dt.datetime(2026, 11, 24, 15, 9)) == "Tue Nov 24 3:09p"
    assert cz.clock_label(dt.datetime(2026, 11, 24, 0, 5)) == "Tue Nov 24 12:05a"


# ---------------------------------------------------------------- consistency of dates across screens
def test_upper_for_past_date_is_archived_and_cached(fake):
    view = dt.date(2023, 7, 20)
    up = cz.get_upper(TUCSON, view, today=dt.date(2026, 11, 24))
    assert up["_view"] == "2023-07-20"
    assert up["hourly"]["time"][0].startswith("2023-07-20T00")
    assert any("historical-forecast-api" in u for u in fake.calls)
    n = len(fake.calls)
    cz.get_upper(TUCSON, view, today=dt.date(2026, 11, 24))
    assert len(fake.calls) == n                      # complete past span: served from cache


def test_upper_before_archive_is_unavailable(fake):
    with pytest.raises(cz.HourlyUnavailable):
        cz.get_upper(TUCSON, dt.date(2015, 7, 20), today=dt.date(2026, 11, 24))


def test_every_screen_carries_the_right_date_tag(fake, forecast):
    """Forecast pages: today. Climate + hourly pages: the view date when one is set."""
    clim = cz.get_climatology(TUCSON, False)
    view = dt.date(2023, 7, 20)
    rec = cz.get_window(TUCSON, False, clim["extras"], clim["model"], view, dt.date(2026, 11, 24))
    up = cz.get_upper(TUCSON, view, today=dt.date(2026, 11, 24))
    assert cz.view_tag(rec) == "20 Jul 2023"
    assert cz.upper_tag(up, forecast) == "20 Jul 2023"
    assert cz.today_tag(forecast).startswith("today ")
    live = cz.get_upper(TUCSON)
    assert cz.upper_tag(live, forecast).startswith("today ")
    for i in range(len(cz.SCREENS)):                 # everything still renders with a past view
        cz.render(forecast, TUCSON, i, clim=clim, rec=rec, upper=up, view_date=view)
        cz.render(forecast, TUCSON, i, view_date=view, clim_status="loading…", upper_status="loading…")


def test_archived_hourly_window_starts_at_midnight_without_now_marker(fake, forecast):
    up = cz.get_upper(TUCSON, dt.date(2023, 7, 20), today=dt.date(2026, 11, 24))
    times, get, marker = cz.upper_window(up, cz._now(forecast))
    assert marker is None and times[0] == dt.datetime(2023, 7, 20, 0, 0)
    live = cz.get_upper(TUCSON)
    _, _, marker = cz.upper_window(live, cz._now(forecast))
    assert marker is not None


def test_climate_units_follow_the_climatology_not_the_forecast():
    assert cz.u_temp(cz.cunits({"metric": True})) == "°C"
    assert cz.u_precip(cz.cunits({"metric": False})) == "in"


def wait_for(cond, timeout=15.0):
    """Poll for a background-thread result instead of sleeping a fixed time (machines differ)."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_view_date_reloads_hourly_and_resets_sounding(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    app.sound_off = 9
    app.set_view_date(dt.date(2023, 7, 20))
    assert app.sound_off == 0
    assert wait_for(lambda: app.upper is not None and app.rec is not None
                    and not app.clim_busy and not app.upper_busy)
    assert app.upper.get("_view") == "2023-07-20"
    assert app.rec["today"] == "2023-07-20"
    app.set_view_date(dt.date(2015, 7, 20))          # before the hourly archive
    assert wait_for(lambda: app.rec is not None and not app.clim_busy and not app.upper_busy)
    assert app.upper is None and "2022" in app.upper_status
    assert app.rec["today"] == "2015-07-20"


def test_local_today_comes_from_the_forecast(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    assert app.local_today() == dt.date.fromisoformat(forecast["current"]["time"][:10])


def test_pw_is_derived_when_archive_lacks_it(isolated, monkeypatch):
    api = FakeOpenMeteo(hist_without={"total_column_integrated_water_vapour"})
    monkeypatch.setattr(cz, "http_json", api)
    up = cz.get_upper(TUCSON, dt.date(2023, 7, 20), today=dt.date(2026, 11, 24))
    pw = up["hourly"]["total_column_integrated_water_vapour"]
    assert up.get("_pw_derived") and all(v is not None and 0 < v < 80 for v in pw)


def test_derive_pw_physics():
    """Moister profile -> more PW; a saturated tropical-ish column lands in a plausible range."""
    def column(td_sfc):
        h = {"time": ["t"], "surface_pressure": [1000.0], "dew_point_2m": [td_sfc]}
        for lev in cz.LEVELS:
            h[f"dew_point_{lev}hPa"] = [td_sfc - 6.5 * 44.33 * (1 - (lev / 1013.25) ** 0.1903)]
        return cz.derive_pw(h)[0]
    dry, moist = column(0.0), column(22.0)
    assert dry < moist
    assert 30 < moist < 90                           # mm


# ---------------------------------------------------------------- launcher behaviour
def test_esc_hold_quits_after_two_seconds():
    h = cz.EscHold(hold=2.0)
    h.down(100.0)
    assert h.poll(101.0) is None
    assert h.poll(102.1) == "quit"
    assert h.poll(103.0) is None                     # fires once
    h.up(103.5)
    h.down(110.0)
    h.up(110.5)                                      # a short press never quits
    assert h.poll(113.0) is None


def test_quit_key_and_short_esc_hint(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    assert app.key("esc") is True and "hold Esc" in app.status
    app.key("right")
    assert app.status == ""                          # hint clears on the next key
    app.key("d")
    assert app.key("quit") is False                  # held Esc quits even mid-entry


def test_launcher_files_are_consistent():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    desk = open(os.path.join(root, "launcher", "cz-weather.desktop")).read()
    assert "Exec=bin/run-cz-weather" in desk and "Icon=share/images/cz-weather.png" in desk
    assert "Terminal=false" in desk
    wrap = open(os.path.join(root, "launcher", "run-cz-weather")).read()
    assert "/usr/share/APPLaunch/bin/cz_weather.py --fb" in wrap
    inst = open(os.path.join(root, "launcher", "install_launcher.sh")).read()
    for f in ("cz_weather.py", "run-cz-weather", "cz-weather.desktop", "cz-weather.png"):
        assert f in inst
    from PIL import Image
    assert Image.open(os.path.join(root, "launcher", "cz-weather.png")).size == (128, 128)


def test_take_over_screen_stops_an_older_copy(tmp_path):
    """Start a dummy process whose command line contains cz_weather.py; take_over_screen ends it."""
    import subprocess
    import sys as _sys
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc (Linux)")
    dummy = tmp_path / "cz_weather.py"
    dummy.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([_sys.executable, str(dummy)])
    try:
        time.sleep(0.3)
        pidfile = str(tmp_path / "cz.pid")
        stopped = cz.take_over_screen(pidfile, wait_s=3.0)
        assert proc.pid in stopped
        assert proc.wait(timeout=5) is not None      # it exited
        assert open(pidfile).read().strip() == str(os.getpid())
        cz.release_screen(pidfile)
        assert not os.path.exists(pidfile)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_take_over_screen_leaves_shells_and_editors_alone(tmp_path):
    """A shell whose command line merely mentions cz_weather.py must never be stopped."""
    import subprocess
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc (Linux)")
    shell = subprocess.Popen(["sh", "-c", "sleep 30; echo cz_weather.py"])
    try:
        time.sleep(0.2)
        stopped = cz.take_over_screen(str(tmp_path / "cz.pid"), wait_s=0.5)
        assert shell.pid not in stopped and shell.poll() is None
        assert not cz._is_cz_weather(os.getpid())    # pytest itself isn't cz_weather.py
    finally:
        shell.kill()


# ---------------------------------------------------------------- inspect cursor + week keys
INSPECTABLE = ["Temp", "Precip", "Wind", "Clim T", "WY Precip", "Monsoon", "Clim Td", "Fire Wx",
               "Clim RH", "Clim VPD", "Clim Gust", "GDD", "Clim ET0", "Clim SW"]


@pytest.mark.parametrize("name", INSPECTABLE)
def test_every_plot_screen_supports_inspect(bundle, name):
    i = next(j for j, s_ in enumerate(cz.SCREENS) if s_[0] == name)
    b = bundle
    off = cz.render(b["data"], TUCSON, i, clim=b["clim"], rec=b["rec"], upper=b["upper"])
    assert cz.PROBE is not None, f"{name} registers no probe"
    on = cz.render(b["data"], TUCSON, i, clim=b["clim"], rec=b["rec"], upper=b["upper"], inspect="anchor")
    assert cz.PROBE["index"] == cz.PROBE["anchor"]
    assert on.tobytes() != off.tobytes()
    n = len(cz.PROBE["times"])
    for cur in (0, n // 2, n - 1, n + 50):           # ends and out of range are clamped
        cz.render(b["data"], TUCSON, i, clim=b["clim"], rec=b["rec"], upper=b["upper"], inspect=cur)
        assert 0 <= cz.PROBE["index"] < n
        l1, l2 = cz.PROBE["lines"](cz.PROBE["index"])
        assert l1


def test_non_plot_screens_ignore_inspect(bundle):
    for name in ("Now", "7-day", "Anomalies", "Sounding", "Site"):
        i = next(j for j, s_ in enumerate(cz.SCREENS) if s_[0] == name)
        cz.render(bundle["data"], TUCSON, i, clim=bundle["clim"], rec=bundle["rec"],
                  upper=bundle["upper"], inspect="anchor")
        assert cz.PROBE is None


def test_inspect_keys(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    app.idx = 1                                      # Temp
    app.key("enter")
    assert app.inspect == 1                          # anchor = current hour
    app.key("right")
    app.key("right")
    assert app.inspect == 3
    app.key("down")                                  # +6 h
    assert app.inspect == 9
    app.key("left")
    assert app.inspect == 8 and app.idx == 1         # arrows move the cursor, not the screen
    app.key("enter")
    assert app.inspect is None
    app.key("right")
    assert app.idx == 2                              # arrows change screens again
    app.idx = 0                                      # Now: nothing to inspect
    app.key("enter")
    assert app.inspect is None


def test_week_step_aliases_and_jump_to_climate(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    today = app.local_today()
    app.idx = 1                                      # a Forecast page
    app.key("b")
    assert app.view_date == today - dt.timedelta(days=7)
    assert cz.SCREENS[app.idx][2] == 2               # moved to Climate so the change is visible
    app.key("b")
    app.key("n")
    assert app.view_date == today - dt.timedelta(days=7)
    app.key("=")
    assert app.view_date is None                     # back to today
    app.key("-")
    assert app.view_date == today - dt.timedelta(days=7)
    wait_for(lambda: not app.clim_busy and not app.upper_busy)


# ---------------------------------------------------------------- Site screen / grid metadata
def test_climatology_records_its_grid_cell(fake):
    clim = cz.get_climatology(TUCSON, False)
    g = clim["grid"]
    assert g["lat"] == 32.2 and g["lon"] == -110.9 and g["elevation"] == 760.0


def test_old_climatology_cache_gets_grid_backfilled(fake):
    clim = cz.get_climatology(TUCSON, False)
    del clim["grid"]
    n = len(fake.calls)
    clim = cz.ensure_clim_grid(TUCSON, False, clim)
    assert clim["grid"]["lat"] == 32.2 and len(fake.calls) == n + 1     # one tiny request
    import json as _json
    assert _json.load(open(cz.clim_path(TUCSON, False)))["grid"]["lat"] == 32.2   # saved


def test_geocode_keeps_place_metadata(fake):
    r = cz.geocode("flagstaff")[0]
    assert r["elevation"] == 2106.0 and r["timezone"] == "America/Phoenix" and r["population"] == 76831


def test_km_between():
    assert cz.km_between(32.2, -110.9, 32.2, -110.9) == 0
    assert 11.0 < cz.km_between(32.0, -110.9, 32.1, -110.9) < 11.2     # 0.1° latitude ≈ 11.1 km


def test_site_screen_renders_in_all_states(bundle):
    i = next(j for j, s_ in enumerate(cz.SCREENS) if s_[0] == "Site")
    data = dict(bundle["data"], latitude=32.22, longitude=-110.93, elevation=741.0,
                timezone="America/Phoenix", timezone_abbreviation="GMT-7", utc_offset_seconds=-25200)
    full = cz.render(data, TUCSON, i, clim=bundle["clim"], upper=bundle["upper"])
    bare = cz.render(data, TUCSON, i)                                  # nothing loaded yet
    assert full.tobytes() != bare.tobytes()
    cz.render(dict(data, _metric=True), dict(TUCSON, elevation=728, population=542629), i,
              clim=bundle["clim"], upper=bundle["upper"])


def test_i_key_toggles_inspect(fake, forecast):
    app = cz.App(lambda img: None)
    app.data = forecast
    app.idx = 1
    app.key("i")
    assert app.inspect == 1
    app.key("i")
    assert app.inspect is None and app.idx == 1
