from openapi_agent.models.metadata import (
    Confidence,
    Endpoint,
    Evidence,
    GeneratorInfo,
    MetadataDocument,
    Operation,
    RepoInfo,
    ResponseVariant,
    Service,
    to_canonical_json,
)


def _operation(method: str) -> Operation:
    return Operation(
        method=method,
        operation_id=f"svc.handler.{method}",
        handler="app.handler",
        responses=[
            ResponseVariant(
                status="500",
                origin="raise_site",
                confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
            ),
            ResponseVariant(
                status="200",
                origin="return_type",
                confidence=Confidence(level="high", reason_code="declared_type"),
            ),
        ],
        confidence=Confidence(level="high", reason_code="declared_annotation"),
    )


def _document(order: list[str]) -> MetadataDocument:
    return MetadataDocument(
        metadata_version="1.0.0",
        generator=GeneratorInfo(tool_version="0.0.0"),
        repo=RepoInfo(),
        services=[
            Service(
                id="svc",
                name="svc",
                language="python",
                framework="fastapi",
                endpoints=[
                    Endpoint(
                        path="/things",
                        raw_path="/things",
                        operations=[_operation(m) for m in order],
                    )
                ],
            )
        ],
    )


def test_canonical_json_is_order_independent():
    doc_a = _document(["post", "get"])
    doc_b = _document(["get", "post"])
    assert to_canonical_json(doc_a) == to_canonical_json(doc_b)


def test_responses_sorted_by_status():
    doc = _document(["get"])
    to_canonical_json(doc)
    statuses = [r.status for r in doc.services[0].endpoints[0].operations[0].responses]
    assert statuses == ["200", "500"]


def test_evidence_paths_are_posix_and_relative():
    evidence = Evidence(file=".\\pkg\\module.py", start_line=1, end_line=2, kind="decorator")
    assert evidence.file == "pkg/module.py"


def test_no_timestamps_or_usernames_in_document():
    payload = to_canonical_json(_document(["get"]))
    import getpass

    assert getpass.getuser() not in payload
    assert "timestamp" not in payload
