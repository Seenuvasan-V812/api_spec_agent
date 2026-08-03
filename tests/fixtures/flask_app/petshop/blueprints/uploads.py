"""Uploads blueprint: pet photo attachments."""

from flask import Blueprint, jsonify, request

uploads_bp = Blueprint("uploads", __name__, url_prefix="/pets")


@uploads_bp.post("/<int:pet_id>/photo")
def upload_photo(pet_id):
    """Attach a photo to a pet."""
    photo = request.files["photo"]
    caption = request.form.get("caption")
    return jsonify(filename=photo.filename, caption=caption), 201
