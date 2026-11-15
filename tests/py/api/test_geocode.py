"""Pickup field: GET /geocode/search and /geocode/reverse — Photon through the API.

The geocoder is injected (``cantrack_api.geocode.get_geocoder``) so these tests never touch the
network. Every answer is limited to Pasto, cached, and an upstream failure is a 200 with
``status: "unavailable"`` (the UI then offers the map), never a 5xx.
"""

import pytest

from cantrack_api import geocode
from cantrack_api.geocode import get_geocoder

from .conftest import bearer


def feature(name=None, street=None, number=None, district=None, locality=None, city="Pasto", lat=1.2136, lng=-77.2811):
    """A Photon GeoJSON feature, only with the properties the test sets."""
    props = {"name": name, "street": street, "housenumber": number, "district": district,
             "locality": locality, "city": city}
    return {
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {key: value for key, value in props.items() if value is not None},
    }


class FakeGeocoder:
    def __init__(self, features=None):
        self.features = features if features is not None else []
        self.search_calls = []
        self.reverse_calls = []
        self.fail = False

    def search(self, query):
        self.search_calls.append(query)
        if self.fail:
            raise RuntimeError("photon down")
        return self.features

    def reverse(self, lat, lng):
        self.reverse_calls.append((lat, lng))
        if self.fail:
            raise RuntimeError("photon down")
        return self.features


@pytest.fixture(autouse=True)
def empty_cache():
    geocode.GEOCODE_CACHE.clear()
    yield
    geocode.GEOCODE_CACHE.clear()


@pytest.fixture
def source(fake_client):
    fake_source = FakeGeocoder()
    fake_client.app.dependency_overrides[get_geocoder] = lambda: fake_source
    return fake_source


def search(client, q="unicentro", token="token-a"):
    return client.get("/geocode/search", params={"q": q}, headers=bearer(token))


def reverse(client, lat=1.2136, lng=-77.2811, token="token-a"):
    return client.get("/geocode/reverse", params={"lat": lat, "lng": lng}, headers=bearer(token))


