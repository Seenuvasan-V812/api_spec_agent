"""Flask framework adapter (blueprints, MethodView, path converters).

Handles: multiple ``Flask()`` apps, ``Blueprint`` composition through
``register_blueprint`` (including blueprints imported from other modules and
nested registrations), ``@app.route``/shortcut decorators, ``add_url_rule``
with plain functions and ``MethodView.as_view`` class-based views, werkzeug
path converters (``<int:pet_id>``), best-effort request contracts from
``request.args/headers/cookies/form/files/get_json`` usage, response variants
from ``return`` statements (``jsonify``, tuple status form), ``abort()`` call
sites through a bounded call chain, recognizable werkzeug exception raises,
and ``@app.errorhandler(<int>)`` evidence.

Flask has no built-in auth: security is never guessed, the security list
stays empty unless proven (currently: never).
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
from openapi_agent.analysis.python.context import PythonAnalysisContext
from openapi_agent.analysis.python.type_schema import (
    PyTypeSchemaConverter,
    dotted_name,
    literal_value,
)
from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Condition,
    Confidence,
    DependencyEdge,
    Evidence,
    MediaTypeContract,
    Operation,
    Parameter,
    RequestBody,
    ResponseVariant,
    Service,
)

log = get_logger("analysis.python.flask")

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SHORTCUT_DECORATORS = ("get", "post", "put", "delete", "patch")

#: werkzeug path converter -> JSON Schema fragment
_CONVERTER_SCHEMAS: dict[str, dict] = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
    "path": {"type": "string"},
    "string": {"type": "string"},
    "any": {"type": "string"},
}

#: ``<converter(args):name>`` or ``<name>``
_FLASK_RULE_RE = re.compile(
    r"<(?:([A-Za-z_][A-Za-z0-9_]*)(?:\([^<>]*\))?:)?([A-Za-z_][A-Za-z0-9_]*)>"
)

#: werkzeug HTTP exceptions trivially recognizable by short name
_WERKZEUG_STATUS = {
    "BadRequest": 400,
    "Unauthorized": 401,
    "Forbidden": 403,
    "NotFound": 404,
    "Conflict": 409,
}

#: ``request.args.get("x", type=int)`` type= argument -> JSON type
_GET_TYPE_SCHEMAS = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "str": {"type": "string"},
}

_COLLECTION_LOCATIONS = {"args": "query", "headers": "header", "cookies": "cookie"}


@dataclass
class OwnerInfo:
    """A route owner: the Flask app itself or a Blueprint."""

    key: tuple[str, str]  # (module name, variable name)
    name: str = ""  # blueprint name (tags hint); "" for the app
    url_prefix: str = ""
    file: str = ""
    line: int = 1
    is_app: bool = False


@dataclass
class RegistrationEdge:
    parent: tuple[str, str]
    child: tuple[str, str]
    url_prefix: str = ""
    file: str = ""


@dataclass
class UrlRule:
    owner: tuple[str, str]
    rule: str
    view: nodes.NodeNG
    methods: list[str]  # explicit methods kwarg (lowercased); [] if absent
    module: nodes.Module
    file: str
    line: int


@dataclass
class ServiceState:
    service_id: str
    app_key: tuple[str, str]
    file: str
    owners: dict[tuple[str, str], OwnerInfo] = dc_field(default_factory=dict)
    registrations: list[RegistrationEdge] = dc_field(default_factory=list)
    url_rules: list[UrlRule] = dc_field(default_factory=list)
    error_handlers: dict[int, Evidence] = dc_field(default_factory=dict)
    route_details: dict[tuple[str, int, str], dict] = dc_field(default_factory=dict)
    graph_built: bool = False


class FlaskAdapter(FrameworkAdapter):
    name = "flask"
    language = "python"

    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        score = 0.0
        rationale = []
        deps = facts.manifest_dep_names()
        if "flask" in deps:
            score += 0.6
            rationale.append("flask in manifest")
        if "fastapi" in deps:
            score -= 0.2  # fastapi apps often carry flask transitively in lockfiles
        hits = facts.import_hits.get("flask", [])
        if hits:
            score += 0.35
            rationale.append(f"flask imported in {len(hits)} files")
        return DetectionResult(score=max(0.0, min(score, 0.95)), rationale="; ".join(rationale))

    # -------------------------------------------------------------- services
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        assert isinstance(ctx, PythonAnalysisContext)
        services: list[Service] = []
        states: dict[str, ServiceState] = {}
        used_ids: set[str] = set()

        for rel in ctx.repo_facts.import_hits.get("flask", []):
            module = ctx.astroid_module(rel)
            if module is None:
                continue
            for assign in module.nodes_of_class(nodes.Assign):
                call = assign.value
                if not isinstance(call, nodes.Call) or len(assign.targets) != 1:
                    continue
                callee = (dotted_name(call.func) or "").rsplit(".", 1)[-1]
                if callee != "Flask":
                    continue
                target = assign.targets[0]
                if not isinstance(target, nodes.AssignName):
                    continue
                service_id = _service_id_for(rel, used_ids)
                used_ids.add(service_id)
                service = Service(
                    id=service_id,
                    name=service_id,
                    language="python",
                    framework="flask",
                    framework_version=ctx.repo_facts.dep_version("flask"),
                    build_system=_build_system(ctx.repo_facts),
                    root_path=rel.rsplit("/", 1)[0] if "/" in rel else "",
                )
                services.append(service)
                states[service_id] = ServiceState(
                    service_id=service_id,
                    app_key=(module.name, target.name),
                    file=rel,
                )
        ctx.extras["flask_states"] = states
        if not services:
            ctx.warnings.emit("W201", "flask imported but no Flask() application found")
        return services

    # ---------------------------------------------------------------- routes
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: ServiceState = ctx.extras["flask_states"][service.id]
        converter = PyTypeSchemaConverter(ctx, service.id)
        ctx.extras.setdefault("flask_converters", {})[service.id] = converter

        self._build_graph(ctx, state, converter)

        routes: list[RouteRef] = []
        # BFS over the registration graph from the app node
        app_info = state.owners.get(state.app_key)
        if app_info is None:
            app_info = OwnerInfo(key=state.app_key, is_app=True, file=state.file)
            state.owners[state.app_key] = app_info
        queue: list[tuple[tuple[str, str], str]] = [(state.app_key, "")]
        seen: set[tuple[tuple[str, str], str]] = set()
        while queue:
            key, prefix = queue.pop(0)
            if (key, prefix) in seen:
                continue
            seen.add((key, prefix))
            owner = state.owners.get(key)
            if owner is None:
                continue
            tags = [owner.name] if owner.name else []
            routes.extend(
                self._decorated_routes(ctx, state, service, converter, key, prefix, tags)
            )
            routes.extend(
                self._url_rule_routes(ctx, state, service, converter, key, prefix, tags)
            )
            for edge in state.registrations:
                if edge.parent == key:
                    child = state.owners.get(edge.child)
                    child_prefix = _join_prefixes(
                        prefix, edge.url_prefix, child.url_prefix if child else ""
                    )
                    queue.append((edge.child, child_prefix))
        return routes

    def _build_graph(
        self, ctx: PythonAnalysisContext, state: ServiceState, converter: PyTypeSchemaConverter
    ) -> None:
        """Collect owners (apps + blueprints), registrations, add_url_rule
        calls and errorhandlers across every flask-importing file."""
        if state.graph_built:
            return
        state.graph_built = True
        for rel in ctx.repo_facts.import_hits.get("flask", []):
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
                if callee not in ("Flask", "Blueprint"):
                    continue
                kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
                blueprint_name = ""
                if callee == "Blueprint" and call.args:
                    blueprint_name = _lit_str(call.args[0]) or ""
                info = OwnerInfo(
                    key=(module.name, target.name),
                    name=blueprint_name,
                    url_prefix=_lit_str(kwargs.get("url_prefix")) or "",
                    file=rel,
                    line=assign.lineno or 1,
                    is_app=callee == "Flask",
                )
                state.owners.setdefault(info.key, info)
            for call in module.nodes_of_class(nodes.Call):
                func = call.func
                if not isinstance(func, nodes.Attribute):
                    continue
                if func.attrname == "register_blueprint":
                    self._collect_registration(ctx, state, converter, module, rel, call)
                elif func.attrname == "add_url_rule":
                    self._collect_url_rule(ctx, state, converter, module, rel, call)
            self._collect_error_handlers(module, rel, state)

    def _collect_registration(
        self,
        ctx: PythonAnalysisContext,
        state: ServiceState,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        rel: str,
        call: nodes.Call,
    ) -> None:
        parent_key = self._resolve_var_key(ctx, module, call.func.expr, state, converter)
        if parent_key is None:
            ctx.warnings.emit(
                "W202",
                "register_blueprint owner could not be resolved",
                file=rel,
                start_line=call.lineno,
                service_id=state.service_id,
            )
            return
        if not call.args:
            return
        child_key = self._resolve_var_key(ctx, module, call.args[0], state, converter)
        if child_key is None:
            ctx.warnings.emit(
                "W202",
                "registered blueprint could not be resolved",
                file=rel,
                start_line=call.lineno,
                service_id=state.service_id,
            )
            return
        kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
        state.registrations.append(
            RegistrationEdge(
                parent=parent_key,
                child=child_key,
                url_prefix=_lit_str(kwargs.get("url_prefix")) or "",
                file=rel,
            )
        )

    def _collect_url_rule(
        self,
        ctx: PythonAnalysisContext,
        state: ServiceState,
        converter: PyTypeSchemaConverter,
        module: nodes.Module,
        rel: str,
        call: nodes.Call,
    ) -> None:
        owner_key = self._resolve_var_key(ctx, module, call.func.expr, state, converter)
        if owner_key is None:
            ctx.warnings.emit(
                "W202",
                "add_url_rule owner could not be resolved",
                file=rel,
                start_line=call.lineno,
                service_id=state.service_id,
            )
            return
        if not call.args:
            return
        rule = _lit_str(call.args[0])
        if rule is None:
            ctx.warnings.emit(
                "W204",
                "route path is not a literal string; endpoint skipped",
                file=rel,
                start_line=call.lineno,
                service_id=state.service_id,
            )
            return
        kwargs = {k.arg: k.value for k in call.keywords or [] if k.arg}
        view = kwargs.get("view_func")
        if view is None and len(call.args) >= 3:
            view = call.args[2]  # add_url_rule(rule, endpoint, view_func)
        if view is None:
            return
        methods = [
            m.lower() for m in _lit_str_list(kwargs.get("methods")) if m.lower() in _HTTP_METHODS
        ]
        state.url_rules.append(
            UrlRule(
                owner=owner_key,
                rule=rule,
                view=view,
                methods=methods,
                module=module,
                file=rel,
                line=call.lineno or 1,
            )
        )

    @staticmethod
    def _collect_error_handlers(module: nodes.Module, rel: str, state: ServiceState) -> None:
        for func in module.nodes_of_class(nodes.FunctionDef):
            if not func.decorators:
                continue
            for decorator in func.decorators.nodes:
                if not isinstance(decorator, nodes.Call):
                    continue
                dfunc = decorator.func
                if (
                    isinstance(dfunc, nodes.Attribute)
                    and dfunc.attrname == "errorhandler"
                    and decorator.args
                ):
                    ok, status = literal_value(decorator.args[0])
                    if ok and isinstance(status, int) and not isinstance(status, bool):
                        state.error_handlers.setdefault(
                            status,
                            Evidence(
                                file=rel,
                                start_line=func.lineno or 1,
                                end_line=func.end_lineno or func.lineno or 1,
                                kind="exception_handler",
                                symbol=f"{module.name}.{func.name}",
                            ),
                        )

    # -- owner/variable resolution (mirrors the fastapi adapter) --------------

    def _resolve_var_key(
        self,
        ctx: PythonAnalysisContext,
        module: nodes.Module,
        expr: nodes.NodeNG,
        state: ServiceState,
        converter: PyTypeSchemaConverter,
    ) -> tuple[str, str] | None:
        """Resolve an app/blueprint expression to its defining (module, var) key."""
        if isinstance(expr, nodes.Name):
            if (module.name, expr.name) in state.owners:
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

    # -- route enumeration ------------------------------------------------------

    def _decorated_routes(
        self,
        ctx: PythonAnalysisContext,
        state: ServiceState,
        service: Service,
        converter: PyTypeSchemaConverter,
        key: tuple[str, str],
        prefix: str,
        tags: list[str],
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
                if attr in _SHORTCUT_DECORATORS:
                    methods = [attr]
                elif attr == "route":
                    kwargs = {k.arg: k.value for k in decorator.keywords or [] if k.arg}
                    methods = [
                        m.lower()
                        for m in _lit_str_list(kwargs.get("methods"))
                        if m.lower() in _HTTP_METHODS
                    ] or ["get"]
                else:
                    continue
                raw = _lit_str(decorator.args[0]) if decorator.args else None
                if raw is None:
                    ctx.warnings.emit(
                        "W204",
                        "route path is not a literal string; endpoint skipped",
                        file=rel,
                        start_line=decorator.lineno,
                        service_id=service.id,
                    )
                    continue
                full_path = _join_path(prefix, raw)
                detail_key = (rel, decorator.lineno or 1, ",".join(methods))
                state.route_details[detail_key] = {
                    "func": func,
                    "module": module,
                    "tags": tags,
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
                        DependencyEdge(from_file=state.file, to_file=rel, kind="import")
                    )
        return refs

    def _url_rule_routes(
        self,
        ctx: PythonAnalysisContext,
        state: ServiceState,
        service: Service,
        converter: PyTypeSchemaConverter,
        key: tuple[str, str],
        prefix: str,
        tags: list[str],
    ) -> list[RouteRef]:
        refs: list[RouteRef] = []
        for rule in state.url_rules:
            if rule.owner != key:
                continue
            full_path = _join_path(prefix, rule.rule)
            view = rule.view
            # MethodView: SomeView.as_view("name")
            if (
                isinstance(view, nodes.Call)
                and isinstance(view.func, nodes.Attribute)
                and view.func.attrname == "as_view"
            ):
                cls_name = dotted_name(view.func.expr)
                cls = converter.resolve_symbol(rule.module, cls_name) if cls_name else None
                if not isinstance(cls, nodes.ClassDef) or not _is_method_view(cls):
                    ctx.warnings.emit(
                        "W202",
                        "add_url_rule view class could not be resolved",
                        file=rule.file,
                        start_line=rule.line,
                        service_id=service.id,
                    )
                    continue
                allowed = set(rule.methods) if rule.methods else None
                cls_module = cls.root()
                cls_rel = ctx.rel(cls_module.file) if cls_module.file else "unknown"
                for statement in cls.body:
                    if not isinstance(statement, nodes.FunctionDef):
                        continue
                    method = statement.name
                    if method not in _HTTP_METHODS:
                        continue
                    if allowed is not None and method not in allowed:
                        continue
                    detail_key = (rule.file, rule.line, method)
                    state.route_details[detail_key] = {
                        "func": statement,
                        "module": cls_module,
                        "tags": tags,
                    }
                    refs.append(
                        RouteRef(
                            service_hint=service.id,
                            raw_path=full_path,
                            methods=[method],
                            handler_symbol=f"{cls_module.name}.{cls.name}.{method}",
                            file=rule.file,
                            start_line=rule.line,
                            kind="method_view",
                        )
                    )
                    if state.file != cls_rel:
                        service.dependencies.append(
                            DependencyEdge(from_file=state.file, to_file=cls_rel, kind="import")
                        )
                continue
            # plain function view
            view_name = dotted_name(view)
            target = converter.resolve_symbol(rule.module, view_name) if view_name else None
            if not isinstance(target, nodes.FunctionDef):
                ctx.warnings.emit(
                    "W202",
                    "add_url_rule view function could not be resolved",
                    file=rule.file,
                    start_line=rule.line,
                    service_id=service.id,
                )
                continue
            methods = rule.methods or ["get"]
            target_module = target.root()
            detail_key = (rule.file, rule.line, ",".join(methods))
            state.route_details[detail_key] = {
                "func": target,
                "module": target_module,
                "tags": tags,
            }
            refs.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=full_path,
                    methods=methods,
                    handler_symbol=f"{target_module.name}.{target.name}",
                    file=rule.file,
                    start_line=rule.line,
                    kind="functional",
                )
            )
        return refs

    # ------------------------------------------------------------ extraction
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        assert isinstance(ctx, PythonAnalysisContext)
        state: ServiceState = ctx.extras["flask_states"][service.id]
        converter: PyTypeSchemaConverter = ctx.extras["flask_converters"][service.id]
        detail_key = (route.file, route.start_line, ",".join(route.methods))
        details = state.route_details.get(detail_key)
        if details is None:
            return []
        func: nodes.FunctionDef = details["func"]
        module: nodes.Module = details["module"]
        handler_rel = ctx.rel(module.file) if module.file else route.file

        normalized_path = _normalize_flask_path(route.raw_path)
        registration_evidence = Evidence(
            file=route.file,
            start_line=route.start_line,
            end_line=route.start_line,
            kind="decorator" if route.kind == "decorator" else "call_site",
            symbol=route.handler_symbol,
        )
        signature_evidence = Evidence(
            file=handler_rel,
            start_line=func.lineno or 1,
            end_line=func.end_lineno or func.lineno or 1,
            kind="signature",
            symbol=route.handler_symbol,
        )

        doc = func.doc_node.value.strip() if func.doc_node else None
        summary = doc.splitlines()[0].strip() if doc else None
        description = None
        if doc:
            body_lines = doc.splitlines()[1:]
            description = "\n".join(line.strip() for line in body_lines).strip() or None

        extractions: list[OperationExtraction] = []
        for method in route.methods:
            unresolved: list[UnresolvedSite] = []
            parameters = self._path_parameters(
                ctx, service, route, method, registration_evidence, unresolved
            )
            request_body = self._request_contract(
                ctx, service, func, handler_rel, route, method,
                normalized_path, parameters, unresolved,
            )
            responses = self._responses(
                ctx, service, converter, func, handler_rel, route, method,
                normalized_path, state, unresolved,
            )
            operation = Operation(
                method=method,  # type: ignore[arg-type]
                operation_id=f"{service.id}.{route.handler_symbol}.{method}",
                handler=route.handler_symbol,
                parameters=parameters,
                request_body=request_body,
                responses=responses,
                security=[],  # flask has no built-in auth; never guess
                tags_hint=list(dict.fromkeys(t for t in details["tags"] if t)),
                summary_hint=summary,
                description_hint=description,
                evidence=(
                    [registration_evidence]
                    if route.file == handler_rel
                    else [registration_evidence, signature_evidence]
                ),
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

    # -- path parameters ---------------------------------------------------------

    def _path_parameters(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        route: RouteRef,
        method: str,
        evidence: Evidence,
        unresolved: list[UnresolvedSite],
    ) -> list[Parameter]:
        parameters: list[Parameter] = []
        for match in _FLASK_RULE_RE.finditer(route.raw_path):
            converter_name, param_name = match.group(1), match.group(2)
            schema = _CONVERTER_SCHEMAS.get(converter_name or "string")
            if schema is None:
                ctx.warnings.emit(
                    "W205",
                    f"unknown path converter {converter_name!r}; treating as string",
                    file=route.file,
                    start_line=route.start_line,
                    service_id=service.id,
                )
                unresolved.append(
                    UnresolvedSite(
                        service_id=service.id,
                        path=_normalize_flask_path(route.raw_path),
                        method=method,
                        site=f"parameters/{param_name}/schema",
                        kind="parameter_type",
                        reason_code="unresolved_symbol",
                        evidence=[evidence],
                    )
                )
                confidence = Confidence(level="low", reason_code="unresolved_symbol")
                schema = {"type": "string"}
            else:
                schema = dict(schema)
                confidence = Confidence(level="high", reason_code="declared_annotation")
            parameters.append(
                Parameter(
                    name=param_name,
                    location="path",
                    required=True,
                    schema=schema,
                    evidence=[evidence],
                    confidence=confidence,
                )
            )
        return parameters

    # -- request contract from handler body ---------------------------------------

    def _request_contract(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        func: nodes.FunctionDef,
        handler_rel: str,
        route: RouteRef,
        method: str,
        normalized_path: str,
        parameters: list[Parameter],
        unresolved: list[UnresolvedSite],
    ) -> RequestBody | None:
        seen_params: set[tuple[str, str]] = {(p.location, p.name) for p in parameters}
        form_parts: list[tuple[str, dict, bool]] = []  # name, schema, required
        file_parts: list[tuple[str, dict, bool]] = []
        seen_parts: set[tuple[str, str]] = set()
        json_node: nodes.NodeNG | None = None
        first_part_node: nodes.NodeNG | None = None

        def access_evidence(node: nodes.NodeNG) -> Evidence:
            return Evidence(
                file=handler_rel,
                start_line=node.lineno or 1,
                end_line=node.end_lineno or node.lineno or 1,
                kind="call_site",
                symbol=route.handler_symbol,
            )

        def add_param(location: str, name: str, schema: dict, required: bool,
                      default, node: nodes.NodeNG) -> None:
            if (location, name) in seen_params:
                return
            seen_params.add((location, name))
            parameters.append(
                Parameter(
                    name=name,
                    location=location,  # type: ignore[arg-type]
                    required=required,
                    schema=schema,
                    default_repr=default,
                    evidence=[access_evidence(node)],
                    confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
                )
            )

        def add_part(kind: str, name: str, required: bool, node: nodes.NodeNG) -> None:
            nonlocal first_part_node
            if (kind, name) in seen_parts:
                return
            seen_parts.add((kind, name))
            if first_part_node is None:
                first_part_node = node
            if kind == "files":
                file_parts.append((name, {"type": "string", "format": "binary"}, required))
            else:
                form_parts.append((name, {"type": "string"}, required))

        for node in func.nodes_of_class((nodes.Call, nodes.Subscript, nodes.Attribute)):
            if isinstance(node, nodes.Call):
                callee = node.func
                if not isinstance(callee, nodes.Attribute):
                    continue
                if callee.attrname == "get_json" and _is_request(callee.expr):
                    json_node = json_node or node
                    continue
                if callee.attrname != "get":
                    continue
                collection = _request_collection(callee.expr)
                if collection is None or not node.args:
                    continue
                name = _lit_str(node.args[0])
                if name is None:
                    continue
                schema = dict(_get_type_schema(node) or {"type": "string"})
                ok, default = (False, None)
                default_node = next(
                    (k.value for k in node.keywords or [] if k.arg == "default"),
                    node.args[1] if len(node.args) > 1 else None,
                )
                if default_node is not None:
                    ok, default = literal_value(default_node)
                if ok and default is not None:
                    schema.setdefault("default", default)
                if collection in _COLLECTION_LOCATIONS:
                    add_param(
                        _COLLECTION_LOCATIONS[collection], name, schema, False,
                        default if ok else None, node,
                    )
                elif collection in ("form", "files"):
                    add_part(collection, name, False, node)
            elif isinstance(node, nodes.Subscript):
                collection = _request_collection(node.value)
                if collection is None:
                    continue
                name = _lit_str(node.slice)
                if name is None:
                    continue
                if collection in _COLLECTION_LOCATIONS:
                    add_param(
                        _COLLECTION_LOCATIONS[collection], name, {"type": "string"}, True,
                        None, node,
                    )
                elif collection in ("form", "files"):
                    add_part(collection, name, True, node)
            elif isinstance(node, nodes.Attribute):
                if node.attrname == "json" and _is_request(node.expr):
                    json_node = json_node or node

        content: dict[str, MediaTypeContract] = {}
        body_confidence: Confidence | None = None
        body_evidence: list[Evidence] = []
        required_body = False
        if form_parts or file_parts:
            properties: dict[str, dict] = {}
            required_names: list[str] = []
            for name, schema, req in form_parts + file_parts:
                properties[name] = schema
                if req:
                    required_names.append(name)
            body_schema: dict = {"type": "object", "properties": properties}
            if required_names:
                body_schema["required"] = sorted(required_names)
            media = "multipart/form-data" if file_parts else "application/x-www-form-urlencoded"
            content[media] = MediaTypeContract(schema=body_schema)
            body_confidence = Confidence(level="medium", reason_code="inferred_return_flow")
            required_body = bool(required_names)
            if first_part_node is not None:
                body_evidence.append(access_evidence(first_part_node))
        if json_node is not None:
            # honest default: shape of the parsed JSON is unknown statically
            content["application/json"] = MediaTypeContract(schema={})
            unresolved.append(
                UnresolvedSite(
                    service_id=service.id,
                    path=normalized_path,
                    method=method,
                    site="request_body/content/application~1json/schema",
                    kind="request_schema",
                    reason_code="dynamic_type",
                    evidence=[access_evidence(json_node)],
                )
            )
            if body_confidence is None:
                body_confidence = Confidence(level="low", reason_code="dynamic_type")
                required_body = True
            body_evidence.append(access_evidence(json_node))
        if not content:
            return None
        return RequestBody(
            required=required_body,
            content=content,
            evidence=body_evidence,
            confidence=body_confidence or Confidence(level="low", reason_code="dynamic_type"),
        )

    # -- responses ---------------------------------------------------------------

    def _responses(
        self,
        ctx: PythonAnalysisContext,
        service: Service,
        converter: PyTypeSchemaConverter,
        func: nodes.FunctionDef,
        handler_rel: str,
        route: RouteRef,
        method: str,
        normalized_path: str,
        state: ServiceState,
        unresolved: list[UnresolvedSite],
    ) -> list[ResponseVariant]:
        variants: list[ResponseVariant] = []
        variant_counter: dict[str, int] = {}
        seen_success: set[str] = set()

        def add(variant: ResponseVariant) -> None:
            variant.variant_index = variant_counter.get(variant.status, 0)
            variant_counter[variant.status] = variant.variant_index + 1
            variants.append(variant)

        def return_evidence(node: nodes.NodeNG) -> Evidence:
            return Evidence(
                file=handler_rel,
                start_line=node.lineno or 1,
                end_line=node.end_lineno or node.lineno or 1,
                kind="return_stmt",
                symbol=route.handler_symbol,
            )

        returns = [
            ret for ret in func.nodes_of_class(nodes.Return) if _frame_of(ret) is func
        ]
        for ret in returns:
            status, content, confidence, needs_unresolved = self._return_variant(
                converter, ret
            )
            fingerprint = f"{status}:{sorted(content)}"
            if fingerprint in seen_success:
                continue
            seen_success.add(fingerprint)
            if needs_unresolved:
                unresolved.append(
                    UnresolvedSite(
                        service_id=service.id,
                        path=normalized_path,
                        method=method,
                        site=f"responses/{status}/content/application~1json/schema",
                        kind="response_schema",
                        reason_code=confidence.reason_code,
                        evidence=[return_evidence(ret)],
                    )
                )
            add(
                ResponseVariant(
                    status=status,
                    origin="return_type",
                    content=content,
                    evidence=[return_evidence(ret)],
                    confidence=confidence,
                )
            )
        if not returns:
            confidence = Confidence(level="low", reason_code="dynamic_type")
            unresolved.append(
                UnresolvedSite(
                    service_id=service.id,
                    path=normalized_path,
                    method=method,
                    site="responses/200/content/application~1json/schema",
                    kind="response_schema",
                    reason_code="dynamic_type",
                    evidence=[
                        Evidence(
                            file=handler_rel,
                            start_line=func.lineno or 1,
                            end_line=func.end_lineno or func.lineno or 1,
                            kind="signature",
                            symbol=route.handler_symbol,
                        )
                    ],
                )
            )
            add(
                ResponseVariant(
                    status="200",
                    origin="framework_default",
                    content={"application/json": MediaTypeContract(schema={})},
                    evidence=[
                        Evidence(
                            file=handler_rel,
                            start_line=func.lineno or 1,
                            end_line=func.end_lineno or func.lineno or 1,
                            kind="signature",
                            symbol=route.handler_symbol,
                        )
                    ],
                    confidence=confidence,
                )
            )

        # abort(<status>) in the handler and one call level below
        seen_errors: set[str] = set()
        for status_code, description, node, node_rel in self._abort_sites(
            ctx, converter, func
        ):
            key = f"abort:{status_code}"
            if key in seen_errors:
                continue
            seen_errors.add(key)
            evidence = [
                Evidence(
                    file=node_rel,
                    start_line=node.lineno or 1,
                    end_line=node.end_lineno or node.lineno or 1,
                    kind="call_site",
                    symbol=route.handler_symbol,
                )
            ]
            if status_code in state.error_handlers:
                evidence.append(state.error_handlers[status_code])
            add(
                ResponseVariant(
                    status=str(status_code),
                    origin="raise_site",
                    description_hint=description,
                    content={"application/json": MediaTypeContract(schema={})},
                    evidence=evidence,
                    confidence=Confidence(level="medium", reason_code="framework_default"),
                )
            )

        # werkzeug HTTP exceptions raised in the bounded call chain
        max_depth = ctx.config.analysis.call_graph_max_depth
        try:
            sites = collect_raise_sites(
                func, converter, max_depth, ctx.rel, dependency_edges=service.dependencies
            )
        except Exception:  # noqa: BLE001 - degrade, never sink the operation
            sites = []
        for site in sites:
            status_code = _WERKZEUG_STATUS.get(site.exception_short)
            if status_code is None:
                continue
            key = f"raise:{status_code}:{site.exception_short}"
            if key in seen_errors:
                continue
            seen_errors.add(key)
            exception_type = (
                site.exception_qname
                if "." in site.exception_qname
                else f"werkzeug.exceptions.{site.exception_short}"
            )
            evidence = [site.evidence]
            if status_code in state.error_handlers:
                evidence.append(state.error_handlers[status_code])
            add(
                ResponseVariant(
                    status=str(status_code),
                    origin="raise_site",
                    condition=Condition(kind="exception_handled", exception_type=exception_type),
                    content={"application/json": MediaTypeContract(schema={})},
                    evidence=evidence,
                    confidence=Confidence(level="medium", reason_code="framework_default"),
                )
            )
        return variants

    def _return_variant(
        self, converter: PyTypeSchemaConverter, ret: nodes.Return
    ) -> tuple[str, dict[str, MediaTypeContract], Confidence, bool]:
        """(status, content, confidence, needs_unresolved) for one return stmt."""
        value = ret.value
        status = 200
        if isinstance(value, nodes.Tuple) and len(value.elts) >= 2:
            ok, literal_status = literal_value(value.elts[1])
            if ok and isinstance(literal_status, int) and not isinstance(literal_status, bool):
                status = literal_status
            value = value.elts[0]

        if value is None:
            return (
                str(status),
                {},
                Confidence(level="low", reason_code="dynamic_type"),
                False,
            )
        if isinstance(value, nodes.Const) and isinstance(value.value, str):
            if value.value == "":
                return (
                    str(status),
                    {},
                    Confidence(level="high", reason_code="declared_annotation"),
                    False,
                )
            return (
                str(status),
                {"text/html": MediaTypeContract(schema={"type": "string"})},
                Confidence(level="medium", reason_code="inferred_return_flow"),
                False,
            )
        body_node = value
        if isinstance(value, nodes.Call):
            callee = (dotted_name(value.func) or "").rsplit(".", 1)[-1]
            if callee == "jsonify":
                schema = _jsonify_schema(value)
                if schema is not None:
                    return (
                        str(status),
                        {"application/json": MediaTypeContract(schema=schema)},
                        Confidence(level="medium", reason_code="inferred_return_flow"),
                        False,
                    )
                return (
                    str(status),
                    {"application/json": MediaTypeContract(schema={})},
                    Confidence(level="low", reason_code="dynamic_type"),
                    True,
                )
        if isinstance(body_node, nodes.Dict):
            schema = _dict_node_schema(body_node)
            if schema is not None:
                return (
                    str(status),
                    {"application/json": MediaTypeContract(schema=schema)},
                    Confidence(level="medium", reason_code="inferred_return_flow"),
                    False,
                )
        return (
            str(status),
            {"application/json": MediaTypeContract(schema={})},
            Confidence(level="low", reason_code="dynamic_type"),
            True,
        )

    def _abort_sites(
        self,
        ctx: PythonAnalysisContext,
        converter: PyTypeSchemaConverter,
        func: nodes.FunctionDef,
        depth: int = 0,
        visited: set[str] | None = None,
    ) -> list[tuple[int, str | None, nodes.Call, str]]:
        """(status, description, node, repo-relative file) for every
        ``abort(<int>)`` in the handler and one call level below."""
        if visited is None:
            visited = set()
        try:
            key = func.qname()
        except Exception:  # noqa: BLE001
            return []
        if key in visited:
            return []
        visited.add(key)
        module = func.root()
        rel = ctx.rel(module.file) if module.file else "unknown"
        sites: list[tuple[int, str | None, nodes.Call, str]] = []
        for call in func.nodes_of_class(nodes.Call):
            name = dotted_name(call.func)
            if not name:
                continue
            short = name.rsplit(".", 1)[-1]
            if short == "abort" and call.args:
                ok, code = literal_value(call.args[0])
                if ok and isinstance(code, int) and not isinstance(code, bool):
                    description = None
                    for keyword in call.keywords or []:
                        if keyword.arg == "description":
                            description = _lit_str(keyword.value)
                    if description is None and len(call.args) > 1:
                        description = _lit_str(call.args[1])
                    sites.append((code, description, call, rel))
            elif depth < 1:
                try:
                    target = converter.resolve_symbol(module, name)
                except Exception:  # noqa: BLE001
                    target = None
                if isinstance(target, nodes.FunctionDef):
                    sites.extend(
                        self._abort_sites(ctx, converter, target, depth + 1, visited)
                    )
        return sites


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


def _join_prefixes(*parts: str) -> str:
    joined = ""
    for part in parts:
        if part:
            joined = joined.rstrip("/") + "/" + part.strip("/")
    return joined


def _join_path(prefix: str, path: str) -> str:
    joined = (prefix.rstrip("/") + "/" + path.lstrip("/")).rstrip("/")
    if not joined.startswith("/"):
        joined = "/" + joined
    return joined or "/"


def _normalize_flask_path(raw: str) -> str:
    return _FLASK_RULE_RE.sub(lambda m: "{%s}" % m.group(2), raw) or "/"


def _is_method_view(cls: nodes.ClassDef) -> bool:
    for base in cls.bases:
        name = dotted_name(base if not isinstance(base, nodes.Subscript) else base.value)
        if name and name.rsplit(".", 1)[-1] == "MethodView":
            return True
    try:
        return any(a.qname() == "flask.views.MethodView" for a in cls.ancestors())
    except Exception:  # noqa: BLE001
        return False


def _is_request(node: nodes.NodeNG) -> bool:
    return isinstance(node, nodes.Name) and node.name == "request"


def _request_collection(node: nodes.NodeNG) -> str | None:
    """'args'|'headers'|'cookies'|'form'|'files' when node is request.<coll>."""
    if (
        isinstance(node, nodes.Attribute)
        and node.attrname in ("args", "headers", "cookies", "form", "files")
        and _is_request(node.expr)
    ):
        return node.attrname
    return None


def _get_type_schema(call: nodes.Call) -> dict | None:
    """Schema from a ``type=`` keyword on ``request.args.get(...)``."""
    for keyword in call.keywords or []:
        if keyword.arg == "type":
            name = dotted_name(keyword.value)
            if name:
                return _GET_TYPE_SCHEMAS.get(name.rsplit(".", 1)[-1])
    return None


def _jsonify_schema(call: nodes.Call) -> dict | None:
    """Object schema from jsonify(dict-literal) or jsonify(key=value, ...)."""
    if call.keywords and not call.args:
        properties: dict[str, dict] = {}
        for keyword in call.keywords:
            if keyword.arg is None:  # **kwargs splat: shape unknown
                return None
            ok, value = literal_value(keyword.value)
            properties[keyword.arg] = _json_type_of(value) if ok else {}
        return {"type": "object", "properties": properties}
    if len(call.args) == 1 and not call.keywords:
        arg = call.args[0]
        if isinstance(arg, nodes.Dict):
            return _dict_node_schema(arg)
        if isinstance(arg, nodes.List):
            return {"type": "array"}
    return None


def _dict_node_schema(node: nodes.Dict) -> dict | None:
    """Object schema from a dict literal; keys must be string literals."""
    properties: dict[str, dict] = {}
    for key_node, value_node in node.items:
        ok, key = literal_value(key_node)
        if not ok or not isinstance(key, str):
            return None
        value_ok, value = literal_value(value_node)
        properties[key] = _json_type_of(value) if value_ok else {}
    return {"type": "object", "properties": properties}


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


def _frame_of(node: nodes.NodeNG):
    try:
        return node.frame()
    except Exception:  # noqa: BLE001
        return None
