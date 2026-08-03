from openapi_agent.llm.base import OperationEnrichment
from openapi_agent.llm.grounding import is_grounded
from openapi_agent.models.metadata import (
    Confidence,
    MediaTypeContract,
    Operation,
    Parameter,
    ResponseVariant,
    SecurityEvidence,
    Service,
)

CONF = Confidence(level="high", reason_code="declared_type")


def _operation(secured: bool = False) -> Operation:
    return Operation(
        method="get",
        operation_id="svc.app.get_pet.get",
        handler="app.get_pet",
        parameters=[
            Parameter(name="pet_id", location="path", required=True, schema={"type": "integer"}, confidence=CONF)
        ],
        responses=[
            ResponseVariant(status="200", origin="return_type", confidence=CONF,
                            content={"application/json": MediaTypeContract(schema={"type": "object", "properties": {"name": {}}})}),
            ResponseVariant(status="404", origin="raise_site", confidence=CONF),
        ],
        security=[SecurityEvidence(scheme_id="oauth", mechanism="decorator", confidence=CONF)] if secured else [],
        confidence=CONF,
    )


SERVICE = Service(id="svc", name="svc", language="python", framework="fastapi")


def test_grounded_output_accepted():
    enrichment = OperationEnrichment(
        summary="Fetch a pet by id",
        response_descriptions={"200": "The pet", "404": "Pet not found"},
        parameter_descriptions={"pet_id": "Pet identifier"},
        tags=["pets"],
    )
    ok, reason = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert ok, reason


def test_undeclared_status_rejected():
    enrichment = OperationEnrichment(summary="x", response_descriptions={"500": "boom"})
    ok, reason = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert not ok and "500" in reason


def test_status_mentioned_in_text_rejected():
    enrichment = OperationEnrichment(summary="Returns 418 when the teapot is busy")
    ok, _ = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert not ok


def test_undeclared_parameter_rejected():
    enrichment = OperationEnrichment(summary="x", parameter_descriptions={"limit": "page size"})
    ok, _ = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert not ok


def test_auth_claim_without_security_rejected():
    enrichment = OperationEnrichment(summary="x", description="Requires an API key to call.")
    ok, reason = is_grounded(enrichment, _operation(secured=False), "/pets/{pet_id}", SERVICE)
    assert not ok and "authentication" in reason
    ok_secured, _ = is_grounded(enrichment, _operation(secured=True), "/pets/{pet_id}", SERVICE)
    assert ok_secured


def test_invented_tag_rejected():
    enrichment = OperationEnrichment(summary="x", tags=["billing"])
    ok, _ = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert not ok


def test_foreign_path_rejected():
    enrichment = OperationEnrichment(summary="See /admin/users for details")
    ok, _ = is_grounded(enrichment, _operation(), "/pets/{pet_id}", SERVICE)
    assert not ok
