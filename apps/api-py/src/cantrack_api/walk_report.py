"""The one-time walk report written at check-out (FR-21).

After a successful check-out the walk's duration and distance are computed
exactly and stored on the walk request, and the text model is asked ONCE for
a short ``{text}`` note built only from the dog name, the stats and the
timeline kinds. The report never blocks the check-out.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from .db import run_query
from .geo import haversine_km

#: The stored text is cut here so one row never grows without bound.
logger = logging.getLogger(__name__)

MAX_REPORT_CHARS = 600


def build_and_store_walk_report(
    supabase: Any, request_id: str, now: datetime, text_model: Any
) -> None:
    """Compute the stats, ask the text model once and store the report. O(g + t).

    Duration is whole minutes from the walk's ``check_in`` timeline row to
    ``now``; distance is the haversine sum over the walk's GPS points ordered
    by ``at_epoch``, rounded to 2 decimals. The model is asked once with only
    the dog name, the stats and the timeline kinds; the answer's ``text`` is
    cut to 600 chars. Stored columns on ``walk_requests`` are
    ``report_status`` (``ok``/``unavailable``/``disabled``), ``report_text``,
    ``report_distance_km`` and ``report_duration_min``.

    Big-O: O(g + t), one pass over the walk's GPS points plus one over its
    timeline rows; the model is called at most once.

    Args:
        supabase: The Supabase client.
        request_id: The walk request just checked out.
        now: The current time (the check-out moment).
        text_model: The text model client (asked once at most).
    """
    try:
        duration_min = _duration_min(supabase, request_id, now)
    except Exception:
        duration_min = 0
    try:
        distance_km = _distance_km(supabase, request_id)
    except Exception:
        distance_km = 0.0
    try:
        dog_name = _dog_name(supabase, request_id)
    except Exception:
        dog_name = "the dog"
    try:
        kinds = _timeline_kinds(supabase, request_id)
    except Exception:
        kinds = []
    status = "unavailable"
    text: str | None = None
    try:
        if os.environ.get("AI_WALK_REPORT") == "off":
            status = "disabled"
        elif not getattr(text_model, "enabled", True):
            status = "disabled"
        else:
            system = (
                "You write short dog-walk reports for the dog's owner, in Spanish "
                "(Colombia), 2 or 3 sentences, warm and factual. Use only the facts "
                "given; never invent incidents or health claims. Answer with one "
                'JSON object {"text": ...}.'
            )
            prompt = (
                f"Dog {dog_name} walked {distance_km:.2f} km "
                f"in {duration_min} min. Events: {', '.join(kinds) if kinds else 'walk'}. "
                "Write a short friendly report as {text}."
            )
            answer = text_model.complete_json(system, prompt)
            if isinstance(answer, dict) and isinstance(answer.get("text"), str):
                raw = answer["text"]
                if raw.strip():
                    status = "ok"
                    text = raw[:MAX_REPORT_CHARS]
    except Exception:
        logger.exception("walk report: model call failed for %s", request_id)
        status = "unavailable"
        text = None
    try:
        run_query(
            supabase.table("walk_requests")
            .update(
                {
                    "report_status": status,
                    "report_text": text,
                    "report_distance_km": distance_km,
                    "report_duration_min": duration_min,
                }
            )
            .eq("id", request_id)
        )
    except Exception:
        logger.exception("walk report: storing the report failed for %s", request_id)


def _duration_min(supabase: Any, request_id: str, now: datetime) -> int:
    """Whole minutes from the walk's check-in row to now. O(t)."""
    rows = run_query(
        supabase.table("walk_timeline").select("*").eq("walk_request_id", request_id)
    )
    check_ins = [row for row in rows if row.get("kind") == "check_in"]
    if not check_ins:
        return 0
    check_ins.sort(key=lambda row: str(row.get("at", "")))
    start = datetime.fromisoformat(check_ins[0]["at"])
    delta = (now - start).total_seconds()
    if delta <= 0:
        return 0
    return int(delta // 60)


def _distance_km(supabase: Any, request_id: str) -> float:
    """Haversine sum over the walk's GPS points ordered by at_epoch. O(g)."""
    rows = run_query(
        supabase.table("walk_gps_points")
        .select("*")
        .eq("walk_request_id", request_id)
        .order("at_epoch")
    )
    total = 0.0
    for first, second in zip(rows, rows[1:]):
        total += haversine_km(
            first["lat"], first["lng"], second["lat"], second["lng"]
        )
    return round(total, 2)


def _dog_name(supabase: Any, request_id: str) -> str:
    """The walk's dog name, without any owner or contact data. O(1)."""
    walk_rows = run_query(
        supabase.table("walk_requests").select("*").eq("id", request_id)
    )
    if not walk_rows:
        return "the dog"
    dog_rows = run_query(
        supabase.table("dogs").select("*").eq("id", walk_rows[0].get("dog_id"))
    )
    if not dog_rows or not dog_rows[0].get("name"):
        return "the dog"
    return str(dog_rows[0]["name"])


def _timeline_kinds(supabase: Any, request_id: str) -> list[str]:
    """The walk's timeline kinds in order, for the prompt facts. O(t)."""
    rows = run_query(
        supabase.table("walk_timeline").select("*").eq("walk_request_id", request_id)
    )
    rows.sort(key=lambda row: str(row.get("at", "")))
    kinds: list[str] = []
    for row in rows:
        kind = str(row.get("kind", ""))
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds
