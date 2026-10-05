"""S3: checkout — the walker's photos to the owner, behind a human gate.

Flow (docs/superpowers/specs/2026-09-30 section S3, briefs/s3-tests.md): a
walker finishes an ACCEPTED request and uploads 1..3 photos
(``POST /requests/{id}/checkout``, multipart field ``image``). The photos go
to the PRIVATE Storage bucket ``checkout-photos`` under a server-built path
``{walker_id}/{checkout_id}/{index}.{ext}`` — the client's filename never
reaches the path — with the stored ``content-type`` derived from the MAGIC
BYTES, never from the client's header. The vision model is shown the first
photo and its answer becomes the draft's ``ai_note``; the walker edits their
own ``note`` (initially a copy of the AI text, ``null`` when the AI gave
nothing) and sends it. Only a SENT checkout is visible to the owner, only as
signed URLs (3600 s), and only through queries scoped by the caller's id.

Provenance split (PR #8 review): ``ai_note`` is what the model said and is
changed ONLY by draft creation and by an answered ``ai-note`` re-run;
``note`` is the walker's text and the only field ``PATCH`` writes. A re-run
replaces ``note`` only when the walker had not edited it; an unavailable
re-run (or draft creation) changes no text at all — an infrastructure
failure never destroys data and never burns quota. The quota counts ANSWERED
vision calls per walker per UTC calendar month (60), attributed by the time
of each call (``ai_usage`` log), so the counter resets on the 1st no matter
when the checkout was created.

Retention: photos of sent checkouts are purged at 30 days — lazily, when
either side lists checkouts or reads the timeline (there is no cron on the
free tier). Removals happen per path and a path whose removal failed keeps
its entry: the row never claims objects that are gone, and never drops the
entry of an object that may still exist.

The per-dog timeline mixes the dog's accepted walks and sent checkouts in
time order through the hand-written DoublyLinkedList (``to_array`` forward,
``to_array_reverse`` backward); ties at the same instant break walk-first.
Drafts never appear anywhere an owner can read.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import Client
from supabase_auth.types import User

from cantrack_ai.vision import OllamaVision
from ..db import first_row, run_query
from ..deps import get_current_user, get_now, get_supabase, get_vision
from ..schemas import UpdateCheckoutNoteBody
from ..structures import DoublyLinkedList

router = APIRouter()

CHECKOUTS_TABLE = "checkouts"
AI_USAGE_TABLE = "ai_usage"
REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
PROFILES_TABLE = "walker_profiles"

#: The one private bucket checkout photos live in. It is never served through
#: ``get_public_url`` — signed URLs only.
BUCKET = "checkout-photos"

#: Signed URLs expire in exactly one hour (pinned by the tests).
SIGNED_URL_SECONDS = 3600

#: 1..3 photos per checkout; each at most 5 MiB = 5 * 1024 * 1024 bytes.
MIN_PHOTOS = 1
MAX_PHOTOS = 3
MAX_PHOTO_BYTES = 5 * 1024 * 1024

#: The walker's note and the AI observation are both capped at 200 chars.
NOTE_MAX_CHARS = 200

#: Answered vision calls per walker per UTC calendar month.
AI_LIMIT = 60

#: Sent checkouts keep their photos for 30 days = 30 * 86,400 s.
RETENTION = timedelta(days=30)

#: Owner/walker list paging.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

NOT_FOUND_MESSAGE = "Checkout not found."
REQUEST_NOT_FOUND_MESSAGE = "Request not found."
DOG_NOT_FOUND_MESSAGE = "Dog not found."
WALKER_ROLE_MESSAGE = "Only walkers can create a checkout."
ROLE_MESSAGE = "Unsupported role."
CHECKOUT_STATE_MESSAGE = "Only an accepted request can have a checkout."
CHECKOUT_EXISTS_MESSAGE = "This request already has a checkout."
PHOTOS_COUNT_MESSAGE = f"Between {MIN_PHOTOS} and {MAX_PHOTOS} photos are required."
PHOTO_SIZE_MESSAGE = "Each photo must be 5 MiB or less."
UNREADABLE_MESSAGE = "Unreadable image."
UPLOAD_FAILED_MESSAGE = "Photo upload failed."
IMMUTABLE_MESSAGE = "A sent checkout is immutable."
NOTE_LEN_MESSAGE = f"The note must be {NOTE_MAX_CHARS} characters or less."
QUOTA_MESSAGE = "Monthly AI limit reached."
QUOTA_ROLE_MESSAGE = "Only walkers have an AI quota."
LIMIT_MESSAGE = f"limit must be between 1 and {MAX_LIST_LIMIT}."
ORDER_MESSAGE = "order must be 'asc' or 'desc'."

#: Magic bytes decide the type — a lying Content-Type header changes nothing
#: (the stored object's content-type comes from here too).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


def _role(user: User) -> str | None:
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def _image_kind(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, content_type)`` from the photo's magic bytes.

    JPEG is ``FF D8 FF``, PNG the 8-byte signature ``89 50 4E 47 0D 0A 1A
    0A``, WEBP ``RIFF`` + 4 size bytes + ``WEBP``. Anything else is not an
    image CanTrack stores, whatever the multipart header claimed.
    """
    if data[:3] == _JPEG_MAGIC:
        return "jpg", "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "png", "image/png"
    if data[:4] == _RIFF_MAGIC and data[8:12] == _WEBP_MAGIC:
        return "webp", "image/webp"
    return None


