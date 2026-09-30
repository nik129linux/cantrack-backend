"""FR-06..FR-11: a walker's routes as ordered stops, reorder/remove, next pickup,
and checking a dog in by photo.

A route is nothing but an ordered list of stops, and the hand-written structures
earn their keep here: the stops are held in a ``SinglyLinkedList`` so reordering
and removing are index operations on a chain rather than array splices, the
earliest pickup is a ``AvlTree.min()`` over every stop the walker owns, the walk
between two nodes is a ``WeightedGraph.shortest_path``, and undoing a check-in
pops the newest row off a ``Stack``.

A route row only ever stores a dog's id per stop, so every route this router
returns is enriched with the dogs' names through a single batched lookup.
"""

import uuid
from operator import itemgetter
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from supabase import Client
from supabase_auth.types import User

from ..ai.embeddings import ClipEmbedder, cosine_similarity, parse_embedding
from ..ai.vision import OllamaVision
from ..db import first_row, run_query
from ..deps import get_current_user, get_embedder, get_supabase, get_vision
from ..schemas import (
    ConfirmCheckinBody,
    CreateRouteBody,
    ReorderRouteStopsBody,
    RoutePathBody,
    RouteStopBody,
)
from ..structures import AvlTree, SinglyLinkedList, Stack, WeightedGraph
from ..uploads import embed_image, read_image

router = APIRouter(prefix="/routes")

ROUTES_TABLE = "routes"
DOGS_TABLE = "dogs"
CHECKINS_TABLE = "checkins"

#: Cosine similarity at or above which the check-in is trusted to be the right
#: dog and is written without asking the walker (FR-10).
AUTO_CONFIRM_SIMILARITY = 0.8

NOT_FOUND_MESSAGE = "Route not found."
ORDER_MESSAGE = "Order must contain every stop exactly once."
STOP_INDEX_MESSAGE = "Stop index is invalid."
NO_ROUTES_MESSAGE = "No routes found."
NO_PICKUPS_MESSAGE = "No scheduled pickups found."
NO_PATH_MESSAGE = "No path exists between the requested nodes."
NO_DOG_MESSAGE = "No dog detected in the photo."
NOT_ON_ROUTE_MESSAGE = "That dog is not on this route."
NO_CHECKIN_MESSAGE = "No check-in found."
UNDONE_MESSAGE = "Check-in undone."


def _to_stored_stop(stop: RouteStopBody) -> dict[str, str]:
    """Return the database column names for one stop."""
    return {"dog_id": stop.dogId, "pickup_time": stop.pickupTime}


def _to_api_stop(stop: dict[str, Any]) -> dict[str, Any]:
    """Return the camelCase shape the API answers a stop with."""
    return {"dogId": stop["dog_id"], "pickupTime": stop["pickup_time"]}


