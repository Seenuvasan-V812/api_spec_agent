from openapi_agent.models.metadata import (
    Confidence,
    Evidence,
    GeneratorInfo,
    LangTypeRef,
    MetadataDocument,
    RepoInfo,
)
from openapi_agent.models.registry import (
    REF_PREFIX,
    SchemaRegistryBuilder,
    compute_structural_hashes,
    finalize_document,
    make_pending_id,
)

CONF = Confidence(level="high", reason_code="declared_type")
EV = [Evidence(file="m.py", start_line=1, end_line=1, kind="class_def")]


def _doc() -> MetadataDocument:
    return MetadataDocument(
        metadata_version="1.0.0", generator=GeneratorInfo(tool_version="0"), repo=RepoInfo()
    )


def test_pending_id_format_with_generics():
    ref = LangTypeRef(
        language="java",
        qualified_name="com.acme.Page",
        type_args=[LangTypeRef(language="java", qualified_name="com.acme.User")],
    )
    assert make_pending_id(ref) == "java.com.acme.Page__of__com.acme.User"


def test_recursive_cycle_hashes_deterministically():
    schemas_a = {
        "py.User": {"type": "object", "properties": {"team": {"$ref": REF_PREFIX + "py.Team"}}},
        "py.Team": {"type": "object", "properties": {"lead": {"$ref": REF_PREFIX + "py.User"}}},
    }
    schemas_b = dict(reversed(list(schemas_a.items())))
    hashes_a = compute_structural_hashes(schemas_a)
    hashes_b = compute_structural_hashes(schemas_b)
    assert hashes_a == hashes_b
    assert hashes_a["py.User"] != hashes_a["py.Team"]  # distinct members of the SCC


def test_same_qname_different_shape_gets_variant_key():
    builder = SchemaRegistryBuilder()
    lang = LangTypeRef(language="python", qualified_name="app.User")
    ref1 = builder.intern(lang, {"type": "object", "properties": {"a": {"type": "string"}}}, EV, CONF, "s1")
    ref2 = builder.intern(lang, {"type": "object", "properties": {"b": {"type": "integer"}}}, EV, CONF, "s2")
    assert ref1 != ref2
    assert len(builder.entries) == 2


def test_same_shape_same_qname_deduplicates_and_tracks_services():
    builder = SchemaRegistryBuilder()
    lang = LangTypeRef(language="python", qualified_name="app.User")
    shape = {"type": "object", "properties": {"a": {"type": "string"}}}
    ref1 = builder.intern(lang, dict(shape), EV, CONF, "s1")
    ref2 = builder.intern(lang, dict(shape), EV, CONF, "s2")
    assert ref1 == ref2
    entry = next(iter(builder.entries.values()))
    assert entry.used_by_services == ["s1", "s2"]


def test_finalize_rewrites_refs_everywhere():
    builder = SchemaRegistryBuilder()
    user = LangTypeRef(language="python", qualified_name="app.User")
    builder.intern(user, {"type": "object"}, EV, CONF, "svc")
    document = _doc()
    finalize_document(document, builder)
    (schema_id,) = document.schema_registry.schemas
    assert "--" in schema_id
    entry = document.schema_registry.schemas[schema_id]
    assert entry.structural_hash and entry.schema_id == schema_id
