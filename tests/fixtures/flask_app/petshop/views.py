"""Class-based views (MethodView)."""

from flask import jsonify, request
from flask.views import MethodView


class OrderAPI(MethodView):
    """Order collection endpoints."""

    def get(self):
        """List orders."""
        status = request.args.get("status")
        return jsonify(items=[], status=status)

    def post(self):
        """Place a new order."""
        payload = request.get_json()
        return jsonify(id=1), 201