def _stored_stops(route: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the raw stored stops of a route, tolerating a null column."""
    return list(route.get("stops") or [])


def _enrich_routes(
    supabase: Client, routes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add each stop's ``dogName`` to every route, with one batched query.

    A route's row never stores the dog's name, and asking for each stop's dog
    separately would be one query per stop. All the distinct dog ids across the
    given routes are fetched in a single round trip instead, and a stop whose
    dog no longer exists keeps a null name rather than failing the request.

    Args:
        supabase: The Supabase client used to read the dogs.
        routes: Stored route rows, already converted to the API stop shape.

    Returns:
        The same routes, each stop now carrying ``dogName``.
    """
    responses = [
        {**route, "stops": [_to_api_stop(stop) for stop in _stored_stops(route)]}
        for route in routes
    ]
    dog_ids = list(
        dict.fromkeys(
            stop["dogId"] for route in responses for stop in route["stops"]
        )
    )
    if not dog_ids:
        return responses

    dogs = run_query(supabase.table(DOGS_TABLE).select("id,name").in_("id", dog_ids))
    names_by_id = {dog["id"]: dog["name"] for dog in dogs}

    for route in responses:
        for stop in route["stops"]:
            stop["dogName"] = names_by_id.get(stop["dogId"])

    return responses


def _enrich_route(supabase: Client, route: dict[str, Any]) -> dict[str, Any]:
    """Return one stored route row in the API's shape, with its dog's names."""
    return _enrich_routes(supabase, [route])[0]


def _load_route(supabase: Client, walker_id: str, route_id: uuid.UUID) -> dict[str, Any]:
    """Read one of the walker's own routes, or raise.

    Both filters are mandatory: without the ``walker_id`` one a route belonging
    to somebody else would be readable, and without the ``id`` one the walker's
    first route would answer every request.

    Args:
        supabase: The Supabase client used to read the row.
        walker_id: The id of the authenticated walker.
        route_id: The id of the route to read.

    Returns:
        The stored route row.

    Raises:
        HTTPException: 404 if the route does not exist or is not the caller's.
    """
    rows = run_query(
        supabase.table(ROUTES_TABLE)
        .select("*")
        .eq("id", str(route_id))
        .eq("walker_id", walker_id)
    )
    return first_row(rows, NOT_FOUND_MESSAGE)


def _save_stops(
    supabase: Client, walker_id: str, route_id: uuid.UUID, stops: list[dict[str, Any]]
) -> dict[str, Any]:
    """Write a whole new stop list back onto one of the walker's routes.

    Args:
        supabase: The Supabase client used to write the row.
        walker_id: The id of the authenticated walker.
        route_id: The id of the route to update.
        stops: The new stored stops, in order.

    Returns:
        The route as stored after the update, in the API's shape.

    Raises:
        HTTPException: 404 if the route is not the caller's after all.
    """
    rows = run_query(
        supabase.table(ROUTES_TABLE)
        .update({"stops": stops})
        .eq("id", str(route_id))
        .eq("walker_id", walker_id)
    )
    return _enrich_route(supabase, first_row(rows, NOT_FOUND_MESSAGE))


def _stop_dog_ids(route: dict[str, Any]) -> list[str]:
    """Return the route's dog ids in stop order, without repeats.

    A route may visit the same dog twice (a drop-off and a pick-up, say), but a
    check-in is about which dogs the walk covers, so the second visit adds
    nothing. The order is kept because it is the order the walker will meet them.

    Args:
        route: A stored route row.

    Returns:
        The distinct dog ids of the route's stops, in stop order.
    """
    return list(dict.fromkeys(stop["dog_id"] for stop in _stored_stops(route)))


def _record_checkin(supabase: Client, route_id: uuid.UUID, dog_id: str) -> str:
    """Write one confirmed check-in and return its generated id.

    Args:
        supabase: The Supabase client used to insert the row.
        route_id: The id of the route the dog was checked in on.
        dog_id: The id of the dog that was checked in.

    Returns:
        The id the database generated for the new check-in.

    Raises:
        HTTPException: 404 if the insert produced no row, or 500 if the database
            cannot be written.
    """
    rows = run_query(
        supabase.table(CHECKINS_TABLE).insert(
            {"route_id": str(route_id), "dog_id": dog_id}
        )
    )
    return str(first_row(rows, NOT_FOUND_MESSAGE)["id"])


def _rank_candidates(
    supabase: Client, embedding: list[float], dog_ids: list[str]
) -> list[dict[str, Any]]:
    """Score every dog of the route against a photo and order them best first.

    One batched lookup covers every dog on the route, so a six-stop walk is one
    query instead of six. A dog that was never enrolled scores zero rather than
    being dropped: the walker is the one who can see it in the photo, so it still
    belongs in the list to be picked by hand. The sort is stable, so equally
    scored dogs keep the order the route visits them in.

    Args:
        supabase: The Supabase client used to read the dogs.
        embedding: The photo's embedding.
        dog_ids: The route's distinct dog ids, in stop order.

    Returns:
        One ``{dogId, dogName, similarity}`` entry per dog found, sorted by
        descending similarity.
    """
    dogs = run_query(
        supabase.table(DOGS_TABLE).select("id,name,embedding").in_("id", dog_ids)
    )
    by_id = {dog["id"]: dog for dog in dogs}

    candidates: list[dict[str, Any]] = []
    for dog_id in dog_ids:
        dog = by_id.get(dog_id)
        if dog is None:
            continue
        stored = parse_embedding(dog.get("embedding"))
        similarity = 0.0 if stored is None else cosine_similarity(embedding, stored)
        candidates.append(
            {"dogId": dog_id, "dogName": dog.get("name"), "similarity": similarity}
        )

    candidates.sort(key=itemgetter("similarity"), reverse=True)
    return candidates


@router.get("/next-pickup")
def next_pickup(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, str]:
    """Find the stop due soonest across all of the walker's routes (FR-08).

    Every stop of every route is keyed by its pickup time in one AVL tree, so
    the answer is a single leftmost descent — O(log n) — instead of comparing
    every stop against a running best.

    Args:
        user: The authenticated walker.
        supabase: The Supabase client used to read the routes.

    Returns:
        The ``routeId``, ``dogId`` and ``pickupTime`` of the earliest stop.

    Raises:
        HTTPException: 404 if the walker has no routes at all, or has routes but
            none of them schedules a pickup.
    """
    routes = run_query(supabase.table(ROUTES_TABLE).select("*").eq("walker_id", user.id))
    if not routes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_ROUTES_MESSAGE
        )

    tree: AvlTree[str, dict[str, str]] = AvlTree()
    for route in routes:
        for stop in _stored_stops(route):
            tree.insert(
                stop["pickup_time"],
                {
                    "routeId": route["id"],
                    "dogId": stop["dog_id"],
                    "pickupTime": stop["pickup_time"],
                },
            )

    if tree.size() == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_PICKUPS_MESSAGE
        )

    return tree.min()[1]


