"""Demo accounts and walks for the professor's review.

Running this module seeds a full set of demo data in Pasto, Colombia: two
owners, three walkers, their dogs, one open walk with two offers, one assigned
upcoming walk and one finished walk with a GPS track, timeline, rating and a
stored report. The plan is deterministic; only the passwords are random and
they are written to a private file, never printed.

Dry run by default; pass ``--apply`` to write:

    cd apps/api-py && uv run python -m cantrack_api.seed_demo
    cd apps/api-py && uv run python -m cantrack_api.seed_demo --apply
    cd apps/api-py && uv run python -m cantrack_api.seed_demo --apply --reset
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from supabase import create_client

from .db import run_query
from .deps import get_now

DEMO_DOMAIN = "example.com"
DEFAULT_CREDENTIALS_FILE = "demo-credentials.local.txt"

#: Pagination for ``auth.admin.list_users``; 200 is the admin API maximum.
PAGE_SIZE = 200

#: Pasto, Nariño: the centre every demo location is measured from.
PASTO_LAT = 1.2136
PASTO_LNG = -77.2811

#: Bytes of urandom behind each password (``token_urlsafe`` yields ~4/3 this).
PASSWORD_BYTES = 24


def _demo_id(kind: str, key: str) -> str:
    """Derive the stable UUID for a plan row from its ``kind`` and ``key``.

    Namespaced UUIDv5 keeps ids identical between runs and between the owner,
    dog and walk references, without storing a random id in the plan.

    Big-O: O(1).

    Args:
        kind: The table or entity kind, e.g. ``"dog"``.
        key: The plan-local key, e.g. ``"nube"``.

    Returns:
        A deterministic UUID string.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cantrack-demo:{kind}:{key}"))


def _at(now: datetime, *, hours: float = 0.0, minutes: float = 0.0) -> datetime:
    """Return ``now`` shifted by the given offset. O(1)."""
    return now + timedelta(hours=hours, minutes=minutes)


