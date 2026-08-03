from openapi_agent.config.loader import AgentConfig
from openapi_agent.models.metadata import (
    Confidence,
    Endpoint,
    GeneratorInfo,
    MetadataDocument,
    Operation,
    RepoInfo,
    ResponseVariant,
    Service,
)
from openapi_agent.openapi.validators import ValidationOutcome, run_gates

CONF = Confidence(level="high", reason_code="declared_annotation")


def _metadata() -> tuple[MetadataDocument, Service]:
    service = Service(
        id="svc", name="svc", language="python", framework="fastapi",
        endpoints=[
            Endpoint(path="/pets/{pet_id}", raw_path="/pets/{pet_id}", operations=[
                Operation(method="get", operation_id="op1", handler="h",
                          responses=[ResponseVariant(status="200", origin="return_type", confidence=CONF)],
                          confidence=CONF),
            ]),
        ],
    )
    document = MetadataDocument(
        metadata_version="1.0.0", generator=GeneratorInfo(tool_version="0"),
        repo=RepoInfo(), services=[service],
    )
    return document, service


def _valid_doc() -> dict:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/pets/{pet_id}": {
                "get": {
                    "operationId": "op1",
                    "parameters": [
                        {"name": "pet_id", "in": "path", "required": True, "schema": {}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


def _run(doc, strict=False):
    metadata, service = _metadata()
    outcome = ValidationOutcome()
    run_gates(doc, metadata, service, outcome, strict=strict)
    return outcome


def test_valid_document_passes_all_gates():
    outcome = _run(_valid_doc())
    assert all(outcome.gates.values()), outcome.gates


def test_invented_path_fails_gate():
    doc = _valid_doc()
    doc["paths"]["/admin"] = {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}
    outcome = _run(doc)
    assert outcome.gates["no_invented_operations"] is False


def test_omitted_endpoint_fails_coverage():
    doc = _valid_doc()
    del doc["paths"]["/pets/{pet_id}"]
    outcome = _run(doc)
    assert outcome.gates["endpoint_coverage"] is False


def test_invented_status_fails_gate():
    doc = _valid_doc()
    doc["paths"]["/pets/{pet_id}"]["get"]["responses"]["500"] = {"description": "boom"}
    outcome = _run(doc)
    assert outcome.gates["no_invented_details"] is False


def test_invented_security_fails_gate():
    doc = _valid_doc()
    doc["paths"]["/pets/{pet_id}"]["get"]["security"] = [{"madeUpAuth": []}]
    outcome = _run(doc)
    assert outcome.gates["no_invented_details"] is False


def test_undeclared_path_param_fails_gate():
    doc = _valid_doc()
    doc["paths"]["/pets/{pet_id}"]["get"]["parameters"] = []
    outcome = _run(doc)
    assert outcome.gates["path_params_declared"] is False


def test_duplicate_operation_ids_fail_gate():
    doc = _valid_doc()
    doc["paths"]["/pets/{pet_id}"]["delete"] = {
        "operationId": "op1", "responses": {"200": {"description": "ok"}},
        "parameters": [{"name": "pet_id", "in": "path", "required": True, "schema": {}}],
    }
    outcome = _run(doc)
    assert outcome.gates["unique_operation_ids"] is False
