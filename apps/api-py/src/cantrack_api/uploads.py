"""The photo uploads CanTrack's AI endpoints accept, validated in one place.

Both endpoints take their photos as ``image`` parts of a multipart request — a
check-in (FR-10) always with exactly one, an enrolment (FR-05) with between one
and five of them, averaged into the single vector stored for the dog. Either way
the bytes have to be *something the model can read*, so the cheap rejections — an
unsupported content type, a body larger than any reference photo has any business
being — happen here, before any inference is asked for. Expensive work stays
behind these checks, and a failure inside the model itself is translated into the
same answer from both routers instead of leaking a stack trace out of a request.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, UploadFile, status

from .ai.embeddings import ClipEmbedder, ImageError

#: Largest photo accepted, in bytes. A few reference shots of one dog never come
#: close to this; anything bigger is a mistake or an attempt to exhaust the box.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: Most reference photos one enrolment may carry. More would cost inference for
#: no extra certainty: a handful of good shots already pin down what a dog looks
#: like, and a long tail of near-duplicates only drifts the mean towards blur.
MAX_REFERENCE_PHOTOS = 5

#: The encodings the CLIP preprocessor can actually decode.
SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

UNSUPPORTED_TYPE_MESSAGE = "Unsupported image type."
TOO_LARGE_MESSAGE = "Image is too large."
UNREADABLE_MESSAGE = "Unreadable image."
MODEL_UNAVAILABLE_MESSAGE = "Image model unavailable."
PHOTO_COUNT_MESSAGE = f"Between 1 and {MAX_REFERENCE_PHOTOS} photos are required."


def read_image(upload: UploadFile) -> bytes:
    """Validate one uploaded photo and return its bytes.

    The declared content type is the only thing trusted about the part, and it is
    checked first: an HTML error page or a PDF sent as an "image" never reaches
    the decoder. The body is read one byte past the limit, so an oversized upload
    is detected without ever holding the whole thing in memory.

    Args:
        upload: The ``image`` part of a multipart request.

    Returns:
        The raw bytes of the photo.

    Raises:
        HTTPException: 400 if the declared type is not a supported image type, or
            413 if the photo is larger than ``MAX_IMAGE_BYTES``.
    """
    if upload.content_type not in SUPPORTED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNSUPPORTED_TYPE_MESSAGE
        )

    # One byte over the limit is enough to tell "fits" from "does not", and it
    # keeps a hostile upload from ever being fully buffered.
    data = upload.file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=TOO_LARGE_MESSAGE,
        )

    return data


def embed_image(embedder: ClipEmbedder, image_bytes: bytes) -> list[float]:
    """Embed photo bytes, mapping every model failure onto an HTTP answer.

    This is the single place the embedding model is called from, so both photo
    endpoints report the same thing for the same failure. The two failures are
    kept apart on purpose: an image the model refuses to decode is the caller's
    mistake (400, retrying will not help), while a model that is missing,
    unreachable or broken is ours (503, worth retrying).

    Args:
        embedder: The CLIP embedder, injected as a dependency.
        image_bytes: The raw bytes of an already-validated photo.

    Returns:
        The photo's embedding, as a list of floats.

    Raises:
        HTTPException: 400 if the bytes are not a readable image, or 503 if the
            model itself failed.
    """
    try:
        return embedder.embed(image_bytes)
    except ImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=UNREADABLE_MESSAGE
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MODEL_UNAVAILABLE_MESSAGE,
        ) from exc


def mean_embedding(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """Average several photo embeddings into the one vector stored for a dog.

    The mean is the point of taking several reference photos: each shot of a dog
    lands somewhere slightly different in the vector space — side on, running,
    in bad light — and averaging those is a far better stand-in for "this is what
    the dog looks like" than any single shot is. It is the arithmetic mean,
    computed one coordinate at a time, so the result has the same width as the
    inputs.

    Args:
        embeddings: The photos' embeddings, in upload order.

    Returns:
        The element-wise mean, as a list of floats.

    Raises:
        HTTPException: 400 if no embedding was given, or 503 if the model returned
            vectors of different widths, which means the mean is undefined and
            the model cannot be trusted with this request.
    """
    if not embeddings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PHOTO_COUNT_MESSAGE
        )

    width = len(embeddings[0])
    if any(len(vector) != width for vector in embeddings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MODEL_UNAVAILABLE_MESSAGE,
        )

    count = len(embeddings)
    return [sum(vector[at] for vector in embeddings) / count for at in range(width)]