class TestSearch:
    def test_a_place_is_a_label_a_detail_and_a_point(self, fake_client, source):
        source.features = [feature(name="Unicentro Pasto", district="Torobajo", lat=1.2201, lng=-77.2745)]
        body = search(fake_client).json()
        assert body == {
            "status": "ok",
            "results": [{"label": "Unicentro Pasto", "detail": "Torobajo, Pasto", "lat": 1.2201, "lng": -77.2745}],
        }

    def test_a_street_with_a_number_is_the_label(self, fake_client, source):
        source.features = [feature(street="Carrera 27", number="15-30", district="Centro")]
        result = search(fake_client).json()["results"][0]
        assert result["label"] == "Carrera 27 15-30"
        assert result["detail"] == "Centro, Pasto"

    def test_a_street_without_a_number_is_just_the_street(self, fake_client, source):
        source.features = [feature(street="Calle 18")]
        assert search(fake_client).json()["results"][0]["label"] == "Calle 18"

    def test_detail_is_just_the_city_without_a_district(self, fake_client, source):
        source.features = [feature(name="Parque Infantil")]
        assert search(fake_client).json()["results"][0]["detail"] == "Pasto"

    def test_the_barrio_leads_the_detail_and_the_city_is_left_out_when_there_is_a_barrio(self, fake_client, source):
        source.features = [feature(name="Escuela", locality="San Andresito", district="Comuna 1")]
        assert search(fake_client).json()["results"][0]["detail"] == "San Andresito, Comuna 1"

    def test_the_same_place_mapped_twice_is_listed_once(self, fake_client, source):
        # Real Photon returns "Plaza de Nariño" as several OSM objects a metre apart, even with
        # different barrio tags.
        source.features = [
            feature(name="Plaza de Nariño", district="Comuna 1", lat=1.2146113, lng=-77.2782881),
            feature(name="Plaza de Nariño", district="Comuna 1", lat=1.2146073, lng=-77.2782916),
            feature(name="Plaza de Nariño", district="Comuna 1", locality="Marcos de la Rosa", lat=1.2146, lng=-77.2783),
        ]
        results = search(fake_client).json()["results"]
        assert [(r["label"], r["detail"]) for r in results] == [("Plaza de Nariño", "Comuna 1, Pasto")]

    def test_the_same_name_far_apart_is_two_places(self, fake_client, source):
        # "Carrera 27" runs through several comunas: each one is a different pickup.
        source.features = [
            feature(street="Carrera 27", district="Comuna 7", lat=1.2071, lng=-77.2924),
            feature(street="Carrera 27", district="Comuna 10", lat=1.2323, lng=-77.2674),
        ]
        assert [r["detail"] for r in search(fake_client).json()["results"]] == ["Comuna 7, Pasto", "Comuna 10, Pasto"]

    def test_results_outside_pasto_are_dropped(self, fake_client, source):
        source.features = [feature(name="Bogota Plaza", lat=4.6, lng=-74.08), feature(name="Plaza de Nariño")]
        assert [r["label"] for r in search(fake_client).json()["results"]] == ["Plaza de Nariño"]

    def test_features_without_any_name_or_street_are_dropped(self, fake_client, source):
        source.features = [feature(district="Centro"), feature(name="Plaza de Nariño")]
        assert [r["label"] for r in search(fake_client).json()["results"]] == ["Plaza de Nariño"]

    def test_malformed_features_are_skipped_not_fatal(self, fake_client, source):
        source.features = [{"properties": {"name": "x"}}, {"geometry": {"coordinates": ["a", "b"]}}, feature(name="Ok")]
        assert [r["label"] for r in search(fake_client).json()["results"]] == ["Ok"]

    def test_at_most_six_results(self, fake_client, source):
        source.features = [feature(name=f"Place {n}") for n in range(10)]
        assert len(search(fake_client).json()["results"]) == 6

    def test_no_matches_is_ok_and_empty(self, fake_client, source):
        assert search(fake_client).json() == {"status": "ok", "results": []}


class TestSearchValidation:
    def test_one_character_is_400(self, fake_client, source):
        assert search(fake_client, q="a").status_code == 400

    def test_a_blank_query_is_400(self, fake_client, source):
        assert search(fake_client, q="   ").status_code == 400

    def test_more_than_80_characters_is_400(self, fake_client, source):
        assert search(fake_client, q="x" * 81).status_code == 400

    def test_a_missing_query_is_400(self, fake_client, source):
        assert fake_client.get("/geocode/search", headers=bearer("token-a")).status_code == 400

    def test_no_token_is_401(self, fake_client, source):
        assert fake_client.get("/geocode/search", params={"q": "plaza"}).status_code == 401

    def test_a_walker_can_search_too(self, fake_client, source):
        assert search(fake_client, token="token-w").status_code == 200

    def test_the_query_is_trimmed_before_it_goes_upstream(self, fake_client, source):
        search(fake_client, q="  plaza de nariño  ")
        assert source.search_calls == ["plaza de nariño"]


class TestCache:
    def test_the_same_query_hits_upstream_once(self, fake_client, source):
        source.features = [feature(name="Plaza")]
        search(fake_client, q="plaza")
        search(fake_client, q="plaza")
        assert len(source.search_calls) == 1

    def test_case_and_spacing_share_one_entry(self, fake_client, source):
        search(fake_client, q="Plaza  de Nariño")
        search(fake_client, q="plaza de nariño")
        assert len(source.search_calls) == 1

    def test_the_cache_is_bounded(self, fake_client, source):
        for n in range(geocode.MAX_CACHE_ENTRIES + 20):
            search(fake_client, q=f"calle {n}")
        assert len(geocode.GEOCODE_CACHE) <= geocode.MAX_CACHE_ENTRIES

    def test_a_failure_is_not_cached(self, fake_client, source):
        source.fail = True
        search(fake_client, q="plaza")
        source.fail = False
        source.features = [feature(name="Plaza")]
        assert search(fake_client, q="plaza").json()["results"][0]["label"] == "Plaza"


