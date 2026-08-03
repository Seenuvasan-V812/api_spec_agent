"""FastAPI framework adapter.

Handles: multiple ``FastAPI()`` apps, nested ``APIRouter`` include graphs with
prefix/tag/dependency composition, path/query/header/cookie/body/form/file
parameters (positional markers and ``Annotated`` style), Pydantic request and
response models, ``responses={...}`` maps, ``response_class`` variants
(file/stream/redirect/plain/html), ``HTTPException`` raise sites through a
bounded call chain, custom ``@app.exception_handler`` mappings, and security
via ``Depends``/``Security`` on routes, routers, and apps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from astroid import nodes

from openapi_agent.analysis.base import (
    AnalysisContext,
    DetectionResult,
    FrameworkAdapter,
    OperationExtraction,
    RouteRef,
    UnresolvedSite,
)
from openapi_agent.analysis.python.callchain import collect_raise_sites
from openapi_agent.analysis.python.context import PythonAnalysisContext, module_name_for
from openapi_agent.analysis.python.type_schema import (
    PyTypeSchemaConverter,
    classify_class,
    dotted_name,
    literal_value,
    parse_field_kwargs,
)
from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Condition,
    Confidence,
    DependencyEdge,
    Evidence,
    HeaderSpec,
    LangTypeRef,
    MediaTypeContract,
    Operation,
    Parameter,
    RequestBody,
    ResponseVariant,
    SecurityEvidence,
    SecuritySchemeDecl,
    Service,
)

log = get_logger("analysis.python.fastapi")

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")
_STATUS_ATTR_RE = re.compile(r"HTTP_(\d{3})_")

_SKIP_PARAM_TYPES = {
    "Request",
    "Response",
    "WebSocket",
    "BackgroundTasks",
    "SecurityScopes",
    "State",
}

_SECURITY_CLASSES = {
    "OAuth2PasswordBearer": ("oauth2", "password"),
    "OAuth2AuthorizationCodeBearer": ("oauth2", "authorizationCode"),
    "OAuth2ClientCredentials": ("oauth2", "clientCredentials"),
    "HTTPBearer": ("http_bearer", None),
    "HTTPBasic": ("http_basic", None),
    "HTTPDigest": ("custom", None),
    "APIKeyHeader": ("apikey_header", None),
    "APIKeyQuery": ("apikey_query", None),
    "APIKeyCookie": ("apikey_cookie", None),
    "OpenIdConnect": ("openid_connect", None),
}

_RESPONSE_CLASSES = {
    "JSONResponse": ("application/json", None),
    "ORJSONResponse": ("application/json", None),
    "UJSONResponse": ("application/json", None),
    "PlainTextResponse": ("text/plain", {"type": "string"}),
    "HTMLResponse": ("text/html", {"type": "string"}),
    "FileResponse": ("application/octet-stream", {"type": "string", "format": "binary"}),
    "StreamingResponse": ("application/octet-stream", {"type": "string", "format": "binary"}),
    "RedirectResponse": (None, None),
    "Response": (None, None),
}


@dataclass
class RouterInfo:
    key: tuple[str, str]  # (module name, variable name)
    prefix: str = ""
    tags: list[str] = dc_field(default_factory=list)
    dependency_nodes: list[nodes.NodeNG] = dc_field(default_factory=list)
    file: str = ""
    line: int = 1
    is_app: bool = False


@dataclass
class IncludeEdge:
    parent: tuple[str, str]
    child: tuple[str, str]
    prefix: str = ""
    tags: list[str] = dc_field(default_factory=list)
    dependency_nodes: list[nodes.NodeNG] = dc_field(default_factory=list)
    file: str = ""


@dataclass
class ExcHandlerInfo:
    exception_qname: str
    status_code: int | None
    file: str
    line: int


@dataclass
class ServiceState:
    service_id: str
    app_key: tuple[str, str]
    file: str
    routers: dict[tuple[str, str], RouterInfo] = dc_field(default_factory=dict)
    includes: list[IncludeEdge] = dc_field(default_factory=list)
    exception_handlers: dict[str, ExcHandlerInfo] = dc_field(default_factory=dict)
    route_details: dict[tuple[str, int, str], dict] = dc_field(default_factory=dict)


class FastAPIAdapter(FrameworkAdapter):
    name = "fastapi"
    language = "python"

    # ------------------------------------------------------------------ scan
    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        score = 0.0
        rationale = []
        if "fastapi" in facts.manifest_dep_names():
            score += 0.6
            rationale.append("fastapi in manifest")
        hits = facts.import_hits.get("fastapi", [])
        if hits:
            score += 0.35
            rationale.append(f"fastapi imported in {len(hits)} files")
        return DetectionResult(score=min(score, 0.95), rationale="; ".join(rationale))

    # -------------------------------------------------------------- services
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        assert isinstance(ctx, PythonAnalysisContext)
        services: list[Service] = []
        states: dict[str, ServiceState] = {}
        used_ids: set[str] = set()
        fastapi_files = ctx.repo_facts.import_hits.get("fastapi", [])

        for rel in fastapi_files:
            module = ctx.astroid_module(rel)
            if module is None:
                continue
            for assign in module.nodes_of_class(nodes.Assign):
                call = assign.value
                if not isinstance(call, nodes.Call):
                    continue
                callee = (dotted_name(call.func) or "").rsplit(".", 1)[-1]
                if callee != "FastAPI" or len(assign.targets) != 1:
                    continue
                target = assign.targets[0]
                if not isinstance(target, nodes.AssignName):
                    continue
                kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
                title = _lit_str(kwargs.get("title"))
                version = _lit_str(kwargs.get("version"))
                description = _lit_str(kwargs.get("description"))
                root_path = _lit_str(kwargs.get("root_path")) or ""
                service_id = _service_id_for(rel, used_ids)
                used_ids.add(service_id)
                service = Service(
                    id=service_id,
                    name=title or service_id,
                    language="python",
                    framework="fastapi",
                    framework_version=ctx.repo_facts.dep_version("fastapi"),
                    build_system=_build_system(ctx.repo_facts),
                    root_path=rel.rsplit("/", 1)[0] if "/" in rel else "",
                    base_paths=[root_path] if root_path else [],
                    description_hint=description or version and f"version {version}" or None,
                )
                services.append(service)
                states[service_id] = ServiceState(
                    service_id=service_id,
                    app_key=(module.name, target.name),
                    file=rel,
                )
        ctx.extras["fastapi_states"] = states
        if not services:
            ctx.warnings.emit("W201", "fastapi imported but no FastAPI() application found")
        return services

    # ---------------------------------------------------------------- routes
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: ServiceState = ctx.extras["fastapi_states"][service.id]
        converter = PyTypeSchemaConverter(ctx, service.id)
        ctx.extras.setdefault("fastapi_converters", {})[service.id] = converter

        self._build_router_graph(ctx, state, converter)
        self._collect_exception_handlers(ctx, state, converter)

        routes: list[RouteRef] = []
        # BFS over include graph from the app node
        app_info = state.routers.get(state.app_key)
        if app_info is None:
            app_info = RouterInfo(key=state.app_key, is_app=True, file=state.file)
            state.routers[state.app_key] = app_info
        queue: list[tuple[tuple[str, str], str, list[str], list[nodes.NodeNG]]] = [
            (state.app_key, "", [], list(app_info.dependency_nodes))
        ]
        seen: set[tuple[tuple[str, str], str]] = set()
        while queue:
            key, prefix, tags, dependency_nodes = queue.pop(0)
            if (key, prefix) in seen:
                continue
            seen.add((key, prefix))
            router = state.routers.get(key)
            if router is None:
                continue
            routes.extend(
                self._routes_on_router(ctx, state, service, key, prefix, tags, dependency_nodes)
            )
            for edge in state.includes:
                if edge.parent == key:
                    child = state.routers.get(edge.child)
                    child_prefix = prefix + edge.prefix + (child.prefix if child else "")
                    child_tags = tags + edge.tags + (child.tags if child else [])
                    child_dependencies = (
                        dependency_nodes
                        + edge.dependency_nodes
                        + (child.dependency_nodes if child else [])
                    )
                    queue.append((edge.child, child_prefix, child_tags, child_dependencies))
        return routes

    def _build_router_graph(
        self, ctx: PythonAnalysisContext, state: ServiceState, converter: PyTypeSchemaConverter
    ) -> None:
        fastapi_files = ctx.repo_facts.import_hits.get("fastapi", [])
        for rel in fastapi_files:
            module = ctx.astroid_module(rel)
            if module is None:
                continue
            for assign in module.nodes_of_class(nodes.Assign):
                call = assign.value
                if not isinstance(call, nodes.Call) or len(assign.targets) != 1:
                    continue
                target = assign.targets[0]
                if not isinstance(target, nodes.AssignName):
                    continue
                callee = (dotted_name(call.func) or "").rsplit(".", 1)[-1]
                if callee not in ("APIRouter", "FastAPI"):
                    continue
                kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
                info = RouterInfo(
                    key=(module.name, target.name),
                    prefix=_lit_str(kwargs.get("prefix")) or "",
                    tags=_lit_str_list(kwargs.get("tags")),
                    dependency_nodes=_dependency_items(kwargs.get("dependencies")),
                    file=rel,
                    line=assign.lineno or 1,
                    is_app=callee == "FastAPI",
                )
                state.routers.setdefault(info.key, info)
            for call in module.nodes_of_class(nodes.Call):
                func = call.func
                if not isinstance(func, nodes.Attribute) or func.attrname != "include_router":
                    continue
                parent_key = self._resolve_var_key(ctx, module, func.expr, state, converter)
                if parent_key is None:
                    ctx.warnings.emit(
                        "W202",
                        "include_router owner could not be resolved",
                        file=rel,
                        start_line=call.lineno,
                        service_id=state.service_id,
                    )
                    continue
                if not call.args:
                    continue
                child_key = self._resolve_var_key(ctx, module, call.args[0], state, converter)
                if child_key is None:
                    ctx.warnings.emit(
                        "W202",
                        "included router could not be resolved",
                        file=rel,
                        start_line=call.lineno,
                        service_id=state.service_id,
                    )
                    continue
                kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
                state.includes.append(
                    IncludeEdge(
                        parent=parent_key,
                        child=child_key,
                        prefix=_lit_str(kwargs.get("prefix")) or "",
                        tags=_lit_str_list(kwargs.get("tags")),
                        dependency_nodes=_dependency_items(kwargs.get("dependencies")),
                        file=rel,
                    )
                )

    def _resolve_var_key(
        self,
        ctx: PythonAnalysisContext,
        module: nodes.Module,
        expr: nodes.NodeNG,
        state: ServiceState,
        converter: PyTypeSchemaConverter,
    ) -> tuple[str, str] | None:
        """Resolve app/router expression to its defining (module, var) key."""
        if isinstance(expr, nodes.Name):
            if (module.name, expr.name) in state.routers:
                return (module.name, expr.name)
            try:
                assignments = module.getattr(expr.name)
            except Exception:  # noqa: BLE001
                return None
            for assignment in assignments:
                if isinstance(assignment, nodes.AssignName):
                    return (module.name, expr.name)
                if isinstance(assignment, nodes.ImportFrom):
                    modname = converter._resolve_import_from(assignment, module)
                    if modname is None:
                        continue
                    real = assignment.real_name(expr.name)
                    target_module = ctx.module_by_name(modname)
                    if target_module is not None and real in target_module.locals:
                        return (target_module.name, real)
                    submodule = ctx.module_by_name(f"{modname}.{real}")
                    if submodule is not None:
                        return None  # module itself, caller uses Attribute form
            return None
        if isinstance(expr, nodes.Attribute):
            base = dotted_name(expr.expr)
            if base is None:
                return None
            target_module = self._module_from_name(ctx, module, base, converter)
            if target_module is not None:
                return (target_module.name, expr.attrname)
        return None

    @staticmethod
    def _module_from_name(
        ctx: PythonAnalysisContext,
        module: nodes.Module,
        base: str,
        converter: PyTypeSchemaConverter,
    ):
        head = base.split(".")[0]
        try:
            assignments = module.getattr(head)
        except Exception:  # noqa: BLE001
            return None
        for assignment in assignments:
            if isinstance(assignment, nodes.ImportFrom):
                modname = converter._resolve_import_from(assignment, module)
                if modname:
                    real = assignment.real_name(head)
                    candidate = ctx.module_by_name(f"{modname}.{real}")
                    if candidate is not None:
                        return candidate
            elif isinstance(assignment, nodes.Import):
                for name, alias in assignment.names:
                    if (alias or name.split(".")[0]) == head:
                        return ctx.module_by_name(name)
        return None

    def _routes_on_router(
        self,
        ctx: PythonAnalysisContext,
        state: ServiceState,
        service: Service,
        key: tuple[str, str],
        prefix: str,
        tags: list[str],
        dependency_nodes: list[nodes.NodeNG],
    ) -> list[RouteRef]:
        modname, varname = key
        module = ctx.module_by_name(modname)
        if module is None:
            return []
        rel = ctx.rel(module.file) if module.file else "unknown"
        refs: list[RouteRef] = []
        for func in module.nodes_of_class(nodes.FunctionDef):
            if not func.decorators:
                continue
            for decorator in func.decorators.nodes:
                if not isinstance(decorator, nodes.Call):
                    continue
                dfunc = decorator.func
                if not isinstance(dfunc, nodes.Attribute):
                    continue
                owner = dfunc.expr
                owner_name = dotted_name(owner)
                if owner_name != varname and not (
                    isinstance(owner, nodes.Attribute) and owner.attrname == varname
                ):
                    continue
                attr = dfunc.attrname
                if attr in _HTTP_METHODS:
                    methods = [attr]
                elif attr == "api_route":
                    kwargs = {k.arg: k.value for k in decorator.keywords or [] if k.arg}
                    methods = [m.lower() for m in _lit_str_list(kwargs.get("methods"))] or ["get"]
                elif attr == "websocket":
                    ctx.warnings.emit(
                        "W203",
                        "websocket route skipped (not representable in OpenAPI paths)",
                        file=rel,
                        start_line=decorator.lineno,
                        service_id=service.id,
                    )
                    continue
                else:
                    continue
                raw_path = ""
                if decorator.args:
                    raw = _lit_str(decorator.args[0])
                    if raw is None:
                        ctx.warnings.emit(
                            "W204",
                            "route path is not a literal string; endpoint skipped",
                            file=rel,
                            start_line=decorator.lineno,
                            service_id=service.id,
                        )
                        continue
                    raw_path = raw
                full_path = _join_path(prefix, raw_path)
                detail_key = (rel, decorator.lineno or 1, ",".join(methods))
                state.route_details[detail_key] = {
                    "func": func,
                    "decorator": decorator,
                    "tags": tags,
                    "dependency_nodes": dependency_nodes,
                    "module": module,
                }
                refs.append(
                    RouteRef(
                        service_hint=service.id,
                        raw_path=full_path,
                        methods=methods,
                        handler_symbol=f"{modname}.{func.name}",
                        file=rel,
                        start_line=decorator.lineno or 1,
                        kind="decorator",
                    )
                )
                if state.file != rel:
                    service.dependencies.append(
                        DependencyEdge(from_file=state.file, to_file=rel, kind="include_router")
                    )
        return refs

    def _collect_exception_handlers(
        self, ctx: PythonAnalysisContext, state: ServiceState, converter: PyTypeSchemaConverter
    ) -> None:
        for rel in ctx.repo_facts.import_hits.get("fastapi", []):
            module = ctx.astroid_module(rel)
            if module is None:
                continue
            for func in module.nodes_of_class(nodes.FunctionDef):
                if not func.decorators:
                    continue
                for decorator in func.decorators.nodes:
                    if not isinstance(decorator, nodes.Call):
                        continue
                    dfunc = decorator.func
                    if (
                        isinstance(dfunc, nodes.Attribute)
                        and dfunc.attrname == "exception_handler"
                        and decorator.args
                    ):
                        exc_name = dotted_name(decorator.args[0])
                        if not exc_name:
                            continue
                        resolved = converter.resolve_symbol(module, exc_name)
                        qname = (
                            resolved.qname()
                            if isinstance(resolved, nodes.ClassDef)
                            else exc_name
                        )
                        status = _handler_status(func)
                        state.exception_handlers[qname] = ExcHandlerInfo(
                            exception_qname=qname,
                            status_code=status,
                            file=rel,
                            line=func.lineno or 1,
                        )

    # ------------------------------------------------------------ extraction
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: ServiceState = ctx.extras["fastapi_states"][service.id]
        converter: PyTypeSchemaConverter = ctx.extras["fastapi_converters"][service.id]
        detail_key = (route.file, route.start_line, ",".join(route.methods))
        details = state.route_details.get(detail_key)
        if details is None:
            return []
        func: nodes.FunctionDef = details["func"]
        decorator: nodes.Call = details["decorator"]
        module: nodes.Module = details["module"]
        kwargs = {k.arg: k.value for k in decorator.keywords or [] if k.arg}

        normalized_path = _normalize_path(route.raw_path)
        path_param_names = set(_PATH_PARAM_RE.findall(route.raw_path))

        extractions: list[OperationExtraction] = []
        for method in route.methods:
            unresolved: list[UnresolvedSite] = []
            evidence = [
                Evidence(
                    file=route.file,
                    start_line=route.start_line,
                    end_line=func.end_lineno or route.start_line,
                    kind="decorator",
                    symbol=route.handler_symbol,
                )
            ]

            parameters, request_body, security, param_notes = self._signature_contract(
                ctx, service, converter, func, module, path_param_names, route, method, unresolved
            )
            # router/app level dependencies contribute security
            for dependency_node in details["dependency_nodes"]:
                sec = self._security_from_dependency(
                    ctx, service, converter, module, dependency_node, route
                )
                if sec is not None:
                    security.append(sec)

            responses = self._responses(
                ctx,
                service,
                converter,
                func,
                module,
                kwargs,
                route,
                method,
                has_validated_input=bool(parameters or request_body),
                state=state,
                unresolved=unresolved,
                normalized_path=normalized_path,
            )

            tags = details["tags"] + _lit_str_list(kwargs.get("tags"))
            summary = _lit_str(kwargs.get("summary"))
            description = _lit_str(kwargs.get("description"))
            doc = func.doc_node.value.strip() if func.doc_node else None
            if doc and not summary:
                summary = doc.splitlines()[0].strip()
            if doc and not description:
                body_lines = doc.splitlines()[1:]
                description = "\n".join(line.strip() for line in body_lines).strip() or None

            explicit_id = _lit_str(kwargs.get("operation_id"))
            operation_id = explicit_id or f"{service.id}.{route.handler_symbol}.{method}"
            deprecated_node = kwargs.get("deprecated")
            deprecated_ok, deprecated_value = literal_value(deprecated_node) if deprecated_node else (False, None)

            operation = Operation(
                method=method,  # type: ignore[arg-type]
                operation_id=operation_id,
                handler=route.handler_symbol,
                parameters=parameters,
                request_body=request_body,
                responses=responses,
                security=_dedupe_security(security),
                deprecated=deprecated_value if deprecated_ok and isinstance(deprecated_value, bool) else None,
                tags_hint=list(dict.fromkeys(t for t in tags if t)),
                summary_hint=summary,
                description_hint=description,
                evidence=evidence,
                confidence=Confidence(level="high", reason_code="declared_annotation"),
            )
            extractions.append(
                OperationExtraction(
                    endpoint_path=normalized_path,
                    raw_path=route.raw_path,
                    operation=operation,
                    unresolved=unresolved,
                )
            )
        return extractions

    # -- signature ------------------------------------------------------------

    def _signature_contract(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        func: nodes.FunctionDef,
        module: nodes.Module,
        path_param_names: set[str],
        route: RouteRef,
        method: str,
        unresolved: list[UnresolvedSite],
        depth: int = 0,
    ) -> tuple[list[Parameter], RequestBody | None, list[SecurityEvidence], dict]:
        parameters: list[Parameter] = []
        security: list[SecurityEvidence] = []
        json_parts: list[tuple[str, dict, bool, bool]] = []  # name, schema, required, embed
        form_parts: list[tuple[str, dict, bool]] = []
        file_parts: list[tuple[str, dict, bool]] = []
        media_type_override: str | None = None

        sig_evidence = Evidence(
            file=route.file,
            start_line=func.lineno or 1,
            end_line=(func.args.end_lineno or func.lineno or 1),
            kind="signature",
            symbol=f"{module.name}.{func.name}",
        )

        for param_name, annotation, default in _iter_params(func):
            if param_name in ("self", "cls"):
                continue
            ann_short = _short_of(annotation)
            if ann_short in _SKIP_PARAM_TYPES:
                continue

            marker_call, ann_core = _extract_marker(annotation, default)
            marker_name = (
                (dotted_name(marker_call.func) or "").rsplit(".", 1)[-1] if marker_call else None
            )

            # Depends / Security
            if marker_name in ("Depends", "Security"):
                sec = self._security_from_dependency(
                    ctx, service, converter, module, marker_call, route,
                    scopes=_security_scopes(marker_call),
                )
                if sec is not None:
                    security.append(sec)
                elif depth < 1 and marker_call.args:
                    target = converter.resolve_symbol(
                        module, dotted_name(marker_call.args[0]) or ""
                    )
                    if isinstance(target, nodes.FunctionDef):
                        sub_params, _, sub_security, _ = self._signature_contract(
                            ctx, service, converter, target, target.root(),
                            path_param_names, route, method, unresolved, depth + 1,
                        )
                        parameters.extend(sub_params)
                        security.extend(sub_security)
                continue

            schema, confidence = converter.convert(ann_core, module)
            constraints: dict = {}
            extra: dict = {}
            if marker_call is not None:
                constraints, extra = parse_field_kwargs(marker_call)
                if "$ref" not in schema:
                    schema.update({k: v for k, v in constraints.items() if k != "description"})
            has_default, default_value = (False, None)
            if "default" in extra:
                has_default, default_value = True, extra["default"]
            elif default is not None and not isinstance(default, nodes.Call):
                has_default, default_value = literal_value(default)
                if not has_default and default is not None:
                    has_default = True  # non-literal default still means optional
                    default_value = None
            required = not has_default and not extra.get("has_default_factory")
            if extra.get("required"):
                required = True

            location = _marker_location(marker_name)
            if location is None:
                if param_name in path_param_names:
                    location = "path"
                elif ann_short in ("UploadFile",) or _is_upload_list(ann_core):
                    location = "file"
                elif _is_model_like(converter, ann_core, module):
                    location = "body"
                else:
                    location = "query"

            if location == "path":
                required = True
                if not schema:
                    unresolved.append(
                        UnresolvedSite(
                            service_id=service.id,
                            path=_normalize_path(route.raw_path),
                            method=method,
                            site=f"parameters/{param_name}/schema",
                            kind="parameter_type",
                            reason_code=confidence.reason_code,
                            evidence=[sig_evidence],
                        )
                    )
                parameters.append(
                    Parameter(
                        name=extra.get("alias", param_name),
                        location="path",
                        required=True,
                        schema=schema or {"type": "string"},
                        description_hint=extra.get("description"),
                        evidence=[sig_evidence],
                        confidence=confidence,
                    )
                )
            elif location in ("query", "header", "cookie"):
                name = extra.get("alias", param_name)
                if location == "header":
                    name = name.replace("_", "-")
                if schema.get("type") == "array":
                    pass  # style defaults are fine (form/explode)
                if not schema:
                    unresolved.append(
                        UnresolvedSite(
                            service_id=service.id,
                            path=_normalize_path(route.raw_path),
                            method=method,
                            site=f"parameters/{name}/schema",
                            kind="parameter_type",
                            reason_code=confidence.reason_code,
                            evidence=[sig_evidence],
                        )
                    )
                if has_default and default_value is not None and "$ref" not in schema:
                    schema.setdefault("default", default_value)
                parameters.append(
                    Parameter(
                        name=name,
                        location=location,  # type: ignore[arg-type]
                        required=required,
                        schema=schema,
                        description_hint=extra.get("description"),
                        default_repr=default_value if has_default else None,
                        deprecated=extra.get("deprecated"),
                        evidence=[sig_evidence],
                        confidence=confidence,
                    )
                )
            elif location == "file":
                item = {"type": "string", "format": "binary"}
                if _is_upload_list(ann_core):
                    file_parts.append((param_name, {"type": "array", "items": item}, required))
                else:
                    file_parts.append((param_name, item, required))
            elif location == "form":
                if has_default and default_value is not None:
                    schema.setdefault("default", default_value)
                form_parts.append((extra.get("alias", param_name), schema or {"type": "string"}, required))
            elif location == "body":
                if extra.get("media_type"):
                    media_type_override = extra["media_type"]
                json_parts.append((param_name, schema, required, bool(extra.get("embed"))))
                if not schema:
                    unresolved.append(
                        UnresolvedSite(
                            service_id=service.id,
                            path=_normalize_path(route.raw_path),
                            method=method,
                            site="request_body/content/application~1json/schema",
                            kind="request_schema",
                            reason_code=confidence.reason_code,
                            evidence=[sig_evidence],
                        )
                    )

        request_body = self._assemble_body(
            json_parts, form_parts, file_parts, media_type_override, sig_evidence
        )
        return parameters, request_body, security, {}

    @staticmethod
    def _assemble_body(
        json_parts: list[tuple[str, dict, bool, bool]],
        form_parts: list[tuple[str, dict, bool]],
        file_parts: list[tuple[str, dict, bool]],
        media_type_override: str | None,
        evidence: Evidence,
    ) -> RequestBody | None:
        if file_parts or form_parts:
            properties: dict = {}
            required: list[str] = []
            for name, schema, req in form_parts + file_parts:
                properties[name] = schema
                if req:
                    required.append(name)
            # json models mixed with files become object parts of the multipart body
            for name, schema, req, _embed in json_parts:
                properties[name] = schema
                if req:
                    required.append(name)
            media = "multipart/form-data" if file_parts else "application/x-www-form-urlencoded"
            body_schema: dict = {"type": "object", "properties": properties}
            if required:
                body_schema["required"] = sorted(required)
            return RequestBody(
                required=bool(required),
                content={media: MediaTypeContract(schema=body_schema)},
                evidence=[evidence],
                confidence=Confidence(level="high", reason_code="declared_type"),
            )
        if not json_parts:
            return None
        media = media_type_override or "application/json"
        if len(json_parts) == 1 and not json_parts[0][3]:
            name, schema, required, _ = json_parts[0]
            return RequestBody(
                required=required,
                content={media: MediaTypeContract(schema=schema)},
                evidence=[evidence],
                confidence=Confidence(level="high", reason_code="declared_type"),
            )
        properties = {name: schema for name, schema, _req, _e in json_parts}
        required_names = sorted(name for name, _s, req, _e in json_parts if req)
        body_schema = {"type": "object", "properties": properties}
        if required_names:
            body_schema["required"] = required_names
        return RequestBody(
            required=bool(required_names),
            content={media: MediaTypeContract(schema=body_schema)},
            evidence=[evidence],
            confidence=Confidence(level="high", reason_code="declared_type"),
        )

    # -- security ---------------------------------------------------------------

    def _security_from_dependency(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        node: nodes.NodeNG,
        route: RouteRef,
        scopes: list[str] | None = None,
        depth: int = 0,
    ) -> SecurityEvidence | None:
        """Resolve Depends(x)/Security(x) to a proven security scheme, if any."""
        if not isinstance(node, nodes.Call) or not node.args:
            return None
        target_name = dotted_name(node.args[0])
        if not target_name:
            return None
        try:
            assignments = module.getattr(target_name.split(".")[0])
        except Exception:  # noqa: BLE001
            assignments = []
        # variable assigned to a security class instance?
        decl = self._scheme_from_symbol(ctx, service, converter, module, target_name)
        if decl is not None:
            scheme_id, mechanism_evidence = decl
            return SecurityEvidence(
                scheme_id=scheme_id,
                scopes=sorted(scopes or []),
                mechanism="dependency_injection",
                evidence=mechanism_evidence,
                confidence=Confidence(level="high", reason_code="declared_annotation"),
            )
        # function dependency: look one level in for a nested scheme
        if depth < 2:
            target = converter.resolve_symbol(module, target_name)
            if isinstance(target, nodes.FunctionDef):
                for _name, annotation, default in _iter_params(target):
                    marker_call, _core = _extract_marker(annotation, default)
                    if marker_call is not None and (
                        (dotted_name(marker_call.func) or "").rsplit(".", 1)[-1]
                        in ("Depends", "Security")
                    ):
                        result = self._security_from_dependency(
                            ctx, service, converter, target.root(), marker_call, route,
                            scopes=scopes or _security_scopes(marker_call), depth=depth + 1,
                        )
                        if result is not None:
                            return result
        return None

    def _scheme_from_symbol(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        target_name: str,
    ) -> tuple[str, list[Evidence]] | None:
        parts = target_name.split(".")
        owner_module = module
        var = parts[-1]
        if len(parts) > 1:
            resolved_module = self._module_from_name(ctx, module, ".".join(parts[:-1]), converter)
            if resolved_module is None:
                return None
            owner_module = resolved_module
        try:
            assignments = owner_module.getattr(var)
        except Exception:  # noqa: BLE001
            return None
        for assignment in assignments:
            if isinstance(assignment, nodes.ImportFrom):
                modname = converter._resolve_import_from(assignment, owner_module)
                if modname is None:
                    continue
                target_module = ctx.module_by_name(modname)
                if target_module is not None:
                    return self._scheme_from_symbol(
                        ctx, service, converter, target_module,
                        assignment.real_name(var),
                    )
            if not isinstance(assignment, nodes.AssignName):
                continue
            parent = assignment.parent
            if not isinstance(parent, nodes.Assign) or not isinstance(parent.value, nodes.Call):
                continue
            class_short = (dotted_name(parent.value.func) or "").rsplit(".", 1)[-1]
            if class_short not in _SECURITY_CLASSES:
                continue
            kind, flow = _SECURITY_CLASSES[class_short]
            kwargs = {k.arg: k.value for k in parent.value.keywords or [] if k.arg}
            detail: dict = {}
            if kind == "oauth2" and flow:
                flow_detail: dict = {}
                token_url = _lit_str(kwargs.get("tokenUrl"))
                auth_url = _lit_str(kwargs.get("authorizationUrl"))
                if token_url:
                    flow_detail["tokenUrl"] = token_url
                if auth_url:
                    flow_detail["authorizationUrl"] = auth_url
                scopes_node = kwargs.get("scopes")
                ok, scope_map = literal_value(scopes_node) if scopes_node else (False, None)
                flow_detail["scopes"] = scope_map if ok and isinstance(scope_map, dict) else {}
                detail["flows"] = {flow: flow_detail}
            elif kind.startswith("apikey"):
                api_key_name = _lit_str(kwargs.get("name"))
                if api_key_name:
                    detail["name"] = api_key_name
            elif kind == "openid_connect":
                url = _lit_str(kwargs.get("openIdConnectUrl"))
                if url:
                    detail["openIdConnectUrl"] = url
            scheme_id = var
            file_rel = ctx.rel(owner_module.file) if owner_module.file else "unknown"
            evidence = [
                Evidence(
                    file=file_rel,
                    start_line=parent.lineno or 1,
                    end_line=parent.end_lineno or parent.lineno or 1,
                    kind="assignment",
                    symbol=f"{owner_module.name}.{var}",
                )
            ]
            if scheme_id not in service.security_schemes:
                service.security_schemes[scheme_id] = SecuritySchemeDecl(
                    scheme_id=scheme_id,
                    kind=kind,  # type: ignore[arg-type]
                    detail=detail,
                    evidence=evidence,
                )
            return scheme_id, evidence
        return None

    # -- responses ---------------------------------------------------------------

    def _responses(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        func: nodes.FunctionDef,
        module: nodes.Module,
        kwargs: dict[str, nodes.NodeNG],
        route: RouteRef,
        method: str,
        has_validated_input: bool,
        state: ServiceState,
        unresolved: list[UnresolvedSite],
        normalized_path: str,
    ) -> list[ResponseVariant]:
        variants: list[ResponseVariant] = []
        variant_counter: dict[str, int] = {}

        def add(variant: ResponseVariant) -> None:
            variant.variant_index = variant_counter.get(variant.status, 0)
            variant_counter[variant.status] = variant.variant_index + 1
            variants.append(variant)

        decorator_evidence = Evidence(
            file=route.file,
            start_line=route.start_line,
            end_line=route.start_line,
            kind="decorator",
            symbol=route.handler_symbol,
        )

        success_status = _status_from_node(kwargs.get("status_code")) or 200
        response_class_short = _short_of(kwargs.get("response_class"))
        return_short = _short_of(func.returns)

        # success variant
        if response_class_short == "RedirectResponse" or return_short == "RedirectResponse":
            add(
                ResponseVariant(
                    status=str(_status_from_node(kwargs.get("status_code")) or 307),
                    origin="explicit_response_object",
                    content={},
                    headers={
                        "Location": HeaderSpec(
                            name="Location",
                            schema={"type": "string"},
                            evidence=[decorator_evidence],
                            confidence=Confidence(level="high", reason_code="framework_default"),
                        )
                    },
                    evidence=[decorator_evidence],
                    confidence=Confidence(level="high", reason_code="declared_annotation"),
                )
            )
        elif response_class_short in _RESPONSE_CLASSES and response_class_short not in (
            "JSONResponse",
            "ORJSONResponse",
            "UJSONResponse",
        ):
            media, schema = _RESPONSE_CLASSES[response_class_short]
            content = (
                {media: MediaTypeContract(schema=dict(schema))} if media and schema else {}
            )
            add(
                ResponseVariant(
                    status=str(success_status),
                    origin="explicit_response_object",
                    content=content,
                    evidence=[decorator_evidence],
                    confidence=Confidence(level="high", reason_code="declared_annotation"),
                )
            )
        else:
            response_model = kwargs.get("response_model")
            schema: dict = {}
            origin = "annotation"
            confidence = Confidence(level="high", reason_code="declared_annotation")
            if response_model is not None:
                schema, confidence = converter.convert(response_model, module)
            elif func.returns is not None and return_short not in _RESPONSE_CLASSES:
                schema, confidence = converter.convert(func.returns, module)
                origin = "return_type"
            elif success_status != 204:
                # untyped handler: infer from return statements (best effort)
                schema, confidence, origin = self._infer_return_schema(converter, func, module)
            if success_status == 204:
                add(
                    ResponseVariant(
                        status="204",
                        origin="annotation",
                        content={},
                        evidence=[decorator_evidence],
                        confidence=Confidence(level="high", reason_code="declared_annotation"),
                    )
                )
            else:
                if not schema:
                    unresolved.append(
                        UnresolvedSite(
                            service_id=service.id,
                            path=normalized_path,
                            method=method,
                            site=f"responses/{success_status}/content/application~1json/schema",
                            kind="response_schema",
                            reason_code=confidence.reason_code,
                            evidence=[decorator_evidence],
                        )
                    )
                add(
                    ResponseVariant(
                        status=str(success_status),
                        origin=origin,  # type: ignore[arg-type]
                        content={"application/json": MediaTypeContract(schema=schema)},
                        evidence=[decorator_evidence],
                        confidence=confidence,
                    )
                )

        # responses={...} map
        responses_node = kwargs.get("responses")
        if isinstance(responses_node, nodes.Dict):
            for key_node, value_node in responses_node.items:
                status = _status_from_node(key_node)
                if status is None:
                    continue
                schema: dict = {}
                description = None
                confidence = Confidence(level="high", reason_code="declared_annotation")
                if isinstance(value_node, nodes.Dict):
                    for k2, v2 in value_node.items:
                        key_ok, key_value = literal_value(k2)
                        if key_ok and key_value == "model":
                            schema, confidence = converter.convert(v2, module)
                        elif key_ok and key_value == "description":
                            ok, desc = literal_value(v2)
                            if ok:
                                description = desc
                add(
                    ResponseVariant(
                        status=str(status),
                        origin="annotation",
                        description_hint=description,
                        content={"application/json": MediaTypeContract(schema=schema)} if schema else {},
                        evidence=[decorator_evidence],
                        confidence=confidence,
                    )
                )

        # HTTPException raise sites (direct + bounded call chain)
        max_depth = ctx.config.analysis.call_graph_max_depth
        sites = collect_raise_sites(
            func, converter, max_depth, ctx.rel, dependency_edges=service.dependencies
        )
        seen_statuses: set[tuple[str, str]] = set()
        for site in sites:
            if site.exception_short == "HTTPException":
                if site.status_code is None:
                    ctx.warnings.emit(
                        "W302",
                        "HTTPException with non-literal status code",
                        file=site.evidence.file,
                        start_line=site.evidence.start_line,
                        service_id=service.id,
                    )
                    continue
                key = (str(site.status_code), site.exception_qname)
                if key in seen_statuses:
                    continue
                seen_statuses.add(key)
                detail_schema = {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "required": ["detail"],
                }
                if isinstance(site.call_kwargs.get("detail"), str):
                    detail_schema["properties"]["detail"] = {  # type: ignore[index]
                        "type": "string",
                        "examples": [site.call_kwargs["detail"]],
                    }
                add(
                    ResponseVariant(
                        status=str(site.status_code),
                        origin="raise_site",
                        condition=Condition(
                            kind="exception_handled", exception_type=site.exception_qname
                        ),
                        content={"application/json": MediaTypeContract(schema=detail_schema)},
                        evidence=[site.evidence],
                        confidence=Confidence(
                            level="high" if site.depth == 0 else "medium",
                            reason_code="framework_default",
                        ),
                    )
                )
            elif site.exception_qname in state.exception_handlers:
                handler = state.exception_handlers[site.exception_qname]
                if handler.status_code is None:
                    continue
                key = (str(handler.status_code), site.exception_qname)
                if key in seen_statuses:
                    continue
                seen_statuses.add(key)
                add(
                    ResponseVariant(
                        status=str(handler.status_code),
                        origin="exception_handler",
                        condition=Condition(
                            kind="exception_handled", exception_type=site.exception_qname
                        ),
                        content={"application/json": MediaTypeContract(schema={})},
                        evidence=[
                            site.evidence,
                            Evidence(
                                file=handler.file,
                                start_line=handler.line,
                                end_line=handler.line,
                                kind="exception_handler",
                                symbol=site.exception_qname,
                            ),
                        ],
                        confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
                    )
                )

        # framework validation error
        if has_validated_input:
            add(
                ResponseVariant(
                    status="422",
                    origin="framework_default",
                    description_hint="Validation Error",
                    content={
                        "application/json": MediaTypeContract(
                            schema=self._validation_error_schema(converter, service.id)
                        )
                    },
                    evidence=[decorator_evidence],
                    confidence=Confidence(level="medium", reason_code="framework_default"),
                )
            )
        return variants

    def _infer_return_schema(
        self, converter: PyTypeSchemaConverter, func: nodes.FunctionDef, module: nodes.Module
    ) -> tuple[dict, Confidence, str]:
        """Best-effort: literal dict returns and returned model constructors."""
        for ret in func.nodes_of_class(nodes.Return):
            if ret.value is None:
                continue
            ok, value = literal_value(ret.value)
            if ok and isinstance(value, dict):
                properties = {
                    k: _json_type_of(v) for k, v in value.items() if isinstance(k, str)
                }
                return (
                    {"type": "object", "properties": properties},
                    Confidence(level="medium", reason_code="inferred_return_flow"),
                    "return_type",
                )
            if isinstance(ret.value, nodes.Call):
                name = dotted_name(ret.value.func)
                if name:
                    resolved = converter.resolve_symbol(module, name)
                    if isinstance(resolved, nodes.ClassDef) and classify_class(resolved) in (
                        "pydantic",
                        "dataclass",
                        "attrs",
                    ):
                        schema = converter._nominal_ref(resolved)
                        return (
                            schema,
                            Confidence(level="medium", reason_code="inferred_return_flow"),
                            "return_type",
                        )
        return {}, Confidence(level="low", reason_code="dynamic_type"), "return_type"

    def _validation_error_schema(self, converter: PyTypeSchemaConverter, service_id: str) -> dict:
        lang_type = LangTypeRef(language="python", qualified_name="fastapi.HTTPValidationError")
        from openapi_agent.models.registry import REF_PREFIX, make_pending_id

        pending = make_pending_id(lang_type)
        if not converter.ctx.registry.contains(pending):
            converter.ctx.registry.intern(
                lang_type,
                {
                    "type": "object",
                    "properties": {
                        "detail": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "loc": {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [{"type": "string"}, {"type": "integer"}]
                                        },
                                    },
                                    "msg": {"type": "string"},
                                    "type": {"type": "string"},
                                },
                                "required": ["loc", "msg", "type"],
                            },
                        }
                    },
                },
                [],
                Confidence(level="medium", reason_code="framework_default"),
                service_id,
                title="HTTPValidationError",
            )
        return {"$ref": REF_PREFIX + pending}


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def _lit_str(node) -> str | None:
    if node is None:
        return None
    ok, value = literal_value(node)
    return value if ok and isinstance(value, str) else None


def _lit_str_list(node) -> list[str]:
    if node is None:
        return []
    ok, value = literal_value(node)
    if ok and isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _dependency_items(node) -> list[nodes.NodeNG]:
    if isinstance(node, (nodes.List, nodes.Tuple)):
        return [e for e in node.elts if isinstance(e, nodes.Call)]
    return []


def _security_scopes(call: nodes.Call | None) -> list[str]:
    if call is None:
        return []
    for keyword in call.keywords or []:
        if keyword.arg == "scopes":
            ok, value = literal_value(keyword.value)
            if ok and isinstance(value, list):
                return [v for v in value if isinstance(v, str)]
    return []


def _service_id_for(rel_path: str, used: set[str]) -> str:
    parts = rel_path.split("/")
    base = parts[0] if len(parts) > 1 else (parts[0].rsplit(".", 1)[0] or "app")
    if base in ("src", "app") and len(parts) > 2:
        base = parts[1]
    if base.endswith(".py"):
        base = base[:-3] or "app"
    candidate = base
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _build_system(facts: RepoFacts) -> str | None:
    kinds = {m.kind for m in facts.manifests}
    if "pyproject" in kinds:
        return "pyproject"
    if "requirements" in kinds:
        return "pip"
    return None


def _join_path(prefix: str, path: str) -> str:
    joined = (prefix.rstrip("/") + "/" + path.lstrip("/")).rstrip("/")
    if not joined.startswith("/"):
        joined = "/" + joined
    return joined or "/"


def _normalize_path(raw: str) -> str:
    # FastAPI already uses {name}; strip converter suffixes like {p:path}
    return _PATH_PARAM_RE.sub(lambda m: "{%s}" % m.group(1), raw) or "/"


def _short_of(node) -> str | None:
    if node is None:
        return None
    name = dotted_name(node if not isinstance(node, nodes.Subscript) else node.value)
    return name.rsplit(".", 1)[-1] if name else None


def _iter_params(func: nodes.FunctionDef):
    """Yield (name, annotation, default) for every parameter."""
    args = func.args
    all_args = list(args.posonlyargs or []) + list(args.args or [])
    all_annotations = list(args.posonlyargs_annotations or []) + list(args.annotations or [])
    defaults = list(args.defaults or [])
    offset = len(all_args) - len(defaults)
    for i, arg in enumerate(all_args):
        annotation = all_annotations[i] if i < len(all_annotations) else None
        default = defaults[i - offset] if i >= offset else None
        yield arg.name, annotation, default
    for i, arg in enumerate(args.kwonlyargs or []):
        annotation = (args.kwonlyargs_annotations or [None] * len(args.kwonlyargs))[i]
        default = (args.kw_defaults or [None] * len(args.kwonlyargs))[i]
        yield arg.name, annotation, default


def _extract_marker(
    annotation: nodes.NodeNG | None, default: nodes.NodeNG | None
) -> tuple[nodes.Call | None, nodes.NodeNG | None]:
    """Find the FastAPI marker call (Query/Path/.../Depends) in either the
    default value or Annotated metadata. Returns (marker_call, core_annotation)."""
    marker_names = {"Query", "Path", "Header", "Cookie", "Body", "Form", "File", "Depends", "Security"}
    core = annotation
    if isinstance(annotation, nodes.Subscript):
        base = dotted_name(annotation.value) or ""
        if base.rsplit(".", 1)[-1] == "Annotated":
            slice_node = annotation.slice
            args = list(slice_node.elts) if isinstance(slice_node, nodes.Tuple) else [slice_node]
            if args:
                core = args[0]
                for meta in args[1:]:
                    if isinstance(meta, nodes.Call):
                        name = (dotted_name(meta.func) or "").rsplit(".", 1)[-1]
                        if name in marker_names:
                            return meta, core
    if isinstance(default, nodes.Call):
        name = (dotted_name(default.func) or "").rsplit(".", 1)[-1]
        if name in marker_names:
            return default, core
    return None, core


def _marker_location(marker_name: str | None) -> str | None:
    return {
        "Query": "query",
        "Path": "path",
        "Header": "header",
        "Cookie": "cookie",
        "Body": "body",
        "Form": "form",
        "File": "file",
        None: None,
    }.get(marker_name)


def _is_upload_list(ann) -> bool:
    if isinstance(ann, nodes.Subscript):
        base = (dotted_name(ann.value) or "").rsplit(".", 1)[-1]
        if base in ("list", "List"):
            inner = ann.slice
            return (_short_of(inner) or "") == "UploadFile"
    return False


def _is_model_like(converter: PyTypeSchemaConverter, ann, module) -> bool:
    if ann is None:
        return False
    node = ann
    if isinstance(ann, nodes.Subscript):
        base = (dotted_name(ann.value) or "").rsplit(".", 1)[-1]
        if base in ("Optional", "List", "list", "Sequence"):
            node = ann.slice
        else:
            node = ann.value
    name = dotted_name(node)
    if not name:
        return False
    resolved = converter.resolve_symbol(module, name)
    if isinstance(resolved, nodes.ClassDef):
        return classify_class(resolved) in ("pydantic", "dataclass", "attrs", "typeddict")
    return False


def _status_from_node(node) -> int | None:
    if node is None:
        return None
    ok, value = literal_value(node)
    if ok and isinstance(value, int):
        return value
    name = dotted_name(node)
    if name:
        match = _STATUS_ATTR_RE.search(name)
        if match:
            return int(match.group(1))
    return None


def _handler_status(func: nodes.FunctionDef) -> int | None:
    for ret in func.nodes_of_class(nodes.Return):
        if isinstance(ret.value, nodes.Call):
            for keyword in ret.value.keywords or []:
                if keyword.arg == "status_code":
                    status = _status_from_node(keyword.value)
                    if status:
                        return status
    return None


def _json_type_of(value) -> dict:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {"type": "object"}
    return {}


def _dedupe_security(security: list[SecurityEvidence]) -> list[SecurityEvidence]:
    seen: set[tuple[str, str]] = set()
    result: list[SecurityEvidence] = []
    for evidence in security:
        key = (evidence.scheme_id, evidence.mechanism)
        if key not in seen:
            seen.add(key)
            result.append(evidence)
    return result
