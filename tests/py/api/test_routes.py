"""FR-06..FR-09: routes as ordered stops, reorder/remove, next pickup (AVL), shortest path."""

import uuid

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer

W = "token-w"  # walker-w
OTHER = "token-a"  # a different authenticated user


def seed_dog(fake, name, dog_id=None):
    return fake.seed("dogs", id=dog_id or str(uuid.uuid4()), owner_id="owner-a", name=name)


def stops_for(*dogs, times=None):
    times = times or [f"2026-10-05T{8 + i:02d}:00:00Z" for i in range(len(dogs))]
    return [{"dogId": d["id"], "pickupTime": t} for d, t in zip(dogs, times)]


def create_route(client, stops, token=W):
    return client.post("/routes", json={"stops": stops}, headers=bearer(token))


@pytest.fixture
def dogs(fake):
    return [seed_dog(fake, "Firulais"), seed_dog(fake, "Rex"), seed_dog(fake, "Luna")]


class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,path",
        [("get", "/routes"), ("post", "/routes"), ("get", "/routes/next-pickup"),
         ("post", "/routes/path"), ("get", f"/routes/{uuid.uuid4()}"),
         ("patch", f"/routes/{uuid.uuid4()}/stops/reorder"),
         ("delete", f"/routes/{uuid.uuid4()}/stops/0")],
    )
    def test_no_token_is_401(self, fake_client, method, path):
        assert getattr(fake_client, method)(path).status_code == 401


class TestCreate:
    def test_creates_a_route_with_ordered_stops_and_dog_names(self, fake_client, fake, dogs):
        res = create_route(fake_client, stops_for(*dogs))
        assert res.status_code == 201
        body = res.json()
        assert body["walker_id"] == "walker-w"
        assert [s["dogId"] for s in body["stops"]] == [d["id"] for d in dogs]
        assert [s["dogName"] for s in body["stops"]] == ["Firulais", "Rex", "Luna"]
        assert body["stops"][0]["pickupTime"] == "2026-10-05T08:00:00Z"

    def test_stores_stops_in_snake_case(self, fake_client, fake, dogs):
        create_route(fake_client, stops_for(dogs[0]))
        stored = fake.tables["routes"][0]["stops"]
        assert stored == [{"dog_id": dogs[0]["id"], "pickup_time": "2026-10-05T08:00:00Z"}]

    def test_walker_id_comes_from_the_token_not_the_body(self, fake_client, fake, dogs):
        res = fake_client.post(
            "/routes",
            json={"stops": stops_for(dogs[0]), "walker_id": "someone-else"},
            headers=bearer(W),
        )
        assert res.status_code == 201
        assert fake.tables["routes"][0]["walker_id"] == "walker-w"

    @pytest.mark.parametrize(
        "body",
        [{}, {"stops": []}, {"stops": "x"}, {"stops": [{"dogId": "d"}]},
         {"stops": [{"pickupTime": "t"}]}, {"stops": [{"dogId": "", "pickupTime": "t"}]},
         {"stops": [{"dogId": "d", "pickupTime": ""}]}],
    )
    def test_invalid_body_is_400_and_stores_nothing(self, fake_client, fake, body):
        res = fake_client.post("/routes", json=body, headers=bearer(W))
        assert res.status_code == 400
        assert fake.tables.get("routes", []) == []

    def test_a_stop_whose_dog_does_not_exist_gets_null_name(self, fake_client):
        res = create_route(fake_client, [{"dogId": "ghost", "pickupTime": "2026-10-05T08:00:00Z"}])
        assert res.status_code == 201
        assert res.json()["stops"][0]["dogName"] is None

    def test_database_error_is_500(self, fake_client, fake, dogs):
        fake.fail_with = APIError({"message": "down", "code": "X", "hint": None, "details": None})
        assert create_route(fake_client, stops_for(dogs[0])).status_code == 500


