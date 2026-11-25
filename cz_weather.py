#!/usr/bin/env python3
"""
cz-weather: an Open-Meteo forecast viewer for the M5Stack CardputerZero (320x170).

Pulls current conditions, a 48-hour hourly forecast and a 7-day daily forecast
for any location from Open-Meteo (free, no API key), plus a 1991-2020 ERA5 reanalysis
climatology for the same point, and draws them as 18 screens in six sections sized for the
CardputerZero LCD. The climatology is downloaded and computed once per location
and cached in ~/.cache/cz-weather/.

Display backends
  --fb [/dev/fbN]   draw straight to the framebuffer (on the device)
  --window          a Tk window scaled up 3x (for developing on a desktop/VM)
  --png DIR         render every screen to PNG files and exit (no input needed)

Keys
  left/right  or  , /     previous / next screen
  1-6                     jump to a section: 1 Forecast, 2 Climate, 3 Monsoon, 4 Fire, 5 Ag, 6 Sounding
  up/down                 on the Sounding screen, step the valid time -/+ 3 h
  d                       view a past date on the climate screens (YYYY-MM-DD; Enter alone = today)
  b / n  (or - / =)       step the view date back / forward a week
  Enter or i              inspect a plot: cursor with values; arrows move it, Enter/i/Esc leave
  h or ?                  show the key list
  r                       refresh now
  l                       set location: type a place name or "lat, lon", Enter; Esc cancels
  u                       toggle imperial / metric
  q  or hold Esc 2 s      quit (back to the launcher)

Only dependency beyond the standard library: Pillow (and python-evdev on the device).
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

W, H = 320, 170
APP_NAME = "cz-weather"
APP_VERSION = "0.10 (2026-11-24)"    # shown on the help screen: bump with each install
CACHE_DIR = os.path.expanduser("~/.cache/cz-weather")
CONFIG_PATH = os.path.expanduser("~/.config/cz-weather.json")
REFRESH_MIN = 15
USER_AGENT = "cz-weather/0.1 (CardputerZero)"

# ---------------------------------------------------------------- colours
BG = (12, 16, 24)
PANEL = (24, 30, 42)
GRID = (44, 52, 68)
FG = (230, 234, 240)
MUTED = (140, 150, 168)
TEMP = (255, 122, 69)     # orange
DEW = (77, 182, 172)      # teal
PRECIP = (79, 140, 255)   # blue
WIND = (186, 160, 255)    # lavender
GUST = (120, 104, 176)
ALERT = (255, 82, 82)


# ---------------------------------------------------------------- fonts
def _font(size, bold=False):
    names = (["DejaVuSans-Bold.ttf", "DejaVuSansCondensed-Bold.ttf", "segoeuib.ttf", "arialbd.ttf"]
             if bold else
             ["DejaVuSansCondensed.ttf", "DejaVuSans.ttf", "segoeui.ttf", "arial.ttf"])
    windir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    dirs = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
            "/usr/share/fonts/dejavu", "/Library/Fonts", windir, ""]
    for d in dirs:
        for n in names:
            try:
                return ImageFont.truetype(os.path.join(d, n), size)
            except OSError:
                continue
    return ImageFont.load_default()


F_XS, F_S, F_M = _font(10), _font(12), _font(14)
F_MB, F_L, F_XL = _font(14, True), _font(20, True), _font(44, True)


# ---------------------------------------------------------------- WMO codes
WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Lt drizzle", 53: "Drizzle", 55: "Hvy drizzle",
    56: "Frz drizzle", 57: "Frz drizzle", 61: "Lt rain", 63: "Rain", 65: "Hvy rain",
    66: "Frz rain", 67: "Frz rain", 71: "Lt snow", 73: "Snow", 75: "Hvy snow",
    77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Hvy showers",
    85: "Snow shwrs", 86: "Snow shwrs", 95: "T-storms", 96: "T-storm/hail",
    99: "T-storm/hail",
}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    if deg is None:
        return "--"
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


# ---------------------------------------------------------------- data
ERROR_LOG = os.path.join(os.path.expanduser("~/.cache/cz-weather"), "errors.log")


def log_error(where, ex):
    """Append the full error to ~/.cache/cz-weather/errors.log (the screen only has room for a summary)."""
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} [{where}] {type(ex).__name__}: {ex}\n")
            url = getattr(ex, "url", None) or getattr(ex, "filename", None)
            if url:
                f.write(f"    url: {url}\n")
    except OSError:
        pass


def short_err(ex):
    msg = getattr(ex, "api_reason", None) or str(ex)
    return msg[:38]


# Open-Meteo's free tier limits calls per minute/hour/day, and counts a big request (many
# variables or a long period) as many calls. On HTTP 429 we wait and retry; WAIT_CALLBACK lets
# the app show "waiting…" on screen. Tests replace SLEEP to avoid real waits.
RATE_LIMIT_WAIT_S = 61
RATE_LIMIT_RETRIES = 3
WAIT_CALLBACK = None
SLEEP = time.sleep


def http_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            # Open-Meteo explains errors in the body: {"error": true, "reason": "..."}
            try:
                ex.api_reason = json.loads(ex.read().decode("utf-8", "replace")).get("reason")
            except Exception:
                ex.api_reason = None
            if ex.api_reason:
                ex.msg = f"{ex.msg}: {ex.api_reason}"
            ex.url = url
            if ex.code == 429 and attempt < RATE_LIMIT_RETRIES:
                if WAIT_CALLBACK:
                    WAIT_CALLBACK(f"API rate limit: waiting {RATE_LIMIT_WAIT_S} s…")
                SLEEP(RATE_LIMIT_WAIT_S)
                continue
            raise


def geocode(name, count=5):
    q = urllib.parse.urlencode({"name": name, "count": count, "language": "en",
                                "format": "json"})
    res = http_json("https://geocoding-api.open-meteo.com/v1/search?" + q)
    out = []
    for r in res.get("results", []) or []:
        label = ", ".join(x for x in [r.get("name"), r.get("admin1"),
                                      r.get("country_code")] if x)
        out.append({"name": label, "lat": r["latitude"], "lon": r["longitude"],
                    "elevation": r.get("elevation"), "timezone": r.get("timezone"),
                    "population": r.get("population")})
    return out


def parse_latlon(text):
    """'32.25, -110.95' or '32.25 -110.95' -> location dict, else None."""
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return {"name": f"{lat:.3f}, {lon:.3f}", "lat": lat, "lon": lon}
    return None


def fetch_forecast(lat, lon, metric=False):
    params = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "timezone": "auto", "forecast_days": 16,
        "current": ",".join(["temperature_2m", "relative_humidity_2m",
                             "dew_point_2m", "apparent_temperature",
                             "weather_code", "wind_speed_10m",
                             "wind_direction_10m", "wind_gusts_10m",
                             "pressure_msl", "precipitation"]),
        "hourly": ",".join(["temperature_2m", "dew_point_2m",
                            "precipitation_probability", "precipitation",
                            "wind_speed_10m", "wind_gusts_10m",
                            "pressure_msl"]),
        "daily": ",".join(["weather_code", "temperature_2m_max",
                           "temperature_2m_min", "precipitation_sum",
                           "precipitation_probability_max",
                           "wind_speed_10m_max"]),
    }
    if not metric:
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph",
                      precipitation_unit="inch")
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = http_json(url)
    data["_fetched"] = time.time()
    data["_metric"] = metric
    return data


def cache_path(loc):
    return os.path.join(CACHE_DIR, f"{loc['lat']:.3f}_{loc['lon']:.3f}.json")


def save_cache(loc, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(loc), "w") as f:
        json.dump(data, f)


def load_cache(loc):
    try:
        with open(cache_path(loc)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"location": {"name": "Tucson, Arizona, US",
                             "lat": 32.2217, "lon": -110.9265},
                "metric": False}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- helpers
def parse_times(strs):
    return [dt.datetime.fromisoformat(s) for s in strs]


def window_hourly(data, hours=48):
    """Slice the hourly arrays to [now-1h, now+hours)."""
    h = data["hourly"]
    times = parse_times(h["time"])
    now = dt.datetime.fromisoformat(data["current"]["time"]).replace(minute=0)
    start = next((i for i, t in enumerate(times) if t >= now - dt.timedelta(hours=1)), 0)
    end = min(len(times), start + hours)
    out = {k: v[start:end] for k, v in h.items() if k != "time"}
    out["time"] = times[start:end]
    out["now"] = dt.datetime.fromisoformat(data["current"]["time"])
    return out


def nice_range(vals, pad_frac=0.08, min_span=1.0, floor=None):
    v = [x for x in vals if x is not None]
    if not v:
        return 0, 1
    lo, hi = min(v), max(v)
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    pad = (hi - lo) * pad_frac
    lo, hi = lo - pad, hi + pad
    if floor is not None:
        lo = max(lo, floor)
    return lo, hi


def nice_ticks(lo, hi, n=4):
    span = hi - lo
    raw = span / n
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n:
            break
    first = (int(lo / step) + (1 if lo > 0 else 0)) * step
    ticks, t = [], first
    while t <= hi + 1e-9:
        if t >= lo - 1e-9:
            ticks.append(t)
        t += step
    return ticks


def fmt_tick(v):
    return f"{v:.0f}" if abs(v) >= 10 or v == int(v) else f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}"


def text_w(d, s, font):
    return d.textlength(s, font=font)


# ---------------------------------------------------------------- plot core
class Plot:
    """A tiny time-series plotting area: y axis on the left, day labels below."""

    def __init__(self, d, box, times, ylo, yhi):
        self.d, self.box, self.times = d, box, times
        self.ylo, self.yhi = ylo, yhi
        self.x0, self.y0, self.x1, self.y1 = box
        self.t0, self.t1 = times[0], times[-1]

    def x(self, t):
        span = (self.t1 - self.t0).total_seconds() or 1
        return self.x0 + (t - self.t0).total_seconds() / span * (self.x1 - self.x0)

    def y(self, v):
        return self.y1 - (v - self.ylo) / ((self.yhi - self.ylo) or 1) * (self.y1 - self.y0)

    def axes(self, unit=""):
        d = self.d
        for tv in nice_ticks(self.ylo, self.yhi):
            yy = self.y(tv)
            d.line([(self.x0, yy), (self.x1, yy)], fill=GRID)
            lbl = fmt_tick(tv)
            d.text((self.x0 - 3 - text_w(d, lbl, F_XS), yy - 6), lbl, font=F_XS, fill=MUTED)
        if unit:
            d.text((2, self.y0 - 15), unit, font=F_XS, fill=MUTED)
        # midnight lines + day labels, 6-hourly ticks
        for t in self.times:
            xx = self.x(t)
            if t.hour == 0:
                d.line([(xx, self.y0), (xx, self.y1)], fill=MUTED)
                lbl = t.strftime("%a")
                d.text((xx + 2, self.y1 + 1), lbl, font=F_XS, fill=FG)
            elif t.hour in (6, 12, 18):
                d.line([(xx, self.y1), (xx, self.y1 + 3)], fill=MUTED)
                if t.hour == 12 and xx - self.x0 > 14 and self.x1 - xx > 14:
                    d.text((xx - 7, self.y1 + 1), "12p", font=F_XS, fill=MUTED)
        d.rectangle(self.box, outline=GRID)

    def now_marker(self, now):
        if now is None:                      # archived (past-date) data: no "now" line
            return
        if self.t0 <= now <= self.t1:
            xx = self.x(now)
            for yy in range(int(self.y0), int(self.y1), 4):
                self.d.line([(xx, yy), (xx, yy + 1)], fill=FG)

    def line(self, vals, color, width=2):
        pts = [(self.x(t), self.y(v)) for t, v in zip(self.times, vals) if v is not None]
        if len(pts) > 1:
            self.d.line(pts, fill=color, width=width, joint="curve")

    def bars(self, vals, color):
        n = len(self.times)
        bw = max(1, (self.x1 - self.x0) / max(n, 1) - 1)
        for t, v in zip(self.times, vals):
            if v is None or v <= 0:
                continue
            xx = self.x(t)
            self.d.rectangle([xx - bw / 2, min(self.y(v), self.y1 - 1), xx + bw / 2, self.y1 - 1], fill=color)


# ---------------------------------------------------------------- screens
def header(d, title, loc_name, data, idx, n, tag=""):
    d.rectangle([0, 0, W, 15], fill=PANEL)
    d.text((4, 1), title, font=F_MB, fill=FG)
    tw = text_w(d, title, F_MB)
    name = loc_name.split(",")[0]
    d.text((tw + 10, 2), name, font=F_S, fill=MUTED)
    if tag:                                  # the view date on climate screens
        x = tw + 16 + text_w(d, name, F_S)
        past = not tag.startswith("today")
        d.rectangle([x - 2, 1, x + text_w(d, tag, F_XS) + 2, 14], fill=GOLD_BG if past else GRID)
        d.text((x, 2), tag, font=F_XS, fill=GOLD if past else MUTED)
    # section number + dots for the pages within this section
    sec = SCREENS[idx][2]
    pages = [i for i, s in enumerate(SCREENS) if s[2] == sec]
    for j, i in enumerate(pages):
        cx = W - 8 - (len(pages) - 1 - j) * 8
        d.ellipse([cx - 2, 6, cx + 2, 10], fill=FG if i == idx else GRID)
    lbl = f"{sec}"
    d.text((W - 8 - (len(pages) - 1) * 8 - 8 - text_w(d, lbl, F_XS), 2), lbl, font=F_XS, fill=MUTED)


def footer_status(d, data, status):
    age = (time.time() - data.get("_fetched", time.time())) / 60
    msg = status or (f"updated {age:.0f} min ago" if age >= 1 else "updated just now")
    col = ALERT if (status and "offline" in status) or age > 90 else MUTED
    tw = text_w(d, msg, F_XS)
    d.text((W - tw - 3, H - 12), msg, font=F_XS, fill=col)


def u_temp(data):
    return "°C" if data["_metric"] else "°F"


def u_wind(data):
    return "km/h" if data["_metric"] else "mph"


def u_precip(data):
    return "mm" if data["_metric"] else "in"


GOLD, GOLD_BG = (255, 210, 120), (70, 52, 20)


def today_tag(data):
    """Gray header tag for screens showing today's/live data."""
    t = dt.datetime.fromisoformat(data["current"]["time"]).date()
    return f"today {t.day} {t:%b}"


def date_tag(d):
    """Gold header tag for a past date."""
    return f"{d.day} {d:%b %Y}"


def clock_label(t):
    """'Tue Nov 24 3:09p' (no %-I/%-d: Windows strftime rejects them)."""
    hr = t.hour % 12 or 12
    return f"{t:%a %b} {t.day} {hr}:{t:%M}{'a' if t.hour < 12 else 'p'}"


