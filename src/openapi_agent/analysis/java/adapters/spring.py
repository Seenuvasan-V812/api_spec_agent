"""Spring MVC / Spring WebFlux adapters.

Annotated routes: class-level ``@RequestMapping`` composition, ``@*Mapping``
methods, ``@PathVariable``/``@RequestParam``/``@RequestHeader``/``@CookieValue``/
``@RequestBody``/``@RequestPart``/``MultipartFile``/``@ModelAttribute``/
``Pageable`` parameters, ``ResponseEntity``/``Mono``/``Flux`` unwrapping,
``@ResponseStatus``, ``produces``/``consumes``, throw-site → ``@ControllerAdvice``
/ ``@ExceptionHandler`` mapping, bounded call-chain into injected services,
Bean-Validation-triggered 400s, and ``@PreAuthorize``/``@Secured``/
``@RolesAllowed`` security backed by provable ``SecurityFilterChain`` evidence.

WebFlux functional routes (``RouterFunction`` beans) are discovered from bean
method bodies (``route(GET("/x"), handler::get)`` and builder style) and
extracted with reduced confidence, as the spec requires — never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from openapi_agent.analysis.base import (
    AnalysisContext,
    DetectionResult,
    FrameworkAdapter,
    OperationExtraction,
    RouteRef,
    UnresolvedSite,
)
from openapi_agent.analysis.java.context import JavaAnalysisContext
from openapi_agent.analysis.java.ts_scanner import (
    JavaAnnotation,
    JavaClass,
    JavaMethod,
    JavaParam,
)
from openapi_agent.analysis.java.type_schema import JavaTypeConverter, split_generic
from openapi_agent.detection.repo import ManifestInfo, RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Condition,
    Confidence,
    Evidence,
    HeaderSpec,
    MediaTypeContract,
    Operation,
    Parameter,
    RequestBody,
    ResponseVariant,
    SecurityEvidence,
    SecuritySchemeDecl,
    Service,
)

log = get_logger("analysis.java.spring")

_MAPPING_METHODS = {
    "GetMapping": "get",
    "PostMapping": "post",
    "PutMapping": "put",
    "DeleteMapping": "delete",
    "PatchMapping": "patch",
}

_HTTP_STATUS_NAMES = {
    "CONTINUE": 100, "OK": 200, "CREATED": 201, "ACCEPTED": 202, "NO_CONTENT": 204,
    "MOVED_PERMANENTLY": 301, "FOUND": 302, "SEE_OTHER": 303, "NOT_MODIFIED": 304,
    "TEMPORARY_REDIRECT": 307, "PERMANENT_REDIRECT": 308,
    "BAD_REQUEST": 400, "UNAUTHORIZED": 401, "PAYMENT_REQUIRED": 402, "FORBIDDEN": 403,
    "NOT_FOUND": 404, "METHOD_NOT_ALLOWED": 405, "NOT_ACCEPTABLE": 406, "CONFLICT": 409,
    "GONE": 410, "PRECONDITION_FAILED": 412, "PAYLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415, "UNPROCESSABLE_ENTITY": 422, "TOO_MANY_REQUESTS": 429,
    "INTERNAL_SERVER_ERROR": 500, "NOT_IMPLEMENTED": 501, "BAD_GATEWAY": 502,
    "SERVICE_UNAVAILABLE": 503, "GATEWAY_TIMEOUT": 504,
}

_MEDIA_TYPE_CONSTANTS = {
    "APPLICATION_JSON_VALUE": "application/json",
    "APPLICATION_JSON": "application/json",
    "APPLICATION_XML_VALUE": "application/xml",
    "APPLICATION_XML": "application/xml",
    "TEXT_PLAIN_VALUE": "text/plain",
    "TEXT_PLAIN": "text/plain",
    "TEXT_HTML_VALUE": "text/html",
    "TEXT_HTML": "text/html",
    "TEXT_EVENT_STREAM_VALUE": "text/event-stream",
    "TEXT_EVENT_STREAM": "text/event-stream",
    "APPLICATION_OCTET_STREAM_VALUE": "application/octet-stream",
    "APPLICATION_OCTET_STREAM": "application/octet-stream",
    "MULTIPART_FORM_DATA_VALUE": "multipart/form-data",
    "MULTIPART_FORM_DATA": "multipart/form-data",
    "APPLICATION_NDJSON_VALUE": "application/x-ndjson",
    "APPLICATION_NDJSON": "application/x-ndjson",
}

_SKIP_PARAM_TYPES = {
    "HttpServletRequest", "HttpServletResponse", "ServerHttpRequest", "ServerHttpResponse",
    "ServerWebExchange", "WebSession", "Principal", "Authentication", "Model", "ModelMap",
    "BindingResult", "Errors", "UriComponentsBuilder", "Locale", "HttpSession", "HttpHeaders",
    "SseEmitter", "WebRequest", "NativeWebRequest",
}

_FUNCTIONAL_ROUTE_RE = re.compile(
    r"\b(?:RequestPredicates\.)?(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*\"([^\"]+)\""
)
_HANDLER_REF_RE = re.compile(r"(\w+)\s*::\s*(\w+)")
_STATUS_IN_BODY_RE = re.compile(r"HttpStatus\.([A-Z_]+)")


@dataclass
class SecurityModel:
    """A discovered Spring Security configuration bound to one module.

    ``permit_all`` / ``authenticated_patterns`` are the Ant path patterns from
    ``authorizeHttpRequests`` (or the legacy ``authorizeRequests``); an
    operation is treated as secured when it matches an authenticated pattern or
    ``anyRequest().authenticated()`` is in effect and it is not permitted.
    """

    scheme_id: str
    decl: SecuritySchemeDecl
    root: str = ""
    permit_all: list[str] = dc_field(default_factory=list)
    authenticated_patterns: list[str] = dc_field(default_factory=list)
    default_authenticated: bool = False
    config_evidence: Evidence | None = None


@dataclass
class SpringState:
    controllers: dict[str, list[JavaClass]] = dc_field(default_factory=dict)  # service -> classes
    advice_handlers: list[tuple[JavaClass, JavaMethod, str, int | None]] = dc_field(default_factory=list)
    # (advice class, handler method, exception simple name, status)
    security_models: list[SecurityModel] = dc_field(default_factory=list)
    security_by_service: dict[str, SecurityModel] = dc_field(default_factory=dict)
    functional_routes: dict[str, list[dict]] = dc_field(default_factory=dict)  # service -> routes
    converters: dict[str, JavaTypeConverter] = dc_field(default_factory=dict)
    error_schema_by_service: dict[str, dict] = dc_field(default_factory=dict)  # service -> $ref schema


def _spring_signals(facts: RepoFacts) -> tuple[bool, bool, bool]:
    deps = facts.manifest_dep_names()
    has_web = any(name.endswith(("spring-boot-starter-web", "spring-webmvc")) for name in deps)
    has_webflux = any(
        name.endswith(("spring-boot-starter-webflux", "spring-webflux")) for name in deps
    )
    has_annotations = bool(
        facts.annotation_hits.get("@RestController") or facts.annotation_hits.get("@Controller")
    )
    return has_web, has_webflux, has_annotations


class _SpringBase(FrameworkAdapter):
    language = "java"
    webflux = False

    # ------------------------------------------------------------- services
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: SpringState = ctx.extras.setdefault("spring_state", SpringState())
        module_roots = _module_roots(ctx.repo_facts)

        controllers: list[JavaClass] = []
        for cls in ctx.index.classes.values():
            if cls.find_annotation("RestController") is not None or (
                cls.find_annotation("Controller") is not None
                and cls.find_annotation("ResponseBody") is not None
            ):
                controllers.append(cls)
        router_beans = self._find_functional_beans(ctx) if self.webflux else []

        # group by deepest module root
        by_service: dict[str, list[JavaClass]] = {}
        service_names: dict[str, str] = {}
        for cls in controllers:
            root, name = _owning_module(cls.file, module_roots)
            by_service.setdefault(root, []).append(cls)
            service_names[root] = name
        for cls, routes in router_beans:
            root, name = _owning_module(cls.file, module_roots)
            by_service.setdefault(root, [])
            service_names[root] = name
            key = _service_id(root, name)
            state.functional_routes.setdefault(key, []).extend(routes)

        self._collect_advice(ctx, state)
        self._collect_security(ctx, state, module_roots)

        services: list[Service] = []
        for root in sorted(by_service):
            service_id = _service_id(root, service_names[root])
            base_path = _context_path(ctx, root, webflux=self.webflux)
            service = Service(
                id=service_id,
                name=service_names[root] or service_id,
                language="java",
                framework=self.name,
                framework_version=_spring_version(ctx.repo_facts),
                build_system=_java_build_system(ctx.repo_facts),
                root_path=root,
                base_paths=[base_path] if base_path else [],
            )
            model = _model_for_root(state.security_models, root)
            if model is not None:
                service.security_schemes[model.scheme_id] = model.decl
                state.security_by_service[service_id] = model
            state.controllers[service_id] = sorted(by_service[root], key=lambda c: c.qualified)
            state.converters[service_id] = JavaTypeConverter(
                ctx.index,
                ctx.registry,
                service_id,
                sidecar_facts=ctx.sidecar.types_by_qualified,
                sidecar_available=ctx.sidecar.available,
            )
            services.append(service)
        return services

    # --------------------------------------------------------------- routes
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: SpringState = ctx.extras["spring_state"]
        refs: list[RouteRef] = []
        route_details = ctx.extras.setdefault("spring_route_details", {})

        for cls in state.controllers.get(service.id, []):
            class_mapping = cls.find_annotation("RequestMapping")
            class_paths = _annotation_paths(class_mapping) or [""]
            class_produces = _media_types(class_mapping, "produces") if class_mapping else []
            class_consumes = _media_types(class_mapping, "consumes") if class_mapping else []
            for method in cls.methods:
                for annotation in method.annotations:
                    http_methods: list[str] = []
                    if annotation.name in _MAPPING_METHODS:
                        http_methods = [_MAPPING_METHODS[annotation.name]]
                    elif annotation.name == "RequestMapping":
                        http_methods = _request_mapping_methods(annotation)
                    else:
                        continue
                    method_paths = _annotation_paths(annotation) or [""]
                    for class_path in class_paths:
                        for method_path in method_paths:
                            raw_path = _join(class_path, method_path)
                            key = (cls.file, method.start_line, raw_path, ",".join(http_methods))
                            route_details[key] = {
                                "cls": cls,
                                "method": method,
                                "annotation": annotation,
                                "produces": _media_types(annotation, "produces") or class_produces,
                                "consumes": _media_types(annotation, "consumes") or class_consumes,
                                "kind": "annotation",
                            }
                            refs.append(
                                RouteRef(
                                    service_hint=service.id,
                                    raw_path=raw_path,
                                    methods=http_methods,
                                    handler_symbol=f"{cls.qualified}.{method.name}",
                                    file=cls.file,
                                    start_line=method.start_line,
                                    kind="annotation",
                                )
                            )
        for route in state.functional_routes.get(service.id, []):
            key = (route["file"], route["line"], route["path"], route["method"])
            ctx.extras["spring_route_details"][key] = {
                "cls": route.get("handler_cls"),
                "method": route.get("handler_method"),
                "annotation": None,
                "produces": [],
                "consumes": [],
                "kind": "functional",
            }
            refs.append(
                RouteRef(
                    service_hint=service.id,
                    raw_path=route["path"],
                    methods=[route["method"]],
                    handler_symbol=route["handler_symbol"],
                    file=route["file"],
                    start_line=route["line"],
                    kind="functional",
                )
            )
        return refs

    # ----------------------------------------------------------- extraction
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: SpringState = ctx.extras["spring_state"]
        converter = state.converters[service.id]
        details = ctx.extras["spring_route_details"].get(
            (route.file, route.start_line, route.raw_path, ",".join(route.methods))
        )
        if details is None:
            return []
        raw_path = _apply_base(service, route.raw_path)
        normalized = _normalize_spring_path(raw_path)

        if details["kind"] == "functional":
            return self._extract_functional(ctx, service, route, details, converter, normalized, raw_path)

        cls: JavaClass = details["cls"]
        method: JavaMethod = details["method"]
        produces: list[str] = details["produces"]

        extractions: list[OperationExtraction] = []
        for http_method in route.methods:
            unresolved: list[UnresolvedSite] = []
            evidence = [
                Evidence(
                    file=cls.file,
                    start_line=method.start_line,
                    end_line=method.end_line,
                    kind="annotation",
                    symbol=f"{cls.qualified}.{method.name}",
                )
            ]
            parameters, request_body, has_validation = self._parameters(
                ctx, service, converter, cls, method, normalized, http_method,
                details["consumes"], unresolved,
            )
            responses = self._responses(
                ctx, service, state, converter, cls, method, produces, normalized,
                http_method, has_validation, request_body is not None or bool(parameters),
                unresolved,
            )
            security = self._security(ctx, state, service, cls, method, normalized)
            # route/method/param locations come straight from annotations and
            # are exact even without the sidecar; type-resolution gaps are
            # reflected on the individual schemas instead.
            confidence = Confidence(level="high", reason_code="declared_annotation")
            tag = _tag_for_class(cls)
            operation = Operation(
                method=http_method,  # type: ignore[arg-type]
                operation_id=f"{service.id}.{cls.qualified}.{method.name}.{http_method}",
                handler=f"{cls.qualified}.{method.name}",
                parameters=parameters,
                request_body=request_body,
                responses=responses,
                security=security,
                tags_hint=[tag] if tag else [],
                summary_hint=method.javadoc.split(". ")[0][:200] if method.javadoc else None,
                description_hint=method.javadoc,
                evidence=evidence,
                confidence=confidence,
            )
            extractions.append(
                OperationExtraction(
                    endpoint_path=normalized,
                    raw_path=raw_path,
                    operation=operation,
                    unresolved=unresolved,
                )
            )
        return extractions

    # -- functional routes ------------------------------------------------------

    def _find_functional_beans(self, ctx: JavaAnalysisContext) -> list[tuple[JavaClass, list[dict]]]:
        results: list[tuple[JavaClass, list[dict]]] = []
        for cls in ctx.index.classes.values():
            if cls.find_annotation("Configuration") is None and cls.find_annotation("Component") is None:
                continue
            for method in cls.methods:
                if not method.return_type.startswith("RouterFunction"):
                    continue
                routes: list[dict] = []
                handler_hint = None
                for param in method.params:
                    handler_hint = ctx.index.resolve(param.type_text, cls)
                    if handler_hint is not None:
                        break
                for match in _FUNCTIONAL_ROUTE_RE.finditer(method.body_text):
                    http_method = match.group(1).lower()
                    path = match.group(2)
                    # nearest handler reference after this predicate
                    tail = method.body_text[match.end(): match.end() + 200]
                    handler_match = _HANDLER_REF_RE.search(tail)
                    handler_cls = handler_hint
                    handler_method = None
                    handler_symbol = f"{cls.qualified}.{method.name}"
                    if handler_match:
                        method_name = handler_match.group(2)
                        if handler_cls is not None:
                            handler_method = next(
                                (m for m in handler_cls.methods if m.name == method_name), None
                            )
                            handler_symbol = f"{handler_cls.qualified}.{method_name}"
                    routes.append(
                        {
                            "method": http_method,
                            "path": path,
                            "file": cls.file,
                            "line": method.start_line,
                            "handler_cls": handler_cls,
                            "handler_method": handler_method,
                            "handler_symbol": handler_symbol,
                        }
                    )
                if routes:
                    results.append((cls, routes))
        return results

    def _extract_functional(
        self, ctx, service, route, details, converter, normalized, raw_path
    ) -> list[OperationExtraction]:
        handler_cls: JavaClass | None = details["cls"]
        handler_method: JavaMethod | None = details["method"]
        unresolved: list[UnresolvedSite] = []
        evidence = [
            Evidence(
                file=route.file,
                start_line=route.start_line,
                end_line=route.start_line,
                kind="call_site",
                symbol=route.handler_symbol,
            )
        ]
        schema: dict = {}
        confidence = Confidence(level="low", reason_code="dynamic_type")
        if handler_method is not None and handler_cls is not None:
            # Mono<ServerResponse> bodies are dynamic; look for bodyValue(x)/body(..., X.class)
            body_class_match = re.search(r"body(?:Value)?\s*\(.*?(\w+)\.class", handler_method.body_text)
            if body_class_match:
                schema, confidence = converter.convert(body_class_match.group(1), handler_cls)
                confidence = Confidence(level="medium", reason_code="inferred_return_flow")
        if not schema:
            unresolved.append(
                UnresolvedSite(
                    service_id=service.id,
                    path=normalized,
                    method=route.methods[0],
                    site="responses/200/content/application~1json/schema",
                    kind="response_schema",
                    reason_code="dynamic_type",
                    evidence=evidence,
                )
            )
        parameters = [
            Parameter(
                name=match.group(1),
                location="path",
                required=True,
                schema={"type": "string"},
                evidence=evidence,
                confidence=Confidence(level="medium", reason_code="framework_default"),
            )
            for match in re.finditer(r"\{([^}/]+)\}", normalized)
        ]
        operation = Operation(
            method=route.methods[0],  # type: ignore[arg-type]
            operation_id=f"{service.id}.{route.handler_symbol}.{route.methods[0]}",
            handler=route.handler_symbol,
            parameters=parameters,
            responses=[
                ResponseVariant(
                    status="200",
                    origin="return_type",
                    content={"application/json": MediaTypeContract(schema=schema)},
                    evidence=evidence,
                    confidence=confidence,
                )
            ],
            summary_hint=handler_method.javadoc if handler_method else None,
            evidence=evidence,
            confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
        )
        return [
            OperationExtraction(
                endpoint_path=normalized, raw_path=raw_path, operation=operation, unresolved=unresolved
            )
        ]

    # -- parameters/body ----------------------------------------------------------

    def _parameters(
        self,
        ctx: JavaAnalysisContext,
        service: Service,
        converter: JavaTypeConverter,
        cls: JavaClass,
        method: JavaMethod,
        normalized_path: str,
        http_method: str,
        consumes: list[str],
        unresolved: list[UnresolvedSite],
    ) -> tuple[list[Parameter], RequestBody | None, bool]:
        parameters: list[Parameter] = []
        body: RequestBody | None = None
        multipart_parts: list[tuple[str, dict, bool]] = []
        has_validation = False
        sig_evidence = Evidence(
            file=cls.file,
            start_line=method.start_line,
            end_line=method.end_line,
            kind="signature",
            symbol=f"{cls.qualified}.{method.name}",
        )
        path_params = set(re.findall(r"\{([^}/:]+)", normalized_path))

        for param in method.params:
            base_type = split_generic(param.type_text)[0].rsplit(".", 1)[-1]
            if base_type in _SKIP_PARAM_TYPES:
                continue
            annotation_names = {a.name for a in param.annotations}
            if "Valid" in annotation_names or "Validated" in annotation_names:
                has_validation = True
            if "AuthenticationPrincipal" in annotation_names:
                continue

            def get(name: str) -> JavaAnnotation | None:
                return next((a for a in param.annotations if a.name == name), None)

            path_variable = get("PathVariable")
            request_param = get("RequestParam")
            request_header = get("RequestHeader")
            cookie_value = get("CookieValue")
            request_body_ann = get("RequestBody")
            request_part = get("RequestPart")
            model_attribute = get("ModelAttribute")

            if path_variable is not None or (param.name in path_params and not annotation_names & {"RequestParam", "RequestBody", "RequestHeader"}):
                name = (path_variable.value or path_variable.kw("name") or path_variable.kw("value") if path_variable else None) or param.name
                schema, confidence = converter.convert(param.type_text, cls)
                parameters.append(
                    Parameter(
                        name=name, location="path", required=True,
                        schema=schema or {"type": "string"},
                        evidence=[sig_evidence], confidence=confidence,
                    )
                )
            elif request_param is not None or (
                base_type == "MultipartFile" and request_part is None
            ):
                if base_type == "MultipartFile":
                    item = {"type": "string", "format": "binary"}
                    required = request_param is None or request_param.kw("required") != "false"
                    part_name = param.name
                    if request_param is not None:
                        part_name = (
                            request_param.value
                            or request_param.kw("name")
                            or request_param.kw("value")
                            or param.name
                        )
                    multipart_parts.append((part_name, item, required))
                    continue
                name = request_param.value or request_param.kw("name") or request_param.kw("value") or param.name
                schema, confidence = converter.convert(param.type_text, cls)
                required = request_param.kw("required") != "false" and request_param.kw("defaultValue") is None
                default = request_param.kw("defaultValue")
                if default is not None and "$ref" not in schema:
                    schema.setdefault("default", _coerce_default(default, schema))
                if not schema:
                    unresolved.append(UnresolvedSite(
                        service_id=service.id, path=normalized_path, method=http_method,
                        site=f"parameters/{name}/schema", kind="parameter_type",
                        reason_code=confidence.reason_code, evidence=[sig_evidence],
                    ))
                parameters.append(Parameter(
                    name=name, location="query", required=required,
                    schema=schema, default_repr=default,
                    evidence=[sig_evidence], confidence=confidence,
                ))
            elif request_header is not None:
                name = request_header.value or request_header.kw("name") or request_header.kw("value") or param.name
                schema, confidence = converter.convert(param.type_text, cls)
                parameters.append(Parameter(
                    name=name, location="header",
                    required=request_header.kw("required") != "false" and request_header.kw("defaultValue") is None,
                    schema=schema, evidence=[sig_evidence], confidence=confidence,
                ))
            elif cookie_value is not None:
                name = cookie_value.value or cookie_value.kw("name") or cookie_value.kw("value") or param.name
                schema, confidence = converter.convert(param.type_text, cls)
                parameters.append(Parameter(
                    name=name, location="cookie",
                    required=cookie_value.kw("required") != "false",
                    schema=schema, evidence=[sig_evidence], confidence=confidence,
                ))
            elif request_body_ann is not None:
                schema, confidence = converter.convert(param.type_text, cls)
                media = consumes[0] if consumes else "application/json"
                if not schema:
                    unresolved.append(UnresolvedSite(
                        service_id=service.id, path=normalized_path, method=http_method,
                        site=f"request_body/content/{media.replace('/', '~1')}/schema",
                        kind="request_schema", reason_code=confidence.reason_code,
                        evidence=[sig_evidence],
                    ))
                body = RequestBody(
                    required=request_body_ann.kw("required") != "false",
                    content={media: MediaTypeContract(schema=schema)},
                    evidence=[sig_evidence], confidence=confidence,
                )
            elif request_part is not None:
                name = request_part.value or request_part.kw("name") or request_part.kw("value") or param.name
                if base_type == "MultipartFile":
                    schema: dict = {"type": "string", "format": "binary"}
                else:
                    schema, _confidence = converter.convert(param.type_text, cls)
                multipart_parts.append((name, schema, request_part.kw("required") != "false"))
            elif base_type == "Pageable":
                for page_param, page_schema in (
                    ("page", {"type": "integer", "minimum": 0, "default": 0}),
                    ("size", {"type": "integer", "minimum": 1, "default": 20}),
                    ("sort", {"type": "array", "items": {"type": "string"}}),
                ):
                    parameters.append(Parameter(
                        name=page_param, location="query", required=False, schema=page_schema,
                        evidence=[sig_evidence],
                        confidence=Confidence(level="medium", reason_code="framework_default"),
                    ))
            elif model_attribute is not None or (
                http_method == "get" and self._is_dto(ctx, cls, param)
            ):
                # DTO expanded into query parameters
                dto = ctx.index.resolve(param.type_text, cls)
                if dto is not None:
                    for dto_field in dto.fields:
                        if "static" in dto_field.modifiers:
                            continue
                        schema, confidence = converter.convert(dto_field.type_text, dto)
                        parameters.append(Parameter(
                            name=dto_field.name, location="query", required=False,
                            schema=schema,
                            evidence=[sig_evidence],
                            confidence=Confidence(level="medium", reason_code="inferred_serializer"),
                        ))

        if multipart_parts:
            properties = {name: schema for name, schema, _req in multipart_parts}
            required_names = sorted(name for name, _s, req in multipart_parts if req)
            body_schema: dict = {"type": "object", "properties": properties}
            if required_names:
                body_schema["required"] = required_names
            existing_json = None
            if body is not None:
                existing_json = body  # @RequestPart JSON alongside files
                for media, contract in existing_json.content.items():
                    properties.setdefault("body", contract.schema_)
            body = RequestBody(
                required=bool(required_names),
                content={"multipart/form-data": MediaTypeContract(schema=body_schema)},
                evidence=[sig_evidence],
                confidence=Confidence(level="high", reason_code="declared_annotation"),
            )
        return parameters, body, has_validation

    def _is_dto(self, ctx: JavaAnalysisContext, cls: JavaClass, param: JavaParam) -> bool:
        if param.annotations:
            return False
        resolved = ctx.index.resolve(param.type_text, cls)
        return resolved is not None and resolved.kind in ("class", "record") and bool(resolved.fields or resolved.record_components)

    # -- responses ----------------------------------------------------------------

    def _responses(
        self,
        ctx: JavaAnalysisContext,
        service: Service,
        state: SpringState,
        converter: JavaTypeConverter,
        cls: JavaClass,
        method: JavaMethod,
        produces: list[str],
        normalized_path: str,
        http_method: str,
        has_validation: bool,
        has_inputs: bool,
        unresolved: list[UnresolvedSite],
    ) -> list[ResponseVariant]:
        variants: list[ResponseVariant] = []
        annotation_evidence = Evidence(
            file=cls.file, start_line=method.start_line, end_line=method.end_line,
            kind="annotation", symbol=f"{cls.qualified}.{method.name}",
        )
        status = 200
        response_status = next((a for a in method.annotations if a.name == "ResponseStatus"), None)
        if response_status is not None:
            code = _http_status(response_status.value or response_status.kw("value") or response_status.kw("code") or "")
            if code is not None:
                status = code

        return_type = method.return_type.strip()
        base_return = split_generic(return_type)[0].rsplit(".", 1)[-1]
        is_flux = base_return == "Flux"
        media_types = produces or ["application/json"]

        # ResponseEntity / Mono builders in the body carry the real success
        # status and whether a body is written (e.g. .created(..).build() => 201
        # no-content + Location; .noContent() => 204). Source-grounded inference.
        builder_status, is_created, builder_bodyless = _infer_response_entity(method.body_text)
        if response_status is None and builder_status is not None:
            status = builder_status

        success_headers: dict[str, HeaderSpec] = {}
        if is_created:
            success_headers["Location"] = HeaderSpec(
                name="Location",
                schema={"type": "string", "format": "uri"},
                required=True,
                evidence=[annotation_evidence],
                confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
            )

        effective_type = _strip_body_wrappers(return_type)
        is_bodyless = effective_type in ("void", "Void") or builder_bodyless or status == 204
        if is_bodyless:
            # No response body: emit a bodyless response (never an empty {} schema).
            # 204/201-no-content/202 come straight from the annotated/inferred status.
            variants.append(ResponseVariant(
                status=str(status),
                origin="annotation" if response_status else "return_type",
                content={},
                headers=success_headers,
                evidence=[annotation_evidence],
                confidence=Confidence(
                    level="high" if response_status else "medium",
                    reason_code="declared_annotation" if response_status else "inferred_return_flow",
                ),
            ))
        else:
            schema, confidence = converter.convert(return_type, cls)
            # binary bodies (Resource / byte[] / StreamingResponseBody) must not
            # be advertised as application/json when no explicit produces is set.
            if not produces and _is_binary_schema(schema):
                media_types = ["application/octet-stream"]
            if not schema and base_return not in ("String",):
                unresolved.append(UnresolvedSite(
                    service_id=service.id, path=normalized_path, method=http_method,
                    site=f"responses/{status}/content/{media_types[0].replace('/', '~1')}/schema",
                    kind="response_schema", reason_code=confidence.reason_code,
                    evidence=[annotation_evidence],
                ))
            content = {}
            for media in media_types:
                media_schema = schema
                if media in ("text/plain", "text/html", "text/csv") and not schema:
                    media_schema = {"type": "string"}
                content[media] = MediaTypeContract(schema=media_schema)
            variants.append(ResponseVariant(
                status=str(status),
                origin="annotation" if response_status else "return_type",
                content=content,
                headers=success_headers,
                evidence=[annotation_evidence],
                confidence=confidence,
            ))

        # exception flow: direct throws + one level into invoked service methods
        throw_sites = list(method.throw_sites)
        seen_methods = {f"{cls.qualified}.{method.name}"}
        depth_budget = max(1, ctx.config.analysis.call_graph_max_depth)
        frontier = [(cls, method)]
        for _ in range(depth_budget):
            next_frontier = []
            for owner, owner_method in frontier:
                for invocation in owner_method.invocations:
                    for target_cls, target_method in _resolve_invocation(ctx, owner, invocation):
                        key = f"{target_cls.qualified}.{target_method.name}"
                        if key in seen_methods:
                            continue
                        seen_methods.add(key)
                        throw_sites.extend(target_method.throw_sites)
                        next_frontier.append((target_cls, target_method))
            frontier = next_frontier

        seen_statuses: set[str] = set()
        for site in throw_sites:
            exception_simple = split_generic(site.exception_type)[0].rsplit(".", 1)[-1]
            status_code, schema, origin, extra_evidence = self._map_exception(
                ctx, state, converter, cls, exception_simple, site
            )
            if status_code is None or str(status_code) in seen_statuses:
                continue
            seen_statuses.add(str(status_code))
            evidence_list = [Evidence(
                file=cls.file, start_line=site.line, end_line=site.line,
                kind="raise_stmt", symbol=f"{cls.qualified}.{method.name}",
            )]
            evidence_list.extend(extra_evidence)
            if schema is None:
                content: dict = {}
            elif not schema:
                # empty error shape → normalize to the one shared error envelope
                content = {"application/json": MediaTypeContract(
                    schema=self._error_schema(state, converter, service, evidence_list[0])
                )}
            else:
                content = {"application/json": MediaTypeContract(schema=schema)}
            variants.append(ResponseVariant(
                status=str(status_code),
                origin=origin,  # type: ignore[arg-type]
                condition=Condition(kind="exception_handled", exception_type=site.exception_type),
                content=content,
                evidence=evidence_list,
                confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
            ))

        if has_validation:
            error_schema = self._error_schema(state, converter, service, annotation_evidence)
            variants.append(ResponseVariant(
                status="400",
                origin="framework_default",
                description_hint="Validation failure",
                content={"application/json": MediaTypeContract(schema=error_schema)},
                evidence=[annotation_evidence],
                confidence=Confidence(level="medium", reason_code="framework_default"),
            ))
        return variants

    def _error_schema(
        self, state: SpringState, converter: JavaTypeConverter, service: Service, evidence: Evidence
    ) -> dict:
        """The one canonical error envelope for this service.

        Prefers the type returned by a ``@ControllerAdvice`` handler (the real
        error shape Spring emits, e.g. shared ``ErrorResponse``); otherwise
        interns a single shared ``Error`` envelope so every error response
        references one component instead of duplicating inline objects.
        """
        cached = state.error_schema_by_service.get(service.id)
        if cached is not None:
            return dict(cached)
        chosen: dict | None = None
        for advice_cls, handler, _exc, _status in state.advice_handlers:
            schema, _confidence = converter.convert(handler.return_type, advice_cls)
            if isinstance(schema, dict) and "$ref" in schema:
                # prefer an error-named type when several advice handlers exist
                if chosen is None or "error" in schema["$ref"].lower():
                    chosen = schema
        if chosen is None:
            envelope = {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string", "format": "date-time"},
                    "status": {"type": "integer"},
                    "error": {"type": "string"},
                    "message": {"type": "string"},
                    "path": {"type": "string"},
                },
            }
            ref = converter.registry.intern(
                None, envelope, [evidence],
                Confidence(level="medium", reason_code="framework_default"),
                service.id, synthetic_name="Error",
            )
            chosen = {"$ref": ref}
        state.error_schema_by_service[service.id] = chosen
        return dict(chosen)

    def _map_exception(self, ctx, state: SpringState, converter, cls, exception_simple: str, site):
        # ResponseStatusException(HttpStatus.X, ...)
        if exception_simple == "ResponseStatusException" and site.arg_text:
            match = _STATUS_IN_BODY_RE.search(site.arg_text)
            if match and match.group(1) in _HTTP_STATUS_NAMES:
                return _HTTP_STATUS_NAMES[match.group(1)], {}, "raise_site", []
            return None, None, "raise_site", []
        # exception class with @ResponseStatus
        exception_cls = ctx.index.resolve(exception_simple, cls)
        if exception_cls is not None:
            response_status = exception_cls.find_annotation("ResponseStatus")
            if response_status is not None:
                code = _http_status(response_status.value or response_status.kw("value") or response_status.kw("code") or "")
                if code is not None:
                    return code, {}, "raise_site", [Evidence(
                        file=exception_cls.file, start_line=exception_cls.start_line,
                        end_line=exception_cls.start_line, kind="class_def",
                        symbol=exception_cls.qualified,
                    )]
        # @ControllerAdvice handler
        for advice_cls, handler, handled_exception, status in state.advice_handlers:
            if handled_exception == exception_simple and status is not None:
                schema, _confidence = converter.convert(handler.return_type, advice_cls)
                return status, schema, "controller_advice", [Evidence(
                    file=advice_cls.file, start_line=handler.start_line,
                    end_line=handler.end_line, kind="exception_handler",
                    symbol=f"{advice_cls.qualified}.{handler.name}",
                )]
        return None, None, "raise_site", []

    # -- advice & security ---------------------------------------------------------

    def _collect_advice(self, ctx: JavaAnalysisContext, state: SpringState) -> None:
        if state.advice_handlers:
            return
        for cls in ctx.index.classes.values():
            if cls.find_annotation("ControllerAdvice", "RestControllerAdvice") is None:
                continue
            for method in cls.methods:
                handler = next((a for a in method.annotations if a.name == "ExceptionHandler"), None)
                if handler is None:
                    continue
                exceptions = [e.replace(".class", "").rsplit(".", 1)[-1] for e in handler.kw_list("value")]
                if not exceptions and handler.value:
                    exceptions = [handler.value.replace(".class", "").rsplit(".", 1)[-1]]
                status = None
                response_status = next((a for a in method.annotations if a.name == "ResponseStatus"), None)
                if response_status is not None:
                    status = _http_status(response_status.value or response_status.kw("value") or response_status.kw("code") or "")
                if status is None:
                    match = _STATUS_IN_BODY_RE.search(method.body_text)
                    if match and match.group(1) in _HTTP_STATUS_NAMES:
                        status = _HTTP_STATUS_NAMES[match.group(1)]
                for exception_name in exceptions:
                    state.advice_handlers.append((cls, method, exception_name, status))

    def _collect_security(
        self, ctx: JavaAnalysisContext, state: SpringState, module_roots: dict[str, str]
    ) -> None:
        if state.security_models:
            return
        for cls in ctx.index.classes.values():
            for method in cls.methods:
                if not method.return_type.startswith("SecurityFilterChain"):
                    continue
                body = method.body_text
                evidence = Evidence(
                    file=cls.file, start_line=method.start_line, end_line=method.end_line,
                    kind="filter_chain", symbol=f"{cls.qualified}.{method.name}",
                )
                scheme = _detect_scheme(cls, body, evidence)
                if scheme is None:
                    continue
                permit_all, authenticated_patterns, default_authenticated = _parse_authorize_rules(body)
                root, _name = _owning_module(cls.file, module_roots)
                state.security_models.append(SecurityModel(
                    scheme_id=scheme[0], decl=scheme[1], root=root,
                    permit_all=permit_all, authenticated_patterns=authenticated_patterns,
                    default_authenticated=default_authenticated, config_evidence=evidence,
                ))

    def _security(
        self, ctx, state: SpringState, service: Service, cls: JavaClass,
        method: JavaMethod, path: str,
    ) -> list[SecurityEvidence]:
        annotations = [
            a for a in list(method.annotations) + list(cls.annotations)
            if a.name in ("PreAuthorize", "Secured", "RolesAllowed")
        ]
        model = state.security_by_service.get(service.id)
        if model is None:
            if annotations:
                ctx.warnings.emit(
                    "W402",
                    f"security annotation on {cls.name}.{method.name} but no provable "
                    "authentication scheme (no SecurityFilterChain evidence); omitted",
                    file=cls.file,
                    start_line=method.start_line,
                    service_id=service.id,
                )
            return []

        # explicit permitAll always wins — never mark a public route as secured
        if _ant_matches_any(path, model.permit_all):
            return []
        secured = (
            bool(annotations)
            or model.default_authenticated
            or _ant_matches_any(path, model.authenticated_patterns)
        )
        if not secured:
            return []

        if annotations:
            annotation = annotations[0]
            mechanism = "annotation"
            ev = Evidence(
                file=cls.file, start_line=annotation.line, end_line=annotation.line,
                kind="annotation", symbol=f"{cls.qualified}.{method.name}",
            )
        else:
            mechanism = "filter_chain_config"
            ev = model.config_evidence  # type: ignore[assignment]
        return [SecurityEvidence(
            scheme_id=model.scheme_id,
            scopes=[],
            mechanism=mechanism,  # type: ignore[arg-type]
            evidence=[ev] if ev is not None else [],
            confidence=Confidence(level="high", reason_code="declared_annotation"),
        )]


class SpringMvcAdapter(_SpringBase):
    name = "spring-mvc"
    webflux = False

    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        has_web, has_webflux, has_annotations = _spring_signals(facts)
        score = 0.0
        rationale = []
        if has_web:
            score += 0.55
            rationale.append("spring-web in build")
        if has_annotations:
            score += 0.35
            rationale.append("@RestController present")
        if not has_web and facts.import_hits.get("org.springframework.web") and not facts.import_hits.get("org.springframework.web.reactive"):
            score += 0.4
            rationale.append("spring-web imports")
        if has_webflux and not has_web:
            score = min(score, 0.3)
        return DetectionResult(score=min(score, 0.95), rationale="; ".join(rationale))


class SpringWebFluxAdapter(_SpringBase):
    name = "spring-webflux"
    webflux = True

    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        has_web, has_webflux, has_annotations = _spring_signals(facts)
        score = 0.0
        rationale = []
        if has_webflux:
            score += 0.6
            rationale.append("spring-webflux in build")
        if facts.import_hits.get("org.springframework.web.reactive"):
            score += 0.3
            rationale.append("reactive imports")
        if facts.annotation_hits.get("RouterFunction"):
            score += 0.1
            rationale.append("RouterFunction usage")
        if has_web and not has_webflux:
            score = 0.0
        return DetectionResult(score=min(score, 0.95), rationale="; ".join(rationale))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _module_roots(facts: RepoFacts) -> dict[str, str]:
    """module root dir -> service name (from pom artifactId / dir name)."""
    roots: dict[str, str] = {}
    for manifest in facts.manifests:
        if manifest.kind == "pom" and manifest.packaging != "pom":
            root = manifest.path.rsplit("/", 1)[0] if "/" in manifest.path else ""
            roots[root] = manifest.artifact_id or (root.rsplit("/", 1)[-1] if root else "app")
        elif manifest.kind == "gradle":
            root = manifest.path.rsplit("/", 1)[0] if "/" in manifest.path else ""
            roots.setdefault(root, root.rsplit("/", 1)[-1] if root else "app")
    if not roots:
        roots[""] = "app"
    return roots


def _owning_module(file: str, roots: dict[str, str]) -> tuple[str, str]:
    best = ""
    for root in roots:
        if root and (file == root or file.startswith(root + "/")):
            if len(root) > len(best):
                best = root
    return best, roots.get(best, "app")


def _service_id(root: str, name: str) -> str:
    slug = (root.replace("/", "-") if root else name or "app").strip("-") or "app"
    return slug


def _spring_version(facts: RepoFacts) -> str | None:
    for key in ("parent:org.springframework.boot:spring-boot-starter-parent",):
        version = facts.dep_version(key)
        if version:
            return version
    return None


def _java_build_system(facts: RepoFacts) -> str | None:
    kinds = {m.kind for m in facts.manifests}
    if "pom" in kinds:
        return "maven"
    if "gradle" in kinds:
        return "gradle"
    return None


def _context_path(ctx: JavaAnalysisContext, root: str, webflux: bool) -> str:
    key = "spring.webflux.base-path" if webflux else "server.servlet.context-path"
    for rel in ctx.repo_facts.config_files:
        if root and not rel.startswith(root):
            continue
        if rel.endswith(".properties"):
            try:
                for line in (ctx.repo_root / rel).read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip().startswith(key + "="):
                        return line.split("=", 1)[1].strip()
            except OSError:
                continue
        elif rel.endswith((".yml", ".yaml")):
            try:
                from ruamel.yaml import YAML

                data = YAML(typ="safe").load((ctx.repo_root / rel).read_text(encoding="utf-8", errors="replace"))
                node = data or {}
                for part in key.split("."):
                    if not isinstance(node, dict) or part not in node:
                        node = None
                        break
                    node = node[part]
                if isinstance(node, str):
                    return node
            except Exception:  # noqa: BLE001
                continue
    return ""


def _annotation_paths(annotation: JavaAnnotation | None) -> list[str]:
    if annotation is None:
        return []
    paths = annotation.kw_list("value") or annotation.kw_list("path")
    if not paths and annotation.value:
        paths = [annotation.value]
    return [p for p in paths if p is not None]


def _media_types(annotation: JavaAnnotation | None, key: str) -> list[str]:
    if annotation is None:
        return []
    values = annotation.kw_list(key)
    resolved = []
    for value in values:
        constant = value.rsplit(".", 1)[-1]
        resolved.append(_MEDIA_TYPE_CONSTANTS.get(constant, value if "/" in value else "application/json"))
    return resolved


def _request_mapping_methods(annotation: JavaAnnotation) -> list[str]:
    methods = annotation.kw_list("method")
    result = []
    for method in methods:
        name = method.rsplit(".", 1)[-1].lower()
        if name in ("get", "post", "put", "delete", "patch", "head", "options", "trace"):
            result.append(name)
    return result or ["get"]


def _join(prefix: str, path: str) -> str:
    joined = "/" + "/".join(part for part in (prefix.strip("/") + "/" + path.strip("/")).split("/") if part)
    return joined if joined != "//" else "/"


def _apply_base(service: Service, raw_path: str) -> str:
    base = service.base_paths[0] if service.base_paths else ""
    return _join(base, raw_path) if base else raw_path


def _normalize_spring_path(raw: str) -> str:
    # {id:\d+} -> {id}; matrix/regex parts stripped from the template
    return re.sub(r"\{([^}/:]+)(?::[^}]*)?\}", r"{\1}", raw) or "/"


def _http_status(text: str) -> int | None:
    name = text.rsplit(".", 1)[-1].strip()
    return _HTTP_STATUS_NAMES.get(name)


def _coerce_default(text: str, schema: dict):
    if schema.get("type") == "integer":
        try:
            return int(text)
        except ValueError:
            return text
    if schema.get("type") == "number":
        try:
            return float(text)
        except ValueError:
            return text
    if schema.get("type") == "boolean":
        return text.lower() == "true"
    return text


def _resolve_invocation(ctx: JavaAnalysisContext, owner: JavaClass, invocation: str):
    """Find candidate (class, method) targets for a method invocation via the
    owner's injected fields and own methods."""
    results = []
    for method in owner.methods:
        if method.name == invocation:
            results.append((owner, method))
    for field in owner.fields:
        target = ctx.index.resolve(field.type_text, owner)
        if target is not None:
            for method in target.methods:
                if method.name == invocation:
                    results.append((target, method))
    return results