class TestRead:
    def test_lists_only_own_routes(self, fake_client, dogs):
        create_route(fake_client, stops_for(dogs[0]), W)
        create_route(fake_client, stops_for(dogs[1]), OTHER)
        res = fake_client.get("/routes", headers=bearer(W))
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["stops"][0]["dogName"] == "Firulais"

    def test_list_is_empty_for_a_new_walker(self, fake_client):
        assert fake_client.get("/routes", headers=bearer(W)).json() == []

    def test_gets_one_own_route(self, fake_client, dogs):
        route = create_route(fake_client, stops_for(*dogs[:2])).json()
        res = fake_client.get(f"/routes/{route['id']}", headers=bearer(W))
        assert res.status_code == 200
        assert res.json()["id"] == route["id"]
        assert len(res.json()["stops"]) == 2

    def test_someone_elses_route_is_404(self, fake_client, dogs):
        route = create_route(fake_client, stops_for(dogs[0]), OTHER).json()
        assert fake_client.get(f"/routes/{route['id']}", headers=bearer(W)).status_code == 404

    def test_unknown_route_is_404(self, fake_client):
        res = fake_client.get(f"/routes/{uuid.uuid4()}", headers=bearer(W))
        assert res.status_code == 404
        assert res.json() == {"detail": "Route not found."}

    def test_non_uuid_route_id_is_400(self, fake_client):
        assert fake_client.get("/routes/not-a-uuid", headers=bearer(W)).status_code == 400


class TestReorder:
    def reorder(self, client, route_id, order, token=W):
        return client.patch(
            f"/routes/{route_id}/stops/reorder", json={"order": order}, headers=bearer(token)
        )

    def test_reorders_stops_by_index_permutation(self, fake_client, fake, dogs):
        route = create_route(fake_client, stops_for(*dogs)).json()
        res = self.reorder(fake_client, route["id"], [2, 0, 1])
        assert res.status_code == 200
        assert [s["dogName"] for s in res.json()["stops"]] == ["Luna", "Firulais", "Rex"]
        stored = fake.tables["routes"][0]["stops"]
        assert [s["dog_id"] for s in stored] == [dogs[2]["id"], dogs[0]["id"], dogs[1]["id"]]

    @pytest.mark.parametrize(
        "order",
        [[0, 1], [0, 1, 2, 3], [0, 0, 1], [0, 1, 3], [-1, 0, 1], [0, 1, "x"], []],
    )
    def test_anything_but_a_full_permutation_is_400_and_changes_nothing(
        self, fake_client, fake, dogs, order
    ):
        route = create_route(fake_client, stops_for(*dogs)).json()
        before = list(fake.tables["routes"][0]["stops"])
        assert self.reorder(fake_client, route["id"], order).status_code == 400
        assert fake.tables["routes"][0]["stops"] == before

    def test_someone_elses_route_is_404(self, fake_client, dogs):
        route = create_route(fake_client, stops_for(*dogs), OTHER).json()
        assert self.reorder(fake_client, route["id"], [2, 1, 0]).status_code == 404


class TestRemoveStop:
    def remove(self, client, route_id, index, token=W):
        return client.delete(f"/routes/{route_id}/stops/{index}", headers=bearer(token))

    def test_removes_the_stop_at_an_index(self, fake_client, fake, dogs):
        route = create_route(fake_client, stops_for(*dogs)).json()
        res = self.remove(fake_client, route["id"], 1)
        assert res.status_code == 200
        assert [s["dogName"] for s in res.json()["stops"]] == ["Firulais", "Luna"]
        assert len(fake.tables["routes"][0]["stops"]) == 2

    @pytest.mark.parametrize("index", [3, 99, -1, "abc", "1.5"])
    def test_bad_index_is_400_and_changes_nothing(self, fake_client, fake, dogs, index):
        route = create_route(fake_client, stops_for(*dogs)).json()
        assert self.remove(fake_client, route["id"], index).status_code == 400
        assert len(fake.tables["routes"][0]["stops"]) == 3

    def test_someone_elses_route_is_404(self, fake_client, dogs):
        route = create_route(fake_client, stops_for(*dogs), OTHER).json()
        assert self.remove(fake_client, route["id"], 0).status_code == 404