def _parse_utc(value: str) -> datetime:
    """Parse a stored timestamptz (always UTC-normalized) into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of the UTC calendar month ``now`` falls in."""
    utc_now = now.astimezone(timezone.utc)
    start = datetime(utc_now.year, utc_now.month, 1, tzinfo=timezone.utc)
    end = (
        datetime(utc_now.year + 1, 1, 1, tzinfo=timezone.utc)
        if utc_now.month == 12
        else datetime(utc_now.year, utc_now.month + 1, 1, tzinfo=timezone.utc)
    )
    return start, end


def _ai_used_this_month(supabase: Client, walker_id: str, now: datetime) -> int:
    """Count the walker's ANSWERED vision calls in ``now``'s UTC month.

    Attribution is by the time of each call (``ai_usage.used_at``), not by
    the checkout's ``created_at`` — a draft from last month re-run today
    spends today's quota, and the counter resets on the 1st.
    """
    start, end = _month_bounds(now)
    rows = run_query(
        supabase.table(AI_USAGE_TABLE).select("used_at").eq("walker_id", walker_id)
    )
    return sum(1 for row in rows if start <= _parse_utc(row["used_at"]) < end)


def _record_ai_usage(supabase: Client, walker_id: str, now: datetime) -> None:
    """Log one answered vision call against the walker's monthly quota."""
    run_query(
        supabase.table(AI_USAGE_TABLE).insert(
            {"walker_id": walker_id, "used_at": now.isoformat()}
        )
    )


def _bucket(supabase: Client) -> Any:
    """The private checkout-photos bucket proxy (never ``get_public_url``)."""
    return supabase.storage.from_(BUCKET)


def _sign(bucket: Any, path: str) -> dict[str, Any]:
    """One photo as ``{url, expiresIn}`` — a signed URL, expiring in 3600 s."""
    try:
        signed = bucket.create_signed_url(path, SIGNED_URL_SECONDS)
    except (StorageApiError, APIError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
        ) from exc
    return {"url": signed["signedURL"], "expiresIn": SIGNED_URL_SECONDS}


