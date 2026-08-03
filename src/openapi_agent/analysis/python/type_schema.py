"""Static Python type-annotation → JSON Schema 2020-12 conversion.

Everything here is purely static (astroid parse trees; no imports of target
code). Nominal types (Pydantic v1/v2 models, dataclasses, attrs classes,
TypedDicts, enums, plain annotated classes) are interned into the schema
registry and referenced by ``$ref``; anonymous shapes stay inline.

The converter tracks the *worst* confidence encountered during a conversion:
an unresolvable member degrades the fact that owns the schema, never invents.
"""

from __future__ import annotations

from typing import Any, Optional

import astroid
from astroid import nodes

from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import Confidence, Evidence, JsonSchemaDict, LangTypeRef
from openapi_agent.models.registry import REF_PREFIX

log = get_logger("analysis.python.types")

_SIMPLE_TYPES: dict[str, JsonSchemaDict] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "bytes": {"type": "string", "format": "binary"},
    "bytearray": {"type": "string", "format": "binary"},
    "complex": {"type": "string"},
    "object": {},
    "None": {"type": "null"},
    "NoneType": {"type": "null"},
    "dict": {"type": "object"},
    "list": {"type": "array"},
    "set": {"type": "array", "uniqueItems": True},
    "frozenset": {"type": "array", "uniqueItems": True},
    "tuple": {"type": "array"},
    "Any": {},
    "datetime.datetime": {"type": "string", "format": "date-time"},
    "datetime.date": {"type": "string", "format": "date"},
    "datetime.time": {"type": "string", "format": "time"},
    "datetime.timedelta": {"type": "string", "format": "duration"},
    "uuid.UUID": {"type": "string", "format": "uuid"},
    "decimal.Decimal": {"type": "number"},
    "pathlib.Path": {"type": "string"},
    "pydantic.EmailStr": {"type": "string", "format": "email"},
    "pydantic.networks.EmailStr": {"type": "string", "format": "email"},
    "pydantic.HttpUrl": {"type": "string", "format": "uri"},
    "pydantic.AnyUrl": {"type": "string", "format": "uri"},
    "pydantic.AnyHttpUrl": {"type": "string", "format": "uri"},
    "pydantic.SecretStr": {"type": "string", "writeOnly": True},
    "pydantic.Json": {},
    "pydantic.types.SecretStr": {"type": "string", "writeOnly": True},
}

#: short names usable without a dotted prefix (fastapi/pydantic re-exports etc.)
_SIMPLE_SHORT = {
    "datetime": {"type": "string", "format": "date-time"},
    "date": {"type": "string", "format": "date"},
    "time": {"type": "string", "format": "time"},
    "timedelta": {"type": "string", "format": "duration"},
    "UUID": {"type": "string", "format": "uuid"},
    "Decimal": {"type": "number"},
    "Path": {"type": "string"},  # pathlib.Path in annotation position
    "EmailStr": {"type": "string", "format": "email"},
    "HttpUrl": {"type": "string", "format": "uri"},
    "AnyUrl": {"type": "string", "format": "uri"},
    "AnyHttpUrl": {"type": "string", "format": "uri"},
    "SecretStr": {"type": "string", "writeOnly": True},
}

_SEQUENCE_NAMES = {"list", "List", "Sequence", "Iterable", "Iterator", "MutableSequence"}
_SET_NAMES = {"set", "Set", "FrozenSet", "frozenset", "MutableSet"}
_MAPPING_NAMES = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "DefaultDict"}

_CONSTRAINT_KWARGS = {
    "ge": "minimum",
    "gt": "exclusiveMinimum",
    "le": "maximum",
    "lt": "exclusiveMaximum",
    "min_length": "minLength",
    "max_length": "maxLength",
    "pattern": "pattern",
    "regex": "pattern",
    "min_items": "minItems",
    "max_items": "maxItems",
    "multiple_of": "multipleOf",
    "title": "title",
    "description": "description",
}

_LEVEL_RANK = {"high": 0, "medium": 1, "low": 2}