class TestNextPickup:
    def test_returns_the_earliest_pickup_across_all_own_routes(self, fake_client, dogs):
        create_route(fake_client, stops_for(dogs[0], dogs[1], times=[
            "2026-10-05T10:00:00Z", "2026-10-05T09:00:00Z"]))
        second = create_route(fake_client, stops_for(dogs[2], times=["2026-10-05T07:30:00Z"])).json()
        # another walker's earlier route must be ignored
        create_route(fake_client, stops_for(dogs[0], times=["2026-10-05T06:00:00Z"]), OTHER)
        res = fake_client.get("/routes/next-pickup", headers=bearer(W))
        assert res.status_code == 200
        assert res.json() == {
            "routeId": second["id"],
            "dogId": dogs[2]["id"],
            "pickupTime": "2026-10-05T07:30:00Z",
        }

    def test_no_routes_is_404(self, fake_client):
        assert fake_client.get("/routes/next-pickup", headers=bearer(W)).status_code == 404

    def test_routes_without_stops_is_404(self, fake_client, fake):
        fake.seed("routes", walker_id="walker-w", stops=[])
        assert fake_client.get("/routes/next-pickup", headers=bearer(W)).status_code == 404


class TestShortestPath:
    def path(self, client, body):
        return client.post("/routes/path", json=body, headers=bearer(W))

    def test_computes_the_shortest_path_over_a_provided_graph(self, fake_client):
        res = self.path(fake_client, {
            "edges": [{"from": "home", "to": "b", "weight": 10},
                      {"from": "home", "to": "c", "weight": 1},
                      {"from": "c", "to": "b", "weight": 1}],
            "start": "home", "end": "b",
        })
        assert res.status_code == 200
        assert res.json() == {"distance": 2, "path": ["home", "c", "b"]}

    def test_no_path_is_400(self, fake_client):
        res = self.path(fake_client, {
            "edges": [{"from": "a", "to": "b", "weight": 1}], "start": "b", "end": "a"})
        assert res.status_code == 400

    def test_unknown_node_is_400(self, fake_client):
        res = self.path(fake_client, {
            "edges": [{"from": "a", "to": "b", "weight": 1}], "start": "a", "end": "zzz"})
        assert res.status_code == 400

    @pytest.mark.parametrize(
        "body",
        [{}, {"edges": [], "start": "a"}, {"edges": [], "end": "a"},
         {"edges": [{"from": "a", "to": "b", "weight": -1}], "start": "a", "end": "b"},
         {"edges": [{"from": "", "to": "b", "weight": 1}], "start": "a", "end": "b"},
         {"edges": [{"from": "a", "to": "b", "weight": "x"}], "start": "a", "end": "b"},
         {"edges": [{"from": "a", "to": "b"}], "start": "a", "end": "b"}],
    )
    def test_invalid_body_is_400(self, fake_client, body):
        assert self.path(fake_client, body).status_code == 400

    def test_empty_edges_with_start_equal_end_is_400_because_the_node_is_unknown(self, fake_client):
        assert self.path(fake_client, {"edges": [], "start": "a", "end": "a"}).status_code == 400


def test_every_route_query_is_scoped_to_the_walker(fake_client, fake, dogs):
    route = create_route(fake_client, stops_for(*dogs)).json()
    fake_client.get("/routes", headers=bearer(W))
    fake_client.get(f"/routes/{route['id']}", headers=bearer(W))
    fake_client.patch(
        f"/routes/{route['id']}/stops/reorder", json={"order": [1, 0, 2]}, headers=bearer(W)
    )
    fake_client.delete(f"/routes/{route['id']}/stops/0", headers=bearer(W))
    fake_client.get("/routes/next-pickup", headers=bearer(W))
    route_queries = [q for q in fake.queries if q[0] == "routes" and q[1] != "insert"]
    assert route_queries
    for _table, _op, filters in route_queries:
        assert ("walker_id", "walker-w") in filters
