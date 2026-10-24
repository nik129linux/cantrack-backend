"""Uvicorn entrypoint: load the environment, then expose the ASGI app."""

import os

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .settings import validate_settings

load_dotenv()

app = create_app()


def run() -> None:
    """Validate settings then serve the API with uvicorn. O(1).

    Reads ``PORT`` and ``TRUSTED_PROXY_IPS`` from the environment on every
    call so tests and deploys can change them without a rebuild.
    """
    validate_settings(os.environ)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "3000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("TRUSTED_PROXY_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    run()