def build_plan() -> dict[str, Any]:
    """Build the deterministic demo plan (same people, dogs and walks every run).

    Timestamps are stored as offsets from the ``now`` passed to
    :func:`apply_plan`, so the finished walk always sits in the past and the
    upcoming walk in the future without hard-coding a date here.

    Big-O: O(1) — the plan has a fixed number of rows.

    Returns:
        A dict with one list per table: ``people``, ``owner_profiles``,
        ``walker_profiles``, ``walker_positions``, ``dogs``, ``walk_requests``,
        ``walk_offers``, ``walk_timeline``, ``walk_gps_points`` and
        ``walk_ratings``.
    """
    owners = [
        {
            "email": "camila.rosero@example.com",
            "role": "owner",
            "display_name": "Camila Rosero",
        },
        {
            "email": "andres.burbano@example.com",
            "role": "owner",
            "display_name": "Andrés Burbano",
        },
    ]
    walkers = [
        {
            "email": "juan.erazo@example.com",
            "role": "walker",
            "display_name": "Juan Pablo Erazo",
        },
        {
            "email": "maria.lopez@example.com",
            "role": "walker",
            "display_name": "María Fernanda López",
        },
        {
            "email": "sebastian.cortes@example.com",
            "role": "walker",
            "display_name": "Sebastián Cortés",
        },
    ]

    plan: dict[str, Any] = {
        "people": owners + walkers,
        "owner_profiles": [
            {
                "email": owners[0]["email"],
                "display_name": "Camila Rosero",
                "phone": "+57 300 555 0101",
                "home_lat": 1.2145,
                "home_lng": -77.2790,
                "home_label": "Pasto, Nariño",
            },
            {
                "email": owners[1]["email"],
                "display_name": "Andrés Burbano",
                "phone": "+57 300 555 0102",
                "home_lat": 1.2080,
                "home_lng": -77.2860,
                "home_label": "Pasto, Nariño",
            },
        ],
        "walker_profiles": [
            {
                "email": walkers[0]["email"],
                "display_name": "Juan Pablo Erazo",
                "bio": "Paseos alegres y puntuales por el centro de Pasto.",
                "service_area": "Pasto, Nariño",
                "price_per_walk": 20000,
            },
            {
                "email": walkers[1]["email"],
                "display_name": "María Fernanda López",
                "bio": "Experiencia con perros mayores y paseos tranquilos.",
                "service_area": "Pasto, Nariño",
                "price_per_walk": 19000,
            },
            {
                "email": walkers[2]["email"],
                "display_name": "Sebastián Cortés",
                "bio": "Amante del deporte; paseos largos y activos.",
                "service_area": "Pasto, Nariño",
                "price_per_walk": 18000,
            },
        ],
        "walker_positions": [
            {"email": walkers[0]["email"], "lat": 1.2190, "lng": -77.2750},
            {"email": walkers[1]["email"], "lat": 1.2085, "lng": -77.2860},
            {"email": walkers[2]["email"], "lat": 1.2180, "lng": -77.2880},
        ],
        "dogs": [
            {
                "key": "nube",
                "owner_email": owners[0]["email"],
                "name": "Nube",
                "breed": "Criolla",
                "notes": "Muy juguetona; le encanta el parque.",
            },
            {
                "key": "coco",
                "owner_email": owners[0]["email"],
                "name": "Coco",
                "breed": "Beagle",
                "notes": "Tranquilo y sociable con otros perros.",
            },
            {
                "key": "luna",
                "owner_email": owners[1]["email"],
                "name": "Luna",
                "breed": "Labrador",
                "notes": "Mayor, camina despacio y agradece el agua.",
            },
            {
                "key": "toby",
                "owner_email": owners[1]["email"],
                "name": "Toby",
                "breed": "Poodle",
                "notes": "Curioso y obediente; tira poco de la correa.",
            },
        ],
        "walk_requests": [
            {
                "key": "open-park",
                "owner_email": owners[0]["email"],
                "walker_email": None,
                "dog_key": "nube",
                "status": "open",
                "requested_offset_hours": 6,
                "window_end_offset_hours": 7,
                "deadline_offset_hours": 4,
                "pickup_lat": 1.2145,
                "pickup_lng": -77.2790,
                "price_cop": 18000,
                "duration_hours": 1.0,
            },
            {
                "key": "assigned-morning",
                "owner_email": owners[1]["email"],
                "walker_email": walkers[0]["email"],
                "dog_key": "luna",
                "status": "assigned",
                "requested_offset_hours": 20,
                "window_end_offset_hours": 21.5,
                "deadline_offset_hours": 18,
                "pickup_lat": 1.2080,
                "pickup_lng": -77.2860,
                "price_cop": 22000,
                "duration_hours": 1.5,
            },
            {
                "key": "done-yesterday",
                "owner_email": owners[1]["email"],
                "walker_email": walkers[1]["email"],
                "dog_key": "toby",
                "status": "done",
                "requested_offset_hours": -26,
                "window_end_offset_hours": -25,
                "deadline_offset_hours": -27,
                "pickup_lat": 1.2080,
                "pickup_lng": -77.2860,
                "price_cop": 20000,
                "duration_hours": 1.0,
                "report": {
                    "status": "ok",
                    "text": (
                        "Toby disfrutó un paseo tranquilo por el barrio. "
                        "Caminó de buen ánimo, saludó a sus amigos de siempre "
                        "y volvió a casa contento y con sed."
                    ),
                    "distance_km": 3.2,
                    "duration_min": 62,
                },
            },
        ],
        "walk_offers": [
            {
                "walk_key": "open-park",
                "walker_email": walkers[0]["email"],
                "price_cop": 20000,
                "eta_min": 8.0,
                "distance_km": 0.9,
            },
            {
                "walk_key": "open-park",
                "walker_email": walkers[1]["email"],
                "price_cop": 19000,
                "eta_min": 12.0,
                "distance_km": 1.4,
            },
        ],
        "walk_timeline": [
            {
                "walk_key": "done-yesterday",
                "kind": "check_in",
                "offset_minutes": -1560,
                "payload": {"note": "Recogí a Toby en la puerta."},
            },
            {
                "walk_key": "done-yesterday",
                "kind": "check_out",
                "offset_minutes": -1498,
                "payload": {"note": "Devolví a Toby sano y salvo."},
            },
        ],
        "walk_gps_points": [
            {
                "walk_key": "done-yesterday",
                "offset_minutes": -1560,
                "lat": 1.2080,
                "lng": -77.2860,
            },
            {
                "walk_key": "done-yesterday",
                "offset_minutes": -1540,
                "lat": 1.2110,
                "lng": -77.2830,
            },
            {
                "walk_key": "done-yesterday",
                "offset_minutes": -1520,
                "lat": 1.2140,
                "lng": -77.2800,
            },
            {
                "walk_key": "done-yesterday",
                "offset_minutes": -1498,
                "lat": 1.2080,
                "lng": -77.2860,
            },
        ],
        "walk_ratings": [
            {
                "walk_key": "done-yesterday",
                "walker_email": walkers[1]["email"],
                "rating": 5,
            }
        ],
    }
    return plan


