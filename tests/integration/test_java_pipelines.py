"""End-to-end pipeline tests for the Java fixtures (tree-sitter fallback path:
no sidecar JAR is present in CI, which exercises the JVM-absent degradation)."""

from tests.conftest import doc_operations, run_pipeline

EXPECTED_SPRING = {
    ("/api/v1/books", "get"),
    ("/api/v1/books", "post"),
    ("/api/v1/books/{id}", "get"),
    ("/api/v1/books/{id}", "put"),
    ("/api/v1/books/{id}", "delete"),
    ("/api/v1/books/{id}/cover", "post"),
}

EXPECTED_JAXRS = {
    ("/api/items", "get"),
    ("/api/items", "post"),
    ("/api/items/{id}", "get"),
    ("/api/items/{id}", "put"),
    ("/api/items/{id}", "delete"),
}

EXPECTED_WEBFLUX = {
    ("/quotes/{symbol}", "get"),
    ("/quotes/stream", "get"),
    ("/quotes", "post"),
    ("/watchlists", "get"),
    ("/watchlists", "post"),
    ("/watchlists/{name}", "get"),
}


def test_spring_mvc_coverage_and_contracts(tmp_path):
    metadata, docs, results = run_pipeline("spring_mvc", tmp_path)
    (result,) = results
    assert result.validation.ok
    assert all(result.validation.gates.values())
    doc = docs["bookstore"]
    assert doc_operations(doc) == EXPECTED_SPRING

    create = doc["paths"]["/api/v1/books"]["post"]
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("Book")
    assert "400" in create["responses"]  # @Valid
    assert create["security"] == [{"bearerAuth": []}]  # @PreAuthorize + filter chain proof
    body_ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert body_ref.endswith("CreateBookRequest")

    get_book = doc["paths"]["/api/v1/books/{id}"]["get"]
    assert "404" in get_book["responses"]  # throw in service -> @ControllerAdvice
    assert get_book["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")

    schemas = doc["components"]["schemas"]
    assert schemas["Genre"]["enum"] == ["FICTION", "NON_FICTION", "SCIENCE", "HISTORY"]
    assert "PageResponse_Book" in schemas
    assert doc["components"]["securitySchemes"]["bearerAuth"]["bearerFormat"] == "JWT"


def test_jaxrs_coverage_and_contracts(tmp_path):
    metadata, docs, results = run_pipeline("jaxrs_app", tmp_path)
    (result,) = results
    assert result.validation.ok
    doc = docs["inventory"]
    assert doc_operations(doc) == EXPECTED_JAXRS
    get_item = doc["paths"]["/api/items/{id}"]["get"]
    assert "404" in get_item["responses"]  # ExceptionMapper
    list_items = doc["paths"]["/api/items"]["get"]
    limit = next(p for p in list_items["parameters"] if p["name"] == "limit")
    assert limit["schema"]["default"] == 50  # @DefaultValue coerced to integer


def test_webflux_annotated_and_functional_routes(tmp_path):
    metadata, docs, results = run_pipeline("spring_webflux", tmp_path)
    (result,) = results
    assert result.validation.ok
    doc = docs["quotes"]
    assert doc_operations(doc) == EXPECTED_WEBFLUX
    stream = doc["paths"]["/quotes/stream"]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    # functional route with body(..., Watchlist.class) hint
    watchlists = doc["paths"]["/watchlists"]["get"]
    schema = watchlists["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema.get("$ref", "").endswith("Watchlist")


def test_jvm_absent_fallback_is_recorded(tmp_path):
    metadata, _docs, results = run_pipeline("spring_mvc", tmp_path)
    assert any(w["code"] == "W401" for w in metadata["warnings"])
    assert results[0].validation.ok  # degraded but valid


def test_maven_multimodule_one_spec_per_service_plus_catalog(tmp_path):
    import json

    metadata, docs, results = run_pipeline("maven_multimodule", tmp_path)
    assert {r.service_id for r in results} == {"orders-service", "users-service"}
    assert all(r.validation.ok for r in results)
    assert doc_operations(docs["orders-service"]) == {("/orders/{id}", "get"), ("/orders", "post")}
    assert doc_operations(docs["users-service"]) == {("/users/{id}", "get")}
    catalog = json.loads((tmp_path / "openapi.catalog.json").read_text(encoding="utf-8"))
    assert {s["id"] for s in catalog["services"]} == {"orders-service", "users-service"}