_BODY_WRAPPERS = {
    "ResponseEntity", "HttpEntity", "Mono", "CompletableFuture",
    "CompletionStage", "Callable", "DeferredResult", "Optional",
}
_RE_BUILDER = re.compile(
    r"ResponseEntity\s*\.\s*(created|accepted|noContent|ok|status)\b"
)
_RE_STATUS_ARG = re.compile(r"\.status\(\s*(?:HttpStatus\.)?([A-Z_]+|\d{3})")


def _strip_body_wrappers(type_text: str) -> str:
    """Unwrap ResponseEntity/Mono/... to the innermost body type text."""
    text = type_text.strip()
    for _ in range(6):
        base, args = split_generic(text)
        simple = base.rsplit(".", 1)[-1]
        if simple in _BODY_WRAPPERS and args:
            text = args[0].strip()
            continue
        break
    return text.strip()


def _infer_response_entity(body_text: str) -> tuple[int | None, bool, bool]:
    """(success_status, is_created, bodyless) inferred from ResponseEntity builders."""
    statuses: set[int] = set()
    is_created = False
    for match in _RE_BUILDER.finditer(body_text):
        kind = match.group(1)
        if kind == "created":
            statuses.add(201)
            is_created = True
        elif kind == "accepted":
            statuses.add(202)
        elif kind == "noContent":
            statuses.add(204)
        elif kind == "ok":
            statuses.add(200)
    for match in _RE_STATUS_ARG.finditer(body_text):
        token = match.group(1)
        code = _http_status(token) if not token.isdigit() else int(token)
        if code is not None and 200 <= code < 300:
            statuses.add(code)
    has_body = bool(re.search(r"\.\s*body\s*\(\s*[^)\s]", body_text))
    build_only = bool(re.search(r"\.\s*build\s*\(\s*\)", body_text))
    bodyless = (204 in statuses) or (build_only and not has_body and (is_created or 202 in statuses))
    success: int | None = None
    for preferred in (201, 202, 204):
        if preferred in statuses:
            success = preferred
            break
    if success is None:
        two_xx = sorted(s for s in statuses if 200 <= s < 300)
        success = two_xx[0] if two_xx else None
    return success, is_created, bodyless