class TestUpstreamDown:
    def test_search_is_200_unavailable_never_5xx(self, fake_client, source):
        source.fail = True
        res = search(fake_client)
        assert res.status_code == 200
        assert res.json() == {"status": "unavailable", "results": []}

    def test_reverse_is_200_unavailable_never_5xx(self, fake_client, source):
        source.fail = True
        res = reverse(fake_client)
        assert res.status_code == 200
        assert res.json() == {"status": "unavailable", "results": []}


class TestReverse:
    def test_the_nearest_address_comes_back(self, fake_client, source):
        source.features = [feature(street="Calle 18", number="22-10", district="Centro", lat=1.2140, lng=-77.2810)]
        body = reverse(fake_client).json()
        assert body["status"] == "ok"
        assert body["results"][0]["label"] == "Calle 18 22-10"
        assert source.reverse_calls == [(1.2136, -77.2811)]

    def test_the_street_wins_over_the_name_of_the_nearest_object(self, fake_client, source):
        # Real Photon answered the Pasto centre with an all-caps school; the pin wants the address.
        source.features = [feature(name="ESCUELA N. 4 DE NIÑAS", street="Calle 15", locality="San Andresito")]
        assert reverse(fake_client).json()["results"][0]["label"] == "Calle 15"

    def test_without_a_street_the_name_is_the_label(self, fake_client, source):
        source.features = [feature(name="Parque Infantil")]
        assert reverse(fake_client).json()["results"][0]["label"] == "Parque Infantil"

    def test_nearby_points_share_a_cache_cell(self, fake_client, source):
        reverse(fake_client, lat=1.21361, lng=-77.28111)
        reverse(fake_client, lat=1.21362, lng=-77.28112)
        assert len(source.reverse_calls) == 1

    def test_a_point_outside_pasto_is_400(self, fake_client, source):
        res = reverse(fake_client, lat=4.6, lng=-74.08)
        assert res.status_code == 400
        assert source.reverse_calls == []

    def test_impossible_coordinates_are_400(self, fake_client, source):
        assert reverse(fake_client, lat=95, lng=0).status_code == 400

    def test_no_token_is_401(self, fake_client, source):
        assert fake_client.get("/geocode/reverse", params={"lat": 1.2, "lng": -77.28}).status_code == 401


class TestPhotonClient:
    def test_search_asks_photon_with_the_pasto_box_and_a_generic_user_agent(self, monkeypatch):
        seen = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"features": [feature(name="Plaza")]}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params, headers=headers, timeout=timeout)
            return Response()

        monkeypatch.setattr(geocode.httpx, "get", fake_get)
        assert geocode.PhotonClient().search("plaza") == [feature(name="Plaza")]
        assert seen["url"] == "https://photon.komoot.io/api/"
        assert seen["params"]["q"] == "plaza"
        assert seen["params"]["bbox"] == "-77.36,1.12,-77.2,1.3"
        assert seen["params"]["limit"] == 10
        assert seen["headers"]["User-Agent"] == "CanTrack/0.1"
        # Photon answers 400 to a language it does not support (es), so none is sent.
        assert "lang" not in seen["params"]
        assert seen["timeout"] <= 5.0

    def test_reverse_asks_photon_for_the_point(self, monkeypatch):
        seen = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"features": []}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params)
            return Response()

        monkeypatch.setattr(geocode.httpx, "get", fake_get)
        assert geocode.PhotonClient().reverse(1.2136, -77.2811) == []
        assert seen["url"] == "https://photon.komoot.io/reverse"
        assert seen["params"]["lat"] == 1.2136 and seen["params"]["lon"] == -77.2811
        assert "lang" not in seen["params"]
