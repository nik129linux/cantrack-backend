"""FR-04, FR-05: an owner can create, read, update and delete their own dogs, and
enrol one from its reference photos.

Every query is scoped to the authenticated user's id, so a dog belonging to
somebody else is indistinguishable from one that does not exist: both are a
404.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from supabase import Client
from supabase_auth.types import User

from ..ai.embeddings import ClipEmbedder
from ..db import first_row, run_query
from ..deps import get_current_user, get_embedder, get_supabase
from ..schemas import CreateDogBody, DogProfileBody, UpdateDogBody
from ..uploads import (
    MAX_REFERENCE_PHOTOS,
    PHOTO_COUNT_MESSAGE,
    embed_image,
    mean_embedding,
    read_image,
)

router = APIRouter(prefix="/dogs")

DOGS_TABLE = "dogs"
NOT_FOUND_MESSAGE = "Dog not found."
DELETED_MESSAGE = "Dog deleted."

#: The column the server-side embedding lives in; it is never sent back.
EMBEDDING_COLUMN = "embedding"


@router.get("")
def list_dogs(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List the dogs owned by the caller.

    Args:
        user: The authenticated owner.
        supabase: The Supabase client used to read the rows.

    Returns:
        Every dog whose ``owner_id`` is the caller's, possibly empty.

    Raises:
        HTTPException: 500 if the database cannot be read.
    """
    rows = run_query(supabase.table(DOGS_TABLE).select("*").eq("owner_id", user.id))
    return rows


