"""Java type text → JSON Schema 2020-12.

Resolution order: primitive/JDK map → container/wrapper unwrapping →
repo-indexed class (interned into the schema registry). Jackson annotations
(@JsonProperty/@JsonIgnore), Bean Validation constraints (@NotNull, @Size,
@Min, @Max, @Pattern, @Email, ...), records, enums, inheritance chains, and
generic instantiations (``Page<UserDto>``) are all handled from the
tree-sitter index. When the JavaParser sidecar contributed solver-resolved
facts they take precedence; without a JVM the same code runs with reduced
confidence (``sidecar_unavailable``) — never guessed shapes.
"""

from __future__ import annotations

import re

from openapi_agent.analysis.java.ts_scanner import JavaClass, JavaField, JavaIndex, JavaParam
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import Confidence, Evidence, JsonSchemaDict, LangTypeRef
from openapi_agent.models.registry import REF_PREFIX, make_pending_id

log = get_logger("analysis.java.types")

_LEVEL_RANK = {"high": 0, "medium": 1, "low": 2}

_SIMPLE: dict[str, JsonSchemaDict] = {
    "void": {},
    "Void": {},
    "String": {"type": "string"},
    "CharSequence": {"type": "string"},
    "char": {"type": "string"},
    "Character": {"type": "string"},
    "int": {"type": "integer", "format": "int32"},
    "Integer": {"type": "integer", "format": "int32"},
    "long": {"type": "integer", "format": "int64"},
    "Long": {"type": "integer", "format": "int64"},
    "short": {"type": "integer"},
    "Short": {"type": "integer"},
    "byte": {"type": "integer"},
    "Byte": {"type": "integer"},
    "BigInteger": {"type": "integer"},
    "double": {"type": "number", "format": "double"},
    "Double": {"type": "number", "format": "double"},
    "float": {"type": "number", "format": "float"},
    "Float": {"type": "number", "format": "float"},
    "BigDecimal": {"type": "number"},
    "boolean": {"type": "boolean"},
    "Boolean": {"type": "boolean"},
    "Object": {},
    "JsonNode": {},
    "ObjectNode": {"type": "object"},
    "UUID": {"type": "string", "format": "uuid"},
    "LocalDate": {"type": "string", "format": "date"},
    "LocalTime": {"type": "string", "format": "time"},
    "LocalDateTime": {"type": "string", "format": "date-time"},
    "OffsetDateTime": {"type": "string", "format": "date-time"},
    "ZonedDateTime": {"type": "string", "format": "date-time"},
    "Instant": {"type": "string", "format": "date-time"},
    "Date": {"type": "string", "format": "date-time"},
    "Duration": {"type": "string", "format": "duration"},
    "URI": {"type": "string", "format": "uri"},
    "URL": {"type": "string", "format": "uri"},
    "Locale": {"type": "string"},
    "MultipartFile": {"type": "string", "format": "binary"},
    "FilePart": {"type": "string", "format": "binary"},
    "Resource": {"type": "string", "format": "binary"},
    "InputStreamResource": {"type": "string", "format": "binary"},
    "ByteArrayResource": {"type": "string", "format": "binary"},
    "StreamingResponseBody": {"type": "string", "format": "binary"},
}

_SEQUENCES = {"List", "Set", "Collection", "Iterable", "ArrayList", "LinkedList", "HashSet", "SortedSet", "Flux", "Stream"}
_UNWRAP = {"ResponseEntity", "HttpEntity", "Mono", "CompletableFuture", "CompletionStage", "Callable", "DeferredResult", "Optional"}
_MAPS = {"Map", "HashMap", "TreeMap", "SortedMap", "LinkedHashMap", "MultiValueMap"}

_PAGE_TYPES = {"Page", "Slice"}