def _is_binary_schema(schema: dict) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "string" and schema.get("format") == "binary"


def _model_for_root(models: list[SecurityModel], root: str) -> SecurityModel | None:
    """The security config owned by ``root`` (or its nearest ancestor module).

    Falls back to a single repo-wide config (root ``""``) when a module has no
    config of its own — a common shared-security setup.
    """
    if not models:
        return None
    # nearest ancestor whose root is a prefix of this module's root
    candidates = [
        m for m in models
        if m.root == root or (m.root and root.startswith(m.root + "/")) or m.root == ""
    ]
    if candidates:
        return max(candidates, key=lambda m: len(m.root))
    return models[0] if len(models) == 1 else None


_ADD_FILTER_RE = re.compile(r"\.addFilter(?:Before|After|At)\s*\(\s*(?:new\s+)?([A-Za-z_]\w*)")
# no word boundaries: must match camelCase identifiers like JwtAuthenticationFilter
_JWT_HINT_RE = re.compile(r"(?i)(jwt|bearer|token)")


def _detect_scheme(
    cls: JavaClass, body: str, evidence: Evidence
) -> tuple[str, SecuritySchemeDecl] | None:
    """Prove an authentication scheme from a SecurityFilterChain body.

    Recognizes resource-server JWT, HTTP Basic, and custom bearer-token filters
    wired via ``addFilterBefore/After`` (the common ``JwtAuthenticationFilter``
    pattern). Never invents a scheme it cannot name from source.
    """
    if ".oauth2ResourceServer(" in body and "jwt" in body:
        return "bearerAuth", SecuritySchemeDecl(
            scheme_id="bearerAuth", kind="http_bearer",
            detail={"bearerFormat": "JWT"}, evidence=[evidence],
        )
    if ".httpBasic(" in body:
        return "basicAuth", SecuritySchemeDecl(
            scheme_id="basicAuth", kind="http_basic", evidence=[evidence],
        )
    match = _ADD_FILTER_RE.search(body)
    if match:
        # the injected filter variable name plus the config class name are the
        # provable textual evidence that this is a bearer-token filter chain
        haystack = f"{match.group(1)} {cls.name} {body}"
        if _JWT_HINT_RE.search(haystack):
            fmt = "JWT" if re.search(r"(?i)jwt", haystack) else None
            detail = {"bearerFormat": fmt} if fmt else {}
            return "bearerAuth", SecuritySchemeDecl(
                scheme_id="bearerAuth", kind="http_bearer",
                detail=detail, evidence=[evidence],
            )
    return None


