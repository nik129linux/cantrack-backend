"""Production boot checks: refuse to serve when required secrets are missing.

The API reads its configuration from the environment at startup. In
development an unset variable only fails on the first request that needs it,
but in production that would mean a deploy that looks healthy until traffic
arrives. Validating up front turns a misconfigured deploy into a crash loop
with a clear message instead.
"""

from typing import Mapping


def validate_settings(environ: Mapping[str, str]) -> None:
    """Check the environment mapping and raise when production cannot boot.

    O(n) in the number of configured web origins, which is a handful at most.

    Args:
        environ: The environment mapping to check, usually ``os.environ``.

    Returns:
        None when the configuration is usable.

    Raises:
        RuntimeError: If ``ENV`` is ``production`` and a required secret is
            missing, or ``WEB_ORIGIN`` allows a wildcard, a non-HTTPS origin,
            or a localhost address.
    """
    if environ.get("ENV") != "production":
        return
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "WEB_ORIGIN"):
        if not environ.get(key):
            raise RuntimeError(f"{key} must be set in production.")
    raw = environ.get("WEB_ORIGIN", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        raise RuntimeError("WEB_ORIGIN must be set in production.")
    for origin in origins:
        if "*" in origin or "localhost" in origin or "127.0.0.1" in origin:
            raise RuntimeError(f"WEB_ORIGIN is not safe for production: {origin}")
        if not origin.startswith("https://"):
            raise RuntimeError(f"WEB_ORIGIN must use https in production: {origin}")