def _list_all_users(admin: Any) -> list[Any]:
    """Page through every auth user. Big-O: O(n) in the number of users."""
    users: list[Any] = []
    page = 1
    while True:
        batch = admin.list_users(page=page, per_page=PAGE_SIZE)
        users.extend(batch)
        if len(batch) < PAGE_SIZE:
            return users
        page += 1


def _new_password(used: set[str]) -> str:
    """Return a random password of at least 20 chars not already in ``used``.

    Big-O: O(1) expected — a collision is astronomically unlikely, and even
    then the loop runs once more.
    """
    while True:
        password = secrets.token_urlsafe(PASSWORD_BYTES)
        if len(password) >= 20 and password not in used:
            used.add(password)
            return password


def apply_plan(supabase: Any, plan: dict[str, Any], now: datetime) -> list[dict[str, str]]:
    """Create every demo account and row, returning the credentials once.

    The accounts are created with the admin API (confirmed email and the role
    mirrored into ``app_metadata`` and ``user_metadata``), then the profiles,
    positions, dogs and walks are inserted with only the columns the schema
    declares. Timestamps come from ``now`` and the plan's offsets.

    Big-O: O(p + r) where ``p`` is the number of people and ``r`` the number of
    rows in the plan, with one admin call per person and one insert per table.

    Args:
        supabase: The service-role Supabase client.
        plan: A plan from :func:`build_plan`.
        now: The reference time used for every offset in the plan.

    Returns:
        One ``{"email", "role", "password"}`` dict per person, in plan order.

    Raises:
        RuntimeError: If a demo account already exists; pass ``--reset`` to
            remove the previous demo data first.
    """
    admin = supabase.auth.admin
    existing = [
        user.email
        for user in _list_all_users(admin)
        if (user.email or "").lower().endswith("@" + DEMO_DOMAIN)
    ]
    if existing:
        raise RuntimeError(
            "Demo accounts already exist. Re-run with --reset to delete them first."
        )

    user_ids: dict[str, str] = {}
    credentials: list[dict[str, str]] = []
    used_passwords: set[str] = set()
    for person in plan["people"]:
        password = _new_password(used_passwords)
        result = admin.create_user(
            {
                "email": person["email"],
                "password": password,
                "email_confirm": True,
                "app_metadata": {"role": person["role"]},
                "user_metadata": {"role": person["role"]},
            }
        )
        user_ids[person["email"]] = result.user.id
        credentials.append(
            {
                "email": person["email"],
                "role": person["role"],
                "password": password,
            }
        )

    for profile in plan["owner_profiles"]:
        run_query(
            supabase.table("owner_profiles").insert(
                {
                    "owner_id": user_ids[profile["email"]],
                    "display_name": profile["display_name"],
                    "phone": profile["phone"],
                    "home_lat": profile["home_lat"],
                    "home_lng": profile["home_lng"],
                    "home_label": profile["home_label"],
                    "updated_at": now.isoformat(),
                }
            )
        )

    for profile in plan["walker_profiles"]:
        run_query(
            supabase.table("walker_profiles").insert(
                {
                    "walker_id": user_ids[profile["email"]],
                    "display_name": profile["display_name"],
                    "bio": profile["bio"],
                    "service_area": profile["service_area"],
                    "price_per_walk": profile["price_per_walk"],
                }
            )
        )

    for position in plan["walker_positions"]:
        run_query(
            supabase.table("walker_positions").insert(
                {
                    "walker_id": user_ids[position["email"]],
                    "lat": position["lat"],
                    "lng": position["lng"],
                    "updated_at": now.isoformat(),
                }
            )
        )

    dog_ids: dict[str, str] = {}
    for dog in plan["dogs"]:
        dog_id = _demo_id("dog", dog["key"])
        dog_ids[dog["key"]] = dog_id
        run_query(
            supabase.table("dogs").insert(
                {
                    "id": dog_id,
                    "owner_id": user_ids[dog["owner_email"]],
                    "name": dog["name"],
                    "breed": dog["breed"],
                    "notes": dog["notes"],
                }
            )
        )

    walk_ids: dict[str, str] = {}
    for walk in plan["walk_requests"]:
        walk_id = _demo_id("walk", walk["key"])
        walk_ids[walk["key"]] = walk_id
        requested = _at(now, hours=walk["requested_offset_hours"])
        row: dict[str, Any] = {
            "id": walk_id,
            "owner_id": user_ids[walk["owner_email"]],
            "dog_id": dog_ids[walk["dog_key"]],
            "status": walk["status"],
            "requested_time": requested.isoformat(),
            "window_end": _at(now, hours=walk["window_end_offset_hours"]).isoformat(),
            "deadline_at": _at(now, hours=walk["deadline_offset_hours"]).isoformat(),
            "pickup_lat": walk["pickup_lat"],
            "pickup_lng": walk["pickup_lng"],
            "price_cop": walk["price_cop"],
            "duration_hours": walk["duration_hours"],
        }
        if walk["walker_email"] is not None:
            row["walker_id"] = user_ids[walk["walker_email"]]
        report = walk.get("report")
        if report is not None:
            row.update(
                {
                    "report_status": report["status"],
                    "report_text": report["text"],
                    "report_distance_km": report["distance_km"],
                    "report_duration_min": report["duration_min"],
                }
            )
        run_query(supabase.table("walk_requests").insert(row))

    for offer in plan["walk_offers"]:
        run_query(
            supabase.table("walk_offers").insert(
                {
                    "walk_request_id": walk_ids[offer["walk_key"]],
                    "walker_id": user_ids[offer["walker_email"]],
                    "price_cop": offer["price_cop"],
                    "eta_min": offer["eta_min"],
                    "distance_km": offer["distance_km"],
                }
            )
        )

    for event in plan["walk_timeline"]:
        run_query(
            supabase.table("walk_timeline").insert(
                {
                    "walk_request_id": walk_ids[event["walk_key"]],
                    "kind": event["kind"],
                    "at": _at(now, minutes=event["offset_minutes"]).isoformat(),
                    "payload": event["payload"],
                }
            )
        )

    for point in plan["walk_gps_points"]:
        run_query(
            supabase.table("walk_gps_points").insert(
                {
                    "walk_request_id": walk_ids[point["walk_key"]],
                    "at_epoch": _at(
                        now, minutes=point["offset_minutes"]
                    ).timestamp(),
                    "lat": point["lat"],
                    "lng": point["lng"],
                }
            )
        )

    for rating in plan["walk_ratings"]:
        run_query(
            supabase.table("walk_ratings").insert(
                {
                    "walk_request_id": walk_ids[rating["walk_key"]],
                    "walker_id": user_ids[rating["walker_email"]],
                    "rating": rating["rating"],
                }
            )
        )

    return credentials


