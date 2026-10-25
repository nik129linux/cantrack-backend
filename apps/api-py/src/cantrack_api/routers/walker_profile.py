"""W5 Perfil: the walker's profile, avatar and data-protection consent.

Every endpoint hangs from ``walker_access.walker_from_path``, so the walker id
always comes from the path guard and never from the request body. The wire
format is camelCase; ``walker_profiles`` is snake_case with one row per walker,
shared with the legacy ``/walker-profile`` catalog.
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
from ..walker_access import walker_from_path
from .profile import (
    MAX_AVATAR_BYTES,
    MAX_AVATAR_PIXELS,
    MAX_AVATAR_SIDE,
    PHONE_PATTERN,
    TOO_BIG_MESSAGE,
    UNREADABLE_MESSAGE,
    _signed_avatar,
)

router = APIRouter()

PROFILES_TABLE = "walker_profiles"
WALK_REQUESTS_TABLE = "walk_requests"
RATINGS_TABLE = "walk_ratings"
AVATAR_BUCKET = "avatars"
SAVED_MESSAGE = "Walker profile not found."
NO_PROFILE_MESSAGE = "Save your profile before adding a photo."


class SaveWalkerProfileBody(BaseModel):
    """Body of ``PUT /walkers/{walkerId}/profile``.

    Only the fields a walker may set are declared, so ``walkerId`` or any
    other column sent by the client is dropped by the schema. Display name and
    price are required because ``walker_profiles`` needs them on insert.
    """

    displayName: str = Field(min_length=1, max_length=80)
    pricePerWalk: int = Field(ge=0, le=1_000_000)
    bio: str | None = Field(default=None, max_length=500)
    serviceArea: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)


class ConsentBody(BaseModel):
    """Body of ``POST /walkers/{walkerId}/consent``."""

    version: str = Field(min_length=1, max_length=20)


class Rating(BaseModel):
    """The walker's average rating and how many walks were rated."""

    average: float | None
    count: int


class Stats(BaseModel):
    """Per-walker activity counters shown on the profile tab."""

    walksDone: int
    rating: Rating


class Consent(BaseModel):
    """The version of the terms the walker accepted and when."""

    version: str
    at: str


class WalkerProfile(BaseModel):
    """The full profile payload returned by ``GET`` and ``PUT``."""

    walkerId: str
    email: str | None
    displayName: str | None
    bio: str | None
    serviceArea: str | None
    pricePerWalk: int | None
    phone: str | None
    avatarUrl: str | None
    consent: Consent | None
    stats: Stats


class AvatarResponse(BaseModel):
    """The freshly signed URL of the walker's avatar."""

    avatarUrl: str


def _profile_row(supabase: Client, walker_id: str) -> dict[str, Any]:
    """Return the walker's profile row, or an empty dict when there is none.

    O(1): one indexed lookup by ``walker_id``.

    Args:
        supabase: The Supabase client used to read the row.
        walker_id: The walker whose row is wanted.

    Returns:
        The stored row, or ``{}`` before the walker saved anything.
    """
    rows = run_query(
        supabase.table(PROFILES_TABLE).select("*").eq("walker_id", walker_id)
    )
    return rows[0] if rows else {}


def _save_row(
    supabase: Client, walker_id: str, values: dict[str, Any]
) -> dict[str, Any]:
    """Write ``values`` to the walker's profile row, inserting it if missing.

    O(1): one lookup plus one update or insert, all keyed by ``walker_id``.

    Args:
        supabase: The Supabase client used to read and write the row.
        walker_id: The walker the row belongs to.
        values: The snake_case columns to set.

    Returns:
        The stored row after the write.

    Raises:
        HTTPException: 404 if the write reports no row, or 500 if the database
            rejects a query.
    """
    existing = run_query(
        supabase.table(PROFILES_TABLE).select("walker_id").eq("walker_id", walker_id)
    )
    if existing:
        rows = run_query(
            supabase.table(PROFILES_TABLE).update(values).eq("walker_id", walker_id)
        )
    else:
        rows = run_query(
            supabase.table(PROFILES_TABLE).insert({"walker_id": walker_id, **values})
        )
    return first_row(rows, SAVED_MESSAGE)


def _stats(supabase: Client, walker_id: str) -> Stats:
    """Count the walker's finished walks and average their ratings.

    O(n) in the walker's walk and rating rows: each table is read with one
    query and both counters are computed in Python (the client offers no
    aggregate).

    Args:
        supabase: The Supabase client used to read the rows.
        walker_id: The walker whose counters are wanted.

    Returns:
        The ``walksDone`` count and the ``{average, count}`` rating.
    """
    walks = run_query(
        supabase.table(WALK_REQUESTS_TABLE)
        .select("id,status")
        .eq("walker_id", walker_id)
    )
    ratings = run_query(
        supabase.table(RATINGS_TABLE).select("rating").eq("walker_id", walker_id)
    )
    count = len(ratings)
    average = (
        round(sum(row["rating"] for row in ratings) / count, 2) if count else None
    )
    return Stats(
        walksDone=sum(1 for walk in walks if walk.get("status") == "done"),
        rating=Rating(average=average, count=count),
    )


def _view(
    walker: User, row: dict[str, Any], stats: Stats, avatar_url: str | None
) -> WalkerProfile:
    """Translate a stored row to the camelCase profile payload.

    O(1): fixed-size field mapping.

    Args:
        walker: The authenticated walker.
        row: The stored profile row, possibly empty.
        stats: The walker's activity counters.
        avatar_url: The signed avatar URL, or None.

    Returns:
        The profile in the API shape.
    """
    version = row.get("consent_version")
    at = row.get("consent_at")
    return WalkerProfile(
        walkerId=walker.id,
        email=walker.email,
        displayName=row.get("display_name"),
        bio=row.get("bio"),
        serviceArea=row.get("service_area"),
        pricePerWalk=row.get("price_per_walk"),
        phone=row.get("phone"),
        avatarUrl=avatar_url,
        consent=Consent(version=version, at=at) if version is not None and at else None,
        stats=stats,
    )


@router.get("/walkers/{walkerId}/profile", response_model=WalkerProfile)
@limiter.limit("30/minute")
def get_walker_profile(
    request: Request,
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
) -> WalkerProfile:
    """Return the caller's profile, avatar URL, consent and counters.

    O(n) in the walker's walk and rating rows (the counters), with the rest
    O(1).

    Args:
        request: The request, read by the rate limiter.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client used to read rows and sign the avatar.

    Returns:
        The profile; a new walker gets nulls and zeroed counters.

    Raises:
        HTTPException: 403 when the caller is not that walker, or 500 if the
            database or storage fails.
    """
    row = _profile_row(supabase, walker.id)
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    return _view(walker, row, _stats(supabase, walker.id), avatar_url)


@router.put("/walkers/{walkerId}/profile", response_model=WalkerProfile)
@limiter.limit("30/minute")
def save_walker_profile(
    request: Request,
    body: SaveWalkerProfileBody,
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> WalkerProfile:
    """Create or replace the caller's editable profile fields.

    O(n) in the walker's walk and rating rows (the counters), plus O(1) for the
    profile write. The avatar and consent columns are left untouched.

    Args:
        request: The request, read by the rate limiter.
        body: The editable fields; display name and price are required.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client used to read and write the row.
        now: The current time, injected so tests can freeze it.

    Returns:
        The saved profile in the API shape.

    Raises:
        HTTPException: 400 when a field fails validation, 403 when the caller is
            not that walker, or 500 if the database rejects the write.
    """
    values = {
        "display_name": body.displayName,
        "bio": body.bio,
        "service_area": body.serviceArea,
        "price_per_walk": body.pricePerWalk,
        "phone": body.phone,
        "updated_at": now.isoformat(),
    }
    row = _save_row(supabase, walker.id, values)
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    return _view(walker, row, _stats(supabase, walker.id), avatar_url)


def _webp(data: bytes) -> bytes:
    """Decode an uploaded image, fit it in 512 px and encode it as WEBP.

    O(n) in the decoded image size: the picture is decoded once, rescaled and
    re-encoded.

    Args:
        data: The raw bytes of the uploaded file.

    Returns:
        The WEBP bytes to store.

    Raises:
        HTTPException: 400 when the bytes are not a readable image or declare a
            canvas past the decompression-bomb cap.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_AVATAR_PIXELS:
                raise ValueError("image dimensions too large")
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            image.thumbnail((MAX_AVATAR_SIDE, MAX_AVATAR_SIDE))
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=85)
            return buffer.getvalue()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNREADABLE_MESSAGE
        ) from exc


@router.post("/walkers/{walkerId}/avatar", response_model=AvatarResponse)
@limiter.limit("5/minute")
def upload_avatar(
    request: Request,
    photo: UploadFile = File(...),
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
) -> AvatarResponse:
    """Replace the caller's avatar with a 512 px WEBP and return a signed URL.

    O(n) in the decoded image size: the picture is decoded once, rescaled and
    re-encoded before the old object is removed and the new one stored.

    Args:
        request: The request, read by the rate limiter.
        photo: The ``photo`` multipart part.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client used to store and sign the object.

    Returns:
        ``{"avatarUrl": ...}`` pointing at the private object for one hour.

    Raises:
        HTTPException: 409 when the walker has no profile row yet, 413 when the
            photo is over 2 MB, 400 when it cannot be decoded, 403 when the
            caller is not that walker, or 500 if storage fails.
    """
    if not _profile_row(supabase, walker.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=NO_PROFILE_MESSAGE
        )

    data = photo.file.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=TOO_BIG_MESSAGE
        )
    encoded = _webp(data)

    path = f"{walker.id}/avatar.webp"
    bucket = supabase.storage.from_(AVATAR_BUCKET)
    # The bucket rejects a duplicate path, so the previous object goes first.
    try:
        bucket.remove([path])
    except (StorageApiError, APIError):
        pass
    bucket.upload(path, encoded, {"content-type": "image/webp"})

    row = _save_row(supabase, walker.id, {"avatar_path": path})
    avatar_url = _signed_avatar(supabase, row.get("avatar_path"))
    if avatar_url is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The avatar could not be read.",
        )
    return AvatarResponse(avatarUrl=avatar_url)


@router.post("/walkers/{walkerId}/consent", response_model=Consent)
@limiter.limit("10/minute")
def record_consent(
    request: Request,
    body: ConsentBody,
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> Consent:
    """Record which version of the terms the caller accepted, and when.

    O(1): one profile lookup plus one update.

    Args:
        request: The request, read by the rate limiter.
        body: The accepted version, 1 to 20 characters.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client used to write the row.
        now: The current time, injected so tests can freeze it.

    Returns:
        The stored ``{"version", "at"}``.

    Raises:
        HTTPException: 409 when the walker has no profile row yet, 400 when the
            version is out of bounds, 403 when the caller is not that walker, or
            500 if the database rejects the write.
    """
    if not _profile_row(supabase, walker.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=NO_PROFILE_MESSAGE
        )
    at = now.isoformat()
    _save_row(
        supabase,
        walker.id,
        {"consent_version": body.version, "consent_at": at},
    )
    return Consent(version=body.version, at=at)