@router.post("", status_code=status.HTTP_201_CREATED)
def create_dog(
    body: CreateDogBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Register a new dog for the caller.

    The owner is always taken from the bearer token, never from the body, and
    unknown fields in the body are dropped by the schema.

    Args:
        body: The dog's name and, optionally, its breed and notes.
        user: The authenticated owner.
        supabase: The Supabase client used to insert the row.

    Returns:
        The stored dog, including the id the database generated for it.

    Raises:
        HTTPException: 500 if the insert fails.
    """
    row = {**body.model_dump(exclude_unset=True), "owner_id": user.id}
    rows = run_query(supabase.table(DOGS_TABLE).insert(row))
    return first_row(rows, NOT_FOUND_MESSAGE)


@router.post("/{dog_id}/photos")
def enroll_dog_photo(
    dog_id: uuid.UUID,
    image: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    embedder: ClipEmbedder = Depends(get_embedder),
) -> dict[str, Any]:
    """Enrol one of the caller's dogs from one to five reference photos (FR-05).

    The ``image`` multipart field may be repeated, and the vector stored for the
    dog is the mean of every photo's embedding: one shot catches the dog in one
    light and one angle only, while several of them average out into a better
    stand-in for what the dog actually looks like. A single photo is the same
    operation, since the mean of one vector is that vector.

    The photos are embedded on the server with the same CLIP model the browser
    uses, so a dog enrolled either side ends up in the same vector space and
    stays comparable at check-in time. The order of the checks is deliberate: the
    photo count is refused before a single byte is read, every photo is validated
    before any of them is embedded, and the owner's dog is looked up *before* the
    model is loaded. A request for somebody else's dog, or one carrying a photo
    the model cannot read, must not be able to make the server run inference, and
    a single bad photo rejects the whole enrolment rather than storing a mean that
    quietly drops it.

    Args:
        dog_id: The id of the dog to enrol.
        image: The reference photos, as the repeated ``image`` multipart field.
        user: The authenticated owner.
        supabase: The Supabase client used to read and write the row.
        embedder: The CLIP embedder, injected as a dependency.

    Returns:
        The dog as stored after the enrolment, without its embedding.

    Raises:
        HTTPException: 400 if more than ``MAX_REFERENCE_PHOTOS`` were sent or one
            of them is not a readable image, 404 if the dog does not exist or is
            not the caller's, or 413/503 for an oversized upload or a model
            failure.
    """
    if not image or len(image) > MAX_REFERENCE_PHOTOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PHOTO_COUNT_MESSAGE
        )

    photos = [read_image(upload) for upload in image]

    found = run_query(
        supabase.table(DOGS_TABLE)
        .select("*")
        .eq("id", str(dog_id))
        .eq("owner_id", user.id)
    )
    first_row(found, NOT_FOUND_MESSAGE)

    embedding = mean_embedding([embed_image(embedder, photo) for photo in photos])
    rows = run_query(
        supabase.table(DOGS_TABLE)
        .update({EMBEDDING_COLUMN: embedding})
        .eq("id", str(dog_id))
        .eq("owner_id", user.id)
    )
    updated = first_row(rows, NOT_FOUND_MESSAGE)

    # The embedding is 512 floats the client never needs back; answering without
    # it keeps the response a readable dog instead of a wall of numbers.
    return {key: value for key, value in updated.items() if key != EMBEDDING_COLUMN}


@router.patch("/{dog_id}")
def update_dog(
    dog_id: uuid.UUID,
    body: UpdateDogBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Apply a partial update to one of the caller's dogs.

    An empty body carries no editable field — either because the client sent
    none or because it only sent columns the schema does not own — so nothing
    is written and the dog is simply read back.

    Args:
        dog_id: The id of the dog to update.
        body: The columns to change; unset columns are left alone.
        user: The authenticated owner.
        supabase: The Supabase client used to write the row.

    Returns:
        The dog as stored after the update.

    Raises:
        HTTPException: 404 if the dog does not exist or is not the caller's, or
            500 if the database cannot be written.
    """
    patch = body.model_dump(exclude_unset=True)
    table = supabase.table(DOGS_TABLE)

    if patch:
        rows = run_query(table.update(patch).eq("id", str(dog_id)).eq("owner_id", user.id))
    else:
        rows = run_query(
            table.select("*").eq("id", str(dog_id)).eq("owner_id", user.id)
        )

    return first_row(rows, NOT_FOUND_MESSAGE)


@router.delete("/{dog_id}")
def delete_dog(
    dog_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, str]:
    """Delete one of the caller's dogs.

    Args:
        dog_id: The id of the dog to delete.
        user: The authenticated owner.
        supabase: The Supabase client used to delete the row.

    Returns:
        A short confirmation message.

    Raises:
        HTTPException: 404 if the dog does not exist or is not the caller's, or
            500 if the database cannot be written.
    """
    rows = run_query(
        supabase.table(DOGS_TABLE).delete().eq("id", str(dog_id)).eq("owner_id", user.id)
    )
    first_row(rows, NOT_FOUND_MESSAGE)
    return {"message": DELETED_MESSAGE}


@router.put("/{dog_id}/profile")
def save_dog_profile(
    dog_id: uuid.UUID,
    body: DogProfileBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Replace the S1 questionnaire of one of the caller's dogs.

    The eight answers live in the ``dogs.profile`` jsonb column. A PUT replaces
    the whole questionnaire, so a partial answer never merges with a stale one,
    and only the dog's owner can write it — anybody else gets the same 404 as
    for a dog that does not exist, and nothing changes.

    Args:
        dog_id: The dog the questionnaire describes.
        body: The eight answers (validated enums and bool, optional texts).
        user: The authenticated owner.
        supabase: The Supabase client used to read and write the row.

    Returns:
        The stored questionnaire in the camelCase wire shape.

    Raises:
        HTTPException: 404 if the dog is not the caller's; 500 on a database
            error.
    """
    first_row(
        run_query(
            supabase.table(DOGS_TABLE)
            .select("id")
            .eq("id", dog_id)
            .eq("owner_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )
    profile = {
        "size": body.size,
        "temperament": body.temperament,
        "energy": body.energy,
        "leash_trained": body.leashTrained,
        "allergies": body.allergies,
        "medical_notes": body.medicalNotes,
        "vet_contact": body.vetContact,
        "emergency_contact": body.emergencyContact,
    }
    run_query(
        supabase.table(DOGS_TABLE)
        .update({"profile": profile})
        .eq("id", dog_id)
        .eq("owner_id", user.id)
    )
    return {
        "size": body.size,
        "temperament": body.temperament,
        "energy": body.energy,
        "leashTrained": body.leashTrained,
        "allergies": body.allergies,
        "medicalNotes": body.medicalNotes,
        "vetContact": body.vetContact,
        "emergencyContact": body.emergencyContact,
    }
