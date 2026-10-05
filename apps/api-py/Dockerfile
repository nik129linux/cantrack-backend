FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    CLIP_MODEL_PATH=/models/clip_vision_q.onnx

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Bake the CLIP model (~89 MB) into the image so the first photo request is not a cold download.
RUN mkdir -p /models && uv run python -c "from cantrack_ai.embeddings import ClipEmbedder; ClipEmbedder().ensure_model()"

# Render injects PORT; cantrack_api.main reads it.
CMD ["uv", "run", "--no-sync", "python", "-m", "cantrack_api.main"]
