import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import cz_weather as cz  # noqa: E402
from fake_api import FakeOpenMeteo  # noqa: E402

TUCSON = {"name": "Tucson, Arizona, US", "lat": 32.2217, "lon": -110.9265}


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="also run tests that call the real Open-Meteo API")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls the real Open-Meteo API (run with --live)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point cache and config at a temp folder so tests never touch your real ones."""
    monkeypatch.setattr(cz, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cz, "CONFIG_PATH", str(tmp_path / "config.json"))
    return tmp_path


@pytest.fixture
def fake(isolated, monkeypatch):
    api = FakeOpenMeteo()
    monkeypatch.setattr(cz, "http_json", api)
    return api


@pytest.fixture
def forecast():
    with open(os.path.join(os.path.dirname(__file__), "data", "sample_forecast.json")) as f:
        data = json.load(f)
    data["_fetched"] = time.time()
    data.setdefault("_metric", False)
    return data


@pytest.fixture
def bundle(fake, forecast):
    """Everything the screens need, built through the real code paths against the fake API."""
    clim = cz.get_climatology(TUCSON, False)
    rec = cz.get_recent(TUCSON, False, clim["extras"], clim["model"])
    upper = cz.get_upper(TUCSON)
    return {"data": forecast, "clim": clim, "rec": rec, "upper": upper}
