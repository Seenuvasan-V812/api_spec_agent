"""Python analysis context: astroid + libcst + tree-sitter + griffe + grimp.

- tree-sitter: error-tolerant candidate index (built eagerly, cheap)
- astroid: cross-module name resolution / inference (modules parsed lazily)
- libcst: lossless CST for precise spans & docstring extraction (lazy cache)
- griffe: docstring-grounded model/field descriptions (best-effort)
- grimp: import graph for dependency edges (best-effort fallback to astroid)

Nothing here executes target code: astroid/libcst/griffe/grimp all operate on
source text only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from openapi_agent.analysis.base import AnalysisContext
from openapi_agent.analysis.python.ts_scanner import PyFileIndex, scan_repository
from openapi_agent.logging_utils import get_logger

log = get_logger("analysis.python.context")


def module_name_for(rel_path: str) -> str:
    """Dotted module name for a repo-relative path (best-effort)."""
    parts = rel_path[:-3].split("/") if rel_path.endswith(".py") else rel_path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class PythonAnalysisContext(AnalysisContext):
    ts_index: dict[str, PyFileIndex] = field(default_factory=dict)
    _astroid_cache: dict = field(default_factory=dict)
    _cst_cache: dict = field(default_factory=dict)
    _module_by_name: dict = field(default_factory=dict)
    _griffe_docs: dict = field(default_factory=dict)
    _import_graph=None
    _search_paths: list[str] = field(default_factory=list)

    # -- astroid -----------------------------------------------------------
    def astroid_module(self, rel_path: str):
        """Parse (never import) a module with astroid; None on failure."""
        import astroid

        if rel_path in self._astroid_cache:
            return self._astroid_cache[rel_path]
        abs_path = self.repo_root / rel_path
        modname = module_name_for(rel_path)
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            module = astroid.parse(source, module_name=modname, path=str(abs_path))
        except Exception as exc:  # noqa: BLE001 - single-file failure must not sink analysis
            self.warnings.parse_failure(rel_path, str(exc))
            module = None
        self._astroid_cache[rel_path] = module
        if module is not None:
            self._module_by_name[modname] = module
        return module

    def module_by_name(self, modname: str):
        """Resolve a dotted module name to a parsed astroid module within the repo."""
        if modname in self._module_by_name:
            return self._module_by_name[modname]
        candidates = [
            modname.replace(".", "/") + ".py",
            modname.replace(".", "/") + "/__init__.py",
        ]
        for search_root in [""] + self._search_paths:
            for candidate in candidates:
                rel = f"{search_root}/{candidate}".lstrip("/")
                if (self.repo_root / rel).is_file():
                    module = self.astroid_module(rel)
                    if module is not None:
                        self._module_by_name[modname] = module
                        return module
        self._module_by_name[modname] = None
        return None

    # -- libcst ------------------------------------------------------------
    def cst_module(self, rel_path: str):
        import libcst as cst

        if rel_path in self._cst_cache:
            return self._cst_cache[rel_path]
        try:
            source = (self.repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
            wrapper = cst.MetadataWrapper(cst.parse_module(source))
        except Exception as exc:  # noqa: BLE001
            self.warnings.parse_failure(rel_path, f"libcst: {exc}")
            wrapper = None
        self._cst_cache[rel_path] = wrapper
        return wrapper

    # -- griffe docstrings ---------------------------------------------------
    def griffe_docs(self, package: str) -> dict:
        """qualified class name -> {"doc": str, "attrs": {name: doc}}; best-effort."""
        if package in self._griffe_docs:
            return self._griffe_docs[package]
        docs: dict = {}
        try:
            import griffe

            loaded = griffe.load(
                package,
                search_paths=[str(self.repo_root)] + [str(self.repo_root / p) for p in self._search_paths],
                allow_inspection=False,  # static only — never import target code
            )
            self._collect_griffe_docs(loaded, docs)
        except Exception as exc:  # noqa: BLE001
            log.debug("griffe load failed for %s: %s", package, exc)
        self._griffe_docs[package] = docs
        return docs

    @staticmethod
    def _collect_griffe_docs(obj, docs: dict) -> None:
        try:
            members = getattr(obj, "members", {}) or {}
        except Exception:  # noqa: BLE001
            return
        for member in members.values():
            try:
                if member.is_alias:
                    continue
                if member.kind.value == "class":
                    attrs: dict[str, str] = {}
                    for attr_name, attr in (member.members or {}).items():
                        doc = getattr(getattr(attr, "docstring", None), "value", None)
                        if doc:
                            attrs[attr_name] = doc.strip().splitlines()[0]
                    docs[member.path] = {
                        "doc": getattr(getattr(member, "docstring", None), "value", None),
                        "attrs": attrs,
                    }
                PythonAnalysisContext._collect_griffe_docs(member, docs)
            except Exception:  # noqa: BLE001
                continue

    # -- grimp import graph --------------------------------------------------
    def import_graph(self, package: str):
        """grimp ImportGraph for a top-level package; None if unbuildable."""
        if self._import_graph is not None:
            return self._import_graph
        added = []
        for path in [str(self.repo_root)] + [str(self.repo_root / p) for p in self._search_paths]:
            if path not in sys.path:
                sys.path.insert(0, path)
                added.append(path)
        try:
            import grimp

            self._import_graph = grimp.build_graph(package, include_external_packages=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("grimp graph failed for %s: %s", package, exc)
            self._import_graph = None
        finally:
            for path in added:
                if path in sys.path:
                    sys.path.remove(path)
        return self._import_graph


def build_python_context(base: AnalysisContext) -> PythonAnalysisContext:
    ctx = PythonAnalysisContext(
        repo_root=base.repo_root,
        repo_facts=base.repo_facts,
        config=base.config,
        warnings=base.warnings,
        registry=base.registry,
        extras=base.extras,
    )
    # source roots: repo root plus conventional src/ layouts
    search_paths = []
    for candidate in ("src", "app", "apps"):
        if (base.repo_root / candidate).is_dir():
            search_paths.append(candidate)
    ctx._search_paths = search_paths
    ctx.ts_index = scan_repository(base.repo_root, base.repo_facts.python_files)
    return ctx