def screen_now(img, data, loc, status, view_date=None):
    d = ImageDraw.Draw(img)
    c = data["current"]
    d.rectangle([0, 0, W, 15], fill=PANEL)
    when = clock_label(dt.datetime.fromisoformat(c["time"]))
    ww = text_w(d, when, F_S)
    d.text((W - 4 - ww, 2), when, font=F_S, fill=FG)
    name = loc["name"]
    while name and text_w(d, name, F_MB) > W - ww - 16:
        name = name[:-1]
    d.text((4, 1), name, font=F_MB, fill=FG)

    t = c.get("temperature_2m")
    big = f"{t:.0f}°" if t is not None else "--"
    d.text((8, 18), big, font=F_XL, fill=TEMP)
    bw = text_w(d, big, F_XL)
    cond = WMO.get(c.get("weather_code"), "")
    d.text((bw + 16, 26), cond, font=F_L, fill=FG)
    fl = c.get("apparent_temperature")
    if fl is not None:
        d.text((bw + 16, 50), f"feels {fl:.0f}{u_temp(data)}", font=F_S, fill=MUTED)

    daily = data["daily"]
    hi, lo = daily["temperature_2m_max"][0], daily["temperature_2m_min"][0]
    d.text((W - 70, 22), f"H {hi:.0f}°", font=F_MB, fill=TEMP)
    d.text((W - 70, 40), f"L {lo:.0f}°", font=F_MB, fill=DEW)

    rows = [
        ("Dewpt", f"{c.get('dew_point_2m', 0):.0f}{u_temp(data)}", DEW),
        ("RH", f"{c.get('relative_humidity_2m', 0):.0f}%", DEW),
        ("Wind", f"{compass(c.get('wind_direction_10m'))} {c.get('wind_speed_10m', 0):.0f}"
                 f" g{c.get('wind_gusts_10m', 0):.0f} {u_wind(data)}", WIND),
        ("Press", f"{c.get('pressure_msl', 0):.0f} hPa {pressure_trend(data)}", FG),
        ("Precip", f"{daily['precipitation_probability_max'][0] or 0:.0f}% today, "
                   f"{daily['precipitation_sum'][0] or 0:.{1 if data['_metric'] else 2}f} {u_precip(data)}", PRECIP),
    ]
    y = 68
    for i, (k, v, col) in enumerate(rows):
        x = 8 if i % 2 == 0 else 166
        if i == 4:
            x = 8
        d.text((x, y), k, font=F_S, fill=MUTED)
        d.text((x + 44, y), v, font=F_S, fill=col)
        if i % 2 == 1 or i == 4:
            y += 16
    sparkline(d, data, (8, 122, W - 8, 152))
    hint = f"viewing {date_tag(view_date)} · d to change" if view_date else "l location · d date · h help"
    d.text((4, H - 12), hint, font=F_XS, fill=GOLD)
    footer_status(d, data, status)


def sparkline(d, data, box):
    """Next-24h temperature strip with min/max labels, for the Now screen."""
    h = window_hourly(data, 25)
    vals = h["temperature_2m"]
    if len(vals) < 2:
        return
    x0, y0, x1, y1 = box
    lo, hi = nice_range(vals, min_span=4, pad_frac=0.05)
    p = Plot(d, (x0 + 26, y0, x1 - 26, y1), h["time"], lo, hi)
    for t in h["time"]:
        if t.hour == 0:
            xx = p.x(t)
            d.line([(xx, y0), (xx, y1)], fill=GRID)
    p.line(vals, TEMP)
    d.text((x0, y0 - 2), "24h", font=F_XS, fill=MUTED)
    d.text((x1 - 22, y0 - 2), f"{max(vals):.0f}°", font=F_XS, fill=TEMP)
    d.text((x1 - 22, y1 - 10), f"{min(vals):.0f}°", font=F_XS, fill=DEW)


def pressure_trend(data):
    h = window_hourly(data, 6)
    p = [x for x in h.get("pressure_msl", []) if x is not None]
    if len(p) < 4:
        return ""
    dp = p[3] - p[0]
    return "↑" if dp > 0.7 else "↓" if dp < -0.7 else "→"


PLOT_BOX = (30, 32, W - 6, H - 28)


def legend(d, items, y=19):
    x = 32
    for label, col in items:
        d.rectangle([x, y + 4, x + 8, y + 8], fill=col)
        d.text((x + 11, y), label, font=F_XS, fill=FG)
        x += 14 + text_w(d, label, F_XS) + 8


# ---------------------------------------------------------------- inspect cursor ("probe")
# Plot screens register what's under the cursor; render() draws the cursor + readout when the
# app is in inspect mode. lines(i) -> (line1, line2): lists of (text, color); line2 None keeps the
# footer. one_line=True (stacked hourly panels) puts everything in the footer row.
PROBE = None


def set_probe(times, xfn, y0, y1, lines, anchor=0, hourly=True, one_line=False):
    global PROBE
    PROBE = {"times": times, "x": xfn, "y0": y0, "y1": y1, "lines": lines,
             "anchor": max(0, min(anchor, len(times) - 1)), "hourly": hourly, "one_line": one_line}


def probe_time(t, hourly):
    if hourly:
        hr = t.hour % 12 or 12
        return f"{t:%a} {hr}{'a' if t.hour < 12 else 'p'}"
    return f"{t:%a %b} {t.day}"


def fv(v, nd=0, unit=""):
    """Format a value for the readout; '--' for missing."""
    return "--" if v is None else f"{v:.{nd}f}{unit}"


def draw_probe(img, probe, cursor):
    """Draw the cursor and readout; returns the resolved cursor index."""
    n = len(probe["times"])
    if not n:
        return 0
    i = probe["anchor"] if cursor == "anchor" else max(0, min(int(cursor), n - 1))
    d = ImageDraw.Draw(img)
    x = probe["x"](probe["times"][i])
    for yy in range(int(probe["y0"]), int(probe["y1"]), 3):
        d.line([(x, yy), (x, yy + 1)], fill=GOLD)
    d.polygon([(x - 3, probe["y0"] - 1), (x + 3, probe["y0"] - 1), (x, probe["y0"] + 3)], fill=GOLD)
    l1, l2 = probe["lines"](i)
    stamp = [(probe_time(probe["times"][i], probe["hourly"]), GOLD)]

    def write(y, segs):
        d.rectangle([0, y - 1, W, y + 12], fill=BG)
        xx = 4
        for txt, col in segs:
            if not txt:
                continue
            d.text((xx, y), txt, font=F_XS, fill=col)
            xx += text_w(d, txt, F_XS) + 6
    if probe["one_line"]:
        write(H - 12, stamp + l1 + (l2 or []))
    else:
        write(18, stamp + l1)
        if l2 is not None:
            write(H - 12, l2)
    return i


def screen_temp(img, data, loc, status, idx, n):
    d = ImageDraw.Draw(img)
    header(d, "Temp", loc["name"], data, idx, n, tag=today_tag(data))
    h = window_hourly(data)
    lo, hi = nice_range(h["temperature_2m"] + h["dew_point_2m"], min_span=5)
    p = Plot(d, PLOT_BOX, h["time"], lo, hi)
    p.axes(u_temp(data))
    p.now_marker(h["now"])
    p.line(h["dew_point_2m"], DEW)
    p.line(h["temperature_2m"], TEMP)
    tmax = max(v for v in h["temperature_2m"] if v is not None)
    tmin = min(v for v in h["temperature_2m"] if v is not None)
    legend(d, [(f"Temp {tmin:.0f}–{tmax:.0f}", TEMP), ("Dewpoint", DEW)])
    footer_status(d, data, status)
    tu = u_temp(data)
    set_probe(h["time"], p.x, PLOT_BOX[1], PLOT_BOX[3], lambda i: (
        [(f"Temp {fv(h['temperature_2m'][i], 0, tu)}", TEMP), (f"Td {fv(h['dew_point_2m'][i], 0, tu)}", DEW)],
        None), anchor=1)


def screen_precip(img, data, loc, status, idx, n):
    d = ImageDraw.Draw(img)
    header(d, "Precip", loc["name"], data, idx, n, tag=today_tag(data))
    h = window_hourly(data)
    p = Plot(d, PLOT_BOX, h["time"], 0, 100)
    p.axes("%")
    p.bars(h["precipitation_probability"], PRECIP)
    p.now_marker(h["now"])
    # amount as a cumulative line on a hidden secondary scale
    amt = [a or 0 for a in h["precipitation"]]
    cum, s = [], 0.0
    for a in amt:
        s += a
        cum.append(s)
    total = cum[-1] if cum else 0
    if total > 0:
        p2 = Plot(d, PLOT_BOX, h["time"], 0, total * 1.1)
        p2.line(cum, FG, width=1)
    legend(d, [("Chance", PRECIP),
               (f"48h total {total:.2f} {u_precip(data)}", FG)])
    footer_status(d, data, status)
    pu, nd = u_precip(data), (1 if data["_metric"] else 2)
    set_probe(h["time"], p.x, PLOT_BOX[1], PLOT_BOX[3], lambda i: (
        [(f"chance {fv(h['precipitation_probability'][i], 0, '%')}", PRECIP),
         (f"{fv(amt[i], nd)} {pu}", FG), (f"total so far {fv(cum[i], nd)}", MUTED)], None), anchor=1)


def screen_wind(img, data, loc, status, idx, n):
    d = ImageDraw.Draw(img)
    header(d, "Wind", loc["name"], data, idx, n, tag=today_tag(data))
    h = window_hourly(data)
    lo, hi = nice_range(h["wind_speed_10m"] + h["wind_gusts_10m"], min_span=5, floor=0)
    p = Plot(d, PLOT_BOX, h["time"], 0, hi)
    p.axes(u_wind(data))
    p.now_marker(h["now"])
    p.line(h["wind_gusts_10m"], GUST, width=1)
    p.line(h["wind_speed_10m"], WIND)
    gmax = max(v for v in h["wind_gusts_10m"] if v is not None)
    legend(d, [("Sustained", WIND), (f"Gusts (max {gmax:.0f})", GUST)])
    footer_status(d, data, status)
    wu = u_wind(data)
    set_probe(h["time"], p.x, PLOT_BOX[1], PLOT_BOX[3], lambda i: (
        [(f"wind {fv(h['wind_speed_10m'][i])}", WIND), (f"gust {fv(h['wind_gusts_10m'][i])} {wu}", GUST)],
        None), anchor=1)


MODEL_RES = {"era5_seamless": "ERA5-Land 0.1° (~9–11 km), radiation ERA5 0.25°",
             "era5_land": "ERA5-Land 0.1° (~9–11 km)", "era5": "ERA5 0.25° (~25 km)"}


