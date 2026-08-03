"""End-to-end pipeline test for the Django/DRF fixture."""

from tests.conftest import doc_operations, run_pipeline

EXPECTED = {
    ("/api/v1/pets/", "get"),
    ("/api/v1/pets/", "post"),
    ("/api/v1/pets/{pk}/", "get"),
    ("/api/v1/pets/{pk}/", "put"),
    ("/api/v1/pets/{pk}/", "patch"),
    ("/api/v1/pets/{pk}/", "delete"),
    ("/api/v1/pets/{pk}/adopt/", "post"),
    ("/api/v1/stats/", "get"),
    ("/api/v1/stats/", "post"),
    ("/health/", "get"),
}


def test_django_drf_coverage_and_contracts(tmp_path):
    metadata, docs, results = run_pipeline("django_drf", tmp_path)
    (result,) = results
    assert result.validation.ok, [m.text for m in result.validation.messages]
    assert all(result.validation.gates.values()), result.validation.gates
    doc = next(iter(docs.values()))
    assert doc_operations(doc) == EXPECTED

    # ViewSet CRUD details
    list_op = doc["paths"]["/api/v1/pets/"]["get"]
    assert list_op["security"] == [{"tokenAuth": []}]
    create = doc["paths"]["/api/v1/pets/"]["post"]
    assert "201" in create["responses"] and "400" in create["responses"]
    detail = doc["paths"]["/api/v1/pets/{pk}/"]["get"]
    pk = next(p for p in detail["parameters"] if p["in"] == "path")
    assert pk["schema"]["type"] == "integer"
    assert "404" in detail["responses"]
    # partial update uses the no-required patched variant
    patch_ref = doc["paths"]["/api/v1/pets/{pk}/"]["patch"]["requestBody"]["content"][
        "application/json"]["schema"]["$ref"]
    assert "Patched" in patch_ref

    schemes = doc["components"]["securitySchemes"]
    assert schemes["tokenAuth"] == {"type": "apiKey", "in": "header", "name": "Authorization"}

    # serializer-driven constraints survive into components
    import json

    blob = json.dumps(doc["components"]["schemas"])
    assert "maxLength" in blob and "readOnly" in blob and "enum" in blob


def test_django_apiview_without_permissions_has_no_security(tmp_path):
    _metadata, docs, _results = run_pipeline("django_drf", tmp_path)
    doc = next(iter(docs.values()))
    stats = doc["paths"]["/api/v1/stats/"]["get"]
    assert "security" not in stats
