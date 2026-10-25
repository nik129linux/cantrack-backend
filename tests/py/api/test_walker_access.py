"""Walker foundation: the /walkers/{walkerId} guard and the online heartbeat."""

from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from cantrack_api.walker_access import ONLINE_TTL_SECONDS, is_online, walker_from_path

from .conftest import FIXED_NOW, bearer


@pytest.fixture
def guarded(fake_client):
    app = fake_client.app

    @app.get("/walkers/{walkerId}/_guard_probe")
    def probe(user=Depends(walker_from_path)):
        return {"id": user.id}

    return TestClient(app)


class TestGuard:
    def test_the_walker_named_in_the_path_passes(self, guarded):
        res = guarded.get("/walkers/walker-w/_guard_probe", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == {"id": "walker-w"}

    def test_another_walker_id_is_403(self, guarded):
        res = guarded.get("/walkers/walker-other/_guard_probe", headers=bearer("token-w"))
        assert res.status_code == 403
        assert res.json() == {"detail": "You can only access your own account."}

    def test_an_owner_is_403_even_with_their_own_id(self, guarded):
        res = guarded.get("/walkers/owner-a/_guard_probe", headers=bearer("token-a"))
        assert res.status_code == 403
        assert res.json() == {"detail": "Only walkers can use this."}

    def test_no_token_is_401(self, guarded):
        assert guarded.get("/walkers/walker-w/_guard_probe").status_code == 401


class TestOnline:
    def at(self, seconds_ago):
        return {"updated_at": (FIXED_NOW - timedelta(seconds=seconds_ago)).isoformat()}

    def test_ttl_is_two_minutes(self):
        assert ONLINE_TTL_SECONDS == 120

    @pytest.mark.parametrize("seconds_ago, online", [
        (0, True), (120, True), (121, False), (3600, False), (-30, True),
    ])
    def test_heartbeat_age(self, seconds_ago, online):
        assert is_online(self.at(seconds_ago), FIXED_NOW) is online

    @pytest.mark.parametrize("row", [None, {}, {"updated_at": None}, {"updated_at": "yesterday"}])
    def test_missing_or_broken_rows_are_offline(self, row):
        assert is_online(row, FIXED_NOW) is False