@router.post("/path")
def compute_path(
    body: RoutePathBody,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the cheapest walk between two nodes of a client-supplied graph (FR-09).

    The graph is built per request from the body's edges and thrown away
    afterwards: it describes this walk, not a persisted street network.

    Args:
        body: The directed legs, the start node and the target node.
        user: The authenticated walker.

    Returns:
        The total ``distance`` and the ``path`` of nodes it walks through.

    Raises:
        HTTPException: 400 if either node is unknown or the target cannot be
            reached from the start.
    """
    graph: WeightedGraph[str] = WeightedGraph()
    try:
        for edge in body.edges:
            graph.add_edge(edge.from_, edge.to, edge.weight)
        shortest = graph.shortest_path(body.start, body.end)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NO_PATH_MESSAGE
        ) from exc

    return {"distance": shortest.distance, "path": shortest.path}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_route(
    body: CreateRouteBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Plan a new walk for the caller (FR-06).

    The stops are appended to a singly linked list in the order they were sent
    and stored from it, so the order the client asked for is the order that is
    walked. The walker always comes from the bearer token.

    Args:
        body: The ordered stops of the walk.
        user: The authenticated walker.
        supabase: The Supabase client used to insert the route and read the dogs.

    Returns:
        The stored route, with each stop's dog name attached.

    Raises:
        HTTPException: 500 if the insert fails.
    """
    stops: SinglyLinkedList[RouteStopBody] = SinglyLinkedList()
    for stop in body.stops:
        stops.append(stop)

    rows = run_query(
        supabase.table(ROUTES_TABLE).insert(
            {
                "walker_id": user.id,
                "stops": [_to_stored_stop(stop) for stop in stops.to_array()],
            }
        )
    )
    return _enrich_route(supabase, first_row(rows, NOT_FOUND_MESSAGE))


@router.get("")
def list_routes(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List every route planned by the caller (FR-06).

    Args:
        user: The authenticated walker.
        supabase: The Supabase client used to read the routes and the dogs.

    Returns:
        Every route whose ``walker_id`` is the caller's, possibly empty.

    Raises:
        HTTPException: 500 if the database cannot be read.
    """
    routes = run_query(supabase.table(ROUTES_TABLE).select("*").eq("walker_id", user.id))
    return _enrich_routes(supabase, routes)


@router.get("/{route_id}")
def get_route(
    route_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Read one of the caller's routes in full (FR-06).

    Args:
        route_id: The id of the route to read.
        user: The authenticated walker.
        supabase: The Supabase client used to read the route and the dogs.

    Returns:
        The route, with each stop's dog name attached.

    Raises:
        HTTPException: 404 if the route does not exist or is not the caller's.
    """
    return _enrich_route(supabase, _load_route(supabase, user.id, route_id))


@router.patch("/{route_id}/stops/reorder")
def reorder_route_stops(
    route_id: uuid.UUID,
    body: ReorderRouteStopsBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Walk the same stops in a different order (FR-07).

    ``order`` is a list of the route's current stop indexes, and it has to name
    every one of them exactly once: a shorter, longer, repeated or out-of-range
    list would silently drop or duplicate a dog, so it is rejected before
    anything is written. The reordered stops are appended to a fresh singly
    linked list in the requested order.

    Args:
        route_id: The id of the route to reorder.
        body: The new order, as indexes into the current stops.
        user: The authenticated walker.
        supabase: The Supabase client used to read and write the route.

    Returns:
        The route as stored after the reorder, in the API's shape.

    Raises:
        HTTPException: 400 if ``order`` is not a permutation of the route's
            stop indexes, 404 if the route is not the caller's.
    """
    route = _load_route(supabase, user.id, route_id)
    stops = _stored_stops(route)

    if sorted(body.order) != list(range(len(stops))):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_MESSAGE
        )

    reordered: SinglyLinkedList[dict[str, Any]] = SinglyLinkedList()
    for index in body.order:
        reordered.append(stops[index])

    return _save_stops(supabase, user.id, route_id, reordered.to_array())


@router.delete("/{route_id}/stops/{index}")
def remove_route_stop(
    route_id: uuid.UUID,
    index: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Drop one stop from a route (FR-07).

    The index arrives as a path segment, so anything that is not a whole number
    is rejected here; the list itself rejects an index it does not have, which
    covers a negative one and one past the end.

    Args:
        route_id: The id of the route to edit.
        index: The position of the stop to drop, as sent in the URL.
        user: The authenticated walker.
        supabase: The Supabase client used to read and write the route.

    Returns:
        The route as stored after the removal, in the API's shape.

    Raises:
        HTTPException: 400 if the index is not a position of this route, or 404
            if the route is not the caller's.
    """
    route = _load_route(supabase, user.id, route_id)

    try:
        position = int(index)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=STOP_INDEX_MESSAGE
        ) from exc

    stops: SinglyLinkedList[dict[str, Any]] = SinglyLinkedList()
    for stop in _stored_stops(route):
        stops.append(stop)

    try:
        stops.remove_at(position)
    except IndexError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=STOP_INDEX_MESSAGE
        ) from exc

    return _save_stops(supabase, user.id, route_id, stops.to_array())


@router.post("/{route_id}/checkin")
def check_in(
    route_id: uuid.UUID,
    response: Response,
    image: UploadFile = File(...),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    embedder: ClipEmbedder = Depends(get_embedder),
    vision: OllamaVision = Depends(get_vision),
) -> dict[str, Any]:
    """Check a dog in on one of the caller's routes from a photo (FR-10).

    The photo is embedded on the server and compared against every dog on the
    route. A confident match is written straight away and answered with 201; an
    unsure one is not written at all — the walker is handed the ranking and picks
    with their own eyes, because a wrong auto-confirmed check-in is worse than a
    question.

    The vision model is asked separately whether a dog is even in the photo. That
    answer is the one hard gate: a photo of an empty pavement is a mistake worth
    rejecting. The model being unavailable is not — ``dog_visible`` is then
    ``None``, which blocks nothing, and the check-in carries on without it.

    Nothing is loaded until the route is known to be the caller's, so a request
    against somebody else's route cannot make the server run inference.

    Args:
        route_id: The id of the route being walked.
        response: The response object, used to answer 201 on an auto-confirmed
            check-in.
        image: The check-in photo, as the ``image`` multipart field.
        user: The authenticated walker.
        supabase: The Supabase client used to read the route and dogs and to
            write the check-in.
        embedder: The CLIP embedder, injected as a dependency.
        vision: The vision model, injected as a dependency.

    Returns:
        With a confident match, ``{autoConfirmed: true, dogId, dogName,
        similarity, checkinId, ai}``; otherwise ``{autoConfirmed: false,
        candidates: [{dogId, dogName, similarity}...], ai}``.

    Raises:
        HTTPException: 400 if the upload is not a readable image or the photo
            shows no dog, 404 if the route is not the caller's, or 413/503 for an
            oversized upload or a model failure.
    """
    photo = read_image(image)
    route = _load_route(supabase, user.id, route_id)

    dog_ids = _stop_dog_ids(route)
    if not dog_ids:
        # A route with nobody on it cannot be checked into, and there is nothing
        # to compare the photo against, so no model is worth loading.
        return {
            "autoConfirmed": False,
            "candidates": [],
            "ai": {"dogVisible": None, "note": None},
        }

    embedding = embed_image(embedder, photo)
    analysis = vision.check(photo)
    if analysis.dog_visible is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NO_DOG_MESSAGE
        )
    ai = {"dogVisible": analysis.dog_visible, "note": analysis.note}

    candidates = _rank_candidates(supabase, embedding, dog_ids)
    best = candidates[0] if candidates else None
    if best is None or best["similarity"] < AUTO_CONFIRM_SIMILARITY:
        return {"autoConfirmed": False, "candidates": candidates, "ai": ai}

    response.status_code = status.HTTP_201_CREATED
    return {
        "autoConfirmed": True,
        "dogId": best["dogId"],
        "dogName": best["dogName"],
        "similarity": best["similarity"],
        "checkinId": _record_checkin(supabase, route_id, best["dogId"]),
        "ai": ai,
    }