def km_between(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def screen_site(img, data, loc, status, idx, n, clim=None, upper=None, **_):
    """Where the numbers come from: the place, then each Open-Meteo source's grid cell."""
    d = ImageDraw.Draw(img)
    header(d, "Site", loc["name"], data, idx, n, tag=today_tag(data))
    m = data.get("_metric", False)

    def elev(v):
        if v is None:
            return "--"
        return f"{v:.0f} m" if m else f"{v * 3.28084:,.0f} ft"

    y = 19
    d.text((4, y), loc["name"][:40], font=F_MB, fill=FG)
    y += 17
    place = f"{loc['lat']:.3f}, {loc['lon']:.3f}"
    if loc.get("elevation") is not None:
        place += f" · elev {elev(loc['elevation'])}"
    if loc.get("population"):
        place += f" · pop {loc['population']:,}"
    d.text((4, y), place, font=F_S, fill=MUTED)
    y += 18

    cols = (4, 70, 170, 218, 270)
    for x, h in zip(cols, ("source", "grid-cell center", "off", "elev", "")):
        d.text((x, y), h, font=F_XS, fill=MUTED)
    y += 12
    d.line([(4, y), (W - 4, y)], fill=GRID)
    y += 3
    rows = [("Forecast", grid_info(data), TEMP, "best-match model blend")]
    rows.append(("Climate", (clim or {}).get("grid"), PRECIP, MODEL_RES.get((clim or {}).get("model"), "")))
    if upper is not None:
        v = upper.get("_view")
        note = f"forecast archive, {date_tag(dt.date.fromisoformat(v))}" if v else "live forecast"
        rows.append(("Hourly", grid_info(upper), DEW, note))
    for name, g, col, note in rows:
        d.text((cols[0], y), name, font=F_S, fill=col)
        if g:
            off = km_between(loc["lat"], loc["lon"], g["lat"], g["lon"])
            d.text((cols[1], y), f"{g['lat']:.3f}, {g['lon']:.3f}", font=F_S, fill=FG)
            d.text((cols[2], y), f"{off:.1f} km" if m else f"{off * 0.621371:.1f} mi", font=F_S, fill=FG)
            d.text((cols[3], y), elev(g.get("elevation")), font=F_S, fill=FG)
        else:
            d.text((cols[1], y), "not loaded yet", font=F_S, fill=MUTED)
        y += 13
        d.text((cols[1], y), note, font=F_XS, fill=MUTED)
        y += 13
    tz = data.get("timezone")
    if tz:
        off_h = (data.get("utc_offset_seconds") or 0) / 3600
        d.text((4, H - 25), f"time zone {tz} ({data.get('timezone_abbreviation', '')}, UTC{off_h:+g})",
               font=F_XS, fill=MUTED)
    d.text((4, H - 12), "elev: 90 m DEM height the values are downscaled to", font=F_XS, fill=MUTED)


def screen_7day(img, data, loc, status, idx, n):
    d = ImageDraw.Draw(img)
    header(d, "7-day", loc["name"], data, idx, n, tag=today_tag(data))
    dl = data["daily"]
    days = parse_times(dl["time"])[:7]
    his, los = dl["temperature_2m_max"][:7], dl["temperature_2m_min"][:7]
    tlo, thi = nice_range(his + los, min_span=10)
    x_bar0, x_bar1 = 124, 258
    y = 20
    row_h = (H - 34) / len(days)
    for i, day in enumerate(days):
        cy = y + i * row_h + row_h / 2
        if i % 2 == 0:
            d.rectangle([0, cy - row_h / 2, W, cy + row_h / 2], fill=(16, 21, 31))
        name = "Today" if i == 0 else day.strftime("%a")
        d.text((4, cy - 7), name, font=F_S, fill=FG)
        cond = WMO.get(dl["weather_code"][i], "")
        while cond and text_w(d, cond, F_XS) > 64:
            cond = cond[:-1]
        d.text((42, cy - 6), cond, font=F_XS, fill=MUTED)

        def bx(v):
            return x_bar0 + (v - tlo) / (thi - tlo) * (x_bar1 - x_bar0)
        lo_, hi_ = los[i], his[i]
        d.text((bx(lo_) - 4 - text_w(d, f"{lo_:.0f}", F_XS), cy - 6), f"{lo_:.0f}",
               font=F_XS, fill=DEW)
        d.rounded_rectangle([bx(lo_), cy - 3, bx(hi_), cy + 3], radius=3, fill=TEMP)
        d.text((bx(hi_) + 4, cy - 6), f"{hi_:.0f}", font=F_XS, fill=TEMP)
        pp = dl["precipitation_probability_max"][i] or 0
        col = PRECIP if pp >= 20 else MUTED
        d.text((W - 30, cy - 6), f"{pp:.0f}%", font=F_XS, fill=col)
    d.text((4, H - 12), f"temps {u_temp(data)} · max precip chance", font=F_XS, fill=MUTED)
    footer_status(d, data, status)


# ================================================================ climatology
# Everything here comes from Open-Meteo:
#   baseline  : ERA5 reanalysis daily, 1991-2020 (Oct 1990 start so all 30 water years are complete)
#   recent    : the same reanalysis via the archive API (about 5 days behind)
#   gap + fcst: forecast API daily (past_days fills the last few days, then 16 days ahead)
# The archive model is chosen at build time: ERA5-Seamless (ERA5-Land temperature/humidity
# + ERA5 radiation) if the API accepts it, otherwise ERA5-Land.
CLIM_MODELS = ["era5_seamless", "era5_land"]
CLIM_START, CLIM_END = dt.date(1990, 10, 1), dt.date(2020, 12, 31)
CLIM_YEARS = range(1991, 2021)
WINDOW = 7            # +/- days pooled for daily percentiles
RECENT_TTL_H = 3      # refresh the recent/forecast series this often
CLIM_VERSION = 2

BASE_DAILY = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
# Optional daily variables: kept only if the archive returns data for them (probed per location).
EXTRA_DAILY = {
    "dew_point_2m_mean":           {"label": "Td mean",  "color": "DEW"},
    "relative_humidity_2m_min":    {"label": "RH min",   "color": "DEW"},
    "vapour_pressure_deficit_max": {"label": "VPD max",  "color": "TEMP"},
    "wind_gusts_10m_max":          {"label": "Gust max", "color": "WIND"},
    "et0_fao_evapotranspiration":  {"label": "ET0",      "color": "PRECIP"},
    "shortwave_radiation_sum":     {"label": "SW rad",   "color": "TEMP"},
}
SHORT = {"temperature_2m_max": "tmax", "temperature_2m_min": "tmin", "precipitation_sum": "pr"}


def units_params(metric):
    return ({"wind_speed_unit": "kmh"} if metric else
            {"temperature_unit": "fahrenheit", "precipitation_unit": "inch", "wind_speed_unit": "mph"})


def doy366(d):
    """0..365 index on a leap-year calendar, so Feb 29 has its own slot."""
    return (d.replace(year=2000) - dt.date(2000, 1, 1)).days


def wy_start(d):
    return dt.date(d.year if d.month >= 10 else d.year - 1, 10, 1)


def pctl(sorted_vals, q):
    n = len(sorted_vals)
    if n == 0:
        return None
    pos = q * (n - 1)
    i = int(pos)
    f = pos - i
    return sorted_vals[i] if i + 1 >= n else sorted_vals[i] * (1 - f) + sorted_vals[i + 1] * f


def gdd_day(tx, tn, metric):
    """Growing degree days, base 50°F/10°C with an 86°F/30°C cap (simple average method)."""
    if tx is None or tn is None:
        return 0.0
    base, cap = (10.0, 30.0) if metric else (50.0, 86.0)
    hi, lo = min(tx, cap), min(max(tn, base), cap)
    return max(0.0, (max(hi, base) + lo) / 2 - base)


def _r(v, nd=3):
    return None if v is None else round(v, nd)


def doy_stats(dates, vals, nd=2, with_rec=None):
    """Day-of-year p10/p50/p90/mean pooled over +/-WINDOW days; optional record max/min."""
    by = [[] for _ in range(366)]
    for d, v in zip(dates, vals):
        if v is not None and d.year in CLIM_YEARS:
            by[doy366(d)].append(v)
    out = {"p10": [], "p50": [], "p90": [], "mean": []}
    if with_rec:
        out["rec"] = []
    for k in range(366):
        v = []
        for o in range(-WINDOW, WINDOW + 1):
            v.extend(by[(k + o) % 366])
        v.sort()
        out["p10"].append(_r(pctl(v, .1), nd))
        out["p50"].append(_r(pctl(v, .5), nd))
        out["p90"].append(_r(pctl(v, .9), nd))
        out["mean"].append(_r(sum(v) / len(v), nd) if v else None)
        if with_rec:
            out["rec"].append(with_rec(by[k]) if by[k] else None)
    return out, by


def cum_curves(dates, vals, start_fn, years, nd=3):
    """One cumulative curve per year (366 slots, padded); start_fn(year) gives the start date."""
    idx = {d: i for i, d in enumerate(dates)}
    curves = []
    for y in years:
        s0 = start_fn(y)
        nxt = start_fn(y + 1)
        cum, tot = [], 0.0
        for i in range(366):
            d = s0 + dt.timedelta(days=i)
            if d >= nxt:
                cum.append(cum[-1])
                continue
            j = idx.get(d)
            tot += (vals[j] if j is not None and vals[j] is not None else 0.0)
            cum.append(round(tot, nd))
        curves.append(cum)
    stats = {"p10": [], "p50": [], "p90": [], "mean": []}
    for i in range(366):
        v = sorted(c[i] for c in curves)
        stats["p10"].append(round(pctl(v, .1), nd))
        stats["p50"].append(round(pctl(v, .5), nd))
        stats["p90"].append(round(pctl(v, .9), nd))
        stats["mean"].append(round(sum(v) / len(v), nd))
    return stats, curves


def build_climatology(daily, metric=False, extras=()):
    """daily: Open-Meteo 'daily' dict over CLIM_START..CLIM_END. Returns the compact climatology."""
    dates = [dt.date.fromisoformat(x) for x in daily["time"]]
    tx, tn, pr = daily["temperature_2m_max"], daily["temperature_2m_min"], daily["precipitation_sum"]
    clim = {}
    clim["tmax"], _ = doy_stats(dates, tx, with_rec=max)
    clim["tmin"], _ = doy_stats(dates, tn, with_rec=min)
    _, by_p = doy_stats(dates, pr)
    clim["pr_mean"] = [round(sum(v) / len(v), 4) if v else 0.0 for v in by_p]
    # water year (Oct 1) cumulative precipitation, WY1991..2020
    clim["wy"], clim["wy_curves"] = cum_curves(dates, pr, lambda y: dt.date(y - 1, 10, 1), CLIM_YEARS)
    # growing degree days since Jan 1
    g = [gdd_day(a, b, metric) for a, b in zip(tx, tn)]
    clim["gdd"], clim["gdd_curves"] = cum_curves(dates, g, lambda y: dt.date(y, 1, 1), CLIM_YEARS, nd=1)
    # optional daily variables
    clim["vars"] = {}
    for v in extras:
        if v in daily:
            clim["vars"][v], _ = doy_stats(dates, daily[v], nd=3)
    return clim


def grid_info(resp):
    """Grid-cell metadata Open-Meteo returns with every response: the cell center used and the
    90 m DEM elevation the values are downscaled to. (No cell size or model name is returned.)"""
    if not isinstance(resp, dict) or resp.get("latitude") is None:
        return None
    return {"lat": resp.get("latitude"), "lon": resp.get("longitude"), "elevation": resp.get("elevation"),
            "timezone": resp.get("timezone"), "tz_abbr": resp.get("timezone_abbreviation"),
            "utc_offset_s": resp.get("utc_offset_seconds")}


def ensure_clim_grid(loc, metric, clim):
    """Climatologies cached before the Site screen existed lack grid info: fetch one week (~1 API
    call) to get it and save it back into the cache."""
    if clim.get("grid"):
        return clim
    try:
        raw = _archive({"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}",
                        "start_date": "2019-07-01", "end_date": "2019-07-01", "timezone": "auto",
                        "daily": "temperature_2m_max", "models": clim.get("model", CLIM_MODELS[-1])})
        clim["grid"] = grid_info(raw)
        with open(clim_path(loc, metric), "w") as f:
            json.dump(clim, f)
    except Exception as ex:
        log_error("grid info", ex)
    return clim


def clim_path(loc, metric):
    return os.path.join(CACHE_DIR, f"clim{CLIM_VERSION}_{loc['lat']:.3f}_{loc['lon']:.3f}_"
                                   f"{'metric' if metric else 'imperial'}.json")


def _archive(q):
    return http_json("https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(q))


def probe_archive(loc, metric):
    """Pick an archive model and find which EXTRA_DAILY variables have data at this point."""
    q = {"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}", "timezone": "auto",
         "start_date": "2019-07-01", "end_date": "2019-07-07", **units_params(metric)}
    for model in CLIM_MODELS:
        try:
            _archive({**q, "models": model, "daily": ",".join(BASE_DAILY)})
        except Exception:
            continue
        ok = []
        try:
            res = _archive({**q, "models": model, "daily": ",".join(BASE_DAILY + list(EXTRA_DAILY))})
            ok = [v for v in EXTRA_DAILY if any(x is not None for x in res["daily"].get(v, []))]
        except Exception:          # an unknown name rejects the whole request: test one by one
            for v in EXTRA_DAILY:
                try:
                    res = _archive({**q, "models": model, "daily": v})
                    if any(x is not None for x in res["daily"].get(v, [])):
                        ok.append(v)
                except Exception:
                    pass
        return model, ok
    raise RuntimeError("archive API rejected all models")


def get_climatology(loc, metric, progress=lambda m: None):
    p = clim_path(loc, metric)
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    progress("checking available variables…")
    model, extras = probe_archive(loc, metric)
    # three ~decade chunks: a single 30-year request counts as ~700 API calls, over the
    # free per-minute limit; chunks let the 429 wait-and-retry spread the load
    edges = [CLIM_START, dt.date(2000, 10, 1), dt.date(2010, 10, 1), CLIM_END + dt.timedelta(days=1)]
    daily, units = {}, {}
    for n, (a, b) in enumerate(zip(edges, edges[1:]), 1):
        progress(f"downloading 30-yr {model} ({n}/3)…")
        raw = _archive({"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}",
                        "start_date": a.isoformat(), "end_date": (b - dt.timedelta(days=1)).isoformat(),
                        "daily": ",".join(BASE_DAILY + extras), "models": model, "timezone": "auto",
                        **units_params(metric)})
        for k, v in raw["daily"].items():
            daily.setdefault(k, []).extend(v)
        units.update(raw.get("daily_units", {}))
        grid = grid_info(raw)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(raw_path(loc, metric), "w") as f:      # kept so 1991-2020 dates need no download
        json.dump(daily, f, separators=(",", ":"))
    progress("computing percentiles…")
    clim = build_climatology(daily, metric, extras)
    clim.update(built=time.time(), metric=metric, model=model, extras=extras,
                units=units, base="1991–2020", grid=grid)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(p, "w") as f:
        json.dump(clim, f)
    return clim


def _loc_key(loc, metric):
    return f"{loc['lat']:.3f}_{loc['lon']:.3f}_{'metric' if metric else 'imperial'}"


def raw_path(loc, metric):
    return os.path.join(CACHE_DIR, f"base{CLIM_VERSION}_{_loc_key(loc, metric)}.json")


def year_path(loc, metric, year):
    return os.path.join(CACHE_DIR, f"year{CLIM_VERSION}_{_loc_key(loc, metric)}_{year}.json")


def _slice_daily(daily, a, b, variables):
    """Rows of an Open-Meteo daily dict between dates a..b (inclusive)."""
    out = {"time": []}
    for v in variables:
        out[v] = []
    for i, t in enumerate(daily["time"]):
        if a.isoformat() <= t <= b.isoformat():
            out["time"].append(t)
            for v in variables:
                out[v].append(daily[v][i] if v in daily else None)
    return out


def _concat_daily(parts, variables):
    out = {"time": [], **{v: [] for v in variables}}
    for p in parts:
        out["time"].extend(p["time"])
        for v in variables:
            out[v].extend(p.get(v, [None] * len(p["time"])))
    return out


def archive_year(loc, metric, model, extras, year, today):
    """One calendar year of reanalysis. Comes from the cached 1990-2020 baseline when it can (no
    download); otherwise one archive request, cached forever for complete past years and for
    RECENT_TTL_H hours for the current year."""
    allv = BASE_DAILY + list(extras)
    a, b = dt.date(year, 1, 1), min(dt.date(year, 12, 31), today - dt.timedelta(days=1))
    if b < a:
        return _slice_daily({"time": []}, a, b, allv)
    if CLIM_START <= a and b <= CLIM_END:
        try:
            with open(raw_path(loc, metric)) as f:
                return _slice_daily(json.load(f), a, b, allv)
        except (OSError, ValueError):
            pass                       # baseline saved by an older version: fall through and fetch
    yp = year_path(loc, metric, year)
    try:
        with open(yp) as f:
            blk = json.load(f)
        complete = year < today.year
        if (complete or time.time() - blk.get("_fetched", 0) < RECENT_TTL_H * 3600) \
                and set(allv) <= set(blk["daily"]):
            return _slice_daily(blk["daily"], a, b, allv)
    except (OSError, ValueError, KeyError):
        pass
    raw = _archive({"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}",
                    "timezone": "auto", **units_params(metric), "models": model,
                    "daily": ",".join(allv), "start_date": a.isoformat(), "end_date": b.isoformat()})
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(yp, "w") as f:
        json.dump({"daily": raw["daily"], "_fetched": time.time()}, f)
    return _slice_daily(raw["daily"], a, b, allv)


def window_bounds(center):
    """Days needed around a view date: 45 back (or to the water year / Jan 1 start), 16 ahead."""
    start = min(center - dt.timedelta(days=45), wy_start(center), dt.date(center.year, 1, 1))
    return start, center + dt.timedelta(days=16)


def get_window(loc, metric, extras=(), model=CLIM_MODELS[-1], center=None, today=None):
    """Daily series around a view date (None = today), tagged 'obs' (reanalysis) or 'fc' (forecast
    API, used only for days the reanalysis hasn't reached yet)."""
    base = {"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}",
            "timezone": "auto", **units_params(metric)}
    allv = BASE_DAILY + list(extras)
    furl = "https://api.open-meteo.com/v1/forecast?"

    def forecast(past_days):
        q = {**base, "past_days": past_days, "forecast_days": 16}
        try:
            return http_json(furl + urllib.parse.urlencode({**q, "daily": ",".join(allv)}))["daily"]
        except Exception:          # an extra the forecast API doesn't know: fall back to the core set
            return http_json(furl + urllib.parse.urlencode({**q, "daily": ",".join(BASE_DAILY)}))["daily"]

    fc, today = None, None
    if center is None:             # live view: the forecast also tells us the location's local date
        fc = forecast(10)
        today = dt.date.fromisoformat(fc["time"][10])
        center = today
    else:
        today = today or dt.date.today()
    start, end = window_bounds(center)
    last_ar = min(end, today - dt.timedelta(days=1))
    parts = [archive_year(loc, metric, model, extras, y, today)
             for y in range(start.year, last_ar.year + 1)] if last_ar >= start else []
    ar = _slice_daily(_concat_daily(parts, allv), start, end, allv)
    have = {t for t, x in zip(ar["time"], ar["temperature_2m_max"]) if x is not None}
    need_fc = any((start + dt.timedelta(days=i)).isoformat() not in have
                  for i in range((min(end, today + dt.timedelta(days=15)) - start).days + 1))
    if need_fc and fc is None and end >= today - dt.timedelta(days=92):
        fc = forecast(min(92, max(10, (today - start).days)))
    rec = merge_recent(ar, fc or {"time": []}, center, extras)
    rec["days"] = [r for r in rec["days"] if start.isoformat() <= r["date"] <= end.isoformat()]
    rec["view_past"] = center < today
    return rec


def get_recent(loc, metric, extras=(), model=CLIM_MODELS[-1]):
    """Today's window (kept for the tests and run_png)."""
    return get_window(loc, metric, extras, model, None)


def merge_recent(ar, fc, today, extras=()):
    days, last_obs = {}, None
    for src, blk in (("obs", ar), ("fc", fc)):
        for i, t in enumerate(blk["time"]):
            if t in days:
                continue
            a, b = blk["temperature_2m_max"][i], blk["temperature_2m_min"][i]
            if a is None or b is None:
                continue
            row = {"date": t, "tmax": a, "tmin": b, "pr": blk["precipitation_sum"][i] or 0.0, "src": src}
            for v in extras:
                row[v] = blk[v][i] if v in blk else None
            days[t] = row
            if src == "obs":
                last_obs = t
    return {"days": [days[k] for k in sorted(days)], "today": today.isoformat(),
            "last_obs": last_obs, "built": time.time()}


# ---------------------------------------------------------------- hourly "upper" data
# Monsoon, fire-weather and sounding screens use a separate 3-day hourly request, always in
# metric (°C, km/h, mm); conversion to display units happens at draw time.
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200]
UPPER_SFC = ["temperature_2m", "dew_point_2m", "relative_humidity_2m", "vapour_pressure_deficit",
             "wind_speed_10m", "wind_gusts_10m", "surface_pressure", "cape",
             "precipitation_probability", "freezing_level_height"]
UPPER_OPTIONAL = ["total_column_integrated_water_vapour", "lifted_index", "convective_inhibition"]
UPPER_TTL_MIN = 60


HIST_HOURLY_START = dt.date(2022, 1, 1)   # historical-forecast archive (default model) begins ~2022


class HourlyUnavailable(Exception):
    """No archived hourly/pressure-level data for the requested date."""


def _fetch_upper(url, q):
    """Request the hourly set, dropping UPPER_OPTIONAL names one at a time if the API rejects them."""
    lev = [f"{n}_{p}hPa" for p in LEVELS
           for n in ("temperature", "dew_point", "wind_speed", "wind_direction", "geopotential_height")]
    opt = list(UPPER_OPTIONAL)
    while True:
        try:
            return http_json(url + urllib.parse.urlencode({**q, "hourly": ",".join(UPPER_SFC + opt + lev)}))
        except urllib.error.HTTPError as ex:
            if ex.code != 400 or not opt:
                raise
            bad = None
            for v in opt:
                try:
                    probe = {k: v2 for k, v2 in q.items()}
                    http_json(url + urllib.parse.urlencode({**probe, "hourly": v}))
                except urllib.error.HTTPError:
                    bad = v
                    break
            if bad is None:
                raise
            opt.remove(bad)


def _q_from_td(td_c, p_hpa):
    """Specific humidity (kg/kg) from dewpoint (°C) and pressure (hPa), Bolton (1980) vapour pressure."""
    e = 6.112 * math.exp(17.67 * td_c / (td_c + 243.5))
    return 0.622 * e / (p_hpa - 0.378 * e)


def derive_pw(hourly):
    """Precipitable water (mm = kg/m²) per hour, integrating q dp/g from the surface to 200 hPa with
    the pressure-level dewpoints. Used when the archive has no total_column_integrated_water_vapour.
    Ignores moisture above 200 hPa (well under 1 mm)."""
    out = []
    n = len(hourly.get("time", []))
    for i in range(n):
        sp = (hourly.get("surface_pressure") or [None] * n)[i]
        td2 = (hourly.get("dew_point_2m") or [None] * n)[i]
        prof = []
        if sp is not None and td2 is not None:
            prof.append((sp, _q_from_td(td2, sp)))
        for lev in LEVELS:
            if sp is not None and lev >= sp:
                continue
            td = (hourly.get(f"dew_point_{lev}hPa") or [None] * n)[i]
            if td is not None:
                prof.append((float(lev), _q_from_td(td, lev)))
        if len(prof) < 3:
            out.append(None)
            continue
        prof.sort(reverse=True)                            # surface first
        pw = sum((q1 + q2) / 2 * (p1 - p2) * 100 / 9.80665
                 for (p1, q1), (p2, q2) in zip(prof, prof[1:]))
        out.append(round(pw, 2))
    return out


def _ensure_pw(data):
    """The historical archive has no PW: integrate it from the levels (also for older cache files)."""
    h = data["hourly"]
    if not any(v is not None for v in h.get("total_column_integrated_water_vapour") or []):
        h["total_column_integrated_water_vapour"] = derive_pw(h)
        data["_pw_derived"] = True
    return data


def upper_hist_path(loc, view):
    return os.path.join(CACHE_DIR, f"hourly{CLIM_VERSION}_{loc['lat']:.3f}_{loc['lon']:.3f}_{view.isoformat()}.json")


def get_upper(loc, view=None, today=None):
    """3 days of hourly surface + pressure-level fields (always metric).
    view=None: live forecast from now. view=date: that day 00:00 local + 2 more days, from the
    historical-forecast archive (cached permanently once the whole span is in the past)."""
    base = {"latitude": f"{loc['lat']:.4f}", "longitude": f"{loc['lon']:.4f}", "timezone": "auto",
            "wind_speed_unit": "kmh"}
    if view is None:
        data = _fetch_upper("https://api.open-meteo.com/v1/forecast?", {**base, "forecast_days": 3})
        data["_fetched"] = time.time()
        return data
    today = today or dt.date.today()
    if view < HIST_HOURLY_START:
        raise HourlyUnavailable(f"hourly archive starts {HIST_HOURLY_START:%b %Y}")
    end = min(view + dt.timedelta(days=2), today)
    path = upper_hist_path(loc, view)
    try:
        with open(path) as f:
            return _ensure_pw(json.load(f))
    except (OSError, ValueError):
        pass
    data = _fetch_upper("https://historical-forecast-api.open-meteo.com/v1/forecast?",
                        {**base, "start_date": view.isoformat(), "end_date": end.isoformat()})
    t2 = data.get("hourly", {}).get("temperature_2m") or []
    if not any(v is not None for v in t2):
        raise HourlyUnavailable("no hourly archive for this date")
    _ensure_pw(data)
    data["_fetched"] = time.time()
    data["_view"] = view.isoformat()
    if end < today:                    # complete: cache permanently
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    return data


# ---------------------------------------------------------------- climate drawing
def tint(c, a):
    return tuple(int(BG[i] + (c[i] - BG[i]) * a) for i in range(3))


def dashed(d, pts, color, width=2, on=4, off=3):
    pos, draw_on = 0.0, True
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        t = 0.0
        while t < seg:
            lim = on if draw_on else off
            step = min(lim - pos, seg - t)
            if draw_on:
                fa, fb = t / seg, (t + step) / seg
                d.line([(x0 + (x1 - x0) * fa, y0 + (y1 - y0) * fa),
                        (x0 + (x1 - x0) * fb, y0 + (y1 - y0) * fb)], fill=color, width=width)
            t += step
            pos += step
            if pos >= lim - 1e-9:
                pos, draw_on = 0.0, not draw_on


class DayPlot(Plot):
    """Plot over daily dates (datetime at midnight), labelled on the 1st and 15th
    (labels='mid') or on month starts only (labels='month')."""

    def __init__(self, d, box, times, ylo, yhi, labels="mid"):
        super().__init__(d, box, times, ylo, yhi)
        self.labels = labels

    def axes(self, unit=""):
        d = self.d
        for tv in nice_ticks(self.ylo, self.yhi):
            yy = self.y(tv)
            d.line([(self.x0, yy), (self.x1, yy)], fill=GRID)
            lbl = fmt_tick(tv)
            d.text((self.x0 - 3 - text_w(d, lbl, F_XS), yy - 6), lbl, font=F_XS, fill=MUTED)
        if unit:
            d.text((2, self.y0 - 15), unit, font=F_XS, fill=MUTED)
        days = (1,) if self.labels == "month" else (1, 15)
        for t in self.times:
            if t.day in days:
                xx = self.x(t)
                d.line([(xx, self.y1), (xx, self.y1 + 3)], fill=MUTED)
                lbl = (t.strftime("%b")[0] if self.labels == "month" else
                       f"{t:%b} {t.day}" if t.day == 1 else "15")
                if self.x1 - xx > 10:
                    d.text((xx - text_w(d, lbl, F_XS) / 2, self.y1 + 2), lbl, font=F_XS,
                           fill=FG if t.day == 1 else MUTED)
        d.rectangle(self.box, outline=GRID)

    def band(self, lo, hi, color):
        top = [(self.x(t), self.y(v)) for t, v in zip(self.times, hi)]
        bot = [(self.x(t), self.y(v)) for t, v in zip(self.times, lo)]
        self.d.polygon(top + bot[::-1], fill=color)

    def series(self, vals, srcs, color, width=2):
        """Solid where src == 'obs', dashed where it's model/forecast (sharing the joint)."""
        pts = [(self.x(t), self.y(v), s) for t, v, s in zip(self.times, vals, srcs) if v is not None]
        obs = [(x, y) for x, y, s in pts if s == "obs"]
        if len(obs) > 1:
            self.d.line(obs, fill=color, width=width, joint="curve")
        fc_start = next((i for i, p in enumerate(pts) if p[2] != "obs"), None)
        if fc_start is not None:
            seg = [(x, y) for x, y, _ in pts[max(fc_start - 1, 0):]]
            if len(seg) > 1:
                dashed(self.d, seg, color, width=width)


CLIM_BOX = (30, 32, W - 6, H - 28)


def recent_window(rec, back=45):
    today = dt.date.fromisoformat(rec["today"])
    lo = today - dt.timedelta(days=back)
    return [r for r in rec["days"] if dt.date.fromisoformat(r["date"]) >= lo], today


def cunits(clim):
    """Unit context for screens drawn from the climatology (its own units, not the forecast's)."""
    return {"_metric": bool(clim.get("metric", False))}


SRC_WORD = {"obs": "reanalysis", "fc": "forecast"}


def src_note(rows):
    """Left footer: what the solid/dashed lines are."""
    return "solid reanalysis · dashed forecast" if any(r["src"] == "fc" for r in rows) else "reanalysis (ERA5)"


def clim_footer(d, rows, rec):
    d.text((4, H - 12), src_note(rows), font=F_XS, fill=MUTED)
    if not rec.get("view_past") and rec.get("last_obs"):
        lo = dt.date.fromisoformat(rec["last_obs"])
        msg = f"reanalysis thru {lo:%b} {lo.day}"
        d.text((W - 3 - text_w(d, msg, F_XS), H - 12), msg, font=F_XS, fill=MUTED)


def view_tag(rec):
    """Header date tag for climate screens, e.g. '23 Jul 2021' (gold) or 'today 24 Nov' (gray)."""
    if not rec:
        return ""
    v = dt.date.fromisoformat(rec["today"])
    if rec.get("view_past"):
        return date_tag(v)
    return f"today {v.day} {v:%b}"


def screen_clim_temp(img, data, loc, status, idx, n, clim=None, rec=None):
    d = ImageDraw.Draw(img)
    header(d, "Clim T", loc["name"], data, idx, n, tag=view_tag(rec))
    rows, today = recent_window(rec)
    dates = [dt.date.fromisoformat(r["date"]) for r in rows]
    times = [dt.datetime.combine(x, dt.time()) for x in dates]
    k = [doy366(x) for x in dates]
    cx, cn = clim["tmax"], clim["tmin"]
    allv = ([cx["p90"][i] for i in k] + [cn["p10"][i] for i in k] +
            [r["tmax"] for r in rows] + [r["tmin"] for r in rows])
    lo, hi = nice_range(allv, min_span=10, pad_frac=0.04)
    p = DayPlot(d, CLIM_BOX, times, lo, hi)
    p.band([cx["p10"][i] for i in k], [cx["p90"][i] for i in k], tint(TEMP, .28))
    p.band([cn["p10"][i] for i in k], [cn["p90"][i] for i in k], tint(DEW, .28))
    p.axes(u_temp(cunits(clim)))
    p.line([cx["p50"][i] for i in k], tint(TEMP, .6), width=1)
    p.line([cn["p50"][i] for i in k], tint(DEW, .6), width=1)
    p.now_marker(dt.datetime.combine(today, dt.time()))
    srcs = [r["src"] for r in rows]
    p.series([r["tmin"] for r in rows], srcs, DEW)
    p.series([r["tmax"] for r in rows], srcs, TEMP)
    legend(d, [("Tmax", TEMP), ("Tmin", DEW), ("band p10–p90", tint(FG, .35))])
    clim_footer(d, rows, rec)

    def pct3(c, j, nd=0):
        return f"p10/50/90 {fv(c['p10'][j], nd)}/{fv(c['p50'][j], nd)}/{fv(c['p90'][j], nd)}"
    anchor = next((j for j, x in enumerate(dates) if x >= today), 0)
    set_probe(times, p.x, CLIM_BOX[1], CLIM_BOX[3], lambda i: (
        [(f"Tmax {fv(rows[i]['tmax'], 0, '°')}", TEMP), (pct3(cx, k[i]), tint(TEMP, .8))],
        [(f"Tmin {fv(rows[i]['tmin'], 0, '°')}", DEW), (pct3(cn, k[i]), tint(DEW, .8)),
         (SRC_WORD[rows[i]["src"]], MUTED)]), anchor=anchor, hourly=False)


def wy_series(rec):
    today = dt.date.fromisoformat(rec["today"])
    s0 = wy_start(today)
    rows = [r for r in rec["days"] if dt.date.fromisoformat(r["date"]) >= s0]
    cum, tot = [], 0.0
    for r in rows:
        tot += r["pr"] or 0.0
        cum.append(tot)
    return s0, today, rows, cum


def screen_clim_wy(img, data, loc, status, idx, n, clim=None, rec=None):
    d = ImageDraw.Draw(img)
    header(d, "WY Precip", loc["name"], data, idx, n, tag=view_tag(rec))
    s0, today, rows, cum = wy_series(rec)
    end = max(dt.date.fromisoformat(rows[-1]["date"]), s0 + dt.timedelta(days=75))
    ndays = (end - s0).days + 1
    times = [dt.datetime.combine(s0 + dt.timedelta(days=i), dt.time()) for i in range(ndays)]
    wy = clim["wy"]
    hi = max(max(wy["p90"][:ndays]), max(cum) if cum else 0) * 1.08 or 1
    p = DayPlot(d, CLIM_BOX, times, 0, hi, labels="month" if ndays > 120 else "mid")
    p.band(wy["p10"][:ndays], wy["p90"][:ndays], tint(PRECIP, .28))
    p.axes(u_precip(cunits(clim)))
    p.line(wy["p50"][:ndays], tint(FG, .55), width=1)
    p.now_marker(dt.datetime.combine(today, dt.time()))
    rt = [dt.datetime.combine(dt.date.fromisoformat(r["date"]), dt.time()) for r in rows]
    sub = DayPlot(d, CLIM_BOX, times, 0, hi)
    pts = [(sub.x(t), sub.y(v), r["src"]) for t, v, r in zip(rt, cum, rows)]
    obs = [(x, y) for x, y, s in pts if s == "obs"]
    if len(obs) > 1:
        d.line(obs, fill=PRECIP, width=2)
    i0 = next((i for i, q in enumerate(pts) if q[2] != "obs"), None)
    if i0 is not None:
        dashed(d, [(x, y) for x, y, _ in pts[max(i0 - 1, 0):]], PRECIP)
    i_today = (today - s0).days
    so_far = sum(r["pr"] for r in rows if dt.date.fromisoformat(r["date"]) < today)
    avg = wy["mean"][max(i_today - 1, 0)]
    pct = f" · {so_far / avg * 100:.0f}% of avg" if avg > 0.01 else ""
    fmt = "{:.1f}" if clim.get("metric") else "{:.2f}"
    legend(d, [(f"WY {fmt.format(so_far)} {u_precip(cunits(clim))}{pct}", PRECIP), ("median", tint(FG, .55))])
    d.text((4, H - 12), f"since Oct 1 · band p10–p90 {clim['base']}", font=F_XS, fill=MUTED)
    nd = 1 if clim.get("metric") else 2
    cum_by_day = {r["date"]: c for r, c in zip(rows, cum)}
    src_by_day = {r["date"]: r["src"] for r in rows}

    def wy_lines(i):
        day = times[i].date().isoformat()
        v = cum_by_day.get(day)
        return ([(f"WY {fv(v, nd)} {u_precip(cunits(clim))}", PRECIP),
                 (SRC_WORD.get(src_by_day.get(day), "") if v is not None else "", MUTED)],
                [(f"p10/50/90 {fv(wy['p10'][i], nd)}/{fv(wy['p50'][i], nd)}/{fv(wy['p90'][i], nd)}", FG),
                 (f"avg {fv(wy['mean'][i], nd)}", MUTED)])
    set_probe(times, p.x, CLIM_BOX[1], CLIM_BOX[3], wy_lines,
              anchor=(today - s0).days, hourly=False)


def rank_pct(value, sample):
    below = sum(1 for v in sample if v < value)
    ties = sum(1 for v in sample if v == value)
    return 100 * (below + 0.5 * ties) / len(sample)


def anomaly_summary(clim, rec):
    today = dt.date.fromisoformat(rec["today"])
    past = [r for r in rec["days"] if dt.date.fromisoformat(r["date"]) < today][-30:]
    nxt = [r for r in rec["days"] if dt.date.fromisoformat(r["date"]) >= today][:7]

    def anom(rows, key):
        v = [r[key] - clim[key]["mean"][doy366(dt.date.fromisoformat(r["date"]))] for r in rows]
        return sum(v) / len(v) if v else None

    out = {"n30": len(past), "tx30": anom(past, "tmax"), "tn30": anom(past, "tmin"),
           "tx7f": anom(nxt, "tmax"), "tn7f": anom(nxt, "tmin")}
    out["p90_days"] = sum(1 for r in past
                          if r["tmax"] > clim["tmax"]["p90"][doy366(dt.date.fromisoformat(r["date"]))])
    out["p10_days"] = sum(1 for r in past
                          if r["tmin"] < clim["tmin"]["p10"][doy366(dt.date.fromisoformat(r["date"]))])
    pr30 = sum(r["pr"] for r in past)
    norm30 = sum(clim["pr_mean"][doy366(dt.date.fromisoformat(r["date"]))] for r in past)
    out.update(pr30=pr30, pr30_pct=(pr30 / norm30 * 100) if norm30 > 0.005 else None)
    s0, _, rows, _ = wy_series(rec)
    i = (today - s0).days
    wy_tot = sum(r["pr"] for r in rows if dt.date.fromisoformat(r["date"]) < today)
    wy_avg = clim["wy"]["mean"][max(i - 1, 0)]
    out.update(wy=wy_tot, wy_pct=(wy_tot / wy_avg * 100) if wy_avg > 0.01 else None,
               wy_rank=rank_pct(wy_tot, [c[max(i - 1, 0)] for c in clim["wy_curves"]]))
    out["fc7_pr"] = sum(r["pr"] for r in nxt)
    return out


def screen_clim_anom(img, data, loc, status, idx, n, clim=None, rec=None):
    d = ImageDraw.Draw(img)
    header(d, "Anomalies", loc["name"], data, idx, n, tag=view_tag(rec))
    a = anomaly_summary(clim, rec)
    deg = "°"
    pu = u_precip(cunits(clim))
    pf = "{:.1f}" if clim.get("metric") else "{:.2f}"

    def sgn(v):
        return "--" if v is None else f"{round(v, 1) + 0.0:+.1f}{deg}"

    def col(v):
        return MUTED if v is None else TEMP if v > 0.5 else DEW if v < -0.5 else FG

    y = 20
    d.text((4, y), f"Last {a['n30']} days", font=F_MB, fill=FG)
    y += 18
    d.text((10, y), "Tmax", font=F_S, fill=MUTED)
    d.text((48, y), sgn(a["tx30"]), font=F_MB, fill=col(a["tx30"]))
    d.text((112, y), "Tmin", font=F_S, fill=MUTED)
    d.text((150, y), sgn(a["tn30"]), font=F_MB, fill=col(a["tn30"]))
    d.text((216, y), f">p90 Tx: {a['p90_days']}", font=F_S, fill=TEMP if a["p90_days"] else MUTED)
    y += 17
    pct = f"  ({a['pr30_pct']:.0f}% of avg)" if a["pr30_pct"] is not None else "  (normally dry)"
    d.text((10, y), "Precip", font=F_S, fill=MUTED)
    d.text((56, y), f"{pf.format(a['pr30'])} {pu}{pct}", font=F_S, fill=PRECIP)
    y += 21
    d.text((4, y), "Water year (Oct 1–)", font=F_MB, fill=FG)
    y += 18
    wp = f" · {a['wy_pct']:.0f}% of avg" if a["wy_pct"] is not None else ""
    d.text((10, y), f"{pf.format(a['wy'])} {pu}{wp} · pctile {a['wy_rank']:.0f}",
           font=F_S, fill=PRECIP)
    y += 21
    d.text((4, y), "Following 7 days" if rec.get("view_past") else "Next 7 days", font=F_MB, fill=FG)
    y += 18
    d.text((10, y), "Tmax", font=F_S, fill=MUTED)
    d.text((48, y), sgn(a["tx7f"]), font=F_MB, fill=col(a["tx7f"]))
    d.text((112, y), "Tmin", font=F_S, fill=MUTED)
    d.text((150, y), sgn(a["tn7f"]), font=F_MB, fill=col(a["tn7f"]))
    d.text((216, y), f"P {pf.format(a['fc7_pr'])} {pu}", font=F_S, fill=PRECIP)
    lo = rec.get("last_obs") or "--"
    tail = "" if rec.get("view_past") else f" · obs thru {lo[5:]}"
    d.text((4, H - 12), f"vs {clim['base']} {clim.get('model', 'ERA5')}{tail}", font=F_XS, fill=MUTED)




# ---------------------------------------------------------------- unit helpers (upper data is metric)
def disp_t(c, metric):
    return None if c is None else (c if metric else c * 9 / 5 + 32)


def disp_wind(kmh, metric):
    return None if kmh is None else (kmh if metric else kmh / 1.609344)


def disp_pw(mm, metric):
    return None if mm is None else (mm if metric else mm / 25.4)


def upper_window(upper, now, hours=72):
    """Times, a getter and the 'now' marker for the hourly screens. Live data starts an hour
    before now (index 1 = current hour); archived data starts at 00:00 of the view date."""
    h = upper["hourly"]
    times = parse_times(h["time"])
    if upper.get("_view"):
        i0, marker = 0, None
    else:
        now_h = now.replace(minute=0, second=0, microsecond=0)
        i0 = next((i for i, t in enumerate(times) if t >= now_h - dt.timedelta(hours=1)), 0)
        marker = now
    i1 = min(len(times), i0 + hours)
    return times[i0:i1], (lambda key: (h.get(key) or [None] * len(times))[i0:i1]), marker


def hourly_panels(d, times, now, panels, shade=None, x0=34, top=19, bottom=H - 13):
    """Stacked hourly panels sharing an x axis. panels: dicts with vals, color, label,
    and optional lo/hi, kind ('line'|'bars'), ref=(value, text), vals2/color2."""
    n = len(panels)
    gap = 3
    ph = (bottom - top - 10 - gap * (n - 1)) / n
    for j, pnl in enumerate(panels):
        y0 = top + j * (ph + gap)
        box = (x0, y0, W - 4, y0 + ph)
        vals = [v for v in pnl["vals"] + pnl.get("vals2", []) if v is not None]
        lo = pnl.get("lo", min(vals) if vals else 0)
        hi = pnl.get("hi", max(vals) if vals else 1)
        if pnl.get("ref") is not None:
            lo, hi = min(lo, pnl["ref"][0]), max(hi, pnl["ref"][0])
        if hi - lo < pnl.get("min_span", 1e-6):
            hi = lo + pnl.get("min_span", 1)
        pad = (hi - lo) * 0.08
        lo2 = lo if "lo" in pnl else lo - pad
        p = Plot(d, box, times, lo2, hi + pad)
        if shade:
            hw = (box[2] - box[0]) / max(len(times) - 1, 1) / 2 + 0.5   # half an hour wide: no gaps
            for t, s in zip(times, shade):
                if s:
                    xx = p.x(t)
                    d.rectangle([xx - hw, box[1], xx + hw, box[3]], fill=(70, 22, 26))
        for t in times:
            if t.hour == 0:
                xx = p.x(t)
                d.line([(xx, box[1]), (xx, box[3])], fill=GRID)
                if j == n - 1:
                    d.text((xx + 2, box[3] + 1), t.strftime("%a"), font=F_XS, fill=FG)
        if pnl.get("ref") is not None:
            yy = p.y(pnl["ref"][0])
            dashed(d, [(box[0], yy), (box[2], yy)], MUTED, width=1, on=2, off=2)
            ty = yy - 11 if yy - box[1] > 12 else yy + 1
            d.text((box[2] - text_w(d, pnl["ref"][1], F_XS) - 2, ty), pnl["ref"][1],
                   font=F_XS, fill=MUTED)
        if pnl.get("kind") == "bars":
            p.bars(pnl["vals"], pnl["color"])
        else:
            if pnl.get("vals2"):
                p.line(pnl["vals2"], pnl["color2"], width=1)
            p.line(pnl["vals"], pnl["color"])
        p.now_marker(now)
        d.rectangle(box, outline=GRID)
        d.text((2, y0), pnl["label"], font=F_XS, fill=pnl["color"])
        d.text((2, y0 + 11), fmt_tick(hi), font=F_XS, fill=MUTED)
        if ph > 30:
            d.text((2, y0 + ph - 11), fmt_tick(lo2 if "lo" in pnl else lo), font=F_XS, fill=MUTED)


def upper_tag(upper, data):
    v = upper.get("_view") if upper else None
    return date_tag(dt.date.fromisoformat(v)) if v else today_tag(data)


def _now(data):
    return dt.datetime.fromisoformat(data["current"]["time"])


def _first_valid(seq):
    return next((v for v in seq if v is not None), None)


def screen_monsoon(img, data, loc, status, idx, n, upper=None, **_):
    d = ImageDraw.Draw(img)
    header(d, "Monsoon", loc["name"], data, idx, n, tag=upper_tag(upper, data))
    m = data["_metric"]
    times, get, marker = upper_window(upper, _now(data))
    td = [disp_t(v, m) for v in get("dew_point_2m")]
    pw = [disp_pw(v, m) for v in get("total_column_integrated_water_vapour")]
    cape = get("cape")
    panels = [{"vals": td, "color": DEW, "label": "Td " + u_temp(data),
               "ref": (12.0 if m else 54.0, "12°C" if m else "54°F"), "min_span": 5}]
    if any(v is not None for v in pw):
        panels.append({"vals": pw, "color": PRECIP,
                       "label": ("PW* " if upper.get("_pw_derived") else "PW ") + ("mm" if m else "in"),
                       "lo": 0, "ref": (25.4 if m else 1.0, "25mm" if m else "1 in")})
    panels.append({"vals": cape, "color": TEMP, "label": "CAPE", "lo": 0, "kind": "bars",
                   "min_span": 100})
    hourly_panels(d, times, marker, panels)
    ax = Plot(d, (34, 19, W - 4, H - 23), times, 0, 1)
    pwl = "PW*" if upper.get("_pw_derived") else "PW"

    def mon_lines(i):
        return ([(f"Td {fv(td[i], 0, '°')}", DEW),
                 (f"{pwl} {fv(pw[i], 1 if m else 2)}", PRECIP),
                 (f"CAPE {fv(cape[i])}", TEMP)], None)
    mon_anchor = 1 if marker is not None else 0
    cmax = max([c for c in cape if c is not None] or [0])
    if marker is not None:                       # live: values now, CAPE max ahead
        td_v, pw_v, pre = _first_valid(td[1:] or td), _first_valid(pw[1:] or pw), "now "
    else:                                        # past date: maxima over the 3 days
        td_v = max([v for v in td if v is not None], default=None)
        pw_v = max([v for v in pw if v is not None], default=None)
        pre = "max "
    txt = pre + (f"Td {td_v:.0f}°" if td_v is not None else "Td --")
    if pw_v is not None:
        txt += f" · PW{'*' if upper.get('_pw_derived') else ''} {pw_v:.{1 if m else 2}f}"
    txt += f" · {'max ' if marker is not None else ''}CAPE {cmax:.0f}"
    set_probe(times, ax.x, 19, H - 23, mon_lines, anchor=mon_anchor, one_line=True)
    d.text((4, H - 12), txt, font=F_XS, fill=MUTED)


# fire-weather "critical" hours, as an illustrative screen (not an official Red Flag criterion)
FIRE_RH, FIRE_WIND_MPH, FIRE_GUST_MPH = 15, 20, 35


def screen_fire(img, data, loc, status, idx, n, upper=None, **_):
    d = ImageDraw.Draw(img)
    header(d, "Fire Wx", loc["name"], data, idx, n, tag=upper_tag(upper, data))
    m = data["_metric"]
    times, get, marker = upper_window(upper, _now(data))
    rh, vpd = get("relative_humidity_2m"), get("vapour_pressure_deficit")
    ws_k, wg_k = get("wind_speed_10m"), get("wind_gusts_10m")
    ws = [disp_wind(v, m) for v in ws_k]
    wg = [disp_wind(v, m) for v in wg_k]
    crit = [r is not None and s is not None and g is not None and r <= FIRE_RH and
            (s / 1.609344 >= FIRE_WIND_MPH or g / 1.609344 >= FIRE_GUST_MPH)
            for r, s, g in zip(rh, ws_k, wg_k)]
    hourly_panels(d, times, marker, [
        {"vals": rh, "color": DEW, "label": "RH %", "lo": 0, "ref": (FIRE_RH, f"{FIRE_RH}%"),
         "min_span": 20},
        {"vals": ws, "vals2": wg, "color": WIND, "color2": GUST, "label": u_wind(data),
         "lo": 0, "min_span": 10},
        {"vals": vpd, "color": TEMP, "label": "VPD", "lo": 0, "min_span": 1},
    ], shade=crit)
    rhmin = min([r for r in rh if r is not None] or [0])
    gmax = max([g for g in wg if g is not None] or [0])
    nc = sum(crit)
    msg = f"RH min {rhmin:.0f}% · gust max {gmax:.0f} · crit hrs {nc}"
    ax = Plot(d, (34, 19, W - 4, H - 23), times, 0, 1)
    set_probe(times, ax.x, 19, H - 23, lambda i: (
        [(f"RH {fv(rh[i], 0, '%')}", DEW), (f"{fv(ws[i])} g{fv(wg[i])} {u_wind(data)}", WIND),
         (f"VPD {fv(vpd[i], 1)}", TEMP), ("CRIT" if crit[i] else "", ALERT)], None),
        anchor=1 if marker is not None else 0, one_line=True)
    d.text((4, H - 12), msg, font=F_XS, fill=ALERT if nc else MUTED)


def screen_sounding(img, data, loc, status, idx, n, upper=None, off=0, **_):
    """Skew-T style profile at the selected hour (°C, kt), stepped with up/down."""
    d = ImageDraw.Draw(img)
    header(d, "Sounding", loc["name"], data, idx, n, tag=upper_tag(upper, data))
    m = data["_metric"]
    times, get, marker = upper_window(upper, _now(data), hours=72)
    first = 0 if marker is None else 1                # live: index 1 is the current hour
    i = min(max(first + off, 0), len(times) - 1)
    t_valid = times[i]
    sfc_p = get("surface_pressure")[i] or 1013.0
    pts_t, pts_d = [], []
    t2, d2 = get("temperature_2m")[i], get("dew_point_2m")[i]
    if t2 is not None:
        pts_t.append((sfc_p, t2))
    if d2 is not None:
        pts_d.append((sfc_p, d2))
    for p in LEVELS:
        if p >= sfc_p:
            continue
        tv, dv = get(f"temperature_{p}hPa")[i], get(f"dew_point_{p}hPa")[i]
        if tv is not None:
            pts_t.append((p, tv))
        if dv is not None:
            pts_d.append((p, dv))

    # skew-T canvas, drawn on its own image so lines clip to the box
    bx0, by0, bx1, by1 = 3, 18, 214, H - 14
    cw, ch = bx1 - bx0, by1 - by0
    sk = Image.new("RGB", (cw, ch), BG)
    s = ImageDraw.Draw(sk)
    pbot, ptop = 1050.0, 200.0
    lb, lt = math.log(pbot), math.log(ptop)

    def fy(p):
        return (math.log(p) - lt) / (lb - lt) * ch

    def fx(tc, p):
        frac = (lb - math.log(p)) / (lb - lt)
        return (tc + 40 + 45 * frac) / 85 * cw

    for theta in range(270, 440, 20):                      # dry adiabats
        pts = []
        for p in range(1050, 190, -25):
            tk = theta * (p / 1000) ** 0.2857 - 273.15
            pts.append((fx(tk, p), fy(p)))
        s.line(pts, fill=(34, 40, 30))
    for tc in range(-100, 50, 10):                         # isotherms
        s.line([(fx(tc, pbot), fy(pbot)), (fx(tc, ptop), fy(ptop))],
               fill=(70, 80, 100) if tc == 0 else GRID)
    for p in (850, 700, 500, 300):
        s.line([(0, fy(p)), (cw, fy(p))], fill=GRID)
        s.text((2, fy(p) - 11), str(p), font=F_XS, fill=MUTED)
    s.rectangle([0, fy(sfc_p), cw, ch], fill=(30, 24, 18))  # below ground
    if len(pts_d) > 1:
        s.line([(fx(t, p), fy(p)) for p, t in pts_d], fill=DEW, width=2)
    if len(pts_t) > 1:
        s.line([(fx(t, p), fy(p)) for p, t in pts_t], fill=TEMP, width=2)
    img.paste(sk, (bx0, by0))
    d.rectangle([bx0, by0, bx1, by1], outline=GRID)

    # right column: winds and indices
    x = bx1 + 5
    d.text((x, 18), "hPa   wind kt", font=F_XS, fill=MUTED)
    y = 30
    for p in (850, 700, 500, 300, 250):
        if p >= sfc_p:
            continue
        ws, wd = get(f"wind_speed_{p}hPa")[i], get(f"wind_direction_{p}hPa")[i]
        if ws is None:
            continue
        kt = ws / 1.852
        d.text((x, y), f"{p}", font=F_XS, fill=MUTED)
        d.text((x + 26, y), f"{compass(wd):>3} {kt:.0f}", font=F_XS, fill=WIND if kt < 50 else ALERT)
        y += 12
    y += 4
    pw = get("total_column_integrated_water_vapour")[i]
    cape = get("cape")[i]
    li = get("lifted_index")[i]
    fz = get("freezing_level_height")[i]
    lines = []
    if pw is not None:
        star = "*" if upper.get("_pw_derived") else ""
        lines.append(f"PW{star} {disp_pw(pw, m):.{1 if m else 2}f}{'mm' if m else 'in'}")
    if cape is not None:
        lines.append(f"CAPE {cape:.0f}")
    if li is not None:
        lines.append(f"LI {li:+.1f}")
    if fz is not None:
        lines.append(f"FZL {fz / 1000:.1f}km" if m else f"FZL {fz * 3.28084 / 1000:.1f}kft")
    h500 = get("geopotential_height_500hPa")[i]
    if h500 is not None:
        lines.append(f"500z {h500 / 10:.0f}dm")
    for ln in lines:
        d.text((x, y), ln, font=F_XS, fill=FG)
        y += 12
    hr12 = t_valid.hour % 12 or 12                   # no %-I: Windows strftime rejects it
    lbl = f"{t_valid:%a} {hr12}{'a' if t_valid.hour < 12 else 'p'}"
    rel = f"+{max(off, 0)}h" if marker is not None else f"{t_valid:%b} {t_valid.day}"
    d.text((4, H - 12), f"{lbl} ({rel}) · ↑↓ ±3h · °C", font=F_XS, fill=FG)


# ---------------------------------------------------------------- more climate screens
def screen_gdd(img, data, loc, status, idx, n, clim=None, rec=None, **_):
    d = ImageDraw.Draw(img)
    header(d, "GDD", loc["name"], data, idx, n, tag=view_tag(rec))
    today = dt.date.fromisoformat(rec["today"])
    s0 = dt.date(today.year, 1, 1)
    m = clim.get("metric", False)
    rows = [r for r in rec["days"] if dt.date.fromisoformat(r["date"]) >= s0]
    cum, tot = [], 0.0
    for r in rows:
        tot += gdd_day(r["tmax"], r["tmin"], m)
        cum.append(tot)
    ndays = 366 if (s0.replace(year=s0.year + 1) - s0).days == 366 else 365
    times = [dt.datetime.combine(s0 + dt.timedelta(days=i), dt.time()) for i in range(ndays)]
    g = clim["gdd"]
    hi = max(max(g["p90"][:ndays]), max(cum) if cum else 0) * 1.05 or 1
    p = DayPlot(d, CLIM_BOX, times, 0, hi, labels="month")
    p.band(g["p10"][:ndays], g["p90"][:ndays], tint(TEMP, .28))
    p.axes("GDD")
    p.line(g["p50"][:ndays], tint(FG, .55), width=1)
    p.now_marker(dt.datetime.combine(today, dt.time()))
    rt = [dt.datetime.combine(dt.date.fromisoformat(r["date"]), dt.time()) for r in rows]
    sub = DayPlot(d, CLIM_BOX, rt, 0, hi)
    sub.t0, sub.t1 = times[0], times[-1]
    sub.series(cum, [r["src"] for r in rows], TEMP)
    i = (today - s0).days
    so_far = sum(gdd_day(r["tmax"], r["tmin"], m) for r in rows
                 if dt.date.fromisoformat(r["date"]) < today)
    avg = g["mean"][max(i - 1, 0)]
    pct = f" ({(so_far / avg - 1) * 100:+.0f}% vs avg)" if avg > 1 else ""
    legend(d, [(f"YTD {so_far:,.0f}{pct}", TEMP), ("median", tint(FG, .55))])
    base = "base 10/30°C" if m else "base 50/86°F"
    d.text((4, H - 12), f"since Jan 1 · {base}", font=F_XS, fill=MUTED)
    cum_by_day = {r["date"]: c for r, c in zip(rows, cum)}

    def gdd_lines(i):
        v = cum_by_day.get(times[i].date().isoformat())
        return ([(f"GDD {fv(v)}", TEMP)],
                [(f"p10/50/90 {fv(g['p10'][i])}/{fv(g['p50'][i])}/{fv(g['p90'][i])}", FG),
                 (f"avg {fv(g['mean'][i])}", MUTED)])
    set_probe(times, p.x, CLIM_BOX[1], CLIM_BOX[3], gdd_lines, anchor=(today - s0).days, hourly=False)


UNIT_NAMES = {"mp/h": "mph", "inch": "in", "km/h": "km/h", "MJ/m²": "MJ/m²"}


def nice_unit(u):
    return UNIT_NAMES.get(u, u)


def make_clim_var_screen(var, title):
    meta = EXTRA_DAILY[var]

    def fn(img, data, loc, status, idx, n, clim=None, rec=None, **_):
        d = ImageDraw.Draw(img)
        header(d, title, loc["name"], data, idx, n, tag=view_tag(rec))
        cv = clim.get("vars", {}).get(var)
        if cv is None:
            msg = [f"{meta['label']} climatology", "not available from the",
                   f"{clim.get('model', 'archive')} archive here"]
            for j, ln in enumerate(msg):
                d.text(((W - text_w(d, ln, F_M)) / 2, 60 + 18 * j), ln, font=F_M,
                       fill=FG if j == 0 else MUTED)
            return
        color = globals()[meta["color"]]
        rows, today = recent_window(rec)
        dates = [dt.date.fromisoformat(r["date"]) for r in rows]
        times = [dt.datetime.combine(x, dt.time()) for x in dates]
        k = [doy366(x) for x in dates]
        vals = [r.get(var) for r in rows]
        lo_b = [cv["p10"][i] for i in k]
        hi_b = [cv["p90"][i] for i in k]
        allv = [v for v in vals + lo_b + hi_b if v is not None]
        lo, hi = nice_range(allv, min_span=1e-3, pad_frac=0.06)
        if min(allv or [0]) >= 0:
            lo = max(lo, 0)
        p = DayPlot(d, CLIM_BOX, times, lo, hi)
        p.band([v if v is not None else 0 for v in lo_b], [v if v is not None else 0 for v in hi_b],
               tint(color, .28))
        unit = nice_unit(clim.get("units", {}).get(var, ""))
        p.axes("")                        # unit goes in the legend: long units collide up here
        p.line([cv["p50"][i] for i in k], tint(color, .6), width=1)
        p.now_marker(dt.datetime.combine(today, dt.time()))
        p.series(vals, [r["src"] for r in rows], color)
        past = [(v, cv["mean"][i]) for v, i, r in zip(vals, k, rows)
                if v is not None and cv["mean"][i] is not None and r["src"] == "obs"][-30:]
        an = sum(a - b for a, b in past) / len(past) if past else None
        nd = 2 if (hi - lo) < 5 else 1 if (hi - lo) < 50 else 0
        an_txt = f" · 30d anom {round(an, nd) + 0.0:+.{nd}f}" if an is not None else ""
        lbl = f"{meta['label']} ({unit})" if unit else meta["label"]
        legend(d, [(lbl + an_txt, color), ("p10–p90", tint(FG, .35))])
        clim_footer(d, rows, rec)
        anchor = next((j for j, x in enumerate(dates) if x >= today), 0)
        ul = f" {unit}" if unit else ""

        def var_lines(i):
            m_ = cv["mean"][k[i]]
            an_i = vals[i] - m_ if vals[i] is not None and m_ is not None else None
            return ([(f"{meta['label']} {fv(vals[i], nd)}{ul}", color),
                     (f"anom {an_i:+.{nd}f}" if an_i is not None else "", MUTED)],
                    [(f"p10/50/90 {fv(cv['p10'][k[i]], nd)}/{fv(cv['p50'][k[i]], nd)}/{fv(cv['p90'][k[i]], nd)}", FG),
                     (SRC_WORD[rows[i]["src"]], MUTED)])
        set_probe(times, p.x, CLIM_BOX[1], CLIM_BOX[3], var_lines, anchor=anchor, hourly=False)
    fn.needs = "clim"
    return fn


for _f in (screen_clim_temp, screen_clim_wy, screen_clim_anom, screen_gdd):
    _f.needs = "clim"
for _f in (screen_monsoon, screen_fire, screen_sounding):
    _f.needs = "upper"

# (name, function, section number); digits 1-6 jump to a section
SCREENS = [
    ("Now", None, 1), ("Temp", screen_temp, 1), ("Precip", screen_precip, 1),
    ("Wind", screen_wind, 1), ("7-day", screen_7day, 1), ("Site", screen_site, 1),
    ("Clim T", screen_clim_temp, 2), ("WY Precip", screen_clim_wy, 2),
    ("Anomalies", screen_clim_anom, 2),
    ("Monsoon", screen_monsoon, 3), ("Clim Td", make_clim_var_screen("dew_point_2m_mean", "Clim Td"), 3),
    ("Fire Wx", screen_fire, 4),
    ("Clim RH", make_clim_var_screen("relative_humidity_2m_min", "Clim RHmin"), 4),
    ("Clim VPD", make_clim_var_screen("vapour_pressure_deficit_max", "Clim VPD"), 4),
    ("Clim Gust", make_clim_var_screen("wind_gusts_10m_max", "Clim Gust"), 4),
    ("GDD", screen_gdd, 5),
    ("Clim ET0", make_clim_var_screen("et0_fao_evapotranspiration", "Clim ET0"), 5),
    ("Clim SW", make_clim_var_screen("shortwave_radiation_sum", "Clim SWrad"), 5),
    ("Sounding", screen_sounding, 6),
]
SECTION_NAMES = {1: "Forecast", 2: "Climate", 3: "Monsoon", 4: "Fire", 5: "Ag", 6: "Sounding"}
SOUNDING_IDX = next(i for i, s in enumerate(SCREENS) if s[0] == "Sounding")


def section_start(sec):
    return next(i for i, s in enumerate(SCREENS) if s[2] == sec)


def render(data, loc, idx, status="", clim=None, rec=None, clim_status="",
           upper=None, upper_status="", sound_off=0, view_date=None, inspect=None):
    """inspect: None (off), "anchor" (start at now / the view date) or a cursor index."""
    global PROBE
    PROBE = None
    img = Image.new("RGB", (W, H), BG)
    name, fn, _ = SCREENS[idx]
    needs = getattr(fn, "needs", None)
    tag = date_tag(view_date) if view_date else today_tag(data)
    if idx == 0:
        screen_now(img, data, loc, status, view_date)
    elif fn is screen_site:
        fn(img, data, loc, status, idx, len(SCREENS), clim=clim, upper=upper)
    elif needs == "clim" and (clim is None or rec is None):
        _placeholder(img, data, loc, idx, "Climatology", clim_status or "not loaded yet", tag)
    elif needs == "upper" and upper is None:
        _placeholder(img, data, loc, idx, "Hourly profiles", upper_status or "not loaded yet", tag)
    elif needs == "clim":
        fn(img, data, loc, status, idx, len(SCREENS), clim=clim, rec=rec)
    elif needs == "upper":
        fn(img, data, loc, status, idx, len(SCREENS), upper=upper, off=sound_off)
    else:
        fn(img, data, loc, status, idx, len(SCREENS))
    if inspect is not None and PROBE is not None:
        PROBE["index"] = draw_probe(img, PROBE, inspect)
    return img


def _placeholder(img, data, loc, idx, title, msg, tag=""):
    d = ImageDraw.Draw(img)
    header(d, SCREENS[idx][0], loc["name"], data, idx, len(SCREENS), tag=tag)
    for j, ln in enumerate([title, msg]):
        d.text(((W - text_w(d, ln, F_M)) / 2, 70 + 20 * j), ln, font=F_M,
               fill=FG if j == 0 else MUTED)

def render_message(lines, color=FG):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = H / 2 - len(lines) * 9
    for ln in lines:
        d.text(((W - text_w(d, ln, F_M)) / 2, y), ln, font=F_M, fill=color)
        y += 18
    return img


HELP_KEYS = [
    ("← →", "previous / next screen  (fn+Z / fn+C)"),
    ("1 – 6", "Forecast · Climate · Monsoon · Fire · Ag · Sounding"),
    ("l", "change location (name or lat, lon)"),
    ("d", "view a past date (YYYY-MM-DD; Enter = today)"),
    ("", "applies to all but Forecast; hourly pages 2022+"),
    ("b n", "step the date back / forward a week (also - =)"),
    ("Enter / i", "cursor + values on plots; arrows move it, Enter exits"),
    ("↑ ↓", "Sounding: step time ±3 h  (fn+F / fn+X)"),
    ("u  r", "°F/°C · refresh"),
    ("q", "quit (or hold Esc 2 s)"),
]


def render_help(loc, view_date):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 15], fill=PANEL)
    d.text((4, 1), "Keys", font=F_MB, fill=FG)
    tip = f"v{APP_VERSION} · any key closes"
    d.text((W - 4 - text_w(d, tip, F_XS), 3), tip, font=F_XS, fill=MUTED)
    y = 18
    for k, what in HELP_KEYS:
        d.text((6, y), k, font=F_S, fill=(255, 210, 120))
        f = F_S if text_w(d, what, F_S) <= W - 62 else F_XS   # long lines drop to the small font
        d.text((58, y + (0 if f is F_S else 1)), what, font=f, fill=FG)
        y += 13
    vd = view_date.isoformat() if view_date else "today"
    d.text((4, H - 13), f"{loc['name'][:24]} ({loc['lat']:.2f}, {loc['lon']:.2f}) · date: {vd}",
           font=F_XS, fill=MUTED)
    return img