_AUTHORIZE_RULE_RE = re.compile(
    r"\.\s*(?:requestMatchers|antMatchers|mvcMatchers|regexMatchers)\s*\((?P<args>[^)]*)\)"
    r"\s*\.\s*(?P<action>permitAll|authenticated|fullyAuthenticated|denyAll|"
    r"hasRole|hasAnyRole|hasAuthority|hasAnyAuthority|access)\b"
)
_QUOTED_RE = re.compile(r"\"([^\"]+)\"")


def _parse_authorize_rules(body: str) -> tuple[list[str], list[str], bool]:
    """(permit_all_patterns, authenticated_patterns, any_request_authenticated)."""
    permit_all: list[str] = []
    authenticated: list[str] = []
    for match in _AUTHORIZE_RULE_RE.finditer(body):
        patterns = _QUOTED_RE.findall(match.group("args"))
        if not patterns:
            continue
        action = match.group("action")
        if action == "permitAll":
            permit_all.extend(patterns)
        elif action != "denyAll":  # authenticated / hasRole / hasAuthority / access
            authenticated.extend(patterns)
    default_authenticated = bool(
        re.search(r"anyRequest\s*\(\s*\)\s*\.\s*(?:authenticated|fullyAuthenticated|hasRole|hasAnyRole|hasAuthority|hasAnyAuthority|access)\s*\(", body)
    )
    return permit_all, authenticated, default_authenticated


def _ant_to_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern)
    # order matters: ** before *
    escaped = escaped.replace(r"\*\*", "\x00").replace(r"\*", "[^/]*").replace("\x00", ".*")
    escaped = escaped.replace(r"\?", "[^/]")
    return re.compile("^" + escaped + "$")


def _ant_matches_any(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if _ant_to_regex(pattern).match(path):
            return True
        if pattern.endswith("/**"):
            base = pattern[:-3]
            if path == base or path.startswith(base + "/"):
                return True
    return False


def _tag_for_class(cls: JavaClass) -> str | None:
    tag_annotation = cls.find_annotation("Tag")
    if tag_annotation is not None:
        name = tag_annotation.kw("name") or tag_annotation.value
        if name:
            return name
    name = cls.name.rsplit(".", 1)[-1]
    for suffix in ("Controller", "Resource", "Api"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].lower()
    return name.lower()
