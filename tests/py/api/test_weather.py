"""PR1 Inicio: GET /weather — Open-Meteo through the API, cached, never a 500.

The forecast source is injected (``cantrack_api.weather.get_weather_source``)
so these tests never touch the network. The best-walk window is deterministic
(no LLM): the driest mild 2-hour slot left today.
"""

from datetime import timedelta

import pytest

from cantrack_api import weather
from cantrack_api.weather import get_weather_source

from .conftest import FIXED_NOW, bearer

UNAVAILABLE = {"detail": "Weather is unavailable right now."}

# Pasto is UTC-5. FIXED_NOW (2026-10-01T00:00Z) is 2026-09-30 19:00 local, too late
# for a window, so the window tests move the clock to 13:00Z = 08:00 local.
HOURS = [f"2026-10-01T{hour:02d}:00" for hour in range(0, 24)]


def forecast(temps=None, rain=None, codes=None, current_temp=14.0, current_code=3):
    temps = temps or [14.0] * 24
    rain = rain or [10] * 24
    codes = codes or [3] * 24
    return {
        "latitude": 1.21, "longitude": -77.28, "utc_offset_seconds": -18000, "timezone": "America/Bogota",
        "current": {"time": "2026-10-01T08:00", "temperature_2m": current_temp, "weather_code": current_code},
        "hourly": {"time": HOURS, "temperature_2m": temps, "precipitation_probability": rain, "weather_code": codes},
    }


class FakeSource:
    def __init__(self, payload=None):
        self.payload = payload or forecast()
        self.calls = []
        self.fail = False

    def forecast(self, lat, lng):
        self.calls.append((lat, lng))
        if self.fail:
            raise RuntimeError("open-meteo down")
        return self.payload


@pytest.fixture(autouse=True)
def empty_cache():
    weather.WEATHER_CACHE.clear()
    yield
    weather.WEATHER_CACHE.clear()


@pytest.fixture
def source(fake_client):
    fake_source = FakeSource()
    fake_client.app.dependency_overrides[get_weather_source] = lambda: fake_source
    return fake_source


@pytest.fixture
def morning(fake_clock):
    fake_clock.now = FIXED_NOW + timedelta(hours=13)  # 08:00 in Pasto
    return fake_clock


def get_weather(client, lat=1.2136, lng=-77.2811, token="token-a"):
    return client.get("/weather", params={"lat": lat, "lng": lng}, headers=bearer(token))


class TestShape:
    def test_current_conditions_and_next_twelve_hours(self, fake_client, source, morning):
        body = get_weather(fake_client).json()
        assert body["lat"] == 1.21 and body["lng"] == -77.28
        assert body["temperatureC"] == 14.0
        assert body["condition"] == "cloudy"
        assert body["stale"] is False
        assert len(body["hours"]) == 12
        assert body["hours"][0] == {"at": "2026-10-01T08:00:00-05:00", "temperatureC": 14.0,
                                    "condition": "cloudy", "precipitationProbability": 10}

    @pytest.mark.parametrize("code,condition", [
        (0, "clear"), (2, "cloudy"), (45, "fog"), (53, "drizzle"), (63, "rain"),
        (81, "rain"), (73, "snow"), (95, "storm"),
    ])
    def test_wmo_codes_map_to_conditions(self, fake_client, source, morning, code, condition):
        source.payload = forecast(current_code=code)
        assert get_weather(fake_client).json()["condition"] == condition

    def test_any_authenticated_role_may_ask(self, fake_client, source, morning):
        assert get_weather(fake_client, token="token-w").status_code == 200

    def test_requires_a_token(self, fake_client, source):
        assert fake_client.get("/weather", params={"lat": 1.2, "lng": -77.2}).status_code == 401

    @pytest.mark.parametrize("params", [{"lat": 91, "lng": 0}, {"lat": 0, "lng": 181}, {"lat": "x", "lng": 0}, {}])
    def test_bad_coordinates_are_400(self, fake_client, source, params):
        assert fake_client.get("/weather", params=params, headers=bearer("token-a")).status_code == 400


class TestBestWindow:
    def test_picks_the_driest_mild_two_hours(self, fake_client, source, morning):
        rain = [10] * 24
        temps = [15.0] * 24
        rain[8], rain[9] = 40, 40      # 08-10 wet
        rain[11], rain[12] = 5, 0      # 11-13 driest
        temps[16] = 25.0               # 16:00 too hot
        source.payload = forecast(temps=temps, rain=rain)
        assert get_weather(fake_client).json()["bestWindow"] == {
            "start": "2026-10-01T11:00:00-05:00", "end": "2026-10-01T13:00:00-05:00", "reason": "dry_and_mild",
        }

    def test_ties_go_to_the_earliest_slot(self, fake_client, source, morning):
        assert get_weather(fake_client).json()["bestWindow"]["start"] == "2026-10-01T08:00:00-05:00"

    def test_rainy_day_has_no_window(self, fake_client, source, morning):
        source.payload = forecast(rain=[80] * 24, codes=[63] * 24)
        assert get_weather(fake_client).json()["bestWindow"] is None

    def test_cold_day_has_no_window(self, fake_client, source, morning):
        source.payload = forecast(temps=[8.0] * 24)
        assert get_weather(fake_client).json()["bestWindow"] is None

    def test_window_never_ends_after_20h(self, fake_client, source, fake_clock):
        fake_clock.now = FIXED_NOW + timedelta(hours=13 + 11)  # 19:00 local
        assert get_weather(fake_client).json()["bestWindow"] is None


class TestCacheAndFailure:
    def test_nearby_requests_share_one_upstream_call(self, fake_client, source, morning):
        get_weather(fake_client, lat=1.2136, lng=-77.2811)
        get_weather(fake_client, lat=1.2141, lng=-77.2799)  # same ~1 km cell
        assert len(source.calls) == 1

    def test_cache_expires_after_15_minutes(self, fake_client, source, morning):
        get_weather(fake_client)
        morning.now += timedelta(minutes=16)
        get_weather(fake_client)
        assert len(source.calls) == 2

    def test_upstream_failure_serves_stale_cache(self, fake_client, source, morning):
        get_weather(fake_client)
        morning.now += timedelta(minutes=30)
        source.fail = True
        res = get_weather(fake_client)
        assert res.status_code == 200
        assert res.json()["stale"] is True

    def test_upstream_failure_without_cache_is_503_not_500(self, fake_client, source, morning):
        source.fail = True
        res = get_weather(fake_client)
        assert res.status_code == 503
        assert res.json() == UNAVAILABLE

    def test_malformed_upstream_payload_is_503(self, fake_client, source, morning):
        source.payload = {"hourly": {"time": ["x"]}}
        assert get_weather(fake_client).status_code == 503

    def test_weather_is_rate_limited_per_user(self, fake_client, source, morning):
        codes = [get_weather(fake_client).status_code for _ in range(31)]
        assert codes[-1] == 429
