"""Schema registry builder: deterministic IDs + SCC-based structural hashing.

Adapters intern JSON Schema fragments under *pending* ids (language-qualified
names, generics-instantiation aware). After analysis, :func:`finalize_document`
computes structural hashes — recursion-safe via strongly-connected components
of the ref graph — renames every registry key to ``<pending>--<hash8>``, and
rewrites every ``$ref`` in the document (registry entries and inline endpoint
schemas alike).

Ref format used everywhere in the metadata document:
    ``#/schema_registry/schemas/<schema_id>``
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterator

from openapi_agent.models.metadata import (
    Confidence,
    Evidence,
    JsonSchemaDict,
    LangTypeRef,
    MetadataDocument,
    SchemaEntry,
)

REF_PREFIX = "#/schema_registry/schemas/"

_ID_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def make_pending_id(lang_type: LangTypeRef) -> str:
    """``<lang>.<qualified_name>[__of__<arg>[__and__<arg>...]]`` (no hash yet)."""

    def flat(t: LangTypeRef) -> str:
        base = t.qualified_name
        if t.type_args:
            base += "__of__" + "__and__".join(flat(a) for a in t.type_args)
        return base

    raw = f"{lang_type.language[:2] if lang_type.language == 'python' else lang_type.language}.{flat(lang_type)}"
    # 'python' -> 'py', 'java' stays 'java'
    return _ID_SAFE.sub("_", raw)


def iter_refs(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(REF_PREFIX):
                yield value[len(REF_PREFIX):]
            else:
                yield from iter_refs(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from iter_refs(item)


def rewrite_refs(schema: Any, mapping: dict[str, str]) -> None:
    """In-place rename of pending ids to final ids inside a schema fragment."""
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith(REF_PREFIX):
            target = ref[len(REF_PREFIX):]
            if target in mapping:
                schema["$ref"] = REF_PREFIX + mapping[target]
        for value in schema.values():
            rewrite_refs(value, mapping)
    elif isinstance(schema, list):
        for item in schema:
            rewrite_refs(item, mapping)


def _canonical_fragment(schema: JsonSchemaDict) -> Any:
    """Copy stripped of descriptive noise; used for hashing and equality."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: strip(v)
                for k, v in node.items()
                if k not in ("title", "description", "examples", "example")
            }
        if isinstance(node, list):
            return [strip(i) for i in node]
        return node

    return strip(schema)


