"""End-to-end pipeline tests for the Python framework fixtures."""

import json

from tests.conftest import doc_operations, make_config, run_pipeline

EXPECTED_FASTAPI = {
    ("/api/v1/pets", "get"),
    ("/api/v1/pets", "post"),
    ("/api/v1/pets/{pet_id}", "get"),
    ("/api/v1/pets/{pet_id}", "delete"),
    ("/api/v1/pets/{pet_id}/photo", "post"),
    ("/api/v1/pets/{pet_id}/photo", "get"),
    ("/api/v1/orders", "post"),
    ("/api/v1/orders/{order_id}", "get"),
    ("/health", "get"),
}


def test_fastapi_full_coverage_and_validity(tmp_path):
    metadata, docs, results = run_pipeline("fastapi_app", tmp_path)
    (result,) = results
    assert result.validation.ok, [m.text for m in result.validation.messages]
    assert all(result.validation.gates.values()), result.validation.gates
    doc = docs["store"]
    assert doc_operations(doc) == EXPECTED_FASTAPI
    assert doc["openapi"] == "3.1.0"

    # contract details
    create = doc["paths"]["/api/v1/pets"]["post"]
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("Pet")
    assert create["security"] == [{"oauth2_scheme": ["pets:write"]}]
    upload = doc["paths"]["/api/v1/pets/{pet_id}/photo"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    schemes = doc["components"]["securitySchemes"]
    assert schemes["oauth2_scheme"]["type"] == "oauth2"
    assert "pets:write" in schemes["oauth2_scheme"]["flows"]["password"]["scopes"]
    # error from the service layer call chain
    assert "404" in doc["paths"]["/api/v1/pets/{pet_id}"]["get"]["responses"]


def test_fastapi_deterministic_output(tmp_path):
    run_pipeline("fastapi_app", tmp_path / "a")
    run_pipeline("fastapi_app", tmp_path / "b")
    meta_a = (tmp_path / "a" / "meta.json").read_bytes()
    meta_b = (tmp_path / "b" / "meta.json").read_bytes()
    assert meta_a == meta_b
    doc_a = (tmp_path / "a" / "openapi.yaml").read_bytes()
    doc_b = (tmp_path / "b" / "openapi.yaml").read_bytes()
    assert doc_a == doc_b


def test_flask_full_coverage_and_validity(tmp_path):
    metadata, docs, results = run_pipeline("flask_app", tmp_path)
    (result,) = results
    assert result.validation.ok
    assert all(result.validation.gates.values())
    doc = next(iter(docs.values()))
    operations = doc_operations(doc)
    assert ("/api/v1/pets/{pet_id}", "get") in operations
    assert ("/health", "get") in operations
    # path converter typing
    get_pet = doc["paths"]["/api/v1/pets/{pet_id}"]["get"]
    pet_id = next(p for p in get_pet["parameters"] if p["in"] == "path")
    assert pet_id["schema"]["type"] == "integer"
    assert "404" in get_pet["responses"]


def test_no_fabricated_content_against_metadata(tmp_path):
    metadata, docs, _results = run_pipeline("fastapi_app", tmp_path)
    doc = docs["store"]
    service = metadata["services"][0]
    metadata_ops = {
        (endpoint["path"], operation["method"])
        for endpoint in service["endpoints"]
        for operation in endpoint["operations"]
    }
    assert doc_operations(doc) == metadata_ops
    # every documented status exists in metadata
    for endpoint in service["endpoints"]:
        for operation in endpoint["operations"]:
            doc_statuses = set(doc["paths"][endpoint["path"]][operation["method"]]["responses"])
            metadata_statuses = {v["status"] for v in operation["responses"]}
            assert doc_statuses <= metadata_statuses


def test_metadata_validates_against_published_schema(tmp_path):
    import jsonschema

    metadata, _docs, _results = run_pipeline("microservices", tmp_path)
    from openapi_agent.models.metadata import export_metadata_schema

    jsonschema.validate(metadata, export_metadata_schema(),
                        cls=jsonschema.validators.Draft202012Validator)


def test_internal_metadata_never_leaks_into_document(tmp_path):
    _metadata, docs, _results = run_pipeline("fastapi_app", tmp_path)
    blob = json.dumps(docs["store"])
    for needle in ("confidence", "evidence", "structural_hash", "raw_path",
                   "schema_registry", "reason_code", "handler"):
        assert needle not in blob, needle
