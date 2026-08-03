from pathlib import Path

from openapi_agent.analysis.java.ts_scanner import build_java_index
from openapi_agent.analysis.java.type_schema import JavaTypeConverter, split_generic
from openapi_agent.models.registry import SchemaRegistryBuilder

FIXTURE = Path(__file__).parents[1] / "fixtures" / "spring_mvc"


def _converter():
    java_files = [str(p.relative_to(FIXTURE)).replace("\\", "/") for p in FIXTURE.rglob("*.java")]
    index = build_java_index(FIXTURE, sorted(java_files))
    return JavaTypeConverter(index, SchemaRegistryBuilder(), "svc"), index


def test_split_generic():
    assert split_generic("Map<String, List<Book>>") == ("Map", ["String", "List<Book>"])


def test_primitives_and_jdk_types():
    converter, _ = _converter()
    assert converter.convert("long", None)[0] == {"type": "integer", "format": "int64"}
    assert converter.convert("OffsetDateTime", None)[0] == {"type": "string", "format": "date-time"}
    assert converter.convert("byte[]", None)[0] == {"type": "string", "format": "binary"}


def test_enum_from_index():
    converter, index = _converter()
    cls = index.resolve("com.example.bookstore.dto.Genre")
    schema, confidence = converter.convert("Genre", cls)
    entry = next(iter(converter.registry.entries.values()))
    assert entry.json_schema["enum"] == ["FICTION", "NON_FICTION", "SCIENCE", "HISTORY"]
    assert confidence.level == "high"


def test_bean_validation_constraints():
    converter, index = _converter()
    site = index.resolve("com.example.bookstore.controller.BookController")
    converter.convert("CreateBookRequest", site)
    entry = converter.registry.entries["java.com.example.bookstore.dto.CreateBookRequest"]
    props = entry.json_schema["properties"]
    assert props["title"] == {"type": "string", "minLength": 1, "maxLength": 200}
    assert props["publicationYear"]["minimum"] == 1450
    assert props["isbn"]["pattern"] == "^\\d{13}$"
    assert set(entry.json_schema["required"]) == {"title", "genre"}


def test_generic_instantiation_and_jackson():
    converter, index = _converter()
    site = index.resolve("com.example.bookstore.controller.BookController")
    schema, _ = converter.convert("PageResponse<Book>", site)
    assert schema["$ref"].endswith("PageResponse__of__com.example.bookstore.dto.Book")
    page_entry = converter.registry.entries[
        "java.com.example.bookstore.dto.PageResponse__of__com.example.bookstore.dto.Book"
    ]
    items = page_entry.json_schema["properties"]["items"]
    assert items["type"] == "array" and "Book" in items["items"]["$ref"]
    book_entry = converter.registry.entries["java.com.example.bookstore.dto.Book"]
    assert "authorName" in book_entry.json_schema["properties"]  # @JsonProperty
    assert "internalNote" not in book_entry.json_schema["properties"]  # @JsonIgnore


def test_response_entity_and_reactive_unwrap():
    converter, index = _converter()
    site = index.resolve("com.example.bookstore.controller.BookController")
    assert converter.convert("ResponseEntity<Book>", site)[0]["$ref"].endswith(".Book")
    assert converter.convert("Mono<Book>", site)[0]["$ref"].endswith(".Book")
    flux, _ = converter.convert("Flux<Book>", site)
    assert flux["type"] == "array"


def test_unresolved_type_degrades_honestly():
    converter, _ = _converter()
    schema, confidence = converter.convert("com.unknown.Thing", None)
    assert schema == {}
    assert confidence.level == "low"