def parse_date(text):
    """'2021-07-23', '20210723', '7/23/2021' -> date, else None."""
    t = text.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def render_date_entry(text, current, msg=""):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 15], fill=PANEL)
    d.text((4, 1), "View date", font=F_MB, fill=FG)
    cur = current.isoformat() if current else "today"
    d.text((W - 4 - text_w(d, "now: " + cur, F_XS), 3), "now: " + cur, font=F_XS, fill=MUTED)
    d.rectangle([4, 22, W - 4, 44], outline=MUTED)
    d.text((8, 25), (text or "") + "_", font=F_L, fill=FG)
    tips = ["YYYY-MM-DD from 1940 on · Enter alone = today",
            "Climate, Clim, GDD: any date (1991–2020 from cache)",
            "Monsoon, Fire Wx, Sounding: 2022 onward",
            "Forecast pages always show today",
            "b / n (or - / =) step a week when viewing"]
    for j, ln in enumerate(tips):
        d.text((8, 52 + 16 * j), ln, font=F_S, fill=MUTED)
    if msg:
        d.text((4, H - 12), msg, font=F_XS, fill=ALERT)
    return img


def render_search(query, results, sel, msg=""):  # noqa: C901
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 15], fill=PANEL)
    d.text((4, 1), "Find location", font=F_MB, fill=FG)
    d.rectangle([4, 20, W - 4, 40], outline=MUTED)
    d.text((8, 23), query + "_", font=F_M, fill=FG)
    y = 46
    for i, r in enumerate(results):
        if i == sel:
            d.rectangle([4, y - 1, W - 4, y + 16], fill=PANEL)
        d.text((8, y), r["name"][:44], font=F_S, fill=FG if i == sel else MUTED)
        y += 18
    d.text((4, H - 12), msg or "place name or lat, lon · Enter · Esc cancels", font=F_XS, fill=MUTED)
    return img


