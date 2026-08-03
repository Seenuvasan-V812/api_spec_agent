from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Query, Security, UploadFile
from fastapi.responses import FileResponse

from store.models import ErrorMessage, Pet, PetCreate, PetStatus
from store.security import get_current_user
from store.services import find_pet, remove_pet

router = APIRouter(prefix="/pets", tags=["pets"])


@router.get("", response_model=list[Pet])
def list_pets(
    status: Optional[PetStatus] = Query(default=None, description="Filter by status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = 0,
):
    """List pets.

    Returns a page of pets, optionally filtered by status.
    """
    return []


@router.get("/{pet_id}", response_model=Pet, responses={404: {"model": ErrorMessage, "description": "Pet not found"}})
def get_pet(pet_id: int):
    """Fetch a single pet by its id."""
    return find_pet(pet_id)


@router.post("", response_model=Pet, status_code=201)
def create_pet(payload: PetCreate, user: dict = Security(get_current_user, scopes=["pets:write"])):
    """Register a new pet."""
    return Pet(id=1, name=payload.name)


@router.delete("/{pet_id}", status_code=204)
def delete_pet(pet_id: int, user: dict = Security(get_current_user, scopes=["pets:write"])):
    """Remove a pet from the store."""
    remove_pet(pet_id)


@router.post("/{pet_id}/photo")
def upload_photo(
    pet_id: int,
    photo: Annotated[UploadFile, File(description="JPEG or PNG image")],
    caption: Optional[str] = None,
):
    """Attach a photo to a pet."""
    find_pet(pet_id)
    return {"filename": photo.filename, "caption": caption}


@router.get("/{pet_id}/photo", response_class=FileResponse)
def download_photo(pet_id: int):
    """Download the current photo of a pet."""
    find_pet(pet_id)
    return FileResponse("photo.jpg")