def split_generic(type_text: str) -> tuple[str, list[str]]:
    """``Page<UserDto>`` -> ("Page", ["UserDto"]); array suffix preserved by caller."""
    text = type_text.strip()
    if "<" not in text:
        return text, []
    base, _, rest = text.partition("<")
    inner = rest.rsplit(">", 1)[0]
    args: list[str] = []
    depth = 0
    current = ""
    for char in inner:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        args.append(current.strip())
    return base.strip(), args


class JavaTypeConverter:
    def __init__(self, index: JavaIndex, registry, service_id: str, sidecar_facts=None, sidecar_available: bool = False) -> None:
        self.index = index
        self.registry = registry
        self.service_id = service_id
        self.sidecar_facts = sidecar_facts or {}
        self.sidecar_available = sidecar_available
        self._in_progress: set[str] = set()
        self._worst: tuple[str, str] = ("high", "declared_type")

    # -- public ---------------------------------------------------------------

    def convert(self, type_text: str, from_class: JavaClass | None) -> tuple[JsonSchemaDict, Confidence]:
        self._worst = ("high", "declared_type")
        schema = self._convert(type_text, from_class, {})
        return schema, self._confidence()

    def _confidence(self) -> Confidence:
        level, reason = self._worst
        return Confidence(level=level, reason_code=reason)  # type: ignore[arg-type]

    def _degrade(self, level: str, reason: str) -> None:
        if _LEVEL_RANK[level] > _LEVEL_RANK[self._worst[0]]:
            self._worst = (level, reason)

    # -- conversion -------------------------------------------------------------

    def _convert(self, type_text: str, from_class: JavaClass | None, binding: dict[str, str]) -> JsonSchemaDict:
        text = type_text.strip()
        if not text:
            self._degrade("low", "dynamic_type")
            return {}
        # arrays
        if text.endswith("[]"):
            element = text[:-2].strip()
            if element in ("byte", "Byte"):
                return {"type": "string", "format": "binary"}
            return {"type": "array", "items": self._convert(element, from_class, binding)}
        if text.endswith("..."):
            return {"type": "array", "items": self._convert(text[:-3], from_class, binding)}
        if text in binding:
            bound = binding[text]
            if bound == text:  # unresolved type var
                self._degrade("low", "dynamic_type")
                return {}
            return self._convert(bound, from_class, {})
        base, args = split_generic(text)
        simple = base.rsplit(".", 1)[-1]

        if simple == "Optional" and args:
            inner = self._convert(args[0], from_class, binding)
            return _nullable(inner)
        if simple in _UNWRAP:
            if args:
                return self._convert(args[0], from_class, binding)
            self._degrade("medium", "dynamic_type")
            return {}
        if simple in _SEQUENCES:
            items = self._convert(args[0], from_class, binding) if args else {}
            if not args:
                self._degrade("medium", "dynamic_type")
            schema: JsonSchemaDict = {"type": "array", "items": items}
            if simple in ("Set", "HashSet", "SortedSet"):
                schema["uniqueItems"] = True
            return schema
        if simple in _MAPS:
            value = self._convert(args[1], from_class, binding) if len(args) > 1 else {}
            return {"type": "object", "additionalProperties": value or True}
        if simple in _PAGE_TYPES and args:
            # Spring Data Page: framework-documented envelope
            self._degrade("medium", "framework_default")
            return {
                "type": "object",
                "properties": {
                    "content": {"type": "array", "items": self._convert(args[0], from_class, binding)},
                    "totalElements": {"type": "integer", "format": "int64"},
                    "totalPages": {"type": "integer"},
                    "number": {"type": "integer"},
                    "size": {"type": "integer"},
                },
            }
        if simple in _SIMPLE and not args:
            return dict(_SIMPLE[simple])

        resolved = self.index.resolve(base, from_class)
        if resolved is not None:
            return self._nominal_ref(resolved, args, from_class)

        # unresolved: reason depends on whether the sidecar could have helped
        self._degrade("low", "unresolved_symbol" if self.sidecar_available else "sidecar_unavailable")
        return {}

    # -- nominal types ------------------------------------------------------------

    def _nominal_ref(self, cls: JavaClass, generic_args: list[str], usage_site: JavaClass | None) -> JsonSchemaDict:
        type_args = [
            self._lang_ref_for(argument, usage_site) for argument in generic_args
        ]
        lang_type = LangTypeRef(language="java", qualified_name=cls.qualified, type_args=type_args)
        pending_id = make_pending_id(lang_type)
        if pending_id in self._in_progress or self.registry.contains(pending_id):
            return {"$ref": REF_PREFIX + pending_id}
        self._in_progress.add(pending_id)
        try:
            evidence = [
                Evidence(
                    file=cls.file,
                    start_line=cls.start_line,
                    end_line=cls.end_line,
                    kind="class_def",
                    symbol=cls.qualified,
                )
            ]
            saved = self._worst
            self._worst = ("high", "declared_type")
            schema = self._class_schema(cls, generic_args, usage_site)
            class_confidence = self._confidence()
            self._worst = max((saved, self._worst), key=lambda w: _LEVEL_RANK[w[0]])
            self.registry.intern(lang_type, schema, evidence, class_confidence, self.service_id)
        finally:
            self._in_progress.discard(pending_id)
        return {"$ref": REF_PREFIX + pending_id}

    def _lang_ref_for(self, type_text: str, usage_site: JavaClass | None) -> LangTypeRef:
        base, args = split_generic(type_text)
        resolved = self.index.resolve(base, usage_site)
        qualified = resolved.qualified if resolved is not None else base
        return LangTypeRef(
            language="java",
            qualified_name=qualified,
            type_args=[self._lang_ref_for(a, usage_site) for a in args],
        )

    def _class_schema(self, cls: JavaClass, generic_args: list[str], usage_site: JavaClass | None) -> JsonSchemaDict:
        if cls.kind == "enum":
            schema: JsonSchemaDict = {"type": "string"}
            if cls.enum_constants:
                schema["enum"] = list(cls.enum_constants)
            if cls.javadoc:
                schema["description"] = cls.javadoc
            return schema

        binding: dict[str, str] = {}
        if cls.type_params and generic_args:
            binding = dict(zip(cls.type_params, generic_args))
        elif cls.type_params and not generic_args:
            binding = {p: p for p in cls.type_params}  # erased usage
            self._degrade("low", "dynamic_type")

        properties: dict[str, JsonSchemaDict] = {}
        required: list[str] = []

        # inheritance: parent fields first
        if cls.extends_text:
            parent_base, parent_args = split_generic(cls.extends_text)
            parent = self.index.resolve(parent_base, cls)
            if parent is not None and parent.qualified != cls.qualified:
                parent_binding: dict[str, str] = {}
                if parent.type_params and parent_args:
                    resolved_args = [binding.get(a, a) for a in parent_args]
                    parent_binding = dict(zip(parent.type_params, resolved_args))
                self._collect_fields(parent, parent_binding, properties, required)
            elif parent is None and parent_base.rsplit(".", 1)[-1] not in ("Object", "Exception", "RuntimeException", "Throwable"):
                self._degrade("medium", "unresolved_symbol")

        self._collect_fields(cls, binding, properties, required)

        if cls.kind == "record":
            for component in cls.record_components:
                schema = self._convert(component.type_text, cls, binding)
                self._apply_validation(component.annotations, schema, component.type_text)
                name = _jackson_name(component.annotations) or component.name
                properties[name] = schema
                if _is_required(component.annotations):
                    required.append(name)

        if not properties:
            self._degrade("low", "dynamic_type")
            return {"type": "object", "additionalProperties": True}
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = sorted(set(required))
        if cls.javadoc:
            schema["description"] = cls.javadoc
        return schema

    def _collect_fields(self, cls: JavaClass, binding: dict[str, str], properties: dict, required: list[str]) -> None:
        for field in cls.fields:
            if "static" in field.modifiers or "transient" in field.modifiers:
                continue
            if any(a.name == "JsonIgnore" for a in field.annotations):
                continue
            schema = self._convert(field.type_text, cls, binding)
            self._apply_validation(field.annotations, schema, field.type_text)
            if field.javadoc and "$ref" not in schema and "description" not in schema:
                schema["description"] = field.javadoc
            name = _jackson_name(field.annotations) or field.name
            properties[name] = schema
            if _is_required(field.annotations):
                required.append(name)

    def _apply_validation(self, annotations, schema: JsonSchemaDict, type_text: str) -> None:
        if "$ref" in schema:
            return
        is_string = schema.get("type") == "string"
        is_array = schema.get("type") == "array"
        for annotation in annotations:
            name = annotation.name
            if name == "Size":
                min_value = _to_int(annotation.kw("min"))
                max_value = _to_int(annotation.kw("max"))
                if is_array:
                    if min_value is not None:
                        schema["minItems"] = min_value
                    if max_value is not None:
                        schema["maxItems"] = max_value
                else:
                    if min_value is not None:
                        schema["minLength"] = min_value
                    if max_value is not None:
                        schema["maxLength"] = max_value
            elif name == "Min":
                value = _to_int(annotation.value or annotation.kw("value"))
                if value is not None:
                    schema["minimum"] = value
            elif name == "Max":
                value = _to_int(annotation.value or annotation.kw("value"))
                if value is not None:
                    schema["maximum"] = value
            elif name == "DecimalMin":
                value = _to_float(annotation.value or annotation.kw("value"))
                if value is not None:
                    schema["minimum"] = value
            elif name == "DecimalMax":
                value = _to_float(annotation.value or annotation.kw("value"))
                if value is not None:
                    schema["maximum"] = value
            elif name == "Pattern":
                regexp = annotation.kw("regexp")
                if regexp:
                    schema["pattern"] = _anchor_pattern(regexp)
            elif name == "Email" and is_string:
                schema["format"] = "email"
            elif name == "Positive":
                schema["exclusiveMinimum"] = 0
            elif name == "PositiveOrZero":
                schema["minimum"] = 0
            elif name == "Negative":
                schema["exclusiveMaximum"] = 0
            elif name == "NegativeOrZero":
                schema["maximum"] = 0
            elif name == "NotBlank" and is_string:
                schema.setdefault("minLength", 1)
            elif name == "NotEmpty":
                if is_array:
                    schema.setdefault("minItems", 1)
                elif is_string:
                    schema.setdefault("minLength", 1)


def _anchor_pattern(regexp: str) -> str:
    """Anchor an unanchored regex so it matches the whole value (JSON Schema /
    OpenAPI treat ``pattern`` as an unanchored partial match otherwise)."""
    anchored = regexp
    if not anchored.startswith("^"):
        anchored = "^(?:" + anchored + ")" if not anchored.endswith("$") else "^" + anchored
    if not anchored.endswith("$"):
        anchored = anchored + "$"
    return anchored


def _jackson_name(annotations) -> str | None:
    for annotation in annotations:
        if annotation.name == "JsonProperty":
            return annotation.value or annotation.kw("value")
    return None


def _is_required(annotations) -> bool:
    for annotation in annotations:
        if annotation.name in ("NotNull", "NotBlank", "NotEmpty"):
            return True
        if annotation.name == "JsonProperty" and annotation.kw("required") == "true":
            return True
    return False


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.strip().strip('"'))
    except ValueError:
        return None


def _nullable(schema: JsonSchemaDict) -> JsonSchemaDict:
    if not schema:
        return {}
    if set(schema) == {"type"} and isinstance(schema["type"], str):
        return {"type": [schema["type"], "null"]}
    return {"anyOf": [schema, {"type": "null"}]}
