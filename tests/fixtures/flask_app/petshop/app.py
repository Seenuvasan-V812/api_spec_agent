"""Pet shop Flask application."""

from flask import Flask, jsonify

from petshop.blueprints.pets import pets_bp
from petshop.blueprints.uploads import uploads_bp
from petshop.views import OrderAPI

app = Flask(__name__)

app.register_blueprint(pets_bp, url_prefix="/api/v1")
app.register_blueprint(uploads_bp, url_prefix="/api/v1")

app.add_url_rule("/orders", view_func=OrderAPI.as_view("orders"))


def version():
    """Report the running API version."""
    return jsonify(version="1.0.0")


app.add_url_rule("/version", "version", view_func=version, methods=["GET"])


@app.get("/health")
def health():
    """Service liveness probe."""
    return jsonify(status="ok")


@app.errorhandler(404)
def not_found(error):
    return jsonify(error="not found"), 404
