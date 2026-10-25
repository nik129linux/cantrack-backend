"""Explorar: the optional typed-request parser backed by the text model (PR2)."""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client
from supabase_auth.types import User

from ..ai_text import OllamaText
from ..app import limiter
from ..db import run_query
from ..deps import current_role, get_current_user, get_now, get_supabase, get_text_model

router = APIRouter()

DOGS_TABLE = "dogs"
USAGE_TABLE = "owner_ai_usage"
USAGE_FEATURE = "request_parse"

OWNERS_ONLY_MESSAGE = "Only owners can use this."
UNAVAILABLE_MESSAGE = "The assistant is not available. Fill in the form."
QUOTA_MESSAGE = "Daily assistant limit reached."

#: Answered parses one owner may spend per UTC day; the refused call gives its slot back.
DAILY_LIMIT = 10
#: The only durations the draft form offers.
DURATIONS = frozenset({30, 45, 60, 90, 120})
PRICE_MIN_COP = 8000
PRICE_MAX_COP = 1_000_000
MAX_WINDOW_AHEAD_DAYS = 30

#: America/Bogota is UTC-5 with no daylight saving, so a fixed offset is exact.
BOGOTA = timezone(timedelta(hours=-5))


class ParseBody(BaseModel):
    """Body of ``POST /bidding/requests/parse``: the owner's typed request."""

    text: str = Field(min_length=1, max_length=280)


class ParseDraft(BaseModel):
    """The validated draft form: every unproven field stays null."""

    dogId: str | None = None
    windowStart: str | None = None
    durationMinutes: int | None = None
    priceCop: int | None = None


class ParseResponse(BaseModel):
    """The parse answer: draft, null fields in form order, and model status."""

    draft: ParseDraft
    missing: list[str]
    aiStatus: str


def _parse_utc(value: str) -> datetime:
    """Parse a stored timestamp into an aware UTC datetime. O(1)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _local_stamp(now: datetime) -> str:
    """The current time in America/Bogota as ``YYYY-MM-DDTHH:MM``. O(1)."""
    return now.astimezone(BOGOTA).strftime("%Y-%m-%dT%H:%M")


def _build_system(dog_names: list[str], now: datetime) -> str:
    """The system prompt: exact JSON keys, the caller's dogs only, local now. O(d)."""
    dogs = ", ".join(dog_names) if dog_names else "none"
    return (
        "Extract a walk request as a JSON object with exactly these keys: "
        "dogName, windowStart (ISO 8601 with offset, America/Bogota -05:00), "
        "durationMinutes, priceCop. "
        f"The owner's dogs are: {dogs}. "
        "Choose dogName from that list only. "
        f"Current local time (America/Bogota): {_local_stamp(now)}."
    )


def _resolve_dog(dogs: list[dict[str, Any]], answer: dict[str, Any]) -> str | None:
    """Match the model's ``dogName`` to the caller's dogs, case-insensitively. O(d)."""
    name = answer.get("dogName")
    if not isinstance(name, str):
        return None
    wanted = name.strip().lower()
    for dog in dogs:
        if isinstance(dog.get("name"), str) and dog["name"].lower() == wanted:
            return dog["id"]
    return None


def _resolve_window(value: Any, now: datetime) -> str | None:
    """Keep an ISO-8601 window with an offset in the future within 30 days. O(1)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    if parsed <= now:
        return None
    if parsed > now + timedelta(days=MAX_WINDOW_AHEAD_DAYS):
        return None
    return value


def _resolve_duration(value: Any) -> int | None:
    """Keep a duration the draft form offers, else null. O(1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in DURATIONS else None


def _resolve_price(value: Any) -> int | None:
    """Keep a real int price inside the honest range, else null. O(1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if PRICE_MIN_COP <= value <= PRICE_MAX_COP else None


def _give_back_slot(supabase: Client, reservation_id: str) -> None:
    """Delete one quota reservation row, giving the slot back. O(1)."""
    run_query(supabase.table(USAGE_TABLE).delete().eq("id", reservation_id))


def _unavailable_draft() -> dict[str, Any]:
    """The empty draft answered when the model has no answer. O(1)."""
    return {
        "draft": {"dogId": None, "windowStart": None, "durationMinutes": None, "priceCop": None},
        "missing": ["dogId", "windowStart", "durationMinutes", "priceCop"],
        "aiStatus": "unavailable",
    }


@router.post("/bidding/requests/parse", response_model=ParseResponse)
@limiter.limit("5/minute")
def parse_request(
    request: Request,
    body: ParseBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
    text_model: OllamaText = Depends(get_text_model),
) -> dict[str, Any]:
    """Turn a typed walk request into a validated draft form (owners only). O(d + u).

    The model only ever proposes a DRAFT; nothing is created. Its answer is
    re-validated field by field against the owner's own dogs, the clock and the
    honest ranges, so a prompt injection can at most leave fields empty. The
    daily quota is reserved before the model is called and given back when the
    model does not answer.

    Args:
        request: The incoming request (read by the rate limiter).
        body: The owner's typed text, 1 to 280 chars.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).
        text_model: The text model double in tests.

    Returns:
        ``{draft, missing, aiStatus}`` — ``missing`` names the null draft
        fields in form order; ``aiStatus`` is ``"ok"`` or ``"unavailable"``.

    Raises:
        HTTPException: 403 (not an owner), 503 (model disabled), 429 (daily
            quota spent), 500 (database).
    """
    if current_role(user) != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNERS_ONLY_MESSAGE)
    if not text_model.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_MESSAGE
        )

    dogs = run_query(supabase.table(DOGS_TABLE).select("*").eq("owner_id", user.id))

    reservation = run_query(
        supabase.table(USAGE_TABLE).insert(
            {"owner_id": user.id, "feature": USAGE_FEATURE, "used_at": now.isoformat()}
        )
    )[0]

    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    used_rows = run_query(
        supabase.table(USAGE_TABLE)
        .select("used_at")
        .eq("owner_id", user.id)
        .eq("feature", USAGE_FEATURE)
    )
    used_today = 0
    for row in used_rows:
        try:
            seen = _parse_utc(row["used_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if day_start <= seen < day_end:
            used_today += 1
    if used_today > DAILY_LIMIT:
        _give_back_slot(supabase, reservation["id"])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=QUOTA_MESSAGE
        )

    system = _build_system([dog["name"] for dog in dogs if isinstance(dog.get("name"), str)], now)
    try:
        answer = text_model.complete_json(system, body.text)
    except Exception:
        _give_back_slot(supabase, reservation["id"])
        return _unavailable_draft()
    if answer is None:
        _give_back_slot(supabase, reservation["id"])
        return _unavailable_draft()

    draft = {
        "dogId": _resolve_dog(dogs, answer),
        "windowStart": _resolve_window(answer.get("windowStart"), now),
        "durationMinutes": _resolve_duration(answer.get("durationMinutes")),
        "priceCop": _resolve_price(answer.get("priceCop")),
    }
    missing = [field for field in ("dogId", "windowStart", "durationMinutes", "priceCop") if draft[field] is None]
    return {"draft": draft, "missing": missing, "aiStatus": "ok"}