def literal_value(node: Optional[nodes.NodeNG]) -> tuple[bool, Any]:
    """(is_json_safe_literal, value) for Const/List/Tuple/Dict literals."""
    if isinstance(node, nodes.Const):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return True, node.value
        return False, None
    if isinstance(node, (nodes.List, nodes.Tuple)):
        items = []
        for element in node.elts:
            ok, value = literal_value(element)
            if not ok:
                return False, None
            items.append(value)
        return True, items
    if isinstance(node, nodes.Dict):
        result = {}
        for key_node, value_node in node.items:
            key_ok, key = literal_value(key_node)
            value_ok, value = literal_value(value_node)
            if not (key_ok and value_ok and isinstance(key, str)):
                return False, None
            result[key] = value
        return True, result
    return False, None


def dotted_name(node: Optional[nodes.NodeNG]) -> str | None:
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Attribute):
        base = dotted_name(node.expr)
        return f"{base}.{node.attrname}" if base else node.attrname
    return None


class PyTypeSchemaConverter:
    """One instance per analyzed service."""

    def __init__(self, ctx, service_id: str) -> None:
        self.ctx = ctx
        self.service_id = service_id
        self._in_progress: set[str] = set()
        self._worst: tuple[str, str] = ("high", "declared_type")

    # -- public API ---------------------------------------------------------

    def convert(
        self, ann: Optional[nodes.NodeNG], module: nodes.Module
    ) -> tuple[JsonSchemaDict, Confidence]:
        """Convert an annotation node; returns (schema, worst confidence seen)."""
        self._worst = ("high", "declared_type")
        if ann is None:
            self._degrade("low", "dynamic_type")
            return {}, self._confidence()
        schema = self._convert(ann, module)
        return schema, self._confidence()

    def convert_class(self, cls: nodes.ClassDef) -> tuple[JsonSchemaDict, Confidence]:
        self._worst = ("high", "declared_type")
        schema = self._class_schema(cls)
        return schema, self._confidence()

    def _confidence(self) -> Confidence:
        level, reason = self._worst
        return Confidence(level=level, reason_code=reason)  # type: ignore[arg-type]

    def _degrade(self, level: str, reason: str) -> None:
        if _LEVEL_RANK[level] > _LEVEL_RANK[self._worst[0]]:
            self._worst = (level, reason)

    # -- annotation dispatch --------------------------------------------------

    def _convert(self, ann: nodes.NodeNG, module: nodes.Module) -> JsonSchemaDict:
        if isinstance(ann, nodes.Const):
            if ann.value is None:
                return {"type": "null"}
            if isinstance(ann.value, str):
                return self._convert_forward_ref(ann.value, module)
        if isinstance(ann, (nodes.Name, nodes.Attribute)):
            return self._convert_named(ann, module)
        if isinstance(ann, nodes.Subscript):
            return self._convert_subscript(ann, module)
        if isinstance(ann, nodes.BinOp) and ann.op == "|":
            return self._union([ann.left, ann.right], module)
        self._degrade("low", "dynamic_type")
        return {}

    def _convert_forward_ref(self, text: str, module: nodes.Module) -> JsonSchemaDict:
        try:
            parsed = astroid.parse(text)
            expr = parsed.body[0].value  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            self._degrade("low", "unresolved_symbol")
            return {}
        return self._convert(expr, module)

    def _convert_named(self, ann: nodes.NodeNG, module: nodes.Module) -> JsonSchemaDict:
        name = dotted_name(ann)
        if name is None:
            self._degrade("low", "dynamic_type")
            return {}
        if name in _SIMPLE_TYPES:
            return dict(_SIMPLE_TYPES[name])
        if name in _SIMPLE_SHORT:
            return dict(_SIMPLE_SHORT[name])
        if name in ("Any", "typing.Any", "object"):
            return {}
        if name in ("None", "NoneType"):
            return {"type": "null"}
        resolved = self.resolve_symbol(module, name)
        if isinstance(resolved, nodes.ClassDef):
            return self._nominal_ref(resolved)
        self._degrade("low", "unresolved_symbol")
        return {}

    def _convert_subscript(self, ann: nodes.Subscript, module: nodes.Module) -> JsonSchemaDict:
        base = dotted_name(ann.value) or ""
        short = base.rsplit(".", 1)[-1]
        slice_node = ann.slice
        args: list[nodes.NodeNG] = (
            list(slice_node.elts) if isinstance(slice_node, nodes.Tuple) else [slice_node]
        )

        if short in _SEQUENCE_NAMES:
            return {"type": "array", "items": self._convert(args[0], module) if args else {}}
        if short in _SET_NAMES:
            items = self._convert(args[0], module) if args else {}
            return {"type": "array", "uniqueItems": True, "items": items}
        if short == "tuple" or short == "Tuple":
            if len(args) == 2 and isinstance(args[1], nodes.Const) and args[1].value is Ellipsis:
                return {"type": "array", "items": self._convert(args[0], module)}
            return {
                "type": "array",
                "prefixItems": [self._convert(a, module) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        if short in _MAPPING_NAMES:
            value_schema = self._convert(args[1], module) if len(args) > 1 else {}
            return {"type": "object", "additionalProperties": value_schema or True}
        if short == "Optional":
            inner = self._convert(args[0], module) if args else {}
            return self._nullable(inner)
        if short == "Union":
            return self._union(args, module)
        if short == "Literal":
            values = []
            for arg in args:
                ok, value = literal_value(arg)
                if ok:
                    values.append(value)
                else:
                    self._degrade("medium", "conditional_conflict")
            if not values:
                self._degrade("low", "dynamic_type")
                return {}
            if len(values) == 1:
                return {"const": values[0]}
            return {"enum": values}
        if short == "Annotated":
            schema = self._convert(args[0], module) if args else {}
            for meta in args[1:]:
                self._apply_annotated_meta(schema, meta, module)
            return schema
        if short in ("Type", "type"):
            return {"type": "string"}
        if short in ("ClassVar", "Final"):
            return self._convert(args[0], module) if args else {}
        # Generic nominal type (e.g. Page[User]) — resolve base class
        resolved = self.resolve_symbol(module, base)
        if isinstance(resolved, nodes.ClassDef):
            type_args = [self._lang_type_for_node(a, module) for a in args]
            return self._nominal_ref(resolved, type_args=type_args, arg_nodes=args, module=module)
        self._degrade("low", "unresolved_symbol")
        return {}

    def _apply_annotated_meta(
        self, schema: JsonSchemaDict, meta: nodes.NodeNG, module: nodes.Module
    ) -> None:
        if isinstance(meta, nodes.Call):
            func = dotted_name(meta.func) or ""
            short = func.rsplit(".", 1)[-1]
            if short in ("Field", "Query", "Path", "Header", "Cookie", "Body", "Form", "File"):
                constraints, extra = parse_field_kwargs(meta)
                schema.update(constraints)
                discriminator = extra.get("discriminator")
                if discriminator and ("anyOf" in schema or "oneOf" in schema):
                    variants = schema.pop("anyOf", None) or schema.get("oneOf")
                    schema["oneOf"] = variants
                    schema["discriminator"] = {"propertyName": discriminator}
            elif short in ("StringConstraints",):
                constraints, _ = parse_field_kwargs(meta)
                schema.update(constraints)

    def _union(self, parts: list[nodes.NodeNG], module: nodes.Module) -> JsonSchemaDict:
        schemas: list[JsonSchemaDict] = []
        has_null = False
        for part in parts:
            converted = self._convert(part, module)
            if converted == {"type": "null"}:
                has_null = True
            elif converted not in schemas:
                schemas.append(converted)
        if not schemas:
            return {"type": "null"} if has_null else {}
        if len(schemas) == 1:
            return self._nullable(schemas[0]) if has_null else schemas[0]
        result: JsonSchemaDict = {"anyOf": schemas}
        if has_null:
            result["anyOf"] = schemas + [{"type": "null"}]
        return result

    @staticmethod
    def _nullable(schema: JsonSchemaDict) -> JsonSchemaDict:
        if not schema:
            return {}
        if set(schema) == {"type"} and isinstance(schema["type"], str):
            return {"type": [schema["type"], "null"]}
        if "$ref" in schema or "anyOf" in schema or "oneOf" in schema or len(schema) > 1:
            return {"anyOf": [schema, {"type": "null"}]}
        return {"anyOf": [schema, {"type": "null"}]}

    # -- nominal classes ------------------------------------------------------

    def _lang_type_for_node(self, node: nodes.NodeNG, module: nodes.Module) -> LangTypeRef:
        name = dotted_name(node)
        if name:
            resolved = self.resolve_symbol(module, name)
            if isinstance(resolved, nodes.ClassDef):
                return LangTypeRef(language="python", qualified_name=resolved.qname())
            return LangTypeRef(language="python", qualified_name=name)
        return LangTypeRef(language="python", qualified_name="object")

    def _nominal_ref(
        self,
        cls: nodes.ClassDef,
        type_args: list[LangTypeRef] | None = None,
        arg_nodes: list[nodes.NodeNG] | None = None,
        module: nodes.Module | None = None,
    ) -> JsonSchemaDict:
        qname = cls.qname()
        lang_type = LangTypeRef(
            language="python", qualified_name=qname, type_args=type_args or []
        )
        from openapi_agent.models.registry import make_pending_id

        pending_id = make_pending_id(lang_type)
        if pending_id in self._in_progress or self.ctx.registry.contains(pending_id):
            return {"$ref": REF_PREFIX + pending_id}

        self._in_progress.add(pending_id)
        try:
            evidence = [
                Evidence(
                    file=self._file_of(cls),
                    start_line=cls.lineno or 1,
                    end_line=cls.end_lineno or cls.lineno or 1,
                    kind="class_def",
                    symbol=qname,
                )
            ]
            saved_worst = self._worst
            self._worst = ("high", "declared_type")
            schema = self._class_schema(cls, generic_args=arg_nodes, generic_module=module)
            class_confidence = self._confidence()
            # class resolution issues degrade the owner too
            self._worst = max((saved_worst, self._worst), key=lambda w: _LEVEL_RANK[w[0]])
            self.ctx.registry.intern(
                lang_type, schema, evidence, class_confidence, self.service_id
            )
        finally:
            self._in_progress.discard(pending_id)
        return {"$ref": REF_PREFIX + pending_id}

    def _file_of(self, node: nodes.NodeNG) -> str:
        try:
            path = node.root().file
            if path:
                return self.ctx.rel(path)
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    def _class_schema(
        self,
        cls: nodes.ClassDef,
        generic_args: list[nodes.NodeNG] | None = None,
        generic_module: nodes.Module | None = None,
    ) -> JsonSchemaDict:
        kind = classify_class(cls)
        if kind == "enum":
            return self._enum_schema(cls)
        if kind in ("pydantic", "dataclass", "attrs", "typeddict", "namedtuple", "plain"):
            return self._object_schema(cls, kind, generic_args, generic_module)
        self._degrade("low", "dynamic_type")
        return {}

    def _enum_schema(self, cls: nodes.ClassDef) -> JsonSchemaDict:
        values: list[Any] = []
        for statement in cls.body:
            if isinstance(statement, nodes.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, nodes.AssignName) and not target.name.startswith("_"):
                    value_node = statement.value
                    if isinstance(value_node, nodes.Call) and dotted_name(value_node.func) in (
                        "auto",
                        "enum.auto",
                    ):
                        values.append(len(values) + 1)
                        continue
                    ok, value = literal_value(value_node)
                    if ok:
                        values.append(value)
        if not values:
            self._degrade("medium", "inferred_return_flow")
            return {"type": "string"}
        schema: JsonSchemaDict = {"enum": values}
        if all(isinstance(v, str) for v in values):
            schema["type"] = "string"
        elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            schema["type"] = "integer"
        doc = _first_line(cls.doc_node.value) if cls.doc_node else None
        if doc:
            schema["description"] = doc
        return schema

    def _generic_binding(
        self,
        cls: nodes.ClassDef,
        generic_args: list[nodes.NodeNG] | None,
    ) -> dict[str, nodes.NodeNG]:
        """Map TypeVar names (Generic[T]) to instantiation argument nodes."""
        if not generic_args:
            return {}
        for base in cls.bases:
            if isinstance(base, nodes.Subscript):
                base_name = dotted_name(base.value) or ""
                if base_name.rsplit(".", 1)[-1] in ("Generic", "BaseModel"):
                    params = (
                        list(base.slice.elts)
                        if isinstance(base.slice, nodes.Tuple)
                        else [base.slice]
                    )
                    names = [dotted_name(p) for p in params]
                    return {
                        n: generic_args[i]
                        for i, n in enumerate(names)
                        if n and i < len(generic_args)
                    }
        return {}

    def _object_schema(
        self,
        cls: nodes.ClassDef,
        kind: str,
        generic_args: list[nodes.NodeNG] | None = None,
        generic_module: nodes.Module | None = None,
    ) -> JsonSchemaDict:
        properties: dict[str, JsonSchemaDict] = {}
        required: list[str] = []
        binding = self._generic_binding(cls, generic_args)

        klasses: list[nodes.ClassDef] = []
        try:
            for ancestor in reversed(list(cls.ancestors())):
                if classify_class(ancestor) in ("pydantic", "dataclass", "attrs", "typeddict", "plain") and (
                    ancestor.qname().split(".")[0]
                    not in ("pydantic", "builtins", "typing", "dataclasses", "enum")
                ):
                    klasses.append(ancestor)
        except Exception:  # noqa: BLE001 - unresolvable ancestors: fields of self still extracted
            self._degrade("medium", "unresolved_symbol")
        klasses.append(cls)

        attr_docs = self._attribute_docs(cls)

        for klass in klasses:
            module = klass.root()
            for statement in klass.body:
                if not isinstance(statement, nodes.AnnAssign):
                    continue
                target = statement.target
                if not isinstance(target, nodes.AssignName):
                    continue
                field_name = target.name
                if field_name.startswith("_") or field_name in ("model_config",):
                    continue
                annotation = statement.annotation
                # ClassVar fields are not part of the payload
                ann_base = dotted_name(
                    annotation.value if isinstance(annotation, nodes.Subscript) else annotation
                )
                if ann_base and ann_base.rsplit(".", 1)[-1] == "ClassVar":
                    continue
                # substitute TypeVars from generic instantiation
                subst_name = dotted_name(annotation)
                if subst_name in binding and generic_module is not None:
                    field_schema = self._convert(binding[subst_name], generic_module)
                else:
                    field_schema = self._convert_with_binding(
                        annotation, module, binding, generic_module
                    )

                is_required = statement.value is None
                property_name = field_name
                if statement.value is not None:
                    field_schema, property_name, is_required = self._apply_field_default(
                        statement.value, field_schema, field_name
                    )
                if kind == "typeddict":
                    is_required = not _is_total_false(cls)
                doc = attr_docs.get(field_name)
                if doc and "description" not in field_schema and "$ref" not in field_schema:
                    field_schema["description"] = doc
                properties[property_name] = field_schema
                if is_required:
                    required.append(property_name)

        schema: JsonSchemaDict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = sorted(required)
        if kind == "pydantic" and _forbids_extra(cls):
            schema["additionalProperties"] = False
        doc = _first_line(cls.doc_node.value) if cls.doc_node else None
        if doc:
            schema["description"] = doc
        if not properties:
            self._degrade("low", "dynamic_type")
            return {"type": "object", "additionalProperties": True}
        return schema

    def _convert_with_binding(
        self,
        annotation: nodes.NodeNG,
        module: nodes.Module,
        binding: dict[str, nodes.NodeNG],
        generic_module: nodes.Module | None,
    ) -> JsonSchemaDict:
        """Convert, substituting bound TypeVar names inside subscripts (List[T])."""
        if binding and isinstance(annotation, nodes.Subscript) and generic_module is not None:
            slice_node = annotation.slice
            inner = dotted_name(slice_node)
            if inner in binding:
                base = dotted_name(annotation.value) or ""
                short = base.rsplit(".", 1)[-1]
                item = self._convert(binding[inner], generic_module)
                if short in _SEQUENCE_NAMES:
                    return {"type": "array", "items": item}
                if short in _SET_NAMES:
                    return {"type": "array", "uniqueItems": True, "items": item}
                if short == "Optional":
                    return self._nullable(item)
        return self._convert(annotation, module)

    def _apply_field_default(
        self, value: nodes.NodeNG, schema: JsonSchemaDict, field_name: str
    ) -> tuple[JsonSchemaDict, str, bool]:
        """Handle ``= Field(...)`` / ``= default`` on a model field."""
        property_name = field_name
        required = False
        if isinstance(value, nodes.Call):
            func_name = (dotted_name(value.func) or "").rsplit(".", 1)[-1]
            if func_name in ("Field", "field", "attrib", "attr"):
                constraints, extra = parse_field_kwargs(value)
                if "$ref" in schema and constraints:
                    schema = {"allOf": [schema], **constraints}
                else:
                    schema.update(constraints)
                if extra.get("alias"):
                    property_name = extra["alias"]
                required = extra.get("required", False)
                if "default" in extra:
                    schema.setdefault("default", extra["default"])
                    required = False
                elif not extra.get("has_default_factory") and not extra.get(
                    "explicit_optional", False
                ):
                    # Field(...) with Ellipsis or no default => required
                    required = extra.get("required", True)
                return schema, property_name, required
        ok, default = literal_value(value)
        if ok:
            if "$ref" not in schema:
                schema.setdefault("default", default)
            return schema, property_name, False
        return schema, property_name, False

    def _attribute_docs(self, cls: nodes.ClassDef) -> dict[str, str]:
        """Field descriptions grounded in docstrings: griffe first, then the
        class docstring's Attributes section, then PEP-224 style literals."""
        docs: dict[str, str] = {}
        module_name = cls.root().name or ""
        package = module_name.split(".")[0] if module_name else ""
        if package:
            griffe_docs = self.ctx.griffe_docs(package)
            entry = griffe_docs.get(cls.qname())
            if entry:
                docs.update(entry.get("attrs", {}))
        if cls.doc_node:
            docs.update(_parse_attributes_section(cls.doc_node.value))
        # PEP-224 style: a string literal statement directly after an AnnAssign
        previous_field: str | None = None
        for statement in cls.body:
            if isinstance(statement, nodes.AnnAssign) and isinstance(
                statement.target, nodes.AssignName
            ):
                previous_field = statement.target.name
            elif (
                isinstance(statement, nodes.Expr)
                and isinstance(statement.value, nodes.Const)
                and isinstance(statement.value.value, str)
                and previous_field
            ):
                docs.setdefault(previous_field, _first_line(statement.value.value) or "")
                previous_field = None
            else:
                previous_field = None
        return {k: v for k, v in docs.items() if v}

    # -- symbol resolution ------------------------------------------------------

    def resolve_symbol(self, module: nodes.Module, dotted: str):
        """Resolve a (possibly dotted) name used in ``module`` to its definition
        node, following imports across repo files. Returns None if unresolved."""
        parts = dotted.split(".")
        head, rest = parts[0], parts[1:]
        try:
            assignments = module.getattr(head)
        except astroid.AttributeInferenceError:
            return None
        except Exception:  # noqa: BLE001
            return None
        for assignment in assignments:
            resolved = self._follow(assignment, head, rest, module)
            if resolved is not None:
                return resolved
        return None

    def _follow(self, assignment, head: str, rest: list[str], module: nodes.Module):
        if isinstance(assignment, (nodes.ClassDef, nodes.FunctionDef)):
            return self._descend(assignment, rest)
        if isinstance(assignment, nodes.ImportFrom):
            target_modname = self._resolve_import_from(assignment, module)
            if target_modname is None:
                return None
            real = assignment.real_name(head)
            target_module = self.ctx.module_by_name(target_modname)
            if target_module is None:
                # maybe importing a submodule: from app import models
                submodule = self.ctx.module_by_name(f"{target_modname}.{real}")
                if submodule is not None:
                    return self._descend_module(submodule, rest)
                return None
            resolved = self.resolve_symbol(target_module, ".".join([real] + rest))
            if resolved is not None:
                return resolved
            submodule = self.ctx.module_by_name(f"{target_modname}.{real}")
            if submodule is not None:
                return self._descend_module(submodule, rest)
            return None
        if isinstance(assignment, nodes.Import):
            for modname, alias in assignment.names:
                local = alias or modname.split(".")[0]
                if local == head:
                    full = modname if alias else modname.split(".")[0]
                    target_module = self.ctx.module_by_name(full)
                    if target_module is not None:
                        return self._descend_module(target_module, rest)
            return None
        if isinstance(assignment, nodes.AssignName):
            # alias assignment: X = SomeClass
            parent = assignment.parent
            if isinstance(parent, nodes.Assign):
                name = dotted_name(parent.value)
                if name and name != head:
                    return self.resolve_symbol(module, ".".join([name] + rest))
        return None

    @staticmethod
    def _resolve_import_from(node: nodes.ImportFrom, module: nodes.Module) -> str | None:
        if not node.level:
            return node.modname
        # relative import: resolve against the module's dotted name
        base_parts = (module.name or "").split(".")
        # package __init__ counts as its own package level
        if module.file and module.file.endswith("__init__.py"):
            base_parts = base_parts
        else:
            base_parts = base_parts[:-1]
        level = node.level
        if level - 1 > 0:
            base_parts = base_parts[: len(base_parts) - (level - 1)]
        if not base_parts:
            return node.modname or None
        return ".".join(base_parts + ([node.modname] if node.modname else []))

    def _descend(self, node, rest: list[str]):
        current = node
        for part in rest:
            if isinstance(current, nodes.ClassDef):
                try:
                    current = current.getattr(part)[0]
                except Exception:  # noqa: BLE001
                    return None
            else:
                return None
        return current

    def _descend_module(self, module: nodes.Module, rest: list[str]):
        if not rest:
            return module
        return self.resolve_symbol(module, ".".join(rest))


# ---------------------------------------------------------------------------
# helpers shared with adapters
# ---------------------------------------------------------------------------


def parse_field_kwargs(call: nodes.Call) -> tuple[JsonSchemaDict, dict[str, Any]]:
    """Extract JSON-Schema constraints + metadata from Field/Query/Path/... calls.

    Returns (constraints, extra) where extra may contain: default, alias,
    required, has_default_factory, discriminator, description, embed, media_type.
    """
    constraints: JsonSchemaDict = {}
    extra: dict[str, Any] = {}
    if call.args:
        first = call.args[0]
        if isinstance(first, nodes.Const) and first.value is Ellipsis:
            extra["required"] = True
        else:
            ok, value = literal_value(first)
            if ok:
                extra["default"] = value
    for keyword in call.keywords or []:
        if keyword.arg is None:
            continue
        if keyword.arg in _CONSTRAINT_KWARGS:
            ok, value = literal_value(keyword.value)
            if ok and value is not None:
                constraints[_CONSTRAINT_KWARGS[keyword.arg]] = value
        elif keyword.arg == "default":
            if isinstance(keyword.value, nodes.Const) and keyword.value.value is Ellipsis:
                extra["required"] = True
            else:
                ok, value = literal_value(keyword.value)
                if ok:
                    extra["default"] = value
        elif keyword.arg == "default_factory":
            extra["has_default_factory"] = True
        elif keyword.arg == "alias":
            ok, value = literal_value(keyword.value)
            if ok and isinstance(value, str):
                extra["alias"] = value
        elif keyword.arg == "discriminator":
            ok, value = literal_value(keyword.value)
            if ok:
                extra["discriminator"] = value
        elif keyword.arg == "embed":
            ok, value = literal_value(keyword.value)
            if ok:
                extra["embed"] = value
        elif keyword.arg == "media_type":
            ok, value = literal_value(keyword.value)
            if ok:
                extra["media_type"] = value
        elif keyword.arg == "examples":
            ok, value = literal_value(keyword.value)
            if ok:
                constraints["examples"] = value
        elif keyword.arg == "deprecated":
            ok, value = literal_value(keyword.value)
            if ok:
                extra["deprecated"] = value
    if "description" in constraints:
        extra["description"] = constraints["description"]
    return constraints, extra


def classify_class(cls: nodes.ClassDef) -> str:
    """enum | pydantic | dataclass | attrs | typeddict | namedtuple | plain."""
    base_names: set[str] = set()
    for base in cls.bases:
        name = dotted_name(base if not isinstance(base, nodes.Subscript) else base.value)
        if name:
            base_names.add(name.rsplit(".", 1)[-1])
    try:
        ancestor_qnames = {a.qname() for a in cls.ancestors()}
    except Exception:  # noqa: BLE001
        ancestor_qnames = set()

    if base_names & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"} or any(
        q.startswith("enum.") for q in ancestor_qnames
    ):
        return "enum"
    if base_names & {"BaseModel", "GenericModel"} or any(
        q in ("pydantic.main.BaseModel", "pydantic.BaseModel") for q in ancestor_qnames
    ):
        return "pydantic"
    if base_names & {"TypedDict"}:
        return "typeddict"
    if base_names & {"NamedTuple"}:
        return "namedtuple"
    for decorator in cls.decorators.nodes if cls.decorators else []:
        name = dotted_name(decorator if not isinstance(decorator, nodes.Call) else decorator.func) or ""
        short = name.rsplit(".", 1)[-1]
        if short == "dataclass":
            return "dataclass"
        if short in ("define", "frozen", "attrs", "attr_s", "s"):
            return "attrs"
    # heuristic: pydantic model imported under an unresolvable alias still has
    # annotated fields; treat any class with only AnnAssign fields as plain-annotated
    return "plain"


def _forbids_extra(cls: nodes.ClassDef) -> bool:
    for statement in cls.body:
        if isinstance(statement, nodes.Assign):
            targets = [t.name for t in statement.targets if isinstance(t, nodes.AssignName)]
            if "model_config" in targets and isinstance(statement.value, nodes.Call):
                for keyword in statement.value.keywords or []:
                    if keyword.arg == "extra":
                        ok, value = literal_value(keyword.value)
                        if ok and value == "forbid":
                            return True
        if isinstance(statement, nodes.ClassDef) and statement.name == "Config":
            for inner in statement.body:
                if isinstance(inner, nodes.Assign):
                    names = [t.name for t in inner.targets if isinstance(t, nodes.AssignName)]
                    if "extra" in names:
                        ok, value = literal_value(inner.value)
                        if ok and value == "forbid":
                            return True
    return False


def _is_total_false(cls: nodes.ClassDef) -> bool:
    for base in cls.bases:
        if isinstance(base, nodes.Call):
            for keyword in base.keywords or []:
                if keyword.arg == "total":
                    ok, value = literal_value(keyword.value)
                    if ok and value is False:
                        return True
    # class Foo(TypedDict, total=False) — astroid puts keywords on the ClassDef
    for keyword in getattr(cls, "keywords", None) or []:
        if keyword.arg == "total":
            ok, value = literal_value(keyword.value)
            if ok and value is False:
                return True
    return False


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _parse_attributes_section(docstring: str) -> dict[str, str]:
    """Google/NumPy/Sphinx attribute docs; uses docstring_parser when present."""
    try:
        import docstring_parser

        parsed = docstring_parser.parse(docstring)
        return {p.arg_name: (p.description or "").strip().splitlines()[0]
                for p in parsed.params if p.arg_name and p.description}
    except Exception:  # noqa: BLE001
        return {}
