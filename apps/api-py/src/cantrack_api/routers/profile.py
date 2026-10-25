"""Perfil: the owner's profile, avatar and data-protection consent (PR4).

Every endpoint hangs from ``owner_access.owner_from_path``, so the owner id
always comes from the path guard and never from the request body. The wire
format is camelCase; ``owner_profiles`` is snake_case with one row per owner.
"""

import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from PIL import Image, UnidentifiedImageError
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from storage3.exceptions import StorageApiError
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..db import first_row, run_query
from ..deps import get_now, get_supabase
from ..owner_access import owner_from_path

router = APIRouter()

PROFILES_TABLE = "owner_profiles"
DOGS_TABLE = "dogs"
WALK_REQUESTS_TABLE = "walk_requests"
AVATAR_BUCKET = "avatars"
SIGNED_URL_SECONDS = 3600
#: Largest canvas accepted before decoding (25 MP ~ 5000 x 5000).
MAX_AVATAR_PIXELS = 25_000_000
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_SIDE = 512

TOO_BIG_MESSAGE = "The avatar must be 2 MB or less."
UNREADABLE_MESSAGE = "Unreadable image."
SAVED_MESSAGE = "Profile not found."

PHONE_PATTERN = r"^\+?[0-9][0-9 ]{6,19}$"


class Home(BaseModel):
    """The owner's saved home pin."""

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    label: str = Field(max_length=120)


class SaveProfileBody(BaseModel):
    """Body of ``PUT /owners/{ownerId}/profile``.

    Only the fields an owner may set are declared, so ``ownerId`` or any other
    column sent by the client is dropped by the schema.
    """

    displayName: str | None = Field(default=None, min_length=1, max_length=80)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    home: Home | None = None


class ConsentBody(BaseModel):
    """Body of ``POST /owners/{ownerId}/consent``."""

    version: str = Field(min_length=1, max_length=20)


class Stats(BaseModel):
    """Per-owner activity counters shown on the profile tab."""

    dogs: int
    walksDone: int


class Consent(BaseModel):
    """The version of the terms the owner accepted and when."""

    version: str
    at: str


class OwnerProfile(BaseModel):
    """The full profile payload returned by ``GET`` and ``PUT``."""

    ownerId: str
    email: str | None
    displayName: str | None
    phone: str | None
    avatarUrl: str | None
    home: Home | None
    consent: Consent | None
    stats: Stats


class AvatarResponse(BaseModel):
    """The freshly signed URL of the owner's avatar."""

    avatarUrl: str


def _profile_row(supabase: Client, owner_id: str) -> dict[str, Any]:
    """Return the owner's profile row, or an empty dict when there is none.

    O(1): one indexed lookup by ``owner_id``.

    Args:
        supabase: The Supabase client used to read the row.
        owner_id: The owner whose row is wanted.

    Returns:
        The stored row, or ``{}`` before the owner has saved anything.
    """
    rows = run_query(
        supabase.table(PROFILES_TABLE).select("*").eq("owner_id", owner_id)
    )
    return rows[0] if rows else {}


def _save_row(
    supabase: Client, owner_id: str, values: dict[str, Any]
) -> dict[str, Any]:
    """Write ``values`` to the owner's profile row, inserting it if missing.

    O(1): one lookup plus one update or insert, all keyed by ``owner_id``.

    Args:
        supabase: The Supabase client used to read and write the row.
        owner_id: The owner the row belongs to.
        values: The snake_case columns to set.

    Returns:
        The stored row after the write.

    Raises:
        HTTPException: 500 if the database rejects a query, 404 if the write
            reports no row.
    """
    existing = run_query(
        supabase.table(PROFILES_TABLE).select("owner_id").eq("owner_id", owner_id)
    )
    if existing:
        rows = run_query(
            supabase.table(PROFILES_TABLE).update(values).eq("owner_id", owner_id)
        )
    else:
        rows = run_query(
            supabase.table(PROFILES_TABLE).insert({"owner_id": owner_id, **values})
        )
    return first_row(rows, SAVED_MESSAGE)


