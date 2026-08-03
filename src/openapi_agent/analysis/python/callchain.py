"""Bounded-depth call-chain analysis for Python handlers.

Follows calls from a handler into same-repo functions (services, helpers,
shared response builders) to collect exception raise sites that translate to
HTTP error responses. Depth-limited (config ``call_graph_max_depth``) and
cycle-safe; never leaves the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from astroid import nodes

from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import DependencyEdge, Evidence

log = get_logger("analysis.python.callchain")


@dataclass(frozen=True)
class RaiseSite:
    exception_qname: str  # e.g. "fastapi.HTTPException" or "app.errors.NotFound"
    exception_short: str
    status_code: int | None  # literal status when present in the constructor
    detail_is_str: bool
    call_kwargs: dict  # literal kwargs of the constructor (status_code, detail, headers)
    evidence: Evidence
    depth: int


def _literal_kwargs(call: nodes.Call) -> dict:
    from openapi_agent.analysis.python.type_schema import literal_value

    out: dict = {}
    for i, arg in enumerate(call.args):
        ok, value = literal_value(arg)
        if ok:
            out[f"__arg{i}"] = value
    for keyword in call.keywords or []:
        if keyword.arg:
            ok, value = literal_value(keyword.value)
            if ok:
                out[keyword.arg] = value
    return out


def collect_raise_sites(
    func: nodes.FunctionDef,
    converter,  # PyTypeSchemaConverter (for resolve_symbol)
    max_depth: int,
    rel_of,  # callable: abs path -> repo-relative
    dependency_edges: list[DependencyEdge] | None = None,
) -> list[RaiseSite]:
    """All raise sites reachable from ``func`` within ``max_depth`` calls."""
    sites: list[RaiseSite] = []
    visited: set[str] = set()

    def visit(fn: nodes.FunctionDef, depth: int) -> None:
        try:
            key = fn.qname()
        except Exception:  # noqa: BLE001
            return
        if key in visited:
            return
        visited.add(key)
        module = fn.root()

        for node in fn.nodes_of_class(nodes.Raise):
            exc = node.exc
            if isinstance(exc, nodes.Call):
                name = _resolve_exception_name(exc.func, module, converter)
                if name is None:
                    continue
                kwargs = _literal_kwargs(exc)
                status = kwargs.get("status_code")
                if status is None and "__arg0" in kwargs and isinstance(kwargs["__arg0"], int):
                    status = kwargs["__arg0"]
                sites.append(
                    RaiseSite(
                        exception_qname=name,
                        exception_short=name.rsplit(".", 1)[-1],
                        status_code=status if isinstance(status, int) else None,
                        detail_is_str=isinstance(kwargs.get("detail"), str),
                        call_kwargs=kwargs,
                        evidence=Evidence(
                            file=_file_rel(node, rel_of),
                            start_line=node.lineno or 1,
                            end_line=node.end_lineno or node.lineno or 1,
                            kind="raise_stmt",
                            symbol=key,
                        ),
                        depth=depth,
                    )
                )
            elif isinstance(exc, (nodes.Name, nodes.Attribute)):
                name = _resolve_exception_name(exc, module, converter)
                if name:
                    sites.append(
                        RaiseSite(
                            exception_qname=name,
                            exception_short=name.rsplit(".", 1)[-1],
                            status_code=None,
                            detail_is_str=False,
                            call_kwargs={},
                            evidence=Evidence(
                                file=_file_rel(node, rel_of),
                                start_line=node.lineno or 1,
                                end_line=node.end_lineno or node.lineno or 1,
                                kind="raise_stmt",
                                symbol=key,
                            ),
                            depth=depth,
                        )
                    )

        if depth >= max_depth:
            return
        for call in fn.nodes_of_class(nodes.Call):
            from openapi_agent.analysis.python.type_schema import dotted_name

            name = dotted_name(call.func)
            if not name:
                continue
            target = converter.resolve_symbol(module, name)
            if isinstance(target, nodes.FunctionDef):
                target_file = _file_rel(target, rel_of)
                if target_file == "unknown":
                    continue
                if dependency_edges is not None:
                    source_file = _file_rel(fn, rel_of)
                    if source_file != target_file and source_file != "unknown":
                        dependency_edges.append(
                            DependencyEdge(from_file=source_file, to_file=target_file, kind="call")
                        )
                visit(target, depth + 1)

    visit(func, 0)
    return sites


def _resolve_exception_name(func_node, module, converter) -> str | None:
    from openapi_agent.analysis.python.type_schema import dotted_name

    name = dotted_name(func_node)
    if not name:
        return None
    short = name.rsplit(".", 1)[-1]
    if short in ("HTTPException", "WebSocketException"):
        return f"fastapi.{short}" if short == "HTTPException" else short
    resolved = converter.resolve_symbol(module, name)
    if isinstance(resolved, nodes.ClassDef):
        try:
            return resolved.qname()
        except Exception:  # noqa: BLE001
            return name
    return name


def _file_rel(node, rel_of) -> str:
    try:
        path = node.root().file
        return rel_of(path) if path else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"