def reset_demo(supabase: Any) -> int:
    """Delete every auth user whose email ends with ``@example.com``.

    Big-O: O(n) in the number of auth users (one list pass, one delete per
    demo account).

    Args:
        supabase: The service-role Supabase client.

    Returns:
        The number of accounts deleted.
    """
    admin = supabase.auth.admin
    demo_users = [
        user
        for user in _list_all_users(admin)
        if (user.email or "").lower().endswith("@" + DEMO_DOMAIN)
    ]
    for user in demo_users:
        admin.delete_user(user.id)
    return len(demo_users)


def write_credentials(path: Any, credentials: list[dict[str, str]]) -> None:
    """Write the demo credentials to ``path`` with owner-only permissions.

    The file is created (or truncated) with mode ``0600`` and the mode is
    re-applied afterwards, so an existing looser file cannot keep its
    permissions.

    Big-O: O(c) in the number of credential entries.

    Args:
        path: Filesystem path to write (``str`` or path-like).
        credentials: The entries returned by :func:`apply_plan`.
    """
    target = os.fspath(path)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("# CanTrack demo credentials. Do not commit this file.\n")
        handle.write("# email\tpassword\trole\n")
        for entry in credentials:
            handle.write(
                f"{entry['email']}\t{entry['password']}\t{entry['role']}\n"
            )
    os.chmod(target, 0o600)


def main(argv: list[str] | None = None) -> int:
    """Run the seeder CLI: dry run by default, write only with ``--apply``.

    Passwords are written to the credentials file and never printed; stdout
    only ever shows emails and roles.

    Big-O: O(p + r) — the same cost as :func:`apply_plan`.

    Args:
        argv: Command-line arguments without the program name; defaults to
            ``sys.argv[1:]``.

    Returns:
        ``0`` on success.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    reset = "--reset" in args
    out_path: Any = DEFAULT_CREDENTIALS_FILE
    if "--out" in args:
        index = args.index("--out")
        if index + 1 >= len(args):
            raise SystemExit("--out requires a file path")
        out_path = args[index + 1]

    load_dotenv()
    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )

    if reset:
        removed = reset_demo(supabase)
        print(f"Deleted {removed} demo account(s).")

    plan = build_plan()
    if not apply:
        print("Dry run: no accounts created. Pass --apply to write.")
        for person in plan["people"]:
            print(f"{person['email']}\t{person['role']}")
        return 0

    credentials = apply_plan(supabase, plan, get_now())
    write_credentials(out_path, credentials)
    for entry in credentials:
        print(f"{entry['email']}\t{entry['role']}")
    print(f"Wrote {len(credentials)} credential(s) to {out_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