def _stats(supabase: Client, owner_id: str) -> Stats:
    """Count the owner's dogs and finished walks.

    O(n) in the number of the owner's rows: the two tables are read with one
    query each and ``walksDone`` is counted in Python (the client offers no
    aggregate).

    Args:
        supabase: The Supabase client used to read the rows.
        owner_id: The owner whose counters are wanted.

    Returns:
        ``{"dogs": n, "walksDone": m}``.
    """
    dogs = run_query(
        supabase.table(DOGS_TABLE).select("id").eq("owner_id", owner_id)
    )
    walks = run_query(
        supabase.table(WALK_REQUESTS_TABLE)
        .select("id,status")
        .eq("owner_id", owner_id)
    )
    return Stats(
        dogs=len(dogs),
        walksDone=sum(1 for walk in walks if walk.get("status") == "done"),
    )


def _signed_avatar(supabase: Client, path: str | None) -> str | None:
    """Return a one-hour signed URL for an avatar path, or None.

    O(1): one signing call for a single object.

    Args:
        supabase: The Supabase client whose private bucket holds the avatar.
        path: The stored object path, or None when there is no avatar.

    Returns:
        The signed URL, or None when no avatar was stored (or it vanished).

    Raises:
        HTTPException: 500 if the storage service rejects the request for any
            reason other than a missing object.
    """
    if not path:
        return None
    try:
        signed = supabase.storage.from_(AVATAR_BUCKET).create_signed_url(
            path, SIGNED_URL_SECONDS
        )
    except StorageApiError as exc:
        if getattr(exc, "code", None) == "not_found":
            return None
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The avatar could not be read.",
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The avatar could not be read.",
        ) from exc
    return signed["signedURL"]


def _view(
    owner: User, row: dict[str, Any], stats: Stats, avatar_url: str | None
) -> OwnerProfile:
    """Translate a stored row to the camelCase profile payload.

    O(1): fixed-size field mapping.

    Args:
        owner: The authenticated owner.
        row: The stored profile row, possibly empty.
        stats: The owner's activity counters.
        avatar_url: The signed avatar URL, or None.

    Returns:
        The profile in the API shape.
    """
    has_home = row.get("home_lat") is not None and row.get("home_lng") is not None
    version = row.get("consent_version")
    at = row.get("consent_at")
    return OwnerProfile(
        ownerId=owner.id,
        email=owner.email,
        displayName=row.get("display_name"),
        phone=row.get("phone"),
        avatarUrl=avatar_url,
        home=(
            Home(lat=row["home_lat"], lng=row["home_lng"], label=row.get("home_label") or "")
            if has_home
            else None
        ),
        consent=Consent(version=version, at=at) if version is not None and at else None,
        stats=stats,
    )


@router.get("/owners/{ownerId}/profile", response_model=OwnerProfile)
@limiter.limit("30/minute")
def get_owner_profile(
    request: Request,
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
) -> OwnerProfile:
    """Return the caller's profile, avatar URL, consent and counters.

    O(n) in the owner's dog and walk rows (the counters), with the rest O(1).

    Args:
        request: The request, read by the rate limiter.
        owner: The authenticated owner named in the path.
        supabase: The Supabase client used to read rows and sign the avatar.

    Returns:
        The profile; a new owner gets nulls and zeroed counters.

    Raises:
        HTTPException: 403 when the caller is not that owner, or 500 if the
            database or storage fails.
    """
    row = _profile_row(supabase, owner.id)
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    return _view(owner, row, _stats(supabase, owner.id), avatar_url)


