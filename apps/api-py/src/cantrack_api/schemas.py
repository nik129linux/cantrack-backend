"""Pydantic request bodies for the auth, dogs and routes endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool

UserRole = Literal["walker", "owner"]


class SignupBody(BaseModel):
    """Body of ``POST /auth/signup``."""

    email: EmailStr
    password: str = Field(min_length=1)
    role: UserRole


class LoginBody(BaseModel):
    """Body of ``POST /auth/login``."""

    email: EmailStr
    password: str = Field(min_length=1)


class ResetPasswordBody(BaseModel):
    """Body of ``POST /auth/reset-password``."""

    email: EmailStr


class CreateDogBody(BaseModel):
    """Body of ``POST /dogs``.

    Only the columns an owner is allowed to set are declared, so any other
    field sent by the client (``owner_id``, ``id``, ``embedding``...) is
    dropped instead of reaching the database.
    """

    name: str = Field(min_length=1)
    breed: str | None = None
    notes: str | None = None


class UpdateDogBody(BaseModel):
    """Body of ``PATCH /dogs/{dog_id}``.

    Every field is optional and unset fields are left out of the update, so a
    partial body only touches the columns it actually carries.
    """

    name: str | None = Field(default=None, min_length=1)
    breed: str | None = None
    notes: str | None = None


class RouteStopBody(BaseModel):
    """One pickup stop as the client sends it (FR-06).

    Only the dog to visit and when to collect it are accepted, so nothing else
    reaches the database. A route with no usable stop is rejected: a walk that
    visits nobody is not a walk.
    """

    dogId: str = Field(min_length=1)
    pickupTime: str = Field(min_length=1)


class CreateRouteBody(BaseModel):
    """Body of ``POST /routes``.

    The walker is always taken from the bearer token, so a ``walker_id`` in the
    body is dropped by the schema rather than trusted.
    """

    stops: list[RouteStopBody] = Field(min_length=1)


class ReorderRouteStopsBody(BaseModel):
    """Body of ``PATCH /routes/{route_id}/stops/reorder`` (FR-07).

    ``order`` is a list of current stop indexes, so it must name at least one
    stop; the router rejects it unless it is a permutation of every index.
    """

    order: list[int] = Field(min_length=1)


class ConfirmCheckinBody(BaseModel):
    """Body of ``POST /routes/{route_id}/checkin/confirm`` (FR-10).

    The walker is looking at the candidates the check-in answered with and picking
    one by hand, so the only thing to send is which dog it is.
    """

    dogId: str = Field(min_length=1)


class PathEdgeBody(BaseModel):
    """One directed walk leg of the graph sent with ``POST /routes/path`` (FR-09).

    ``from`` is a Python keyword, so the field is declared as ``from_`` and read
    from the ``from`` alias clients actually send. ``populate_by_name`` also lets
    the snake_case name be used directly.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    weight: float = Field(ge=0, allow_inf_nan=False)


class RoutePathBody(BaseModel):
    """Body of ``POST /routes/path`` (FR-09).

    The graph is supplied per request and lives only for the length of the
    call, so ``edges`` may legitimately be empty — in which case any path is
    impossible. Costs must be finite and non-negative for the shortest path to
    mean anything.
    """

    edges: list[PathEdgeBody] = Field(default_factory=list)
    start: str = Field(min_length=1)
    end: str = Field(min_length=1)

class SaveWalkerProfileBody(BaseModel):
    """Body of ``PUT /walker-profile`` (S1).

    The profile always belongs to the caller (``walker_id`` comes from the
    bearer token), and the price is a non-negative integer in COP. Any other
    field sent by the client is dropped by the schema.
    """

    displayName: str = Field(min_length=1)
    bio: str | None = None
    serviceArea: str | None = None
    pricePerWalk: int = Field(ge=0)


class DogProfileBody(BaseModel):
    """Body of ``PUT /dogs/{dog_id}/profile`` — the S1 questionnaire.

    All eight fields are declared, the four enums/bool are required and the
    four free-text ones are optional; a PUT replaces the whole questionnaire,
    so nothing is merged with what was stored before.
    """

    size: Literal["small", "medium", "large"]
    temperament: Literal["friendly", "shy", "reactive"]
    energy: Literal["low", "medium", "high"]
    # Strict: a real JSON boolean, never a coerced string ("yes" must be a 400).
    leashTrained: StrictBool
    allergies: str | None = None
    medicalNotes: str | None = None
    vetContact: str | None = None
    emergencyContact: str | None = None


class CreateRequestBody(BaseModel):
    """Body of ``POST /requests`` (S1).

    The owner is always the caller, and ``price_cop`` is never taken from the
    body: the server copies it from the walker's profile. Coordinates are the
    pin the owner dropped (no geocoding service), so they must be valid WGS84
    numbers; ``requestedTime`` is an ISO-8601 string the router parses to
    enforce the timezone offset and the future-time rule.
    """

    walkerId: str = Field(min_length=1)
    dogId: str = Field(min_length=1)
    # No min_length on purpose: the empty string must reach the router's
    # ISO-8601 parser and answer the exact TIME_MESSAGE 400, not pydantic's
    # generic validation list.
    requestedTime: str
    pickupLat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    pickupLng: float = Field(ge=-180, le=180, allow_inf_nan=False)
