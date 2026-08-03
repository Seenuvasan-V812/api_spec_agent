"""JAX-RS / Jakarta REST adapter.

Handles ``@Path`` composition (class + method), ``@GET``/``@POST``/... verbs,
``@Produces``/``@Consumes`` (class and method level), ``@PathParam``/
``@QueryParam``/``@HeaderParam``/``@CookieParam``/``@FormParam``/
``@DefaultValue``, the unannotated entity body parameter, ``Response`` vs
typed returns, ``WebApplicationException`` subclasses and ``ExceptionMapper``
providers, ``@ApplicationPath`` prefixes, and ``@RolesAllowed`` security
(emitted only when an authentication mechanism is provable).
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
from openapi_agent.analysis.java.ts_scanner import JavaAnnotation, JavaClass, JavaMethod
from openapi_agent.analysis.java.type_schema import JavaTypeConverter, split_generic
from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger
from openapi_agent.models.metadata import (
    Condition,
    Confidence,
    Evidence,
    MediaTypeContract,
    Operation,
    Parameter,
    RequestBody,
    ResponseVariant,
    SecurityEvidence,
    Service,
)

log = get_logger("analysis.java.jaxrs")

_VERBS = {"GET": "get", "POST": "post", "PUT": "put", "DELETE": "delete", "PATCH": "patch", "HEAD": "head", "OPTIONS": "options"}

#: javax/jakarta.ws.rs.core.MediaType constants
_MEDIA_CONSTANTS = {
    "APPLICATION_JSON": "application/json",
    "APPLICATION_XML": "application/xml",
    "TEXT_PLAIN": "text/plain",
    "TEXT_HTML": "text/html",
    "APPLICATION_OCTET_STREAM": "application/octet-stream",
    "APPLICATION_FORM_URLENCODED": "application/x-www-form-urlencoded",
    "MULTIPART_FORM_DATA": "multipart/form-data",
}

_WAE_STATUS = {
    "BadRequestException": 400,
    "NotAuthorizedException": 401,
    "ForbiddenException": 403,
    "NotFoundException": 404,
    "NotAllowedException": 405,
    "NotAcceptableException": 406,
    "NotSupportedException": 415,
    "ClientErrorException": 400,
    "InternalServerErrorException": 500,
    "ServiceUnavailableException": 503,
}

_STATUS_CONST_RE = re.compile(r"(?:Response\.)?Status\.([A-Z_]+)")
_STATUS_NAMES = {
    "OK": 200, "CREATED": 201, "ACCEPTED": 202, "NO_CONTENT": 204,
    "BAD_REQUEST": 400, "UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
    "CONFLICT": 409, "INTERNAL_SERVER_ERROR": 500,
}
_STATUS_INT_RE = re.compile(r"\bstatus\s*\(\s*(\d{3})\s*\)")


@dataclass
class JaxRsState:
    resources: dict[str, list[JavaClass]] = dc_field(default_factory=dict)
    exception_mappers: dict[str, tuple[JavaClass, int | None]] = dc_field(default_factory=dict)
    application_path: str = ""
    has_auth_mechanism: bool = False
    converters: dict[str, JavaTypeConverter] = dc_field(default_factory=dict)


class JaxRsAdapter(FrameworkAdapter):
    name = "jaxrs"
    language = "java"

    def can_handle(self, facts: RepoFacts) -> DetectionResult:
        score = 0.0
        rationale = []
        if facts.import_hits.get("javax.ws.rs") or facts.import_hits.get("jakarta.ws.rs"):
            score += 0.7
            rationale.append("JAX-RS imports")
        deps = facts.manifest_dep_names()
        if any("jersey" in n or "resteasy" in n or "jaxrs" in n or "quarkus-rest" in n for n in deps):
            score += 0.2
            rationale.append("JAX-RS implementation in build")
        if facts.annotation_hits.get("@Path"):
            score += 0.1
            rationale.append("@Path present")
        return DetectionResult(score=min(score, 0.95), rationale="; ".join(rationale))

    # -------------------------------------------------------------- services
    def discover_services(self, ctx: AnalysisContext) -> list[Service]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: JaxRsState = ctx.extras.setdefault("jaxrs_state", JaxRsState())

        resources: list[JavaClass] = []
        for cls in ctx.index.classes.values():
            app_path = cls.find_annotation("ApplicationPath")
            if app_path is not None:
                state.application_path = app_path.value or app_path.kw("value") or ""
            if cls.find_annotation("Path") is not None and cls.kind in ("class", "interface"):
                if any(m.annotations for m in cls.methods):
                    resources.append(cls)
            # ExceptionMapper<X> providers
            for implemented in cls.implements:
                base, args = split_generic(implemented)
                if base.rsplit(".", 1)[-1] == "ExceptionMapper" and args:
                    status = self._mapper_status(cls)
                    state.exception_mappers[args[0].rsplit(".", 1)[-1]] = (cls, status)
        if not resources:
            return []

        # authentication mechanism proof: container security annotations only
        # count when web.xml / a security filter proves a scheme — search for
        # common filter signatures
        for cls in ctx.index.classes.values():
            if any(f.type_text.endswith("ContainerRequestFilter") for f in cls.fields) or any(
                i.rsplit(".", 1)[-1] == "ContainerRequestFilter" for i in cls.implements
            ):
                if "Authorization" in " ".join(m.body_text for m in cls.methods):
                    state.has_auth_mechanism = True

        service_id = "jaxrs-app"
        manifest_names = [m.artifact_id for m in ctx.repo_facts.manifests if m.kind == "pom" and m.artifact_id]
        if manifest_names:
            service_id = manifest_names[0]
        service = Service(
            id=service_id,
            name=service_id,
            language="java",
            framework="jaxrs",
            build_system="maven" if any(m.kind == "pom" for m in ctx.repo_facts.manifests) else None,
            base_paths=[state.application_path] if state.application_path else [],
        )
        state.resources[service_id] = sorted(resources, key=lambda c: c.qualified)
        state.converters[service_id] = JavaTypeConverter(
            ctx.index, ctx.registry, service_id,
            sidecar_facts=ctx.sidecar.types_by_qualified,
            sidecar_available=ctx.sidecar.available,
        )
        return [service]

    def _mapper_status(self, cls: JavaClass) -> int | None:
        for method in cls.methods:
            if method.name == "toResponse":
                match = _STATUS_CONST_RE.search(method.body_text)
                if match and match.group(1) in _STATUS_NAMES:
                    return _STATUS_NAMES[match.group(1)]
                int_match = _STATUS_INT_RE.search(method.body_text)
                if int_match:
                    return int(int_match.group(1))
        return None

    # ---------------------------------------------------------------- routes
    def discover_routes(self, ctx: AnalysisContext, service: Service) -> list[RouteRef]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: JaxRsState = ctx.extras["jaxrs_state"]
        details = ctx.extras.setdefault("jaxrs_route_details", {})
        refs: list[RouteRef] = []
        for cls in state.resources.get(service.id, []):
            class_path_ann = cls.find_annotation("Path")
            class_path = (class_path_ann.value or class_path_ann.kw("value") or "") if class_path_ann else ""
            class_produces = _media_list(cls.find_annotation("Produces"))
            class_consumes = _media_list(cls.find_annotation("Consumes"))
            for method in cls.methods:
                verb = next((v for a in method.annotations if (v := _VERBS.get(a.name))), None)
                if verb is None:
                    continue
                method_path_ann = next((a for a in method.annotations if a.name == "Path"), None)
                method_path = (method_path_ann.value or method_path_ann.kw("value") or "") if method_path_ann else ""
                raw_path = _join(state.application_path, _join(class_path, method_path))
                key = (cls.file, method.start_line, raw_path, verb)
                details[key] = {
                    "cls": cls,
                    "method": method,
                    "produces": _media_list(next((a for a in method.annotations if a.name == "Produces"), None)) or class_produces,
                    "consumes": _media_list(next((a for a in method.annotations if a.name == "Consumes"), None)) or class_consumes,
                }
                refs.append(RouteRef(
                    service_hint=service.id,
                    raw_path=raw_path,
                    methods=[verb],
                    handler_symbol=f"{cls.qualified}.{method.name}",
                    file=cls.file,
                    start_line=method.start_line,
                    kind="annotation",
                ))
        return refs

    # ------------------------------------------------------------ extraction
    def extract_operation(
        self, ctx: AnalysisContext, service: Service, route: RouteRef
    ) -> list[OperationExtraction]:
        assert isinstance(ctx, JavaAnalysisContext)
        state: JaxRsState = ctx.extras["jaxrs_state"]
        converter = state.converters[service.id]
        details = ctx.extras["jaxrs_route_details"].get(
            (route.file, route.start_line, route.raw_path, route.methods[0])
        )
        if details is None:
            return []
        cls: JavaClass = details["cls"]
        method: JavaMethod = details["method"]
        produces: list[str] = details["produces"] or ["application/json"]
        consumes: list[str] = details["consumes"]
        verb = route.methods[0]
        normalized = _normalize_path(route.raw_path)
        unresolved: list[UnresolvedSite] = []

        evidence = [Evidence(
            file=cls.file, start_line=method.start_line, end_line=method.end_line,
            kind="annotation", symbol=f"{cls.qualified}.{method.name}",
        )]
        sig_evidence = Evidence(
            file=cls.file, start_line=method.start_line, end_line=method.end_line,
            kind="signature", symbol=f"{cls.qualified}.{method.name}",
        )

        parameters: list[Parameter] = []
        form_parts: list[tuple[str, dict, bool]] = []
        body: RequestBody | None = None

        for param in method.params:
            def get(name: str) -> JavaAnnotation | None:
                return next((a for a in param.annotations if a.name == name), None)

            default_ann = get("DefaultValue")
            default = (default_ann.value or default_ann.kw("value")) if default_ann else None
            location_map = (
                ("PathParam", "path"),
                ("QueryParam", "query"),
                ("HeaderParam", "header"),
                ("CookieParam", "cookie"),
            )
            matched = False
            for annotation_name, location in location_map:
                annotation = get(annotation_name)
                if annotation is None:
                    continue
                matched = True
                name = annotation.value or annotation.kw("value") or param.name
                schema, confidence = converter.convert(param.type_text, cls)
                if default is not None and "$ref" not in schema:
                    from openapi_agent.analysis.java.adapters.spring import _coerce_default

                    default = _coerce_default(default, schema)
                    schema.setdefault("default", default)
                if not schema and location != "path":
                    unresolved.append(UnresolvedSite(
                        service_id=service.id, path=normalized, method=verb,
                        site=f"parameters/{name}/schema", kind="parameter_type",
                        reason_code=confidence.reason_code, evidence=[sig_evidence],
                    ))
                parameters.append(Parameter(
                    name=name, location=location,  # type: ignore[arg-type]
                    required=location == "path" or (default is None and _has_notnull(param)),
                    schema=schema or ({"type": "string"} if location == "path" else {}),
                    default_repr=default,
                    evidence=[sig_evidence], confidence=confidence,
                ))
                break
            if matched:
                continue
            form_param = get("FormParam")
            if form_param is not None:
                name = form_param.value or form_param.kw("value") or param.name
                schema, _confidence = converter.convert(param.type_text, cls)
                form_parts.append((name, schema or {"type": "string"}, _has_notnull(param)))
                continue
            if get("Context") is not None or get("BeanParam") is not None or get("Suspended") is not None:
                continue
            # unannotated parameter = entity body
            schema, confidence = converter.convert(param.type_text, cls)
            media = consumes[0] if consumes else "application/json"
            if not schema:
                unresolved.append(UnresolvedSite(
                    service_id=service.id, path=normalized, method=verb,
                    site=f"request_body/content/{media.replace('/', '~1')}/schema",
                    kind="request_schema", reason_code=confidence.reason_code,
                    evidence=[sig_evidence],
                ))
            body = RequestBody(
                required=True,
                content={media: MediaTypeContract(schema=schema)},
                evidence=[sig_evidence], confidence=confidence,
            )

        if form_parts:
            properties = {name: schema for name, schema, _r in form_parts}
            required_names = sorted(name for name, _s, req in form_parts if req)
            form_schema: dict = {"type": "object", "properties": properties}
            if required_names:
                form_schema["required"] = required_names
            media = "multipart/form-data" if "multipart/form-data" in consumes else "application/x-www-form-urlencoded"
            body = RequestBody(
                required=bool(required_names),
                content={media: MediaTypeContract(schema=form_schema)},
                evidence=[sig_evidence],
                confidence=Confidence(level="high", reason_code="declared_annotation"),
            )

        # responses
        variants: list[ResponseVariant] = []
        return_type = method.return_type.strip()
        if return_type in ("void", "Void"):
            variants.append(ResponseVariant(
                status="204", origin="return_type", content={},
                evidence=evidence,
                confidence=Confidence(level="high", reason_code="framework_default"),
            ))
        elif split_generic(return_type)[0].rsplit(".", 1)[-1] == "Response":
            status, entity_schema = self._response_from_body(converter, cls, method)
            content = {}
            if entity_schema is not None:
                content = {produces[0]: MediaTypeContract(schema=entity_schema)}
            else:
                unresolved.append(UnresolvedSite(
                    service_id=service.id, path=normalized, method=verb,
                    site=f"responses/{status}/content/{produces[0].replace('/', '~1')}/schema",
                    kind="response_schema", reason_code="dynamic_type",
                    evidence=evidence,
                ))
                content = {produces[0]: MediaTypeContract(schema={})}
            variants.append(ResponseVariant(
                status=str(status), origin="return_type", content=content,
                evidence=evidence,
                confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
            ))
        else:
            schema, confidence = converter.convert(return_type, cls)
            if not schema and return_type not in ("String",):
                unresolved.append(UnresolvedSite(
                    service_id=service.id, path=normalized, method=verb,
                    site=f"responses/200/content/{produces[0].replace('/', '~1')}/schema",
                    kind="response_schema", reason_code=confidence.reason_code,
                    evidence=evidence,
                ))
            if return_type == "String" and not schema:
                schema = {"type": "string"}
            variants.append(ResponseVariant(
                status="200", origin="return_type",
                content={media: MediaTypeContract(schema=schema) for media in produces},
                evidence=evidence, confidence=confidence,
            ))

        # exception flow
        seen_statuses = {v.status for v in variants}
        for site in method.throw_sites:
            simple = split_generic(site.exception_type)[0].rsplit(".", 1)[-1]
            status: int | None = _WAE_STATUS.get(simple)
            origin = "raise_site"
            extra: list[Evidence] = []
            mapper_schema: dict = {}
            if status is None and simple == "WebApplicationException" and site.arg_text:
                match = _STATUS_CONST_RE.search(site.arg_text)
                if match and match.group(1) in _STATUS_NAMES:
                    status = _STATUS_NAMES[match.group(1)]
                else:
                    int_match = re.search(r"\b(\d{3})\b", site.arg_text)
                    if int_match:
                        status = int(int_match.group(1))
            if status is None and simple in state.exception_mappers:
                mapper_cls, mapper_status = state.exception_mappers[simple]
                if mapper_status is not None:
                    status = mapper_status
                    origin = "exception_handler"
                    extra = [Evidence(
                        file=mapper_cls.file, start_line=mapper_cls.start_line,
                        end_line=mapper_cls.start_line, kind="exception_handler",
                        symbol=mapper_cls.qualified,
                    )]
            if status is None or str(status) in seen_statuses:
                continue
            seen_statuses.add(str(status))
            variants.append(ResponseVariant(
                status=str(status),
                origin=origin,  # type: ignore[arg-type]
                condition=Condition(kind="exception_handled", exception_type=site.exception_type),
                content={"application/json": MediaTypeContract(schema=mapper_schema)},
                evidence=[Evidence(
                    file=cls.file, start_line=site.line, end_line=site.line,
                    kind="raise_stmt", symbol=f"{cls.qualified}.{method.name}",
                )] + extra,
                confidence=Confidence(level="medium", reason_code="inferred_return_flow"),
            ))

        # security: @RolesAllowed / @DenyAll etc. only with a provable mechanism
        security: list[SecurityEvidence] = []
        roles_allowed = next(
            (a for a in list(method.annotations) + list(cls.annotations) if a.name == "RolesAllowed"),
            None,
        )
        if roles_allowed is not None:
            if state.has_auth_mechanism:
                scheme_id = "bearerAuth"
                if scheme_id not in service.security_schemes:
                    from openapi_agent.models.metadata import SecuritySchemeDecl

                    service.security_schemes[scheme_id] = SecuritySchemeDecl(
                        scheme_id=scheme_id, kind="http_bearer", detail={},
                    )
                security.append(SecurityEvidence(
                    scheme_id=scheme_id, scopes=[], mechanism="annotation",
                    evidence=[Evidence(
                        file=cls.file, start_line=roles_allowed.line, end_line=roles_allowed.line,
                        kind="annotation", symbol=f"{cls.qualified}.{method.name}",
                    )],
                    confidence=Confidence(level="high", reason_code="declared_annotation"),
                ))
            else:
                ctx.warnings.emit(
                    "W402",
                    f"@RolesAllowed on {cls.name}.{method.name} but no provable auth mechanism; omitted",
                    file=cls.file, start_line=method.start_line, service_id=service.id,
                )

        operation = Operation(
            method=verb,  # type: ignore[arg-type]
            operation_id=f"{service.id}.{cls.qualified}.{method.name}.{verb}",
            handler=f"{cls.qualified}.{method.name}",
            parameters=parameters,
            request_body=body,
            responses=variants,
            security=security,
            tags_hint=[_tag(cls)],
            summary_hint=method.javadoc.split(". ")[0][:200] if method.javadoc else None,
            description_hint=method.javadoc,
            evidence=evidence,
            confidence=Confidence(level="high", reason_code="declared_annotation"),
        )
        return [OperationExtraction(
            endpoint_path=normalized, raw_path=route.raw_path,
            operation=operation, unresolved=unresolved,
        )]

    def _response_from_body(self, converter: JavaTypeConverter, cls: JavaClass, method: JavaMethod) -> tuple[int, dict | None]:
        body = method.body_text
        status = 200
        match = _STATUS_CONST_RE.search(body)
        if match and match.group(1) in _STATUS_NAMES:
            status = _STATUS_NAMES[match.group(1)]
        int_match = _STATUS_INT_RE.search(body)
        if int_match:
            status = int(int_match.group(1))
        if "Response.created(" in body:
            status = 201
        elif "Response.noContent(" in body:
            return 204, None
        entity_match = re.search(r"\.entity\s*\(\s*new\s+(\w+)", body)
        if entity_match:
            schema, _confidence = converter.convert(entity_match.group(1), cls)
            return status, schema
        return status, None


# ---------------------------------------------------------------------------


def _media_list(annotation: JavaAnnotation | None) -> list[str]:
    if annotation is None:
        return []
    values = annotation.kw_list("value")
    if not values and annotation.value:
        values = [annotation.value]
    resolved = []
    for value in values:
        constant = value.rsplit(".", 1)[-1]
        resolved.append(_MEDIA_CONSTANTS.get(constant, value if "/" in value else "application/json"))
    return resolved


def _join(prefix: str, path: str) -> str:
    joined = "/" + "/".join(p for p in (prefix.strip("/") + "/" + path.strip("/")).split("/") if p)
    return joined if joined != "//" else "/"


def _normalize_path(raw: str) -> str:
    return re.sub(r"\{\s*([^}/:]+?)\s*(?::[^}]*)?\}", r"{\1}", raw) or "/"


def _has_notnull(param) -> bool:
    return any(a.name in ("NotNull", "NotBlank", "NotEmpty") for a in param.annotations)


def _tag(cls: JavaClass) -> str:
    name = cls.name.rsplit(".", 1)[-1]
    for suffix in ("Resource", "Controller", "Endpoint", "Api"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].lower()
    return name.lower()
