"""Metadata schema registry → OpenAPI ``components.schemas``.

Responsibilities:
- assign stable, human-friendly component names (entry titles; the structural
  hash suffix is appended only on title collisions),
- collapse structurally identical entries (equal ``structural_hash``) into a
  single component,
- rewrite every ``#/schema_registry/schemas/<id>`` ref to
  ``#/components/schemas/<name>``,
- keep only schemas actually reachable from the service being emitted.
"""

from __future__ import annotations

import copy
from typing import Any

from openapi_agent.models.metadata import MetadataDocument, SchemaEntry
from openapi_agent.models.registry import REF_PREFIX

COMPONENTS_PREFIX = "#/components/schemas/"


class ComponentsRegistry:
    def __init__(self, document: MetadataDocument) -> None:
        self._entries = document.schema_registry.schemas
        self._name_by_id: dict[str, str] = {}
        self._schema_by_name: dict[str, dict] = {}
        self._assign_names()

    def _assign_names(self) -> None:
        # pass 1: assign every name (collapse identical structures: first id
        # in sorted order wins the short name; identical shape but different
        # domain names stay separate)
        canonical_by_hash: dict[str, str] = {}
        used_names: set[str] = set()
        rewrite_ids: list[str] = []
        for schema_id in sorted(self._entries):
            entry = self._entries[schema_id]
            if entry.structural_hash in canonical_by_hash:
                canonical_id = canonical_by_hash[entry.structural_hash]
                if self._entries[canonical_id].title == entry.title:
                    self._name_by_id[schema_id] = self._name_by_id[canonical_id]
                    continue
            name = _sanitize(entry.title)
            if name in used_names:
                name = f"{name}_{entry.structural_hash[:8]}"
            used_names.add(name)
            self._name_by_id[schema_id] = name
            canonical_by_hash.setdefault(entry.structural_hash, schema_id)
            rewrite_ids.append(schema_id)
        # pass 2: rewrite refs only after the full name map exists — otherwise
        # forward references would dangle and degrade to free-form schemas
        for schema_id in rewrite_ids:
            entry = self._entries[schema_id]
            name = self._name_by_id[schema_id]
            self._schema_by_name[name] = self._rewrite(copy.deepcopy(entry.json_schema))

    def _rewrite(self, node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(REF_PREFIX):
                target = ref[len(REF_PREFIX):]
                name = self._name_by_id.get(target)
                if name is not None:
                    node["$ref"] = COMPONENTS_PREFIX + name
                else:
                    # dangling ref must never reach the document: free-form fallback
                    node.pop("$ref")
                    node["additionalProperties"] = True
            return {k: self._rewrite(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._rewrite(item) for item in node]
        return node

    def rewrite_inline(self, schema: dict) -> dict:
        """Rewrite refs in an endpoint-inline schema fragment (copy)."""
        return self._rewrite(copy.deepcopy(schema))

    def reachable_components(self, roots: list[dict]) -> dict[str, dict]:
        """Component name -> schema for everything reachable from ``roots``
        (already-rewritten fragments)."""
        pending: list[str] = []

        def scan(node: Any) -> None:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith(COMPONENTS_PREFIX):
                    pending.append(ref[len(COMPONENTS_PREFIX):])
                for value in node.values():
                    scan(value)
            elif isinstance(node, list):
                for item in node:
                    scan(item)

        for root in roots:
            scan(root)
        included: dict[str, dict] = {}
        while pending:
            name = pending.pop()
            if name in included or name not in self._schema_by_name:
                continue
            included[name] = self._schema_by_name[name]
            scan(self._schema_by_name[name])
        return dict(sorted(included.items()))


def _sanitize(title: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in title)
    return cleaned or "Schema"
