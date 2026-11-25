"""A deterministic stand-in for the Open-Meteo APIs, used by the offline tests.

It answers the same URLs cz_weather builds (forecast, archive, geocoding) with
synthetic but plausible Tucson-like data, and can pretend specific variable
names are unknown (HTTP 400) to exercise the app's fallback paths.
"""
import datetime as dt
import io
import json
import math
import os
import random
import urllib.error
import urllib.parse

HERE = os.path.dirname(__file__)
TODAY = dt.date(2026, 11, 24)


class BadRequest(urllib.error.HTTPError):
    def __init__(self, url="fake"):
        super().__init__(url, 400, "Bad Request", {}, io.BytesIO(b'{"error":true}'))


class RateLimited(urllib.error.HTTPError):
    def __init__(self, url="fake"):
        super().__init__(url, 429, "Too Many Requests", {},
                         io.BytesIO(b'{"error":true,"reason":"Minutely API request limit exceeded."}'))


def _seas(d, lo, hi, peak=172):
    return lo + (hi - lo) * (0.5 + 0.5 * math.cos((d.timetuple().tm_yday - peak) / 365 * 2 * math.pi))


def _monsoon(d):
    return math.exp(-((d.timetuple().tm_yday - 215) / 25) ** 2)


class FakeOpenMeteo:
    def __init__(self, unknown=(), bad_models=(), seed=1, rate_limit_first=0, hist_without=()):
        self.hist_without = set(hist_without)       # variables the historical archive returns as nulls
        self.rate_limit_first = rate_limit_first   # answer the first N calls with HTTP 429
        self.unknown = set(unknown)          # variable names to reject with HTTP 400
        self.bad_models = set(bad_models)    # archive model names to reject
        self.calls = []
        self.seed = seed

    # ---------------------------------------------------------------- generators
    def _daily_value(self, var, d, rng, warm):
        if var == "temperature_2m_max":
            return _seas(d, 64, 104) + rng.gauss(warm, 5)
        if var == "temperature_2m_min":
            return _seas(d, 39, 75, 195) + rng.gauss(warm, 4)
        if var == "precipitation_sum":
            p = 0.03 + 0.25 * _monsoon(d) + 0.04 * (d.month in (12, 1, 2))
            return round(rng.expovariate(1 / 0.35), 2) if rng.random() < p else 0.0
        if var == "dew_point_2m_mean":
            return 25 + 8 * rng.random() + 35 * _monsoon(d) + rng.gauss(0, 4)
        if var == "relative_humidity_2m_min":
            return max(2.0, 12 + 25 * _monsoon(d) + rng.gauss(0, 4))
        if var == "vapour_pressure_deficit_max":
            return max(0.2, _seas(d, 1.4, 6.5, 165) - 3 * _monsoon(d) + rng.gauss(0, 0.4))
        if var == "wind_gusts_10m_max":
            return max(5.0, 18 + rng.gauss(0, 5))
        if var == "et0_fao_evapotranspiration":
            return max(0.02, _seas(d, 0.07, 0.36, 165) + rng.gauss(0, 0.02))
        if var == "shortwave_radiation_sum":
            return max(4.0, _seas(d, 12, 31, 170) - 6 * _monsoon(d) + rng.gauss(0, 2))
        if var in ("weather_code",):
            return 1
        if var in ("precipitation_probability_max",):
            return 10
        if var in ("wind_speed_10m_max",):
            return 12
        return None

    def _daily_block(self, a, b, variables, warm=0.0, null_tail=0):
        rng = random.Random(self.seed + a.toordinal())
        days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
        out = {"time": [x.isoformat() for x in days]}
        for v in variables:
            vals = []
            for i, x in enumerate(days):
                val = None if i >= len(days) - null_tail else self._daily_value(v, x, rng, warm)
                vals.append(None if val is None else round(val, 3))
            out[v] = vals
        return out

    def _hourly_upper(self, variables, hours=72, start=TODAY):
        rng = random.Random(self.seed)
        t0 = dt.datetime.combine(start, dt.time())
        hrs = [t0 + dt.timedelta(hours=i) for i in range(hours)]
        h = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in hrs]}
        for v in variables:
            vals = []
            for i, t in enumerate(hrs):
                di = math.sin((t.hour - 9) / 24 * 2 * math.pi)
                fr = 1 / (1 + math.exp(-(i - 40) / 3))
                x = None
                if v == "temperature_2m":
                    x = 19 + 9 * di
                elif v == "dew_point_2m":
                    x = -4 + 8 * fr
                elif v == "relative_humidity_2m":
                    x = max(4, 12 - 8 * di + 25 * fr)
                elif v == "vapour_pressure_deficit":
                    x = max(0.1, 2.2 + 1.6 * di - 1.5 * fr)
                elif v == "wind_speed_10m":
                    x = 12 + 10 * max(0, di) + (28 if 30 < i < 40 else 0)
                elif v == "wind_gusts_10m":
                    x = (12 + 10 * max(0, di) + (28 if 30 < i < 40 else 0)) * 1.7
                elif v == "surface_pressure":
                    x = 925.0
                elif v == "cape":
                    x = max(0, 300 * fr * max(0, di) + rng.gauss(0, 15))
                elif v == "precipitation_probability":
                    x = int(40 * fr)
                elif v == "freezing_level_height":
                    x = 3600 + 300 * fr
                elif v == "total_column_integrated_water_vapour":
                    x = 8 + 10 * fr
                elif v in ("lifted_index",):
                    x = 2 - 4 * fr
                elif v in ("convective_inhibition",):
                    x = -20
                elif v.endswith("hPa"):
                    name, lev = v.rsplit("_", 1)
                    p = int(lev[:-3])
                    z = 44330 * (1 - (p / 1013.25) ** 0.1903)
                    x = {"temperature": 19 - 6.5 * z / 1000 + (2 if p < 400 else 0),
                         "dew_point": 19 - 6.5 * z / 1000 - (20 if p > 700 else 12) + 6 * fr,
                         "wind_speed": 10 + (1013 - p) / 8,
                         "wind_direction": 230 + (1013 - p) / 20,
                         "geopotential_height": z}.get(name)
                vals.append(None if x is None else round(x, 2))
            h[v] = vals
        return h

    def _main_forecast(self, q):
        with open(os.path.join(HERE, "data", "sample_forecast.json")) as f:
            data = json.load(f)
        return data

    @staticmethod
    def _grid(q, res, elev):
        """Top-level metadata like Open-Meteo's: snapped grid-cell center + DEM elevation."""
        lat, lon = float(q.get("latitude", 0)), float(q.get("longitude", 0))
        return {"latitude": round(round(lat / res) * res, 4), "longitude": round(round(lon / res) * res, 4),
                "elevation": elev, "timezone": "America/Phoenix", "timezone_abbreviation": "GMT-7",
                "utc_offset_seconds": -25200}

    # ---------------------------------------------------------------- dispatcher
    def __call__(self, url):
        out = self._dispatch(url)
        if isinstance(out, dict) and "results" not in out:
            u = urllib.parse.urlparse(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            res, elev = (0.1, 760.0) if "archive" in u.netloc else (0.03, 741.0)
            for k, v in self._grid(q, res, elev).items():
                out.setdefault(k, v)
        return out

    def _dispatch(self, url):
        self.calls.append(url)
        if len(self.calls) <= self.rate_limit_first:
            raise RateLimited(url)
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if "geocoding" in u.netloc:
            return {"results": [{"name": q["name"].title(), "admin1": "Arizona", "country_code": "US",
                                 "latitude": 35.2, "longitude": -111.65, "elevation": 2106.0,
                                 "timezone": "America/Phoenix", "population": 76831}]}
        names = []
        for key in ("daily", "hourly", "current"):
            names += [x for x in q.get(key, "").split(",") if x]
        if self.unknown.intersection(names):
            raise BadRequest(url)
        if "archive" in u.netloc:
            if q.get("models") in self.bad_models:
                raise BadRequest(url)
            a, b = dt.date.fromisoformat(q["start_date"]), dt.date.fromisoformat(q["end_date"])
            recent = a.year > 2020
            daily = self._daily_block(a, b, q["daily"].split(","), 3.0 if recent else 0.0,
                                      5 if recent else 0)
            units = {"dew_point_2m_mean": "°F", "relative_humidity_2m_min": "%",
                     "vapour_pressure_deficit_max": "kPa", "wind_gusts_10m_max": "mp/h",
                     "et0_fao_evapotranspiration": "inch", "shortwave_radiation_sum": "MJ/m²"}
            return {"daily": daily, "daily_units": units}
        if "current" in q:
            return self._main_forecast(q)
        if "hourly" in q:
            if "start_date" in q:                    # historical-forecast archive
                a, b = dt.date.fromisoformat(q["start_date"]), dt.date.fromisoformat(q["end_date"])
                if a < dt.date(2022, 1, 1):          # mimic: no archive yet -> all nulls
                    h = self._hourly_upper(q["hourly"].split(","), 24 * ((b - a).days + 1), a)
                    return {"hourly": {k: (v if k == "time" else [None] * len(v)) for k, v in h.items()}}
                h = self._hourly_upper(q["hourly"].split(","), 24 * ((b - a).days + 1), a)
                for k in self.hist_without:
                    if k in h:
                        h[k] = [None] * len(h[k])
                return {"hourly": h}
            return {"hourly": self._hourly_upper(q["hourly"].split(","),
                                                 24 * int(q.get("forecast_days", 3)))}
        past = int(q.get("past_days", 0))
        fd = int(q.get("forecast_days", 7))
        return {"daily": self._daily_block(TODAY - dt.timedelta(days=past),
                                           TODAY + dt.timedelta(days=fd - 1),
                                           q["daily"].split(","), 4.0)}