def _tarjan_scc(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative, deterministic (nodes visited in sorted order)."""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for succ in it:
                if succ not in index:
                    index[succ] = lowlink[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(sorted(edges.get(succ, ())))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                sccs.append(sorted(component))
    return sccs


def compute_structural_hashes(schemas: dict[str, JsonSchemaDict]) -> dict[str, str]:
    """16-hex structural hash per pending id; cycles handled via SCC grouping."""
    edges: dict[str, set[str]] = {
        pid: {t for t in iter_refs(schema) if t in schemas} for pid, schema in schemas.items()
    }
    sccs = _tarjan_scc(list(schemas), edges)

    scc_of: dict[str, int] = {}
    for i, scc in enumerate(sccs):
        for member in scc:
            scc_of[member] = i

    hashes: dict[str, str] = {}
    # Tarjan emits SCCs in reverse topological order: dependencies first.
    for scc in sccs:
        ordinal = {member: n for n, member in enumerate(scc)}

        def replace(node: Any) -> Any:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith(REF_PREFIX):
                    target = ref[len(REF_PREFIX):]
                    if target in ordinal:
                        return {"$ref": f"$scc:{ordinal[target]}"}
                    if target in hashes:
                        return {"$ref": f"$hash:{hashes[target]}"}
                    return {"$ref": "$external"}
                return {k: replace(v) for k, v in node.items()}
            if isinstance(node, list):
                return [replace(i) for i in node]
            return node

        payload = [replace(_canonical_fragment(schemas[m])) for m in scc]
        scc_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for member in scc:
            if len(scc) == 1:
                hashes[member] = scc_digest[:16]
            else:
                hashes[member] = hashlib.sha256(
                    f"{scc_digest}#{ordinal[member]}".encode("utf-8")
                ).hexdigest()[:16]
    return hashes


class SchemaRegistryBuilder:
    """Mutable accumulator used during Phase-1 analysis."""

    def __init__(self) -> None:
        self._entries: dict[str, SchemaEntry] = {}  # keyed by pending id

    def intern(
        self,
        lang_type: LangTypeRef | None,
        json_schema: JsonSchemaDict,
        evidence: list[Evidence],
        confidence: Confidence,
        service_id: str,
        title: str | None = None,
        synthetic_name: str | None = None,
    ) -> str:
        """Register a schema; returns a ``$ref`` string (pending id form).

        ``synthetic_name`` names anonymous shapes (no nominal type), e.g.
        ``py.app.routes.create_user.Body``.
        """
        if lang_type is not None:
            pending_id = make_pending_id(lang_type)
            default_title = lang_type.qualified_name.rsplit(".", 1)[-1]
            if lang_type.type_args:
                default_title += "_" + "_".join(
                    a.qualified_name.rsplit(".", 1)[-1] for a in lang_type.type_args
                )
        else:
            if not synthetic_name:
                raise ValueError("anonymous schemas require synthetic_name")
            pending_id = _ID_SAFE.sub("_", synthetic_name)
            default_title = synthetic_name.rsplit(".", 1)[-1]

        pending_id = self._disambiguate(pending_id, json_schema)
        if pending_id in self._entries:
            entry = self._entries[pending_id]
            if service_id not in entry.used_by_services:
                entry.used_by_services.append(service_id)
        else:
            self._entries[pending_id] = SchemaEntry(
                schema_id=pending_id,  # finalized later
                title=title or default_title,
                lang_type=lang_type,
                structural_hash="",  # finalized later
                json_schema=json_schema,
                used_by_services=[service_id],
                evidence=evidence,
                confidence=confidence,
            )
        return REF_PREFIX + pending_id

    def _disambiguate(self, pending_id: str, json_schema: JsonSchemaDict) -> str:
        """Same qname + same shape => same entry; different shape => variant key."""
        candidate = pending_id
        suffix = 1
        new_canon = _canonical_fragment(json_schema)
        while candidate in self._entries:
            existing = _canonical_fragment(self._entries[candidate].json_schema)
            if existing == new_canon:
                return candidate
            suffix += 1
            candidate = f"{pending_id}~{suffix}"
        return candidate

    def contains(self, ref_or_pending_id: str) -> bool:
        pid = ref_or_pending_id
        if pid.startswith(REF_PREFIX):
            pid = pid[len(REF_PREFIX):]
        return pid in self._entries

    @property
    def entries(self) -> dict[str, SchemaEntry]:
        return self._entries


def finalize_document(document: MetadataDocument, builder: SchemaRegistryBuilder) -> None:
    """Compute hashes, assign final ids, rewrite every $ref in the document."""
    pending_schemas = {pid: e.json_schema for pid, e in builder.entries.items()}
    hashes = compute_structural_hashes(pending_schemas)
    mapping = {pid: f"{pid}--{hashes[pid]}" for pid in pending_schemas}

    final_entries: dict[str, SchemaEntry] = {}
    for pid, entry in builder.entries.items():
        entry.schema_id = mapping[pid]
        entry.structural_hash = hashes[pid]
        rewrite_refs(entry.json_schema, mapping)
        final_entries[entry.schema_id] = entry
    document.schema_registry.schemas = final_entries

    for service in document.services:
        for endpoint in service.endpoints:
            for operation in endpoint.operations:
                for param in operation.parameters:
                    rewrite_refs(param.schema_, mapping)
                if operation.request_body is not None:
                    for contract in operation.request_body.content.values():
                        rewrite_refs(contract.schema_, mapping)
                for response in operation.responses:
                    for contract in response.content.values():
                        rewrite_refs(contract.schema_, mapping)
                    for header in response.headers.values():
                        rewrite_refs(header.schema_, mapping)
