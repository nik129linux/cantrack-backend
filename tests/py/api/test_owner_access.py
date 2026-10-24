"""The /owners/{ownerId} guard: only that owner, never a walker, never another owner."""

import pytest
from fastapi import APIRouter, Depends, FastAPI

from cantrack_api.owner_access import owner_from_path

from .conftest import bearer

NOT_YOURS = {"detail": "You can only access your own account."}
OWNERS_ONLY = {"detail": "Only owners can use this."}


@pytest.fixture
def probe_client(fake_client):
    router = APIRouter()

    @router.get("/owners/{ownerId}/probe")
    def probe(owner=Depends(owner_from_path)):
        return {"id": owner.id}

    fake_client.app.include_router(router)
    return fake_client


def test_the_owner_reaches_their_own_path(probe_client):
    res = probe_client.get("/owners/owner-a/probe", headers=bearer("token-a"))
    assert res.status_code == 200
    assert res.json() == {"id": "owner-a"}


def test_another_owner_is_refused(probe_client):
    res = probe_client.get("/owners/owner-a/probe", headers=bearer("token-b"))
    assert res.status_code == 403
    assert res.json() == NOT_YOURS


def test_an_unknown_id_gets_the_same_answer(probe_client):
    res = probe_client.get("/owners/does-not-exist/probe", headers=bearer("token-b"))
    assert res.json() == NOT_YOURS


def test_a_walker_is_refused_even_on_their_own_id(probe_client):
    res = probe_client.get("/owners/walker-w/probe", headers=bearer("token-w"))
    assert res.status_code == 403
    assert res.json() == OWNERS_ONLY


def test_no_token_is_401(probe_client):
    assert probe_client.get("/owners/owner-a/probe").status_code == 401


def test_overlong_id_is_400(probe_client):
    assert probe_client.get("/owners/" + "x" * 65 + "/probe", headers=bearer("token-a")).status_code == 400