@router.post("/{route_id}/checkin/confirm", status_code=status.HTTP_201_CREATED)
def confirm_checkin(
    route_id: uuid.UUID,
    body: ConfirmCheckinBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Check a dog in by hand, after an unsure photo (FR-10).

    The dog's id comes from the candidates the check-in answered with, so it is
    checked against the route's own stops: a walker cannot check a dog in that is
    not on the walk.

    Args:
        route_id: The id of the route being walked.
        body: The dog the walker picked.
        user: The authenticated walker.
        supabase: The Supabase client used to read the route and dogs and to
            write the check-in.

    Returns:
        The ``dogId``, its ``dogName`` and the ``checkinId`` that was written.

    Raises:
        HTTPException: 400 if the dog is not on this route, or 404 if the route is
            not the caller's.
    """
    route = _load_route(supabase, user.id, route_id)

    if body.dogId not in _stop_dog_ids(route):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_ON_ROUTE_MESSAGE
        )

    dogs = run_query(
        supabase.table(DOGS_TABLE).select("id,name").in_("id", [body.dogId])
    )
    dog = first_row(dogs, NOT_ON_ROUTE_MESSAGE)

    return {
        "dogId": dog["id"],
        "dogName": dog["name"],
        "checkinId": _record_checkin(supabase, route_id, dog["id"]),
    }


@router.delete("/{route_id}/checkin/undo")
def undo_checkin(
    route_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, str]:
    """Undo the most recent check-in on one of the caller's routes (FR-11).

    The check-ins are read oldest first and pushed onto a ``Stack`` in that
    order, so the pop returns the newest one — the one the walker just made — and
    the rows below it stay exactly as they were. That LIFO order is the whole
    point of the undo: a mistaken check-in is always the last thing that happened.

    Args:
        route_id: The id of the route to undo on.
        user: The authenticated walker.
        supabase: The Supabase client used to read and delete the check-in.

    Returns:
        A short confirmation message.

    Raises:
        HTTPException: 404 if the route is not the caller's or it has no check-ins
            to undo, or 500 if the database cannot be read or written.
    """
    _load_route(supabase, user.id, route_id)

    rows = run_query(
        supabase.table(CHECKINS_TABLE)
        .select("*")
        .eq("route_id", str(route_id))
        .order("created_at")
    )

    history: Stack[dict[str, Any]] = Stack()
    for row in rows:
        history.push(row)

    if history.is_empty():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_CHECKIN_MESSAGE
        )

    latest = history.pop()
    run_query(
        supabase.table(CHECKINS_TABLE)
        .delete()
        .eq("id", latest["id"])
        .eq("route_id", str(route_id))
    )
    return {"message": UNDONE_MESSAGE}
