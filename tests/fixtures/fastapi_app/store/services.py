"""Business logic layer (exercises cross-file call-chain analysis)."""

from fastapi import HTTPException

from store.models import Pet, PetStatus

_DB: dict[int, Pet] = {}


def find_pet(pet_id: int) -> Pet:
    pet = _DB.get(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail="Pet not found")
    return pet


def remove_pet(pet_id: int) -> None:
    if pet_id not in _DB:
        raise HTTPException(status_code=404, detail="Pet not found")
    del _DB[pet_id]


def place_order_for(pet_id: int, quantity: int) -> int:
    pet = find_pet(pet_id)
    if pet.status != PetStatus.AVAILABLE:
        raise HTTPException(status_code=409, detail="Pet is not available")
    return 1
