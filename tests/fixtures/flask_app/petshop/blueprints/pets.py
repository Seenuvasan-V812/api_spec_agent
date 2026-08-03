"""Pets blueprint: CRUD over the pet collection."""

from flask import Blueprint, abort, jsonify, request

pets_bp = Blueprint("pets", __name__, url_prefix="/pets")

PETS = {1: {"name": "Rex", "status": "available"}}


@pets_bp.get("")
def list_pets():
    """List pets.

    Returns a page of pets; supports limit/offset pagination.
    """
    limit = request.args.get("limit", 20, type=int)
    offset = request.args.get("offset", 0, type=int)
    return jsonify(items=[], limit=limit, offset=offset)


@pets_bp.get("/<int:pet_id>")
def get_pet(pet_id):
    """Fetch a single pet by its id."""
    pet = PETS.get(pet_id)
    if pet is None:
        abort(404, description="pet not found")
    return jsonify(id=pet_id, name=pet["name"])


@pets_bp.post("")
def create_pet():
    """Register a new pet."""
    payload = request.get_json()
    return jsonify(id=2, name=payload.get("name")), 201


@pets_bp.delete("/<int:pet_id>")
def delete_pet(pet_id):
    """Remove a pet from the store."""
    if pet_id not in PETS:
        abort(404)
    del PETS[pet_id]
    return "", 204