# ---------------------------------------------------------------- displays
def pack_rgb565(img, bgr=False):
    """RGB image -> little-endian RGB565 bytes for a 16-bit framebuffer. (Pillow has no
    RGB->RGB565 packer, so the bits are combined with ImageMath; ~1 ms per frame on a laptop.)"""
    from PIL import ImageMath
    r, g, b = img.split()
    if bgr:
        r, b = b, r
    out = ImageMath.lambda_eval(lambda a: ((a["r"] >> 3) << 11) | ((a["g"] >> 2) << 5) | (a["b"] >> 3),
                                r=r, g=g, b=b)
    return out.convert("I;16").tobytes("raw", "I;16")


def default_fb():
    """The CardputerZero exposes its LCD as /dev/fb_lcd (-> fb0)."""
    return "/dev/fb_lcd" if os.path.exists("/dev/fb_lcd") else "/dev/fb0"


PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "cz-weather.pid")


def _is_cz_weather(pid):
    """True only for a Python interpreter running cz_weather.py as its script, e.g.
    'python3 /usr/share/APPLaunch/bin/cz_weather.py --fb ...'. Shells, editors or pgrep whose
    command lines merely mention the file never match."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return False
    if len(argv) < 2 or not os.path.basename(argv[0]).startswith("python"):
        return False
    script = next((a for a in argv[1:] if not a.startswith("-")), "")
    return os.path.basename(script) == "cz_weather.py"


def take_over_screen(pidfile=PIDFILE, wait_s=3.0):
    """Only one copy may draw on the LCD: stop any older cz-weather (SIGTERM, then SIGKILL), then
    record our pid. Two copies would take turns overwriting the screen."""
    import signal
    others = set()
    try:
        with open(pidfile) as f:
            others.add(int(f.read().strip() or 0))
    except (OSError, ValueError):
        pass
    try:                                   # also catch copies started without the pidfile
        for d in os.listdir("/proc"):
            if d.isdigit():
                others.add(int(d))
    except OSError:
        pass
    me = os.getpid()
    victims = [p for p in others if p and p != me and _is_cz_weather(p)]
    for p in victims:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + wait_s
    while time.time() < end and any(_is_cz_weather(p) for p in victims):
        time.sleep(0.1)
    for p in victims:
        if _is_cz_weather(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    try:
        with open(pidfile, "w") as f:
            f.write(str(me))
    except OSError:
        pass
    return victims


def release_screen(pidfile=PIDFILE):
    try:
        with open(pidfile) as f:
            if int(f.read().strip() or 0) == os.getpid():
                os.remove(pidfile)
    except (OSError, ValueError):
        pass


class FramebufferDisplay:
    def __init__(self, dev="/dev/fb0", bgr=False):
        dev = os.path.realpath(dev)                  # /dev/fb_lcd -> /dev/fb0 for the sysfs lookup
        self.dev, self.bgr = dev, bgr
        name = os.path.basename(dev)
        sysd = f"/sys/class/graphics/{name}"
        self.bpp = int(open(f"{sysd}/bits_per_pixel").read())
        self.fw, self.fh = map(int, open(f"{sysd}/virtual_size").read().strip().split(","))
        try:
            self.stride = int(open(f"{sysd}/stride").read())
        except OSError:
            self.stride = self.fw * self.bpp // 8
        self.fb = open(dev, "r+b", buffering=0)

    def show(self, img):
        if img.size != (self.fw, self.fh):
            canvas = Image.new("RGB", (self.fw, self.fh), BG)
            canvas.paste(img.crop((0, 0, min(W, self.fw), min(H, self.fh))), (0, 0))
            img = canvas
        if self.bpp == 16:
            raw = pack_rgb565(img, self.bgr)
            row = self.fw * 2
        else:
            raw = img.convert("RGBA").tobytes("raw", "BGRA")
            row = self.fw * 4
        if self.stride == row:
            self.fb.seek(0)
            self.fb.write(raw)
        else:
            for y in range(self.fh):
                self.fb.seek(y * self.stride)
                self.fb.write(raw[y * row:(y + 1) * row])


# ---------------------------------------------------------------- input
# Normalised key names: left right up down enter esc backspace space, or a single char.
ESC_HOLD_S = 2.0          # hold Esc this long to quit (the CardputerZero launcher convention)


class EscHold:
    """Turns Esc press/release timing into a 'quit' once Esc has been held ESC_HOLD_S."""

    def __init__(self, hold=ESC_HOLD_S):
        self.hold, self.since, self.fired = hold, None, False

    def down(self, t):
        self.since, self.fired = t, False

    def up(self, t):
        self.since = None

    def poll(self, t):
        if self.since is not None and not self.fired and t - self.since >= self.hold:
            self.fired = True
            return "quit"
        return None


def evdev_keys(q, debug=False):
    import select
    import evdev
    from evdev import ecodes as e
    devs = [evdev.InputDevice(p) for p in evdev.list_devices()]
    # prefer the CardputerZero's built-in keyboard (TCA8418 matrix controller), then any real
    # keyboard; the HDMI-CEC and IR receivers also register as "keyboards" and come first
    named = [d for d in devs if "tca8418" in d.name.lower() or "keyboard" in d.name.lower()]
    with_a = [d for d in devs if e.KEY_A in d.capabilities().get(e.EV_KEY, [])]
    kbd = (named or with_a or [None])[0]
    if kbd is None:
        raise RuntimeError("no keyboard input device found (are you in the 'input' group?)")
    special = {e.KEY_LEFT: "left", e.KEY_RIGHT: "right", e.KEY_UP: "up",
               e.KEY_DOWN: "down", e.KEY_ENTER: "enter", e.KEY_ESC: "esc",
               e.KEY_BACKSPACE: "backspace", e.KEY_SPACE: " ",
               e.KEY_COMMA: ",", e.KEY_SLASH: "/", e.KEY_SEMICOLON: ";",
               e.KEY_DOT: ".", e.KEY_MINUS: "-", e.KEY_EQUAL: "=", e.KEY_APOSTROPHE: "'"}
    esc = EscHold()
    while True:
        ready, _, _ = select.select([kbd.fd], [], [], 0.1)   # wake up to time an Esc hold
        hold = esc.poll(time.time())
        if hold:
            q.append(hold)
        if not ready:
            continue
        for ev in kbd.read():
            if ev.type != e.EV_KEY:
                continue
            if ev.code == e.KEY_ESC:
                if ev.value == 1:
                    esc.down(time.time())
                elif ev.value == 0:
                    esc.up(time.time())
            name = e.KEY.get(ev.code)
            if isinstance(name, list):
                name = name[0]
            if debug:
                print(f"key {name} code={ev.code} value={ev.value}", flush=True)
            if ev.value != 1:
                continue
            if ev.code in special:
                q.append(special[ev.code])
            elif name and name.startswith("KEY_") and len(name) == 5:
                q.append(name[4].lower())
            elif name and name.startswith("KEY_") and name[4:].isdigit():
                q.append(name[4:])
            elif not debug:                    # goes to app.log via the launcher wrapper
                print(f"unmapped key {name} (code {ev.code})", flush=True)


def stdin_keys(q):
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    seqs = {"\x1b[D": "left", "\x1b[C": "right", "\x1b[A": "up", "\x1b[B": "down"}
    try:
        while True:
            ch = os.read(fd, 8).decode(errors="ignore")
            if ch in seqs:
                q.append(seqs[ch])
            elif ch == "\x1b":
                q.append("esc")
            elif ch in ("\r", "\n"):
                q.append("enter")
            elif ch in ("\x7f", "\x08"):
                q.append("backspace")
            else:
                q.extend(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------- app
class App:
    def __init__(self, show):
        self.show = show
        self.cfg = load_config()
        self.loc = self.cfg["location"]
        self.metric = self.cfg.get("metric", False)
        self.data = load_cache(self.loc)
        if self.data and self.data.get("_metric") != self.metric:
            self.data = None
        self.idx = 0
        self.status = ""
        self.mode = "view"
        self.query, self.results, self.sel = "", [], 0
        self.last_try = 0
        self.lock = threading.Lock()
        self.clim, self.rec, self.clim_status, self.clim_busy = None, None, "", False
        self.upper, self.upper_status, self.upper_busy, self.sound_off = None, "", False, 0
        self.view_date, self._clim_again, self.date_text = None, False, ""   # always start on today
        self._upper_again = False
        self.inspect, self._probe_n = None, 0          # inspect cursor: None = off
        global WAIT_CALLBACK
        WAIT_CALLBACK = self._on_rate_limit

    def _on_rate_limit(self, msg):
        if self.clim_busy:
            self.clim_status = msg
        if self.upper_busy:
            self.upper_status = msg
        if not (self.clim_busy or self.upper_busy):
            self.status = msg
        self.draw()

    def load_upper(self):
        """Background: 3 days of hourly surface + pressure-level data for Monsoon/Fire/Sounding,
        live from now or, for a past view date, from the historical-forecast archive."""
        if self.upper_busy:
            self._upper_again = True
            return
        self.upper_busy, self._upper_again = True, False
        loc, view, today = dict(self.loc), self.view_date, self.local_today()

        def work():
            try:
                self.upper_status = (f"loading {view.isoformat()} hourly…" if view
                                     else "fetching hourly profiles…")
                u = get_upper(loc, view, today)
                if loc == self.loc and view == self.view_date:
                    with self.lock:
                        self.upper, self.upper_status = u, ""
            except HourlyUnavailable as ex:
                if view == self.view_date:
                    self.upper_status = str(ex)
            except Exception as ex:
                log_error("upper", ex)
                self.upper_status = f"failed: {short_err(ex)}"
            finally:
                self.upper_busy = False
                self._upper_try = time.time()
                if self._upper_again:
                    self.load_upper()
                self.draw()
        threading.Thread(target=work, daemon=True).start()

    def local_today(self):
        """Today's date at the selected location (the forecast is fetched in its timezone);
        the device clock may be on UTC."""
        try:
            return dt.datetime.fromisoformat(self.data["current"]["time"]).date()
        except (TypeError, KeyError, ValueError):
            return dt.date.today()

    def load_climate(self, force_recent=False):
        """Background: climatology (cached forever per location/units) + the daily window around
        the view date (today: 3 h cache; past dates: assembled from cached years/baseline)."""
        if self.clim_busy:
            self._clim_again = True            # e.g. stepping dates quickly: redo when this one ends
            return
        self.clim_busy, self._clim_again = True, False
        loc, metric, view, today = dict(self.loc), self.metric, self.view_date, self.local_today()

        def work():
            def progress(m):
                self.clim_status = m
                self.draw()
            try:
                clim = ensure_clim_grid(loc, metric, get_climatology(loc, metric, progress))
                extras, model = clim.get("extras", []), clim.get("model", CLIM_MODELS[-1])
                if view is None:
                    rp = os.path.join(CACHE_DIR, "recent_" + os.path.basename(clim_path(loc, metric)))
                    rec = None
                    try:
                        with open(rp) as f:
                            rec = json.load(f)
                    except (OSError, ValueError):
                        pass
                    if force_recent or rec is None or time.time() - rec["built"] > RECENT_TTL_H * 3600:
                        progress("fetching recent obs…")
                        try:
                            rec = get_window(loc, metric, extras, model, None)
                            with open(rp, "w") as f:
                                json.dump(rec, f)
                        except Exception:
                            if rec is None:
                                raise
                else:
                    progress(f"loading {view.isoformat()}…")
                    rec = get_window(loc, metric, extras, model, view, today)
                if loc == self.loc and metric == self.metric and view == self.view_date:
                    with self.lock:
                        self.clim, self.rec, self.clim_status = clim, rec, ""
            except Exception as ex:
                log_error("climatology", ex)
                self.clim_status = f"failed: {short_err(ex)}"
            finally:
                self.clim_busy = False
                if self._clim_again:
                    self.load_climate()
                self.draw()
        threading.Thread(target=work, daemon=True).start()

    def set_view_date(self, d):
        """None = today (live). Past dates only; reloads the climate and hourly screens."""
        today = self.local_today()
        if d is not None and d >= today:
            d = None
        if d is not None and d < dt.date(1940, 1, 1):
            d = dt.date(1940, 1, 1)
        if d != self.view_date:
            self.view_date = d
            self.rec, self.upper, self.sound_off = None, None, 0
            self.load_climate()
            self.load_upper()

    def refresh(self, blocking=False):
        def work():
            try:
                d = fetch_forecast(self.loc["lat"], self.loc["lon"], self.metric)
                with self.lock:
                    self.data, self.status = d, ""
                save_cache(self.loc, d)
            except Exception as ex:  # network down etc.
                with self.lock:
                    log_error("forecast", ex)
                    self.status = f"offline: {type(ex).__name__}"
            self.draw()
        self.last_try = time.time()
        if blocking:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def draw(self):
        with self.lock:
            if self.mode == "search":
                img = render_search(self.query, self.results, self.sel, self.status)
            elif self.mode == "date":
                img = render_date_entry(self.date_text, self.view_date, self.status)
            elif self.mode == "help":
                img = render_help(self.loc, self.view_date)
            elif self.data is None:
                img = render_message(["Fetching forecast…", self.loc["name"]] +
                                     ([self.status] if self.status else []))
            else:
                img = render(self.data, self.loc, self.idx, self.status,
                             self.clim, self.rec, self.clim_status,
                             self.upper, self.upper_status, self.sound_off, self.view_date,
                             self.inspect)
                if self.inspect is not None:
                    if PROBE is not None and "index" in PROBE:
                        self.inspect, self._probe_n = PROBE["index"], len(PROBE["times"])
                    else:                      # nothing to inspect on this screen (yet)
                        self.inspect = None
        self.show(img)

    def key(self, k):
        if k == "quit":                      # Esc held 2 s (any mode)
            return False
        if self.mode == "search":
            return self.search_key(k)
        if self.mode == "date":
            return self.date_key(k)
        if self.status.startswith("hold Esc"):
            self.status = ""
        if self.inspect is not None:
            step = 1
            big = 6 if (PROBE or {}).get("hourly", True) else 7
            moves = {"right": step, "/": step, "left": -step, ",": -step,
                     "down": big, ".": big, "up": -big, ";": -big}
            if k in moves:
                cur = self.inspect if isinstance(self.inspect, int) else 0
                self.inspect = max(0, min(cur + moves[k], max(self._probe_n - 1, 0)))
                self.draw()
                return True
            self.inspect = None                # Enter / i / Esc / anything else leaves inspect mode
            if k in ("enter", "esc", "i"):
                self.draw()
                return True
        if self.mode == "help":              # any key closes the help screen
            self.mode = "view"
            self.draw()
            return k != "q"
        if k in ("right", "/"):
            self.idx = (self.idx + 1) % len(SCREENS)
        elif k in ("left", ","):
            self.idx = (self.idx - 1) % len(SCREENS)
        elif k in "123456" and len(k) == 1:
            self.idx = section_start(int(k))
        elif k in ("up", ";") and self.idx == SOUNDING_IDX:
            self.sound_off = max(0, self.sound_off - 3)
        elif k in ("down", ".") and self.idx == SOUNDING_IDX:
            self.sound_off = min(69, self.sound_off + 3)
        elif k == "r":
            self.status = "refreshing…"
            self.refresh()
            self.load_climate(force_recent=True)
            self.load_upper()
        elif k == "u":
            self.metric = not self.metric
            self.cfg["metric"] = self.metric
            save_config(self.cfg)
            self.status = "refreshing…"
            self.clim, self.rec = None, None
            self.refresh()
            self.load_climate()      # upper data is metric internally; no refetch needed
        elif k == "l":
            self.mode, self.query, self.results, self.sel, self.status = "search", "", [], 0, ""
        elif k in ("enter", "i"):
            self.inspect = "anchor"              # draw() resolves it, or turns it off if not a plot
        elif k in ("h", "?"):
            self.mode = "help"
        elif k == "d":
            self.mode, self.date_text, self.status = "date", "", ""
            if SCREENS[self.idx][2] == 1 or getattr(SCREENS[self.idx][1], "needs", "") == "upper":
                self.idx = section_start(2)     # dates apply to the climate screens
        elif k in ("-", "=", "b", "n", "[", "]"):
            cur = self.view_date or self.local_today()
            back = k in ("-", "b", "[")
            self.set_view_date(cur + dt.timedelta(days=-7 if back else 7))
            if SCREENS[self.idx][2] == 1:      # forecast pages ignore the date: show the change
                self.idx = section_start(2)
        elif k == "q":
            return False
        elif k == "esc":
            self.status = "hold Esc 2 s (or q) to exit"
        self.draw()
        return True

    def date_key(self, k):
        if k == "esc":
            self.mode, self.status = "view", ""
        elif k == "backspace":
            self.date_text = self.date_text[:-1]
        elif k == "enter":
            txt = self.date_text.strip().lower()
            if txt in ("", "t", "today"):
                self.mode, self.status = "view", ""
                self.set_view_date(None)
            else:
                d = parse_date(txt)
                if d is None:
                    self.status = "use YYYY-MM-DD, e.g. 2021-07-23"
                elif d >= self.local_today():
                    self.status = "pick a past date (Enter alone = today)"
                elif d < dt.date(1940, 1, 1):
                    self.status = "the archive starts in 1940"
                else:
                    self.mode, self.status = "view", ""
                    self.set_view_date(d)
        elif len(k) == 1 and (k.isdigit() or k in "-/t" or k.isalpha()):
            self.date_text = (self.date_text + k)[:12]
        self.draw()
        return True

    def search_key(self, k):
        if k == "esc":
            self.mode, self.status = "view", ""
        elif k == "backspace":
            self.query, self.results = self.query[:-1], []
        elif k == "up":                  # in the search box ; and . are text, not arrows
            self.sel = max(0, self.sel - 1)
        elif k == "down":
            self.sel = min(max(len(self.results) - 1, 0), self.sel + 1)
        elif k == "enter":
            if self.results:
                self.loc = self.results[self.sel]
                self.cfg["location"] = self.loc
                save_config(self.cfg)
                self.mode, self.idx = "view", 0
                self.data = load_cache(self.loc)
                self.status = "refreshing…"
                self.clim, self.rec, self.upper, self.sound_off = None, None, None, 0
                self.refresh()
                self.load_climate()
                self.load_upper()
            elif self.query.strip():
                self.status = "searching…"
                self.draw()
                try:
                    ll = parse_latlon(self.query)
                    self.results = [ll] if ll else geocode(self.query.strip())
                    self.sel = 0
                    self.status = "" if self.results else "no matches"
                except Exception as ex:
                    log_error("search", ex)
                    self.status = f"search failed: {short_err(ex)}"
        elif len(k) == 1:
            self.query += k
            self.results = []
        self.draw()
        return True

    def tick(self):
        stale_live = self.upper is not None and not self.upper.get("_view") and \
            time.time() - self.upper.get("_fetched", 0) > UPPER_TTL_MIN * 60
        if (self.upper is None and self.view_date is None or stale_live) \
                and not self.upper_busy and time.time() - getattr(self, "_upper_try", 0) > 300:
            self.load_upper()
        if self.rec is None or time.time() - self.rec.get("built", 0) > RECENT_TTL_H * 3600:
            if not self.clim_busy and time.time() - getattr(self, "_clim_try", 0) > 300:
                self._clim_try = time.time()
                self.load_climate()
        stale = self.data is None or time.time() - self.data.get("_fetched", 0) > REFRESH_MIN * 60
        if stale and time.time() - self.last_try > 60:
            self.refresh()
        elif self.idx == 0 or self.data:
            self.draw()  # keeps the "updated N min ago" line current


def run_loop(app, keyq, period=30):
    app.draw()
    app.refresh()
    app.load_climate()
    app.load_upper()
    last = time.time()
    while True:
        while keyq:
            if app.key(keyq.pop(0)) is False:
                return
        if time.time() - last > period:
            app.tick()
            last = time.time()
        time.sleep(0.03)


def run_window(scale=3):
    import tkinter as tk
    import base64
    import io
    root = tk.Tk()
    root.title(APP_NAME)
    label = tk.Label(root, bg="black")
    label.pack()
    state = {}

    def show(img):
        buf = io.BytesIO()
        img.resize((W * scale, H * scale), Image.NEAREST).save(buf, "PNG")
        photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        state["photo"] = photo
        root.after(0, lambda: label.configure(image=photo))

    app = App(show)
    keymap = {"Left": "left", "Right": "right", "Up": "up", "Down": "down",
              "Return": "enter", "Escape": "esc", "BackSpace": "backspace",
              "space": " "}

    def on_key(ev):
        k = keymap.get(ev.keysym, ev.char)
        if k and app.key(k) is False:
            root.destroy()

    root.bind("<Key>", on_key)

    def periodic():
        app.tick()
        root.after(30000, periodic)

    app.draw()
    app.refresh()
    app.load_climate()
    app.load_upper()
    root.after(30000, periodic)
    root.mainloop()


def run_png(outdir, data_file=None, clim=None, rec=None, upper=None, view=None):
    os.makedirs(outdir, exist_ok=True)
    cfg = load_config()
    loc = cfg["location"]
    if data_file:
        with open(data_file) as f:
            data = json.load(f)
        data.setdefault("_fetched", time.time())
        data.setdefault("_metric", False)
    else:
        data = fetch_forecast(loc["lat"], loc["lon"], cfg.get("metric", False))
    if clim is None and not data_file:
        clim = get_climatology(loc, cfg.get("metric", False), print)
        rec = get_window(loc, cfg.get("metric", False), clim.get("extras", []),
                         clim.get("model", CLIM_MODELS[-1]), view)
        try:
            upper = get_upper(loc, view)
        except HourlyUnavailable:
            upper = None
    for i, (name, _, _) in enumerate(SCREENS):
        p = os.path.join(outdir, f"{i:02d}_{name.lower().replace('-', '').replace(' ', '_')}.png")
        render(data, loc, i, clim=clim, rec=rec, upper=upper, view_date=view,
               upper_status="hourly archive starts 2022" if view and upper is None else "").save(p)
        print(p)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fb", nargs="?", const="auto", metavar="DEV",
                   help="draw to a framebuffer device (default /dev/fb_lcd, else /dev/fb0)")
    g.add_argument("--window", action="store_true", help="Tk preview window (desktop)")
    g.add_argument("--png", metavar="DIR", help="render all screens to PNGs and exit")
    ap.add_argument("--data", metavar="JSON", help="with --png: use a saved forecast JSON")
    ap.add_argument("--keys", choices=["evdev", "stdin"], default="evdev",
                    help="with --fb: read keys from the device keyboard (evdev) "
                         "or this terminal (stdin, handy over SSH)")
    ap.add_argument("--location", metavar="NAME", help="set location by name, then run")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="with --png: climate screens for a past date")
    ap.add_argument("--bgr", action="store_true", help="with --fb: swap red/blue if colors look wrong")
    ap.add_argument("--version", action="version", version=f"cz-weather {APP_VERSION}")
    ap.add_argument("--debug-keys", action="store_true",
                    help="with --fb: print every key event (to find what a key sends)")
    a = ap.parse_args()

    if a.location:
        ll = parse_latlon(a.location)
        res = [ll] if ll else geocode(a.location, 1)
        if not res:
            sys.exit(f"no match for {a.location!r}")
        cfg = load_config()
        cfg["location"] = res[0]
        save_config(cfg)
        print("location:", res[0]["name"])

    if a.png:
        view = parse_date(a.date) if a.date else None
        if a.date and view is None:
            sys.exit(f"can't read date {a.date!r}; use YYYY-MM-DD")
        return run_png(a.png, a.data, view=view)
    if a.window:
        return run_window()

    dev = default_fb() if a.fb in (None, "auto") else a.fb
    stopped = take_over_screen()                            # one copy on the LCD at a time
    if stopped:
        print(f"stopped older cz-weather process(es): {stopped}", flush=True)
    disp = FramebufferDisplay(dev, bgr=a.bgr)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # launcher stop -> clear screen, exit
    app = App(disp.show)
    keyq = []
    if a.keys == "evdev":
        threading.Thread(target=evdev_keys, args=(keyq, a.debug_keys), daemon=True).start()
    else:
        threading.Thread(target=stdin_keys, args=(keyq,), daemon=True).start()
    try:
        run_loop(app, keyq)
    finally:
        disp.show(Image.new("RGB", (W, H), (0, 0, 0)))
        release_screen()


if __name__ == "__main__":
    main()