@router.put("/owners/{ownerId}/profile", response_model=OwnerProfile)
@limiter.limit("30/minute")
def save_owner_profile(
    request: Request,
    body: SaveProfileBody,
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
) -> OwnerProfile:
    """Create or update the caller's profile.

    O(n) in the owner's dog and walk rows (the counters), plus O(1) for the
    profile write.

    Args:
        request: The request, read by the rate limiter.
        body: The fields to set; unset ones keep their stored value.
        owner: The authenticated owner named in the path.
        supabase: The Supabase client used to read and write the row.

    Returns:
        The saved profile in the API shape.

    Raises:
        HTTPException: 400 when a field fails validation, 403 when the caller is
            not that owner, or 500 if the database rejects the write.
    """
    values: dict[str, Any] = {}
    if "displayName" in body.model_fields_set:
        values["display_name"] = body.displayName
    if "phone" in body.model_fields_set:
        values["phone"] = body.phone
    if "home" in body.model_fields_set:
        # owner_profiles stores the pin as three columns (home_lat/home_lng/home_label).
        home = body.home
        values["home_lat"] = home.lat if home else None
        values["home_lng"] = home.lng if home else None
        values["home_label"] = home.label if home else None
    row = _save_row(supabase, owner.id, values)
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    return _view(owner, row, _stats(supabase, owner.id), avatar_url)


@router.post("/owners/{ownerId}/avatar", response_model=AvatarResponse)
@limiter.limit("5/minute")
def upload_avatar(
    request: Request,
    photo: UploadFile = File(...),
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
) -> AvatarResponse:
    """Replace the caller's avatar with a 512 px WEBP and return a signed URL.

    O(n) in the decoded image size: the picture is decoded once, rescaled and
    re-encoded before the old object is removed and the new one stored.

    Args:
        request: The request, read by the rate limiter.
        photo: The ``photo`` multipart part.
        owner: The authenticated owner named in the path.
        supabase: The Supabase client used to store and sign the object.

    Returns:
        ``{"avatarUrl": ...}`` pointing at the private object for one hour.

    Raises:
        HTTPException: 413 when the photo is over 2 MB, 400 when it cannot be
            decoded, 403 when the caller is not that owner, or 500 if storage
            fails.
    """
    data = photo.file.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=TOO_BIG_MESSAGE
        )

    try:
        with Image.open(io.BytesIO(data)) as image:
            # Image.open only reads the header. A tiny file can declare a huge
            # canvas (a decompression bomb) that load() would inflate into
            # gigabytes, so the pixel count is checked before decoding.
            width, height = image.size
            if width * height > MAX_AVATAR_PIXELS:
                raise ValueError("image dimensions too large")
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            image.thumbnail((MAX_AVATAR_SIDE, MAX_AVATAR_SIDE))
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=85)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNREADABLE_MESSAGE
        ) from exc

    path = f"{owner.id}/avatar.webp"
    bucket = supabase.storage.from_(AVATAR_BUCKET)
    # The bucket rejects a duplicate path, so the previous object goes first.
    try:
        bucket.remove([path])
    except (StorageApiError, APIError):
        pass
    bucket.upload(path, buffer.getvalue(), {"content-type": "image/webp"})

    row = _save_row(supabase, owner.id, {"avatar_path": path})
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    if avatar_url is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The avatar could not be read.",
        )
    return AvatarResponse(avatarUrl=avatar_url)


@router.post("/owners/{ownerId}/consent", response_model=Consent)
@limiter.limit("10/minute")
def record_consent(
    request: Request,
    body: ConsentBody,
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> Consent:
    """Record which version of the terms the caller accepted, and when.

    O(1): one profile lookup plus one upsert.

    Args:
        request: The request, read by the rate limiter.
        body: The accepted version, 1 to 20 characters.
        owner: The authenticated owner named in the path.
        supabase: The Supabase client used to write the row.
        now: The current time, injected so tests can freeze it.

    Returns:
        The stored ``{"version", "at"}``.

    Raises:
        HTTPException: 400 when the version is out of bounds, 403 when the
            caller is not that owner, or 500 if the database rejects the write.
    """
    at = now.isoformat()
    _save_row(
        supabase,
        owner.id,
        {"consent_version": body.version, "consent_at": at},
    )
    return Consent(version=body.version, at=at)
