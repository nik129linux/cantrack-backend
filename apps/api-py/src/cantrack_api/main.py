"""Uvicorn entrypoint: load the environment, then expose the ASGI app."""

from dotenv import load_dotenv

from .app import create_app

load_dotenv()

app = create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))