def _photos(bucket: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every stored path of a row as a signed URL (purged rows have none)."""
    return [_sign(bucket, path) for path in row["photo_paths"]]


def _rollback(bucket: Any, paths: list[str]) -> None:
    """Best-effort removal of objects an interrupted upload already stored.

    A flaky network must not leave orphan photos of a checkout that was
    never created. If the cleanup itself fails there is nothing better to
    do inside the request: the objects are private, unreferenced and the
    original error is what the caller must see.
    """
    if not paths:
        return
    try:
        bucket.remove(list(paths))
    except Exception:  # noqa: BLE001 - cleanup must never mask the real error
        pass


def _dog_name(supabase: Client, dog_id: str) -> str | None:
    """The name of one dog (the walker may see names of dogs they walk)."""
    rows = run_query(supabase.table(DOGS_TABLE).select("id,name").eq("id", dog_id))
    return rows[0]["name"] if rows else None


def _dog_names(supabase: Client, dog_ids: list[str]) -> dict[str, str]:
    """Names of many dogs at once, keyed by id."""
    unique = sorted(set(dog_ids))
    if not unique:
        return {}
    rows = run_query(supabase.table(DOGS_TABLE).select("id,name").in_("id", unique))
    return {row["id"]: row["name"] for row in rows}


def _walker_names(supabase: Client, walker_ids: list[str]) -> dict[str, str | None]:
    """Display names of many walkers at once, keyed by walker id."""
    unique = sorted(set(walker_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(PROFILES_TABLE).select("walker_id,display_name").in_(
            "walker_id", unique
        )
    )
    return {row["walker_id"]: row["display_name"] for row in rows}


def _walker_view(row: dict[str, Any], dog_name: str | None, photos: list[dict[str, Any]]) -> dict[str, Any]:
    """The checkout as its walker sees it — the only view with the AI fields."""
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "dogId": row["dog_id"],
        "dogName": dog_name,
        "walkerId": row["walker_id"],
        "status": row["status"],
        "photos": photos,
        "aiNote": row["ai_note"],
        "dogVisible": row["dog_visible"],
        "note": row["note"],
        "aiStatus": row["ai_status"],
        "sentAt": row["sent_at"],
        "createdAt": row["created_at"],
    }


def _owner_view(
    row: dict[str, Any],
    dog_name: str | None,
    walker_name: str | None,
    photos: list[dict[str, Any]],
) -> dict[str, Any]:
    """The checkout as its owner sees it: the walker's note, never the AI
    fields (``aiNote``/``aiStatus``/``dogVisible`` are not part of this view).
    """
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "dogId": row["dog_id"],
        "dogName": dog_name,
        "walkerId": row["walker_id"],
        "walkerName": walker_name,
        "note": row["note"],
        "photos": photos,
        "sentAt": row["sent_at"],
    }


def _purge_expired(
    supabase: Client,
    bucket: Any,
    rows: list[dict[str, Any]],
    now: datetime,
    scope_column: str,
    scope_value: str,
) -> None:
    """Drop the photos of sent checkouts older than 30 days (lazy retention).

    Runs where the owner or the walker lists checkouts (there is no cron on
    the free tier). Objects are removed one path at a time; a path whose
    removal raised keeps its entry (the object presumably still exists) and
    the rest are dropped — after the purge a row claims exactly the objects
    that still exist. The row and the note always stay. ``rows`` is updated
    in place so the caller's response reflects the purge.
    """
    cutoff = now.astimezone(timezone.utc) - RETENTION
    for row in rows:
        if row["status"] != "sent" or not row["sent_at"]:
            continue
        if _parse_utc(row["sent_at"]) > cutoff:
            continue
        kept: list[str] = []
        for path in row["photo_paths"]:
            try:
                bucket.remove([path])
            except (StorageApiError, APIError):
                kept.append(path)
        if kept != row["photo_paths"]:
            run_query(
                supabase.table(CHECKOUTS_TABLE)
                .update({"photo_paths": kept})
                .eq("id", row["id"])
                .eq(scope_column, scope_value)
            )
            row["photo_paths"] = kept


def _canonical_checkout_id(value: str) -> str:
    """Canonicalize a checkout id from the path.

    The parameter is a plain string on purpose: an id that is not a UUID
    cannot match any row, and "cannot exist" must answer the same 404 as
    "exists but is not yours" — never a 400 that reveals the id shape rules.
    """
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_MESSAGE
        ) from None


def _load_for_walker(
    supabase: Client, checkout_id: uuid.UUID, user: User
) -> dict[str, Any]:
    """Fetch the caller-walker's checkout row, or 404 (foreign rows included)."""
    return first_row(
        run_query(
            supabase.table(CHECKOUTS_TABLE)
            .select("*")
            .eq("id", checkout_id)
            .eq("walker_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )


def _load_sent_for_owner(
    supabase: Client, filters: list[tuple[str, Any]]
) -> dict[str, Any]:
    """Fetch one SENT checkout of the caller-owner by id or request, or 404.

    A draft is invisible to the owner through every door: the ``status``
    filter is part of the lookup, so an unsent checkout is a 404 that
    reveals nothing.
    """
    query = supabase.table(CHECKOUTS_TABLE).select("*").eq("status", "sent")
    for column, value in filters:
        query = query.eq(column, value)
    return first_row(run_query(query), NOT_FOUND_MESSAGE)


@router.post("/requests/{request_id}/checkout", status_code=status.HTTP_201_CREATED)
def create_checkout(
    request_id: uuid.UUID,
    image: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    vision: OllamaVision = Depends(get_vision),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Create the draft checkout of an accepted request (its walker only).

    Validation order is the attacker order: role, request ownership, request
    status, one-checkout-per-request, photo COUNT — all before a single byte
    is stored or the model is loaded. Then each photo is read (one byte past
    the 5 MiB cap → 413), typed by its magic bytes (garbage → 400, whatever
    the header claims) and uploaded to the private bucket under the
    server-built path, with the content-type derived from those same bytes.
    A failure on any photo rolls the already-uploaded objects back.

    Creating a draft NEVER fails because of the AI: at the quota the model is
    skipped (``aiStatus: "quota"``) and an unanswered call degrades to
    ``"unavailable"`` — neither counts toward the quota, and both leave the
    draft fully usable.

    Args:
        request_id: The accepted request being finished.
        image: 1..3 photos, each a multipart ``image`` part.
        user: The authenticated walker.
        supabase: The Supabase client (tables and storage).
        vision: The vision model, injected as a dependency.
        now: The current time (dependency, frozen in tests).

    Returns:
        201 with the draft in the walker view (signed photo URLs, ``aiNote``,
        ``dogVisible``, ``note``, ``aiStatus``).

    Raises:
        HTTPException: 400 (role, request state, count, size is 413, magic),
            404 (foreign request), 409 (checkout exists), 413 (photo too
            large), 500 (database/storage).
    """
    if _role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WALKER_ROLE_MESSAGE
        )

    request_row = first_row(
        run_query(
            supabase.table(REQUESTS_TABLE)
            .select("*")
            .eq("id", request_id)
            .eq("walker_id", user.id)
        ),
        REQUEST_NOT_FOUND_MESSAGE,
    )
    if request_row["status"] != "accepted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=CHECKOUT_STATE_MESSAGE
        )

    existing = run_query(
        supabase.table(CHECKOUTS_TABLE)
        .select("id")
        .eq("request_id", request_id)
        .eq("walker_id", user.id)
    )
    if existing:
        # Chosen over replacing the draft: a flaky retry (or a second tab)
        # must never destroy a checkout, least of all a sent one.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=CHECKOUT_EXISTS_MESSAGE
        )

    if not MIN_PHOTOS <= len(image) <= MAX_PHOTOS:
        # Before ANY storage or AI work: no object, no row, no model call.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PHOTOS_COUNT_MESSAGE
        )

    checkout_id = str(uuid.uuid4())
    bucket = _bucket(supabase)
    paths: list[str] = []
    payloads: list[bytes] = []
    try:
        for index, upload in enumerate(image, start=1):
            # One byte over the cap tells "fits" from "does not" without
            # buffering more than the limit.
            data = upload.file.read(MAX_PHOTO_BYTES + 1)
            if len(data) > MAX_PHOTO_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=PHOTO_SIZE_MESSAGE,
                )
            kind = _image_kind(data)
            if kind is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=UNREADABLE_MESSAGE
                )
            extension, content_type = kind
            # The path is built by the SERVER; the client's filename (which
            # may be ../../etc/passwd) never reaches it.
            path = f"{user.id}/{checkout_id}/{index}.{extension}"
            bucket.upload(
                path, data, {"content-type": content_type, "upsert": "false"}
            )
            paths.append(path)
            payloads.append(data)
    except HTTPException:
        _rollback(bucket, paths)
        raise
    except (StorageApiError, APIError) as exc:
        _rollback(bucket, paths)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
        ) from exc
    except Exception as exc:
        _rollback(bucket, paths)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=UPLOAD_FAILED_MESSAGE,
        ) from exc

    # The AI step: the model is shown the FIRST photo (one call per draft).
    # An unavailable model or an exhausted quota degrades the draft, never
    # blocks it, and only an ANSWERED call counts toward the quota.
    ai_note: str | None = None
    dog_visible: bool | None = None
    answered = False
    if _ai_used_this_month(supabase, user.id, now) >= AI_LIMIT:
        ai_status = "quota"
    else:
        try:
            analysis = vision.check(payloads[0])
        except Exception:  # noqa: BLE001 - the AI must never block the flow
            analysis = None
        if analysis is None or analysis.dog_visible is None:
            ai_status = "unavailable"
        else:
            ai_status = "ok"
            dog_visible = analysis.dog_visible
            if analysis.dog_visible and analysis.note:
                ai_note = analysis.note[:NOTE_MAX_CHARS]
            answered = True

    try:
        rows = run_query(
            supabase.table(CHECKOUTS_TABLE).insert(
                {
                    "id": checkout_id,
                    "request_id": str(request_id),
                    "dog_id": request_row["dog_id"],
                    "owner_id": request_row["owner_id"],
                    "walker_id": user.id,
                    "photo_paths": paths,
                    "ai_note": ai_note,
                    # The walker's field starts as a copy of the AI text
                    # (null when the AI gave nothing); PATCH writes only this.
                    "note": ai_note,
                    "dog_visible": dog_visible,
                    "ai_status": ai_status,
                    "status": "draft",
                    "sent_at": None,
                }
            )
        )
    except HTTPException:
        # The draft never materialized: do not orphan its photos.
        _rollback(bucket, paths)
        raise
    created = first_row(rows, NOT_FOUND_MESSAGE)
    if answered:
        _record_ai_usage(supabase, user.id, now)

    return _walker_view(
        created, _dog_name(supabase, created["dog_id"]), _photos(bucket, created)
    )


@router.get("/requests/{request_id}/checkout")
def get_request_checkout(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Resume the checkout of one request, role-gated.

    The walker gets their draft or sent checkout; the owner gets it only
    once SENT (a draft is a 404 that reveals nothing).

    Args:
        request_id: The request whose checkout is read.
        user: The authenticated caller.
        supabase: The Supabase client.

    Returns:
        The walker or owner view of the checkout.

    Raises:
        HTTPException: 400 (unknown role), 404 (foreign/invisible), 500.
    """
    role = _role(user)
    bucket = _bucket(supabase)

    if role == "walker":
        row = first_row(
            run_query(
                supabase.table(CHECKOUTS_TABLE)
                .select("*")
                .eq("request_id", request_id)
                .eq("walker_id", user.id)
            ),
            NOT_FOUND_MESSAGE,
        )
        return _walker_view(
            row, _dog_name(supabase, row["dog_id"]), _photos(bucket, row)
        )

    if role == "owner":
        row = _load_sent_for_owner(
            supabase, [("request_id", request_id), ("owner_id", user.id)]
        )
        names = _walker_names(supabase, [row["walker_id"]])
        return _owner_view(
            row,
            _dog_name(supabase, row["dog_id"]),
            names.get(row["walker_id"]),
            _photos(bucket, row),
        )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)


@router.get("/checkouts")
def list_checkouts(
    limit: int = DEFAULT_LIST_LIMIT,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> list[dict[str, Any]]:
    """List the caller's checkouts, newest first, purging expired photos.

    Owner: SENT only (drafts do not exist for them), ``sent_at`` descending
    with ``created_at`` as the tiebreak. Walker: their own rows of any
    status, ``created_at`` descending. ``limit`` defaults to 50 and must be
    1..100.

    Args:
        limit: Page size, 1..100.
        user: The authenticated caller.
        supabase: The Supabase client.
        now: The current time (retention cutoff).

    Returns:
        The role-appropriate views.

    Raises:
        HTTPException: 400 (bad limit or unknown role), 500 (database).
    """
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=LIMIT_MESSAGE
        )

    role = _role(user)
    bucket = _bucket(supabase)

    if role == "walker":
        rows = run_query(
            supabase.table(CHECKOUTS_TABLE).select("*").eq("walker_id", user.id)
        )
        _purge_expired(supabase, bucket, rows, now, "walker_id", user.id)
        rows.sort(key=lambda row: _parse_utc(row["created_at"]), reverse=True)
        rows = rows[:limit]
        names = _dog_names(supabase, [row["dog_id"] for row in rows])
        return [
            _walker_view(row, names.get(row["dog_id"]), _photos(bucket, row))
            for row in rows
        ]

    if role == "owner":
        rows = run_query(
            supabase.table(CHECKOUTS_TABLE)
            .select("*")
            .eq("owner_id", user.id)
            .eq("status", "sent")
        )
        _purge_expired(supabase, bucket, rows, now, "owner_id", user.id)
        rows.sort(
            key=lambda row: (_parse_utc(row["sent_at"]), _parse_utc(row["created_at"])),
            reverse=True,
        )
        rows = rows[:limit]
        dog_names = _dog_names(supabase, [row["dog_id"] for row in rows])
        walker_names = _walker_names(supabase, [row["walker_id"] for row in rows])
        return [
            _owner_view(
                row,
                dog_names.get(row["dog_id"]),
                walker_names.get(row["walker_id"]),
                _photos(bucket, row),
            )
            for row in rows
        ]

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)


@router.get("/checkouts/{checkout_id}")
def get_checkout(
    checkout_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Read one checkout by id, role-gated: its walker (any status), or its
    owner once SENT — a draft, like a foreign row, is a 404.

    Args:
        checkout_id: The checkout to read.
        user: The authenticated caller.
        supabase: The Supabase client.

    Returns:
        The walker or owner view.

    Raises:
        HTTPException: 400 (unknown role), 404 (foreign/invisible), 500.
    """
    checkout_id = _canonical_checkout_id(checkout_id)
    role = _role(user)
    bucket = _bucket(supabase)

    if role == "walker":
        row = _load_for_walker(supabase, checkout_id, user)
        return _walker_view(
            row, _dog_name(supabase, row["dog_id"]), _photos(bucket, row)
        )

    if role == "owner":
        row = _load_sent_for_owner(
            supabase, [("id", checkout_id), ("owner_id", user.id)]
        )
        names = _walker_names(supabase, [row["walker_id"]])
        return _owner_view(
            row,
            _dog_name(supabase, row["dog_id"]),
            names.get(row["walker_id"]),
            _photos(bucket, row),
        )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)


@router.patch("/checkouts/{checkout_id}")
def update_checkout_note(
    checkout_id: str,
    body: UpdateCheckoutNoteBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Edit the walker's note on their DRAFT (nothing else is writable).

    ``note`` is the walker's own text — the AI provenance (``ai_note``,
    ``dog_visible``) is never touched by a human edit. ``null`` clears the
    note for a photos-only checkout. Over 200 characters is a 400 and
    changes nothing; a sent checkout is immutable.

    Args:
        checkout_id: The draft to edit.
        body: ``{"note": str | null}``.
        user: The authenticated walker.
        supabase: The Supabase client.

    Returns:
        The updated walker view.

    Raises:
        HTTPException: 400 (length, immutability), 404 (foreign/owner), 500.
    """
    checkout_id = _canonical_checkout_id(checkout_id)
    row = _load_for_walker(supabase, checkout_id, user)
    if row["status"] != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=IMMUTABLE_MESSAGE
        )
    if body.note is not None and len(body.note) > NOTE_MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NOTE_LEN_MESSAGE
        )

    updated = first_row(
        run_query(
            supabase.table(CHECKOUTS_TABLE)
            .update({"note": body.note})
            .eq("id", checkout_id)
            .eq("walker_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )
    return _walker_view(
        updated,
        _dog_name(supabase, updated["dog_id"]),
        _photos(_bucket(supabase), updated),
    )


@router.post("/checkouts/{checkout_id}/send")
def send_checkout(
    checkout_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Send the draft to the owner (``draft -> sent``, stamped from the clock).

    This is the human gate: before it, the owner sees nothing; after it, the
    checkout is immutable — a second send is a 400, never a second
    notification. The owner receives the walker's ``note`` (the AI text when
    the walker never edited, ``null`` for a photos-only checkout).

    Args:
        checkout_id: The draft to send.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time, stamped as ``sentAt``.

    Returns:
        ``{id, requestId, status: "sent", sentAt}``.

    Raises:
        HTTPException: 400 (already sent), 404 (foreign/owner), 500.
    """
    checkout_id = _canonical_checkout_id(checkout_id)
    row = _load_for_walker(supabase, checkout_id, user)
    if row["status"] != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=IMMUTABLE_MESSAGE
        )

    sent_at = now.isoformat()
    run_query(
        supabase.table(CHECKOUTS_TABLE)
        .update({"status": "sent", "sent_at": sent_at})
        .eq("id", checkout_id)
        .eq("walker_id", user.id)
    )
    return {
        "id": row["id"],
        "requestId": row["request_id"],
        "status": "sent",
        "sentAt": sent_at,
    }


@router.post("/checkouts/{checkout_id}/ai-note")
def rerun_ai_note(
    checkout_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    vision: OllamaVision = Depends(get_vision),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Ask the vision model again about a DRAFT's photos (its walker only).

    The first photo is read back from the private bucket (``download``) and
    shown to the model; the bytes never leave the server except to the AI.
    At the quota the answer is 429 — the re-run is the one place the limit
    refuses outright, because unlike draft creation it has nothing to
    degrade into.

    An ANSWERED re-run replaces ``ai_note`` and ``dog_visible``, and replaces
    ``note`` only when the walker had not edited it (``note`` still equals
    the previous ``ai_note``, or is null); a human edit always survives. An
    UNAVAILABLE model changes nothing except ``ai_status`` — an
    infrastructure failure destroys no data — and does not count toward the
    quota.

    Args:
        checkout_id: The draft to re-run.
        user: The authenticated walker.
        supabase: The Supabase client.
        vision: The vision model, injected as a dependency.
        now: The current time (quota attribution).

    Returns:
        The updated walker view.

    Raises:
        HTTPException: 400 (immutability), 404 (foreign/owner), 429 (quota),
            500 (database/storage).
    """
    checkout_id = _canonical_checkout_id(checkout_id)
    row = _load_for_walker(supabase, checkout_id, user)
    if row["status"] != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=IMMUTABLE_MESSAGE
        )

    if _ai_used_this_month(supabase, user.id, now) >= AI_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=QUOTA_MESSAGE
        )

    bucket = _bucket(supabase)
    paths = row["photo_paths"]
    analysis = None
    if paths:
        try:
            payload = bucket.download(paths[0])
        except (StorageApiError, APIError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
            ) from exc
        try:
            analysis = vision.check(payload)
        except Exception:  # noqa: BLE001 - the AI must never block the flow
            analysis = None

    if analysis is not None and analysis.dog_visible is not None:
        new_ai_note = (
            analysis.note[:NOTE_MAX_CHARS]
            if analysis.dog_visible and analysis.note
            else None
        )
        walker_edited = row["note"] is not None and row["note"] != row["ai_note"]
        patch: dict[str, Any] = {
            "ai_note": new_ai_note,
            "dog_visible": analysis.dog_visible,
            "ai_status": "ok",
        }
        if not walker_edited:
            patch["note"] = new_ai_note
        answered = True
    else:
        # Nothing is destroyed: the last answered observation and the human
        # note stay exactly as they were; only the status reflects the miss.
        patch = {"ai_status": "unavailable"}
        answered = False

    updated = first_row(
        run_query(
            supabase.table(CHECKOUTS_TABLE)
            .update(patch)
            .eq("id", checkout_id)
            .eq("walker_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )
    if answered:
        _record_ai_usage(supabase, user.id, now)

    return _walker_view(
        updated, _dog_name(supabase, updated["dog_id"]), _photos(bucket, updated)
    )


@router.get("/ai-quota")
def get_ai_quota(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """The caller-walker's AI quota for the current UTC month.

    Args:
        user: The authenticated walker (owners have no quota: 400).
        supabase: The Supabase client.
        now: The current time (which month, and when it resets).

    Returns:
        ``{used, limit, resetsAt}`` — ``resetsAt`` is the first instant of
        the next UTC month.

    Raises:
        HTTPException: 400 (non-walker), 500 (database).
    """
    if _role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=QUOTA_ROLE_MESSAGE
        )
    _start, end = _month_bounds(now)
    return {
        "used": _ai_used_this_month(supabase, user.id, now),
        "limit": AI_LIMIT,
        "resetsAt": end.isoformat(),
    }


@router.get("/dogs/{dog_id}/timeline")
def get_dog_timeline(
    dog_id: uuid.UUID,
    order: str = "desc",
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> list[dict[str, Any]]:
    """The owner's per-dog timeline: accepted walks and sent checkouts mixed
    in time order, built with the hand-written DoublyLinkedList.

    Walks contribute their ``requestedTime``, checkouts their ``sentAt``; a
    tie at the same instant breaks walk-first. ``order=asc`` reads the list
    forward (``to_array``), ``order=desc`` — the default — backward
    (``to_array_reverse``). Drafts never appear: the owner must not learn a
    checkout exists before the walker sends it. Listing purges photos past
    the 30-day retention, same as ``GET /checkouts``.

    Args:
        dog_id: The dog whose timeline is read.
        order: ``asc`` or ``desc`` (anything else is a 400).
        user: The authenticated owner (a walker or a foreign owner gets 404).
        supabase: The Supabase client.
        now: The current time (retention cutoff).

    Returns:
        The feed: ``{type: "walk", requestId, walkerId, walkerName,
        requestedTime, status}`` and ``{type: "checkout", checkoutId,
        requestId, walkerId, walkerName, note, sentAt}`` items.

    Raises:
        HTTPException: 400 (bad order), 404 (foreign dog), 500 (database).
    """
    if order not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ORDER_MESSAGE
        )

    first_row(
        run_query(
            supabase.table(DOGS_TABLE)
            .select("id")
            .eq("id", dog_id)
            .eq("owner_id", user.id)
        ),
        DOG_NOT_FOUND_MESSAGE,
    )

    walks = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("dog_id", dog_id)
        .eq("owner_id", user.id)
        .eq("status", "accepted")
    )
    checkouts = run_query(
        supabase.table(CHECKOUTS_TABLE)
        .select("*")
        .eq("dog_id", dog_id)
        .eq("owner_id", user.id)
        .eq("status", "sent")
    )
    _purge_expired(supabase, _bucket(supabase), checkouts, now, "owner_id", user.id)

    walker_names = _walker_names(
        supabase,
        [row["walker_id"] for row in walks] + [row["walker_id"] for row in checkouts],
    )

    # (time, rank, item): rank 0 for walks breaks same-instant ties walk-first.
    events: list[tuple[datetime, int, dict[str, Any]]] = []
    for row in walks:
        events.append(
            (
                _parse_utc(row["requested_time"]),
                0,
                {
                    "type": "walk",
                    "requestId": row["id"],
                    "walkerId": row["walker_id"],
                    "walkerName": walker_names.get(row["walker_id"]),
                    "requestedTime": row["requested_time"],
                    "status": row["status"],
                },
            )
        )
    for row in checkouts:
        events.append(
            (
                _parse_utc(row["sent_at"]),
                1,
                {
                    "type": "checkout",
                    "checkoutId": row["id"],
                    "requestId": row["request_id"],
                    "walkerId": row["walker_id"],
                    "walkerName": walker_names.get(row["walker_id"]),
                    "note": row["note"],
                    "sentAt": row["sent_at"],
                },
            )
        )

    timeline: DoublyLinkedList[dict[str, Any]] = DoublyLinkedList()
    for _when, _rank, item in sorted(events, key=lambda event: (event[0], event[1])):
        timeline.append(item)
    return timeline.to_array() if order == "asc" else timeline.to_array_reverse()
